"""
scrapers/kemono.py — Kemono.su API scraper

Fetch method: plain requests (JSON API, no Cloudflare on API endpoints)

Confirmed API endpoints (kemono.su/api/v1):
  GET /posts/recently-updated?limit=N&offset=N
    Returns: array of post objects
    Post fields:
      id, user, service, title, content, published, added, edited,
      file {name, path},
      attachments [{name, path}]

  GET /{service}/user/{user_id}/profile
    Returns: {id, name, service, indexed, updated, ...}

  GET /creators.txt  (huge JSON array of all creators)
    Fields per creator: id, name, service, indexed, updated

We use /posts/recently-updated as our primary feed (most recent first).
We do NOT download files — metadata only.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fetcher import fetch_json
from index import is_placeholder, now_iso

log = logging.getLogger(__name__)

BASE_URL  = "https://kemono.su/api/v1"
MAX_NEW   = int(os.getenv("MAX_ALBUMS", "500"))
PER_PAGE  = 50   # API default; max is 50 per call


def _kemono_thumb(path: str) -> Optional[str]:
    """Build thumbnail URL from a file path returned by the API."""
    if not path:
        return None
    # Paths are like /data/XX/YY/hash.ext
    # Thumbnails are served at https://kemono.su/thumbnail/data/...
    if path.startswith("/"):
        return f"https://kemono.su/thumbnail{path}"
    return None


def _parse_post(post: dict) -> dict:
    """Convert one Kemono API post dict to our schema."""
    post_id  = post.get("id", "")
    user_id  = post.get("user", "")
    service  = post.get("service", "")
    title    = (post.get("title") or "").strip()
    content  = (post.get("content") or "")[:500]  # trim for storage
    published = post.get("published") or post.get("added") or None

    # Attachments include the primary file and any extras
    attachments = list(post.get("attachments") or [])
    primary_file = post.get("file") or {}
    if primary_file.get("path"):
        attachments = [primary_file] + [a for a in attachments if a.get("path") != primary_file.get("path")]

    file_count = len(attachments)

    # Detect media types
    has_videos = any(
        (a.get("path") or "").lower().endswith((".mp4", ".webm", ".mov", ".avi", ".mkv"))
        for a in attachments
    )
    photo_count = sum(
        1 for a in attachments
        if (a.get("path") or "").lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
    )
    video_count = sum(
        1 for a in attachments
        if (a.get("path") or "").lower().endswith((".mp4", ".webm", ".mov"))
    )

    # Thumbnail from first image attachment
    thumb = None
    for a in attachments:
        p = a.get("path") or ""
        if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            thumb = _kemono_thumb(p)
            break

    # Parse date
    date_str = None
    if published:
        try:
            # API returns "2024-01-15T12:34:56" or "2024-01-15 12:34:56"
            dt = datetime.fromisoformat(published.replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            date_str = dt.isoformat()
        except Exception:
            pass

    canonical_id = f"kemono:{service}:{user_id}:{post_id}"
    url = f"https://kemono.su/{service}/user/{user_id}/post/{post_id}"

    needs_recheck = is_placeholder({"title": title}) or (file_count == 0)

    return {
        "id":            canonical_id,
        "title":         title or f"{service} post {post_id}",
        "source":        "kemono",
        "url":           url,
        "thumbnail":     thumb,
        "file_count":    file_count,
        "photo_count":   photo_count,
        "video_count":   video_count,
        "has_videos":    has_videos,
        "date":          date_str,
        "indexed_at":    now_iso(),
        "needs_recheck": needs_recheck,
        "extra": {
            "service":     service,
            "user_id":     user_id,
            "post_id":     post_id,
            "content":     content,
        },
    }


def scrape(max_posts: int = MAX_NEW) -> list[dict]:
    """
    Fetch recently-updated posts from Kemono.su API.
    Paginates via offset until we have enough or API returns empty.
    """
    records: list[dict] = []
    seen: set[str]      = set()
    offset = 0
    consecutive_empty = 0

    log.info(f"[kemono] Starting API scrape (target: {max_posts} posts)")

    while len(records) < max_posts:
        url  = f"{BASE_URL}/posts/recently-updated?limit={PER_PAGE}&offset={offset}"
        data = fetch_json(url)

        if data is None:
            log.warning(f"[kemono] API returned None at offset {offset}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            offset += PER_PAGE
            continue

        if not isinstance(data, list):
            log.warning(f"[kemono] Unexpected API response type: {type(data)}")
            break

        if len(data) == 0:
            log.info(f"[kemono] Empty page at offset {offset}, done")
            break

        consecutive_empty = 0
        new_here = 0
        for post in data:
            try:
                record = _parse_post(post)
            except Exception as e:
                log.warning(f"[kemono] Error parsing post: {e}")
                continue

            if record["id"] not in seen:
                seen.add(record["id"])
                records.append(record)
                new_here += 1

        log.info(
            f"[kemono] offset={offset}: {len(data)} posts, {new_here} new "
            f"({len(records)} total)"
        )

        if len(data) < PER_PAGE:
            log.info("[kemono] Last page reached")
            break

        offset += PER_PAGE

    log.info(f"[kemono] Done: {len(records)} posts")
    return records
