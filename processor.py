import os
import json
import logging
from glob import glob
from datetime import datetime, timezone

DATA_DIR = "data"
OUTPUT_DIR = "site"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "index.json")

logger = logging.getLogger("processor")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(handler)


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------

REQUIRED_TOP_LEVEL_FIELDS = {
    "normalized_name",
    "display_name",
    "source",
    "entry_type",
    "media",
    "url",
    "last_updated",
}

REQUIRED_MEDIA_FIELDS = {
    "videos",
    "images",
    "total",
}


def is_valid_entry(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False

    if not REQUIRED_TOP_LEVEL_FIELDS.issubset(entry.keys()):
        return False

    if not isinstance(entry["media"], dict):
        return False

    if not REQUIRED_MEDIA_FIELDS.issubset(entry["media"].keys()):
        return False

    return True


# -----------------------------------------------------------------------------
# Deduplication Key
# -----------------------------------------------------------------------------

def make_key(entry: dict):
    return (
        entry["normalized_name"],
        entry["source"],
        entry["url"],
    )


def parse_date(date_str: str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return datetime.min


# -----------------------------------------------------------------------------
# Processor
# -----------------------------------------------------------------------------

def run():
    logger.info("Starting processor")

    if not os.path.exists(DATA_DIR):
        logger.warning("No data directory found")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    files = glob(os.path.join(DATA_DIR, "*.jl"))

    if not files:
        logger.warning("No .jl files found")
        return

    logger.info(f"Found {len(files)} source files")

    deduped = {}

    total_lines = 0
    valid_entries = 0

    for filepath in files:
        logger.info(f"Processing {filepath}")

        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                total_lines += 1

                try:
                    entry = json.loads(line.strip())
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON line skipped")
                    continue

                if not is_valid_entry(entry):
                    logger.warning("Invalid schema entry skipped")
                    continue

                key = make_key(entry)

                if key not in deduped:
                    deduped[key] = entry
                else:
                    # Keep most recent last_updated
                    existing = deduped[key]
                    if parse_date(entry["last_updated"]) > parse_date(existing["last_updated"]):
                        deduped[key] = entry

                valid_entries += 1

    flattened = list(deduped.values())

    output_payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_entries": len(flattened),
        "entries": flattened,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        json.dump(output_payload, out, ensure_ascii=False)

    logger.info(f"Processor complete")
    logger.info(f"Total lines read: {total_lines}")
    logger.info(f"Valid entries: {valid_entries}")
    logger.info(f"Unique entries written: {len(flattened)}")


if __name__ == "__main__":
    run()
