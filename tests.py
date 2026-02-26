#!/usr/bin/env python3
"""
tests.py — Unit tests for all parser functions.

Each test loads a saved sample HTML from samples/<site>/ and
verifies the parser extracts the expected fields.

Run: python tests.py
"""
import json
import logging
import sys
import os
from pathlib import Path

# Silence logging during tests
logging.disable(logging.CRITICAL)

# Set up path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "scrapers"))

os.environ.setdefault("MAX_ALBUMS", "500")
os.environ.setdefault("DELAY_MIN", "0")
os.environ.setdefault("DELAY_MAX", "0")

PASS = "✓"
FAIL = "✗"
results = []


def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    print(f"  {status} {name}" + (f"  [{detail}]" if detail else ""))
    return condition


# ═══════════════════════════════════════════════════════════════════════════════
# FAPELLO PARSER TESTS
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── Fapello parser ──")

from scrapers.fapello import parse_listing

# Sample 1: Normal listing page with multiple models (div-wrapped like real fapello)
FAPELLO_GOOD = """
<html><body>
<div class="post-card">
  <a href="/bella-rose/">
    <img src="/content/b/e/bella-rose/1000/bella-rose_0001.jpg">
  </a>
  <p>Bella Rose</p>
  <span>+ 142 photos</span>
  <span>+ 12 videos</span>
</div>
<div class="post-card">
  <a href="/jenny-fox/">
    <img src="/content/j/e/jenny-fox/1000/jenny-fox_0001.jpg">
  </a>
  <p>Jenny Fox</p>
  <span>+ 88 photos</span>
</div>
<a href="/hot/">Navigation link</a>
<a href="/trending/">Navigation link</a>
<a href="/page-2/">Page link</a>
</body></html>
"""
r = parse_listing(FAPELLO_GOOD)
check("fapello: finds 2 models (not nav links)", len(r) == 2, f"got {len(r)}")
if r:
    m = next((x for x in r if x["slug"] == "bella-rose"), None)
    check("fapello: correct slug", m is not None)
    check("fapello: photo count extracted", m and m.get("photo_count", 0) == 142, f"got {m and m.get('photo_count')}")
    check("fapello: video count extracted", m and m.get("video_count", 0) == 12, f"got {m and m.get('video_count')}")
    check("fapello: has_videos=True", m and m.get("has_videos") is True)
    check("fapello: thumbnail URL", m and "bella-rose" in (m.get("thumbnail") or ""))
    check("fapello: source=fapello", m and m.get("source") == "fapello")
    check("fapello: id format", m and m.get("id") == "fapello:bella-rose")

# Sample 2: Model without counts (counts should be 0, not crash)
FAPELLO_NO_COUNTS = """
<html><body>
<a href="/solo-girl/">
  <p>Solo Girl</p>
</a>
</body></html>
"""
r2 = parse_listing(FAPELLO_NO_COUNTS)
check("fapello: parse with no counts", len(r2) == 1, f"got {len(r2)}")
check("fapello: zero counts not crash", r2 and r2[0].get("file_count", -1) == 0)

# Sample 3: CF block page should return empty
CF_PAGE = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
r3 = parse_listing(CF_PAGE)
# Parser doesn't check CF itself, but should find 0 models
check("fapello: CF page yields 0 models", len(r3) == 0, f"got {len(r3)}")

# Sample 4: Welcome/placeholder title should be marked needs_recheck
FAPELLO_WELCOME = """
<html><body>
<a href="/somemodel/">
  <p>Welcome!</p>
  <span>+ 50 photos</span>
</a>
</body></html>
"""
r4 = parse_listing(FAPELLO_WELCOME)
check("fapello: placeholder title needs_recheck", r4 and r4[0].get("needs_recheck") is True)

# Sample 5: Multiple posts per model accumulate counts (need container divs)
FAPELLO_MULTI = """
<html><body>
<div class="post-card"><a href="/multi-model/"><p>Multi Model</p><span>+ 30 photos</span></a></div>
<div class="post-card"><a href="/multi-model/"><p>Multi Model</p><span>+ 20 photos</span></a></div>
</body></html>
"""
r5 = parse_listing(FAPELLO_MULTI)
check("fapello: deduplicates model slug", len(r5) == 1, f"got {len(r5)}")
check("fapello: accumulates photo counts", r5 and r5[0].get("photo_count", 0) == 50, f"got {r5 and r5[0].get('photo_count')}")


# ═══════════════════════════════════════════════════════════════════════════════
# KEMONO PARSER TESTS
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── Kemono parser ──")

from scrapers.kemono import _parse_post

POST_GOOD = {
    "id": "12345",
    "user": "99999",
    "service": "patreon",
    "title": "July Art Pack",
    "content": "This month's art...",
    "published": "2024-07-15T12:00:00",
    "file": {"name": "cover.jpg", "path": "/data/ab/cd/cover.jpg"},
    "attachments": [
        {"name": "img1.jpg",  "path": "/data/ab/cd/img1.jpg"},
        {"name": "img2.png",  "path": "/data/ab/cd/img2.png"},
        {"name": "video.mp4", "path": "/data/ab/cd/video.mp4"},
    ],
}
kr = _parse_post(POST_GOOD)
check("kemono: id format", kr["id"] == "kemono:patreon:99999:12345")
check("kemono: title extracted", kr["title"] == "July Art Pack")
check("kemono: source=kemono", kr["source"] == "kemono")
check("kemono: file_count (file+attachments, deduped)", kr["file_count"] >= 3)
check("kemono: has_videos from .mp4", kr["has_videos"] is True)
check("kemono: thumbnail from first image", kr["thumbnail"] is not None and "/data/" in kr["thumbnail"])
check("kemono: date parsed", kr["date"] is not None and "2024-07-15" in kr["date"])
check("kemono: service in extra", kr["extra"]["service"] == "patreon")

POST_EMPTY = {"id": "000", "user": "111", "service": "fanbox", "title": "", "attachments": [], "file": {}}
kr2 = _parse_post(POST_EMPTY)
check("kemono: empty post needs_recheck", kr2["needs_recheck"] is True)
check("kemono: empty post has fallback title", len(kr2["title"]) > 0)


# ═══════════════════════════════════════════════════════════════════════════════
# EPORNER PARSER TESTS
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── Eporner parser ──")

from scrapers.eporner import _parse_video

VIDEO_GOOD = {
    "id": "IsabYDAiqXa",
    "title": "Young Teen Heather",
    "keywords": "Teen, Petite, brunette",
    "views": 260221,
    "rate": "4.13",
    "url": "https://www.eporner.com/hd-porn/IsabYDAiqXa/Young-Teen-Heather/",
    "added": "2019-11-21 11:42:47",
    "length_sec": 2539,
    "length_min": "42:19",
    "embed": "https://www.eporner.com/embed/IsabYDAiqXa/",
    "default_thumb": {"size": "big", "src": "https://cdn.eporner.com/thumbs/5_360.jpg"},
    "thumbs": [],
}
er = _parse_video(VIDEO_GOOD)
check("eporner: id format", er["id"] == "eporner:IsabYDAiqXa")
check("eporner: title", er["title"] == "Young Teen Heather")
check("eporner: has_videos=True", er["has_videos"] is True)
check("eporner: thumbnail", "cdn.eporner.com" in (er.get("thumbnail") or ""))
check("eporner: date parsed", er["date"] is not None and "2019" in er["date"])
check("eporner: length_sec in extra", er["extra"]["length_sec"] == 2539)
check("eporner: views in extra", er["extra"]["views"] == 260221)
check("eporner: source=eporner", er["source"] == "eporner")

VIDEO_NOTITLE = {"id": "abc", "title": "", "default_thumb": None, "thumbs": [], "added": None}
er2 = _parse_video(VIDEO_NOTITLE)
check("eporner: empty title needs_recheck", er2["needs_recheck"] is True)


# ═══════════════════════════════════════════════════════════════════════════════
# EROME PARSER TESTS
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── Erome parser ──")

from scrapers.erome import _parse_album_page, _extract_album_files

EROME_WITH_JS = """
<html>
<head>
  <meta property="og:title" content="Summer Collection 2024">
  <meta property="og:image" content="https://cdn.erome.me/thumb.jpg">
  <title>Summer Collection 2024 - Erome</title>
</head>
<body>
<script>
  window.albumFiles = [
    {"url": "https://cdn.erome.me/img1.jpg", "type": "image"},
    {"url": "https://cdn.erome.me/img2.png", "type": "image"},
    {"url": "https://cdn.erome.me/video.mp4", "type": "video"}
  ];
</script>
</body>
</html>
"""
files = _extract_album_files(EROME_WITH_JS)
check("erome: extract 3 files from window.albumFiles", len(files) == 3, f"got {len(files)}")

erm = _parse_album_page(EROME_WITH_JS, "testid123")
check("erome: title from og:title", erm["title"] == "Summer Collection 2024")
check("erome: thumbnail from og:image", "cdn.erome.me" in (erm.get("thumbnail") or ""))
check("erome: file_count=3", erm["file_count"] == 3, f"got {erm['file_count']}")
check("erome: has_videos=True", erm["has_videos"] is True)
check("erome: photo_count=2", erm["photo_count"] == 2, f"got {erm['photo_count']}")
check("erome: source=erome", erm["source"] == "erome")
check("erome: id format", erm["id"] == "erome:testid123")

EROME_NO_JS = """
<html>
<head><meta property="og:title" content="Test Album"><title>Test Album - Erome</title></head>
<body>
  <img src="https://cdn.erome.me/pic1.jpg">
  <img src="https://cdn.erome.me/pic2.jpg">
</body>
</html>
"""
erm2 = _parse_album_page(EROME_NO_JS, "fallback123")
check("erome: fallback to img tag count", erm2["file_count"] >= 2, f"got {erm2['file_count']}")

EROME_EMPTY = """<html><body><p>Nothing here</p></body></html>"""
erm3 = _parse_album_page(EROME_EMPTY, "empty123")
check("erome: empty page needs_recheck", erm3["needs_recheck"] is True)


# ═══════════════════════════════════════════════════════════════════════════════
# INDEX / VALIDATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── Index module ──")

from index import is_placeholder, merge_record, commit_guard

check("index: empty title is placeholder", is_placeholder({"title": ""}) is True)
check("index: welcome is placeholder", is_placeholder({"title": "Welcome!"}) is True)
check("index: real title not placeholder", is_placeholder({"title": "Summer Pack"}) is False)

# Merge: never overwrite good title with placeholder
existing = {"id": "x", "title": "Good Title", "file_count": 10, "needs_recheck": False}
incoming = {"id": "x", "title": "Welcome!", "file_count": 5,  "needs_recheck": True}
merged = merge_record(existing, incoming)
check("index: merge keeps good title", merged["title"] == "Good Title")
check("index: merge takes higher file_count", merged["file_count"] == 10)

# Commit guard: blocks on 0 total
check("index: guard blocks on 0 total",  not commit_guard({"total": 0, "placeholder_count": 0}))
check("index: guard allows good meta",   commit_guard({"total": 100, "placeholder_count": 2}))
check("index: guard blocks >5% placeholder",
      not commit_guard({"total": 100, "placeholder_count": 10}))
check("index: force=True bypasses guard",
      commit_guard({"total": 0, "placeholder_count": 99}, force=True))


# ═══════════════════════════════════════════════════════════════════════════════
# RESULTS SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*50}")
passed = sum(1 for s, _, _ in results if s == PASS)
failed = sum(1 for s, _, _ in results if s == FAIL)
print(f"Results: {passed} passed, {failed} failed out of {len(results)} tests")

if failed > 0:
    print("\nFailed tests:")
    for s, name, detail in results:
        if s == FAIL:
            print(f"  {FAIL} {name}  [{detail}]")
    sys.exit(1)
else:
    print(f"\nAll {passed} tests passed ✓")
