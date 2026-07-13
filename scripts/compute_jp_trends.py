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

# ── H4 WINDOW-HEALTH GUARD (recalibration when the 7-day window is untrustworthy) ──
# Distinguishes genuine movement from a frozen/stale source period and from a
# source-recovery / bulk-correction event. Operates on ACTUAL history prices
# (jpy), on the RAW comparable population (before display filters). No dates hard-coded.
FLAT_DAY_CHANGE_FRAC  = 0.001   # a day->day transition is "effectively identical" if
                                # < 0.1 % of comparable cards changed price (~<=2 of 2,246)
FROZEN_RUN_MIN        = 3       # a frozen sequence = >= 3 consecutive effectively-identical
                                # snapshots (so a SINGLE flat day is NOT frozen)
BULK_MIN_CHANGED      = 30      # bulk rule needs >= 30 actually-changed cards ...
BULK_MIN_CHANGED_FRAC = 0.01    # ... AND >= 1 % of comparable cards changed ...
BULK_EXTREME_PCT      = 80.0    # ... where a move > +-80 % is "extreme" ...
BULK_EXTREME_SHARE    = 0.40    # ... and > 40 % of changed cards being extreme => source correction

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


def _changed_comparable(prev_prices: dict, cur_prices: dict) -> tuple[int, int]:
    """Return (changed, comparable) using ACTUAL jpy prices, over cards present
    in BOTH snapshots (the raw comparable population — no display filters)."""
    changed = comparable = 0
    for cid, cur in cur_prices.items():
        prev = prev_prices.get(cid)
        if prev is None:
            continue
        comparable += 1
        if float(prev.get("jpy", 0)) != float(cur.get("jpy", 0)):
            changed += 1
    return changed, comparable


def _frozen_flags(snapshots: list) -> list:
    """Mark each snapshot True if it belongs to a frozen run of >= FROZEN_RUN_MIN
    consecutive effectively-identical snapshots. A single flat day (run of 2) is
    NOT frozen."""
    n = len(snapshots)
    flat = [False] * n  # flat[i] => transition (i-1 -> i) is effectively identical
    for i in range(1, n):
        changed, comparable = _changed_comparable(snapshots[i - 1]["prices"], snapshots[i]["prices"])
        flat[i] = comparable > 0 and (changed / comparable) < FLAT_DAY_CHANGE_FRAC
    frozen = [False] * n
    i = 1
    while i < n:
        if flat[i]:
            j = i
            while j < n and flat[j]:
                j += 1
            run_len = (j - 1) - (i - 1) + 1   # snapshots i-1 .. j-1 inclusive
            if run_len >= FROZEN_RUN_MIN:
                for k in range(i - 1, j):
                    frozen[k] = True
            i = j
        else:
            i += 1
    return frozen


def window_health(snapshots: list, baseline_idx: int, today_idx: int) -> tuple[bool, str, dict]:
    """Deterministic validity policy for the baseline->today comparison window.
    Detection order: identity -> frozen endpoints -> bulk correction.
    Returns (ok, reason, metrics). Reads only; never writes history."""
    base = snapshots[baseline_idx]["prices"]
    today = snapshots[today_idx]["prices"]
    comparable = sum(1 for cid in today if cid in base)
    metrics = {"comparable": comparable}
    # 1) identity consistency
    if comparable == 0:
        return False, "identity_mismatch", metrics
    # 2) frozen endpoints (>=3-snapshot frozen run at either end)
    frozen = _frozen_flags(snapshots)
    if frozen[baseline_idx]:
        return False, "frozen_baseline", metrics
    if frozen[today_idx]:
        return False, "frozen_current", metrics
    # 3) bulk-correction on the RAW comparable population (actual prices, pre-filter)
    changed = extreme = 0
    for cid, cur in today.items():
        prev = base.get(cid)
        if prev is None:
            continue
        b = float(prev.get("jpy", 0)); t = float(cur.get("jpy", 0))
        if b == t:
            continue
        changed += 1
        if b > 0 and abs((t - b) / b * 100.0) > BULK_EXTREME_PCT:
            extreme += 1
    changed_frac = (changed / comparable) if comparable else 0.0
    extreme_share = (extreme / changed) if changed else 0.0
    metrics.update({"changed": changed, "extreme": extreme,
                    "changed_frac": round(changed_frac, 4),
                    "extreme_share": round(extreme_share, 4)})
    # preconditions (enough real movement) THEN extreme-concentration rule
    if changed >= BULK_MIN_CHANGED and changed_frac >= BULK_MIN_CHANGED_FRAC:
        if extreme_share > BULK_EXTREME_SHARE:
            return False, "bulk_correction", metrics
    return True, "ok", metrics


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

    # ── H4: window-health guard ───────────────────────────────────────────────
    # If the 7-day window is not trustworthy (frozen/stale source at an endpoint,
    # or a source-recovery bulk correction), suppress movers and emit an honest
    # recalibration state. History/snapshots are never modified.
    today_idx = n_days - 1
    ok, reason, health = window_health(snapshots, baseline_idx, today_idx)
    if not ok:
        conf = confidence(n_days)
        recal_meta = {
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
            "cards_with_trend":      0,
            "significance_threshold_pct": TREND_MIN_PCT,
            "cards_significant":     0,
            "market_stable":         False,
            "recalibrating":         True,
            "recalibration_reason":  reason,
            "window_health":         health,
        }
        os.makedirs(os.path.dirname(GAINERS_PATH), exist_ok=True)
        with open(GAINERS_PATH, "w", encoding="utf-8") as f:
            json.dump({**recal_meta, "trend_type": "gainers", "cards": []}, f, ensure_ascii=False, indent=2)
        with open(LOSERS_PATH, "w", encoding="utf-8") as f:
            json.dump({**recal_meta, "trend_type": "losers", "cards": []}, f, ensure_ascii=False, indent=2)
        print(f"  RECALIBRATING ({reason}) -- movers suppressed. Metrics: {health}")
        return

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
        "recalibrating":         False,   # H4: genuine window (see window_health)
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
