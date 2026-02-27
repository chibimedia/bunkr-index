#!/usr/bin/env python3
"""
MediaIndex scraper

Sources:
  1. Eporner — pornstar profiles from /pornstar-list/ using cloudscraper
               (page has JS age gate; plain requests gets blocked, cloudscraper bypasses it)
               One record per performer: name + video count + photo count + profile URL
  2. Kemono.cr — per-service API (403 on CI, kept for when proxy/self-hosted added)
  3. Coomer.st — same

Kemono/Coomer status: hard 403 on all GitHub Actions IPs even with cloudscraper.
Fix requires self-hosted runner or proxy — kept in code, just won't produce records yet.
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
DEBUG_DIR    = HERE / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

MAX_MODELS    = int(os.getenv("MAX_MODELS",   "5000"))
DELAY         = float(os.getenv("DELAY",      "1.5"))
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
        (DEBUG_DIR / name).write_bytes(content[:50000])  # cap at 50KB
    except Exception:
        pass

# ── HTTP ───────────────────────────────────────────────────────────────────────
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_sess = requests.Session()
_sess.headers.update(_HEADERS)

try:
    import cloudscraper as _cs_mod
    _cs = _cs_mod.create_scraper(browser={"browser":"chrome","platform":"windows","mobile":False})
    _cs.headers.update(_HEADERS)
    HAS_CLOUDSCRAPER = True
    log.info("cloudscraper available ✓")
except ImportError:
    HAS_CLOUDSCRAPER = False
    log.warning("cloudscraper not installed — eporner pornstar list will fail without it")

def fetch_html(url: str, use_cs: bool = False) -> Optional[str]:
    """Fetch HTML. use_cs=True forces cloudscraper (for age-gated/CF pages)."""
    client = (_cs if use_cs and HAS_CLOUDSCRAPER else _sess)
    for attempt in range(1, 4):
        time.sleep(DELAY + random.uniform(0, 0.5))
        try:
            r = client.get(url, timeout=30)
            if r.status_code == 200:
                text = r.text
                # Detect age gate / CF block
                low = text.lower()
                if ("want to watch free porn" in low or
                    "age verification" in low or
                    "checking your browser" in low or
                    len(text) < 2000):
                    log.warning(f"Age gate / CF block on {url} (len={len(text)})")
                    save_debug(f"blocked_{url.replace('/','_')[-60:]}.html", r.content)
                    # If we weren't using cloudscraper, retry with it
                    if not use_cs and HAS_CLOUDSCRAPER:
                        log.info("Retrying with cloudscraper...")
                        return fetch_html(url, use_cs=True)
                    return None
                return text
            elif r.status_code == 429:
                log.warning("429 rate-limit, sleeping 60s"); time.sleep(60)
            elif r.status_code == 403:
                log.warning(f"403 on {url}")
                save_debug(f"403_{url.replace('/','_')[-60:]}.html", r.content)
                if not use_cs and HAS_CLOUDSCRAPER:
                    return fetch_html(url, use_cs=True)
                return None
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"fetch_html attempt {attempt}: {e}")
        time.sleep(2 ** attempt)
    return None

def fetch_json(url: str, referer: str = "") -> Optional[Any]:
    """Fetch JSON. Plain requests first, cloudscraper fallback on 403."""
    hdrs = {"Accept": "application/json",
            "Referer": referer, "Origin": referer.rstrip("/")} if referer else {"Accept": "application/json"}
    for attempt in range(1, 4):
        time.sleep(DELAY + random.uniform(0, 0.3))
        try:
            r = _sess.get(url, headers=hdrs, timeout=30)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                log.warning("429, sleeping 60s"); time.sleep(60)
            elif r.status_code == 403:
                log.warning(f"403 on {url} — trying cloudscraper")
                save_debug(f"403_{url.replace('/','_')[-60:]}.html", r.content)
                break
            else:
                log.warning(f"HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"fetch_json attempt {attempt}: {e}")
        time.sleep(2 ** attempt)

    if HAS_CLOUDSCRAPER:
        try:
            time.sleep(DELAY)
            r2 = _cs.get(url, headers=hdrs, timeout=30)
            if r2.status_code == 200:
                return r2.json()
            log.warning(f"cloudscraper got {r2.status_code} for {url}")
            save_debug(f"cs_{r2.status_code}_{url.replace('/','_')[-60:]}.html", r2.content)
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
# Eporner — pornstar profile pages
# Uses cloudscraper to bypass the JS age gate on /pornstar-list/
#
# URL pattern: /pornstar-list/?sort=most-popular&page=N
# Card structure (from nav menu confirms): /pornstar/{name}-{5charID}/
# Each card shows: performer name, video count, photo count
# ══════════════════════════════════════════════════════════════════════════════
def scrape_eporner(max_models: int) -> List[dict]:
    if not HAS_CLOUDSCRAPER:
        log.error("[eporner] cloudscraper required for pornstar list — skipping")
        return []

    records, seen, page = [], set(), 1
    log.info(f"[eporner] Scraping pornstar list via cloudscraper (target: {max_models})")

    while len(records) < max_models:
        url  = f"https://www.eporner.com/pornstar-list/?sort=most-popular&page={page}"
        html = fetch_html(url, use_cs=True)

        if not html:
            log.warning(f"[eporner] No HTML on page {page}, stopping")
            break

        soup = BeautifulSoup(html, "lxml")

        # Save first page for debugging so we can see the actual structure
        if page == 1:
            save_debug("eporner_page1.html", html.encode())

        # Pornstar card links: href="/pornstar/{slug}-{5charID}/"
        cards = soup.find_all("a", href=re.compile(r"^/pornstar/[^/]+-\w{5}/?$"))

        if not cards:
            # Try broader search in case selector is off
            cards = soup.select("a[href*='/pornstar/']")
            cards = [c for c in cards if re.search(r"/pornstar/[^/]+-\w{5}/?$", c.get("href",""))]

        if not cards:
            log.warning(f"[eporner] No pornstar cards on page {page} — check debug/eporner_page1.html")
            break

        new_here = 0
        for card in cards:
            href = card.get("href", "")
            m = re.search(r"/pornstar/(.+?)-(\w{5})/?$", href)
            if not m:
                continue

            slug, ps_id = m.group(1), m.group(2)
            rid = f"eporner:ps:{ps_id}"
            if rid in seen:
                continue
            seen.add(rid)

            # Name: prefer explicit name tag, fall back to slug
            name_tag = (card.find(class_=re.compile(r"name", re.I)) or
                        card.find(["h3", "h2", "strong"]) or
                        card.find("p"))
            name = name_tag.get_text(strip=True) if name_tag else slug.replace("-", " ").title()

            if is_placeholder(name):
                continue

            # File counts: look for "NNN Videos" and "NNN Photos" in card text
            card_text = card.get_text(" ", strip=True)
            vid_match = re.search(r"([\d,]+)\s*Videos?", card_text, re.I)
            pic_match = re.search(r"([\d,]+)\s*Photos?", card_text, re.I)
            video_count = int(vid_match.group(1).replace(",","")) if vid_match else 0
            photo_count = int(pic_match.group(1).replace(",","")) if pic_match else 0
            total_files = video_count + photo_count

            records.append({
                "id":          rid,
                "title":       name,
                "source":      "eporner",
                "url":         f"https://www.eporner.com/pornstar/{slug}-{ps_id}/",
                "file_count":  total_files,
                "video_count": video_count,
                "photo_count": photo_count,
                "has_videos":  video_count > 0,
                "date":        None,
                "indexed_at":  now_iso(),
            })
            new_here += 1
            if len(records) >= max_models:
                break

        log.info(f"[eporner] page {page}: {new_here} new performers ({len(records)} total)")
        if new_here == 0:
            break
        page += 1

    log.info(f"[eporner] Done: {len(records)} performers")
    return records

# ══════════════════════════════════════════════════════════════════════════════
# Kemono / Coomer — per-service API + cloudscraper fallback
# Currently hard-403'd on GitHub Actions IPs.
# Keeping the code — will work when self-hosted runner or proxy is added.
# ══════════════════════════════════════════════════════════════════════════════
KEMONO_SERVICES = ["patreon","fanbox","gumroad","subscribestar","dlsite","fantia","boosty","afdian"]
COOMER_SERVICES = ["onlyfans","fansly","candfans"]

def scrape_creators_by_service(base_url: str, source: str,
                                services: List[str], max_total: int) -> List[dict]:
    all_records: Dict[str, dict] = {}
    log.info(f"[{source}] Fetching creators via per-service API")

    for svc in services:
        if len(all_records) >= max_total:
            break
        offset, svc_count = 0, 0
        log.info(f"[{source}] service={svc}")

        while len(all_records) < max_total:
            url  = f"{base_url}/api/v1/{svc}/creators?o={offset}"
            data = fetch_json(url, referer=f"{base_url}/")

            if data is None:
                log.warning(f"[{source}] {svc} blocked — skipping service")
                break
            if not isinstance(data, list) or not data:
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
