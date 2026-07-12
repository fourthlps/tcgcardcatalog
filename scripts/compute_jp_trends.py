"""
compute_jp_trends.py
Reads jp-price-history.json and computes 7-day price gainers/losers for JP cards.
Writes jp-top-gainers.json and jp-top-losers.json to onepiece-catalog/data/.

Requires at least 2 snapshots to run; confidence rises to "medium" at 7+ days.
Card names and images are resolved from the main cards JSON.
"""

import json
import os
from datetime import date, datetime, timezone

MARKETS_PATH   = "onepiece-catalog/card-markets.json"
HISTORY_PATH   = "onepiece-catalog/data/jp-price-history.json"
CARDS_PATH     = "onepiece-catalog/one_piece_OP01-OP16_with_prices.json"
GAINERS_PATH   = "onepiece-catalog/data/jp-top-gainers.json"
LOSERS_PATH    = "onepiece-catalog/data/jp-top-losers.json"

# DATA-QUALITY filters (affect which cards are eligible at all):
TREND_MIN_JPY      = 500    # ignore bulk commons (< ¥500)
TREND_MAX_PCT_CAP  = 500.0  # > 500 % is treated as bad data
# SIGNIFICANCE threshold (CEO rule 2026-07-12: affects MESSAGING ONLY, never
# whether rankings are displayed — top movers always ship, however small):
TREND_MIN_PCT      = 5.0    # a move >= 5 % counts as "significant"
TREND_TOP_N        = 5

TODAY = date.today().isoformat()


def confidence(history_days: int) -> str:
    if history_days < 7:
        return "low"
    if history_days < 30:
        return "medium"
    return "high"


def load_card_meta() -> dict:
    """Build card_image_id → {name, rarity, image, set_code} from the cards JSON."""
    meta = {}
    if not os.path.exists(CARDS_PATH):
        print(f"  Warning: {CARDS_PATH} not found — card names will fall back to card_id")
        return meta
    with open(CARDS_PATH, "r", encoding="utf-8") as f:
        cards_data = json.load(f)
    for set_code, cards in cards_data.items():
        if not isinstance(cards, list):
            continue
        for card in cards:
            cid = card.get("card_image_id") or card.get("card_number", "")
            if cid:
                meta[cid] = {
                    "name":     card.get("card_name", cid),
                    "rarity":   card.get("rarity", ""),
                    "image":    card.get("card_image", ""),
                    "set_code": set_code,
                }
    print(f"  Loaded metadata for {len(meta)} cards")
    return meta


def main():
    print(f"[compute_jp_trends] date={TODAY}")

    # ── Load history ─────────────────────────────────────────────────────────
    if not os.path.exists(HISTORY_PATH):
        print(f"  {HISTORY_PATH} not found — run append_jp_history.py first")
        return

    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        history = json.load(f)

    snapshots = sorted(history.get("snapshots", []), key=lambda s: s["date"])
    n_days = len(snapshots)
    print(f"  Loaded {n_days} snapshots")

    if n_days < 2:
        print("  Need at least 2 snapshots to compute trends — skipping")
        return

    # Most recent snapshot is "today"; baseline is ~7 days back (or earliest).
    today_snap    = snapshots[-1]
    baseline_idx  = max(0, n_days - 8)   # index 7 positions before last
    baseline_snap = snapshots[baseline_idx]

    days_apart = (
        date.fromisoformat(today_snap["date"])
        - date.fromisoformat(baseline_snap["date"])
    ).days

    print(
        f"  Comparing {today_snap['date']} vs {baseline_snap['date']} "
        f"({days_apart} days apart)"
    )

    today_prices    = today_snap["prices"]
    baseline_prices = baseline_snap["prices"]

    # ── Load card metadata ────────────────────────────────────────────────────
    card_meta = load_card_meta()

    # ── Compute movers ────────────────────────────────────────────────────────
    movers = []
    for card_id, today_data in today_prices.items():
        if card_id not in baseline_prices:
            continue

        today_jpy    = float(today_data.get("jpy", 0))
        today_thb    = float(today_data.get("thb", 0))
        baseline_jpy = float(baseline_prices[card_id].get("jpy", 0))
        baseline_thb = float(baseline_prices[card_id].get("thb", 0))

        # Data-quality gates only — significance no longer filters inclusion.
        if today_jpy < TREND_MIN_JPY or baseline_jpy < TREND_MIN_JPY:
            continue
        change_jpy = today_jpy - baseline_jpy
        change_thb = round(today_thb - baseline_thb, 2)
        if change_jpy == 0:
            continue  # zero-change cards are not "movers"
        pct = (change_jpy / baseline_jpy) * 100
        if abs(pct) > TREND_MAX_PCT_CAP:
            continue  # bad data, not real market movement

        meta = card_meta.get(card_id, {})
        movers.append({
            "significant":          abs(pct) >= TREND_MIN_PCT,
            "card_id":              card_id,
            "card_name":            meta.get("name", card_id),
            "set_code":             meta.get("set_code", ""),
            "rarity":               meta.get("rarity", ""),
            "card_image":           meta.get("image", ""),
            "today_price_jpy":      today_jpy,
            "baseline_price_jpy":   baseline_jpy,
            "today_price_thb":      today_thb,
            "baseline_price_thb":   baseline_thb,
            "price_change_jpy":     round(change_jpy, 2),
            "price_change_thb":     change_thb,
            "price_change_percent": round(pct, 2),
        })

    print(f"  {len(movers)} qualifying movers (from {len(today_prices)} JP cards)")

    # Sort by absolute JPY change — same ranking logic as the EN pipeline.
    gainers = sorted(
        [m for m in movers if m["price_change_jpy"] > 0],
        key=lambda m: m["price_change_jpy"], reverse=True
    )[:TREND_TOP_N]

    losers = sorted(
        [m for m in movers if m["price_change_jpy"] < 0],
        key=lambda m: m["price_change_jpy"]
    )[:TREND_TOP_N]

    # ── Build output metadata (same schema as EN top-gainers.json) ────────────
    conf = confidence(n_days)
    base_meta = {
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "snapshot_date":         today_snap["date"],
        "baseline_date":         baseline_snap["date"],
        "days_between_snapshots": days_apart,
        "history_days_collected": n_days,
        "period":                "7_day",
        "confidence":            conf,
        "source":                "Yuyu-Tei via jp-card-prices.com",
        "currency":              "JPY",
        "game":                  "onepiece",
        "cards_evaluated":       len(today_prices),
        "cards_with_trend":      len(movers),
        "significance_threshold_pct": TREND_MIN_PCT,
        "cards_significant":     sum(1 for m in movers if m["significant"]),
        # market_stable => no card moved >= threshold; UI shows a calm badge
        # ABOVE the rankings instead of hiding them (CEO rule 2026-07-12).
        "market_stable":         not any(m["significant"] for m in movers),
    }

    gainers_out = {**base_meta, "trend_type": "gainers", "cards": gainers}
    losers_out  = {**base_meta, "trend_type": "losers",  "cards": losers}

    # ── Save ─────────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(GAINERS_PATH), exist_ok=True)
    with open(GAINERS_PATH, "w", encoding="utf-8") as f:
        json.dump(gainers_out, f, ensure_ascii=False, indent=2)
    with open(LOSERS_PATH, "w", encoding="utf-8") as f:
        json.dump(losers_out, f, ensure_ascii=False, indent=2)

    print(
        f"  Wrote {len(gainers)} gainers → {GAINERS_PATH}\n"
        f"  Wrote {len(losers)} losers  → {LOSERS_PATH}\n"
        f"  Confidence: {conf} ({n_days} days collected)"
    )


if __name__ == "__main__":
    main()
