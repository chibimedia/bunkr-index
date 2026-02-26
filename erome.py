"""
scrapers/erome.py — Erome.com album scraper

Fetch method: plain requests (often works), cloudscraper fallback

Erome album structure:
  URL:  https://www.erome.com/a/{album_id}
  HTML: Contains inline JS with window.albumFiles = {...} or var albumFiles = [...]
        Also has <meta property="og:title">, <meta property="og:image">
  The JS object has file URLs, types, and order.

We scrape the /new/ listing for album IDs, then fetch each album page.
Listing:  https://www.erome.com/
          https://www.erome.com/?page=N
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from bs4 import BeautifulSoup

from fetcher import fetch, fetch_cloudscraper, fetch_playwright, is_cf_block, save_debug
from index import now_iso

log = logging.getLogger(__name__)

BASE = "https://www.erome.com"
MAX_NEW = int(os.getenv("MAX_ALBUMS", "500"))


def _extract_album_files(html: str) -> list[dict]:
    """
    Extract file list from inline JS in erome album pages.
    Tries multiple patterns:
      window.albumFiles = [{...}, ...]
      var albumFiles = [{...}, ...]
      albumFiles = [{...}, ...]
    """
    patterns = [
        r"window\.albumFiles\s*=\s*(\[.+?\]);\s*(?:</script>|var |window\.)",
        r"var\s+albumFiles\s*=\s*(\[.+?\]);\s*(?:</script>|var |window\.)",
        r"albumFiles\s*=\s*(\[.+?\]);\s*(?:</script>|var |window\.)",
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return []


def _parse_album_page(html: str, album_id: str) -> dict:
    """Extract metadata from one erome album page."""
    soup = BeautifulSoup(html, "lxml")

    # og:title
    title = ""
    og = soup.find("meta", property="og:title")
    if og:
        title = (og.get("content") or "").strip()
    if not title:
        t = soup.find("title")
        if t:
            title = t.get_text(strip=True).replace(" - Erome", "").strip()

    # og:image
    thumb = None
    og_img = soup.find("meta", property="og:image")
    if og_img:
        thumb = og_img.get("content")

    # Album files from inline JS
    files = _extract_album_files(html)
    file_count  = len(files)
    has_videos  = any(
        (f.get("url") or f.get("src") or "").lower().endswith((".mp4", ".webm"))
        for f in files
    )
    photo_count = sum(
        1 for f in files
        if (f.get("url") or f.get("src") or "").lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
    )
    video_count = sum(
        1 for f in files
        if (f.get("url") or f.get("src") or "").lower().endswith((".mp4", ".webm"))
    )

    # If JS parse failed but page has images, count img tags as fallback
    if file_count == 0:
        imgs = soup.find_all("img", src=re.compile(r"\.(jpg|jpeg|png|webp)", re.I))
        vids = soup.find_all("source", src=re.compile(r"\.(mp4|webm)", re.I))
        photo_count = len(imgs)
        video_count = len(vids)
        file_count  = photo_count + video_count
        if vids:
            has_videos = True
            if not thumb:
                src = vids[0].get("src", "")
                # Try to get poster from parent <video>
                video_el = vids[0].find_parent("video")
                if video_el:
                    thumb = video_el.get("poster")

    # Uploader
    uploader = ""
    upl = soup.find("a", href=re.compile(r"/u/"))
    if upl:
        uploader = upl.get_text(strip=True)

    return {
        "id":            f"erome:{album_id}",
        "title":         title or f"Erome album {album_id}",
        "source":        "erome",
        "url":           f"{BASE}/a/{album_id}",
        "thumbnail":     thumb,
        "file_count":    file_count,
        "photo_count":   photo_count,
        "video_count":   video_count,
        "has_videos":    has_videos,
        "date":          None,
        "indexed_at":    now_iso(),
        "needs_recheck": file_count == 0,
        "extra":         {"uploader": uploader},
    }


def _scrape_listing(max_pages: int = 10) -> list[str]:
    """Scrape Erome main listing for album IDs."""
    album_ids: list[str] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        url  = BASE + (f"/?page={page}" if page > 1 else "/")
        html = fetch(url, site="erome", slug=f"listing_p{page}", prefer_cs=True)
        if not html or is_cf_block(html):
            log.warning(f"[erome] listing p{page} blocked")
            break

        # Find album links /a/{id}
        for m in re.finditer(r'href=["\'](?:https?://www\.erome\.com)?/a/([A-Za-z0-9_-]{4,30})["\']', html):
            aid = m.group(1)
            if aid not in seen:
                seen.add(aid)
                album_ids.append(aid)

        if not album_ids and page == 1:
            save_debug("erome", "listing_p1", html)
            log.error("[erome] No album IDs found on listing page 1 — check debug/erome/listing_p1.html")
            break

        log.info(f"[erome] listing p{page}: {len(seen)} album IDs found so far")
        if len(album_ids) >= MAX_NEW:
            break

    return album_ids


def scrape(max_albums: int = MAX_NEW) -> list[dict]:
    """
    Scrape Erome albums: listing → album IDs → per-album metadata.
    """
    log.info(f"[erome] Starting (target: {max_albums} albums)")
    album_ids = _scrape_listing(max_pages=min(20, max_albums // 20 + 1))

    records: list[dict] = []
    for album_id in album_ids[:max_albums]:
        url  = f"{BASE}/a/{album_id}"
        html = fetch(url, site="erome", slug=album_id, prefer_cs=True)
        if not html or is_cf_block(html):
            log.warning(f"[erome] Could not fetch album {album_id}")
            continue

        try:
            record = _parse_album_page(html, album_id)
        except Exception as e:
            log.warning(f"[erome] Parse error for {album_id}: {e}")
            continue

        records.append(record)

        if len(records) % 20 == 0:
            log.info(f"[erome] Progress: {len(records)}/{len(album_ids)} albums")

    log.info(f"[erome] Done: {len(records)} albums")
    return records
