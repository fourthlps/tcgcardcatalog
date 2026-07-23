"""
append_jp_history.py
Reads current JP prices from onepiece-catalog/card-markets.json and appends
today's snapshot to onepiece-catalog/data/jp-price-history.json.

Run daily after update_prices.py so JP price history accumulates day-by-day.
After 7+ days the compute_jp_trends.py script can produce gainers/losers.
"""

import json
import os
from datetime import date, datetime, timezone

MARKETS_PATH = "onepiece-catalog/card-markets.json"
HISTORY_PATH = "onepiece-catalog/data/jp-price-history.json"
MAX_SNAPSHOTS = 90  # keep ~3 months; older data is pruned to limit file size

TODAY = date.today().isoformat()


def main():
    print(f"[append_jp_history] date={TODAY}")

    # ── Load card-markets.json ───────────────────────────────────────────────
    print(f"  Loading {MARKETS_PATH}...")
    with open(MARKETS_PATH, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    markets = data.get("markets", {})

    # ── Extract JP prices ────────────────────────────────────────────────────
    today_prices = {}
    for card_id, entries in markets.items():
        for entry in entries:
            if entry.get("source_market") != "JP":
                continue
            # Data model v2 (founder step-6): only publishable LIVE prices may
            # enter published history — OOS, suspect, under-review, unavailable
            # and stale entries are excluded, so they can never reach trends or
            # rankings. Legacy entries without a market_state keep old behavior.
            if entry.get("market_state") not in (None, "live"):
                continue
            jpy = float(entry.get("source_price") or 0)
            thb = float(entry.get("converted_price") or 0)
            if jpy > 0:
                today_prices[card_id] = {"jpy": jpy, "thb": round(thb, 2)}

    print(f"  Extracted {len(today_prices)} JP prices for {TODAY}")

    if not today_prices:
        print("  No JP prices found — skipping append")
        return

    # ── Load existing history ────────────────────────────────────────────────
    history = {"snapshots": []}
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
            print(f"  Loaded existing history ({len(history.get('snapshots', []))} snapshots)")
        except Exception as e:
            print(f"  Warning: could not read history ({e}), starting fresh")

    # ── Append today's snapshot (idempotent: skip if already exists) ─────────
    existing_dates = {s["date"] for s in history.get("snapshots", [])}
    if TODAY in existing_dates:
        print(f"  Snapshot for {TODAY} already exists — skipping (idempotent)")
    else:
        history.setdefault("snapshots", []).append({
            "date": TODAY,
            "prices": today_prices,
        })
        print(f"  Appended snapshot for {TODAY}")

    # ── Prune to MAX_SNAPSHOTS most recent ───────────────────────────────────
    history["snapshots"] = sorted(
        history["snapshots"], key=lambda s: s["date"]
    )[-MAX_SNAPSHOTS:]

    history["last_updated"] = datetime.now(timezone.utc).isoformat()
    history["total_snapshots"] = len(history["snapshots"])

    # ── Save ─────────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))

    print(
        f"  Saved {HISTORY_PATH} "
        f"({history['total_snapshots']} snapshots, "
        f"earliest={history['snapshots'][0]['date']})"
    )


if __name__ == "__main__":
    main()
