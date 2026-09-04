#!/usr/bin/env python3
"""
sgw_search.py — Daily shopgoodwill.com search -> Pinterest-style card gallery

WHAT THIS DOES
--------------
1. Searches shopgoodwill.com for each term in SEARCH_TERMS.
2. Keeps only auctions that haven't ended yet (upcoming/open).
3. Merges results into a running JSON file (sgw_data.json), automatically
   dropping anything that has since ended or disappeared from search.
4. Regenerates gallery.html — a self-contained Pinterest-style card view
   you just double-click to open in a browser. No server needed.

RUNNING IT DAILY
----------------
This script does not run itself — it needs a scheduler:

  Mac/Linux (cron):
    crontab -e
    # add a line to run every day at 7am:
    0 7 * * * /usr/bin/python3 /path/to/sgw_search.py >> /path/to/sgw_log.txt 2>&1

  Windows (Task Scheduler):
    Create Task -> Trigger: Daily, 7:00 AM
    Action: Start a program
      Program: python
      Arguments: "C:/path/to/sgw_search.py"

FIRST RUN / DEBUGGING
----------------------
shopgoodwill.com has no official public API, so the field names below were
reverse-engineered from third-party projects, not tested live from this
environment. Run once with --debug first:

    python3 sgw_search.py --debug

This dumps the raw JSON of the first search result to sgw_raw_sample.json.
If titles/images/prices come through blank in gallery.html, open that file,
find the correct key names, and update the FIELD CANDIDATES section below
(look for get_field(...) calls) — it's a couple of line edits, not a rewrite.

Requires: pip install requests --break-system-packages
"""

import json
import re
import sys
import time
import datetime
import argparse
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SEARCH_TERMS = [
    "1k", "2k", "3k", "4k", "5k", "6k", "7k", "8k", "9k", "0k",
    "Non-standard",
]

API_ROOT = "https://buyerapi.shopgoodwill.com/api"
SEARCH_ENDPOINT = f"{API_ROOT}/Search/ItemListing"
PAGE_SIZE = 40
REQUEST_DELAY_SECONDS = 1.5  # be polite between requests

OUT_DIR = Path(__file__).resolve().parent
DATA_FILE = OUT_DIR / "sgw_data.json"
GALLERY_FILE = OUT_DIR / "gallery.html"
TEMPLATE_FILE = OUT_DIR / "gallery_template.html"
DEBUG_SAMPLE_FILE = OUT_DIR / "sgw_raw_sample.json"

HEADERS = {
    # This exact User-Agent is used by a confirmed-working third-party client;
    # shopgoodwill.com is known to reject requests using the default requests UA.
    "User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64; rv:12.0) Gecko/20100101 Firefox/12.0",
    "Content-Type": "application/json",
}

ITEM_URL_TEMPLATE = "https://shopgoodwill.com/item/{item_id}"


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def get_field(item, *candidates, default=None):
    """Return the first present, non-null value among candidate key names."""
    for key in candidates:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


def parse_end_time(raw):
    """
    shopgoodwill timestamps are typically naive Pacific-time strings like
    '2026-08-25T19:00:00'. Adjust here if --debug shows a different format.
    """
    if not raw:
        return None
    raw = str(raw)
    if "." in raw:
        raw = raw[: raw.find(".")]
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None


def build_image_url(item):
    # Try a few common shapes seen across shopgoodwill front-end/API variants.
    direct = get_field(item, "imageUrl", "imageURL", "thumbnailUrl", "smallImageURL")
    if direct:
        return direct
    images = item.get("images") or item.get("imageList")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return get_field(first, "imageUrl", "url", "path")
    return None


def search_term(session, term, debug=False):
    """Return all upcoming listings matching a single search term."""
    results = []
    page = 1
    while True:
        # IMPORTANT: shopgoodwill's API gives every field except searchText a
        # working default. Earlier versions of this script guessed extra field
        # names (closedAuctions, layout, sortColumn as a string, etc.) that
        # aren't real parameters and appear to have silently zeroed out every
        # search. Keep this body minimal and confirmed.
        body = {
            "searchText": term,
            "page": page,
            "pageSize": PAGE_SIZE,
        }
        resp = session.post(SEARCH_ENDPOINT, json=body, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        payload = resp.json()

        if debug and page == 1 and not DEBUG_SAMPLE_FILE.exists():
            # Write the full response, not a truncated slice — shopgoodwill's
            # category tree alone can run past 20k characters and was eating
            # the whole debug budget before reaching the actual results.
            DEBUG_SAMPLE_FILE.write_text(json.dumps(payload, indent=2))
            print(f"[debug] wrote full raw sample response to {DEBUG_SAMPLE_FILE}")

        # A confirmed-working third-party client treats a missing
        # categoryListModel as the signal of a real error response.
        if payload.get("categoryListModel", None) is None:
            print(f"  ! unexpected/error response for '{term}' (no categoryListModel)")
            break

        search_results = payload.get("searchResults") or {}
        items = search_results.get("items") or []
        if not items:
            break

        for raw_item in items:
            item_id = get_field(raw_item, "itemId", "id")
            end_raw = get_field(raw_item, "endTime", "auctionEndTime", "biddingEndDate")
            end_dt = parse_end_time(end_raw)

            # Only keep auctions that haven't ended yet.
            if end_dt and end_dt < datetime.datetime.now():
                continue

            listing = {
                "id": str(item_id) if item_id else None,
                "title": get_field(raw_item, "title", "itemTitle", default="(untitled)"),
                "current_price": get_field(raw_item, "currentPrice", "startingBid", default=0),
                "buy_now_price": get_field(raw_item, "buyNowPrice"),
                "bid_count": get_field(raw_item, "numberOfBids", "bidCount", default=0),
                "end_time": end_dt.isoformat() if end_dt else None,
                "image": build_image_url(raw_item),
                "seller": get_field(raw_item, "sellerName", "location"),
                "matched_term": term,
                "url": ITEM_URL_TEMPLATE.format(item_id=item_id) if item_id else None,
            }
            if listing["id"] and listing["url"]:
                results.append(listing)

        total_count = search_results.get("itemCount")
        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

        if total_count is not None and len(results) >= total_count:
            break
        if page > 25:  # safety valve
            break

    return results


def run_all_searches(debug=False):
    session = requests.Session()
    all_listings = {}
    for term in SEARCH_TERMS:
        print(f"Searching '{term}'...")
        try:
            term_results = search_term(session, term, debug=debug)
        except Exception as exc:
            print(f"  ! search for '{term}' failed: {exc}")
            continue
        for listing in term_results:
            # de-dupe across terms, but keep track of which terms matched
            existing = all_listings.get(listing["id"])
            if existing:
                if term not in existing["matched_term"]:
                    existing["matched_term"] += f", {term}"
            else:
                all_listings[listing["id"]] = listing
        print(f"  -> {len(term_results)} upcoming listings")
        time.sleep(REQUEST_DELAY_SECONDS)

    return list(all_listings.values())


def load_previous():
    if DATA_FILE.exists():
        try:
            return {item["id"]: item for item in json.loads(DATA_FILE.read_text())}
        except Exception:
            return {}
    return {}


def merge_and_prune(new_listings):
    """
    Merge newly-fetched listings with what we already had, and drop:
      - anything whose end_time has passed
      - anything no longer returned by any search (assume ended/removed)
    """
    now = datetime.datetime.now()
    merged = {item["id"]: item for item in new_listings}

    kept = []
    for item in merged.values():
        end_dt = parse_end_time(item.get("end_time"))
        if end_dt and end_dt < now:
            continue
        kept.append(item)

    kept.sort(key=lambda x: x.get("end_time") or "9999")
    return kept


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="dump raw API response for inspection")
    args = parser.parse_args()

    new_listings = run_all_searches(debug=args.debug)
    final_listings = merge_and_prune(new_listings)

    DATA_FILE.write_text(json.dumps(final_listings, indent=2))
    write_gallery(final_listings)

    print(f"\nDone. {len(final_listings)} upcoming listings across {len(SEARCH_TERMS)} search terms.")
    if not final_listings:
        print("No results — if this is the first run, try `python3 sgw_search.py --debug` "
              "and check sgw_raw_sample.json to confirm field names still match.")


if __name__ == "__main__":
    main()
