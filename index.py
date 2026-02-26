"""
index.py — Load, save, validate and deduplicate albums.json.

Schema for each album record:
{
  "id":            str,   # "{source}:{site_id}" — canonical dedup key
  "title":         str,
  "source":        str,   # "fapello" | "kemono" | "eporner" | "cyberdrop" | "erome" | "bunkr"
  "url":           str,
  "thumbnail":     str | null,
  "file_count":    int,
  "photo_count":   int,
  "video_count":   int,
  "has_videos":    bool,
  "date":          str | null,   # ISO8601
  "indexed_at":    str,          # ISO8601
  "needs_recheck": bool,         # true if title is placeholder or counts suspicious
  "extra":         dict,         # source-specific fields (duration, views, service, etc.)
}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

OUT_FILE    = Path("albums.json")
RECHECK_FILE = Path("recheck.json")
VALIDATION_FILE = Path("validation.json")

# Titles that indicate a blocked / challenge page was parsed by mistake
PLACEHOLDER_TITLES = {
    "", "welcome", "welcome!", "access denied", "just a moment",
    "403", "forbidden", "503", "error", "attention required",
    "checking your browser", "ray id", "untitled",
}


def is_placeholder(record: dict) -> bool:
    t = (record.get("title") or "").strip().lower()
    return t in PLACEHOLDER_TITLES or len(t) < 2


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def save(albums_by_id: dict[str, dict], new_count: int):
    albums = sorted(
        albums_by_id.values(),
        key=lambda a: a.get("date") or a.get("indexed_at") or "",
        reverse=True,
    )
    placeholder_count = sum(1 for a in albums if is_placeholder(a))
    recheck_count = sum(1 for a in albums if a.get("needs_recheck"))

    payload = {
        "meta": {
            "total":             len(albums),
            "last_updated":      now_iso(),
            "new_this_run":      new_count,
            "placeholder_count": placeholder_count,
            "recheck_count":     recheck_count,
            "sources":           sorted({a.get("source", "?") for a in albums}),
        },
        "albums": albums,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    log.info(
        f"✓ Saved {len(albums)} albums "
        f"({new_count} new, {placeholder_count} placeholders, {recheck_count} recheck)"
    )

    # Save recheck queue separately
    recheck = [a for a in albums if a.get("needs_recheck")]
    RECHECK_FILE.write_text(json.dumps(recheck, ensure_ascii=False, indent=2))

    return payload["meta"]


def write_validation(meta: dict, extra: dict | None = None):
    v = {**meta, **(extra or {})}
    VALIDATION_FILE.write_text(json.dumps(v, ensure_ascii=False, indent=2))
    log.info(f"✓ Wrote validation.json")


def commit_guard(meta: dict, force: bool = False) -> bool:
    """
    Returns True if it's safe to commit albums.json.
    Blocks commit if total==0 or placeholder ratio > 5%.
    force=True overrides (for manual workflow_dispatch).
    """
    if force:
        return True
    total = meta.get("total", 0)
    if total == 0:
        log.error("COMMIT GUARD: total==0, refusing commit")
        return False
    ph = meta.get("placeholder_count", 0)
    ratio = ph / total
    if ratio > 0.05:
        log.error(f"COMMIT GUARD: placeholder ratio {ratio:.1%} > 5%, refusing commit")
        return False
    return True


def merge_record(existing: dict, new: dict) -> dict:
    """
    Merge a new record into an existing one, keeping the best data.
    Never overwrite a good title with a placeholder.
    """
    merged = dict(existing)
    for key, val in new.items():
        if key == "title":
            # Only update title if new title is better (not a placeholder)
            if val and not is_placeholder({"title": val}):
                if is_placeholder(existing):
                    merged["title"] = val
                # else keep existing good title
        elif key == "file_count":
            merged[key] = max(merged.get(key, 0), val or 0)
        elif key == "photo_count":
            merged[key] = max(merged.get(key, 0), val or 0)
        elif key == "video_count":
            merged[key] = max(merged.get(key, 0), val or 0)
        elif key == "has_videos":
            merged[key] = merged.get(key, False) or bool(val)
        elif key in ("thumbnail", "date", "url"):
            if val and not merged.get(key):
                merged[key] = val
        elif key == "needs_recheck":
            # Clear needs_recheck only if we now have good data
            if not val:
                merged[key] = False
        elif key == "extra" and isinstance(val, dict):
            merged["extra"] = {**(merged.get("extra") or {}), **val}
        else:
            if val is not None:
                merged[key] = val
    return merged
