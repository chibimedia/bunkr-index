"""
scrapers/fapello.py — Fapello.com scraper

Fetch method: cloudscraper (Cloudflare JS-challenge bypass, works from CI)
Fallback:     playwright (if cloudscraper returns CF block)

Confirmed URL structure (from live fetches):
  Listing:   fapello.com/                  (newest)
             fapello.com/page-N/
             fapello.com/hot/              fapello.com/hot/page-N/
             fapello.com/popular/          fapello.com/popular/page-N/
             fapello.com/trending/         fapello.com/trending/page-N/
  Model:     fapello.com/{slug}/
  Avatar:    fapello.com/content/{c1}/{c2}/{slug}/1000/{slug}_0001.jpg

Bugs fixed vs v4:
  - Removed broken for/else that zeroed photo/video counts
  - cloudscraper instead of requests (CF bypass from CI IPs)
  - Denylist check on extracted title → needs_recheck flag
  - Consecutive-empty guard to prevent runaway pagination
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

from fetcher import fetch_cloudscraper, fetch_playwright, is_cf_block, is_placeholder_title, save_debug
from index import is_placeholder, now_iso, PLACEHOLDER_TITLES

log = logging.getLogger(__name__)

MAX_NEW   = int(os.getenv("MAX_ALBUMS", "500"))
MAX_PAGES = int(os.getenv("FAPELLO_MAX_PAGES", "30"))

_SKIP_SLUGS = {
    "hot", "trending", "popular", "forum", "login", "signup", "welcome",
    "random", "posts", "submit", "videos", "contacts", "activity",
    "daily-search-ranking", "top-likes", "top-followers", "popular_videos",
    "what-is-fapello", "a",
}


def _url(section: str, page: int) -> str:
    base = "https://fapello.com"
    if section == "new":
        return f"{base}/" if page == 1 else f"{base}/page-{page}/"
    return f"{base}/{section}/" if page == 1 else f"{base}/{section}/page-{page}/"


def _fetch(url: str, slug: str = "") -> Optional[str]:
    """Fetch via cloudscraper, fall back to playwright on CF block."""
    html = fetch_cloudscraper(url, site="fapello", slug=slug)
    if html is None or is_cf_block(html):
        log.info(f"[fapello] cloudscraper failed → playwright: {url}")
        html = fetch_playwright(url, site="fapello", slug=slug)
    return html


def parse_listing(html: str) -> list[dict]:
    """
    Parse one fapello.com listing page and return a list of model records.

    HTML structure (confirmed live):
    - Each model link is <a href="/slug/"> wrapping a post card
    - Avatar img: /content/{c1}/{c2}/{slug}/1000/{slug}_0001.jpg
    - Post text contains "+N photos" and/or "+N videos"
    - Model name in <p>, <h2>, <h3>, or <span> near the link
    """
    soup = BeautifulSoup(html, "lxml")
    models: dict[str, dict] = {}

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].rstrip("/")

        m = re.match(
            r"^(?:https?://fapello\.com)?/([a-zA-Z0-9][a-zA-Z0-9_.-]{1,59})$",
            href,
        )
        if not m:
            continue
        slug = m.group(1)
        if slug in _SKIP_SLUGS or slug.startswith("page-"):
            continue
        # Skip anything that ends in 3+ digits (post IDs not model slugs)
        if re.search(r"\d{3,}$", slug):
            continue

        # ── Thumbnail ──────────────────────────────────────────────────────────
        thumb = None
        node = a_tag
        for _ in range(6):
            if node is None:
                break
            for img in node.find_all("img", src=True):
                src = img["src"]
                if "/content/" in src and "/1000/" in src and slug[:5] in src:
                    thumb = src if src.startswith("http") else "https://fapello.com" + src
                    break
            if thumb:
                break
            node = node.parent

        if not thumb and len(slug) >= 2:
            thumb = (
                f"https://fapello.com/content/{slug[0]}/{slug[1]}/"
                f"{slug}/1000/{slug}_0001.jpg"
            )

        # ── Display name ───────────────────────────────────────────────────────
        name = ""
        node = a_tag
        for _ in range(5):
            if node is None:
                break
            for tag in node.find_all(["p", "h2", "h3", "h4", "span"], limit=6):
                t = tag.get_text(strip=True)
                if 2 <= len(t) <= 80 and not t.startswith("+") and not t.startswith("http"):
                    name = t
                    break
            if name:
                break
            node = node.parent
        if not name:
            name = a_tag.get_text(strip=True)
        if not name or len(name) < 2:
            name = slug.replace("-", " ").title()

        # ── Photo/video counts ─────────────────────────────────────────────────
        # Strategy:
        #   1. Search WITHIN the <a> tag itself first (covers flat HTML)
        #   2. Walk UP but only into bounded container tags (div/article/li)
        #      and stop before body/html/nav (avoids cross-card contamination)
        #
        # FIX vs v4: v4 walked up to <body> and got ALL page counts mixed
        # together. The for/else then zeroed everything because there was no
        # break in the loop body when counts were found.
        _CONTAINER = {"div", "article", "li", "section", "figure"}
        _STOP      = {"body", "html", "main", "nav", "header", "footer", "aside"}

        photos, videos = 0, 0

        # Step 1: within the link itself
        self_text = a_tag.get_text()
        pm = re.findall(r"\+\s*(\d+)\s*photos?", self_text, re.I)
        vm = re.findall(r"\+\s*(\d+)\s*videos?", self_text, re.I)
        if pm or vm:
            photos = sum(int(x) for x in pm)
            videos = sum(int(x) for x in vm)
        else:
            # Step 2: walk up, stop at structural boundary
            node = a_tag.parent
            for _ in range(6):
                if node is None or node.name in _STOP:
                    break
                node_text = node.get_text()
                pm = re.findall(r"\+\s*(\d+)\s*photos?", node_text, re.I)
                vm = re.findall(r"\+\s*(\d+)\s*videos?", node_text, re.I)
                if (pm or vm) and node.name in _CONTAINER:
                    photos = sum(int(x) for x in pm)
                    videos = sum(int(x) for x in vm)
                    break
                node = node.parent

        # ── Denylist check ─────────────────────────────────────────────────────
        needs_recheck = is_placeholder_title(name)

        if slug not in models:
            models[slug] = {
                "id":            f"fapello:{slug}",
                "title":         name,
                "slug":          slug,
                "source":        "fapello",
                "url":           f"https://fapello.com/{slug}/",
                "thumbnail":     thumb,
                "file_count":    photos + videos,
                "photo_count":   photos,
                "video_count":   videos,
                "has_videos":    videos > 0,
                "date":          None,
                "indexed_at":    now_iso(),
                "needs_recheck": needs_recheck,
                "extra":         {},
            }
        else:
            # Accumulate counts from multiple posts of the same model
            rec = models[slug]
            rec["photo_count"] = rec.get("photo_count", 0) + photos
            rec["video_count"] = rec.get("video_count", 0) + videos
            rec["file_count"]  = rec["photo_count"] + rec["video_count"]
            if videos > 0:
                rec["has_videos"] = True
            if thumb and not rec.get("thumbnail"):
                rec["thumbnail"] = thumb
            # If we now have a real name, clear needs_recheck
            if not is_placeholder_title(name) and rec.get("needs_recheck"):
                rec["title"] = name
                rec["needs_recheck"] = False

    return list(models.values())


def scrape(max_pages: int = MAX_PAGES) -> list[dict]:
    """
    Scrape Fapello listing feeds and return deduplicated model records.
    Stops early when no new models found for 3 consecutive pages.
    """
    all_models: dict[str, dict] = {}

    feeds = [
        ("new",      range(1, max_pages + 1)),
        ("hot",      range(1, 6)),
        ("popular",  range(1, 6)),
        ("trending", range(1, 6)),
    ]

    for section, page_range in feeds:
        log.info(f"[fapello] ── {section} feed ──")
        empty_streak = 0

        for page_num in page_range:
            if len(all_models) >= MAX_NEW:
                break

            url  = _url(section, page_num)
            html = _fetch(url, slug=f"{section}_p{page_num}")

            if not html or is_cf_block(html):
                log.warning(f"[fapello] No usable HTML for {url}")
                empty_streak += 1
                if empty_streak >= 2:
                    log.info(f"[fapello] 2 consecutive failures on {section}, skipping rest")
                    break
                continue

            models   = parse_listing(html)
            new_here = 0

            for m in models:
                slug = m["slug"]
                if slug not in all_models:
                    all_models[slug] = m
                    new_here += 1
                else:
                    # Update counts if we have more data
                    existing = all_models[slug]
                    if m["file_count"] > existing["file_count"]:
                        all_models[slug].update(m)
                    # Never overwrite a good title with a placeholder
                    if is_placeholder(m) and not is_placeholder(existing):
                        all_models[slug]["title"] = existing["title"]

            log.info(
                f"[fapello] {section} p{page_num}: "
                f"{len(models)} models ({new_here} new, {len(all_models)} total)"
            )

            if new_here == 0:
                empty_streak += 1
                if empty_streak >= 3:
                    log.info(f"[fapello] 3 empty pages, moving to next feed")
                    break
            else:
                empty_streak = 0

        if len(all_models) >= MAX_NEW:
            break

    result = list(all_models.values())
    placeholder_n = sum(1 for r in result if r.get("needs_recheck"))
    log.info(f"[fapello] Done: {len(result)} unique models, {placeholder_n} need recheck")
    return result
