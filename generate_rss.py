#!/usr/bin/env python3
"""Generate feed.xml from albums.json"""
import json
from pathlib import Path
from datetime import datetime
from xml.sax.saxutils import escape

# !! UPDATE THIS to your actual GitHub Pages URL !!
SITE_URL = "https://chibimedia.github.io/bunkr-index"

data = json.loads(Path("albums.json").read_text())
albums = data.get("albums", [])[:50]
updated = data.get("meta", {}).get("last_updated", datetime.utcnow().isoformat())

items = []
for a in albums:
    title    = escape(a.get("title") or a.get("id") or "Untitled")
    link     = escape(a.get("url") or f"https://bunkr.si/a/{a.get('id','')}")
    guid     = escape(str(a.get("id", "")))
    count    = a.get("file_count", 0)
    date_raw = a.get("date") or a.get("indexed_at") or updated
    try:
        dt       = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
        pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        pub_date = updated[:25] + " +0000"

    items.append(f"""    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pub_date}</pubDate>
      <description>{escape(f'{count} files')}</description>
    </item>""")

rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>BunkrIndex — Latest Albums</title>
    <link>{SITE_URL}</link>
    <description>Searchable index of public Bunkr albums</description>
    <language>en-us</language>
    <lastBuildDate>{updated[:25]} +0000</lastBuildDate>
    <atom:link href="{SITE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>"""

Path("feed.xml").write_text(rss.strip())
print(f"Generated feed.xml with {len(items)} items")
