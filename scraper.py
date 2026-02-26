#!/usr/bin/env python3
"""
MediaIndex scraper v8 — API-only, zero placeholder risk.

Sources (confirmed working from CI, no Cloudflare):
  1. Kemono.su   — /api/v1/posts/recently-updated
  2. Coomer.su   — /api/v1/posts/recently-updated  (same API as Kemono)
  3. Eporner.com — /api/v2/video/search/

Sources NOT in this version (CF blocks CI IPs):
  - Fapello    (CF IUAM — add after verifying debug artifacts show real HTML)
  - Erome      (CF)
  - Bunkr      (CF Bot Management, needs playwright)

Schema per record:
  id, title, source, url, file_count, has_videos, date, indexed_at
"""

import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
OUT_FILE       = Path("albums.json")
VALIDATION     = Path("validation.json")
MAX_PER_SOURCE = int(os.getenv("MAX_PER_SOURCE", "1000"))
DELAY          = float(os.getenv("DELAY", "1.5"))
FORCE_COMMIT   = os.getenv("FORCE_COMMIT", "false").lower() == "true"

ENABLE_KEMONO  = os.getenv("ENABLE_KEMONO",  "true").lower()  != "false"
ENABLE_COOMER  = os.getenv("ENABLE_COOMER",  "true").lower()  != "false"
ENABLE_EPORNER = os.getenv("ENABLE_EPORNER", "true").lower()  != "false"

# ── Placeholder denylist ───────────────────────────────────────────────────────
DENYLIST = {
    "", "just a moment", "checking your browser", "access denied",
    "403", "forbidden", "welcome", "welcome!", "error", "503",
    "attention required", "ray id", "cloudflare", "untitled",
}

def is_placeholder(title: str) -> bool:
    t = (title or "").strip().lower()
    return t in DENYLIST or len(t) < 2


# ── HTTP ───────────────────────────────────────────────────────────────────────
_sess = requests.Session()
_sess.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; MediaIndex/1.0)",
    "Accept":     "application/json",
})

def api_get(url: str, retries: int = 3) -> Optional[dict | list]:
    for attempt in range(1, retries + 1):
        time.sleep(DELAY + random.uniform(0, 0.5))
        try:
            r = _sess.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                wait = 30 + attempt * 15
                log.warning(f"429 rate-limited, sleeping {wait}s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"Request error attempt {attempt}: {e}")
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None


# ── Persistence ────────────────────────────────────────────────────────────────
def load_existing() -> dict[str, dict]:
    if OUT_FILE.exists():
        try:
            data = json.loads(OUT_FILE.read_text())
            existing = {a["id"]: a for a in data.get("albums", [])}
            log.info(f"Loaded {len(existing)} existing records")
            return existing
        except Exception as e:
            log.warning(f"Could not load albums.json: {e}")
    return {}

def save(albums: dict, new_count: int) -> dict:
    sorted_albums = sorted(
        albums.values(),
        key=lambda a: a.get("date") or a.get("indexed_at") or "",
        reverse=True,
    )
    placeholder_count = sum(1 for a in sorted_albums if is_placeholder(a.get("title", "")))
    sources = sorted({a.get("source", "?") for a in sorted_albums})
    meta = {
        "total":             len(sorted_albums),
        "new_this_run":      new_count,
        "placeholder_count": placeholder_count,
        "last_updated":      datetime.now(timezone.utc).isoformat(),
        "sources":           sources,
    }
    OUT_FILE.write_text(json.dumps({"meta": meta, "albums": sorted_albums},
                                    ensure_ascii=False, indent=2))
    per_source = {f"{s}_count": sum(1 for a in sorted_albums if a.get("source") == s)
                  for s in ["kemono", "coomer", "eporner"]}
    VALIDATION.write_text(json.dumps({**meta, **per_source}, indent=2))
    log.info(f"✓ {len(sorted_albums)} total  |  {new_count} new  |  {placeholder_count} placeholders")
    return meta

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE: Kemono-style API  (Kemono.su + Coomer.su share the same API schema)
# ══════════════════════════════════════════════════════════════════════════════
def scrape_kemono_api(base_url: str, source_name: str, max_posts: int) -> list[dict]:
    """
    Fetch recently-updated posts from a Kemono-compatible API.
    Works for both kemono.su and coomer.su.
    
    API: GET {base_url}/api/v1/posts/recently-updated?limit=50&offset=N
    Response: array of post objects:
      {id, user, service, title, published, added, file: {path}, attachments: [{path}]}
    """
    records = []
    seen    = set()
    offset  = 0
    PER_PAGE = 50
    consecutive_empty = 0

    log.info(f"[{source_name}] Scraping {base_url} (target: {max_posts})")

    while len(records) < max_posts:
        url  = f"{base_url}/api/v1/posts/recently-updated?limit={PER_PAGE}&offset={offset}"
        data = api_get(url)

        if not data or not isinstance(data, list) or len(data) == 0:
            log.info(f"[{source_name}] Empty page at offset {offset}, stopping")
            break

        consecutive_empty = 0
        new_on_page = 0

        for post in data:
            try:
                pid   = str(post.get("id", ""))
                uid   = str(post.get("user", ""))
                svc   = str(post.get("service", ""))
                title = (post.get("title") or "").strip()
                pub   = post.get("published") or post.get("added")

                if is_placeholder(title):
                    continue

                # Count attachments (primary file + extras)
                att = list(post.get("attachments") or [])
                pf  = post.get("file") or {}
                if pf.get("path"):
                    # Prepend primary file if not already in attachments
                    if not any(a.get("path") == pf["path"] for a in att):
                        att = [pf] + att

                file_count = len(att)
                has_videos = any(
                    (a.get("path") or "").lower().endswith((".mp4", ".webm", ".mov", ".avi"))
                    for a in att
                )

                # Parse date
                date_str = None
                if pub:
                    try:
                        dt = datetime.fromisoformat(pub.replace(" ", "T"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        date_str = dt.isoformat()
                    except Exception:
                        pass

                rid = f"{source_name}:{svc}:{uid}:{pid}"
                if rid in seen:
                    continue
                seen.add(rid)

                records.append({
                    "id":         rid,
                    "title":      title,
                    "source":     source_name,
                    "service":    svc,
                    "url":        f"{base_url}/{svc}/user/{uid}/post/{pid}",
                    "file_count": file_count,
                    "has_videos": has_videos,
                    "date":       date_str,
                    "indexed_at": now_iso(),
                })
                new_on_page += 1

            except Exception as e:
                log.warning(f"[{source_name}] Parse error: {e}")

        log.info(f"[{source_name}] offset={offset}: {len(data)} posts → {new_on_page} new ({len(records)} total)")

        if len(data) < PER_PAGE:
            log.info(f"[{source_name}] Last page reached")
            break
        offset += PER_PAGE

    log.info(f"[{source_name}] Done: {len(records)} posts")
    return records


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE: Eporner API
# ══════════════════════════════════════════════════════════════════════════════
def scrape_eporner(max_videos: int = MAX_PER_SOURCE) -> list[dict]:
    """
    Fetch videos from Eporner's official JSON API.
    API: GET /api/v2/video/search/?query=all&per_page=100&page=N&order=latest&format=json
    Response: {total_count, total_pages, videos: [{id, title, url, added, length_min, views, default_thumb}]}
    
    Uses "latest" order only — we accumulate across runs so one order is enough.
    """
    records  = {}
    PER_PAGE = 100
    page     = 1
    total_pages = None

    log.info(f"[eporner] Scraping API (target: {max_videos})")

    while len(records) < max_videos:
        url  = (
            f"https://www.eporner.com/api/v2/video/search/"
            f"?query=all&per_page={PER_PAGE}&page={page}"
            f"&thumbsize=big&order=latest&gay=0&lq=1&format=json"
        )
        data = api_get(url)

        if not data or not isinstance(data, dict):
            log.warning("[eporner] Unexpected API response, stopping")
            break

        if total_pages is None:
            total_pages = data.get("total_pages", 1)
            log.info(f"[eporner] {data.get('total_count', '?')} total videos, {total_pages} pages")

        videos = data.get("videos") or []
        if not videos:
            break

        new_on_page = 0
        for v in videos:
            vid   = v.get("id", "")
            title = (v.get("title") or "").strip()
            if is_placeholder(title) or not vid:
                continue

            rid = f"eporner:{vid}"
            if rid in records:
                continue

            # Thumbnail
            thumb = None
            dt_d  = v.get("default_thumb")
            if dt_d and dt_d.get("src"):
                thumb = dt_d["src"]
            elif v.get("thumbs"):
                thumb = (v["thumbs"][0] or {}).get("src")

            # Date
            date_str = None
            added = v.get("added")
            if added:
                try:
                    dt = datetime.strptime(added, "%Y-%m-%d %H:%M:%S")
                    date_str = dt.replace(tzinfo=timezone.utc).isoformat()
                except Exception:
                    pass

            records[rid] = {
                "id":         rid,
                "title":      title,
                "source":     "eporner",
                "url":        v.get("url") or f"https://www.eporner.com/hd-porn/{vid}/",
                "file_count": 1,
                "has_videos": True,
                "duration":   v.get("length_min"),
                "views":      v.get("views", 0),
                "date":       date_str,
                "indexed_at": now_iso(),
                "thumbnail":  thumb,
            }
            new_on_page += 1

        log.info(f"[eporner] page {page}/{total_pages}: {new_on_page} new ({len(records)} total)")

        if page >= (total_pages or 1):
            break
        page += 1

    result = list(records.values())
    log.info(f"[eporner] Done: {len(result)} videos")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    log.info("=" * 60)
    log.info("MediaIndex scraper v8 — API sources only")
    log.info(f"  kemono={ENABLE_KEMONO}  coomer={ENABLE_COOMER}  eporner={ENABLE_EPORNER}")
    log.info(f"  max_per_source={MAX_PER_SOURCE}")
    log.info("=" * 60)

    albums    = load_existing()
    new_count = 0

    if ENABLE_KEMONO:
        for rec in scrape_kemono_api("https://kemono.su", "kemono", MAX_PER_SOURCE):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec
                new_count += 1

    if ENABLE_COOMER:
        for rec in scrape_kemono_api("https://coomer.su", "coomer", MAX_PER_SOURCE):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec
                new_count += 1

    if ENABLE_EPORNER:
        for rec in scrape_eporner(MAX_PER_SOURCE):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec
                new_count += 1

    meta = save(albums, new_count)

    # ── Commit guard ───────────────────────────────────────────────────────────
    total = meta["total"]
    ph    = meta["placeholder_count"]

    if not FORCE_COMMIT:
        if total == 0:
            log.error("COMMIT GUARD: 0 records — scraper got nothing, not committing")
            sys.exit(1)
        if total > 0 and ph / total > 0.05:
            log.error(f"COMMIT GUARD: placeholder ratio {ph/total:.1%} > 5% — not committing")
            sys.exit(1)

    log.info(f"✓ Safe to commit: {total} records, {new_count} new, {ph} placeholders")


if __name__ == "__main__":
    main()
