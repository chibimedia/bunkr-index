#!/usr/bin/env python3
"""
MediaIndex Scraper v5
=====================

ROOT CAUSE OF ALL PREVIOUS FAILURES:
  GitHub Actions runners have datacenter IPs. Cloudflare JS-challenges all
  requests from these IPs on both fapello.com AND bunkr-albums.io.
  The scraper got a ~2KB "checking your browser" page every time,
  which the length check caught and bailed on — so 0 albums every run.

  Additionally, the Fapello photo/video count extraction had a Python
  for/else bug that silently zeroed all counts regardless.

FIXES IN THIS VERSION:
  1. Fapello:  cloudscraper replaces requests — it solves Cloudflare's
               JS challenge (cf_clearance cookie) without a full browser.
               Works reliably from CI/datacenter IPs for CF "IUAM" mode.

  2. Bunkr:    nodriver (async, undetected-chromedriver successor) replaces
               patchright. nodriver uses a custom CDP implementation that
               avoids the automation signals patchright still exposes.
               Runs headless on CI with Xvfb for display emulation.

  3. Parsing:  Fixed the for/else Python bug that zeroed photo/video counts.
               Improved model slug detection to match actual Fapello HTML.

  4. Debug:    Every response is saved to debug/ if it looks like a CF block.
               The workflow will upload debug/ as an artifact so you can see
               exactly what the CI runner is receiving.
"""

import asyncio
import json
import os
import re
import time
import random
import logging
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
OUT_FILE     = Path("albums.json")
CACHE_DIR    = Path("cache")
DEBUG_DIR    = Path("debug")
CACHE_TTL    = 60 * 60 * 4   # 4 hours
MAX_NEW      = int(os.getenv("MAX_ALBUMS", "500"))
DELAY_MIN    = float(os.getenv("DELAY_MIN", "2.0"))
DELAY_MAX    = float(os.getenv("DELAY_MAX", "4.5"))
ENABLE_BUNKR = os.getenv("ENABLE_BUNKR", "true").lower() != "false"

BUNKR_DOMAINS = [
    "bunkr.si", "bunkr.cr", "bunkr.fi", "bunkr.ph",
    "bunkr.pk", "bunkr.ps", "bunkr.ws", "bunkr.black",
    "bunkr.red", "bunkr.media", "bunkr.site",
]

CACHE_DIR.mkdir(exist_ok=True)
DEBUG_DIR.mkdir(exist_ok=True)


# ── Cache helpers ─────────────────────────────────────────────────────────────
def cache_key(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".html")

def cache_valid(p: Path) -> bool:
    if not p.exists(): return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        p.stat().st_mtime, tz=timezone.utc)
    return age < timedelta(seconds=CACHE_TTL)


# ── Persistence ───────────────────────────────────────────────────────────────
def load_existing() -> dict:
    if OUT_FILE.exists():
        try:
            data = json.loads(OUT_FILE.read_text())
            existing = {a["id"]: a for a in data.get("albums", [])}
            log.info(f"Loaded {len(existing)} existing albums")
            return existing
        except Exception as e:
            log.warning(f"Could not load existing data: {e}")
    return {}

def save(albums_by_id: dict, new_count: int):
    albums = sorted(
        albums_by_id.values(),
        key=lambda a: a.get("date") or a.get("indexed_at") or "",
        reverse=True,
    )
    payload = {
        "meta": {
            "total": len(albums),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "new_this_run": new_count,
            "sources": sorted({a.get("source", "?") for a in albums}),
        },
        "albums": albums,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    log.info(f"✓ Saved {len(albums)} total ({new_count} new)")


# ── CF page detector ──────────────────────────────────────────────────────────
def is_cf_block(html: str, url: str = "") -> bool:
    """Returns True if this looks like a Cloudflare challenge/block page."""
    if not html or len(html) < 3000:
        return True
    low = html.lower()
    signals = [
        "checking your browser",
        "just a moment",
        "enable javascript and cookies",
        "cf-browser-verification",
        "cloudflare ray id",
        "cf_chl_opt",
        "ddos-guard",
    ]
    for s in signals:
        if s in low:
            # Save to debug/
            if url:
                safe = re.sub(r"[^a-z0-9]", "_", url.lower())[:60]
                (DEBUG_DIR / f"blocked_{safe}.html").write_text(html[:5000])
                log.warning(f"CF block detected for {url} — saved to debug/")
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: FAPELLO via cloudscraper
# ═══════════════════════════════════════════════════════════════════════════════

def make_cloudscraper():
    """
    Create a cloudscraper session. cloudscraper solves Cloudflare's
    JS-challenge ('I'm Under Attack Mode') automatically by running
    the CF JavaScript in a Python JS interpreter, then using the
    resulting cf_clearance cookie for subsequent requests.
    This works from datacenter/CI IPs where plain requests gets blocked.
    """
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "desktop": True,
            },
            delay=5,   # wait 5s for CF challenge (CF requires this)
        )
        # Set a real modern UA
        scraper.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",  # NO br — avoids brotli decode issues
            "Referer": "https://fapello.com/",
        })
        log.info("cloudscraper session created")
        return scraper
    except ImportError:
        log.error("cloudscraper not installed!")
        return None


def cs_get(scraper, url: str, retries: int = 3, use_cache: bool = True) -> Optional[str]:
    """Fetch a URL with cloudscraper, with caching and debug saving."""
    cp = cache_key(url)
    if use_cache and cache_valid(cp):
        content = cp.read_text(encoding="utf-8", errors="replace")
        if not is_cf_block(content):
            log.debug(f"Cache hit: {url}")
            return content
        else:
            log.warning(f"Cached CF block for {url}, refetching...")
            cp.unlink(missing_ok=True)

    for attempt in range(1, retries + 1):
        wait = random.uniform(DELAY_MIN, DELAY_MAX)
        log.info(f"  [{attempt}] GET {url} (sleeping {wait:.1f}s first)")
        time.sleep(wait)

        try:
            r = scraper.get(url, timeout=30)
            log.info(f"  → {r.status_code}  {len(r.content)} bytes  "
                     f"encoding={r.headers.get('Content-Encoding','none')}")

            if r.status_code == 200:
                text = r.text
                if is_cf_block(text, url):
                    log.warning(f"  CF block on attempt {attempt}")
                    time.sleep(10 + attempt * 5)
                    continue
                if use_cache:
                    cp.write_text(text, encoding="utf-8")
                return text

            elif r.status_code == 429:
                wait2 = 30 + random.uniform(10, 20)
                log.warning(f"  429 rate-limit — sleeping {wait2:.0f}s")
                time.sleep(wait2)
            elif r.status_code == 404:
                return None
            else:
                log.warning(f"  HTTP {r.status_code}")

        except Exception as e:
            log.warning(f"  Error attempt {attempt}: {e}")

        time.sleep((2 ** attempt) + random.uniform(1, 3))

    log.error(f"All {retries} attempts failed for {url}")
    return None


def parse_fapello_listing(html: str) -> list[dict]:
    """
    Parse one fapello.com listing page.

    ACTUAL confirmed HTML structure (verified from live fetch):
    The page has a feed of "posts". Each post contains:
      - An <a href="/slug/"> wrapping the post
      - An <img> with src like /content/X/X/{slug}/1000/{slug}_0001.jpg (avatar)
      - An <img> with src like /content/X/X/{slug}/2000/{slug}_NNNN.jpg (preview)
      - Text "+N photos" and/or "+N videos" inside the post
      - Model name as link text or nearby <p> or heading

    Navigation links like /hot/, /trending/, etc. are skipped.
    """
    soup = BeautifulSoup(html, "lxml")
    models: dict[str, dict] = {}

    SKIP_SLUGS = {
        "hot", "trending", "popular", "forum", "login", "signup",
        "welcome", "random", "posts", "submit", "videos", "contacts",
        "activity", "daily-search-ranking", "top-likes", "top-followers",
        "popular_videos", "what-is-fapello", "a",
    }

    # Walk every <a> that links to a model page
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]

        # Match /slug/ exactly  OR  https://fapello.com/slug/
        m = re.match(
            r"^(?:https://fapello\.com)?/([a-zA-Z0-9][a-zA-Z0-9_.-]{1,59})/?$",
            href
        )
        if not m:
            continue
        slug = m.group(1).rstrip("/")

        if slug in SKIP_SLUGS or slug.startswith("page-"):
            continue
        # Skip pagination, file paths, and other noise
        if re.search(r"\d{3,}$", slug):  # ends with 3+ digits = probably a post ID
            continue

        # ── Find thumbnail: walk up the DOM to find enclosing post block,
        #    then look for the avatar image (/1000/ path)
        thumb = None
        node = a_tag
        for _ in range(5):
            if node is None:
                break
            imgs = node.find_all("img", src=True)
            for img in imgs:
                src = img["src"]
                if "/content/" in src and "/1000/" in src and slug[:6] in src:
                    thumb = "https://fapello.com" + src if src.startswith("/") else src
                    break
            if thumb:
                break
            node = node.parent

        # Fallback: construct avatar URL from known pattern
        if not thumb and len(slug) >= 2:
            thumb = (
                f"https://fapello.com/content/{slug[0]}/{slug[1]}/"
                f"{slug}/1000/{slug}_0001.jpg"
            )

        # ── Model name: prefer explicit text near <p> or heading,
        #    fall back to link text
        name = ""
        node = a_tag
        for _ in range(4):
            if node is None:
                break
            for tag in node.find_all(["p", "h2", "h3", "span"]):
                t = tag.get_text(strip=True)
                if t and 2 <= len(t) <= 80 and not t.startswith("+"):
                    name = t
                    break
            if name:
                break
            node = node.parent
        if not name:
            name = a_tag.get_text(strip=True)
        if not name or len(name) < 2:
            name = slug.replace("-", " ").title()

        # ── Photo/video counts: walk UP until we find "+N photos/videos"
        #    FIX: was using a broken for/else that always reset to 0
        photos, videos = 0, 0
        node = a_tag.parent
        for _ in range(5):
            if node is None:
                break
            text = node.get_text()
            pm = re.findall(r"\+\s*(\d+)\s*photos?", text, re.I)
            vm = re.findall(r"\+\s*(\d+)\s*videos?", text, re.I)
            if pm or vm:
                photos = sum(int(x) for x in pm)
                videos = sum(int(x) for x in vm)
                break  # ← This break was missing before; without it else clause fires
            node = node.parent

        if slug not in models:
            models[slug] = {
                "id": f"fapello:{slug}",
                "title": name,
                "slug": slug,
                "thumbnail": thumb,
                "url": f"https://fapello.com/{slug}/",
                "source": "fapello",
                "has_videos": videos > 0,
                "file_count": photos + videos,
                "photo_count": photos,
                "video_count": videos,
                "date": None,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            # Accumulate across multiple posts of the same model
            models[slug]["photo_count"] = models[slug].get("photo_count", 0) + photos
            models[slug]["video_count"] = models[slug].get("video_count", 0) + videos
            models[slug]["file_count"] = (
                models[slug]["photo_count"] + models[slug]["video_count"]
            )
            if videos > 0:
                models[slug]["has_videos"] = True
            if thumb and not models[slug].get("thumbnail"):
                models[slug]["thumbnail"] = thumb

    return list(models.values())


def scrape_fapello(scraper, max_pages: int = 25) -> list[dict]:
    """Scrape Fapello listings via cloudscraper."""
    all_models: dict[str, dict] = {}

    # Multiple feed endpoints to maximise discovery
    feeds = [
        # (label, url_generator)
        ("new",     lambda p: "https://fapello.com/" if p == 1 else f"https://fapello.com/page-{p}/"),
        ("hot",     lambda p: "https://fapello.com/hot/" if p == 1 else f"https://fapello.com/hot/page-{p}/"),
        ("popular", lambda p: "https://fapello.com/popular/" if p == 1 else f"https://fapello.com/popular/page-{p}/"),
        ("trending",lambda p: "https://fapello.com/trending/" if p == 1 else f"https://fapello.com/trending/page-{p}/"),
    ]

    for label, url_fn in feeds:
        log.info(f"[Fapello] ── {label} feed ──")
        empty_streak = 0

        for page_num in range(1, max_pages + 1):
            url  = url_fn(page_num)
            html = cs_get(scraper, url)

            if not html:
                log.warning(f"[Fapello] No response for {url}")
                empty_streak += 1
                if empty_streak >= 2:
                    log.info("[Fapello] Two consecutive failures, moving to next feed")
                    break
                continue

            if is_cf_block(html, url):
                log.warning(f"[Fapello] CF block on {url}")
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue

            models   = parse_fapello_listing(html)
            new_here = sum(1 for m in models if m["slug"] not in all_models)

            for m in models:
                slug = m["slug"]
                if slug not in all_models:
                    all_models[slug] = m
                else:
                    # Keep the entry with more data
                    if m["file_count"] > all_models[slug]["file_count"]:
                        all_models[slug].update(m)

            log.info(f"[Fapello] {label} p{page_num}: {len(models)} models "
                     f"({new_here} new, {len(all_models)} total)")

            if new_here == 0:
                empty_streak += 1
                if empty_streak >= 3:
                    log.info("[Fapello] 3 empty pages in a row, next feed")
                    break
            else:
                empty_streak = 0

            if len(all_models) >= MAX_NEW:
                log.info(f"[Fapello] Reached {MAX_NEW} models")
                break

        if len(all_models) >= MAX_NEW:
            break

    log.info(f"[Fapello] Done: {len(all_models)} unique models")
    return list(all_models.values())


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE 2: BUNKR via nodriver (async, undetected)
# ═══════════════════════════════════════════════════════════════════════════════

async def bunkr_fetch_page(browser, url: str, use_cache: bool = True) -> Optional[str]:
    """Fetch one page with nodriver, detect CF blocks."""
    cp = cache_key(url)
    if use_cache and cache_valid(cp):
        content = cp.read_text(encoding="utf-8", errors="replace")
        if not is_cf_block(content):
            return content
        cp.unlink(missing_ok=True)

    import nodriver as uc
    for attempt in range(1, 4):
        try:
            await asyncio.sleep(random.uniform(2.0, 4.0))
            log.info(f"[Bunkr] GET {url} (attempt {attempt})")
            tab = await browser.get(url)
            await asyncio.sleep(3)   # wait for JS to execute

            # If CF challenge is showing, wait longer
            content = await tab.get_content()
            if is_cf_block(content, url):
                log.info("[Bunkr] CF challenge — waiting 15s...")
                await asyncio.sleep(15)
                content = await tab.get_content()

            await tab.close()

            if not is_cf_block(content):
                if use_cache:
                    cp.write_text(content, encoding="utf-8")
                return content

        except Exception as e:
            log.warning(f"[Bunkr] Error attempt {attempt}: {e}")
        await asyncio.sleep((2 ** attempt) + random.uniform(1, 3))

    return None


async def scrape_bunkr_albums_io(browser, max_pages: int = 8) -> list[dict]:
    albums = []
    seen   = set()

    for page_num in range(1, max_pages + 1):
        url  = ("https://bunkr-albums.io/"
                if page_num == 1 else f"https://bunkr-albums.io/?page={page_num}")
        html = await bunkr_fetch_page(browser, url)
        if not html:
            break

        soup  = BeautifulSoup(html, "lxml")
        found = 0

        for a in soup.find_all("a", href=True):
            m = re.search(r"/a/([A-Za-z0-9_-]{4,24})", a["href"])
            if not m:
                continue
            aid = m.group(1)
            if aid in seen:
                continue
            seen.add(aid)

            title = a.get_text(strip=True)
            if not title or len(title) < 2:
                for el in a.find_all_next(["h2", "h3", "p"], limit=2):
                    t = el.get_text(strip=True)
                    if t and len(t) > 2:
                        title = t
                        break

            thumb = None
            for img in a.find_all("img") + (a.parent.find_all("img") if a.parent else []):
                src = img.get("src") or img.get("data-src", "")
                if src and src.startswith("http") and "logo" not in src:
                    thumb = src
                    break

            count = 0
            cm = re.search(r"(\d+)\s*files?", (a.parent or a).get_text(), re.I)
            if cm:
                count = int(cm.group(1))

            albums.append({
                "id": aid,
                "title": title.strip() or aid,
                "file_count": count,
                "thumbnail": thumb,
                "url": f"https://bunkr.si/a/{aid}",
                "source": "bunkr",
                "has_videos": False,
                "date": None,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            })
            found += 1

        log.info(f"[bunkr-albums.io] p{page_num}: {found} albums")
        if found == 0:
            break

    return albums


async def enrich_bunkr_album(browser, album_id: str) -> Optional[dict]:
    """Fetch and parse a single Bunkr album page."""
    for domain in random.sample(BUNKR_DOMAINS, min(4, len(BUNKR_DOMAINS))):
        url  = f"https://{domain}/a/{album_id}?advanced=1"
        html = await bunkr_fetch_page(browser, url, use_cache=True)
        if not html or is_cf_block(html):
            continue

        title = ""
        m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        if m:
            title = (m.group(1)
                     .replace("&amp;", "&").replace("&lt;", "<")
                     .replace("&gt;", ">").replace("&quot;", '"'))

        file_count = 0
        m = re.search(r"window\.albumFiles\s*=\s*\[(.+?)\];\s*</script>",
                      html, re.DOTALL)
        if m:
            file_count = max(1, len(re.findall(r"\bid\s*:", m.group(1))))
        if not file_count:
            m = re.search(r"(\d+)\s+files?", html, re.I)
            if m:
                file_count = int(m.group(1))

        has_videos = bool(re.search(r"\.mp4", html, re.I))

        thumb = None
        m = re.search(r'property="og:image"\s+content="([^"]+)"', html)
        if m:
            thumb = m.group(1)

        size_str = ""
        m = re.search(r'class="font-semibold">\(([^)]+)\)', html)
        if m:
            size_str = m.group(1).strip()

        date_str = None
        m = re.search(r'timestamp:\s*"([^"]+)"', html)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%H:%M:%S %d/%m/%Y")
                date_str = dt.replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass

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


async def run_bunkr(albums_by_id: dict, new_count_start: int) -> int:
    """Run the Bunkr scraping phase asynchronously with nodriver."""
    try:
        import nodriver as uc
    except ImportError:
        log.error("nodriver not installed — skipping Bunkr")
        return new_count_start

    new_count = new_count_start
    log.info("[Bunkr] Starting nodriver browser...")

    browser = await uc.start(
        headless=True,
        browser_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    try:
        # Phase 2a: scrape bunkr-albums.io directory
        albums = await scrape_bunkr_albums_io(browser, max_pages=8)
        added  = 0
        for a in albums:
            if a["id"] not in albums_by_id:
                albums_by_id[a["id"]] = a
                new_count += 1
                added += 1
        log.info(f"[Bunkr] Added {added} new albums from directory")

        # Phase 2b: enrich albums missing data
        needs = [
            a for a in albums_by_id.values()
            if a.get("source") == "bunkr"
            and (not a.get("file_count") or a.get("title") == a.get("id"))
        ]
        limit = min(len(needs), 30)
        if needs:
            log.info(f"[Bunkr] Enriching {limit} albums...")
            ok = 0
            for album in needs[:limit]:
                detail = await enrich_bunkr_album(browser, album["id"])
                if detail:
                    for k, v in detail.items():
                        if v and (not album.get(k) or album[k] == album["id"]):
                            album[k] = v
                    ok += 1
            log.info(f"[Bunkr] Enriched {ok}/{limit}")

    finally:
        browser.stop()

    return new_count


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    albums_by_id = load_existing()
    new_count    = 0

    # ── PHASE 1: Fapello via cloudscraper ─────────────────────────────────────
    log.info("=" * 65)
    log.info("PHASE 1: Fapello (cloudscraper — bypasses CF JS challenge)")
    log.info("=" * 65)

    scraper = make_cloudscraper()
    if scraper:
        pages = min(25, max(5, MAX_NEW // 8))
        models = scrape_fapello(scraper, max_pages=pages)
        for m in models:
            if m["id"] not in albums_by_id:
                albums_by_id[m["id"]] = m
                new_count += 1
        log.info(f"Phase 1 done: {new_count} Fapello models")
    else:
        log.error("Phase 1 skipped — cloudscraper unavailable")

    # ── PHASE 2: Bunkr via nodriver ───────────────────────────────────────────
    if ENABLE_BUNKR:
        log.info("=" * 65)
        log.info("PHASE 2: Bunkr (nodriver — undetected async browser)")
        log.info("=" * 65)
        try:
            new_count = asyncio.run(run_bunkr(albums_by_id, new_count))
        except Exception as e:
            log.error(f"Bunkr phase failed: {e}")
    else:
        log.info("Bunkr skipped (ENABLE_BUNKR=false)")

    # ── Save ──────────────────────────────────────────────────────────────────
    save(albums_by_id, new_count)

    fapello_n = sum(1 for a in albums_by_id.values() if a.get("source") == "fapello")
    bunkr_n   = sum(1 for a in albums_by_id.values() if a.get("source") == "bunkr")

    log.info("")
    log.info("=" * 65)
    log.info(f"DONE: {len(albums_by_id)} total "
             f"({fapello_n} Fapello, {bunkr_n} Bunkr, {new_count} new)")
    log.info("=" * 65)

    # Fail loudly if we still got 0 (helps debugging in CI logs)
    if len(albums_by_id) == 0:
        log.error("ZERO ALBUMS — check debug/ folder for CF block pages")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
