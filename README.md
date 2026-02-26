# BunkrIndex v2

Fully automated, free, GitHub Pages-hosted searchable index of Bunkr albums.

## ⚠️ Critical fixes in v2 (why v1 returned 0 albums)

1. **Wrong API assumption** — `apidl.bunkr.ru/api/_001_v2` is for resolving individual *file* CDN URLs, **not** a listing/browse API. There is no public "browse all albums" Bunkr API.

2. **Correct strategy**: Scrape **bunkr-albums.io** (the actual directory site) for album IDs and metadata, then optionally enrich via `bunkr.si/a/{id}?advanced=1`.

3. **Workflow file location was wrong** — must be at `.github/workflows/scrape.yml`, not `scrape.yml` at repo root.

---

## Project structure

```
bunkr-index/
├── .github/
│   └── workflows/
│       └── scrape.yml       ← MUST be here (not at repo root)
├── scraper.py               ← Correct scraper using bunkr-albums.io
├── generate_rss.py
├── requirements.txt
├── albums.json              ← Auto-updated by Actions
├── feed.xml
└── index.html               ← GitHub Pages frontend
```

---

## Deploy to your repo (chibimedia/bunkr-index)

### 1. Update all files
Upload/replace all files from this package. The **critical** one is the correct path:
- Delete old `scrape.yml` from **repo root**
- Upload `scrape.yml` to `.github/workflows/scrape.yml`

### 2. Update RSS URL
In `generate_rss.py`, confirm:
```python
SITE_URL = "https://chibimedia.github.io/bunkr-index"
```

### 3. Enable GitHub Pages
Settings → Pages → Deploy from branch → `main` / `(root)` → Save

### 4. Run the scraper
Actions → "Scrape & Index Albums" → Run workflow

---

## How discovery works

```
bunkr-albums.io  (paginated HTML directory)
       ↓ album IDs + card metadata
   albums_by_id dict
       ↓ albums missing details
   bunkr.si/a/{id}?advanced=1  (enrichment)
       ↓ title, file_count, thumbnail, date, size
   albums.json  →  GitHub Pages frontend
```

The scraper:
- Extracts album cards from bunkr-albums.io (title, thumbnail, file count all in card HTML)
- For albums with missing data, enriches via Bunkr's actual album page using gallery-dl's proven parsing approach (`window.albumFiles`, `og:title`, `og:image`)
- Rotates across 14 Bunkr domains to handle CF challenges
- Deduplicates across runs

---

## Tuning

| Variable | Default | Notes |
|---|---|---|
| `MAX_ALBUMS` | 300 | New albums per run |
| `REQUEST_DELAY` | 1.5 | Seconds between requests |

Set in workflow dispatch input or repo Variables.
