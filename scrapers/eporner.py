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

    # Find all links to model pages
    links = soup.find_all("a", href=True)

    for link in links:
        href = link["href"]

        if "/model/" in href:
            name = link.get_text(strip=True)

            parent_text = link.parent.get_text("\n", strip=True)

            videos_match = re.search(r"Videos:\s*([\d,]+)", parent_text)
            photos_match = re.search(r"Photos:\s*([\d,]+)", parent_text)

            if videos_match and photos_match and name:
                videos = int(videos_match.group(1).replace(",", ""))
                photos = int(photos_match.group(1).replace(",", ""))

                models.append({
                    "name": name,
                    "videos": videos,
                    "photos": photos,
                    "source": "eporner"
                })

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


def run():
    main()

if __name__ == "__main__":
    run()
