# MediaIndex v4 — Fapello + Bunkr

## What's confirmed working (tested live Feb 2025)

| Source | Method | Status |
|---|---|---|
| fapello.com | Plain `requests` | ✅ 200 OK, full static HTML |
| bunkr.si/cr/etc | patchright stealth browser | ⚠️ Needs CF bypass |
| bunkr-albums.io | patchright stealth browser | ⚠️ Needs CF bypass |

**Fapello will always populate the index.** Bunkr depends on whether patchright successfully bypasses Cloudflare on that CI run.

---

## Migration from previous versions

### STEP 1 — Clear your repo of old files
Delete or replace everything. Key files to get right:
- `scrape.yml` MUST be at `.github/workflows/scrape.yml` — NOT at repo root
- You can verify this in your repo by checking if `.github/workflows/` exists as a folder

### STEP 2 — Upload all v4 files

### STEP 3 — Run workflow with Fapello only first
In Actions → "Scrape & Index Albums" → Run workflow
Set `enable_bunkr` to **false** for the first run — this guarantees results fast.

### STEP 4 — Verify Fapello populated
Check https://chibimedia.github.io/bunkr-index — you should see model cards.

### STEP 5 — Enable Bunkr
Run again with `enable_bunkr: true`. The Bunkr results will merge with existing Fapello data.

---

## How the scraper works

### Fapello (confirmed working)

```
fapello.com/page-N/   →  requests.get (no auth, no CF)
  ↓ parse HTML
  - model slug from href="/slug/"
  - thumbnail from /content/X/X/{slug}/1000/{slug}_0001.jpg
  - name from link text
  - photo/video counts from "+ N photos / + N videos" text
  ↓
albums.json
```

Scrapes: new (30 pages), hot (5 pages), popular (5 pages) = ~400 models per run.

### Bunkr (stealth browser required)

```
patchright Chromium (patches CDP signals CF looks for)
  ↓
bunkr-albums.io pages  →  album IDs + card metadata
  ↓
bunkr.si/a/{id}?advanced=1  →  window.albumFiles, og:title, og:image
  ↓
albums.json
```

---

## Frontend features

- Unified search across Fapello + Bunkr
- **Source filter**: All / Fapello / Bunkr
- **Videos filter**: 🎬 Has Videos toggle
- **File count filter**: 1-9 / 10-49 / 50-199 / 200+
- **Sort**: newest, oldest, most files, A-Z
- Source badges on every card (pink = Fapello, purple = Bunkr)
- Video badge (green) on cards with video content
- Infinite scroll, Lunr.js fuzzy search, press `/` to focus

---

## Debugging if Bunkr still fails

1. Run locally with visible browser:
```bash
pip install -r requirements.txt
patchright install chromium
HEADLESS=false ENABLE_BUNKR=true MAX_ALBUMS=10 python scraper.py
```

2. Check `cache/` folder after a run — the `.html` files show what the browser actually got.
   If they contain "Just a moment" = CF is still blocking.
   If they contain album titles = the parser selectors may need updating.

3. Increase delays: `DELAY_MIN=3.0 DELAY_MAX=6.0`
