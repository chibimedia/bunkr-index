#!/usr/bin/env python3
"""
MediaIndex Scraper v4
=====================
CONFIRMED STATUS (tested Feb 2025):
  - fapello.com       → plain requests, 200 OK, full static HTML ✓ EASY
  - bunkr.si/.cr/etc  → 403 to plain requests, needs stealth browser
  - bunkr-albums.io   → 403 to plain requests, needs stealth browser

STRATEGY:
  1. Scrape Fapello first — guaranteed results, populates index fast
  2. Attempt Bunkr via patchright stealth browser
  3. Deduplicate and merge everything into albums.json

Fapello URL structure (confirmed from live HTML):
  - Listing:   fapello.com/page-N/
  - Model page: fapello.com/{slug}/
  - Avatar:    fapello.com/content/X/X/{slug}/1000/{slug}_0001.jpg
  - Video URLs: fapello.com/content/.../mp4 (detected from post text "+N videos")
"""

import json, os, re, time, random, logging, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
OUT_FILE      = Path("albums.json")
CACHE_DIR     = Path("cache")
CACHE_TTL     = 60 * 60 * 5  # 5 hours
MAX_NEW       = int(os.getenv("MAX_ALBUMS", "500"))
DELAY_MIN     = float(os.getenv("DELAY_MIN", "1.0"))
DELAY_MAX     = float(os.getenv("DELAY_MAX", "2.5"))
ENABLE_BUNKR  = os.getenv("ENABLE_BUNKR", "true").lower() != "false"
HEADLESS      = os.getenv("HEADLESS", "true").lower() != "false"

BUNKR_DOMAINS = [
    "bunkr.si", "bunkr.cr", "bunkr.fi", "bunkr.ph", "bunkr.pk",
    "bunkr.ps", "bunkr.ws", "bunkr.black", "bunkr.red",
    "bunkr.media", "bunkr.site", "bunkr.ac", "bunkr.ci", "bunkr.sk",
]

FAPELLO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://fapello.com/",
}

CACHE_DIR.mkdir(exist_ok=True)

# ── Cache ─────────────────────────────────────────────────────────────────────
def cache_key(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".html")

def cache_valid(p: Path) -> bool:
    if not p.exists(): return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return age < timedelta(seconds=CACHE_TTL)

def cache_read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")

def cache_write(p: Path, html: str):
    p.write_text(html, encoding="utf-8")

# ── Persistence ───────────────────────────────────────────────────────────────
def load_existing() -> dict[str, dict]:
    if OUT_FILE.exists():
        try:
            data = json.loads(OUT_FILE.read_text())
            existing = {a["id"]: a for a in data.get("albums", [])}
            log.info(f"Loaded {len(existing)} existing albums")
            return existing
        except Exception as e:
            log.warning(f"Could not load {OUT_FILE}: {e}")
    return {}

def save(albums_by_id: dict, new_count: int):
    albums = sorted(
        albums_by_id.values(),
        key=lambda a: a.get("date") or a.get("indexed_at") or "",
        reverse=True,
    )
    OUT_FILE.write_text(json.dumps({
        "meta": {
            "total": len(albums),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "new_this_run": new_count,
            "sources": list({a.get("source", "?") for a in albums}),
        },
        "albums": albums,
    }, ensure_ascii=False, indent=2))
    log.info(f"✓ Saved {len(albums)} total albums ({new_count} new this run)")

# ── HTTP (plain requests — only for sites that work without stealth) ──────────
_session = requests.Session()
_session.headers.update(FAPELLO_HEADERS)

def http_get(url: str, retries: int = 3, use_cache: bool = True) -> Optional[str]:
    cp = cache_key(url)
    if use_cache and cache_valid(cp):
        log.debug(f"Cache hit: {url}")
        return cache_read(cp)

    for attempt in range(1, retries + 1):
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        try:
            r = _session.get(url, timeout=20)
            if r.status_code == 200:
                if use_cache:
                    cache_write(cp, r.text)
                return r.text
            elif r.status_code == 429:
                wait = 30 + random.uniform(5, 15)
                log.warning(f"Rate limited, waiting {wait:.0f}s...")
                time.sleep(wait)
            elif r.status_code == 404:
                log.debug(f"404: {url}")
                return None
            else:
                log.warning(f"HTTP {r.status_code} for {url}")
        except requests.RequestException as e:
            log.warning(f"Request error (attempt {attempt}): {e}")
        time.sleep((2 ** attempt) + random.uniform(0.5, 2))

    return None

# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: FAPELLO — Plain requests, confirmed working
# ═══════════════════════════════════════════════════════════════════════════════

def parse_fapello_page(html: str) -> list[dict]:
    """
    Parse one fapello.com listing page.
    Confirmed HTML structure from live fetch:
      Each post block has:
        - <a href="/slug/"> with avatar img at /content/X/X/slug/1000/slug_0001.jpg
        - Model name in <h2> or link text
        - "+ N photos" / "+ N videos" text for counts
        - Multiple preview thumbnails in the post
    """
    soup = BeautifulSoup(html, "lxml")
    models = {}  # slug → dict (deduplicate multiple posts per model)

    # Find all model links — pattern: fapello.com/{slug}/ where slug is a path component
    for a in soup.find_all("a", href=True):
        href = a["href"]

        # Match model page links: /slug/ or https://fapello.com/slug/
        m = re.match(r"^(?:https://fapello\.com)?/([a-zA-Z0-9_-]{2,60})/$", href)
        if not m:
            continue
        slug = m.group(1)

        # Skip navigation/utility slugs
        skip = {
            "hot", "trending", "popular", "forum", "login", "signup", "welcome",
            "random", "posts", "submit", "videos", "contacts", "activity",
            "daily-search-ranking", "top-likes", "top-followers",
            "popular_videos", "what-is-fapello", "page-2",
        }
        if slug in skip or slug.startswith("page-"):
            continue

        # Find avatar image for this model
        # Fapello pattern: /content/X/X/{slug}/1000/{slug}_0001.jpg
        thumb = None
        # Check for avatar img near this link
        parent = a.parent
        for _ in range(3):  # walk up max 3 levels
            if parent is None:
                break
            img = parent.find("img", src=re.compile(rf"/content/.*{re.escape(slug[:8])}", re.I))
            if img:
                src = img.get("src", "")
                if "/1000/" in src:
                    thumb = src if src.startswith("http") else "https://fapello.com" + src
                break
            parent = parent.parent

        if not thumb:
            # Construct expected avatar URL from confirmed pattern
            s = slug
            if len(s) >= 2:
                thumb = f"https://fapello.com/content/{s[0]}/{s[1]}/{s}/1000/{s}_0001.jpg"

        # Model display name
        name = a.get_text(strip=True) or slug

        # Photo/video counts from post text
        post_block = a.parent
        for _ in range(4):
            if post_block is None:
                break
            text = post_block.get_text()
            photos = sum(int(x) for x in re.findall(r"\+\s*(\d+)\s*photos?", text, re.I))
            videos = sum(int(x) for x in re.findall(r"\+\s*(\d+)\s*videos?", text, re.I))
            if photos or videos:
                break
            post_block = post_block.parent
        else:
            photos, videos = 0, 0

        if slug not in models:
            models[slug] = {
                "id": f"fapello:{slug}",
                "title": name if name and name != slug else slug.replace("-", " ").title(),
                "slug": slug,
                "thumbnail": thumb,
                "url": f"https://fapello.com/{slug}/",
                "source": "fapello",
                "has_videos": videos > 0,
                "file_count": 0,
                "photo_count": 0,
                "video_count": 0,
                "date": None,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            }
        # Accumulate counts (model may appear in multiple posts)
        models[slug]["photo_count"] = models[slug].get("photo_count", 0) + photos
        models[slug]["video_count"] = models[slug].get("video_count", 0) + videos
        models[slug]["file_count"]  = (
            models[slug].get("photo_count", 0) +
            models[slug].get("video_count", 0)
        )
        if videos > 0:
            models[slug]["has_videos"] = True

    return list(models.values())


def scrape_fapello(max_pages: int = 30) -> list[dict]:
    """
    Scrape fapello.com paginated listing. Confirmed: plain requests works.
    Pages: fapello.com/ (page 1), fapello.com/page-2/, fapello.com/page-3/, ...
    """
    all_models: dict[str, dict] = {}
    sources = [
        ("new",     [f"https://fapello.com/" if p == 1 else f"https://fapello.com/page-{p}/" for p in range(1, max_pages + 1)]),
        ("hot",     [f"https://fapello.com/hot/" if p == 1 else f"https://fapello.com/hot/page-{p}/" for p in range(1, 6)]),
        ("popular", [f"https://fapello.com/popular/" if p == 1 else f"https://fapello.com/popular/page-{p}/" for p in range(1, 6)]),
    ]

    for section, urls in sources:
        log.info(f"[Fapello] Scraping '{section}' section ({len(urls)} pages)...")
        consecutive_empty = 0
        for url in urls:
            html = http_get(url)
            if not html:
                log.warning(f"[Fapello] Could not fetch {url}")
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
                continue

            # Check if we actually got content (not an error page)
            if "fapello.com" not in html.lower() or len(html) < 1000:
                log.warning(f"[Fapello] Suspiciously short response from {url}")
                break

            models = parse_fapello_page(html)
            new_on_page = 0
            for m in models:
                slug = m["slug"]
                if slug not in all_models:
                    all_models[slug] = m
                    new_on_page += 1
                else:
                    # Update counts if we got more data
                    if m.get("file_count", 0) > all_models[slug].get("file_count", 0):
                        all_models[slug].update(m)

            log.info(f"[Fapello] {url} → {len(models)} models ({new_on_page} new)")
            if new_on_page == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log.info(f"[Fapello] No new models for 3 pages, moving to next section")
                    break
            else:
                consecutive_empty = 0

            if len(all_models) >= MAX_NEW:
                log.info(f"[Fapello] Reached {MAX_NEW} models, stopping")
                break

        if len(all_models) >= MAX_NEW:
            break

    result = list(all_models.values())
    log.info(f"[Fapello] Total scraped: {len(result)} unique models")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 2: BUNKR — Needs stealth browser (patchright)
# ═══════════════════════════════════════════════════════════════════════════════

class BunkrStealthBrowser:
    """
    Stealth Chromium via patchright.
    patchright patches CDP signals at the protocol level — the main
    thing Cloudflare's bot check looks for.
    """
    INIT_JS = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
        window.chrome = {runtime: {}};
    """

    def __init__(self):
        self._pw  = None
        self._ctx = None

    def start(self):
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            log.error("patchright not installed — skipping Bunkr scraping")
            return False

        log.info(f"[Bunkr] Launching stealth browser (headless={HEADLESS})...")
        Path("browser_profile").mkdir(exist_ok=True)
        self._pw  = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            "browser_profile",
            headless=HEADLESS,
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        self._ctx.add_init_script(self.INIT_JS)
        log.info("[Bunkr] Browser ready.")
        return True

    def stop(self):
        try:
            if self._ctx: self._ctx.close()
            if self._pw:  self._pw.stop()
        except Exception: pass

    def fetch(self, url: str, wait: str = "networkidle",
              retries: int = 3, use_cache: bool = True) -> Optional[str]:
        from patchright.sync_api import TimeoutError as PWTimeout

        cp = cache_key(url)
        if use_cache and cache_valid(cp):
            return cache_read(cp)

        for attempt in range(1, retries + 1):
            page = None
            try:
                page = self._ctx.new_page()
                page.add_init_script(self.INIT_JS)
                time.sleep(random.uniform(0.5, 1.5))

                log.info(f"[Bunkr] GET {url} (attempt {attempt})")
                page.goto(url, wait_until=wait, timeout=45_000)

                # Wait for CF challenge to resolve if present
                content = page.content()
                if "checking your browser" in content.lower() or "just a moment" in content.lower():
                    log.info("[Bunkr] CF challenge — waiting 12s...")
                    time.sleep(12)
                    page.wait_for_load_state("networkidle", timeout=20_000)
                    content = page.content()

                if use_cache and len(content) > 500:
                    cache_write(cp, content)

                time.sleep(random.uniform(2.0, 4.0))
                return content

            except PWTimeout:
                log.warning(f"[Bunkr] Timeout attempt {attempt}")
            except Exception as e:
                log.warning(f"[Bunkr] Error attempt {attempt}: {e}")
            finally:
                try:
                    if page: page.close()
                except: pass
                time.sleep((2 ** attempt) + random.uniform(0.5, 2))

        return None


def scrape_bunkr_albums_io(browser: "BunkrStealthBrowser", max_pages: int = 10) -> list[dict]:
    """Scrape bunkr-albums.io via stealth browser."""
    albums = []
    seen   = set()

    for page_num in range(1, max_pages + 1):
        url  = "https://bunkr-albums.io/" + (f"?page={page_num}" if page_num > 1 else "")
        html = browser.fetch(url)
        if not html:
            break

        # Check if CF blocked us still
        if "checking your browser" in html.lower() or len(html) < 2000:
            log.warning(f"[Bunkr-albums.io] CF still blocking page {page_num}")
            break

        soup  = BeautifulSoup(html, "lxml")
        found = 0

        for a in soup.find_all("a", href=True):
            href = a["href"]
            m    = re.search(r"(?:bunkr\.\w+)?/a/([A-Za-z0-9_-]{4,24})", href)
            if not m: continue

            album_id = m.group(1)
            if album_id in seen: continue
            seen.add(album_id)

            # Title: nearest heading or link text
            title = a.get_text(strip=True)
            if not title or len(title) < 2:
                for el in a.find_all_next(["h2","h3","h4","p"], limit=2):
                    t = el.get_text(strip=True)
                    if t and len(t) > 2:
                        title = t
                        break

            # Thumbnail
            thumb = None
            for img in a.find_all("img") + (a.parent.find_all("img") if a.parent else []):
                src = img.get("src") or img.get("data-src")
                if src and src.startswith("http") and "logo" not in src and "icon" not in src:
                    thumb = src
                    break

            # File count
            count = 0
            card_text = a.parent.get_text() if a.parent else ""
            cm = re.search(r"(\d+)\s*files?", card_text, re.I)
            if cm: count = int(cm.group(1))

            albums.append({
                "id": album_id,
                "title": (title or album_id).strip(),
                "file_count": count,
                "thumbnail": thumb,
                "url": f"https://bunkr.si/a/{album_id}",
                "source": "bunkr",
                "has_videos": False,
                "date": None,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            })
            found += 1

        log.info(f"[bunkr-albums.io] Page {page_num}: {found} albums")
        if found == 0:
            break

    log.info(f"[bunkr-albums.io] Total: {len(albums)}")
    return albums


def enrich_bunkr_album(browser: "BunkrStealthBrowser", album_id: str) -> Optional[dict]:
    """
    Fetch bunkr.si/a/{id}?advanced=1 — gallery-dl's proven parsing method.
    Works IF the stealth browser gets past Cloudflare.
    """
    domains = [BUNKR_DOMAINS[0]] + random.sample(BUNKR_DOMAINS[1:], min(3, len(BUNKR_DOMAINS)-1))
    for domain in domains:
        url  = f"https://{domain}/a/{album_id}?advanced=1"
        html = browser.fetch(url, wait="domcontentloaded", use_cache=True)
        if not html or len(html) < 500: continue
        if "checking your browser" in html.lower(): continue

        # og:title
        title = ""
        m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        if m:
            title = m.group(1).replace("&amp;","&").replace("&lt;","<").replace("&gt;",">")

        # window.albumFiles → file count
        file_count = 0
        m = re.search(r"window\.albumFiles\s*=\s*\[(.+?)\];\s*</script>", html, re.DOTALL)
        if m:
            file_count = max(1, len(re.findall(r"\bid\s*:", m.group(1))))
        if not file_count:
            m = re.search(r"(\d+)\s+files?", html, re.I)
            if m: file_count = int(m.group(1))

        # Has videos? Check for .mp4 in albumFiles
        has_videos = bool(re.search(r"\.mp4", html, re.I))

        # og:image
        thumb = None
        m = re.search(r'property="og:image"\s+content="([^"]+)"', html)
        if m: thumb = m.group(1)

        # Size
        size_str = ""
        m = re.search(r'class="font-semibold">\(([^)]+)\)', html)
        if m: size_str = m.group(1).strip()

        # Date from timestamp: "HH:MM:SS DD/MM/YYYY"
        date_str = None
        m = re.search(r'timestamp:\s*"([^"]+)"', html)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%H:%M:%S %d/%m/%Y")
                date_str = dt.replace(tzinfo=timezone.utc).isoformat()
            except ValueError: pass

        if title or file_count:
            return {
                "id": album_id,
                "title": title or album_id,
                "file_count": file_count,
                "size": size_str,
                "has_videos": has_videos,
                "thumbnail": thumb,
                "url": f"https://bunkr.si/a/{album_id}",
                "source": "bunkr",
                "date": date_str,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            }
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    albums_by_id = load_existing()
    new_count    = 0
    now          = datetime.now(timezone.utc).isoformat()

    # ── PHASE 1: FAPELLO (guaranteed results) ─────────────────────────────────
    log.info("=" * 65)
    log.info("PHASE 1: Fapello (plain requests — no stealth needed)")
    log.info("=" * 65)

    fapello_pages = min(30, MAX_NEW // 10)  # ~10 models per page
    fapello_models = scrape_fapello(max_pages=fapello_pages)

    for model in fapello_models:
        if model["id"] not in albums_by_id:
            albums_by_id[model["id"]] = model
            new_count += 1
            log.info(f"  +[{new_count:>4}] [fapello] {model['title'][:55]}")

    log.info(f"Phase 1 done: {new_count} new Fapello models added")

    # ── PHASE 2: BUNKR (stealth browser, may fail on CI without CF bypass) ────
    if ENABLE_BUNKR:
        log.info("=" * 65)
        log.info("PHASE 2: Bunkr (stealth browser via patchright)")
        log.info("=" * 65)

        browser = BunkrStealthBrowser()
        browser_ok = browser.start()

        if browser_ok:
            try:
                # 2a: bunkr-albums.io directory
                bunkr_albums = scrape_bunkr_albums_io(browser, max_pages=8)
                added_bunkr  = 0
                for album in bunkr_albums:
                    if album["id"] not in albums_by_id:
                        albums_by_id[album["id"]] = album
                        new_count += 1
                        added_bunkr += 1
                log.info(f"Phase 2a: {added_bunkr} new Bunkr albums from bunkr-albums.io")

                # 2b: Enrich bunkr albums missing key data
                needs_enrich = [
                    a for a in albums_by_id.values()
                    if a.get("source") == "bunkr"
                    and (not a.get("file_count") or a.get("title") == a.get("id"))
                ]
                enrich_limit = min(len(needs_enrich), 40)
                if needs_enrich:
                    log.info(f"Phase 2b: Enriching {enrich_limit} Bunkr albums via bunkr.si")
                    ok = 0
                    for album in needs_enrich[:enrich_limit]:
                        detail = enrich_bunkr_album(browser, album["id"])
                        if detail:
                            for k, v in detail.items():
                                if v and (not album.get(k) or album[k] == album["id"]):
                                    album[k] = v
                            ok += 1
                    log.info(f"Phase 2b: Enriched {ok}/{enrich_limit} albums")

            finally:
                browser.stop()
        else:
            log.warning("Bunkr scraping skipped — patchright unavailable")
    else:
        log.info("Bunkr scraping disabled (ENABLE_BUNKR=false)")

    # ── Save ──────────────────────────────────────────────────────────────────
    save(albums_by_id, new_count)

    total = len(albums_by_id)
    fapello_total = sum(1 for a in albums_by_id.values() if a.get("source") == "fapello")
    bunkr_total   = sum(1 for a in albums_by_id.values() if a.get("source") == "bunkr")

    log.info("")
    log.info("=" * 65)
    log.info(f"COMPLETE: {total} total ({fapello_total} Fapello, {bunkr_total} Bunkr)")
    log.info(f"          {new_count} new this run")
    log.info("=" * 65)


if __name__ == "__main__":
    run()
