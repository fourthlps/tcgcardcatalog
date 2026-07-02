"""
fetch_markets_jp.py
Fetches JP card prices from jp-card-prices.com for sets not covered by
the main Yuyu-Tei pipeline (EB04 and ST01-ST30) and merges them into
card-markets.json.

Source: Yuyu-Tei retail prices aggregated by jp-card-prices.com
URL:    https://jp-card-prices.com/en/onepiece/sets/{code}.html

Run this manually after update_prices.py whenever new sets need JP prices.
"""

import json
import re
import time
import sys
from datetime import date

import requests
from bs4 import BeautifulSoup

MARKETS_PATH = "card-markets.json"
BASE_URL = "https://jp-card-prices.com/en/onepiece/sets/{code}.html"

# FX rate — kept in sync with card-markets.json meta.fx.jpy_to_thb
# Update this when you regenerate card-markets.json with a fresh FX pull.
JPY_TO_THB = 0.205477

REQUEST_DELAY = 2.0  # seconds between requests — be polite
TODAY = date.today().isoformat()

# Sets to fetch.
# OP01-OP16 + EB01-EB03 are already covered by Yuyu-Tei in update_prices.py.
# This script fills the gaps: EB04 and all starter decks ST01-ST30.
JP_FETCH_SETS = (
    ["eb04"]
    + [f"st{i:02d}" for i in range(1, 31)]
)


def set_code_to_prefix(code: str) -> str:
    """'eb04' -> 'EB04', 'st01' -> 'ST01'"""
    return code.upper()


def card_image_id(card_number: str, rarity: str) -> str:
    """Parallel cards (rarity starts with 'P-') get a _p1 suffix.
    Base cards use the card number directly.
    """
    if rarity.upper().startswith("P-"):
        return f"{card_number}_p1"
    return card_number


def parse_price_jpy(text: str) -> float:
    """'¥19,800' or '19,800' -> 19800.0; returns 0 on failure."""
    cleaned = re.sub(r"[¥,\s]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def fetch_set(code: str) -> list:
    """Scrape one set page; return list of dicts with card_image_id + price."""
    url = BASE_URL.format(code=code)
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TCGBot/1.0)"},
        )
        if resp.status_code == 404:
            print(f"  {code}: 404 — set not found on jp-card-prices.com, skipping")
            return []
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  {code}: network error ({exc}), skipping")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    prefix = set_code_to_prefix(code)
    entries = []

    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        card_num = cells[0].get_text(strip=True)
        rarity = cells[2].get_text(strip=True)
        price_text = cells[3].get_text(strip=True)

        # Skip rows that are not for this set
        if not card_num.upper().startswith(prefix):
            continue

        price_jpy = parse_price_jpy(price_text)
        if price_jpy <= 0:
            continue

        cid = card_image_id(card_num, rarity)
        converted_thb = round(price_jpy * JPY_TO_THB, 2)

        entries.append({
            "card_image_id": cid,
            "source_price": price_jpy,
            "converted_price": converted_thb,
        })

    print(f"  {code}: {len(entries)} entries found")
    return entries


def build_market_entry(source_price: float, converted_price: float) -> dict:
    return {
        "source_market": "JP",
        "source_name": "Yuyu-Tei",
        "source_currency": "JPY",
        "source_price": source_price,
        "converted_currency": "THB",
        "converted_price": converted_price,
        "conversion_rate_used": JPY_TO_THB,
        "last_updated": TODAY,
        "price_type": "Retail Reference",
        "confidence": "Medium",
        "note": "JP retail reference price via jp-card-prices.com (Yuyu-Tei). Not Thai market price.",
    }


def main():
    print(f"Loading {MARKETS_PATH}...")
    with open(MARKETS_PATH, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    markets = data["markets"]
    added_new = 0    # card_image_id not in markets at all
    added_jp = 0     # card_image_id existed but had no JP entry
    skipped = 0      # JP entry already present

    for code in JP_FETCH_SETS:
        print(f"Fetching {code}...")
        entries = fetch_set(code)
        time.sleep(REQUEST_DELAY)

        for e in entries:
            cid = e["card_image_id"]
            new_entry = build_market_entry(e["source_price"], e["converted_price"])

            if cid not in markets:
                markets[cid] = [new_entry]
                added_new += 1
            else:
                existing_jp = [
                    x for x in markets[cid] if x.get("source_market") == "JP"
                ]
                if not existing_jp:
                    markets[cid].append(new_entry)
                    added_jp += 1
                else:
                    skipped += 1

    # Recalculate meta counts
    jp_total = sum(
        1
        for entries in markets.values()
        if any(x.get("source_market") == "JP" for x in entries)
    )
    if "_meta" in data:
        data["_meta"]["jp_entries"] = jp_total

    print(f"\nSaving {MARKETS_PATH}...")
    with open(MARKETS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    print(
        f"\nDone.\n"
        f"  Added to new card IDs:      {added_new}\n"
        f"  Added JP to existing cards: {added_jp}\n"
        f"  Skipped (JP already exists):{skipped}\n"
        f"  Total JP entries now:       {jp_total}"
    )


if __name__ == "__main__":
    main()
