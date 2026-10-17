# YumRare Bots

Automation for [YumRare](https://yumrare.com) — a rare-snacks store.
Three small Python bots, each with its own GitHub Actions workflow.

> **Separation note:** this repo belongs to the **yumrare** GitHub
> account (repo `yumrare/yumrare-bots`) and is completely separate from
> the 90s Foodie crossposter — different account, different social
> accounts, different secrets, different state. Nothing is shared, by
> the owner's standing rule ("don't mix").

## The bots

### 1. Find of the Day — `find_of_day.py`
Posts one catalog find per day, photo + caption, to Bluesky and
Tumblr. The catalog is the content: 100 finds rotate deterministically
in `data/items.json` order, so the store gets ~100 days of posts per
cycle with no new writing.

Post format:
`Find of the day: <Name> — <short why> $<price> + $3.95 shipping, free at $50+. <item URL>`
(trimmed to Bluesky's 300 characters; the URL rides as a proper link
facet and the product photo is embedded).

Reliability, borrowed from the crossposter fleet's hard lessons:
- **Per-platform state** in `rotation_state.json`: a platform is
  marked only after its post succeeds. If Bluesky posts and Tumblr
  fails, the rotation does **not** advance; the next run retries only
  Tumblr. No duplicates, ever.
- State is committed back by the workflow even on failure
  (`if: always()`), so partial progress is never lost.

### 2. Feed Keeper — `feed_keeper.py`
Regenerates the **Google Merchant Center product feed**
(`google-feed.xml`, RSS 2.0 with the `g:` namespace) from the catalog:
id, title ("Name — Maker"), description, link, image link,
availability (in_stock), price (USD), brand, condition (new). It also
pings **IndexNow** for the homepage plus any URLs in
`changed_urls.txt` (one per line) when `INDEXNOW_KEY` is set; without
the key the ping is skipped quietly.

> **Merchant Center wiring (one-time, manual):** the script only
> writes the feed file and this repo's workflow commits it back.
> Once the Merchant Center account exists, point MC's scheduled fetch
> at the feed's hosted URL (daily fetch) and enter the store's
> shipping as $3.95 flat / free at $50+. Until then the feed is
> generated and versioned here, ready to plug in.

### 3. Shelf Watcher — `shelf_watcher.py`
Checks every outbound shopping URL exactly as the store builds it:
each item's seller "Buy direct" link (reference pages like Wikipedia
are skipped, mirroring the build's guard) and its eBay search URL.
Polite and sequential (browser UA, HEAD-then-GET, ~0.7s apart).
Seller pages get a heuristic price scan compared with the pricing
baseline (`data/pricing-baseline.csv`); drift suspects are flagged
**for human verification**, never treated as proof. Blocked (403)
results mean "bot wall", not "dead link".

Output is a Markdown report in `reports/report-<date>.md`, committed
back by its workflow. **This bot never posts or sends anything.**

## Data

`data/items.json` and `data/pricing-baseline.csv` are snapshots of the
store's catalog and pricing pass. Refresh them when the catalog
changes (the store's source of truth lives in the site build). Photos
are fetched from the live site (`https://yumrare.com/photos/<photo>.jpg`)
unless `PHOTOS_DIR` points at a local photo folder.

## Secrets

Secrets come ONLY from environment variables (GitHub Actions secrets
in production). Nothing is hardcoded; nothing is committed.

| Variable | Bot | Notes |
|---|---|---|
| `BSKY_HANDLE` | Find of the Day | Bluesky handle for the YumRare account |
| `BSKY_APP_PASSWORD` | Find of the Day | Bluesky **app password**, not the login password |
| `TUMBLR_CONSUMER_KEY` | Find of the Day | Tumblr API app credentials |
| `TUMBLR_CONSUMER_SECRET` | Find of the Day | |
| `TUMBLR_TOKEN` | Find of the Day | OAuth1 access token for the YumRare blog |
| `TUMBLR_TOKEN_SECRET` | Find of the Day | |
| `TUMBLR_BLOG` | Find of the Day | Blog identifier (e.g. `yumrare`) |
| `INDEXNOW_KEY` | Feed Keeper | Optional; ping skipped when unset |
| `DRY_RUN` | Find of the Day | Repo variable; `1` parks the bot in compose-only mode |

Other knobs (not secrets): `ITEMS_JSON`, `FEED_OUTPUT`, `PHOTOS_DIR`,
`PHOTO_BASE_URL`, `CHANGED_URLS_FILE`, `PRICING_CSV`, `REPORT_DIR`,
`WATCH_LIMIT`, `WATCH_DELAY`, `EBAY_EPN_PARAMS`.

## Schedules (GitHub Actions, UTC)

| Workflow | Cron (UTC) | ≈ US/Central |
|---|---|---|
| Feed Keeper | 13:30 daily | 8:30 CDT / 7:30 CST |
| Find of the Day | 14:00 daily | 9:00 CDT / 8:00 CST |
| Shelf Watcher | 11:00 daily | 6:00 CDT / 5:00 CST |

GitHub cron is UTC-only, so Central times drift one hour in standard
time — accepted rather than edited twice a year.

## Dry runs

```bash
# Compose today's post, post nothing, touch no state:
DRY_RUN=1 python3 find_of_day.py
# (Running with no secrets at all does the same thing automatically.)

# Generate the feed locally:
FEED_OUTPUT=/tmp/google-feed.xml python3 feed_keeper.py

# Watcher smoke run — first 5 items only:
WATCH_LIMIT=5 python3 shelf_watcher.py
```

## Dependencies

Python 3.12, standard library for everything except Tumblr posting,
which uses `requests` + `requests-oauthlib` (see `requirements.txt`).
They're imported lazily — all dry-run and compose paths work without
them installed.
