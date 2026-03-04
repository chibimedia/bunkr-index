import requests
from bs4 import BeautifulSoup
import json
import re
import os
import time

BASE_URL = "https://www.eporner.com/pornstar-list/"
HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

OUTPUT_FILE = "data/eporner.jl"


def get_total_pages():
    print("[INFO] Detecting total pages...")
    r = requests.get(BASE_URL, headers=HEADERS, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    pagination_links = soup.find_all("a", href=True)
    pages = []

    for link in pagination_links:
        match = re.search(r"/pornstar-list/(\d+)/", link["href"])
        if match:
            pages.append(int(match.group(1)))

    if not pages:
        print("[WARN] Could not detect pagination. Defaulting to 1 page.")
        return 1

    total_pages = max(pages)
    print(f"[INFO] Detected {total_pages} pages")
    return total_pages


def scrape_page(page_number):
    if page_number == 1:
        url = BASE_URL
    else:
        url = f"{BASE_URL}{page_number}/"

    print(f"[INFO] Fetching {url}")
    r = requests.get(url, headers=HEADERS, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    models = []

    # Each model entry appears as plain text with:
    # Model Name
    # Videos: X
    # Photos: Y

    text_blocks = soup.get_text("\n").split("\n")

    for i in range(len(text_blocks)):
        line = text_blocks[i].strip()

        # Look for "Videos: X"
        if line.startswith("Videos:"):
            try:
                videos = int(re.sub(r"[^\d]", "", line))

                photos_line = text_blocks[i + 1].strip()
                if photos_line.startswith("Photos:"):
                    photos = int(re.sub(r"[^\d]", "", photos_line))

                    # Model name is usually 1 line above "Videos:"
                    name = text_blocks[i - 1].strip()

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


def main():
    print("[INFO] Starting Eporner scraper")

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
    main()
