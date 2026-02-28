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
LISTING_URL = f"{BASE_URL}/pornstar-list/1/"   # FIX: was /pornstars/
OUTPUT_FILE = "data/eporner.jl"

MAX_RETRIES = 3
TIMEOUT = 15

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

# FIX: Age gate bypass cookies. Eporner sets these after age confirmation.
# If the site ever rejects these, re-visit in a browser, copy fresh cookies
# from DevTools → Application → Cookies, and update here (or store as
# GitHub Actions secrets and load via os.environ).
AGE_GATE_COOKIES = {
    "age_verified": "1",
    "bs": "1",
}

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
    name = name.strip().lower()
    name = re.sub(r"\s+", " ", name)
    return name


def parse_count(text: str) -> int:
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
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Referer": BASE_URL + "/",
                "Accept-Language": "en-US,en;q=0.9",
            }
            response = requests.get(
                url,
                headers=headers,
                cookies=AGE_GATE_COOKIES,   # FIX: attach age gate cookies
                timeout=TIMEOUT,
            )

            if response.status_code == 200:
                # FIX: sanity-check we didn't land on the age gate
                if "age_verified" in response.url or "Want to watch FREE porn" in response.text[:500]:
                    logger.error(
                        "Age gate detected — cookies not working. "
                        "Update AGE_GATE_COOKIES with fresh values from your browser."
                    )
                    return None
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
    FIX: Updated for /pornstar-list/N/ URL pattern.
    Also falls back to checking a 'last page' or disabled-next-button pattern
    in case the highest numbered link isn't directly visible.
    """
    max_page = 1

    # Primary: scan all hrefs for /pornstar-list/N/
    for link in soup.find_all("a", href=True):
        href = link["href"]
        match = re.search(r"/pornstar-list/(\d+)/", href)
        if match:
            page_num = int(match.group(1))
            max_page = max(max_page, page_num)

    # Fallback: look for a text node that says something like "Page 1 of 47"
    page_of_match = re.search(r"Page\s+\d+\s+of\s+(\d+)", soup.get_text(), re.I)
    if page_of_match:
        max_page = max(max_page, int(page_of_match.group(1)))

    if max_page == 1:
        logger.warning(
            "Could not detect multiple pages — either the cookies aren't working "
            "or the pagination HTML structure has changed. Only page 1 will be scraped."
        )

    return max_page


def parse_listing_page(soup: BeautifulSoup) -> List[dict]:
    """
    Extracts model entries from a listing page.

    FIX: Broadened selectors. The original .pornstar-card / .ps-card / .model-item
    selectors were guesses. We now try a wider net and also attempt to match
    the lightweight structure Brave sees (name + vid count + photo count).

    If you open DevTools on the live page, look for the repeating container
    wrapping each model block and update MODEL_CARD_SELECTORS accordingly.
    """
    results = []

    MODEL_CARD_SELECTORS = [
        # Add the real selector here once confirmed via DevTools, e.g.:
        # ".pscard", ".pornstar-wrap", "div.item",
        ".pornstar-card",
        ".ps-card",
        ".model-item",
        # Generic fallback: any <li> or <div> containing a /pornstar/ link
    ]

    model_cards = []
    for selector in MODEL_CARD_SELECTORS:
        model_cards = soup.select(selector)
        if model_cards:
            logger.info(f"Matched cards using selector: {selector}")
            break

    # Last-resort fallback: find all links pointing to /pornstar/<slug>/
    if not model_cards:
        logger.warning("No cards matched known selectors — falling back to pornstar link scan")
        for link in soup.find_all("a", href=re.compile(r"^/pornstar/[^/]+/$")):
            # Treat the link's parent container as the card
            parent = link.parent
            if parent:
                model_cards.append(parent)

    for card in model_cards:
        try:
            name_tag = card.find("a", href=re.compile(r"/pornstar/"))
            if not name_tag:
                # Try any anchor if the URL pattern differs
                name_tag = card.find("a", href=True)
            if not name_tag:
                continue

            display_name = name_tag.get_text(strip=True)
            if not display_name:
                continue

            href = name_tag["href"]
            profile_url = href if href.startswith("http") else BASE_URL + href

            stats_text = card.get_text(" ", strip=True)

            video_match = re.search(r"([\d.,KM]+)\s*Videos?", stats_text, re.I)
            image_match = re.search(r"([\d.,KM]+)\s*Photos?", stats_text, re.I)

            videos = parse_count(video_match.group(1)) if video_match else 0
            images = parse_count(image_match.group(1)) if image_match else 0
            total = videos + images

            entry = {
                "normalized_name": normalize_name(display_name),
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

    with open(OUTPUT_FILE, "w", encoding="utf-8") as outfile:

        for page in range(1, total_pages + 1):
            # FIX: correct URL pattern for all pages
            page_url = f"{BASE_URL}/pornstar-list/{page}/"

            logger.info(f"Fetching page {page}/{total_pages}")

            resp = fetch_with_retries(page_url)
            if not resp:
                logger.warning(f"Skipping page {page}")
                continue

            page_soup = BeautifulSoup(resp.text, "lxml")
            entries = parse_listing_page(page_soup)

            logger.info(f"  → {len(entries)} models found on page {page}")

            for entry in entries:
                outfile.write(json.dumps(entry, ensure_ascii=False) + "\n")

            polite_delay()

    logger.info("Eporner scraper complete")


if __name__ == "__main__":
    run()
