import asyncio
import json
import os
import re
from playwright.async_api import async_playwright

BASE_URL = "https://www.eporner.com/pornstar-list/"
OUTPUT_FILE = "data/eporner.jl"


async def get_total_pages(page):
    print("[INFO] Detecting total pages...")
    await page.goto(BASE_URL, wait_until="networkidle")

    links = await page.locator("a").all()
    pages = []

    for link in links:
        href = await link.get_attribute("href")
        if href:
            match = re.search(r"/pornstar-list/(\d+)/", href)
            if match:
                pages.append(int(match.group(1)))

    if not pages:
        print("[WARN] Could not detect pagination. Defaulting to 1 page.")
        return 1

    total = max(pages)
    print(f"[INFO] Detected {total} pages")
    return total


async def scrape_page(page, page_number):
    if page_number == 1:
        url = BASE_URL
    else:
        url = f"{BASE_URL}{page_number}/"

    print(f"[INFO] Fetching {url}")
    await page.goto(url, wait_until="networkidle")

    content = await page.content()

    models = []
    lines = content.split("\n")

    for i in range(len(lines)):
        line = lines[i].strip()

        if "Videos:" in line:
            try:
                videos_match = re.search(r"Videos:\s*([\d,]+)", line)
                photos_match = re.search(r"Photos:\s*([\d,]+)", lines[i + 1])

                if videos_match and photos_match:
                    videos = int(videos_match.group(1).replace(",", ""))
                    photos = int(photos_match.group(1).replace(",", ""))

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


async def main_async():
    print("[INFO] Starting Eporner scraper (Playwright)")

    os.makedirs("data", exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        # Inject age gate cookies
        await context.add_cookies([
            {
                "name": "age_verified",
                "value": "1",
                "domain": ".eporner.com",
                "path": "/"
            },
            {
                "name": "bs",
                "value": "1",
                "domain": ".eporner.com",
                "path": "/"
            }
        ])

        page = await context.new_page()

        total_pages = await get_total_pages(page)

        all_models = []

        for page_number in range(1, total_pages + 1):
            models = await scrape_page(page, page_number)
            all_models.extend(models)

        await browser.close()

    print(f"[INFO] Total models scraped: {len(all_models)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for model in all_models:
            f.write(json.dumps(model) + "\n")

    print("[INFO] Eporner scraper complete")


def run():
    asyncio.run(main_async())


if __name__ == "__main__":
    run()
