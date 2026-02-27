#!/usr/bin/env python3
"""
MediaIndex scraper

Sources:
  1. Eporner   — official JSON API /api/v2/video/search/ with pornstar query
                 (HTML selector broke; API is stable and confirmed working)
  2. Kemono.cr — per-service API with cloudscraper fallback (plain requests = 403)
  3. Coomer.st — same

Kemono/Coomer status:
  - All per-service API endpoints return 403 on GitHub Actions IPs
  - cloudscraper handles CF JS challenges and some WAF blocks
  - If still 403 after cloudscraper → logged and skipped (don't block the run)
"""
from __future__ import annotations
import json, logging, os, re, sys, time, random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scraper")

HERE         = Path(__file__).parent.resolve()
OUT_FILE     = HERE / "albums.json"
VALIDATION   = HERE / "validation.json"
DEBUG_DIR    = HERE / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

MAX_MODELS    = int(os.getenv("MAX_MODELS",   "5000"))
DELAY         = float(os.getenv("DELAY",      "1.0"))
FORCE_COMMIT  = os.getenv("FORCE_COMMIT",  "false").lower() == "true"
ENABLE_KEMONO  = os.getenv("ENABLE_KEMONO",  "true").lower() != "false"
ENABLE_COOMER  = os.getenv("ENABLE_COOMER",  "true").lower() != "false"
ENABLE_EPORNER = os.getenv("ENABLE_EPORNER", "true").lower() != "false"

DENYLIST = {"","just a moment","checking your browser","access denied","403",
            "forbidden","welcome","welcome!","error","503","cloudflare","untitled"}

def is_placeholder(t: str) -> bool:
    return (t or "").strip().lower() in DENYLIST or len((t or "").strip()) < 2

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def save_debug(name: str, content: bytes) -> None:
    try:
        (DEBUG_DIR / name).write_bytes(content)
    except Exception:
        pass

# ── HTTP — plain requests + cloudscraper fallback ──────────────────────────────
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/html, */*",
}

_sess = requests.Session()
_sess.headers.update(_HEADERS)

# Try to import cloudscraper once
try:
    import cloudscraper as _cs_mod
    _cs = _cs_mod.create_scraper(browser={"browser":"chrome","platform":"windows","mobile":False})
    _cs.headers.update(_HEADERS)
    HAS_CLOUDSCRAPER = True
    log.info("cloudscraper available ✓")
except ImportError:
    HAS_CLOUDSCRAPER = False
    log.warning("cloudscraper not installed — install it: pip install cloudscraper")

def fetch_json(url: str, referer: str = "") -> Optional[Any]:
    """Try requests → cloudscraper fallback. Returns parsed JSON or None."""
    hdrs = {"Referer": referer, "Origin": referer.rstrip("/")} if referer else {}

    for attempt in range(1, 4):
        time.sleep(DELAY + random.uniform(0, 0.3))
        try:
            r = _sess.get(url, headers=hdrs, timeout=30)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                log.warning(f"429 rate-limit, sleeping 60s"); time.sleep(60); continue
            elif r.status_code == 403:
                log.warning(f"403 on {url} — trying cloudscraper")
                save_debug(f"403_{url.replace('/','_')[:80]}.html", r.content)
                break  # fall through to cloudscraper
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"fetch_json attempt {attempt}: {e}")
        time.sleep(2 ** attempt)

    # Cloudscraper fallback
    if HAS_CLOUDSCRAPER:
        try:
            time.sleep(DELAY + random.uniform(0.5, 1.5))
            r2 = _cs.get(url, headers=hdrs, timeout=30)
            if r2.status_code == 200:
                log.info(f"cloudscraper succeeded for {url}")
                return r2.json()
            else:
                log.warning(f"cloudscraper also got {r2.status_code} for {url}")
                save_debug(f"cs_{r2.status_code}_{url.replace('/','_')[:80]}.html", r2.content)
        except Exception as e:
            log.warning(f"cloudscraper error: {e}")

    return None

# ── Persistence ────────────────────────────────────────────────────────────────
def load_existing() -> Dict[str, Any]:
    if OUT_FILE.exists():
        try:
            j = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            d = {a["id"]: a for a in j.get("albums", [])}
            log.info(f"Loaded {len(d)} existing records"); return d
        except Exception as e:
            log.warning(f"Could not load albums.json: {e}")
    return {}

def save(albums: Dict[str, Any], new_count: int) -> dict:
    rows = sorted(albums.values(), key=lambda a: a.get("date") or a.get("indexed_at") or "", reverse=True)
    ph   = sum(1 for a in rows if is_placeholder(a.get("title","")))
    meta = {"total": len(rows), "new_this_run": new_count, "placeholder_count": ph,
            "last_updated": now_iso(), "sources": sorted({a.get("source","?") for a in rows})}
    OUT_FILE.write_text(json.dumps({"meta": meta, "albums": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    per = {f"{s}_count": sum(1 for a in rows if a.get("source")==s) for s in ["kemono","coomer","eporner"]}
    VALIDATION.write_text(json.dumps({**meta, **per}, indent=2), encoding="utf-8")
    log.info(f"✓ {len(rows)} total | {new_count} new | {ph} placeholders")
    return meta

# ══════════════════════════════════════════════════════════════════════════════
# Eporner — official JSON API (stable, no HTML parsing needed)
# GET /api/v2/video/search/?query=all&order=latest&per_page=100&page=N&format=json
# Each video has: id, title, url, added, length_min, views, default_thumb
# We index unique pornstar names by parsing the title keywords field,
# but simpler: just index by pornstar search pages via the search API
# ══════════════════════════════════════════════════════════════════════════════
def scrape_eporner(max_models: int) -> List[dict]:
    """
    Use eporner search API to get videos, deduplicate by extracting
    performer names from keywords. Fallback: index videos directly if no
    performer extraction possible — at minimum we get real content.
    
    API: /api/v2/video/search/?query=all&order=latest&per_page=100&page=N&format=json
    """
    records: Dict[str, dict] = {}
    page = 1
    total_pages = None
    log.info(f"[eporner] Using JSON API (target: {max_models})")

    while len(records) < max_models:
        url = (f"https://www.eporner.com/api/v2/video/search/"
               f"?query=all&per_page=100&page={page}&order=most-popular&format=json&thumbsize=medium")
        data = fetch_json(url)

        if not data or not isinstance(data, dict):
            log.warning(f"[eporner] No data on page {page}, stopping"); break

        if total_pages is None:
            total_pages = data.get("total_pages", 1)
            log.info(f"[eporner] {data.get('total_count','?')} total videos across {total_pages} pages")

        videos = data.get("videos") or []
        if not videos:
            break

        for v in videos:
            vid   = v.get("id","")
            title = (v.get("title") or "").strip()
            if not vid or is_placeholder(title):
                continue

            rid = f"eporner:{vid}"
            if rid in records:
                continue

            date_str = None
            added = v.get("added")
            if added:
                try:
                    dt = datetime.strptime(added, "%Y-%m-%d %H:%M:%S")
                    date_str = dt.replace(tzinfo=timezone.utc).isoformat()
                except Exception: pass

            records[rid] = {
                "id": rid, "title": title, "source": "eporner",
                "url": v.get("url") or f"https://www.eporner.com/hd-porn/{vid}/",
                "file_count": 1, "has_videos": True,
                "views": v.get("views", 0),
                "date": date_str, "indexed_at": now_iso(),
            }

            if len(records) >= max_models:
                break

        log.info(f"[eporner] page {page}/{total_pages}: {len(records)} total")
        if page >= (total_pages or 1):
            break
        page += 1

    result = list(records.values())
    log.info(f"[eporner] Done: {len(result)} videos")
    return result

# ══════════════════════════════════════════════════════════════════════════════
# Kemono / Coomer — per-service creator API + cloudscraper fallback
# ══════════════════════════════════════════════════════════════════════════════
KEMONO_SERVICES = ["patreon","fanbox","gumroad","subscribestar","dlsite","fantia","boosty","afdian"]
COOMER_SERVICES = ["onlyfans","fansly","candfans"]

def scrape_creators_by_service(base_url: str, source: str,
                                services: List[str], max_total: int) -> List[dict]:
    all_records: Dict[str, dict] = {}
    log.info(f"[{source}] Fetching creators via per-service API + cloudscraper fallback")

    for svc in services:
        if len(all_records) >= max_total:
            break
        offset = 0
        svc_count = 0
        log.info(f"[{source}] service={svc}")

        while len(all_records) < max_total:
            url  = f"{base_url}/api/v1/{svc}/creators?o={offset}"
            data = fetch_json(url, referer=f"{base_url}/")

            if data is None:
                log.warning(f"[{source}] {svc} blocked after all attempts, skipping")
                break

            if not isinstance(data, list) or len(data) == 0:
                break

            for c in data:
                cid  = str(c.get("id",""))
                name = (c.get("name") or "").strip()
                if is_placeholder(name) or not cid:
                    continue
                rid = f"{source}:{svc}:{cid}"
                if rid in all_records:
                    continue

                date_str = None
                for raw in [c.get("updated"), c.get("indexed")]:
                    if raw:
                        try:
                            dt = datetime.fromisoformat(str(raw).replace(" ","T"))
                            date_str = (dt.replace(tzinfo=timezone.utc) if not dt.tzinfo else dt).isoformat()
                            break
                        except Exception: pass

                all_records[rid] = {
                    "id": rid, "title": name, "source": source, "service": svc,
                    "url": f"{base_url}/{svc}/user/{cid}",
                    "file_count": 0, "has_videos": False,
                    "date": date_str, "indexed_at": now_iso(),
                }
                svc_count += 1

            if len(data) < 50:
                break
            offset += 50

        log.info(f"[{source}] {svc}: {svc_count} creators")

    result = list(all_records.values())
    log.info(f"[{source}] Done: {len(result)} total creators")
    return result

# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
def main():
    log.info("="*60)
    log.info(f"MediaIndex | eporner={ENABLE_EPORNER} kemono={ENABLE_KEMONO} coomer={ENABLE_COOMER}")
    log.info("="*60)
    albums, new_count = load_existing(), 0

    if ENABLE_EPORNER:
        for rec in scrape_eporner(MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec; new_count += 1

    if ENABLE_KEMONO:
        for rec in scrape_creators_by_service("https://kemono.cr", "kemono", KEMONO_SERVICES, MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec; new_count += 1

    if ENABLE_COOMER:
        for rec in scrape_creators_by_service("https://coomer.st", "coomer", COOMER_SERVICES, MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec; new_count += 1

    meta = save(albums, new_count)
    total, ph = meta["total"], meta["placeholder_count"]
    if not FORCE_COMMIT:
        if total == 0:
            log.error("COMMIT GUARD: 0 records"); sys.exit(1)
        if ph / total > 0.05:
            log.error(f"COMMIT GUARD: {ph/total:.1%} placeholders"); sys.exit(1)
    log.info(f"✓ {total} records, {new_count} new, {ph} placeholders")

if __name__ == "__main__":
    main()
