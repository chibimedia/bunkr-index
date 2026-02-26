# BunkrIndex — Self-Hosted Album Discovery Site

A fully automated, free, GitHub Pages-hosted searchable index of Bunkr albums.  
**No servers. No costs. Auto-updates every 6 hours.**

---

## 🗂 Project Structure

```
bunkr-index/
├── .github/
│   └── workflows/
│       └── scrape.yml      ← GitHub Actions automation
├── scraper.py              ← Metadata-only scraper (no downloads)
├── generate_rss.py         ← RSS feed generator
├── requirements.txt        ← Python deps
├── albums.json             ← Auto-generated index (committed by bot)
├── feed.xml                ← RSS feed (auto-generated)
└── index.html              ← Static frontend (served by GitHub Pages)
```

---

## 🚀 Deployment (5 minutes)

### Step 1 — Fork / create repo

1. Create a new **public** GitHub repo (e.g. `bunkr-index`).
2. Upload all files from this project into it.

### Step 2 — Enable GitHub Pages

1. Go to **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: `main` / `(root)`
4. Click **Save**

Your site will be live at:  
`https://YOUR_USERNAME.github.io/bunkr-index/`

### Step 3 — Update the RSS site URL

Edit `generate_rss.py` and change:
```python
SITE_URL = "https://YOUR_USERNAME.github.io/bunkr-index"
```

### Step 4 — Trigger the first scrape

1. Go to **Actions → Scrape & Index Albums**
2. Click **Run workflow**
3. Wait ~2 minutes for it to complete
4. Reload your GitHub Pages URL — albums will appear!

### Step 5 — Automatic updates

The workflow runs automatically every 6 hours. No action needed.  
You can also manually trigger it anytime from the Actions tab.

---

## ⚙️ Configuration

| Env Variable | Default | Description |
|---|---|---|
| `MAX_ALBUMS` | `500` | Max new albums to index per run |
| `REQUEST_DELAY` | `1.5` | Seconds between HTTP requests |

Set these in the workflow dispatch inputs or repo **Settings → Secrets and variables → Actions → Variables**.

---

## 🔍 Features

### Frontend
- ⚡ Instant client-side search (Lunr.js full-text with fuzzy matching)
- 🎨 Dark mode only — premium design with animated cards
- 📱 Responsive grid (works on mobile)
- ∞ Infinite scroll — loads 60 cards at a time
- 🔢 Filter by file count range (1–9, 10–49, 50–199, 200+)
- 🖼 Filter by thumbnail presence
- ↕️ Sort by date, file count, or title
- ⌨️ Press `/` to focus the search bar instantly
- 📡 RSS feed (`/feed.xml`) for latest 50 albums

### Scraper
- Tries the unofficial Bunkr API first, falls back to HTML scraping
- Deduplicates across runs — never re-indexes known albums
- Enriches albums without thumbnails via detail page scraping
- Graceful retry on network errors (3 attempts with backoff)
- Stores only metadata — **zero file downloads**

---

## 📊 How the Index Grows

| Run | New Albums Added |
|-----|----------------|
| First | Up to 500 |
| Each subsequent | New albums since last run |
| After 1 week | 500–3,500+ total |

The scraper is conservative with rate limiting (1.5s between requests) to avoid bans.

---

## 🛠 Enhancements You Can Add

### Tag / category filtering
Parse album titles to auto-detect categories and add filter pills.

### Better discovery
Seed with known album IDs from external lists, then let the scraper expand from there.

### Sitemap
Add a `generate_sitemap.py` that creates `sitemap.xml` for Google indexing.

### Dark/light mode toggle
Add a CSS `[data-theme=light]` override and a toggle button.

### Album detail pages
Generate static `a/ALBUM_ID.html` pages for each album (better SEO).

---

## 📜 Legal

This project indexes **only publicly available metadata** (titles, file counts, thumbnail URLs that are already publicly visible). No files are downloaded. This is equivalent to a search engine index.

---

## 🤝 Contributing

PRs welcome! Key areas to improve:
- Better Bunkr API reverse engineering
- Additional scraping fallbacks
- SEO improvements (structured data, sitemaps)
