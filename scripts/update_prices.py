"""
Runs inside GitHub Actions on a schedule (or via manual workflow_dispatch).
Re-fetches all One Piece TCG data from optcgapi.com -- main numbered booster
sets, Extra Boosters, Premium Boosters, and Starter Decks -- and overwrites
the single combined JSON file that index.html reads.
"""

import json
import os
import time

import requests

BASE_URL = "https://optcgapi.com/api"
OUTPUT_PATH = "onepiece-catalog/one_piece_OP01-OP16_with_prices.json"

# Main numbered booster sets (OP-01 .. OP-16, skipping OP-14/OP-15 which were
# never released as standalone boosters -- see EXTRA_SET_CODES below).
SET_CODES = [
    "OP-01", "OP-02", "OP-03", "OP-04", "OP-05", "OP-06", "OP-07",
    "OP-08", "OP-09", "OP-10", "OP-11", "OP-12", "OP-13", "OP-16",
]

# Extra Boosters + Premium Boosters -- same /sets/{id}/ endpoint as above.
#
# KNOWN GAP (2026-06-24): optcgapi.com has no data under EB-04 / EB04 / EB-4
# for "Extra Booster -EGGHEAD CRISIS-" (officially released 2026-01-31). It's
# also absent from /api/allSets/. This is a hobbyist API that simply hasn't
# added that set yet -- not something we can fix on our end without a second
# data source (apitcg.com has a One Piece endpoint but requires a free API
# key signup -- worth doing if this gap still matters once the rest of the
# catalog is fixed). Until then, EB-04 cards/prices will be genuinely missing
# from the site, same as any future not-yet-supported set.
EXTRA_SET_CODES = [
    "EB-01",        # Extra Booster: Memorial Collection
    "EB-02",        # Extra Booster: Anime 25th Collection
    "EB-03",        # Extra Booster: One Piece Heroines Edition
    "OP14-EB04",    # The Azure Sea's Seven -- a real main BOOSTER PACK (OP-14)
                     # on the official site; optcgapi just files it under this
                     # internal code. Confirmed against asia-th.onepiece-cardgame.com.
    "OP15-EB04",    # Adventure on Kami's Island -- real main BOOSTER PACK (OP-15),
                     # same optcgapi naming quirk as above.
    "PRB-01",       # Premium Booster - The Best
    "PRB-02",       # Premium Booster - The Best - Vol. 2
]

# Starter Decks -- separate /decks/{id}/ endpoint. ST-01 through ST-30 cover
# every deck released as of 2026-06-24 (ST-29 "Egghead" and ST-30 EX "Luffy &
# Ace" confirmed live on both optcgapi and the official site). ST-31 through
# ST-36 exist as announced products (release 2026-07-11) but optcgapi has no
# card data for them yet -- they're listed as "coming soon" in index.html
# instead of being fetched here.
DECK_CODES = [f"ST-{i:02d}" for i in range(1, 31)]

REQUEST_DELAY_SECONDS = 1.5  # be polite to a free hobbyist-run API

# optcgapi returns the literal string "NULL" for empty fields instead of a
# real JSON null. Left as-is, the site renders the text "NULL" on cards that
# have no power/life/text/etc. Normalize it here so downstream data is clean.
NULL_LIKE = {"NULL", "null", ""}


def clean_card(card: dict) -> dict:
    return {
        k: (None if isinstance(v, str) and v.strip() in NULL_LIKE else v)
        for k, v in card.items()
    }


def fetch_set_cards(set_id: str):
    url = f"{BASE_URL}/sets/{set_id}/"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_deck_cards(deck_id: str):
    url = f"{BASE_URL}/decks/{deck_id}/"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main():
    combined = {}

    for code in SET_CODES + EXTRA_SET_CODES:
        try:
            cards = fetch_set_cards(code)
            if cards:
                combined[code] = [clean_card(c) for c in cards]
                print(f"{code}: {len(cards)} cards")
            else:
                print(f"{code}: empty response, skipping")
        except requests.RequestException as exc:
            print(f"{code}: failed ({exc}), keeping previous data for this set")
        time.sleep(REQUEST_DELAY_SECONDS)

    for code in DECK_CODES:
        try:
            cards = fetch_deck_cards(code)
            if cards:
                combined[code] = [clean_card(c) for c in cards]
                print(f"{code}: {len(cards)} cards")
            else:
                print(f"{code}: empty response, skipping")
        except requests.RequestException as exc:
            print(f"{code}: failed ({exc}), keeping previous data for this deck")
        time.sleep(REQUEST_DELAY_SECONDS)

    if not combined:
        print("Nothing fetched successfully -- aborting without overwriting file")
        return

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(combined)} sets/decks -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
