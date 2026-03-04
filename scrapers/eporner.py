import json
import os
import re
import time
import cloudscraper
from bs4 import BeautifulSoup

BASE_URL = "https://www.eporner.com/pornstar-list/"
OUTPUT_FILE = "data/eporner.jl"

AGE_GATE_COOKIES = {
    "age_verified": "1",
    "bs": "1"
}

scraper = cloudscraper.create_scraper(
    browser={
        "browser": "chrome",
        "platform": "windows",
        "mobile": False
    }
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
    if page_number == 1:
        url = BASE_URL
    else:
        url = f"{BASE_URL}{page_number}/"

    print(f"[INFO] Fetching {url}")
    r = scraper.get(url, cookies=AGE_GATE_COOKIES, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    text = soup.get_text("\n")
    lines = text.split("\n")

    models = []

    for i in range(len(lines)):
        line = lines[i].strip()

        if line.startswith("Videos:"):
            try:
                videos = int(re.sub(r"[^\d]", "", line))
                photos_line = lines[i + 1].strip()

                if photos_line.startswith("Photos:"):
                    photos = int(re.sub(r"[^\d]", "", photos_line))
                    name = lines[i - 1].strip()

                    if name and videos > 0:
                        models.append({
                            "name": name,
                            "videos": videos,
                            "photos": photos,
                            "source": "eporner"
                        })

            except Exception:
                continue

    print(f"[INFO] → {len(models)} models found on page {page_number}")
    return models


def run():
    print("[INFO] Starting Eporner scraper (cloudscraper)")
    os.makedirs("data", exist_ok=True)

    total_pages = get_total_pages()
    all_models = []

    for page in range(1, total_pages + 1):
        models = scrape_page(page)
        all_models.extend(models)
        time.sleep(1)

    print(f"[INFO] Total models scraped: {len(all_models)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for model in all_models:
            f.write(json.dumps(model) + "\n")

    print("[INFO] Eporner scraper complete")


if __name__ == "__main__":
    run()
