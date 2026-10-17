#!/usr/bin/env python3
"""
shelf_watcher.py — YumRare outbound-link and price-drift watcher.

Checks every item's two outbound shopping URLs, exactly as the store
builds them:
  * the seller "Buy direct" link (the item's `link` field — skipped
    when it is a reference page, mirroring build.py's REFERENCE_HOSTS
    guard, or empty), and
  * the eBay search URL (the item's `ebay` field + EBAY_EPN_PARAMS,
    mirroring build.py's ebay_href(); the params are empty until the
    EPN application is approved).

Checks are polite and sequential: browser user-agent, HEAD first with
a GET fallback, a small delay between requests. Seller pages that load
get a HEURISTIC price scan whose candidates are compared against the
pricing-pass baseline CSV — heuristic only, always flagged as
"verify by hand" in the report.

This bot NEVER posts, sends, or submits anything. Its only output is a
Markdown report file (plus a stdout summary).

Environment:
  ITEMS_JSON     Catalog path (default: data/items.json next to script).
  PRICING_CSV    Baseline CSV (default: data/pricing-baseline.csv next
                 to this script — a snapshot of the store's
                 research/pricing-pass-1.csv). Missing file = liveness
                 checks only.
  REPORT_DIR     Report directory (default: reports/ next to script).
  WATCH_LIMIT    If set, only check the first N catalog items
                 (smoke runs).
  WATCH_DELAY    Seconds between requests (default 0.7).
  EBAY_EPN_PARAMS  Appended to eBay URLs (default empty).
"""

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ITEMS = os.path.join(HERE, "data", "items.json")
DEFAULT_CSV = os.path.join(HERE, "data", "pricing-baseline.csv")
SITE = "https://yumrare.com"
# Mirrors build.py: provenance/reference pages, not sellers.
REFERENCE_HOSTS = {"en.wikipedia.org", "world.openfoodfacts.org"}
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*\.\d{2})")


def log(msg):
    print(f"[shelf-watcher] {msg}", flush=True)


def load_items():
    path = os.environ.get("ITEMS_JSON") or DEFAULT_ITEMS
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"] if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = list(items.values())
    return items


def load_baselines():
    path = os.environ.get("PRICING_CSV") or DEFAULT_CSV
    out = {}
    if not os.path.exists(path):
        log(f"baseline CSV not found at {path} — liveness checks only")
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                out[row["slug"]] = {
                    "source_price": float(row["source_price_usd"]),
                    "source_name": row.get("source_name", ""),
                    "source_url": row.get("source_url", ""),
                }
            except (KeyError, TypeError, ValueError):
                continue
    return out


def seller_url(item):
    link = str(item.get("link") or "").strip()
    if not link:
        return None
    if urllib.parse.urlparse(link).netloc.lower() in REFERENCE_HOSTS:
        return None  # reference page, not a seller — build renders no button
    return link


def ebay_url(item):
    base = str(item.get("ebay") or "").strip()
    if not base:
        return None
    return base + os.environ.get("EBAY_EPN_PARAMS", "")


def _request(url, method):
    req = urllib.request.Request(
        url, method=method,
        headers={"User-Agent": BROWSER_UA,
                 "Accept": "text/html,application/xhtml+xml,*/*"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read(262144).decode("utf-8", "ignore") if method == "GET" else ""
        return r.status, body


def fetch(url, want_body=False):
    """Return (status, body_text). status 0 = transport error.
    Cheap HEAD first; a GET follows when a body is wanted, when HEAD is
    refused (bot wall / no HEAD support), or when HEAD fails outright."""
    status = None
    try:
        status, _ = _request(url, "HEAD")
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 405, 501):
            return e.code, ""  # a real answer (404/410/500…) — trust it
        status = e.code  # wall or no-HEAD — try GET for a better answer
    except Exception:  # noqa: BLE001 — transport error; GET may still work
        status = None
    if status is not None and 200 <= status < 400 and not want_body:
        return status, ""
    try:
        return _request(url, "GET")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return (status if status is not None else 0), ""


def classify(status):
    if 200 <= status < 400:
        return "ok"
    if status in (404, 410):
        return "dead"
    if status in (401, 403):
        return "blocked"  # bot wall — NOT evidence the page is gone
    return "error"


def price_scan(body, baseline):
    """Heuristic: collect $ amounts on the page; report the closest one
    to the baseline and whether any price sits within ±30% of it."""
    found = []
    for m in PRICE_RE.finditer(body or ""):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 0.50 <= v <= 200.00:
            found.append(v)
    if not found or baseline is None:
        return None
    closest = min(found, key=lambda v: abs(v - baseline))
    within = any(abs(v - baseline) <= 0.30 * baseline for v in found)
    return {"candidates": len(found), "closest": closest, "within_30pct": within}


def main():
    items = load_items()
    baselines = load_baselines()
    limit = int(os.environ.get("WATCH_LIMIT") or 0)
    delay = float(os.environ.get("WATCH_DELAY") or 0.7)
    if limit:
        items = items[:limit]
        log(f"WATCH_LIMIT={limit} — checking first {limit} items only")

    checks = []  # (slug, kind, url)
    for it in items:
        s = seller_url(it)
        if s:
            checks.append((it["slug"], "seller", s))
        e = ebay_url(it)
        if e:
            checks.append((it["slug"], "ebay", e))
    log(f"{len(items)} items → {len(checks)} URLs to check")

    results = []
    for i, (slug, kind, url) in enumerate(checks, 1):
        status, body = fetch(url, want_body=(kind == "seller"))
        cls = classify(status)
        drift = None
        if kind == "seller" and cls == "ok" and slug in baselines:
            drift = price_scan(body, baselines[slug]["source_price"])
        results.append({"slug": slug, "kind": kind, "url": url,
                        "status": status, "class": cls, "drift": drift})
        if i % 25 == 0:
            log(f"  …{i}/{len(checks)}")
        if delay and i < len(checks):
            time.sleep(delay)

    counts = {}
    for r in results:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    dead = [r for r in results if r["class"] == "dead"]
    blocked = [r for r in results if r["class"] == "blocked"]
    errors = [r for r in results if r["class"] == "error"]
    drifted = [r for r in results
               if r["drift"] and not r["drift"]["within_30pct"]]

    today = date.today().isoformat()
    report_dir = os.environ.get("REPORT_DIR") or os.path.join(HERE, "reports")
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"report-{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Shelf Watcher report — {today}\n\n")
        f.write(f"Items checked: {len(items)} · URLs checked: {len(results)} · "
                f"OK: {counts.get('ok', 0)} · Dead (404/410): {len(dead)} · "
                f"Blocked (bot wall): {len(blocked)} · Errors: {len(errors)} · "
                f"Price-drift suspects: {len(drifted)}\n\n")
        f.write("Blocked means the seller's site refused the automated "
                "check — it is NOT evidence the page is gone; verify "
                "those by hand before changing any link.\n\n")

        def table(title, rows, extra=None):
            if not rows:
                return
            f.write(f"## {title}\n\n")
            f.write("| Item | Kind | Status | URL |\n|---|---|---|---|\n")
            for r in rows:
                f.write(f"| {r['slug']} | {r['kind']} | {r['status']} | {r['url']} |\n")
            if extra:
                f.write(extra)
            f.write("\n")

        table("Dead links — fix or replace", dead)
        table("Errors — worth a second look", errors)
        table("Blocked by bot walls — verify by hand", blocked)
        if drifted:
            f.write("## Price-drift suspects (heuristic — verify by hand)\n\n")
            f.write("| Item | Baseline (pricing pass) | Closest $ on seller page | Seller URL |\n"
                    "|---|---|---|---|\n")
            for r in drifted:
                b = baselines[r["slug"]]
                f.write(f"| {r['slug']} | ${b['source_price']:.2f} "
                        f"({b['source_name']}) | ${r['drift']['closest']:.2f} "
                        f"| {r['url']} |\n")
            f.write("\nThe baseline is the price recorded when the catalog "
                    "was priced; the seller page may list a different "
                    "pack size. Treat every row as a prompt to look, "
                    "never as proof.\n\n")
        f.write("## All checks\n\n")
        f.write("| Item | Kind | Status | Class |\n|---|---|---|---|\n")
        for r in results:
            f.write(f"| {r['slug']} | {r['kind']} | {r['status']} | {r['class']} |\n")

    log(f"report written: {path}")
    log(f"summary: {len(results)} checked · ok {counts.get('ok', 0)} · "
        f"dead {len(dead)} · blocked {len(blocked)} · errors {len(errors)} · "
        f"drift suspects {len(drifted)}")
    for r in dead:
        log(f"  DEAD: {r['slug']} ({r['kind']}) {r['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
