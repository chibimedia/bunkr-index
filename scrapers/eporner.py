import json
import os
import re
import time
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime, timezone

BASE_URL = "https://www.eporner.com/pornstar-list/"
OUTPUT_FILE = "data/eporner.jl"
AGE_GATE_COOKIES = {"age_verified": "1", "bs": "1"}

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)

def get_total_pages():
    print("[INFO] Detecting total pages...")
    r = scraper.get(BASE_URL, cookies=AGE_GATE_COOKIES, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    pages = []
    for link in soup.find_all("a", href=True):
        match = re.search(r"/pornstar-list/(\d+)/", link["href"])
        if match:
            pages.append(int(match.group(1)))
    if not pages:
        print("[WARN] Could not detect pagination. Defaulting to 1 page.")
        return 1
    total = max(pages)
    print(f"[INFO] Detected {total} pages")
    return total

def scrape_page(page_number):
    url = BASE_URL if page_number == 1 else f"{BASE_URL}{page_number}/"
    print(f"[INFO] Fetching {url}")
    r = scraper.get(url, cookies=AGE_GATE_COOKIES, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    models = []

    for link in soup.find_all("a", href=re.compile(r"/pornstar/[^/]+/$")):
        try:
            display_name = link.get_text(strip=True)
            if not display_name:
                continue

            profile_url = "https://www.eporner.com" + link["href"]

            # Walk up DOM to find container with stats
            container = link.parent
            stats = ""
            for _ in range(6):
                if container is None:
                    break
                text = container.get_text(" ", strip=True)
                if re.search(r"\d+\s*Videos?", text, re.I):
                    stats = text
                    # DEBUG: print first model's container on page 1
                    if page_number == 1 and len(models) == 0:
                        print(f"[DEBUG] Stats container for '{display_name}': {text[:300]}")
                    break
                container = container.parent

            vid_match = re.search(r"([\d,]+)\s*Videos?", stats, re.I)
            img_match = re.search(r"([\d,]+)\s*Photos?", stats, re.I)
            videos = int(vid_match.group(1).replace(",", "")) if vid_match else 0
            images = int(img_match.group(1).replace(",", "")) if img_match else 0

            models.append({
                "normalized_name": display_name.strip().lower(),
                "display_name": display_name,
                "source": "eporner",
                "entry_type": "profile",
                "media": {
                    "videos": videos,
                    "images": images,
                    "total": videos + images,
                },
                "url": profile_url,
                "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            })
        except Exception:
            continue

    print(f"[INFO] → {len(models)} models found on page 1" if page_number == 1 else f"[INFO] → {len(models)} models found on page {page_number}")
    return models

def run():
    print("[INFO] Starting Eporner scraper (cloudscraper)")
    os.makedirs("data", exist_ok=True)
    total_pages = get_total_pages()
    all_models = []
    for page in range(1, total_pages + 1):
        all_models.extend(scrape_page(page))
        time.sleep(1)
    print(f"[INFO] Total models scraped: {len(all_models)}")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for model in all_models:
            f.write(json.dumps(model) + "\n")
    print("[INFO] Eporner scraper complete")

if __name__ == "__main__":
    run()
