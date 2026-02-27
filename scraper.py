#!/usr/bin/env python3
# scraper.py — orchestration (safe, uses fetcher tiered HTTP)
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scraper")

HERE = Path(__file__).parent.resolve()
OUT_FILE = HERE / "albums.json"
VALIDATION_FILE = HERE / "validation.json"
DEBUG_DIR = HERE / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

MAX_MODELS = int(os.getenv("MAX_MODELS", "2000"))
DELAY = float(os.getenv("DELAY", "2.0"))
FORCE_COMMIT = os.getenv("FORCE_COMMIT", "false").lower() == "true"

# defaults (can be toggled via env)
ENABLE_KEMONO = os.getenv("ENABLE_KEMONO", "true").lower() != "false"
ENABLE_COOMER = os.getenv("ENABLE_COOMER", "true").lower() != "false"
ENABLE_EPORNER = os.getenv("ENABLE_EPORNER", "true").lower() != "false"

# Titles we consider placeholders
DENYLIST = {
    "", "just a moment", "checking your browser", "access denied", "403", "forbidden",
    "welcome", "welcome!", "error", "503", "attention required", "cloudflare", "untitled",
}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def is_placeholder(title: str) -> bool:
    t = (title or "").strip().lower()
    return t in DENYLIST or len(t) < 2

# Try to import fetcher (tiered fetch). If missing, fall back to requests.
try:
    import fetcher  # project file fetcher.py — provides fetch, fetch_json, is_cf_block, save_debug
    _have_fetcher = True
    log.info("Using local fetcher (tiered fetch: requests -> cloudscraper -> playwright)")
except Exception:
    _have_fetcher = False
    import requests
    log.warning("fetcher.py not available — falling back to requests (less reliable)")

# -------------------- Helpers for HTTP using fetcher when possible --------------------
def fetch_json(url: str, **kwargs) -> Optional[dict | list]:
    if _have_fetcher:
        return fetcher.fetch_json(url, **kwargs)
    try:
        r = requests.get(url, timeout=30, headers={"Accept": "application/json"})
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug(f"[fallback json] {e}")
    return None

def fetch_html(url: str, site: str = "unknown", slug: str = "", prefer_cs: bool = False, force_playwright: bool = False, **kwargs) -> Optional[str]:
    """
    Fetch HTML using fetcher.fetch (auto tier). Returns None on failure or CF detection.
    """
    if _have_fetcher:
        txt = fetcher.fetch(url, site=site, slug=slug, prefer_cs=prefer_cs, force_playwright=force_playwright)
        # fetcher already saves debug on CF and returns None if blocked
        return txt
    # fallback: simple requests
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            txt = r.text
            low = (txt or "").lower()
            if "checking your browser" in low or "just a moment" in low or len(txt) < 500:
                # consider blocked
                log.warning(f"[fallback] CF-like response for {url}")
                try:
                    p = DEBUG_DIR / f"fallback_{site}_{slug or 'page'}.html"
                    p.write_text(txt, encoding="utf-8", errors="replace")
                    log.info(f"[fallback] saved debug to {p}")
                except Exception:
                    pass
                return None
            return txt
    except Exception as e:
        log.warning(f"[fallback] fetch_html error: {e}")
    return None

# -------------------- Persistence helpers --------------------
def load_existing() -> Dict[str, Dict[str, Any]]:
    if OUT_FILE.exists():
        try:
            j = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            return {a["id"]: a for a in j.get("albums", [])}
        except Exception:
            log.exception("Failed to read existing albums.json — starting fresh")
            return {}
    return {}

def save(albums: Dict[str, Dict[str, Any]], new_count: int) -> Dict[str, Any]:
    rows = sorted(albums.values(), key=lambda a: a.get("date") or a.get("indexed_at") or "", reverse=True)
    ph = sum(1 for a in rows if is_placeholder(a.get("title", "")))
    meta = {
        "total": len(rows),
        "new_this_run": new_count,
        "placeholder_count": ph,
        "last_updated": now_iso(),
        "sources": sorted({a.get("source") or "?" for a in rows}),
    }
    OUT_FILE.write_text(json.dumps({"meta": meta, "albums": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    # per-source quick counts
    per_src = {f"{s}_count": sum(1 for a in rows if a.get("source") == s) for s in ["kemono", "coomer", "eporner"]}
    VALIDATION_FILE.write_text(json.dumps({**meta, **per_src}, indent=2), encoding="utf-8")
    log.info(f"✓ {len(rows)} total | {new_count} new | {ph} placeholders")
    return meta

# -------------------- EPORNER scraping (uses fetch_html) --------------------
from bs4 import BeautifulSoup
import re

def scrape_eporner_models(max_models: int) -> List[Dict[str, Any]]:
    """Scrape eporner.com/pornstar-list/?sort=most-popular&page=N for performer cards."""
    records: List[Dict[str, Any]] = []
    seen = set()
    page = 1
    log.info(f"[eporner] Scraping pornstar profiles (target: {max_models})")
    while len(records) < max_models:
        url = f"https://www.eporner.com/pornstar-list/?sort=most-popular&page={page}"
        html = fetch_html(url, site="eporner", slug=f"list-{page}", prefer_cs=False)
        if not html:
            log.warning(f"[eporner] No HTML on page {page} (maybe blocked); stopping")
            break
        soup = BeautifulSoup(html, "html.parser")
        # Each pornstar link: href like /pornstar/<slug>-<id>/
        cards = soup.find_all("a", href=re.compile(r"^/pornstar/[^/]+-\w{5}/"))
        if not cards:
            log.info(f"[eporner] No cards found on page {page}, stopping")
            break
        new_here = 0
        for card in cards:
            href = card.get("href", "")
            m = re.match(r"/pornstar/(.+)-(\w{5})/?$", href)
            if not m:
                continue
            slug = m.group(1)
            ps_id = m.group(2)
            rid = f"eporner:ps:{ps_id}"
            if rid in seen:
                continue
            seen.add(rid)
            name_tag = card.find(["h3", "p", "span"])
            name = name_tag.get_text(strip=True) if name_tag else slug.replace("-", " ").title()
            if is_placeholder(name):
                continue
            card_text = card.get_text(" ", strip=True)
            vc_match = re.search(r"([\d,]+)\s+videos?", card_text, re.I)
            video_count = int(vc_match.group(1).replace(",", "")) if vc_match else 0
            rec = {
                "id": rid,
                "title": name,
                "source": "eporner",
                "url": f"https://www.eporner.com/pornstar/{slug}-{ps_id}/",
                "file_count": video_count,
                "has_videos": bool(video_count),
                "date": None,
                "indexed_at": now_iso(),
            }
            records.append(rec)
            new_here += 1
            if len(records) >= max_models:
                break
        log.info(f"[eporner] page {page}: {new_here} new models ({len(records)} total)")
        if new_here == 0:
            break
        page += 1
    log.info(f"[eporner] Done: {len(records)} models")
    return records

# -------------------- Kemono / Coomer (bulk JSON) --------------------
import requests

def scrape_kemono_creators(base_url: str, source: str, max_models: int):
    """
    DNS-bypass version.
    Uses direct IP with manual Host header.
    """

    if source == "kemono":
        host = "kemono.su"
    else:
        host = "coomer.su"

    # Hardcoded IPs (current as of 2026 — may need updating later)
    ip_map = {
        "kemono.su": "172.67.205.156",
        "coomer.su": "172.67.205.156"
    }

    ip = ip_map.get(host)
    if not ip:
        log.error(f"[{source}] No IP mapping found")
        return

    url = f"https://{ip}/api/v1/creators"

    log.info(f"[{source}] Fetching creators list via IP {ip}")

    try:
        response = requests.get(
            url,
            headers={
                "Host": host,
                "User-Agent": "Mozilla/5.0"
            },
            timeout=30,
            verify=False  # required because cert won't match IP
        )
    except Exception as e:
        log.error(f"[{source}] Request failed: {e}")
        return

    if response.status_code != 200:
        log.error(f"[{source}] HTTP {response.status_code}")
        return

    try:
        data = response.json()
    except Exception:
        log.error(f"[{source}] Invalid JSON response")
        return

    count = 0

    for creator in data:
        if count >= max_models:
            break

        service = creator.get("service")
        cid = creator.get("id")
        name = creator.get("name")

        if not service or not cid or not name:
            continue

        yield {
            "name": name,
            "url": f"https://{host}/{service}/user/{cid}",
            "source": source
        }

        count += 1

    log.info(f"[{source}] Done: {count} creators")
# -------------------- Main orchestration --------------------
def main():
    log.info("=" * 60)
    log.info("MediaIndex scraper (audited) — using fetcher.js where available")
    log.info(f" eporner={ENABLE_EPORNER} kemono={ENABLE_KEMONO} coomer={ENABLE_COOMER}")
    log.info(f" max_models={MAX_MODELS}")
    log.info("=" * 60)

    albums = load_existing()
    new_count = 0

    if ENABLE_EPORNER:
        for rec in scrape_eporner_models(MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec
                new_count += 1

    if ENABLE_KEMONO:
        for rec in scrape_kemono_creators("https://api.kemono.party", "kemono", MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec
                new_count += 1

    if ENABLE_COOMER:
        for rec in scrape_kemono_creators("https://api.coomer.party", "coomer", MAX_MODELS):
            if rec["id"] not in albums:
                albums[rec["id"]] = rec
                new_count += 1

    meta = save(albums, new_count)
    total = meta["total"]
    ph = meta["placeholder_count"]
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
