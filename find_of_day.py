#!/usr/bin/env python3
"""
find_of_day.py — YumRare "Find of the Day" poster bot.

Picks the next item from the catalog (data/items.json) in a deterministic
rotation and posts it, photo + caption, to Bluesky and Tumblr.

Reliability design (mirrors the proven 90s Foodie crossposter):
  * Per-platform state in rotation_state.json: a platform is marked as
    having an item ONLY after its post succeeds, so a partial failure
    (Bluesky OK, Tumblr down) never duplicates a post on retry — the
    next run retries just the missing platform.
  * The rotation advances (last_slug moves) only when every CONFIGURED
    platform has the item.
  * State is written by this script; the GitHub Actions workflow commits
    it back to the repo.
  * DRY_RUN: set env DRY_RUN=1, or simply run without secrets — a
    platform whose secrets are absent is "not configured" and is
    skipped; with zero configured platforms the bot composes the post,
    logs it, and exits 0 WITHOUT touching state.

Environment:
  DRY_RUN            "1"/"true"/"yes" forces dry-run (nothing posts,
                     nothing is written).
  ITEMS_JSON         Path to the catalog (default: data/items.json
                     next to this script — a snapshot of the store's
                     site/data/items.json; refresh it when the
                     catalog changes).
  PHOTOS_DIR         Optional local photo dir holding <photo>.jpg
                     files. If a photo is not found there it is
                     downloaded from PHOTO_BASE_URL.
  PHOTO_BASE_URL     Default https://yumrare.com/photos
  BSKY_HANDLE        Bluesky handle (e.g. yumrare.bsky.social or
                     yumrare.com once the domain handle is set).
  BSKY_APP_PASSWORD  Bluesky app password (NOT the account password).
  TUMBLR_CONSUMER_KEY, TUMBLR_CONSUMER_SECRET,
  TUMBLR_TOKEN, TUMBLR_TOKEN_SECRET   Tumblr OAuth1 credentials.
  TUMBLR_BLOG        Tumblr blog identifier (e.g. yumrare).

Bluesky posting is pure stdlib (AT Protocol XRPC over urllib).
Tumblr posting needs `requests` + `requests-oauthlib` (OAuth1 multipart
signing); they are imported lazily inside post_tumblr() so this script
runs — compose/dry-run included — without them installed.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "rotation_state.json")
DEFAULT_ITEMS = os.path.join(HERE, "data", "items.json")
SITE = "https://yumrare.com"
BLUESKY_LIMIT = 300  # graphemes; we count chars, close enough with a margin

UA = "yumrare-bots/1.0 (find-of-day; +https://yumrare.com)"


def log(msg):
    print(f"[find-of-day] {msg}", flush=True)


def truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def load_items():
    path = os.environ.get("ITEMS_JSON") or DEFAULT_ITEMS
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"] if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = list(items.values())
    return items


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("last_slug", None)
    state.setdefault("posted", {"bluesky": {}, "tumblr": {}})
    state.setdefault("history", [])
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def item_url(item):
    return f"{SITE}/item-{item['slug']}.html"


def price_str(item):
    return f"${float(item['price']):.2f}"


def compose(item):
    """Compose the post text, trimmed to fit Bluesky's 300-char limit."""
    hook = (item.get("hook") or "").strip()
    if not hook:
        bio = (item.get("bio") or item.get("blurb") or "").strip()
        hook = bio.split(". ")[0].rstrip(".") + "." if bio else ""
    url = item_url(item)
    # Dee's rule (2026-10-21): the bare domain yumrare.com appears in
    # every post as its own closing signature line, not only inside
    # the item URL.
    tail = f" {price_str(item)} + $3.95 shipping, free at $50+. {url}\nyumrare.com"
    head = f"Find of the day: {item['name']} — "
    text = head + hook + tail
    if len(text) > BLUESKY_LIMIT:
        room = BLUESKY_LIMIT - len(head) - len(tail) - 1  # 1 for the ellipsis
        hook = hook[: max(room, 0)].rstrip(" ,;—-") + "…"
        text = head + hook + tail
    return text


def photo_bytes(item):
    """Local photo if present, else download from the live site."""
    photo = item.get("photo") or item["slug"]
    local_dir = os.environ.get("PHOTOS_DIR")
    if local_dir:
        p = os.path.join(local_dir, f"{photo}.jpg")
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read(), "image/jpeg"
    base = os.environ.get("PHOTO_BASE_URL", f"{SITE}/photos").rstrip("/")
    url = f"{base}/{photo}.jpg"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), r.headers.get_content_type() or "image/jpeg"


# ---------------------------------------------------------------- Bluesky

def _xrpc(method, url, token=None, data=None, content_type="application/json"):
    body = None
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        if isinstance(data, (bytes, bytearray)):
            body = bytes(data)
        else:
            body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def post_bluesky(item, text):
    handle = os.environ["BSKY_HANDLE"]
    session = _xrpc(
        "POST",
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        data={"identifier": handle, "password": os.environ["BSKY_APP_PASSWORD"]},
    )
    token, did = session["accessJwt"], session["did"]
    img, mime = photo_bytes(item)
    blob = _xrpc(
        "POST",
        "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
        token=token, data=img, content_type=mime,
    )["blob"]
    # Link facets: byte offsets into the UTF-8 encoding of the text.
    url = item_url(item)
    idx = text.rindex(url)
    bstart = len(text[:idx].encode("utf-8"))
    bend = bstart + len(url.encode("utf-8"))
    facets = [{
        "index": {"byteStart": bstart, "byteEnd": bend},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
    }]
    # Signature facet: the closing "yumrare.com" line links to the
    # storefront. rindex finds the signature (the item URL contains
    # the domain too, but earlier in the text).
    sig = "yumrare.com"
    sidx = text.rindex(sig)
    sb = len(text[:sidx].encode("utf-8"))
    facets.append({
        "index": {"byteStart": sb, "byteEnd": sb + len(sig.encode("utf-8"))},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": SITE}],
    })
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "langs": ["en"],
        "facets": facets,
        "embed": {
            "$type": "app.bsky.embed.images",
            "images": [{"alt": item["name"], "image": blob}],
        },
    }
    out = _xrpc(
        "POST",
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        token=token,
        data={"repo": did, "collection": "app.bsky.feed.post", "record": record},
    )
    return out.get("uri", "")


# ----------------------------------------------------------------- Tumblr

def post_tumblr(item, text):
    try:
        import requests
        from requests_oauthlib import OAuth1
    except ImportError as e:
        raise RuntimeError(
            "Tumblr posting needs `requests` and `requests-oauthlib` "
            "(see requirements.txt)"
        ) from e
    blog = os.environ["TUMBLR_BLOG"]
    auth = OAuth1(
        os.environ["TUMBLR_CONSUMER_KEY"],
        os.environ["TUMBLR_CONSUMER_SECRET"],
        os.environ["TUMBLR_TOKEN"],
        os.environ["TUMBLR_TOKEN_SECRET"],
    )
    img, _mime = photo_bytes(item)
    url = item_url(item)
    caption = f'{text}<br><br><a href="{url}">{url}</a>'
    tags = ",".join(["yumrare", "rare snacks", item.get("aisle", ""), "find of the day"])
    r = requests.post(
        f"https://api.tumblr.com/v2/blog/{blog}/post",
        auth=auth,
        data={"type": "photo", "caption": caption, "tags": tags},
        files={"data": ("photo.jpg", img, "image/jpeg")},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Tumblr post failed: HTTP {r.status_code} {r.text[:200]}")
    return str(r.json().get("response", {}).get("id", ""))


# ------------------------------------------------------------------- main

PLATFORMS = {
    "bluesky": (("BSKY_HANDLE", "BSKY_APP_PASSWORD"), post_bluesky),
    "tumblr": (
        ("TUMBLR_CONSUMER_KEY", "TUMBLR_CONSUMER_SECRET",
         "TUMBLR_TOKEN", "TUMBLR_TOKEN_SECRET", "TUMBLR_BLOG"),
        post_tumblr,
    ),
}


def main():
    dry_run = truthy(os.environ.get("DRY_RUN"))
    items = load_items()
    if not items:
        log("catalog is empty — nothing to do")
        return 1
    slugs = [it["slug"] for it in items]
    state = load_state()

    # Rotation target: the item after last_slug (wraps). If a previous
    # run partially posted it, it is still the target until complete.
    if state["last_slug"] in slugs:
        target = items[(slugs.index(state["last_slug"]) + 1) % len(items)]
    else:
        target = items[0]
    text = compose(target)

    configured = [
        name for name, (keys, _fn) in PLATFORMS.items()
        if all(os.environ.get(k) for k in keys)
    ]
    if dry_run:
        log("DRY_RUN is on — composing only, nothing will post")
    if not configured:
        log("no platform secrets present — dry-run mode")
    log(f"target: {target['slug']} ({target['name']})")
    log(f"post ({len(text)} chars): {text}")
    log(f"photo: {os.environ.get('PHOTO_BASE_URL', SITE + '/photos')}/"
        f"{target.get('photo') or target['slug']}.jpg")

    if dry_run or not configured:
        for name in PLATFORMS:
            have = name in configured
            done = target["slug"] in state["posted"].get(name, {})
            log(f"  {name}: {'would post' if have else 'not configured'}"
                f"{' (already posted)' if done else ''}")
        log("dry-run complete; state untouched")
        return 0

    failures = 0
    for name in configured:
        if target["slug"] in state["posted"].get(name, {}):
            log(f"  {name}: already posted {target['slug']} — skipping (no duplicate)")
            continue
        try:
            ref = PLATFORMS[name][1](target, text)
            state["posted"].setdefault(name, {})[target["slug"]] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "ref": ref,
            }
            save_state(state)  # persist each success immediately
            log(f"  {name}: posted OK ({ref})")
        except Exception as e:  # noqa: BLE001 — report and move on
            failures += 1
            log(f"  {name}: FAILED — {e}")

    if failures == 0:
        state["last_slug"] = target["slug"]
        state["history"].append({
            "slug": target["slug"],
            "at": datetime.now(timezone.utc).isoformat(),
            "platforms": configured,
        })
        save_state(state)
        log(f"rotation advanced: last_slug = {target['slug']}")
        return 0
    log(f"{failures} platform(s) failed; rotation NOT advanced — "
        "next run retries only the missing platform(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
