#!/usr/bin/env python3
"""
Bunkr Album Indexer
Scrapes album metadata (no file downloads) and stores to albums.json
"""

import json
import os
import time
import re
import hashlib
import random
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
API_URL = "https://apidl.bunkr.ru/api/_001_v2"
BASE_URL = "https://bunkr.ru"
OUT_FILE = Path("albums.json")
MAX_ALBUMS = int(os.getenv("MAX_ALBUMS", "500"))   # cap per run
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.5"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL,
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ─── Helpers ─────────────────────────────────────────────────────────────────

def safe_get(url: str, **kwargs) -> Optional[requests.Response]:
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=15, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            wait = (attempt + 1) * 3 + random.uniform(0, 2)
            log.warning(f"Attempt {attempt+1} failed for {url}: {e}. Retrying in {wait:.1f}s")
            time.sleep(wait)
    log.error(f"All attempts failed for {url}")
    return None


def load_existing() -> dict:
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text())
        except Exception:
            pass
    return {"albums": [], "meta": {}}


def save(data: dict):
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log.info(f"Saved {len(data['albums'])} albums to {OUT_FILE}")

# ─── Scraping strategies ──────────────────────────────────────────────────────

def fetch_via_api(page: int = 1) -> list[dict]:
    """Try the unofficial Bunkr API endpoint."""
    params = {"page": page, "limit": 50}
    r = safe_get(API_URL, params=params)
    if not r:
        return []
    try:
        data = r.json()
        # API may return list or dict with albums key
        if isinstance(data, list):
            return data
        return data.get("albums", data.get("data", []))
    except Exception as e:
        log.warning(f"API parse error: {e}")
        return []


def fetch_via_scrape(page: int = 1) -> list[dict]:
    """Fallback: scrape Bunkr's /albums listing page."""
    url = f"{BASE_URL}/albums"
    params = {"page": page} if page > 1 else {}
    r = safe_get(url, params=params)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    albums = []

    # Try multiple selector patterns Bunkr has used over time
    selectors = [
        "div[class*='album'] a[href*='/a/']",
        "a[href*='/a/']",
        ".grid a",
        "article a",
    ]

    cards = []
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards:
        href = card.get("href", "")
        if "/a/" not in href:
            continue
        album_id = href.split("/a/")[-1].strip("/").split("?")[0]
        if not album_id:
            continue

        title_el = card.select_one("h3, h2, p, [class*='title'], [class*='name']")
        title = title_el.get_text(strip=True) if title_el else album_id

        img = card.select_one("img")
        thumb = img.get("src") or img.get("data-src") if img else None

        count_el = card.select_one("[class*='count'], [class*='files'], small")
        count_text = count_el.get_text(strip=True) if count_el else "0"
        count = int(re.search(r"\d+", count_text).group()) if re.search(r"\d+", count_text) else 0

        albums.append({
            "id": album_id,
            "title": title,
            "file_count": count,
            "thumbnail": thumb,
            "url": f"{BASE_URL}/a/{album_id}",
        })

    return albums


def fetch_album_detail(album_id: str) -> Optional[dict]:
    """Fetch individual album page for richer metadata."""
    url = f"{BASE_URL}/a/{album_id}"
    r = safe_get(url)
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # Title
    title_el = soup.select_one("h1, title, [class*='title']")
    title = title_el.get_text(strip=True) if title_el else album_id
    if " - Bunkr" in title:
        title = title.split(" - Bunkr")[0].strip()

    # Thumbnail: first image in album grid
    thumb = None
    img_els = soup.select("img[src]")
    for img in img_els:
        src = img.get("src", "")
        if any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
            if "logo" not in src.lower() and "icon" not in src.lower():
                thumb = src
                break

    # File count
    count = 0
    count_patterns = [
        r"(\d+)\s+files?",
        r"(\d+)\s+items?",
        r"(\d+)\s+media",
    ]
    page_text = soup.get_text()
    for pat in count_patterns:
        m = re.search(pat, page_text, re.IGNORECASE)
        if m:
            count = int(m.group(1))
            break

    # Date: look for time tags or meta
    date_str = None
    time_el = soup.select_one("time[datetime]")
    if time_el:
        date_str = time_el.get("datetime")
    else:
        meta_date = soup.select_one('meta[property="article:published_time"]')
        if meta_date:
            date_str = meta_date.get("content")

    return {
        "id": album_id,
        "title": title,
        "file_count": count,
        "thumbnail": thumb,
        "url": url,
        "date": date_str,
    }


def normalize(raw: dict) -> dict:
    """Normalize API or scraped record to our schema."""
    album_id = (
        raw.get("identifier")
        or raw.get("id")
        or raw.get("slug")
        or hashlib.md5(raw.get("url", "").encode()).hexdigest()[:8]
    )
    title = (
        raw.get("title")
        or raw.get("name")
        or album_id
    )
    thumb = (
        raw.get("thumbnail")
        or raw.get("cover")
        or raw.get("preview")
        or raw.get("image")
    )
    count = (
        raw.get("file_count")
        or raw.get("count")
        or raw.get("files")
        or 0
    )
    if isinstance(count, str):
        m = re.search(r"\d+", count)
        count = int(m.group()) if m else 0

    date = raw.get("date") or raw.get("created_at") or raw.get("updated_at") or raw.get("timestamp")

    return {
        "id": str(album_id),
        "title": str(title).strip(),
        "file_count": int(count),
        "thumbnail": thumb,
        "url": raw.get("url") or f"{BASE_URL}/a/{album_id}",
        "date": date,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }

# ─── Main ─────────────────────────────────────────────────────────────────────

def run():
    existing_data = load_existing()
    existing_ids = {a["id"] for a in existing_data.get("albums", [])}
    albums_by_id = {a["id"]: a for a in existing_data.get("albums", [])}

    log.info(f"Existing index: {len(albums_by_id)} albums")

    new_albums = []
    page = 1
    consecutive_known = 0
    max_consecutive_known = 3  # stop early if we're seeing all old content

    while len(new_albums) < MAX_ALBUMS:
        log.info(f"Fetching page {page}...")

        # Try API first, fallback to scraping
        raw_list = fetch_via_api(page)
        if not raw_list:
            log.info("API returned nothing, trying HTML scrape...")
            raw_list = fetch_via_scrape(page)

        if not raw_list:
            log.info("No more results, stopping.")
            break

        page_new = 0
        for raw in raw_list:
            album = normalize(raw)
            if album["id"] in existing_ids:
                consecutive_known += 1
            else:
                consecutive_known = 0
                new_albums.append(album)
                existing_ids.add(album["id"])
                page_new += 1

        log.info(f"Page {page}: {page_new} new albums found")

        if consecutive_known >= max_consecutive_known * len(raw_list):
            log.info("Mostly known content, stopping pagination.")
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    # If we got very few albums with no thumbnails, enrich a sample via detail pages
    needs_thumb = [a for a in new_albums if not a.get("thumbnail")]
    if needs_thumb:
        log.info(f"Enriching {min(len(needs_thumb), 50)} albums with detail pages...")
        for album in needs_thumb[:50]:
            detail = fetch_album_detail(album["id"])
            if detail:
                album.update({k: v for k, v in detail.items() if v is not None})
            time.sleep(REQUEST_DELAY)

    # Merge new into existing
    for album in new_albums:
        albums_by_id[album["id"]] = album

    all_albums = sorted(
        albums_by_id.values(),
        key=lambda a: a.get("date") or a.get("indexed_at") or "",
        reverse=True,
    )

    output = {
        "meta": {
            "total": len(all_albums),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "new_this_run": len(new_albums),
        },
        "albums": all_albums,
    }

    save(output)
    log.info(f"Done. Total: {len(all_albums)}, New: {len(new_albums)}")


if __name__ == "__main__":
    run()
