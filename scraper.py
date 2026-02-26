#!/usr/bin/env python3
"""
scraper.py — MediaIndex multi-source scraper (v6)

Sources (ordered by reliability):
 1. Eporner — official API, plain requests, always works, no Cloudflare
 2. Kemono — official API, plain requests, always works
 3. Fapello — cloudscraper (CF JS bypass), fallback playwright
 4. Erome — plain requests + cloudscraper fallback
 5. Bunkr — playwright (CF Bot Management)
 6. Cyberdrop/Cyberfile — plain requests + cloudscraper + mirror rotation

Env vars:
  MAX_ALBUMS               how many new records to target per run (default 500)
  ENABLE_BUNKR             true/false (default true)
  ENABLE_FAPELLO           true/false (default true)
  ENABLE_KEMONO            true/false (default true)
  ENABLE_EPORNER           true/false (default true)
  ENABLE_EROME             true/false (default true)
  ENABLE_CYBERDROP         true/false (default false, no public directory)
  DELAY_MIN/MAX            random sleep bounds between requests
  DEBUG_NO_CACHE           true = skip cache, always refetch
  FORCE_COMMIT             true = bypass commit guard (for manual runs)
"""

import json
import logging
import os
import sys
from pathlib import Path

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_NEW = int(os.getenv("MAX_ALBUMS", "500"))
ENABLE_BUNKR = os.getenv("ENABLE_BUNKR", "true").lower() != "false"
ENABLE_FAPELLO = os.getenv("ENABLE_FAPELLO", "true").lower() != "false"
ENABLE_KEMONO = os.getenv("ENABLE_KEMONO", "true").lower() != "false"
ENABLE_EPORNER = os.getenv("ENABLE_EPORNER", "true").lower() != "false"
ENABLE_EROME = os.getenv("ENABLE_EROME", "true").lower() != "false"
ENABLE_CYBERDROP = os.getenv("ENABLE_CYBERDROP", "false").lower() != "false"
FORCE_COMMIT = os.getenv("FORCE_COMMIT", "false").lower() == "true"

# ── Ensure scrapers/ is on path (compatibility; safe if not present) ────────────
# Keep these so "scrapers" package can be used if created later.
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "scrapers"))

import index as idx
from index import load_existing, save, write_validation, commit_guard, merge_record


# Helper to import a module either from scrapers.* or top-level
def import_source_module(mod_name: str):
    """
    Try to import scrapers.<mod_name>, fall back to top-level <mod_name>.
    Returns imported module object or raises ImportError.
    """
    try:
        return __import__(f"scrapers.{mod_name}", fromlist=[mod_name])
    except Exception:
        # fallback to top-level module
        return __import__(mod_name)


# ── Source runner helper ───────────────────────────────────────────────────────
def run_source(
    name: str,
    enabled: bool,
    scrape_fn,
    albums_by_id: dict,
    new_count: int,
) -> int:
    if not enabled:
        log.info(f"[{name}] Disabled — skipping")
        return new_count

    log.info("=" * 65)
    log.info(f"SOURCE: {name.upper()}")
    log.info("=" * 65)

    try:
        records = scrape_fn()
    except Exception as e:
        log.error(f"[{name}] Fatal error: {e}", exc_info=True)
        return new_count

    added = 0
    for record in records:
        rid = record.get("id")
        if not rid:
            continue
        if rid not in albums_by_id:
            albums_by_id[rid] = record
            new_count += 1
            added += 1
        else:
            albums_by_id[rid] = merge_record(albums_by_id[rid], record)

    log.info(f"[{name}] Added {added} new records ({new_count} total new this run)")
    return new_count


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("MediaIndex Scraper v6 — Multi-source")
    log.info(f"Target: {MAX_NEW} new records per run")
    log.info("=" * 65)

    albums_by_id = load_existing()
    new_count = 0

    # ── 1. Eporner (API, always reliable) ─────────────────────────────────────
    if ENABLE_EPORNER:
        try:
            ep_mod = import_source_module("eporner")
        except ImportError:
            log.error("[eporner] Module not found — skipping")
        else:
            new_count = run_source(
                "eporner",
                True,
                lambda: ep_mod.scrape(MAX_NEW // 4),
                albums_by_id,
                new_count,
            )

    # ── 2. Kemono (API, always reliable) ────────────────────────────────────
    if ENABLE_KEMONO:
        try:
            km_mod = import_source_module("kemono")
        except ImportError:
            log.error("[kemono] Module not found — skipping")
        else:
            new_count = run_source(
                "kemono",
                True,
                lambda: km_mod.scrape(MAX_NEW // 4),
                albums_by_id,
                new_count,
            )

    # ── 3. Fapello (cloudscraper) ──────────────────────────────────────────
    if ENABLE_FAPELLO:
        try:
            fp_mod = import_source_module("fapello")
        except ImportError:
            log.error("[fapello] Module not found — skipping")
        else:
            pages = min(30, max(5, MAX_NEW // 8))
            new_count = run_source(
                "fapello",
                True,
                lambda: fp_mod.scrape(max_pages=pages),
                albums_by_id,
                new_count,
            )

    # ── 4. Erome ───────────────────────────────────────────────────────────
    if ENABLE_EROME:
        try:
            er_mod = import_source_module("erome")
        except ImportError:
            log.error("[erome] Module not found — skipping")
        else:
            new_count = run_source(
                "erome",
                True,
                lambda: er_mod.scrape(MAX_NEW // 5),
                albums_by_id,
                new_count,
            )

    # ── 5. Bunkr (playwright, may fail in CI) ───────────────────────────────
    if ENABLE_BUNKR:
        try:
            # import only when enabled — avoids playwright import errors when disabled
            bk_mod = None
            try:
                bk_mod = import_source_module("bunkr")
            except Exception:
                # best-effort fallback; keep error logging separate
                log.warning("[bunkr] Module import failed; attempting top-level import fallback")
                bk_mod = import_source_module("bunkr")
            if bk_mod is None:
                raise ImportError("bunkr module unavailable")
            new_count = run_source(
                "bunkr",
                True,
                lambda: bk_mod.scrape(),
                albums_by_id,
                new_count,
            )
        except ImportError:
            log.warning("[bunkr] Module not found — skipping")
        except Exception as e:
            log.error(f"[bunkr] Failed: {e}", exc_info=True)

    # ── Cyberdrop (no public directory — skipped unless IDs provided) ──────────
    if ENABLE_CYBERDROP:
        log.info("[cyberdrop] Cyberdrop requires known album IDs — add discovery source")

    # ── Save & validate ────────────────────────────────────────────────────────
    meta = save(albums_by_id, new_count)

    # Per-source counts for validation
    extra = {}
    for src in ["fapello", "kemono", "eporner", "erome", "bunkr", "cyberdrop"]:
        extra[f"{src}_count"] = sum(1 for a in albums_by_id.values() if a.get("source") == src)

    write_validation(meta, extra)

    log.info("")
    log.info("=" * 65)
    log.info(f"DONE: {meta['total']} total, {new_count} new this run")
    log.info(f" {meta['placeholder_count']} placeholders, {meta['recheck_count']} recheck")
    for src, cnt in extra.items():
        if cnt:
            log.info(f" {src}: {cnt}")
    log.info("=" * 65)

    # Commit guard: exit non-zero if unsafe to commit
    if not commit_guard(meta, force=FORCE_COMMIT):
        log.error("Commit guard triggered — not safe to push albums.json")
        sys.exit(1)


if __name__ == "__main__":
    main()
