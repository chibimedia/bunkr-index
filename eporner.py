"""
scrapers/eporner.py — Eporner.com API scraper

Fetch method: plain requests (official JSON API, no Cloudflare)

API endpoint: https://www.eporner.com/api/v2/video/search/
Params:
  query      = search term (use "all" for all videos)
  per_page   = 1-1000 (we use 100)
  page       = page number (1 to total_pages)
  thumbsize  = "big" (640x360)
  order      = "latest" | "top-rated" | "most-popular" | "top-weekly" | ...
  gay        = 0 (exclude)
  lq         = 1 (include all quality levels)
  format     = "json"

Response fields per video:
  id, title, keywords, views, rate, url, added, length_sec, length_min,
  embed, default_thumb {src, width, height}, thumbs [{src}]

No auth required. No Cloudflare on API endpoint (confirmed via direct fetch).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fetcher import fetch_json
from index import now_iso

log = logging.getLogger(__name__)

BASE_URL  = "https://www.eporner.com/api/v2/video/search/"
MAX_NEW   = int(os.getenv("MAX_ALBUMS", "500"))
PER_PAGE  = 100  # max useful without excessive requests

# Multiple search queries to get variety across different popular content
SEARCH_QUERIES = [
    ("latest",       "all",       "latest"),
    ("top_weekly",   "all",       "top-weekly"),
    ("top_monthly",  "all",       "top-monthly"),
    ("top_rated",    "all",       "top-rated"),
    ("most_popular", "all",       "most-popular"),
]


def _build_url(query: str, order: str, page: int) -> str:
    return (
        f"{BASE_URL}?query={query}&per_page={PER_PAGE}&page={page}"
        f"&thumbsize=big&order={order}&gay=0&lq=1&format=json"
    )


def _parse_video(v: dict) -> dict:
    vid_id   = v.get("id", "")
    title    = (v.get("title") or "").strip()
    url      = v.get("url") or f"https://www.eporner.com/hd-porn/{vid_id}/"
    added    = v.get("added")
    length_s = v.get("length_sec", 0)
    views    = v.get("views", 0)
    rating   = v.get("rate", "0")
    keywords = v.get("keywords", "")
    embed    = v.get("embed")

    # Thumbnail
    thumb = None
    dt = v.get("default_thumb")
    if dt and dt.get("src"):
        thumb = dt["src"]
    elif v.get("thumbs"):
        thumb = v["thumbs"][0].get("src")

    # Parse date
    date_str = None
    if added:
        try:
            dt_parsed = datetime.strptime(added, "%Y-%m-%d %H:%M:%S")
            date_str = dt_parsed.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            pass

    return {
        "id":            f"eporner:{vid_id}",
        "title":         title,
        "source":        "eporner",
        "url":           url,
        "thumbnail":     thumb,
        "file_count":    1,
        "photo_count":   0,
        "video_count":   1,
        "has_videos":    True,
        "date":          date_str,
        "indexed_at":    now_iso(),
        "needs_recheck": not title or len(title) < 3,
        "extra": {
            "length_sec": length_s,
            "length_min": v.get("length_min"),
            "views":      views,
            "rating":     rating,
            "keywords":   keywords[:300],
            "embed":      embed,
        },
    }


def scrape(max_videos: int = MAX_NEW) -> list[dict]:
    """
    Fetch videos from Eporner API across multiple sort orders.
    Deduplicates by video id.
    """
    records: list[dict] = {}
    log.info(f"[eporner] Starting API scrape (target: {max_videos} videos)")

    for label, query, order in SEARCH_QUERIES:
        if len(records) >= max_videos:
            break

        log.info(f"[eporner] Query: {label}")
        page = 1
        total_pages = None

        while len(records) < max_videos:
            url  = _build_url(query, order, page)
            data = fetch_json(url)

            if data is None:
                log.warning(f"[eporner] API returned None for {url}")
                break

            if not isinstance(data, dict):
                log.warning(f"[eporner] Unexpected response type: {type(data)}")
                break

            if total_pages is None:
                total_pages = data.get("total_pages", 1)
                log.info(f"[eporner] {label}: {data.get('total_count')} total videos, {total_pages} pages")

            videos = data.get("videos") or []
            if not videos:
                break

            new_here = 0
            for v in videos:
                try:
                    record = _parse_video(v)
                except Exception as e:
                    log.warning(f"[eporner] Error parsing video: {e}")
                    continue

                if record["id"] not in records:
                    records[record["id"]] = record
                    new_here += 1

            log.info(f"[eporner] {label} p{page}: {len(videos)} videos, {new_here} new ({len(records)} total)")

            if page >= (total_pages or 1):
                break
            page += 1

    result = list(records.values())
    log.info(f"[eporner] Done: {len(result)} videos")
    return result
