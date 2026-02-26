"""
scrapers/bunkr.py — Bunkr scraper (requires playwright)

Fetch method: playwright (Cloudflare Bot Management — requires full browser)

Bunkr has CF Bot Management (not just IUAM). cloudscraper cannot solve it.
Only a real, undetected browser works reliably.

URL structure:
  Directory: https://bunkr-albums.io/         (album listing)
             https://bunkr-albums.io/?page=N
  Album:     https://bunkr.si/a/{album_id}    (use ?advanced=1 for file list)

Playwright setup notes for CI:
  - Requires: pip install playwright && playwright install chromium --with-deps
  - Requires: Xvfb on Linux CI (or use headless=True)
  - Reuses storage_state.json to avoid re-solving CF challenges

If playwright is not installed, this module logs a warning and returns [].
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from fetcher import fetch_playwright, playwright_stop, is_cf_block, save_debug
from index import now_iso

log = logging.getLogger(__name__)

MAX_NEW = int(os.getenv("MAX_ALBUMS", "500"))

BUNKR_DOMAINS = [
    "bunkr.si", "bunkr.cr", "bunkr.fi", "bunkr.ph",
    "bunkr.pk", "bunkr.ps", "bunkr.ws", "bunkr.black",
    "bunkr.red", "bunkr.media", "bunkr.site",
]


def _parse_albums_io_page(html: str) -> list[str]:
    """Extract album IDs from bunkr-albums.io listing page."""
    ids = []
    for m in re.finditer(
        r'href=["\'](?:https?://bunkr[^"\'/]+)?/a/([A-Za-z0-9_-]{4,24})["\']',
        html
    ):
        ids.append(m.group(1))
    return list(dict.fromkeys(ids))   # deduplicate preserving order


def _parse_album_detail(html: str, album_id: str, domain: str) -> Optional[dict]:
    """
    Parse a Bunkr album detail page.
    Extracts: title, file_count, has_videos, thumbnail, date, size.
    """
    # og:title
    title = ""
    m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    if m:
        title = (m.group(1)
                 .replace("&amp;", "&").replace("&lt;", "<")
                 .replace("&gt;", ">").replace("&quot;", '"').strip())

    # File count: window.albumFiles JS array
    file_count = 0
    m = re.search(r"window\.albumFiles\s*=\s*\[(.+?)\];\s*(?:</script>|var )", html, re.DOTALL)
    if m:
        file_count = max(1, len(re.findall(r'"id"\s*:', m.group(1))))
    if not file_count:
        m = re.search(r"(\d+)\s+files?", html, re.I)
        if m:
            file_count = int(m.group(1))

    # Media types
    has_videos = bool(re.search(r'\.mp4|\.webm|\.mov', html, re.I))

    # og:image thumbnail
    thumb = None
    m = re.search(r'property="og:image"\s+content="([^"]+)"', html)
    if m:
        thumb = m.group(1)

    # Size string
    size_str = ""
    m = re.search(r'class="font-semibold">\(([^)]+)\)', html)
    if m:
        size_str = m.group(1).strip()

    # Date from timestamp: "HH:MM:SS DD/MM/YYYY"
    date_str = None
    m = re.search(r'timestamp:\s*"([^"]+)"', html)
    if m:
        from datetime import datetime, timezone
        try:
            dt = datetime.strptime(m.group(1), "%H:%M:%S %d/%m/%Y")
            date_str = dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    if not title and not file_count:
        return None

    return {
        "id":            album_id,
        "title":         title or album_id,
        "source":        "bunkr",
        "url":           f"https://bunkr.si/a/{album_id}",
        "thumbnail":     thumb,
        "file_count":    file_count,
        "photo_count":   max(0, file_count - (1 if has_videos else 0)),
        "video_count":   1 if has_videos else 0,
        "has_videos":    has_videos,
        "size":          size_str,
        "date":          date_str,
        "indexed_at":    now_iso(),
        "needs_recheck": file_count == 0,
        "extra":         {"domain": domain},
    }


def scrape(max_albums: int = MAX_NEW, max_pages: int = 8) -> list[dict]:
    """
    Scrape Bunkr via playwright.
    Phase 1: scrape bunkr-albums.io for album IDs
    Phase 2: fetch each album's detail page for metadata

    Returns [] if playwright is not available.
    """
    try:
        import playwright
    except ImportError:
        log.warning("[bunkr] playwright not installed — skipping")
        return []

    log.info(f"[bunkr] Starting playwright scrape (target: {max_albums} albums)")
    album_ids: list[str] = []

    # ── Phase 1: directory listing ─────────────────────────────────────────────
    for page_num in range(1, max_pages + 1):
        url  = "https://bunkr-albums.io/" + (f"?page={page_num}" if page_num > 1 else "")
        html = fetch_playwright(url, site="bunkr", slug=f"listing_p{page_num}", wait_ms=4000)

        if not html or is_cf_block(html):
            log.warning(f"[bunkr] Listing page {page_num} blocked")
            if page_num == 1:
                save_debug("bunkr", "listing_p1", html or "")
            break

        ids = _parse_albums_io_page(html)
        new_here = 0
        for aid in ids:
            if aid not in album_ids:
                album_ids.append(aid)
                new_here += 1

        log.info(f"[bunkr] listing p{page_num}: {new_here} new IDs ({len(album_ids)} total)")
        if new_here == 0:
            break
        if len(album_ids) >= max_albums:
            break

    log.info(f"[bunkr] Found {len(album_ids)} album IDs total")

    # ── Phase 2: album detail pages ────────────────────────────────────────────
    records: list[dict] = []
    for album_id in album_ids[:max_albums]:
        detail = None
        import random
        domains = random.sample(BUNKR_DOMAINS, min(3, len(BUNKR_DOMAINS)))
        for domain in domains:
            url  = f"https://{domain}/a/{album_id}?advanced=1"
            html = fetch_playwright(url, site="bunkr", slug=album_id, use_cache=True, wait_ms=3000)
            if html and not is_cf_block(html):
                detail = _parse_album_detail(html, album_id, domain)
                if detail:
                    break
            else:
                save_debug("bunkr", f"{album_id}_{domain}", html or "")

        if detail:
            records.append(detail)
        else:
            log.warning(f"[bunkr] Failed to get detail for {album_id}")
            # Add a stub so we don't lose track of it
            records.append({
                "id":            album_id,
                "title":         album_id,
                "source":        "bunkr",
                "url":           f"https://bunkr.si/a/{album_id}",
                "thumbnail":     None,
                "file_count":    0,
                "photo_count":   0,
                "video_count":   0,
                "has_videos":    False,
                "date":          None,
                "indexed_at":    now_iso(),
                "needs_recheck": True,
                "extra":         {},
            })

        if len(records) % 10 == 0:
            log.info(f"[bunkr] Progress: {len(records)}/{len(album_ids)}")

    playwright_stop()
    log.info(f"[bunkr] Done: {len(records)} albums")
    return records
