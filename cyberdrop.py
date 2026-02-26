"""
scrapers/cyberdrop.py — Cyberdrop.me / Cyberfile.me scraper

Fetch method: plain requests with Referer header (required for some servers)
              cloudscraper fallback on 403
              mirror rotation if primary domain 403s

Known mirror domains for cyberdrop:
  cyberdrop.me, cyberdrop.to, cyberdrop.cc, cyberdrop.nl, cyberdrop.bz

Known domains for cyberfile:
  cyberfile.me, cyberfile.is

Album URL structure:
  https://cyberdrop.me/a/{album_id}
  https://cyberfile.me/folder/{folder_id}

HTML structure (cyberdrop):
  - File links: <a class="image" href="...">
  - File count in <p class="title"> or nearby text "N files"
  - Thumbnail: <img class="image-img"> or <meta property="og:image">
  - Album title: <h1 class="title">, <title>, or <meta property="og:title">

HTML structure (cyberfile):
  - File links wrapped in <div class="file-details">
  - Thumbnails in <img> tags
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from bs4 import BeautifulSoup

from fetcher import fetch_cloudscraper, fetch_plain, is_cf_block, save_debug
from index import now_iso

log = logging.getLogger(__name__)

MAX_NEW = int(os.getenv("MAX_ALBUMS", "500"))

CYBERDROP_MIRRORS = [
    "cyberdrop.me",
    "cyberdrop.to",
    "cyberdrop.cc",
    "cyberdrop.nl",
    "cyberdrop.bz",
]
CYBERFILE_MIRRORS = ["cyberfile.me", "cyberfile.is"]


def _fetch_album(url: str, site_key: str, album_id: str) -> Optional[str]:
    """
    Fetch one album page. Try plain requests first with Referer header.
    If 403 or CF block, try cloudscraper. If still failing, try mirrors.
    """
    # Determine domain for mirror rotation
    m = re.match(r"https?://([^/]+)(/.+)", url)
    if not m:
        return None
    domain, path = m.group(1), m.group(2)

    # Mirrors to try (always try the provided URL first)
    if "cyberdrop" in domain:
        mirrors = [domain] + [d for d in CYBERDROP_MIRRORS if d != domain]
    elif "cyberfile" in domain:
        mirrors = [domain] + [d for d in CYBERFILE_MIRRORS if d != domain]
    else:
        mirrors = [domain]

    for mirror in mirrors:
        attempt_url = f"https://{mirror}{path}"
        extra_headers = {
            "Referer": f"https://{mirror}/",
            "Origin":  f"https://{mirror}",
        }
        html = fetch_plain(
            attempt_url,
            site=site_key,
            slug=album_id,
            extra_headers=extra_headers,
        )
        if html and not is_cf_block(html):
            return html

        # Escalate to cloudscraper on CF block
        log.info(f"[{site_key}] plain failed for {mirror}, trying cloudscraper")
        html = fetch_cloudscraper(attempt_url, site=site_key, slug=f"{album_id}_{mirror}")
        if html and not is_cf_block(html):
            return html

        log.warning(f"[{site_key}] All methods failed for {mirror}, trying next mirror...")

    save_debug(site_key, album_id, html or "")
    return None


def _parse_cyberdrop_album(html: str, album_id: str, base_domain: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    # Title
    title = ""
    og = soup.find("meta", property="og:title")
    if og:
        title = (og.get("content") or "").strip()
    if not title:
        h1 = soup.find("h1", class_=re.compile("title", re.I))
        if h1:
            title = h1.get_text(strip=True)
    if not title:
        t = soup.find("title")
        if t:
            title = t.get_text(strip=True)

    # Thumbnail
    thumb = None
    og_img = soup.find("meta", property="og:image")
    if og_img:
        thumb = og_img.get("content")
    if not thumb:
        img = soup.find("img", class_=re.compile("image", re.I))
        if img:
            thumb = img.get("src")

    # File count - multiple possible locations
    file_count = 0
    # Method 1: explicit count text "N files"
    for txt in soup.find_all(string=re.compile(r"\d+\s+files?", re.I)):
        m = re.search(r"(\d+)\s+files?", txt, re.I)
        if m:
            file_count = int(m.group(1))
            break
    # Method 2: count file links
    if not file_count:
        file_links = soup.find_all("a", class_=re.compile("image|file|download", re.I))
        file_count = len(file_links)
    # Method 3: count img tags in the gallery
    if not file_count:
        gallery = soup.find("div", id=re.compile("gallery|files|images", re.I))
        if gallery:
            file_count = len(gallery.find_all("img"))

    # Media type detection
    all_text = html.lower()
    has_videos = ".mp4" in all_text or ".webm" in all_text
    video_count = len(re.findall(r'href="[^"]*\.(mp4|webm|mov)"', html, re.I))
    photo_count = len(re.findall(r'href="[^"]*\.(jpg|jpeg|png|gif|webp)"', html, re.I))
    if not file_count:
        file_count = photo_count + video_count

    return {
        "id":            f"cyberdrop:{album_id}",
        "title":         title.strip() or f"cyberdrop album {album_id}",
        "source":        "cyberdrop",
        "url":           f"https://{base_domain}/a/{album_id}",
        "thumbnail":     thumb,
        "file_count":    file_count,
        "photo_count":   photo_count,
        "video_count":   video_count,
        "has_videos":    has_videos,
        "date":          None,
        "indexed_at":    now_iso(),
        "needs_recheck": file_count == 0,
        "extra":         {"domain": base_domain},
    }


def _parse_cyberfile_album(html: str, folder_id: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    title = ""
    og = soup.find("meta", property="og:title")
    if og:
        title = (og.get("content") or "").strip()
    if not title:
        h1 = soup.find(["h1", "h2"])
        if h1:
            title = h1.get_text(strip=True)

    thumb = None
    og_img = soup.find("meta", property="og:image")
    if og_img:
        thumb = og_img.get("content")

    # File count
    file_count = 0
    for txt in soup.find_all(string=re.compile(r"\d+\s+(?:files?|items?)", re.I)):
        m = re.search(r"(\d+)\s+(?:files?|items?)", txt, re.I)
        if m:
            file_count = int(m.group(1))
            break

    return {
        "id":            f"cyberfile:{folder_id}",
        "title":         title.strip() or f"cyberfile folder {folder_id}",
        "source":        "cyberfile",
        "url":           f"https://cyberfile.me/folder/{folder_id}",
        "thumbnail":     thumb,
        "file_count":    file_count,
        "photo_count":   file_count,
        "video_count":   0,
        "has_videos":    False,
        "date":          None,
        "indexed_at":    now_iso(),
        "needs_recheck": file_count == 0,
        "extra":         {},
    }


def scrape_album_ids(album_ids: list[str], domain: str = "cyberdrop.me") -> list[dict]:
    """
    Scrape a provided list of cyberdrop album IDs.
    (Discovery/listing is done separately — cyberdrop has no public directory.)
    """
    records = []
    for album_id in album_ids:
        url  = f"https://{domain}/a/{album_id}"
        html = _fetch_album(url, "cyberdrop", album_id)
        if not html:
            log.warning(f"[cyberdrop] Could not fetch {album_id}")
            continue
        try:
            record = _parse_cyberdrop_album(html, album_id, domain)
            records.append(record)
        except Exception as e:
            log.warning(f"[cyberdrop] Parse error {album_id}: {e}")

    return records


def scrape_cyberfile_ids(folder_ids: list[str]) -> list[dict]:
    """Scrape a provided list of cyberfile folder IDs."""
    records = []
    for fid in folder_ids:
        url  = f"https://cyberfile.me/folder/{fid}"
        html = _fetch_album(url, "cyberfile", fid)
        if not html:
            log.warning(f"[cyberfile] Could not fetch {fid}")
            continue
        try:
            record = _parse_cyberfile_album(html, fid)
            records.append(record)
        except Exception as e:
            log.warning(f"[cyberfile] Parse error {fid}: {e}")

    return records


# Note: cyberdrop/cyberfile don't have public directory listings.
# Call scrape_album_ids() / scrape_cyberfile_ids() with known IDs.
# IDs can come from other sources (links in fapello posts, bunkr, etc.)
