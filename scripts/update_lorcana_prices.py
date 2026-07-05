"""Daily Lorcana price refresh — INDEPENDENT of the One Piece pipeline.

Will be scheduled by .github/workflows/lorcana-market.yml (NOT yet enabled —
see docs/multi-game-architecture.md §10). A Lorcana failure must never fail
One Piece and vice versa, hence the separate workflow.

Source: tcgcsv.com daily mirror of TCGplayer prices (category 71).
Join:   LorcanaJSON externalLinks.tcgPlayerId == TCGplayer productId (1:1).
Writes: data/lorcana/prices.json   (card-markets shape, finish-labelled)
        data/lorcana/products.json (market_price_* fields refreshed in place)

Run from the repo's onepiece-catalog/ folder:  python scripts/update_lorcana_prices.py
"""
import json
import os
import time
from datetime import date

import requests

CATEGORY = 71  # Disney Lorcana on TCGplayer
CARDS_PATH = "data/lorcana/cards.json"
PRICES_PATH = "data/lorcana/prices.json"
PRODUCTS_PATH = "data/lorcana/products.json"
DELAY = 1.5  # polite rate limit, same as the One Piece pipeline
USD_TO_THB_FALLBACK = 33.26

FINISH = {"Normal": "normal", "Cold Foil": "cold-foil", "Holofoil": "holofoil"}


def fetch_json(url):
    r = requests.get(url, timeout=30, headers={"User-Agent": "VoyageLog/1.0"})
    r.raise_for_status()
    return r.json()


def main():
    today = date.today().isoformat()
    with open(CARDS_PATH, encoding="utf-8") as f:
        cards_by_set = json.load(f)

    groups = fetch_json(f"https://tcgcsv.com/tcgplayer/{CATEGORY}/groups")["results"]
    price_by_product = {}
    for g in groups:
        try:
            for rec in fetch_json(f"https://tcgcsv.com/tcgplayer/{CATEGORY}/{g['groupId']}/prices")["results"]:
                price_by_product.setdefault(rec["productId"], []).append(rec)
        except Exception as e:  # one group failing must not kill the run
            print(f"WARN group {g['groupId']} ({g.get('name')}): {e}")
        time.sleep(DELAY)

    if not price_by_product:
        print("ABORT: nothing fetched — keeping previous prices file")
        return

    markets = {}
    n_records = 0
    for cards in cards_by_set.values():
        for c in cards:
            pid = c.get("tcgplayer_id")
            if not pid:
                continue
            recs = []
            for p in price_by_product.get(pid, []):
                if p.get("marketPrice") is None:
                    continue
                recs.append({
                    "source_market": "EN",
                    "source_name": "TCGPlayer",
                    "source_currency": "USD",
                    "source_price": p["marketPrice"],
                    "low_price": p.get("lowPrice"),
                    "mid_price": p.get("midPrice"),
                    "high_price": p.get("highPrice"),
                    "finish": FINISH.get(p.get("subTypeName"), "normal"),
                    "last_updated": today,
                    "price_type": "Market Price",
                    "confidence": "High",
                })
            if recs:
                markets[c["card_image_id"]] = recs
                n_records += len(recs)

    out = {
        "_meta": {
            "generated": today,
            "game": "lorcana",
            "fx": {"usd_to_thb": USD_TO_THB_FALLBACK,
                   "fx_note": "THB values are estimates, not verified Thai market prices."},
            "source": "TCGplayer market data mirrored by tcgcsv.com (daily refresh)",
            "join_method": "LorcanaJSON externalLinks.tcgPlayerId == TCGplayer productId (exact 1:1)",
            "en_entries": n_records,
            "cards_with_price": len(markets),
            "note": "Keyed by card_image_id. EN market only. Finishes: normal | cold-foil | holofoil.",
        },
        "markets": markets,
    }
    with open(PRICES_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Saved {len(markets)} priced cards / {n_records} records -> {PRICES_PATH}")

    # Refresh sealed-product market prices in place (identity fields untouched)
    with open(PRODUCTS_PATH, encoding="utf-8") as f:
        products_file = json.load(f)
    updated = 0
    for p in products_file.get("products", []):
        recs = [r for r in price_by_product.get(p.get("tcgplayer_product_id"), []) if r.get("marketPrice")]
        if recs:
            r = recs[0]
            p["market_price_usd"] = r["marketPrice"]
            p["market_price_low_usd"] = r.get("lowPrice")
            p["market_price_high_usd"] = r.get("highPrice")
            p["market_price_updated"] = today
            updated += 1
    products_file["last_updated"] = today
    with open(PRODUCTS_PATH, "w", encoding="utf-8") as f:
        json.dump(products_file, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Refreshed market prices on {updated} products -> {PRODUCTS_PATH}")


if __name__ == "__main__":
    main()
