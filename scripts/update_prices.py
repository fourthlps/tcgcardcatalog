"""
Runs inside GitHub Actions on a schedule (or via manual workflow_dispatch).
Re-fetches all One Piece TCG data from optcgapi.com -- main numbered booster
sets, Extra Boosters, Premium Boosters, and Starter Decks -- and overwrites
the single combined JSON file that index.html reads.

Phase 2: Also appends today's prices to Price History Raw (Google Sheets)
via an Apps Script Web App endpoint.

Phase 3: Computes day-over-day price trends by comparing today's prices
against prices-snapshot.json (the last fully-successful run's prices).
Writes top-gainers.json, top-losers.json, and status.json to data/.
"""

import json
import os
import time
from datetime import date as date_type
from datetime import datetime, timezone

import requests

BASE_URL = "https://optcgapi.com/api"
OUTPUT_PATH = "onepiece-catalog/one_piece_OP01-OP16_with_prices.json"
# Normalized per-market price dataset (keyed by card_image_id). Prices live
# here, NOT on the card objects -- card describes the card, market describes price.
MARKETS_PATH = "onepiece-catalog/card-markets.json"

# --- Phase 3: Trend + status output paths ---
DATA_DIR = "onepiece-catalog/data"
SNAPSHOT_PATH = f"{DATA_DIR}/prices-snapshot.json"
GAINERS_PATH = f"{DATA_DIR}/top-gainers.json"
LOSERS_PATH = f"{DATA_DIR}/top-losers.json"
STATUS_PATH = f"{DATA_DIR}/status.json"

# Trend quality filters -- ALL of the following must pass simultaneously.
# Cards that fail any check are silently skipped; fewer but trustworthy results.
TREND_MIN_PRICE_USD = 1.00  # both today AND baseline price must be >= $1.00
                             # eliminates $0.02 bulk commons and corrupted lookups
TREND_MIN_ABS_USD = 1.00    # absolute dollar change must be >= $1.00
TREND_MIN_PCT = 3.0         # AND percentage change must be >= 3.0%
TREND_MAX_PCT_CAP = 500.0   # hard cap: change > 500% is treated as bad data, not real market movement
TREND_TOP_N = 5             # top N cards shown per direction
                             # Ranking is by dollar change (not %), so $50 on a $200 card
                             # ranks above $10 on a $12 card even if % differs.

# --- Phase 2: Price History Raw ingest ---
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
APPS_SCRIPT_SECRET = os.environ.get("APPS_SCRIPT_SECRET", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
HISTORY_BATCH_SIZE = 200
HISTORY_CURRENCY = "USD"
HISTORY_SOURCE = "optcgapi.com"
HISTORY_MAX_ATTEMPTS = 3
HISTORY_RETRY_DELAY_SECONDS = 2

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
# data source. Until then, EB-04 cards/prices will be genuinely missing.
EXTRA_SET_CODES = [
    "EB-01",        # Extra Booster: Memorial Collection
    "EB-02",        # Extra Booster: Anime 25th Collection
    "EB-03",        # Extra Booster: One Piece Heroines Edition
    "OP14-EB04",    # The Azure Sea's Seven (real main booster OP-14)
    "OP15-EB04",    # Adventure on Kami's Island (real main booster OP-15)
    "PRB-01",       # Premium Booster - The Best
    "PRB-02",       # Premium Booster - The Best - Vol. 2
]

# Starter Decks -- separate /decks/{id}/ endpoint.
DECK_CODES = [f"ST-{i:02d}" for i in range(1, 31)]

REQUEST_DELAY_SECONDS = 1.5  # be polite to a free hobbyist-run API

# optcgapi returns the literal string "NULL" for empty fields instead of a
# real JSON null. Normalize it here so downstream data is always clean.
NULL_LIKE = {"NULL", "null", ""}

# ---------------------------------------------------------------------------
# Phase 4: Yuyu-tei JP retail price scraping
# ---------------------------------------------------------------------------
import re as _re  # noqa: E402 (imported here for locality)

# Map from our internal set code (e.g. "OP-01") to yuyu-tei set slug ("op01")
YUYU_TEI_CODE_MAP = {
    **{f"OP-{i:02d}": f"op{i:02d}" for i in range(1, 17)},
    "EB-01": "eb01",
    "EB-02": "eb02",
    "EB-03": "eb03",
    "OP14-EB04": "op14",   # Adventure on Kami's Island (uses OP-14 slug on yuyu-tei)
    "OP15-EB04": "op15",   # The Azure Sea's Seven
    "PRB-01": "prb01",
    "PRB-02": "prb02",
}
YUYU_TEI_DELAY = 2.0   # polite delay between set pages (seconds)

# Confident variant signatures we map JP prices for. Anything that does not
# reduce cleanly to one of these (Alternate Art, Reprint, Manga, foils, ...)
# stays EN-only rather than risk attaching a JP price to the wrong variant.
CONFIDENT_SIGS = ["base", "parallel", "super", "red-super", "sp", "sp-gold", "sp-silver"]


def opt_sig(card_name: str, card_set_id: str):
    """Reduce an optcgapi variant (English treatment name) to a signature."""
    toks = [t.strip() for t in _re.findall(r"\(([^)]+)\)", card_name)]
    toks = [t for t in toks
            if not _re.fullmatch(r"\d+", t)
            and t != card_set_id
            and not _re.fullmatch(r"[A-Z]{2}\d{2}-\d+", t)]
    if not toks:
        return "base"
    low = [t.lower() for t in toks]
    if "red super alternate art" in low:
        return "red-super"
    if "super alternate art" in low:
        return "super"
    if "sp" in low:
        if "gold" in low:
            return "sp-gold"
        if "silver" in low:
            return "sp-silver"
        return "sp"
    if len(toks) == 1 and "parallel" in low:
        return "parallel"
    return None  # ambiguous -> skip


def yuyu_sig(rarity: str, treatments: list):
    """Reduce a yuyu-tei listing (rarity prefix + JP treatment labels) to a signature."""
    t = treatments or []
    if "レッドスーパーパラレル" in t:
        return "red-super"
    if "スーパーパラレル" in t:
        return "super"
    if rarity == "SP":
        if "金パラレル" in t:
            return "sp-gold"
        if "銀パラレル" in t:
            return "sp-silver"
        if all(x == "パラレル" for x in t):
            return "sp"
        return None
    if "特別パラレル" in t or "刻印なし" in t:
        return None
    is_parallel = rarity.startswith("P-") or "パラレル" in t
    if is_parallel:
        if len(t) == 1 and t[0] == "パラレル":
            return "parallel"
        if rarity.startswith("P-") and len(t) == 0:
            return "parallel"
        return None
    if not rarity.startswith("P-") and rarity != "SP" and len(t) == 0:
        return "base"
    return None


def parse_yuyu_listings(html: str, prefix: str) -> dict:
    """Parse a yuyu-tei set page into {card_number: {signature: [prices_jpy]}}.

    Each product's <img alt> carries the card number, rarity code, and JP
    treatment labels, e.g. "OP13-118 P-SEC ...(パラレル)(レッドスーパーパラレル)".
    """
    out: dict = {}
    seen = set()
    pat = _re.compile(r'alt="(' + prefix + r'\d{2}-\d+)\s+([A-Z][A-Z-]*)\s+([^"]*)"')
    for m in pat.finditer(html):
        cardnum, rarity, rest = m.group(1), m.group(2), m.group(3)
        treatments = _re.findall(r"\(([^)]+)\)", rest)
        after = html[m.start():m.start() + 900]
        pm = _re.search(r"(\d[\d,]*)\s*円", after)
        if not pm:
            continue
        price = int(pm.group(1).replace(",", ""))
        key = f"{cardnum}|{rarity}|{','.join(treatments)}"
        if key in seen:
            continue
        seen.add(key)
        sig = yuyu_sig(rarity, treatments)
        if not sig:
            continue
        out.setdefault(cardnum, {}).setdefault(sig, []).append(price)
    return out


def fetch_yuyu_listings(yuyu_slug: str, expected: int) -> dict:
    """Fetch + parse one yuyu-tei set page, retrying until a plausible number
    of listings is returned (the site intermittently serves a blocked/empty
    page). Returns {} if it never reaches the expected threshold."""
    url = f"https://yuyu-tei.jp/sell/opc/s/{yuyu_slug}"
    prefix = yuyu_slug[:2].upper()
    for attempt in range(1, 5):
        try:
            resp = requests.get(url, timeout=25, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
                "Accept-Language": "ja,en;q=0.9",
            })
            if resp.status_code == 200:
                listings = parse_yuyu_listings(resp.text, prefix)
                if len(listings) >= expected:
                    return listings
                print(f"  yuyu-tei/{yuyu_slug}: attempt {attempt} weak "
                      f"({len(listings)} listings, want {expected}) -- retrying")
            else:
                print(f"  yuyu-tei/{yuyu_slug}: attempt {attempt} HTTP {resp.status_code}")
        except Exception as e:
            print(f"  yuyu-tei/{yuyu_slug}: attempt {attempt} error ({e})")
        time.sleep(3)
    print(f"  yuyu-tei/{yuyu_slug}: SKIPPED (never reached {expected} listings)")
    return {}


def fetch_fx_rates() -> dict:
    """Fetch JPY->THB and USD->THB conversion rates. Falls back to constants
    if the FX API is unreachable so the run never fails on FX alone."""
    rates = {"jpy_to_thb": 0.235, "usd_to_thb": 35.0, "jpy_to_usd": 0.0067}
    try:
        r = requests.get("https://open.er-api.com/v6/latest/JPY", timeout=15)
        if r.status_code == 200:
            j = r.json().get("rates", {})
            if j.get("THB") and j.get("USD"):
                rates["jpy_to_thb"] = j["THB"]
                rates["jpy_to_usd"] = j["USD"]
                rates["usd_to_thb"] = round(j["THB"] / j["USD"], 2)
    except Exception as e:
        print(f"FX: could not fetch live rate ({e}), using fallback constants")
    return rates


def build_card_markets(combined: dict, jp_listings_by_set: dict, fx: dict, run_date: str) -> dict:
    """Build the normalized per-market price dataset, keyed by card_image_id.

    EN entries come from optcgapi market_price (per variant). JP entries come
    from yuyu-tei via a CONFIDENT 1:1 variant-signature match: for a given card
    number, a signature gets a JP price only when exactly one optcgapi variant
    and exactly one yuyu listing share it. Ambiguous treatments stay EN-only.
    JP prices are a converted retail *reference*, never a Thai market price.
    """
    markets: dict = {}
    en_count = jp_count = 0
    jp_by_sig: dict = {}
    jpy_to_thb = fx["jpy_to_thb"]

    for set_code, cards in combined.items():
        # EN entries (per variant) + group optcgapi variants by card number/sig
        opt_by_num: dict = {}
        for c in cards:
            key = c.get("card_image_id") or c.get("card_set_id")
            if not key:
                continue
            mp = c.get("market_price")
            try:
                mp = float(mp)
            except (TypeError, ValueError):
                mp = 0
            if mp > 0:
                markets.setdefault(key, []).append({
                    "source_market": "EN", "source_name": "TCGPlayer",
                    "source_currency": "USD", "source_price": mp,
                    "last_updated": c.get("date_scraped") or run_date,
                    "price_type": "Market Price", "confidence": "High",
                })
                en_count += 1
            sig = opt_sig(c.get("card_name", ""), c.get("card_set_id", ""))
            if sig:
                num = c.get("card_set_id", "")
                opt_by_num.setdefault(num, {}).setdefault(sig, []).append(c)

        # JP entries: confident 1:1 signature match
        yuyu = jp_listings_by_set.get(set_code, {})
        for num, sigs in opt_by_num.items():
            y_sigs = yuyu.get(num)
            if not y_sigs:
                continue
            for sig in CONFIDENT_SIGS:
                opts = sigs.get(sig)
                yprices = y_sigs.get(sig)
                if opts and len(opts) == 1 and yprices and len(yprices) == 1:
                    c = opts[0]
                    key = c.get("card_image_id") or c.get("card_set_id")
                    jpy = yprices[0]
                    markets.setdefault(key, []).append({
                        "source_market": "JP", "source_name": "Yuyu-Tei",
                        "source_currency": "JPY", "source_price": jpy,
                        "converted_currency": "THB",
                        "converted_price": round(jpy * jpy_to_thb),
                        "conversion_rate_used": jpy_to_thb,
                        "last_updated": run_date,
                        "price_type": "Retail Reference", "confidence": "Medium",
                    })
                    jp_count += 1
                    jp_by_sig[sig] = jp_by_sig.get(sig, 0) + 1

    return {
        "_meta": {
            "generated": run_date, "fx": fx,
            "en_entries": en_count, "jp_entries": jp_count, "jp_by_signature": jp_by_sig,
            "note": ("Keyed by card_image_id. JP prices via confident 1:1 variant-signature "
                     "mapping (base, parallel, super/red-super alt, SP gold/silver). Ambiguous "
                     "treatments (Alternate Art, Reprint, Manga, foils) stay EN-only. Converted "
                     "THB is an estimate, not a verified Thai market price."),
        },
        "markets": markets,
    }




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


# ---------------------------------------------------------------------------
# Phase 3: Snapshot-based trend computation
# ---------------------------------------------------------------------------

def load_snapshot() -> dict:
    """Load the last fully-successful prices-snapshot.json.
    Returns {} if the file doesn't exist or can't be parsed -- first-run safe.
    """
    if not os.path.exists(SNAPSHOT_PATH):
        print("Snapshot: no previous snapshot found -- trends will be skipped this run")
        return {}
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        snap_date = data.get("snapshot_date", "unknown")
        total = data.get("total_cards", 0)
        print(f"Snapshot: loaded {total} prices from {snap_date}")
        return data
    except Exception as e:
        print(f"Snapshot: could not read {SNAPSHOT_PATH}: {e} -- trends skipped")
        return {}


def _confidence(history_days: int) -> str:
    """Confidence tier based on how many days of history exist."""
    if history_days < 7:
        return "low"
    elif history_days < 30:
        return "medium"
    return "high"


def _days_between(date_a: str, date_b: str) -> int:
    """Return (date_b - date_a) in whole days. Returns 0 on parse error."""
    try:
        return (date_type.fromisoformat(date_b) - date_type.fromisoformat(date_a)).days
    except Exception:
        return 0


def compute_trends(combined: dict, snapshot: dict, run_date: str, timestamp: str) -> tuple[dict, dict]:
    """Compare today's combined data against the last snapshot.

    Uses dual-threshold filtering: a card only appears in trends if BOTH
    |price_change_usd| >= TREND_MIN_ABS_USD AND |price_change_pct| >= TREND_MIN_PCT.
    This prevents noise on cheap cards (big % / tiny $) and trivial moves on
    expensive cards (tiny % / large $).

    Returns (gainers_payload, losers_payload) -- dicts ready to write to disk.
    Both always include full metadata even when cards list is empty, so the
    frontend can read confidence/history info regardless of whether real trends exist.
    """
    snapshot_prices = snapshot.get("prices", {})
    baseline_date = snapshot.get("snapshot_date", "")
    prev_history_days = snapshot.get("history_days_collected", 0)
    history_days = prev_history_days + 1

    days_between = _days_between(baseline_date, run_date) if baseline_date else 0

    meta = {
        "generated_at": timestamp,
        "snapshot_date": run_date,
        "baseline_date": baseline_date or None,
        "days_between_snapshots": days_between,
        "history_days_collected": history_days,
        "period": "day_over_day",
        "confidence": _confidence(history_days),
        "source": HISTORY_SOURCE,
        "currency": HISTORY_CURRENCY,
        "game": "onepiece",
    }

    # Same-day guard: comparing a snapshot to itself produces noise, not signal.
    # This happens when the pipeline runs twice on the same date (e.g. a manual
    # re-run after an earlier scheduled run already saved the snapshot).
    if not snapshot_prices or not baseline_date:
        print("Trends: no baseline available -- writing empty trend files")
        empty = {**meta, "cards_evaluated": 0, "cards_with_trend": 0}
        return ({**empty, "trend_type": "gainers", "cards": []},
                {**empty, "trend_type": "losers",  "cards": []})

    if days_between == 0:
        print(
            f"Trends: baseline_date == run_date ({run_date}) -- "
            f"same-day comparison skipped, writing empty trend files"
        )
        empty = {**meta, "cards_evaluated": 0, "cards_with_trend": 0}
        return ({**empty, "trend_type": "gainers", "cards": []},
                {**empty, "trend_type": "losers",  "cards": []})

    changes = []
    cards_evaluated = 0
    skipped_no_baseline = skipped_min_price = skipped_min_abs = skipped_min_pct = skipped_cap = 0

    for set_code, cards in combined.items():
        for card in cards:
            card_id = card.get("card_set_id")
            if not card_id:
                continue

            today_raw = card.get("market_price")
            if today_raw is None:
                continue
            try:
                today_price = float(today_raw)
            except (TypeError, ValueError):
                continue
            if today_price <= 0:
                continue

            # Composite key must match what save_snapshot wrote
            composite_key = f"{set_code}::{card_id}"
            baseline_price = snapshot_prices.get(composite_key)
            if baseline_price is None or baseline_price <= 0:
                skipped_no_baseline += 1
                continue  # card not in previous snapshot (new or unmapped) -- skip

            # Minimum price on BOTH sides: eliminates bulk commons and corrupted lookups
            # where a $0.11 base card is compared against a $2,124 alternate art.
            if today_price < TREND_MIN_PRICE_USD or baseline_price < TREND_MIN_PRICE_USD:
                skipped_min_price += 1
                continue

            cards_evaluated += 1
            price_change = today_price - baseline_price
            price_change_pct = (price_change / baseline_price) * 100

            # Hard cap: > 500% or < -99% change indicates bad data, not real market movement
            if abs(price_change_pct) > TREND_MAX_PCT_CAP:
                skipped_cap += 1
                continue

            # Absolute dollar filter
            if abs(price_change) < TREND_MIN_ABS_USD:
                skipped_min_abs += 1
                continue

            # Percentage filter
            if abs(price_change_pct) < TREND_MIN_PCT:
                skipped_min_pct += 1
                continue

            changes.append({
                "card_id": card_id,
                "card_name": card.get("card_name") or "",
                "set_code": set_code,
                "rarity": card.get("rarity") or "",
                "card_image": card.get("card_image") or "",
                "today_price_usd": round(today_price, 4),
                "baseline_price_usd": round(baseline_price, 4),
                "price_change_usd": round(price_change, 4),
                "price_change_percent": round(price_change_pct, 2),
            })

    # Rank by absolute dollar change, not percentage.
    # $50 on a $200 card is more meaningful than $10 on a $12 card.
    gainers = sorted(
        [c for c in changes if c["price_change_usd"] > 0],
        key=lambda x: x["price_change_usd"], reverse=True
    )[:TREND_TOP_N]

    losers = sorted(
        [c for c in changes if c["price_change_usd"] < 0],
        key=lambda x: x["price_change_usd"]
    )[:TREND_TOP_N]

    print(
        f"Trends filter summary: no_baseline={skipped_no_baseline}, "
        f"min_price=${TREND_MIN_PRICE_USD}={skipped_min_price}, "
        f"cap={skipped_cap}, min_abs={skipped_min_abs}, min_pct={skipped_min_pct}"
    )

    full_meta = {**meta, "cards_evaluated": cards_evaluated, "cards_with_trend": len(changes)}
    print(
        f"Trends: {cards_evaluated} cards evaluated, {len(changes)} with qualifying movement "
        f"({len(gainers)} gainers, {len(losers)} losers), "
        f"baseline={baseline_date}, days_between={days_between}, confidence={meta['confidence']}"
    )

    return (
        {**full_meta, "trend_type": "gainers", "cards": gainers},
        {**full_meta, "trend_type": "losers",  "cards": losers},
    )


def write_json_file(path: str, data: dict, label: str = ""):
    """Write a JSON file, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    n = len(data.get("cards", []))
    print(f"{label or path}: written (cards={n})" if "cards" in data else f"{label or path}: written")


def save_snapshot(combined: dict, run_date: str, timestamp: str,
                  prev_history_days: int, failed_sets: int) -> bool:
    """Write prices-snapshot.json with today's prices as the new trend baseline.

    ONLY written when failed_sets == 0 (every set fetched successfully).
    A partial snapshot would corrupt future trend calculations -- cards that
    failed to fetch would appear to 'drop to zero', producing false losers.
    If any set fails, the previous snapshot remains untouched and will be
    used as the baseline for the next successful run.
    """
    if failed_sets > 0:
        print(
            f"Snapshot: skipping save -- {failed_sets} set(s) failed to fetch. "
            f"Previous snapshot will be used as baseline for the next successful run."
        )
        return False

    prices = {}
    total_with_price = 0
    for set_code, cards in combined.items():
        for card in cards:
            cid = card.get("card_set_id")
            price = card.get("market_price")
            if cid and price is not None:
                try:
                    # Composite key: "SET_CODE::card_id" prevents collision when the
                    # same card_id appears in multiple sets (alternate arts, SPs, gold
                    # versions). Without this, last-write wins and the snapshot maps
                    # e.g. OP09-051 to $0.11 (base card) while the alternate art is
                    # $2,124 -- producing a 1,900,000% phantom gain on next run.
                    composite_key = f"{set_code}::{cid}"
                    prices[composite_key] = float(price)
                    total_with_price += 1
                except (TypeError, ValueError):
                    pass

    new_history_days = prev_history_days + 1
    snapshot = {
        "snapshot_date": run_date,
        "generated_at": timestamp,
        "source": HISTORY_SOURCE,
        "total_cards": total_with_price,
        "history_days_collected": new_history_days,
        "prices": prices,
    }
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        # Compact (no indent): this file can be 300-500 KB and is machine-read only
        json.dump(snapshot, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Snapshot: saved {total_with_price} prices for {run_date} (history_days={new_history_days})")
    return True


def write_status(run_date: str, timestamp: str,
                 api_fetched: int, api_failed: int, total_cards: int, cards_with_price: int,
                 history_status: str, history_days: int, last_snapshot_date: str, sheets_status: str,
                 gainers_data: dict, losers_data: dict):
    """Write a lightweight health/status JSON for monitoring and frontend display."""
    trend_meta = gainers_data  # both share the same metadata
    status = {
        "last_updated": timestamp,
        "run_date": run_date,
        "status": "success" if api_failed == 0 else "partial",
        "api": {
            "source": "optcgapi.com",
            "status": "ok" if api_failed == 0 else "partial",
            "sets_fetched": api_fetched,
            "sets_failed": api_failed,
            "total_cards": total_cards,
            "cards_with_price": cards_with_price,
        },
        "history": {
            "status": history_status,
            "days_collected": history_days,
            "last_snapshot_date": last_snapshot_date or None,
            "sheets_append": sheets_status,
        },
        "trends": {
            "status": "ok" if trend_meta.get("baseline_date") else "no_baseline",
            "gainers_count": len(gainers_data.get("cards", [])),
            "losers_count": len(losers_data.get("cards", [])),
            "confidence": trend_meta.get("confidence", "low"),
            "baseline_date": trend_meta.get("baseline_date"),
            "days_between_snapshots": trend_meta.get("days_between_snapshots", 0),
            "history_days_collected": trend_meta.get("history_days_collected", 0),
        },
    }
    write_json_file(STATUS_PATH, status, label="Status")


# ---------------------------------------------------------------------------
# Phase 2: Price History Raw ingest (Google Sheets via Apps Script)
# ---------------------------------------------------------------------------

def build_history_rows(combined: dict, run_date: str, timestamp: str):
    rows = []
    scanned = skipped_no_id = skipped_no_price = 0

    for set_code, cards in combined.items():
        for card in cards:
            scanned += 1
            card_id = card.get("card_set_id")
            if not card_id:
                skipped_no_id += 1
                continue
            price = card.get("market_price")
            if price is None:
                skipped_no_price += 1
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                skipped_no_price += 1
                continue
            rows.append({
                "date": run_date,
                "card_id": card_id,
                "card_name": card.get("card_name") or "",
                "set_code": set_code,
                "market_price": price,
                "currency": HISTORY_CURRENCY,
                "source": HISTORY_SOURCE,
                "source_url": f"{BASE_URL}/sets/{set_code}/",
                "confidence": "auto",
                "timestamp": timestamp,
            })

    stats = {
        "scanned": scanned,
        "with_market_price": len(rows),
        "skipped_no_id": skipped_no_id,
        "skipped_no_price": skipped_no_price,
        "skipped_total": skipped_no_id + skipped_no_price,
    }
    return rows, stats


def check_date_exists(run_date: str) -> str:
    """Read-only check: does Price History Raw already have rows for run_date?
    Returns 'not_exists', 'exists', or 'unknown' (fail-closed on error)."""
    if not APPS_SCRIPT_URL or not APPS_SCRIPT_SECRET:
        return "not_exists"

    payload = {"secret": APPS_SCRIPT_SECRET, "action": "checkDate", "date": run_date}
    last_error = None
    for attempt in range(1, HISTORY_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok"):
                last_error = body.get("error")
                break
            return "exists" if body.get("exists") else "not_exists"
        except requests.RequestException as exc:
            last_error = exc
            if attempt < HISTORY_MAX_ATTEMPTS:
                print(f"Price history: checkDate attempt {attempt}/{HISTORY_MAX_ATTEMPTS} failed ({exc}), retrying...")
                time.sleep(HISTORY_RETRY_DELAY_SECONDS)

    print(f"Price history: checkDate could not get a confirmed answer for {run_date} ({last_error})")
    return "unknown"


def post_wide_update(combined: dict, run_date: str, game: str = "onepiece") -> str:
    """Push today's prices to the Price History Wide pivot sheet via Apps Script.

    Sends one payload with all cards that have a market_price. The Apps Script
    handler writes a new date column (or updates the existing one if re-running
    today) and upserts card rows -- never touching any other date column.

    Returns a status string for the run log: 'ok', 'dry_run', 'skipped', or 'error'.
    Never raises -- a wide-sheet failure must never affect pipeline data.
    """
    if not APPS_SCRIPT_URL or not APPS_SCRIPT_SECRET:
        print("Price History Wide: APPS_SCRIPT_URL/APPS_SCRIPT_SECRET not configured, skipping")
        return "skipped"

    rows = []
    for set_code, cards in combined.items():
        for card in cards:
            price = card.get("market_price")
            if price is None:
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            rows.append({
                "card_id":      card.get("card_set_id") or "",
                "card_name":    card.get("card_name") or "",
                "set_code":     set_code,
                "rarity":       card.get("rarity") or "",
                "market_price": price,
            })

    if not rows:
        print("Price History Wide: no cards with price, skipping")
        return "skipped"

    payload = {
        "secret":  APPS_SCRIPT_SECRET,
        "action":  "updateWide",
        "dryRun":  DRY_RUN,
        "data": {
            "date":     run_date,
            "game":     game,
            "currency": HISTORY_CURRENCY,
            "rows":     rows,
        },
    }

    try:
        resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=120)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            print(f"Price History Wide: Apps Script error: {body.get('error')}")
            return "error"
        if DRY_RUN:
            print(f"Price History Wide: dry-run OK (would process {body.get('wouldProcess')} cards for {run_date})")
            return "dry_run"
        print(
            f"Price History Wide: {body.get('cardsUpdated', 0)} updated, "
            f"{body.get('cardsAdded', 0)} added, "
            f"date_col={'new' if body.get('isNewDateColumn') else 'existing'} "
            f"(col {body.get('dateColumnNumber')}), "
            f"total rows={body.get('totalDataRows')}"
        )
        return "ok"
    except Exception as exc:
        print(f"Price History Wide: failed ({exc}) -- pipeline data is not affected")
        return "error"


def post_run_log(log_data: dict) -> None:
    """Append one admin run-log row to the Run Log sheet via Apps Script.

    Never raises -- a logging failure must never corrupt or abort the pipeline.
    Respects DRY_RUN so test runs don't write phantom rows to the admin sheet.
    """
    if not APPS_SCRIPT_URL or not APPS_SCRIPT_SECRET:
        print("Run log: APPS_SCRIPT_URL/APPS_SCRIPT_SECRET not configured, skipping")
        return

    payload = {
        "secret": APPS_SCRIPT_SECRET,
        "action": "runLog",
        "dryRun": DRY_RUN,
        "data": log_data,
    }
    try:
        resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            print(f"Run log: Apps Script returned error: {body.get('error')}")
            return
        if DRY_RUN:
            print(f"Run log: dry-run OK (would append row for {log_data.get('run_date')})")
        else:
            print(f"Run log: appended at row {body.get('appendedRow')} in Run Log sheet")
    except Exception as exc:
        # Intentionally swallow -- run-log failure must never affect pipeline data
        print(f"Run log: failed to write ({exc}) -- pipeline data is not affected")


def post_history_rows(rows) -> bool:
    if not rows:
        print("Price history: nothing to send (no cards had a real price)")
        return True
    if not APPS_SCRIPT_URL or not APPS_SCRIPT_SECRET:
        print("Price history: APPS_SCRIPT_URL/APPS_SCRIPT_SECRET not configured, skipping ingest")
        return True

    ok = True
    for i in range(0, len(rows), HISTORY_BATCH_SIZE):
        batch = rows[i:i + HISTORY_BATCH_SIZE]
        payload = {"secret": APPS_SCRIPT_SECRET, "dryRun": DRY_RUN, "rows": batch}
        batch_ok = False
        last_error = None

        for attempt in range(1, HISTORY_MAX_ATTEMPTS + 1):
            try:
                resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=30)
                resp.raise_for_status()
                body = resp.json()
                if not body.get("ok"):
                    last_error = body.get("error")
                    break
                if DRY_RUN:
                    print(f"Price history: batch {i}-{i+len(batch)} dry-run OK, would append {body.get('wouldAppend')}")
                else:
                    print(f"Price history: batch {i}-{i+len(batch)} appended at row {body.get('startRow')}")
                batch_ok = True
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt < HISTORY_MAX_ATTEMPTS:
                    print(f"Price history: batch {i}-{i+len(batch)} attempt {attempt}/{HISTORY_MAX_ATTEMPTS} failed ({exc}), retrying...")
                    time.sleep(HISTORY_RETRY_DELAY_SECONDS)

        if not batch_ok:
            print(f"Price history: batch {i}-{i+len(batch)} failed after {HISTORY_MAX_ATTEMPTS} attempt(s) ({last_error})")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    wall_start = time.monotonic()
    now = datetime.now(timezone.utc)
    run_date = now.strftime("%Y-%m-%d")
    timestamp = now.isoformat()
    github_run_id = os.environ.get("GITHUB_RUN_ID", "")

    total_expected = len(SET_CODES) + len(EXTRA_SET_CODES) + len(DECK_CODES)

    # --- Phase 3: Load previous snapshot BEFORE fetching new data ---
    snapshot = load_snapshot()
    prev_history_days = snapshot.get("history_days_collected", 0)

    # --- Fetch all sets and decks ---
    combined = {}
    failed_sets = 0

    for code in SET_CODES + EXTRA_SET_CODES:
        try:
            cards = fetch_set_cards(code)
            if cards:
                combined[code] = [clean_card(c) for c in cards]
                print(f"{code}: {len(cards)} cards")
            else:
                print(f"{code}: empty response, skipping")
                failed_sets += 1
        except requests.RequestException as exc:
            print(f"{code}: failed ({exc}), keeping previous data for this set")
            failed_sets += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    for code in DECK_CODES:
        try:
            cards = fetch_deck_cards(code)
            if cards:
                combined[code] = [clean_card(c) for c in cards]
                print(f"{code}: {len(cards)} cards")
            else:
                print(f"{code}: empty response, skipping")
                failed_sets += 1
        except requests.RequestException as exc:
            print(f"{code}: failed ({exc}), keeping previous data for this deck")
            failed_sets += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    if not combined:
        print("Nothing fetched successfully -- aborting without overwriting any files")
        empty_trend = {"baseline_date": None, "confidence": "low",
                       "history_days_collected": prev_history_days, "days_between_snapshots": 0, "cards": []}
        write_status(
            run_date, timestamp,
            api_fetched=0, api_failed=total_expected,
            total_cards=0, cards_with_price=0,
            history_status="skipped", history_days=prev_history_days,
            last_snapshot_date=snapshot.get("snapshot_date", ""),
            sheets_status="skipped",
            gainers_data=empty_trend, losers_data=empty_trend,
        )
        duration = round(time.monotonic() - wall_start, 1)
        post_run_log({
            "run_date": run_date,
            "run_timestamp_utc": timestamp,
            "status": "failed",
            "source": HISTORY_SOURCE,
            "total_sets": total_expected,
            "successful_sets": 0,
            "failed_sets": total_expected,
            "total_cards": 0,
            "cards_with_price": 0,
            "cards_missing_price": 0,
            "history_rows_written": 0,
            "trend_gainers_count": 0,
            "trend_losers_count": 0,
            "snapshot_saved": False,
            "json_updated": False,
            "wide_sheet_status": "skipped",
            "duration_seconds": duration,
            "error_message": "All sets/decks failed to fetch -- no data written",
            "github_run_id": github_run_id,
            "notes": f"DRY_RUN={DRY_RUN}",
        })
        return

    total_cards = sum(len(v) for v in combined.values())
    api_fetched = len(combined)
    print(f"Fetched {api_fetched} sets/decks ({total_cards} cards total, {failed_sets} failed)")

    # --- Phase 3: Compute trends against snapshot ---
    gainers_data, losers_data = compute_trends(combined, snapshot, run_date, timestamp)
    write_json_file(GAINERS_PATH, gainers_data, label="Top gainers")
    write_json_file(LOSERS_PATH, losers_data, label="Top losers")

    # --- Phase 4: Build normalized market dataset (card-markets.json) ---
    # Scrape JP retail prices per set, then build a separate per-market price
    # dataset keyed by card_image_id. Prices do NOT get stamped onto cards.
    print("yuyu-tei: scraping JP retail prices for market dataset...")
    fx = fetch_fx_rates()
    print(f"FX: 1 JPY = {fx['jpy_to_thb']} THB | 1 USD = {fx['usd_to_thb']} THB")
    jp_listings_by_set = {}
    for api_code in combined:
        yuyu_slug = YUYU_TEI_CODE_MAP.get(api_code)
        if not yuyu_slug:
            continue
        expected = max(20, round(len(combined[api_code]) * 0.4))
        listings = fetch_yuyu_listings(yuyu_slug, expected)
        if listings:
            jp_listings_by_set[api_code] = listings
            print(f"  {api_code}: {len(listings)} card numbers scraped")
        time.sleep(YUYU_TEI_DELAY)

    card_markets = build_card_markets(combined, jp_listings_by_set, fx, run_date)
    os.makedirs(os.path.dirname(MARKETS_PATH), exist_ok=True)
    with open(MARKETS_PATH, "w", encoding="utf-8") as f:
        json.dump(card_markets, f, ensure_ascii=False)
    meta = card_markets["_meta"]
    print(f"Market dataset: {len(card_markets['markets'])} cards, "
          f"{meta['en_entries']} EN entries, {meta['jp_entries']} JP entries "
          f"{meta['jp_by_signature']} -> {MARKETS_PATH}")

    # --- Overwrite main catalog JSON ---
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(combined)} sets/decks -> {OUTPUT_PATH}")

    # --- Phase 3: Save snapshot (only on full success) ---
    snapshot_saved = save_snapshot(combined, run_date, timestamp, prev_history_days, failed_sets)
    last_snapshot_date = run_date if snapshot_saved else snapshot.get("snapshot_date", "")
    history_days_now = (prev_history_days + 1) if snapshot_saved else prev_history_days

    # --- Phase 2: Append to Price History Raw (Google Sheets) ---
    sheets_status = "skipped"
    history_rows_written = 0
    date_status = check_date_exists(run_date)
    if date_status == "exists":
        print(f"Price history: {run_date} snapshot already exists in Sheets. No rows inserted.")
        sheets_status = "already_exists"
    elif date_status == "unknown":
        print(
            f"Price history: could not confirm whether {run_date} already has a snapshot -- "
            f"skipping ingest for safety."
        )
        sheets_status = "skipped_safety"
    else:
        history_rows, stats = build_history_rows(combined, run_date, timestamp)
        print(
            f"Price history: {stats['scanned']} cards scanned, "
            f"{stats['with_market_price']} with market_price, "
            f"{stats['skipped_total']} skipped"
        )
        if DRY_RUN and history_rows:
            print("Price history: sample rows (first 5) --")
            for r in history_rows[:5]:
                print(f"  {r['card_id']:<16} {r['card_name']:<38} {r['market_price']:>8.2f} {r['currency']}")

        success = post_history_rows(history_rows)
        sheets_status = "ok" if success else "partial_failure"
        if success and not DRY_RUN:
            history_rows_written = len(history_rows)

    cards_with_price = sum(
        1 for cards in combined.values()
        for c in cards if c.get("market_price") is not None
    )
    cards_missing_price = total_cards - cards_with_price

    # --- Wide pivot sheet: update Price History Wide (one card per row, one date per column) ---
    wide_status = post_wide_update(combined, run_date)

    # --- Phase 3: Write status.json ---
    write_status(
        run_date, timestamp,
        api_fetched=api_fetched, api_failed=failed_sets,
        total_cards=total_cards, cards_with_price=cards_with_price,
        history_status="ok", history_days=history_days_now,
        last_snapshot_date=last_snapshot_date,
        sheets_status=sheets_status,
        gainers_data=gainers_data,
        losers_data=losers_data,
    )

    # --- Phase 4: Write admin run log to Google Sheets Run Log sheet ---
    duration = round(time.monotonic() - wall_start, 1)
    overall_status = "success" if failed_sets == 0 else "partial"
    notes_parts = [f"DRY_RUN={DRY_RUN}"]
    if failed_sets > 0:
        notes_parts.append(f"{failed_sets} set(s) failed")
    if not snapshot_saved:
        notes_parts.append("snapshot NOT saved (see failed_sets)")
    post_run_log({
        "run_date": run_date,
        "run_timestamp_utc": timestamp,
        "status": overall_status,
        "source": HISTORY_SOURCE,
        "total_sets": total_expected,
        "successful_sets": api_fetched,
        "failed_sets": failed_sets,
        "total_cards": total_cards,
        "cards_with_price": cards_with_price,
        "cards_missing_price": cards_missing_price,
        "history_rows_written": history_rows_written,
        "trend_gainers_count": len(gainers_data.get("cards", [])),
        "trend_losers_count": len(losers_data.get("cards", [])),
        "snapshot_saved": snapshot_saved,
        "json_updated": True,
        "wide_sheet_status": wide_status,
        "duration_seconds": duration,
        "error_message": "",
        "github_run_id": github_run_id,
        "notes": " | ".join(notes_parts),
    })


if __name__ == "__main__":
    main()
