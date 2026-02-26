#!/usr/bin/env python3
"""
BunkrIndex Scraper v3 — Stealth Browser Edition
================================================

WHY PREVIOUS VERSIONS FAILED:
  1. `requests` gets 403'd by Cloudflare on bunkr-albums.io
  2. Plain Playwright is detected by CF's CDP fingerprinting
  3. There is NO public "list all albums" Bunkr API

THIS VERSION:
  - Uses `patchright` (drop-in Playwright replacement that patches CDP signals)
  - Launches real Chromium in non-headless mode on CI (required for CF clearance)
  - Scrapes bunkr-albums.io pages with real browser rendering
  - Falls back to enriching known IDs via bunkr.si/a/{id}?advanced=1
  - Parses window.albumFiles + og:title (gallery-dl's proven approach)
"""

import json, os, re, time, random, logging, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
OUT_FILE      = Path("albums.json")
CACHE_DIR     = Path("cache")
CACHE_TTL     = 60 * 60 * 5          # 5 hours
MAX_NEW       = int(os.getenv("MAX_ALBUMS", "300"))
PAGE_DELAY    = float(os.getenv("REQUEST_DELAY", "3.0"))
HEADLESS      = os.getenv("HEADLESS", "true").lower() != "false"

BUNKR_DOMAINS = [
    "bunkr.si", "bunkr.cr", "bunkr.fi", "bunkr.ph",
    "bunkr.pk", "bunkr.ps", "bunkr.ws", "bunkr.black",
    "bunkr.red", "bunkr.media", "bunkr.site", "bunkr.ac",
    "bunkr.ci", "bunkr.sk",
]

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)
CACHE_DIR.mkdir(exist_ok=True)

# ── Cache helpers ─────────────────────────────────────────────────────────────
def cache_key(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".html")

def cache_valid(p: Path) -> bool:
    if not p.exists(): return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return age < timedelta(seconds=CACHE_TTL)

# ── Stealth browser fetcher ───────────────────────────────────────────────────

class StealthBrowser:
    """
    Wraps patchright (patched Playwright that removes CDP automation signals).
    Keeps a single persistent browser context to reuse CF clearance cookies.
    """

    INIT_SCRIPT = """
        // Remove automation fingerprints
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        window.chrome = { runtime: {} };
        // Realistic screen
        Object.defineProperty(screen, 'width',  {get: () => 1920});
        Object.defineProperty(screen, 'height', {get: () => 1080});
    """

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ]

    def __init__(self):
        self._pw  = None
        self._br  = None
        self._ctx = None
        self._ua  = random.choice(self.USER_AGENTS)

    def start(self):
        log.info(f"Launching stealth browser (headless={HEADLESS})...")
        self._pw = sync_playwright().start()
        # patchright uses chromium; persistent_context keeps CF cookies alive
        profile_dir = Path("browser_profile")
        profile_dir.mkdir(exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=HEADLESS,
            no_viewport=False,
            viewport={"width": 1920, "height": 1080},
            user_agent=self._ua,
            locale="en-US",
            timezone_id="America/New_York",
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
            ignore_https_errors=True,
        )
        self._ctx.add_init_script(self.INIT_SCRIPT)
        log.info("Browser ready.")

    def stop(self):
        try:
            if self._ctx: self._ctx.close()
            if self._pw:  self._pw.stop()
        except Exception: pass

    def fetch(self, url: str, wait_for: str = "networkidle",
              retries: int = 3, cache: bool = True) -> Optional[str]:
        cp = cache_key(url)
        if cache and cache_valid(cp):
            log.debug(f"Cache hit: {url}")
            return cp.read_text(encoding="utf-8")

        for attempt in range(1, retries + 1):
            try:
                page = self._ctx.new_page()
                page.add_init_script(self.INIT_SCRIPT)

                # Human-like: random small delay before navigation
                time.sleep(random.uniform(0.8, 2.0))
                log.info(f"  GET {url} (attempt {attempt})")
                page.goto(url, wait_until=wait_for, timeout=45_000)

                # If Cloudflare challenge, wait up to 20s for it to resolve
                if "challenge" in page.url or "cloudflare" in page.content().lower()[:500]:
                    log.info("  Cloudflare challenge detected, waiting...")
                    time.sleep(random.uniform(8, 15))
                    page.wait_for_load_state("networkidle", timeout=30_000)

                html = page.content()
                page.close()

                if cache:
                    cp.write_text(html, encoding="utf-8")

                time.sleep(random.uniform(PAGE_DELAY * 0.7, PAGE_DELAY * 1.3))
                return html

            except PWTimeout:
                log.warning(f"  Timeout on {url} attempt {attempt}")
            except Exception as e:
                log.warning(f"  Error on {url} attempt {attempt}: {e}")
            finally:
                try: page.close()
                except: pass

            backoff = (2 ** attempt) + random.uniform(1, 3)
            log.info(f"  Backing off {backoff:.1f}s...")
            time.sleep(backoff)

        log.error(f"All {retries} attempts failed for {url}")
        return None

# ── Source 1: bunkr-albums.io (stealth browser required) ─────────────────────

def scrape_bunkr_albums_io(browser: StealthBrowser, max_pages: int = 20) -> list[dict]:
    """Scrape bunkr-albums.io with a stealth browser to bypass CF bot check."""
    albums = []
    seen   = set()

    for page_num in range(1, max_pages + 1):
        url = "https://bunkr-albums.io/" + (f"?page={page_num}" if page_num > 1 else "")
        log.info(f"[bunkr-albums.io] Scraping page {page_num}...")

        html = browser.fetch(url)
        if not html:
            log.warning(f"[bunkr-albums.io] Failed to fetch page {page_num}")
            break

        soup  = BeautifulSoup(html, "lxml")
        found = 0

        # Try multiple selector strategies
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]

            # Direct bunkr.*/a/{id} links OR relative /a/{id} links
            m = re.search(r"(?:bunkr\.\w+)?/a/([A-Za-z0-9_-]{4,24})", href)
            if not m:
                continue
            album_id = m.group(1)
            if album_id in seen:
                continue
            seen.add(album_id)

            # Title: walk up the DOM tree for meaningful text
            title = ""
            node  = a_tag
            for _ in range(4):
                candidate = node.get_text(separator=" ", strip=True)
                if candidate and 3 < len(candidate) < 200:
                    title = candidate
                    break
                node = node.parent
                if node is None:
                    break

            # Thumbnail
            thumb = None
            for img in (a_tag.find_all("img") or (a_tag.parent.find_all("img") if a_tag.parent else [])):
                src = img.get("src") or img.get("data-src") or img.get("data-lazy")
                if src and src.startswith("http") and not any(x in src for x in ["icon", "logo", "avatar"]):
                    thumb = src
                    break

            # File count from nearby text
            count = 0
            card_text = a_tag.parent.get_text() if a_tag.parent else ""
            cm = re.search(r"(\d+)\s*files?", card_text, re.I)
            if cm:
                count = int(cm.group(1))

            albums.append({
                "id": album_id,
                "title": title.strip() or album_id,
                "file_count": count,
                "thumbnail": thumb,
                "url": f"https://bunkr.si/a/{album_id}",
                "date": None,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "source": "bunkr-albums.io",
            })
            found += 1

        log.info(f"[bunkr-albums.io] Page {page_num}: {found} new albums")

        if found == 0:
            log.info("[bunkr-albums.io] No new albums on this page — stopping")
            break

        # Check for next page
        has_next = bool(
            soup.find("a", href=re.compile(rf"[?&]page={page_num+1}")) or
            soup.find("a", string=re.compile(r"next|›|»", re.I))
        )
        if not has_next and page_num > 1:
            log.info("[bunkr-albums.io] No next page detected — done")
            break

    log.info(f"[bunkr-albums.io] Total: {len(albums)} albums discovered")
    return albums

# ── Source 2: bunkr-albums.io search ─────────────────────────────────────────

def scrape_bunkr_albums_io_search(browser: StealthBrowser, query: str = "") -> list[dict]:
    """Use search endpoint for targeted discovery."""
    albums = []
    seen   = set()
    base   = f"https://bunkr-albums.io/?search={requests_quote(query)}" if query else "https://bunkr-albums.io/"

    for page_num in range(1, 8):
        url  = base + (f"&page={page_num}" if page_num > 1 else "")
        html = browser.fetch(url)
        if not html:
            break
        soup  = BeautifulSoup(html, "lxml")
        found = 0
        for a in soup.find_all("a", href=re.compile(r"/a/[A-Za-z0-9_-]+")):
            m = re.search(r"/a/([A-Za-z0-9_-]{4,24})", a["href"])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                albums.append({
                    "id": m.group(1),
                    "title": a.get_text(strip=True) or m.group(1),
                    "url": f"https://bunkr.si/a/{m.group(1)}",
                    "source": "bunkr-albums.io/search",
                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                })
                found += 1
        if found == 0:
            break
    return albums


def requests_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s)

# ── Source 3: Direct Bunkr album page enrichment ──────────────────────────────

def enrich_album(browser: StealthBrowser, album_id: str) -> Optional[dict]:
    """
    Fetch bunkr.si/a/{id}?advanced=1 and parse:
      - og:title  → album title
      - window.albumFiles  → file count + first image thumbnail
      - span.font-semibold → size string
      - timestamp fields   → creation date
    This is exactly how gallery-dl works (from their verified source).
    """
    # Try domains in order
    domains = [BUNKR_DOMAINS[0]] + random.sample(BUNKR_DOMAINS[1:], min(4, len(BUNKR_DOMAINS)-1))

    for domain in domains:
        url  = f"https://{domain}/a/{album_id}?advanced=1"
        html = browser.fetch(url, wait_for="domcontentloaded", cache=True)
        if not html or len(html) < 500:
            continue
        if "cloudflare" in html.lower() and "checking" in html.lower():
            log.warning(f"  CF wall on {domain}, trying next...")
            continue

        # ── Title ─────────────────────────────────────────────────────────────
        title = ""
        m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        if m:
            title = m.group(1)
            title = re.sub(r"&amp;", "&", title)
            title = re.sub(r"&lt;",  "<", title)
            title = re.sub(r"&gt;",  ">", title)
            title = re.sub(r"&quot;", '"', title)
        if not title:
            m = re.search(r"<title[^>]*>([^<]+)<", html, re.I)
            if m:
                title = re.sub(r"\s*[-|–]\s*Bunkr.*$", "", m.group(1)).strip()

        # ── File count from window.albumFiles ─────────────────────────────────
        file_count = 0
        m = re.search(r"window\.albumFiles\s*=\s*\[(.+?)\];\s*</script>", html, re.DOTALL)
        if m:
            items_raw  = m.group(1)
            file_count = max(1, len(re.findall(r"\bid\s*:", items_raw)))
        if not file_count:
            m = re.search(r"(\d+)\s+files?", html, re.I)
            if m: file_count = int(m.group(1))

        # ── Thumbnail: og:image or first CDN image in albumFiles ──────────────
        thumb = None
        m = re.search(r'property="og:image"\s+content="([^"]+)"', html)
        if m:
            thumb = m.group(1)
        if not thumb:
            m = re.search(r"https://cdn\d*\.[^\"']+\.(?:jpg|jpeg|png|webp|gif)", html, re.I)
            if m: thumb = m.group(0)

        # ── Size from gallery-dl: '<span class="font-semibold">(' ─────────────
        size_str = ""
        m = re.search(r'class="font-semibold">\(([^)]+)\)', html)
        if m: size_str = m.group(1).strip()

        # ── Date from timestamp fields ─────────────────────────────────────────
        date_str = None
        m = re.search(r'timestamp:\s*"([^"]+)"', html)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%H:%M:%S %d/%m/%Y")
                date_str = dt.replace(tzinfo=timezone.utc).isoformat()
            except ValueError: pass
        if not date_str:
            m = re.search(r'(?:published_time|updated_time)"\s+content="([^"]+)"', html)
            if m: date_str = m.group(1)

        if title or file_count:
            return {
                "id": album_id,
                "title": title or album_id,
                "file_count": file_count,
                "size": size_str,
                "thumbnail": thumb,
                "url": f"https://bunkr.si/a/{album_id}",
                "date": date_str,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "source": "bunkr.si",
            }

    return None

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
    albums = sorted(albums_by_id.values(),
                    key=lambda a: a.get("date") or a.get("indexed_at") or "",
                    reverse=True)
    OUT_FILE.write_text(json.dumps({
        "meta": {
            "total": len(albums),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "new_this_run": new_count,
        },
        "albums": albums,
    }, ensure_ascii=False, indent=2))
    log.info(f"✓ Saved {len(albums)} total albums ({new_count} new)")

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    albums_by_id = load_existing()
    new_count    = 0

    browser = StealthBrowser()
    try:
        browser.start()

        # ── STEP 1: Scrape bunkr-albums.io directory ──────────────────────────
        log.info("=" * 60)
        log.info("STEP 1: Scraping bunkr-albums.io (stealth browser)")
        log.info("=" * 60)

        cards = scrape_bunkr_albums_io(browser, max_pages=15)

        for card in cards:
            if card["id"] not in albums_by_id:
                albums_by_id[card["id"]] = card
                new_count += 1
                log.info(f"  + [{new_count:>4}] {card['title'][:55]:55s} ({card['id']})")
                if new_count >= MAX_NEW:
                    log.info(f"Reached MAX_ALBUMS={MAX_NEW}, stopping")
                    break

        log.info(f"After step 1: {len(albums_by_id)} total, {new_count} new")

        # ── STEP 2: Enrich albums missing key metadata ─────────────────────────
        needs = [a for a in albums_by_id.values()
                 if not a.get("file_count") or not a.get("title") or a["title"] == a["id"]]

        enrich_limit = min(len(needs), 60)
        if needs:
            log.info("=" * 60)
            log.info(f"STEP 2: Enriching {enrich_limit} albums via bunkr.si")
            log.info("=" * 60)
            ok = 0
            for album in needs[:enrich_limit]:
                detail = enrich_album(browser, album["id"])
                if detail:
                    # Merge: Bunkr's own data wins for non-null fields
                    for k, v in detail.items():
                        if v and (not album.get(k) or album[k] == album["id"]):
                            album[k] = v
                    ok += 1
                    log.info(f"  ✓ {album['id']}: {album.get('title','?')[:50]} ({album.get('file_count',0)} files)")
            log.info(f"Enriched {ok}/{enrich_limit} albums")

    finally:
        browser.stop()

    save(albums_by_id, new_count)

    log.info("")
    log.info("=" * 60)
    log.info(f"DONE: {len(albums_by_id)} total, {new_count} new this run")
    log.info("=" * 60)


if __name__ == "__main__":
    run()
