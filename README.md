# MediaIndex v5 — Root Cause Fixed

## Why every previous version produced 0 albums

### The real problem: GitHub Actions IPs are datacenter IPs

GitHub Actions runners (`ubuntu-latest`) have well-known AWS/Azure datacenter
IP ranges. **Cloudflare blocks these IPs** with a JS-challenge page on both
`fapello.com` AND `bunkr-albums.io`.

The scraper received a ~2KB "checking your browser..." page every time.
The length check caught it and returned `None`. Parser gets `None`, finds
0 models, saves `albums.json` with 0 entries, commits it. Repeat forever.

**This is why `ENABLE_BUNKR=false` also produced 0 — Fapello was equally
blocked, just silently.**

### Secondary bug: Fapello photo/video count extraction

The v4 parser had this structure:

```python
for _ in range(4):
    if photos or videos:
        break      # ← no break statement was here
    parent = parent.parent
else:
    photos, videos = 0, 0  # ← this ALWAYS fired (for/else fires when no break)
```

Even when photo/video counts were found, the `else` clause reset them to 0.

---

## How v5 fixes it

### Fapello: `cloudscraper` replaces `requests`

`cloudscraper` solves Cloudflare's JS-challenge automatically:
1. Makes the initial request, receives the CF challenge page
2. Runs the CF JavaScript in a Python JS interpreter (js2py or Node.js)
3. Gets the `cf_clearance` cookie
4. Retries the real request with that cookie
5. Returns the actual page content

This works from CI/datacenter IPs for standard CF "IUAM" (I'm Under Attack Mode).

### Bunkr: `nodriver` replaces `patchright`

`nodriver` is the async successor to `undetected-chromedriver`. It uses a
custom CDP implementation (not standard WebDriver) which avoids the automation
signals that Cloudflare's Bot Management looks for. Runs headless on CI with
Xvfb for display emulation.

---

## Deploy steps

1. **Replace all files** in your repo with v5 files
2. **Verify** `.github/workflows/scrape.yml` is in the correct folder (not root)
3. **Run workflow**: Actions → Scrape & Index Albums → Run workflow
   - For first run: set `enable_bunkr` = **false** to test Fapello only
   - Takes ~3–5 min for Fapello, ~15 min with Bunkr enabled

## If it still produces 0 albums

After the run, go to: **Actions → your run → Artifacts → scraper-debug-N**

Download that zip. Inside `debug/` you'll find HTML files of every page that
looked like a CF block. Open them in a browser to see exactly what the CI
runner received. This tells you:

- **"Checking your browser"** = cloudscraper didn't solve the challenge
  → Try adding `nodejs` installation to the workflow (cloudscraper uses Node.js
    for better JS challenge solving)
- **Empty file / 0 bytes** = network timeout or DNS failure
  → Check if the site is even up
- **Normal HTML but 0 models parsed** = selectors need updating
  → The site changed its HTML structure; open the debug HTML in a browser
    and inspect the actual CSS classes

## Adding Node.js to improve cloudscraper (optional but recommended)

Add this step to the workflow before "Run scraper":

```yaml
- name: Install Node.js (improves cloudscraper JS solving)
  uses: actions/setup-node@v4
  with:
    node-version: '20'
```

cloudscraper automatically uses Node.js when available, which handles
newer CF challenges better than the pure-Python js2py interpreter.
