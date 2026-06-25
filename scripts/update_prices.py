"""
Runs inside GitHub Actions on a schedule (or via manual workflow_dispatch).
Re-fetches all One Piece TCG data from optcgapi.com -- main numbered booster
sets, Extra Boosters, Premium Boosters, and Starter Decks -- and overwrites
the single combined JSON file that index.html reads.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://optcgapi.com/api"
OUTPUT_PATH = "onepiece-catalog/one_piece_OP01-OP16_with_prices.json"

# --- Phase 2: Price History Raw ingest (see PRICE-TRACKING-ARCHITECTURE.md) ---
#
# Posts one append-only row per card per run to the "Price History Raw" sheet
# via an Apps Script Web App endpoint, using market_price ONLY as the real
# price. inventory_price is intentionally NOT used as a fallback -- per the
# official-ledger sourcing decision made 2026-06-25, only optcgapi.com's own
# market_price field is trusted for this ledger. Cards with a null/missing
# market_price are skipped entirely for that day -- counted in the
# "missing market_price" stat, never faked, never backfilled from
# inventory_price. Source currency is USD -- that's what optcgapi.com
# actually returns; THB is only ever a client-side display conversion in
# index.html, never something to invent here.
#
# Configured via repo secrets APPS_SCRIPT_URL / APPS_SCRIPT_SECRET. DRY_RUN
# defaults to true so this can be wired up and exercised via manual
# workflow_dispatch runs without writing anything to the history ledger until
# it's been verified for a few days -- per the Phase 2 plan in the
# architecture doc ("a bad run shouldn't silently corrupt the history
# ledger"). Set DRY_RUN=false (as a workflow env/input) once that's done.
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
APPS_SCRIPT_SECRET = os.environ.get("APPS_SCRIPT_SECRET", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
HISTORY_BATCH_SIZE = 200
HISTORY_CURRENCY = "USD"
HISTORY_SOURCE = "optcgapi.com"
# Apps Script Web Apps occasionally return a transient 500 under back-to-back
# doPost calls (e.g. brief Sheets-service contention) -- seen once in 21
# batches during manual dry-run testing on 2026-06-25. Retrying a couple of
# times with a short delay lets a single blip self-heal instead of silently
# dropping that batch's rows for the day.
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


def build_history_rows(combined: dict, run_date: str, timestamp: str):
    """One row per card with a real, non-null market_price. market_price is
    the ONLY field trusted for this ledger -- inventory_price is never used
    as a fallback (official-ledger sourcing decision, 2026-06-25). A card
    with no market_price that day is skipped entirely: no row is written for
    it, and it is counted under stats['skipped_no_price'] so the gap is
    visible in the logs rather than silently invented or backfilled."""
    rows = []
    scanned = 0
    skipped_no_id = 0
    skipped_no_price = 0

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


def post_history_rows(rows):
    """Batch-POST rows to the Apps Script ingest endpoint. Returns True if
    every batch succeeded (or DRY_RUN reported success), False otherwise --
    callers should never treat a failure here as fatal for the main JSON
    pipeline, which already succeeded by the time this runs.

    Each batch gets up to HISTORY_MAX_ATTEMPTS tries with a short delay
    between them, so a single transient 500 from the Apps Script Web App
    doesn't drop that batch's rows for the day -- it just retries."""
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
                    # Rejected by the script itself (e.g. bad secret/payload)
                    # -- retrying won't change that, so stop immediately.
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

    # Phase 2: append today's real prices to Price History Raw. This never
    # touches/overwrites the JSON file above and never blocks on failure --
    # the live site's data pipeline (this script's original job) must keep
    # working even if the history ledger endpoint is down or misconfigured.
    now = datetime.now(timezone.utc)
    run_date = now.strftime("%Y-%m-%d")
    timestamp = now.isoformat()
    history_rows, stats = build_history_rows(combined, run_date, timestamp)
    print(
        f"Price history: {stats['scanned']} cards scanned, "
        f"{stats['with_market_price']} with market_price, "
        f"{stats['skipped_total']} skipped total "
        f"({stats['skipped_no_price']} missing market_price, "
        f"{stats['skipped_no_id']} missing card_set_id)"
    )

    # Verification-run diagnostics: only printed during DRY_RUN, since these
    # are for human review before flipping to a real write, not something we
    # need cluttering the daily production log once the ledger is live.
    if DRY_RUN and history_rows:
        print("Price history: sample rows (first 10) --")
        for r in history_rows[:10]:
            print(f"  {r['card_id']:<14} {r['card_name']:<40} {r['market_price']:>8.2f} {r['currency']}")

        top20 = sorted(history_rows, key=lambda r: r["market_price"], reverse=True)[:20]
        print("Price history: top 20 highest market_price --")
        for rank, r in enumerate(top20, start=1):
            print(f"  {rank:>2}. {r['card_id']:<14} {r['card_name']:<40} {r['market_price']:>8.2f} {r['currency']}")

    post_history_rows(history_rows)


if __name__ == "__main__":
    main()
