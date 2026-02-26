#!/usr/bin/env python3
"""
BunkrIndex Scraper v2
=====================
DISCOVERY STRATEGY (why v1 returned 0 albums):
  - Bunkr has NO public "browse all albums" API. The apidl endpoint only
    resolves individual FILE urls — it's not a listing API.
  - bunkr-albums.io is the actual index/directory we scrape for album IDs.
  - Once we have IDs, we enrich each via Bunkr's album page:
      GET https://bunkr.si/a/{id}?advanced=1
      → parse window.albumFiles, og:title, og:image

SOURCES (in priority order):
  1. bunkr-albums.io  — paginated HTML album directory
  2. Bunkr sitemap    — if ever published
  3. Known album ID seeds — bootstraps the index on first run

OUTPUT: albums.json  (title, file_count, thumbnail, url, date, size)
"""

import json
import os
import re
import time
import random
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
OUT_FILE       = Path("albums.json")
MAX_NEW        = int(os.getenv("MAX_ALBUMS", "300"))
REQUEST_DELAY  = float(os.getenv("REQUEST_DELAY", "1.5"))

# These are gallery-dl's verified working Bunkr domains (from bunkr.py source)
BUNKR_DOMAINS = [
    "bunkr.si",
    "bunkr.cr",
    "bunkr.fi",
    "bunkr.ph",
    "bunkr.pk",
    "bunkr.ps",
    "bunkr.ws",
    "bunkr.black",
    "bunkr.red",
    "bunkr.media",
    "bunkr.site",
    "bunkr.ac",
    "bunkr.ci",
    "bunkr.sk",
]
# Start with most reliable
PRIMARY_DOMAIN = "bunkr.si"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "DNT": "1",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.max_redirects = 5

# ── Persistence ───────────────────────────────────────────────────────────────

def load_existing() -> dict:
    if OUT_FILE.exists():
        try:
            d = json.loads(OUT_FILE.read_text())
            log.info(f"Loaded {len(d.get('albums', []))} existing albums")
            return d
        except Exception as e:
            log.warning(f"Could not load existing albums.json: {e}")
    return {"albums": [], "meta": {}}


def save(albums_by_id: dict, new_count: int):
    all_albums = sorted(
        albums_by_id.values(),
        key=lambda a: a.get("date") or a.get("indexed_at") or "",
        reverse=True,
    )
    data = {
        "meta": {
            "total": len(all_albums),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "new_this_run": new_count,
        },
        "albums": all_albums,
    }
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log.info(f"✓ Saved {len(all_albums)} total albums ({new_count} new this run)")

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def safe_get(url: str, retries=3, delay_mult=1, **kwargs) -> Optional[requests.Response]:
    for attempt in range(retries):
        try:
            time.sleep(REQUEST_DELAY * delay_mult + random.uniform(0.3, 1.0))
            r = SESSION.get(url, timeout=20, **kwargs)
            if r.status_code == 403:
                log.warning(f"403 on {url} — trying next domain")
                return None
            if r.status_code == 429:
                wait = 30 + random.uniform(5, 15)
                log.warning(f"Rate limited, waiting {wait:.0f}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.TooManyRedirects:
            log.warning(f"Too many redirects for {url}")
            return None
        except requests.RequestException as e:
            wait = (attempt + 1) * 5 + random.uniform(1, 3)
            log.warning(f"Attempt {attempt+1}/{retries} failed for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(wait)
    return None


def get_bunkr_page(album_id: str) -> Optional[requests.Response]:
    """Try each Bunkr domain until one works (they rotate with CF challenges)."""
    domains = [PRIMARY_DOMAIN] + [d for d in BUNKR_DOMAINS if d != PRIMARY_DOMAIN]
    for domain in domains:
        url = f"https://{domain}/a/{album_id}?advanced=1"
        r = safe_get(url, retries=2, delay_mult=0.5)
        if r and r.status_code == 200:
            return r
        log.debug(f"Domain {domain} failed for {album_id}, trying next...")
    return None

# ── SOURCE 1: bunkr-albums.io ─────────────────────────────────────────────────

def scrape_bunkr_albums_io(max_pages: int = 20) -> list[str]:
    """
    Scrape bunkr-albums.io for album IDs.
    Returns list of album ID strings.
    """
    album_ids = []
    seen = set()

    for page in range(1, max_pages + 1):
        url = f"https://bunkr-albums.io/?page={page}" if page > 1 else "https://bunkr-albums.io/"
        log.info(f"Scraping bunkr-albums.io page {page}...")

        r = safe_get(url, retries=3, delay_mult=1.5)
        if not r:
            log.warning(f"Could not reach bunkr-albums.io page {page}, stopping")
            break

        soup = BeautifulSoup(r.text, "lxml")

        # Find all album links - look for /a/ pattern
        found_on_page = 0
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]

            # Match bunkr album URLs
            m = re.search(r"/a/([A-Za-z0-9_-]{6,20})", href)
            if not m:
                # Also check for links to bunkr.* domains
                m2 = re.search(r"bunkr\.\w+/a/([A-Za-z0-9_-]{6,20})", href)
                if m2:
                    m = m2
            if m:
                aid = m.group(1)
                if aid not in seen:
                    seen.add(aid)
                    album_ids.append(aid)
                    found_on_page += 1

        log.info(f"  Page {page}: {found_on_page} new album IDs")

        if found_on_page == 0:
            log.info("No new albums found, done with bunkr-albums.io")
            break

        # Check if there's a next page
        next_link = soup.find("a", string=re.compile(r"next|›|»|\>", re.I))
        if not next_link and not soup.find("a", href=re.compile(r"page=\d+")):
            log.info("No pagination found, done with bunkr-albums.io")
            break

    log.info(f"bunkr-albums.io total discovered: {len(album_ids)} album IDs")
    return album_ids


def scrape_bunkr_albums_io_search(query: str = "") -> list[str]:
    """Use the search feature of bunkr-albums.io."""
    album_ids = []
    seen = set()

    url = f"https://bunkr-albums.io/?search={requests.utils.quote(query)}" if query else "https://bunkr-albums.io/"

    for page in range(1, 10):
        page_url = url + (f"&page={page}" if page > 1 else "")
        r = safe_get(page_url, retries=2)
        if not r:
            break

        soup = BeautifulSoup(r.text, "lxml")
        count = 0
        for a_tag in soup.find_all("a", href=re.compile(r"/a/[A-Za-z0-9_-]+")):
            m = re.search(r"/a/([A-Za-z0-9_-]{6,20})", a_tag["href"])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                album_ids.append(m.group(1))
                count += 1

        if count == 0:
            break

    return album_ids

# ── SOURCE 2: Bunkr album page enrichment ─────────────────────────────────────

def enrich_album(album_id: str) -> Optional[dict]:
    """
    Fetch https://bunkr.si/a/{id}?advanced=1 and extract metadata.
    Uses gallery-dl's proven parsing approach:
      - og:title for album name
      - window.albumFiles for file list (count + first image as thumbnail)
      - span.font-semibold for size
    """
    r = get_bunkr_page(album_id)
    if not r:
        log.debug(f"Could not fetch album {album_id}")
        return None

    html = r.text

    # Title from og:title (gallery-dl approach)
    title = ""
    m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    if m:
        title = m.group(1).strip()
        # Unescape HTML entities
        title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#039;", "'")
    if not title:
        # Fallback: <title> tag
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        if m:
            title = m.group(1).strip()
            title = re.sub(r"\s*[|\-–]\s*Bunkr.*$", "", title).strip()
    if not title:
        title = album_id

    # Thumbnail from og:image
    thumb = None
    m = re.search(r'property="og:image"\s+content="([^"]+)"', html)
    if m:
        thumb = m.group(1).strip()
    if not thumb:
        # Try first img in albumFiles
        m = re.search(r'cdn\d*\.[^"\']+\.(?:jpg|jpeg|png|webp|gif)', html, re.I)
        if m:
            thumb = "https://" + m.group(0) if not m.group(0).startswith("http") else m.group(0)

    # File count from window.albumFiles
    file_count = 0
    # gallery-dl extracts: text.extr(page, "window.albumFiles = [", "</script>").split("\n},\n")
    m = re.search(r"window\.albumFiles\s*=\s*\[(.*?)\];\s*</script>", html, re.DOTALL)
    if m:
        items_raw = m.group(1)
        # Count individual file objects by counting 'id:' occurrences
        file_count = len(re.findall(r"\bid\s*:", items_raw))
    if file_count == 0:
        # Fallback: look for "X files" text
        m = re.search(r"(\d+)\s+files?", html, re.I)
        if m:
            file_count = int(m.group(1))

    # Album size from gallery-dl: text.extr(page, '<span class="font-semibold">(', ')')
    size_str = ""
    m = re.search(r'<span class="font-semibold">\(([^)]+)\)', html)
    if m:
        size_str = m.group(1).strip()

    # Date: look for timestamp in albumFiles
    date_str = None
    m = re.search(r'timestamp:\s*"([^"]+)"', html)
    if m:
        raw_ts = m.group(1)  # format: "HH:MM:SS DD/MM/YYYY"
        try:
            dt = datetime.strptime(raw_ts, "%H:%M:%S %d/%m/%Y")
            date_str = dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    if not date_str:
        # Try ISO date in og:updated_time or article:published_time
        m = re.search(r'(?:updated_time|published_time)"\s+content="([^"]+)"', html)
        if m:
            date_str = m.group(1)

    return {
        "id": album_id,
        "title": title,
        "file_count": file_count,
        "size": size_str,
        "thumbnail": thumb,
        "url": f"https://bunkr.si/a/{album_id}",
        "date": date_str,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "source": "bunkr",
    }

# ── SOURCE 3: bunkr-albums.io album card scraping ────────────────────────────

def scrape_bunkr_albums_io_cards(max_pages: int = 15) -> list[dict]:
    """
    Extract album metadata directly from bunkr-albums.io card HTML
    without needing to hit Bunkr directly.
    This is faster since bunkr-albums.io already has the metadata cached.
    """
    albums = []
    seen = set()

    for page in range(1, max_pages + 1):
        url = f"https://bunkr-albums.io/?page={page}" if page > 1 else "https://bunkr-albums.io/"
        log.info(f"Scraping bunkr-albums.io cards page {page}...")

        r = safe_get(url, retries=3, delay_mult=1.5)
        if not r:
            log.warning(f"Failed to reach bunkr-albums.io page {page}")
            break

        soup = BeautifulSoup(r.text, "lxml")
        found_on_page = 0

        # Try to find album cards — bunkr-albums.io uses cards/grid layout
        # Multiple selector attempts for resilience
        cards = []

        # Strategy 1: find direct album links with title context
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            m = re.search(r"(?:bunkr\.\w+)?/a/([A-Za-z0-9_-]{6,20})", href)
            if not m:
                continue
            album_id = m.group(1)
            if album_id in seen:
                continue
            seen.add(album_id)

            # Extract title from link text or nearby heading
            title = a.get_text(strip=True)
            if not title or len(title) < 2:
                parent = a.parent
                for selector in ["h2", "h3", "h4", "p", ".title", ".name"]:
                    el = parent.select_one(selector) if parent else None
                    if el:
                        title = el.get_text(strip=True)
                        break

            # Thumbnail
            thumb = None
            img = a.find("img") or (a.parent.find("img") if a.parent else None)
            if img:
                thumb = img.get("src") or img.get("data-src") or img.get("data-lazy-src")

            # File count from text near the card
            count = 0
            card_text = a.parent.get_text() if a.parent else ""
            count_m = re.search(r"(\d+)\s*files?", card_text, re.I)
            if count_m:
                count = int(count_m.group(1))

            # Build the canonical bunkr.si URL
            bunkr_url = f"https://bunkr.si/a/{album_id}"

            albums.append({
                "id": album_id,
                "title": title or album_id,
                "file_count": count,
                "thumbnail": thumb,
                "url": bunkr_url,
                "date": None,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "source": "bunkr-albums.io",
            })
            found_on_page += 1

        log.info(f"  Page {page}: {found_on_page} album cards")

        if found_on_page == 0:
            # Try different URL patterns
            if page == 1:
                log.warning("No albums found on page 1 of bunkr-albums.io — site may have changed structure")
            break

        # Pagination detection
        has_next = bool(
            soup.find("a", href=re.compile(r"[?&]page=" + str(page + 1)))
            or soup.find("a", string=re.compile(r"next|›|»", re.I))
        )
        if not has_next and page > 1:
            log.info(f"No next page link after page {page}")
            break

    log.info(f"bunkr-albums.io cards total: {len(albums)} albums")
    return albums

# ── Main orchestration ────────────────────────────────────────────────────────

def run():
    existing = load_existing()
    albums_by_id: dict[str, dict] = {a["id"]: a for a in existing.get("albums", [])}
    log.info(f"Starting with {len(albums_by_id)} existing albums in index")

    new_count = 0

    # ── Step 1: Scrape bunkr-albums.io for album cards (gets title+thumb without hitting Bunkr)
    log.info("=" * 60)
    log.info("STEP 1: Scraping bunkr-albums.io for album cards")
    log.info("=" * 60)

    cards = scrape_bunkr_albums_io_cards(max_pages=15)

    for card in cards:
        if card["id"] not in albums_by_id:
            albums_by_id[card["id"]] = card
            new_count += 1
            log.info(f"  + [{new_count}] {card['title'][:60]} ({card['id']})")
            if new_count >= MAX_NEW:
                log.info(f"Reached MAX_ALBUMS={MAX_NEW}, stopping card scrape")
                break

    log.info(f"After step 1: {len(albums_by_id)} total, {new_count} new")

    # ── Step 2: For albums missing key metadata, enrich via Bunkr directly
    needs_enrichment = [
        a for a in albums_by_id.values()
        if not a.get("file_count") or not a.get("title") or a["title"] == a["id"]
    ]

    if needs_enrichment:
        enrich_limit = min(len(needs_enrichment), 50)  # Don't hit Bunkr too hard per run
        log.info("=" * 60)
        log.info(f"STEP 2: Enriching {enrich_limit}/{len(needs_enrichment)} albums via Bunkr")
        log.info("=" * 60)

        enriched = 0
        for album in needs_enrichment[:enrich_limit]:
            detail = enrich_album(album["id"])
            if detail:
                # Merge: prefer Bunkr data for fields it has
                for k, v in detail.items():
                    if v and (not album.get(k) or album[k] == album["id"]):
                        album[k] = v
                enriched += 1
                log.info(f"  ✓ Enriched {album['id']}: {album.get('title','?')[:50]} ({album.get('file_count',0)} files)")
            else:
                log.debug(f"  ✗ Could not enrich {album['id']}")

        log.info(f"Enriched {enriched} albums via Bunkr")

    # ── Save
    save(albums_by_id, new_count)

    # ── Summary
    total = len(albums_by_id)
    log.info("")
    log.info("=" * 60)
    log.info(f"DONE: {total} total albums, {new_count} new this run")
    log.info("=" * 60)


if __name__ == "__main__":
    run()
