#!/usr/bin/env python3
"""
feed_keeper.py — YumRare Google Merchant Center feed + IndexNow pinger.

Generates a Google Merchant Center product feed (RSS 2.0 with the
g: namespace) from the catalog snapshot (data/items.json) and, when an
IndexNow key is configured, pings IndexNow for the homepage plus any
URLs listed in a changed-URLs file.

Environment:
  ITEMS_JSON        Catalog path (default: data/items.json next to
                    this script).
  FEED_OUTPUT       Where to write the feed (default: ./google-feed.xml,
                    relative to the working directory).
  INDEXNOW_KEY      IndexNow key. If unset, the ping is skipped
                    gracefully. The key file https://yumrare.com/<key>.txt
                    must exist on the site for verification.
  CHANGED_URLS_FILE  Optional file with one URL per line (default:
                    changed_urls.txt next to this script, if present).

Hosting note: this script only WRITES the feed file (the workflow in
this repo commits it back). Merchant Center scheduled-fetch wiring
(pointing MC at the feed's hosted URL with a daily fetch) happens once,
by hand, when the Merchant Center account exists — see README.md.
"""

import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ITEMS = os.path.join(HERE, "data", "items.json")
DEFAULT_CHANGED = os.path.join(HERE, "changed_urls.txt")
SITE = "https://yumrare.com"
G_NS = "http://base.google.com/ns/1.0"
UA = "yumrare-bots/1.0 (feed-keeper; +https://yumrare.com)"

ET.register_namespace("g", G_NS)


def log(msg):
    print(f"[feed-keeper] {msg}", flush=True)


def g(tag):
    return f"{{{G_NS}}}{tag}"


def load_items():
    path = os.environ.get("ITEMS_JSON") or DEFAULT_ITEMS
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"] if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = list(items.values())
    return items


def build_feed(items):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "YumRare"
    ET.SubElement(channel, "link").text = f"{SITE}/"
    ET.SubElement(channel, "description").text = "Rare snacks. Found."
    for it in items:
        maker = it.get("maker") or it.get("parent") or ""
        desc = (it.get("bio") or it.get("blurb") or it.get("hook") or "").strip()
        desc = " ".join(desc.split())  # normalize whitespace
        photo = it.get("photo") or it["slug"]
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, g("id")).text = it["slug"]
        ET.SubElement(item, "title").text = f"{it['name']} — {maker}" if maker else it["name"]
        ET.SubElement(item, "description").text = desc
        ET.SubElement(item, "link").text = f"{SITE}/item-{it['slug']}.html"
        ET.SubElement(item, g("image_link")).text = f"{SITE}/photos/{photo}.jpg"
        ET.SubElement(item, g("availability")).text = "in_stock"
        ET.SubElement(item, g("price")).text = f"{float(it['price']):.2f} USD"
        if maker:
            ET.SubElement(item, g("brand")).text = maker
        ET.SubElement(item, g("condition")).text = "new"
    return rss


def ping_indexnow(urls):
    key = os.environ.get("INDEXNOW_KEY")
    if not key:
        log("INDEXNOW_KEY not set — skipping IndexNow ping")
        return
    payload = {
        "host": "yumrare.com",
        "key": key,
        "keyLocation": f"{SITE}/{key}.txt",
        "urlList": urls,
    }
    req = urllib.request.Request(
        "https://api.indexnow.org/indexnow",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"IndexNow ping: HTTP {r.status} for {len(urls)} URL(s)")
    except Exception as e:  # noqa: BLE001 — a failed ping never fails the feed
        log(f"IndexNow ping failed (non-fatal): {e}")


def main():
    items = load_items()
    if not items:
        log("catalog is empty — refusing to write an empty feed")
        return 1
    out_path = os.environ.get("FEED_OUTPUT") or "./google-feed.xml"
    tree = ET.ElementTree(build_feed(items))
    ET.indent(tree, space="  ")
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    size = os.path.getsize(out_path)
    log(f"wrote {out_path}: {len(items)} products, {size} bytes")

    urls = [f"{SITE}/"]
    changed_file = os.environ.get("CHANGED_URLS_FILE") or DEFAULT_CHANGED
    if os.path.exists(changed_file):
        with open(changed_file, encoding="utf-8") as f:
            extra = [ln.strip() for ln in f if ln.strip()]
        urls.extend(extra)
        log(f"changed-URLs file: {len(extra)} extra URL(s) for IndexNow")
    ping_indexnow(urls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
