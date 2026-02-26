#!/usr/bin/env python3
"""
MediaIndex scraper v9

Sources:
  1. Eporner — pornstar profiles (name + video count), scraped from /pornstar-list/
  2. Kemono.su / Coomer.su — creator list from /api/v1/creators.txt (bulk endpoint)

Eporner approach:
  - Scrape /pornstar-list/?sort=most-popular&page=N to get model names + slugs
  - Use API search with name to get video count per model
  - Record = one entry per model (not per video)

Kemono/Coomer approach:
  - GET /api/v1/creators.txt — returns ALL creators as JSON array in one shot
  - Fields: id, name, service, indexed, updated, public_id
  - No pagination needed, no rate limit issues
  - Record = one entry per creator
"""

import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

OUT_FILE       = Path("albums.json")
VALIDATION     = Path("validation.json")
MAX_MODELS     = int(os.getenv("MAX_MODELS", "2000"))
DELAY          = float(os.getenv("DELAY", "2.0"))
FORCE_COMMIT   = os.getenv("FORCE_COMMIT", "false").lower() == "true"
ENABLE_KEMONO  = os.getenv("ENABLE_KEMONO",  "true").lower() != "false"
ENABLE_COOMER  = os.getenv("ENABLE_COOMER",  "true").lower() != "false"
ENABLE_EPORNER = os.getenv("ENABLE_EPORNER", "true").lower() != "false"

DENYLIST = {
    "", "just a moment", "checking your browser", "access denied",
    "403", "forbidden", "welcome", "welcome!", "error", "503",
    "attention required", "cloudflare", "untitled",
}

def is_placeholder(title: str) -> bool:
    t = (title or "").strip().lower()
    return t in DENYLIST or len(t) < 2

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── HTTP ───────────────────────────────────────────────────────────────────────
_sess = requests.Session()
_sess.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})

def get_json(url: str, retries: int = 3) -> Optional[dict | list]:
    for attempt in range(1, retries + 1):
        time.sleep(DELAY + random.uniform(0, 0.5))
        try:
            r = _sess.get(url, timeout=30, headers={"Accept": "application/json"})
            log.debug(f"GET {url} → {r.status_code}")
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                wait = 45 + attempt * 15
                log.warning(f"429 rate-limited — sleeping {wait}s")
                time.sleep(wait)
            elif r.status_code in (403, 503):
                log.warning(f"HTTP {r.status_code} on {url} (attempt {attempt})")
                time.sleep(10 + attempt * 5)
            elif r.status_code == 404:
                return None
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"Request error attempt {attempt}: {e}")
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None

def get_html(url: str, retries: int = 3) -> Optional[str]:
    for attempt in range(1, retries + 1):
        time.sleep(DELAY + random.uniform(0, 0.5))
        try:
            r = _sess.get(url, timeout=30, headers={"Accept": "text/html"})
            log.debug(f"GET {url} → {r.status_code} ({len(r.content)}B)")
            if r.status_code == 200:
                # Basic CF check
                if len(r.text) < 3000 and "checking your browser" in r.text.lower():
                    log.warning(f"CF block detected on {url}")
                    return None
                return r.text
            elif r.status_code == 429:
                time.sleep(45 + attempt * 15)
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
    rows = sorted(
        albums.values(),
        key=lambda a: a.get("date") or a.get("indexed_at") or "",
        reverse=True,
    )
    ph = sum(1 for a in rows if is_placeholder(a.get("title", "")))
    meta = {
        "total":             len(rows),
        "new_this_run":      new_count,
        "placeholder_count": ph,
        "last_updated":      now_iso(),
        "sources":           sorted({a.get("source", "?") for a in rows}),
    }
    OUT_FILE.write_text(json.dumps({"meta": meta, "albums": rows},
                                    ensure_ascii=False, indent=2))
    per_src = {f"{s}_count": sum(1 for a in rows if a.get("source") == s)
               for s in ["kemono", "coomer", "eporner"]}
    VALIDATION.write_text(json.dumps({**meta, **per_src}, indent=2))
    log.info(f"✓ {len(rows)} total | {new_count} new | {ph} placeholders")
    return meta


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 1: Eporner — pornstar profiles
# ══════════════════════════════════════════════════════════════════════════════
def scrape_eporner_models(max_models: int) -> list[dict]:
    """
    Scrape eporner.com/pornstar-list/ for model profiles.
    Each record = one model with name + total video count + profile URL.

    Page structure:
      /pornstar-list/?sort=most-popular&page=N
      Each model card: <a href="/pornstar/{name}-{id}/">
        <h3> or <p> with the name
        A video count like "123 Videos"
    """
    records = []
    seen    = set()
    page    = 1

    log.info(f"[eporner] Scraping pornstar profiles (target: {max_models})")

    while len(records) < max_models:
        url  = f"https://www.eporner.com/pornstar-list/?sort=most-popular&page={page}"
        html = get_html(url)

        if not html:
            log.warning(f"[eporner] No HTML on page {page}, stopping")
            break

        soup = BeautifulSoup(html, "html.parser")

        # Each pornstar card is an <a> with href matching /pornstar/name-id/
        cards = soup.find_all("a", href=re.compile(r"^/pornstar/[^/]+-\w{5}/"))
        if not cards:
            log.info(f"[eporner] No cards on page {page}, done")
            break

        new_here = 0
        for card in cards:
            href = card.get("href", "")
            # Extract slug and ID from href like /pornstar/mia-malkova-oPgtJ/
            m = re.match(r"/pornstar/(.+)-(\w{5})/?$", href)
            if not m:
                continue

            slug   = m.group(1)   # e.g. "mia-malkova"
            ps_id  = m.group(2)   # e.g. "oPgtJ"
            rid    = f"eporner:ps:{ps_id}"

            if rid in seen:
                continue
            seen.add(rid)

            # Model name: from h3, or convert slug
            name_tag = card.find(["h3", "p", "span"])
            if name_tag:
                name = name_tag.get_text(strip=True)
            else:
                name = slug.replace("-", " ").title()

            if is_placeholder(name):
                continue

            # Video count: look for "NNN Videos" text in the card
            card_text = card.get_text(" ", strip=True)
            vc_match  = re.search(r"([\d,]+)\s+videos?", card_text, re.I)
            video_count = 0
            if vc_match:
                video_count = int(vc_match.group(1).replace(",", ""))

            records.append({
                "id":         rid,
                "title":      name,
                "source":     "eporner",
                "url":        f"https://www.eporner.com/pornstar/{slug}-{ps_id}/",
                "file_count": video_count,
                "has_videos": True,
                "date":       None,
                "indexed_at": now_iso(),
            })
            new_here += 1

        log.info(f"[eporner] page {page}: {new_here} new models ({len(records)} total)")

        if new_here == 0:
            break
        page += 1

    log.info(f"[eporner] Done: {len(records)} models")
    return records


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 2: Kemono / Coomer — creator list
# ══════════════════════════════════════════════════════════════════════════════
def scrape_kemono_creators(base_url: str, source_name: str, max_creators: int) -> list[dict]:
    """
    Fetch all creators from Kemono/Coomer using the bulk creators endpoint.

    GET {base_url}/api/v1/creators.txt
    Returns: JSON array of creator objects:
      {id, name, service, indexed, updated, public_id}

    This is a single request for ALL creators — no pagination, no rate limit issues.
    Much more reliable than /posts/recently-updated from CI IPs.
    """
    log.info(f"[{source_name}] Fetching creators list from {base_url}")

    url  = f"{base_url}/api/v1/creators.txt"
    data = get_json(url)

    if not data:
        log.error(f"[{source_name}] Failed to fetch creators.txt — got no data")
        log.error(f"[{source_name}] This is why 0 records: CI IP may be rate-limited")
        return []

    if not isinstance(data, list):
        log.error(f"[{source_name}] Unexpected response type: {type(data)}")
        return []

    log.info(f"[{source_name}] Got {len(data)} total creators")

    records = []
    seen    = set()

    for creator in data[:max_creators]:
        try:
            cid   = str(creator.get("id", ""))
            name  = (creator.get("name") or "").strip()
            svc   = str(creator.get("service", ""))
            indexed = creator.get("indexed")
            updated = creator.get("updated")

            if is_placeholder(name) or not cid:
                continue

            rid = f"{source_name}:{svc}:{cid}"
            if rid in seen:
                continue
            seen.add(rid)

            # Parse date from indexed/updated
            date_str = None
            for raw in [updated, indexed]:
                if raw:
                    try:
                        dt = datetime.fromisoformat(str(raw).replace(" ", "T"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        date_str = dt.isoformat()
                        break
                    except Exception:
                        pass

            records.append({
                "id":         rid,
                "title":      name,
                "source":     source_name,
                "service":    svc,
                "url":        f"{base_url}/{svc}/user/{cid}",
                "file_count": 0,   # creator list doesn't include post count
                "has_videos": False,
                "date":       date_str,
                "indexed_at": now_iso(),
            })

        except Exception as e:
            log.warning(f"[{source_name}] Parse error: {e}")

    log.info(f"[{source_name}] Done: {len(records)} creators")
    return records


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    log.info("=" * 60)
    log.info("MediaIndex scraper v9")
    log.info(f"  eporner={ENABLE_EPORNER} kemono={ENABLE_KEMONO} coomer={ENABLE_COOMER}")
    log.info(f"  max_models={MAX_MODELS}")
    log.info("=" * 60)

    albums    = load_existing()
    new_count = 0

    if ENABLE_EPORNER:
        for rec in scrape_eporner_models(MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec
                new_count += 1

    if ENABLE_KEMONO:
        for rec in scrape_kemono_creators("https://kemono.su", "kemono", MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec
                new_count += 1

    if ENABLE_COOMER:
        for rec in scrape_kemono_creators("https://coomer.su", "coomer", MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec
                new_count += 1

    meta = save(albums, new_count)

    total = meta["total"]
    ph    = meta["placeholder_count"]

    if not FORCE_COMMIT:
        if total == 0:
            log.error("COMMIT GUARD: 0 records — not committing")
            sys.exit(1)
        if total > 0 and ph / total > 0.05:
            log.error(f"COMMIT GUARD: {ph/total:.1%} placeholders — not committing")
            sys.exit(1)

    log.info(f"✓ Safe to commit: {total} records, {new_count} new, {ph} placeholders")


if __name__ == "__main__":
    main()
