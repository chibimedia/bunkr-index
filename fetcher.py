"""
fetcher.py — Tiered HTTP fetching with CF bypass and debug artifacts.

Priority:
  1. requests      — fast, no overhead, works for APIs and non-CF sites
  2. cloudscraper  — handles Cloudflare JS challenge (IUAM) from CI/datacenter IPs
  3. playwright    — full browser fallback, saves storage_state on success

CF block detection is shared across all tiers.
Debug HTML saved to debug/<site>/<slug>.html on every fallback trigger.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests as _requests

log = logging.getLogger(__name__)

# ── Global config ──────────────────────────────────────────────────────────────
CACHE_DIR      = Path(os.getenv("CACHE_DIR", "cache"))
CACHE_TTL_SEC  = int(os.getenv("CACHE_TTL", str(6 * 3600)))
DEBUG_NO_CACHE = os.getenv("DEBUG_NO_CACHE", "false").lower() == "true"
DELAY_MIN      = float(os.getenv("DELAY_MIN", "1.5"))
DELAY_MAX      = float(os.getenv("DELAY_MAX", "3.0"))

CACHE_DIR.mkdir(exist_ok=True)

# Denylist: any title matching these (case-insensitive strip) is a placeholder
TITLE_DENYLIST = {
    "welcome", "welcome!", "access denied", "just a moment",
    "403", "forbidden", "503", "error", "attention required",
    "checking your browser", "ray id", "",
}


# ── CF block detection ─────────────────────────────────────────────────────────
_CF_SIGNALS = [
    "checking your browser",
    "just a moment",
    "enable javascript and cookies",
    "cf-browser-verification",
    "cloudflare ray id",
    "cf_chl_opt",
    "ddos-guard",
    "please wait",
    "your ip address",
]

def is_cf_block(html: str) -> bool:
    if not html or len(html) < 3000:
        return True
    low = html.lower()
    return any(s in low for s in _CF_SIGNALS)


def is_placeholder_title(title: str) -> bool:
    return (title or "").strip().lower() in TITLE_DENYLIST


def save_debug(site: str, slug: str, content: str | bytes):
    """Save raw response to debug/<site>/<slug>.html for later inspection."""
    d = Path("debug") / site
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9_-]", "_", slug.lower())[:80]
    fp = d / f"{safe}.html"
    if isinstance(content, bytes):
        fp.write_bytes(content)
    else:
        fp.write_text(content, encoding="utf-8", errors="replace")
    log.warning(f"[debug] Saved blocked response → {fp}")


# ── Cache helpers ──────────────────────────────────────────────────────────────
def _cache_path(url: str) -> Path:
    key = hashlib.sha1(url.encode()).hexdigest()
    return CACHE_DIR / f"{key}.html"

def _cache_valid(p: Path) -> bool:
    if not p.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return age < timedelta(seconds=CACHE_TTL_SEC)

def _cache_read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")

def _cache_write(p: Path, text: str):
    p.write_text(text, encoding="utf-8")


# ── Shared request session ─────────────────────────────────────────────────────
_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",   # NO br — avoids brotli decode failures
}
_session = _requests.Session()
_session.headers.update(_BASE_HEADERS)


def _sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


# ── Tier 1: plain requests ─────────────────────────────────────────────────────
def fetch_plain(
    url: str,
    *,
    site: str = "unknown",
    slug: str = "",
    use_cache: bool = True,
    retries: int = 3,
    timeout: int = 25,
    extra_headers: dict | None = None,
) -> Optional[str]:
    """
    Fetch with plain requests. Returns None if status != 200 or CF blocked.
    Use for APIs and sites without Cloudflare.
    """
    cp = _cache_path(url)
    if use_cache and not DEBUG_NO_CACHE and _cache_valid(cp):
        content = _cache_read(cp)
        if not is_cf_block(content):
            return content

    for attempt in range(1, retries + 1):
        _sleep()
        try:
            headers = dict(_BASE_HEADERS)
            if extra_headers:
                headers.update(extra_headers)
            r = _session.get(url, headers=headers, timeout=timeout)
            log.debug(f"[plain] {url} → {r.status_code} ({len(r.content)}B)")
            if r.status_code == 200:
                text = r.text
                if is_cf_block(text):
                    save_debug(site, slug or url[-40:], text)
                    log.warning(f"[plain] CF block on attempt {attempt}: {url}")
                    time.sleep(8 + attempt * 4)
                    continue
                if use_cache and not DEBUG_NO_CACHE:
                    _cache_write(cp, text)
                return text
            elif r.status_code == 429:
                wait = 30 + random.uniform(10, 20)
                log.warning(f"[plain] 429 rate-limit, sleeping {wait:.0f}s")
                time.sleep(wait)
            elif r.status_code == 404:
                return None
            else:
                log.warning(f"[plain] HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"[plain] Error attempt {attempt}: {e}")
        time.sleep((2 ** attempt) + random.uniform(1, 3))
    return None


def fetch_json(
    url: str,
    *,
    retries: int = 3,
    timeout: int = 25,
    extra_headers: dict | None = None,
) -> Optional[dict | list]:
    """Fetch JSON from an API endpoint (no CF, no caching by default)."""
    for attempt in range(1, retries + 1):
        _sleep()
        try:
            headers = {"Accept": "application/json", **_BASE_HEADERS}
            if extra_headers:
                headers.update(extra_headers)
            r = _session.get(url, headers=headers, timeout=timeout)
            log.debug(f"[json] {url} → {r.status_code}")
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                time.sleep(30 + random.uniform(10, 20))
            elif r.status_code == 404:
                return None
            else:
                log.warning(f"[json] HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"[json] Error attempt {attempt}: {e}")
        time.sleep((2 ** attempt) + random.uniform(1, 3))
    return None


# ── Tier 2: cloudscraper (CF JS-challenge bypass) ──────────────────────────────
_cs_session = None

def _get_cloudscraper():
    global _cs_session
    if _cs_session is not None:
        return _cs_session
    try:
        import cloudscraper
        _cs_session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True},
            delay=5,
        )
        _cs_session.headers.update(_BASE_HEADERS)
        log.info("[cloudscraper] Session created")
        return _cs_session
    except ImportError:
        log.error("[cloudscraper] Not installed!")
        return None


def fetch_cloudscraper(
    url: str,
    *,
    site: str = "unknown",
    slug: str = "",
    use_cache: bool = True,
    retries: int = 3,
) -> Optional[str]:
    """
    Fetch via cloudscraper. Solves Cloudflare JS challenge automatically.
    Works from CI/datacenter IPs for standard CF IUAM protection.
    """
    cs = _get_cloudscraper()
    if cs is None:
        return None

    cp = _cache_path(url)
    if use_cache and not DEBUG_NO_CACHE and _cache_valid(cp):
        content = _cache_read(cp)
        if not is_cf_block(content):
            return content

    for attempt in range(1, retries + 1):
        wait = random.uniform(DELAY_MIN, DELAY_MAX)
        log.info(f"[cs] GET {url} (attempt {attempt}, sleep {wait:.1f}s)")
        time.sleep(wait)
        try:
            r = cs.get(url, timeout=35)
            log.info(f"[cs] → {r.status_code} ({len(r.content)}B) enc={r.headers.get('Content-Encoding','none')}")
            if r.status_code == 200:
                text = r.text
                if is_cf_block(text):
                    save_debug(site, slug or url[-40:], text)
                    log.warning(f"[cs] CF still blocking attempt {attempt}")
                    time.sleep(12 + attempt * 6)
                    continue
                if use_cache and not DEBUG_NO_CACHE:
                    _cache_write(cp, text)
                return text
            elif r.status_code == 429:
                time.sleep(35 + random.uniform(10, 20))
            elif r.status_code == 404:
                return None
            else:
                log.warning(f"[cs] HTTP {r.status_code}: {url}")
        except Exception as e:
            log.warning(f"[cs] Error attempt {attempt}: {e}")
        time.sleep((2 ** attempt) + random.uniform(1, 3))
    return None


# ── Tier 3: playwright (full browser fallback) ─────────────────────────────────
_pw_ctx = None
_pw_instance = None
STORAGE_STATE_FILE = Path("browser_storage_state.json")


def _get_playwright_ctx():
    global _pw_ctx, _pw_instance
    if _pw_ctx is not None:
        return _pw_ctx
    try:
        from playwright.sync_api import sync_playwright
        _pw_instance = sync_playwright().start()
        browser = _pw_instance.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx_kwargs: dict = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": _BASE_HEADERS["User-Agent"],
            "locale": "en-US",
        }
        if STORAGE_STATE_FILE.exists():
            ctx_kwargs["storage_state"] = str(STORAGE_STATE_FILE)
        _pw_ctx = browser.new_context(**ctx_kwargs)
        _pw_ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
            window.chrome = {runtime: {}};
        """)
        log.info("[playwright] Browser context ready")
        return _pw_ctx
    except ImportError:
        log.error("[playwright] Not installed!")
        return None
    except Exception as e:
        log.error(f"[playwright] Failed to start: {e}")
        return None


def fetch_playwright(
    url: str,
    *,
    site: str = "unknown",
    slug: str = "",
    use_cache: bool = True,
    wait_ms: int = 3000,
) -> Optional[str]:
    """
    Full browser fetch via Playwright. Last resort for persistent CF blocks.
    Saves storage_state on successful CF solve for reuse.
    """
    cp = _cache_path(url)
    if use_cache and not DEBUG_NO_CACHE and _cache_valid(cp):
        content = _cache_read(cp)
        if not is_cf_block(content):
            return content

    ctx = _get_playwright_ctx()
    if ctx is None:
        return None

    from playwright.sync_api import TimeoutError as PWTimeout
    page = None
    try:
        page = ctx.new_page()
        log.info(f"[playwright] GET {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(wait_ms)
        content = page.content()

        if is_cf_block(content):
            log.info("[playwright] CF challenge detected, waiting 15s...")
            time.sleep(15)
            page.wait_for_load_state("networkidle", timeout=25_000)
            content = page.content()

        if not is_cf_block(content):
            # Save storage state (CF clearance cookie) for future runs
            try:
                ctx.storage_state(path=str(STORAGE_STATE_FILE))
                log.info("[playwright] Saved storage state")
            except Exception:
                pass
            if use_cache and not DEBUG_NO_CACHE:
                _cache_write(cp, content)
            return content
        else:
            save_debug(site, slug or url[-40:], content)
            log.error(f"[playwright] CF still blocking: {url}")
            return None

    except PWTimeout:
        log.warning(f"[playwright] Timeout: {url}")
        return None
    except Exception as e:
        log.warning(f"[playwright] Error: {e}")
        return None
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def playwright_stop():
    global _pw_ctx, _pw_instance
    try:
        if _pw_ctx:
            _pw_ctx.close()
        if _pw_instance:
            _pw_instance.stop()
    except Exception:
        pass
    _pw_ctx = None
    _pw_instance = None


# ── Auto-tiered fetch (plain → cloudscraper → playwright) ─────────────────────
def fetch(
    url: str,
    *,
    site: str = "unknown",
    slug: str = "",
    use_cache: bool = True,
    prefer_cs: bool = False,   # set True for known CF sites (e.g. fapello)
    force_playwright: bool = False,
) -> Optional[str]:
    """
    Auto-tiered fetch. Tries cheaper tiers first, escalates on CF block.
    - prefer_cs=True: skip plain requests, start with cloudscraper
    - force_playwright=True: go straight to playwright (for known hard blocks)
    """
    if force_playwright:
        return fetch_playwright(url, site=site, slug=slug, use_cache=use_cache)

    if not prefer_cs:
        result = fetch_plain(url, site=site, slug=slug, use_cache=use_cache)
        if result is not None:
            return result
        log.info(f"[fetch] Plain failed, escalating to cloudscraper: {url}")

    result = fetch_cloudscraper(url, site=site, slug=slug, use_cache=use_cache)
    if result is not None:
        return result

    log.info(f"[fetch] Cloudscraper failed, escalating to playwright: {url}")
    return fetch_playwright(url, site=site, slug=slug, use_cache=use_cache)
