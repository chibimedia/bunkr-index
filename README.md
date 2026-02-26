# BunkrIndex v3 — Stealth Browser Edition

## Why everything before this failed

### v1 — wrong assumption about the API
`apidl.bunkr.ru/api/_001_v2` is not a listing API. It's a file-resolution endpoint — you POST a file `{id}` to get its CDN URL. No album browsing.

### v2 — `requests` gets blocked by Cloudflare
`bunkr-albums.io` runs Cloudflare Bot Management. A plain `requests` call returns a CF challenge page with no HTML content. The scraper parsed an empty page = 0 albums.

### v3 — **This version** — stealth Chromium via `patchright`
`patchright` is a drop-in Playwright fork that patches the CDP (Chrome DevTools Protocol) automation signals that Cloudflare uses to detect headless browsers. It:
- Sets `navigator.webdriver = undefined`
- Patches timing/fingerprint signals at the CDP level
- Uses a persistent browser context to retain CF clearance cookies
- Runs fully in GitHub Actions CI (headless mode)

---

## Architecture

```
bunkr-albums.io  (Cloudflare-protected HTML directory)
      ↓  patchright stealth Chromium
   Album IDs + card metadata (title, thumb, file count)
      ↓  for albums missing data
   bunkr.si/a/{id}?advanced=1  (gallery-dl's proven approach)
      ↓  window.albumFiles, og:title, og:image, timestamp
   albums.json  →  GitHub Pages frontend
```

---

## File structure

```
bunkr-index/
├── .github/
│   └── workflows/
│       └── scrape.yml     ← MUST be here (not at repo root!)
├── scraper.py             ← patchright stealth scraper
├── generate_rss.py        ← RSS generator
├── requirements.txt       ← patchright, bs4, lxml, requests
├── albums.json            ← auto-updated by Actions
├── feed.xml               ← RSS feed
└── index.html             ← GitHub Pages frontend
```

---

## Migration steps for chibimedia/bunkr-index

### Step 1 — Delete the misplaced workflow file
In your repo, delete `scrape.yml` from the **root directory**.
The workflow MUST be at `.github/workflows/scrape.yml`.

### Step 2 — Replace all files
Upload all files from this package. The critical new dependency is `patchright`.

### Step 3 — Enable GitHub Pages (if not already)
Settings → Pages → Deploy from branch → `main` / `(root)` → Save

### Step 4 — Run the workflow
Actions → "Scrape & Index Albums" → Run workflow → Run workflow

The first run will:
1. Install Python + patchright
2. Install patched Chromium (`patchright install chromium --with-deps`) — this takes ~2 min
3. Launch stealth browser, open bunkr-albums.io
4. Scrape album cards across up to 15 pages
5. Enrich up to 60 albums via bunkr.si
6. Commit albums.json + feed.xml

---

## Tuning environment variables

| Variable | Default | Notes |
|---|---|---|
| `MAX_ALBUMS` | `300` | New albums per run |
| `REQUEST_DELAY` | `3.0` | Seconds between page loads |
| `HEADLESS` | `true` | Set to `false` for local debugging (opens real window) |

---

## Local debugging

```bash
pip install -r requirements.txt
patchright install chromium

# Run with visible browser window to see what's happening
HEADLESS=false MAX_ALBUMS=10 python scraper.py

# Check what's in cache/ to see what the browser actually loaded
ls cache/
```

---

## If patchright still gets blocked

Some Cloudflare configurations are very aggressive. Options:

1. **Increase delays** — set `REQUEST_DELAY=5.0` or higher
2. **Check `cache/` folder** — look at the HTML files. If they all say "Checking your browser" then CF is blocking. If they have album data, the parser selectors may need adjusting.
3. **Inspect the actual HTML** — run locally with `HEADLESS=false`, open browser DevTools, copy the actual CSS classes from album cards, and update the selectors in `scraper.py`'s `scrape_bunkr_albums_io()` function.
