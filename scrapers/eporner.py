import json
import os
import re
import time
import random
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime, timezone

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

BASE_URL = "https://www.eporner.com/pornstar-list/"
OUTPUT_FILE = "data/eporner.jl"

AGE_GATE_COOKIES = {"age_verified": "1", "bs": "1"}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

MAX_RETRIES = 4
BASE_DELAY = 2.0
RETRY_BACKOFF = 3.0


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

def make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )


def fetch(url, scraper, retries=MAX_RETRIES):
    """
    Fetch a URL with retries and exponential backoff.
    Returns BeautifulSoup or None on total failure.
    """
    for attempt in range(1, retries + 1):
        try:
            scraper.headers.update({"User-Agent": random.choice(USER_AGENTS)})
            r = scraper.get(url, cookies=AGE_GATE_COOKIES, timeout=30)

            if r.status_code == 200:
                if "Want to watch FREE porn" in r.text[:500]:
                    print(f"[WARN] Age gate hit on attempt {attempt} for {url}")
                else:
                    return BeautifulSoup(r.text, "html.parser")

            elif r.status_code == 429:
                wait = RETRY_BACKOFF * (2 ** attempt)
                print(f"[WARN] Rate limited (429) for {url}, waiting {wait:.0f}s...")
                time.sleep(wait)
                continue

            else:
                print(f"[WARN] Status {r.status_code} on attempt {attempt} for {url}")

        except Exception as e:
            print(f"[WARN] Request error on attempt {attempt} for {url}: {e}")

        wait = RETRY_BACKOFF * attempt
        print(f"[INFO] Retrying in {wait:.0f}s...")
        time.sleep(wait)

        # Fresh scraper session on second-to-last retry
        if attempt == retries - 1:
            print("[INFO] Reinitialising scraper session...")
            scraper = make_scraper()

    print(f"[ERROR] All {retries} attempts failed for {url}")
    return None


# -----------------------------------------------------------------------------
# Pagination
# -----------------------------------------------------------------------------

def get_total_pages(scraper):
    print("[INFO] Detecting total pages...")

    for attempt in range(1, 4):
        soup = fetch(BASE_URL, scraper)
        if soup is None:
            print(f"[WARN] Could not fetch listing page (attempt {attempt})")
            time.sleep(RETRY_BACKOFF * attempt)
            continue

        pages = []
        for link in soup.find_all("a", href=True):
            match = re.search(r"/pornstar-list/(\d+)/", link["href"])
            if match:
                pages.append(int(match.group(1)))

        if pages:
            total = max(pages)
            print(f"[INFO] Detected {total} pages")
            return total, soup  # reuse soup for page 1

        print(f"[WARN] No pagination links found (attempt {attempt}), retrying...")
        time.sleep(RETRY_BACKOFF * attempt)

    print("[WARN] Could not detect pagination. Defaulting to 1 page.")
    return 1, soup


# -----------------------------------------------------------------------------
# Parsing
# -----------------------------------------------------------------------------

def parse_counts(text):
    vid_match = re.search(r"([\d,]+)\s*Videos?", text, re.I)
    img_match = re.search(r"([\d,]+)\s*Photos?", text, re.I)
    videos = int(vid_match.group(1).replace(",", "")) if vid_match else 0
    images = int(img_match.group(1).replace(",", "")) if img_match else 0
    return videos, images


def extract_stats(link, soup, display_name):
    """
    Try multiple strategies to find video/photo counts.
    Returns (videos, images).
    """
    # Strategy 1: Walk up DOM
    container = link.parent
    for _ in range(6):
        if container is None:
            break
        text = container.get_text(" ", strip=True)
        if re.search(r"\d+\s*Videos?", text, re.I):
            return parse_counts(text)
        container = container.parent

    # Strategy 2: Siblings of link's parent
    if link.parent:
        for sibling in link.parent.next_siblings:
            try:
                sib_text = sibling.get_text(" ", strip=True)
                if re.search(r"\d+\s*Videos?", sib_text, re.I):
                    return parse_counts(sib_text)
            except AttributeError:
                continue

    # Strategy 3: Full page text context around the name
    full_text = soup.get_text(" ")
    escaped = re.escape(display_name)
    context_match = re.search(escaped + r".{0,300}", full_text, re.I | re.DOTALL)
    if context_match:
        snippet = context_match.group(0)
        if re.search(r"\d+\s*Videos?", snippet, re.I):
            return parse_counts(snippet)

    return 0, 0


def parse_page(soup, page_number):
    """
    Extract all model profiles from a listing page.
    Returns list of dicts conforming to the shared entry schema.
    """
    models = []
    seen_urls = set()
    links = soup.find_all("a", href=re.compile(r"/pornstar/[^/]+/$"))

    # DEBUG: log raw container text for first model on page 1
    if page_number == 1 and links:
        first = links[0]
        c = first.parent
        for _ in range(6):
            if c is None:
                break
            t = c.get_text(" ", strip=True)
            if re.search(r"\d+\s*Videos?", t, re.I):
                print(f"[DEBUG] First model container text: {t[:400]}")
                break
            c = c.parent
        else:
            print("[DEBUG] No stats container found within 6 DOM levels for first model")

    for link in links:
        try:
            display_name = link.get_text(strip=True)
            if not display_name:
                continue

            profile_url = "https://www.eporner.com" + link["href"]
            if profile_url in seen_urls:
                continue
            seen_urls.add(profile_url)

            videos, images = extract_stats(link, soup, display_name)

            models.append({
                "normalized_name": display_name.strip().lower(),
                "display_name": display_name,
                "source": "eporner",
                "entry_type": "model_profile",
                "media": {
                    "videos": videos,
                    "images": images,
                    "total": videos + images,
                },
                "url": profile_url,
                "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            })

        except Exception as e:
            print(f"[WARN] Failed parsing entry: {e}")
            continue

    return models


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def run():
    print("[INFO] Starting Eporner scraper (cloudscraper)")
    os.makedirs("data", exist_ok=True)

    scraper = make_scraper()
    total_pages, page1_soup = get_total_pages(scraper)
    all_models = []

    for page in range(1, total_pages + 1):
        if page == 1 and page1_soup is not None:
            soup = page1_soup
            print(f"[INFO] Using cached page 1 soup")
        else:
            url = f"{BASE_URL}{page}/"
            print(f"[INFO] Fetching page {page}/{total_pages}: {url}")
            soup = fetch(url, scraper)
            if soup is None:
                print(f"[WARN] Skipping page {page} after all retries failed")
                continue

        models = parse_page(soup, page)
        print(f"[INFO] → {len(models)} models on page {page}/{total_pages}")
        all_models.extend(models)

        if page < total_pages:
            delay = BASE_DELAY + random.uniform(0.5, 1.5)
            time.sleep(delay)

    print(f"[INFO] Total models scraped: {len(all_models)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for model in all_models:
            f.write(json.dumps(model, ensure_ascii=False) + "\n")

    print("[INFO] Eporner scraper complete")


if __name__ == "__main__":
    run()
