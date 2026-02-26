# MediaIndex — Multi-source scraper

Indexes albums/videos from Fapello, Kemono.su, Eporner, Erome, Cyberdrop, and Bunkr into a unified `albums.json` with a React frontend.

---

## Architecture

```
scraper.py          ← orchestrator (runs all sources, saves index)
fetcher.py          ← tiered HTTP: requests → cloudscraper → playwright
index.py            ← schema, dedup, merge, commit guard
scrapers/
  fapello.py        ← cloudscraper default (CI-safe CF bypass)
  kemono.py         ← API (plain requests, no Cloudflare)
  eporner.py        ← API (plain requests, no Cloudflare)
  erome.py          ← plain requests + cloudscraper fallback
  cyberdrop.py      ← plain requests + mirror rotation + cloudscraper
  bunkr.py          ← playwright required (CF Bot Management)
tests.py            ← 52 unit tests, all parsers
samples/            ← HTML/JSON fixtures used by tests
debug/              ← saved CF block pages (uploaded as CI artifacts)
```

### Source reliability ranking

| Source     | Method               | Works from CI? | Notes |
|------------|----------------------|----------------|-------|
| Eporner    | Official JSON API    | ✅ Always       | No auth, no CF |
| Kemono     | Official JSON API    | ✅ Always       | No auth, no CF |
| Fapello    | cloudscraper         | ✅ Usually      | CF IUAM bypass |
| Erome      | requests/cloudscraper| ✅ Usually      | CF sometimes |
| Cyberdrop  | requests + mirrors   | ⚠️ Varies       | 403 → mirror rotation |
| Bunkr      | playwright           | ⚠️ Needs setup  | CF Bot Management |

---

## Deploy to your repo

Replace all files, then:

```bash
# First run — test APIs only (fast, reliable)
ENABLE_BUNKR=false ENABLE_FAPELLO=false python scraper.py

# Add Fapello once APIs are confirmed working
ENABLE_BUNKR=false python scraper.py

# Full run with Bunkr (needs playwright installed)
python scraper.py
```

### Required env vars (GitHub Actions secrets/vars)

| Variable         | Default  | Description |
|------------------|----------|-------------|
| `MAX_ALBUMS`     | `500`    | Max new records per run |
| `ENABLE_BUNKR`   | `false`  | Enable Bunkr (playwright required) |
| `ENABLE_FAPELLO` | `true`   | Enable Fapello scraper |
| `ENABLE_KEMONO`  | `true`   | Enable Kemono API |
| `ENABLE_EPORNER` | `true`   | Enable Eporner API |
| `ENABLE_EROME`   | `true`   | Enable Erome scraper |
| `DELAY_MIN`      | `2.0`    | Min seconds between requests |
| `DELAY_MAX`      | `4.5`    | Max seconds between requests |
| `DEBUG_NO_CACHE` | `false`  | Bypass cache (for debugging) |
| `FORCE_COMMIT`   | `false`  | Skip commit guard |

---

## Why previous versions produced 0 albums

GitHub Actions runners have AWS/Azure datacenter IPs. Cloudflare blocks them with a JS-challenge page (~2KB "checking your browser…"). The scraper received this page, the length check triggered, parser got nothing, saved 0 albums.

**Fix:** `cloudscraper` for Fapello/Erome runs the CF JavaScript in Python (via js2py/Node.js), gets the `cf_clearance` cookie, retries with it. Works from CI IPs.

Additionally, the v4 Fapello parser had a `for...else` bug:
```python
for parent in ...:
    ...
else:
    photos, videos = 0, 0  # fires when loop completes WITHOUT break
```
Since there was never a `break`, the `else` always fired, zeroing all counts.

---

## Debugging zero results

After a run, download the **scraper-debug artifact** from GitHub Actions.

- `debug/fapello/*.html` — pages that looked like CF blocks
- `debug/kemono/*.html` — API error responses  
- `validation.json` — counts per source

If `debug/fapello/new_p1.html` contains "Checking your browser" → cloudscraper isn't solving the challenge. Add Node.js to the workflow (cloudscraper uses Node for harder challenges).

If `debug/fapello/new_p1.html` is empty → network timeout. Check if fapello.com is up.

If `debug/fapello/new_p1.html` is real HTML but 0 models → selectors changed. Open the file in a browser and inspect the actual CSS classes, compare to `scrapers/fapello.py`.

---

## Commit guard

`albums.json` is only committed when:
- `meta.total > 0` (non-empty index)
- `meta.placeholder_count / meta.total < 0.05` (< 5% "Welcome!" titles)

Override with `FORCE_COMMIT=true` (for manual debugging runs).

---

## Running tests

```bash
pip install requests cloudscraper brotlicffi beautifulsoup4 lxml
python tests.py
# → 52 passed, 0 failed
```

---

## Adding a new source

1. Create `scrapers/newsite.py` with a `scrape() -> list[dict]` function
2. Each record must have: `id`, `title`, `source`, `url`, `thumbnail`, `file_count`, `has_videos`, `indexed_at`
3. Add 5 sample HTML files to `samples/newsite/`
4. Add unit tests to `tests.py`
5. Call `run_source("newsite", ..., lambda: newsite.scrape(...), ...)` in `scraper.py`
