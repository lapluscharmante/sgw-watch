#!/usr/bin/env python3
"""
sgw_search.py — Daily ShopGoodwill search via Apify -> Pinterest-style card gallery

WHY THIS VERSION EXISTS
------------------------
ShopGoodwill blocks requests from cloud/datacenter IP addresses (which includes
GitHub Actions runners) at the network level. No amount of fixing headers or
request bodies gets around that. This version instead calls the Apify actor
"getascraper/shopgoodwill-scraper", which runs the actual ShopGoodwill request
through its own residential proxy pool. GitHub Actions talks to Apify's API
(not blocked); Apify talks to ShopGoodwill (using an IP that isn't blocked).

COST
----
This actor is billed per item returned (roughly $2 per 1,000 items as of
setup), not per search term. Because we combine all search terms into a
single request, ShopGoodwill listings matching more than one term are only
counted and billed once, not once per matching term.

SETUP REQUIRED (one-time)
--------------------------
1. Create a free Apify account at apify.com (no card required).
2. Get your API token: profile icon -> Settings -> Integrations -> copy
   "Personal API token".
3. In your GitHub repo: Settings -> Secrets and variables -> Actions ->
   New repository secret. Name it APIFY_TOKEN, paste the token as the value.
   NEVER put the token directly in this file or commit it to the repo.

RUNNING IT DAILY
----------------
Same as before — this script does not schedule itself. It's meant to be
triggered by a GitHub Actions workflow (see .github/workflows/daily.yml),
which reads APIFY_TOKEN from the repo secret and passes it as an
environment variable.

To run it manually on your own machine instead:
    export APIFY_TOKEN=your_token_here      (Mac/Linux)
    set APIFY_TOKEN=your_token_here         (Windows cmd)
    python3 sgw_search.py

Requires: pip install requests --break-system-packages
"""

import os
import json
import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SEARCH_TERMS = [
    "1k", "2k", "3k", "4k", "5k", "6k", "7k", "8k", "9k", "0k",
    "Non-standard",
]

APIFY_ACTOR = "getascraper~shopgoodwill-scraper"
APIFY_ENDPOINT = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")

# Global cap across all 11 terms combined. Raise this later if you're
# routinely hitting the ceiling and missing real listings.
MAX_ITEMS = 1000

OUT_DIR = Path(__file__).resolve().parent
DATA_FILE = OUT_DIR / "sgw_data.json"
GALLERY_FILE = OUT_DIR / "gallery.html"
TEMPLATE_FILE = OUT_DIR / "gallery_template.html"
DEBUG_SAMPLE_FILE = OUT_DIR / "sgw_raw_sample.json"

ITEM_URL_FALLBACK = "https://shopgoodwill.com/item/{item_id}"


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def parse_end_time(raw):
    """Best-effort parse of whatever date format Apify's actor returns."""
    if not raw:
        return None
    raw = str(raw).strip()
    # Try a handful of common shapes before giving up.
    candidates = [raw, raw.replace("Z", "+00:00")]
    for candidate in candidates:
        try:
            return datetime.datetime.fromisoformat(candidate)
        except ValueError:
            pass
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            pass
    print(f"  ! could not parse end time: {raw!r} (keeping item anyway)")
    return None


def run_apify_search(debug=False):
    if not APIFY_TOKEN:
        raise RuntimeError(
            "APIFY_TOKEN is not set. Set it as a GitHub repo secret (see the "
            "comment at the top of this file) or as an environment variable "
            "if running locally."
        )

    body = {
        "searchQueries": SEARCH_TERMS,
        "auctionState": "active",   # only open/upcoming auctions
        "runMode": "snapshot",      # NOT "monitor" -- that mode is billed
                                     # per-alert at a much higher rate and we
                                     # don't need it for a daily snapshot.
        "sortBy": "endingSoonest",
        "maxItems": MAX_ITEMS,
    }

    resp = requests.post(
        APIFY_ENDPOINT,
        params={"token": APIFY_TOKEN},
        json=body,
        timeout=280,  # Apify's sync endpoint has its own ~300s ceiling
    )
    resp.raise_for_status()
    items = resp.json()

    if debug:
        DEBUG_SAMPLE_FILE.write_text(json.dumps(items[:5], indent=2))
        print(f"[debug] wrote first 5 raw items to {DEBUG_SAMPLE_FILE}")

    if not isinstance(items, list):
        raise RuntimeError(
            f"Unexpected response shape from Apify (expected a list): "
            f"{str(items)[:500]}"
        )

    return items


def normalize_items(raw_items):
    """Map Apify's field names onto the shape gallery.html expects."""
    now = datetime.datetime.now(datetime.timezone.utc)
    listings = []

    for item in raw_items:
        item_id = item.get("itemId")
        end_dt = parse_end_time(item.get("endTime"))

        # Keep items with no parseable end time (rare) rather than silently
        # dropping real auctions; only drop ones we're SURE have ended.
        if end_dt is not None:
            compare_now = now if end_dt.tzinfo else datetime.datetime.now()
            if end_dt < compare_now:
                continue

        image = item.get("imageURL")
        if not image:
            images = item.get("images")
            if isinstance(images, list) and images:
                image = images[0]

        url = item.get("listingUrl") or (
            ITEM_URL_FALLBACK.format(item_id=item_id) if item_id else None
        )

        if not item_id or not url:
            continue

        listings.append({
            "id": str(item_id),
            "title": item.get("title") or "(untitled)",
            "current_price": item.get("currentPrice") or 0,
            "buy_now_price": item.get("buyNowPrice"),
            "bid_count": item.get("numBids") or 0,
            "end_time": end_dt.isoformat() if end_dt else None,
            "image": image,
            "seller": item.get("sellerName"),
            "matched_term": "",  # not available when terms are combined
            "url": url,
        })

    listings.sort(key=lambda x: x.get("end_time") or "9999")
    return listings


def write_gallery(listings):
    if not TEMPLATE_FILE.exists():
        print(f"! Template not found at {TEMPLATE_FILE}, skipping gallery.html generation")
        return
    template = TEMPLATE_FILE.read_text()
    injected = template.replace(
        "__LISTINGS_JSON__",
        json.dumps(listings, indent=None) if listings else "[]",
    )
    injected = injected.replace(
        "__GENERATED_AT__",
        datetime.datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    GALLERY_FILE.write_text(injected)
    print(f"Wrote {GALLERY_FILE} ({len(listings)} listings)")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="save a sample of raw Apify output")
    args = parser.parse_args()

    print(f"Searching Apify for {len(SEARCH_TERMS)} terms in one combined request...")
    raw_items = run_apify_search(debug=args.debug)
    print(f"Apify returned {len(raw_items)} raw items")

    listings = normalize_items(raw_items)
    DATA_FILE.write_text(json.dumps(listings, indent=2))
    write_gallery(listings)

    print(f"\nDone. {len(listings)} upcoming listings.")
    if not listings:
        print("No results -- if this is the first run, try `python3 sgw_search.py --debug` "
              "and check sgw_raw_sample.json to confirm field names still match.")


if __name__ == "__main__":
    main()
