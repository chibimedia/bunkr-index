#!/usr/bin/env python3
"""
MediaIndex scraper — clean, no raw IP tricks, no dead domains.

Sources:
  1. Eporner   — /pornstar-list/?sort=most-popular scrape (model name + video count)
  2. Kemono.su — /api/v1/creators.txt  (bulk JSON, one request)
  3. Coomer.su — /api/v1/creators.txt  (same API)
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

HERE          = Path(__file__).parent.resolve()
OUT_FILE      = HERE / "albums.json"
VALIDATION    = HERE / "validation.json"

MAX_MODELS    = int(os.getenv("MAX_MODELS",    "2000"))
DELAY         = float(os.getenv("DELAY",       "1.5"))
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

# ── HTTP — always use hostname, never raw IPs ──────────────────────────────
_sess = requests.Session()
_sess.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})

def get_json(url: str) -> Optional[Any]:
    for attempt in range(1, 4):
        time.sleep(DELAY + random.uniform(0, 0.5))
        try:
            r = _sess.get(url, timeout=30, headers={"Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                log.warning(f"429 — sleeping 45s"); time.sleep(45)
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"get_json attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
    return None

def get_html(url: str) -> Optional[str]:
    for attempt in range(1, 4):
        time.sleep(DELAY + random.uniform(0, 0.5))
        try:
            r = _sess.get(url, timeout=30)
            if r.status_code == 200:
                if len(r.text) < 3000 and "checking your browser" in r.text.lower():
                    log.warning(f"CF block on {url}"); return None
                return r.text
            elif r.status_code == 429:
                log.warning(f"429 — sleeping 45s"); time.sleep(45)
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"get_html attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
    return None

# ── Persistence ────────────────────────────────────────────────────────────
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

# ══════════════════════════════════════════════════════════════════════════
# Eporner — pornstar profile pages
# ══════════════════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════════════════
# Kemono / Coomer — bulk creators endpoint
# ══════════════════════════════════════════════════════════════════════════
def scrape_creators(base_url: str, source: str, max_models: int) -> List[dict]:
    """
    GET {base_url}/api/v1/creators.txt
    Returns JSON array: [{id, name, service, indexed, updated, public_id}]
    Single request, no pagination, no CF issues.
    base_url examples: https://kemono.su  https://coomer.su
    """
    log.info(f"[{source}] Fetching {base_url}/api/v1/creators.txt")
    data = get_json(f"{base_url}/api/v1/creators.txt")
    if not data or not isinstance(data, list):
        log.error(f"[{source}] No data — got {type(data)}. Check if endpoint is up.")
        return []
    log.info(f"[{source}] {len(data)} creators returned")
    records, seen = [], set()
    for c in data[:max_models]:
        cid  = str(c.get("id",""))
        name = (c.get("name") or "").strip()
        svc  = str(c.get("service",""))
        if is_placeholder(name) or not cid: continue
        rid = f"{source}:{svc}:{cid}"
        if rid in seen: continue
        seen.add(rid)
        date_str = None
        for raw in [c.get("updated"), c.get("indexed")]:
            if raw:
                try:
                    dt = datetime.fromisoformat(str(raw).replace(" ","T"))
                    date_str = dt.replace(tzinfo=timezone.utc).isoformat() if not dt.tzinfo else dt.isoformat()
                    break
                except Exception: pass
        records.append({
            "id": rid, "title": name, "source": source, "service": svc,
            "url": f"{base_url}/{svc}/user/{cid}",
            "file_count": 0, "has_videos": False,
            "date": date_str, "indexed_at": now_iso(),
        })
    log.info(f"[{source}] Done: {len(records)} creators")
    return records

# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════
def main():
    log.info("="*60)
    log.info(f"MediaIndex scraper | eporner={ENABLE_EPORNER} kemono={ENABLE_KEMONO} coomer={ENABLE_COOMER}")
    log.info("="*60)
    albums, new_count = load_existing(), 0

    if ENABLE_EPORNER:
        for rec in scrape_eporner(MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec; new_count += 1

    if ENABLE_KEMONO:
        for rec in scrape_creators("https://kemono.su", "kemono", MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec; new_count += 1

    if ENABLE_COOMER:
        for rec in scrape_creators("https://coomer.su", "coomer", MAX_MODELS):
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
