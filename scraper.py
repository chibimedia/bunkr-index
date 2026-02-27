#!/usr/bin/env python3
"""
MediaIndex scraper

Sources:
  1. Eporner   — /pornstar-list/ HTML scrape (model name + video count)
  2. Kemono.cr — per-service API /api/v1/{service}/creators (no auth, no JS needed)
  3. Coomer.st — same per-service API

Why per-service API:
  - /api/v1/creators (bulk)  → 403 on CI IPs
  - /artists HTML page       → JS-rendered, requests gets empty shell
  - /api/v1/{svc}/creators   → works without auth, plain JSON, paginated
"""
from __future__ import annotations
import json, logging, os, re, sys, time, random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scraper")

HERE         = Path(__file__).parent.resolve()
OUT_FILE     = HERE / "albums.json"
VALIDATION   = HERE / "validation.json"

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

# ── HTTP ───────────────────────────────────────────────────────────────────────
_sess = requests.Session()
_sess.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})

def get_json(url: str) -> Optional[Any]:
    for attempt in range(1, 4):
        time.sleep(DELAY + random.uniform(0, 0.3))
        try:
            r = _sess.get(url, timeout=30, headers={"Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                log.warning("429 — sleeping 60s"); time.sleep(60)
            elif r.status_code == 403:
                log.warning(f"403 on {url} — endpoint blocked, skipping")
                return None
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"get_json attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
    return None

def get_html(url: str) -> Optional[str]:
    for attempt in range(1, 4):
        time.sleep(DELAY + random.uniform(0, 0.3))
        try:
            r = _sess.get(url, timeout=30)
            if r.status_code == 200:
                if len(r.text) < 3000 and "checking your browser" in r.text.lower():
                    log.warning(f"CF block on {url}"); return None
                return r.text
            elif r.status_code == 429:
                log.warning("429 — sleeping 60s"); time.sleep(60)
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"get_html attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
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
# Eporner — pornstar profile pages (HTML, no JS needed)
# ══════════════════════════════════════════════════════════════════════════════
def scrape_eporner(max_models: int) -> List[dict]:
    records, seen, page = [], set(), 1
    log.info(f"[eporner] Scraping pornstar list (target: {max_models})")
    while len(records) < max_models:
        url  = f"https://www.eporner.com/pornstar-list/?sort=most-popular&page={page}"
        html = get_html(url)
        if not html:
            log.warning(f"[eporner] No HTML page {page}, stopping"); break
        soup  = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("a", href=re.compile(r"^/pornstar/[^/]+-\w{5}/"))
        if not cards:
            log.info(f"[eporner] No cards on page {page}, done"); break
        new_here = 0
        for card in cards:
            m = re.match(r"/pornstar/(.+)-(\w{5})/?$", card.get("href",""))
            if not m: continue
            slug, ps_id = m.group(1), m.group(2)
            rid = f"eporner:ps:{ps_id}"
            if rid in seen: continue
            seen.add(rid)
            name_tag = card.find(["h3","p","span"])
            name = name_tag.get_text(strip=True) if name_tag else slug.replace("-"," ").title()
            if is_placeholder(name): continue
            vc = re.search(r"([\d,]+)\s+videos?", card.get_text(" ", strip=True), re.I)
            records.append({
                "id": rid, "title": name, "source": "eporner",
                "url": f"https://www.eporner.com/pornstar/{slug}-{ps_id}/",
                "file_count": int(vc.group(1).replace(",","")) if vc else 0,
                "has_videos": True, "date": None, "indexed_at": now_iso(),
            })
            new_here += 1
            if len(records) >= max_models: break
        log.info(f"[eporner] page {page}: {new_here} new ({len(records)} total)")
        if new_here == 0: break
        page += 1
    log.info(f"[eporner] Done: {len(records)} models")
    return records

# ══════════════════════════════════════════════════════════════════════════════
# Kemono / Coomer — per-service creator API (works without auth)
#
# /api/v1/creators (bulk)  → 403 blocked on CI
# /artists HTML            → JS-rendered, requests gets empty shell
# /api/v1/{svc}/creators   → ✓ plain JSON, no auth, paginated at 50/page
#
# Services on kemono.cr: patreon, fanbox, gumroad, subscribestar, dlsite,
#                        fantia, boosty, afdian
# Services on coomer.st: onlyfans, fansly, candfans
# ══════════════════════════════════════════════════════════════════════════════
KEMONO_SERVICES = ["patreon", "fanbox", "gumroad", "subscribestar",
                   "dlsite", "fantia", "boosty", "afdian"]
COOMER_SERVICES = ["onlyfans", "fansly", "candfans"]

def scrape_creators_by_service(base_url: str, source: str,
                                services: list[str], max_total: int) -> List[dict]:
    """
    GET {base_url}/api/v1/{service}/creators?o=N
    Returns JSON array of creator objects: [{id, name, service, updated, indexed}]
    50 per page, offset pagination.
    """
    all_records: Dict[str, dict] = {}
    log.info(f"[{source}] Fetching creators via per-service API ({len(services)} services)")

    for svc in services:
        if len(all_records) >= max_total:
            break
        offset = 0
        svc_count = 0
        log.info(f"[{source}] service={svc}")

        while len(all_records) < max_total:
            url  = f"{base_url}/api/v1/{svc}/creators?o={offset}"
            data = get_json(url)

            if data is None:
                # 403 or error — skip this service
                log.warning(f"[{source}] {svc} returned None, skipping service")
                break

            if not isinstance(data, list) or len(data) == 0:
                break  # end of this service's pages

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
                break  # last page
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
