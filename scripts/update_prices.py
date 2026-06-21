"""
Runs inside GitHub Actions on a schedule.
Re-fetches all One Piece TCG sets + prices from optcgapi.com and overwrites
the single combined JSON file that index.html reads.
"""

import json
import os
import time

import requests

BASE_URL = "https://optcgapi.com/api"
OUTPUT_PATH = "onepiece-catalog/one_piece_OP01-OP16_with_prices.json"
SET_CODES = [
    "OP-01", "OP-02", "OP-03", "OP-04", "OP-05", "OP-06", "OP-07",
    "OP-08", "OP-09", "OP-10", "OP-11", "OP-12", "OP-13", "OP-16",
]
REQUEST_DELAY_SECONDS = 1.5  # be polite to a free hobbyist-run API


def fetch_set_cards(set_id: str):
    url = f"{BASE_URL}/sets/{set_id}/"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main():
    combined = {}
    for code in SET_CODES:
        try:
            cards = fetch_set_cards(code)
            if cards:
                combined[code] = cards
                print(f"{code}: {len(cards)} cards")
            else:
                print(f"{code}: empty response, skipping")
        except requests.RequestException as exc:
            print(f"{code}: failed ({exc}), keeping previous data for this set")
        time.sleep(REQUEST_DELAY_SECONDS)

    if not combined:
        print("Nothing fetched successfully -- aborting without overwriting file")
        return

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(combined)} sets -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
