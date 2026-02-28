import os
import re
import time
import random
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List

import requests
from bs4 import BeautifulSoup

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

BASE_URL = "https://www.eporner.com"
LISTING_URL = f"{BASE_URL}/pornstars/"
OUTPUT_FILE = "data/eporner.jl"

MAX_RETRIES = 3
TIMEOUT = 15

USER_AGENTS = [
    # Static rotation pool
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logger = logging.getLogger("eporner_scraper")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(handler)


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """
    Lowercase, trim, collapse multiple spaces.
    Hyphens are preserved exactly as requested.
    """
    name = name.strip().lower()
    name = re.sub(r"\s+", " ", name)
    return name


def parse_count(text: str) -> int:
    """
    Parses numeric counts.
    Handles:
        123
        12,345
        1.2K
        3.4M
    """
    text = text.strip().replace(",", "")
    multiplier = 1

    if text.endswith("K"):
        multiplier = 1_000
        text = text[:-1]
    elif text.endswith("M"):
        multiplier = 1_000_000
        text = text[:-1]

    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def polite_delay():
    time.sleep(random.uniform(1.5, 2.5))


def fetch_with_retries(url: str) -> Optional[requests.Response]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = {"User-Agent": random.choice(USER_AGENTS)}
            response = requests.get(url, headers=headers, timeout=TIMEOUT)

            if response.status_code == 200:
                return response

            logger.warning(f"Non-200 status {response.status_code} for {url}")

        except requests.RequestException as e:
            logger.warning(f"Request error (attempt {attempt}) for {url}: {e}")

        backoff = 2 ** attempt
        time.sleep(backoff)

    logger.error(f"Failed after retries: {url}")
    return None


# -----------------------------------------------------------------------------
# Parsing Logic
# -----------------------------------------------------------------------------

def extract_total_pages(soup: BeautifulSoup) -> int:
    """
    Extracts pagination max page.
    Assumes traditional pagination links like /pornstars/2/
    """
    pagination = soup.find_all("a", href=True)
    max_page = 1

    for link in pagination:
        href = link["href"]
        match = re.search(r"/pornstars/(\d+)/", href)
        if match:
            page_num = int(match.group(1))
            max_page = max(max_page, page_num)

    return max_page


def parse_listing_page(soup: BeautifulSoup) -> List[dict]:
    """
    Extracts model entries from a listing page.
    Assumes counts visible on listing.
    """
    results = []

    # Adjust selector if needed after testing live HTML
    model_cards = soup.select(".pornstar-card, .ps-card, .model-item")

    for card in model_cards:
        try:
            name_tag = card.find("a", href=True)
            if not name_tag:
                continue

            display_name = name_tag.get_text(strip=True)
            profile_url = BASE_URL + name_tag["href"]

            stats_text = card.get_text(" ", strip=True)

            # Attempt to extract counts heuristically
            video_match = re.search(r"([\d.,KM]+)\s+Videos?", stats_text, re.I)
            image_match = re.search(r"([\d.,KM]+)\s+Photos?", stats_text, re.I)

            videos = parse_count(video_match.group(1)) if video_match else 0
            images = parse_count(image_match.group(1)) if image_match else 0
            total = videos + images

            normalized = normalize_name(display_name)

            entry = {
                "normalized_name": normalized,
                "display_name": display_name,
                "source": "eporner",
                "entry_type": "profile",
                "media": {
                    "videos": videos,
                    "images": images,
                    "total": total,
                },
                "url": profile_url,
                "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }

            results.append(entry)

        except Exception as e:
            logger.warning(f"Failed parsing card: {e}")
            continue

    return results


# -----------------------------------------------------------------------------
# Main Scraper
# -----------------------------------------------------------------------------

def run():
    logger.info("Starting Eporner scraper")

    os.makedirs("data", exist_ok=True)

    response = fetch_with_retries(LISTING_URL)
    if not response:
        logger.error("Failed to fetch initial listing page.")
        return

    soup = BeautifulSoup(response.text, "lxml")

    total_pages = extract_total_pages(soup)
    logger.info(f"Detected {total_pages} pages")

    # Overwrite file each run
    with open(OUTPUT_FILE, "w", encoding="utf-8") as outfile:

        for page in range(1, total_pages + 1):
            if page == 1:
                page_url = LISTING_URL
            else:
                page_url = f"{BASE_URL}/pornstars/{page}/"

            logger.info(f"Fetching page {page}/{total_pages}")

            resp = fetch_with_retries(page_url)
            if not resp:
                logger.warning(f"Skipping page {page}")
                continue

            soup = BeautifulSoup(resp.text, "lxml")
            entries = parse_listing_page(soup)

            for entry in entries:
                outfile.write(json.dumps(entry, ensure_ascii=False) + "\n")

            polite_delay()

    logger.info("Eporner scraper complete")


if __name__ == "__main__":
    run()
