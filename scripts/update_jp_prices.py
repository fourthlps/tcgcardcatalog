"""
update_jp_prices.py — KI-40: automated daily JP sell-price refresh.

ARCHITECTURE (CEO 2026-07-13): pipeline core is SOURCE-AGNOSTIC. Each price
source is an Adapter exposing the same tiny contract, so adding CardRush /
SNKRDUNK / Mercari later means writing one new adapter class — never touching
the matcher, guards, writer, or workflow.

    Adapter contract:
      .name                          str, e.g. "yuyu-tei"
      .set_slug(set_code) -> str|None    canonical set code -> source slug
      .fetch_set(slug) -> list[Listing]  Listing dict:
          { cid:str, card_number:str, rarity:str, name:str,
            price_jpy:int, in_stock:bool }

MATCHING: living map at onepiece-catalog/data/jp-yuyu-map.json
    { "_meta": {...}, "manual": {cid: card_image_id},   # founder overrides, win
      "map":   {cid: card_image_id},                    # auto-built, grows
      "unmapped": {cid: {card_number,rarity,name,price_jpy,first_seen,last_seen}} }
  Bootstrap + incremental auto-matching:
    1. count-join: within (card_number, base|parallel class) if exactly one
       listing and one of our entries -> map.
    2. price-join: match stored JPY price uniquely within the card_number.
    Anything ambiguous -> unmapped (reported, NEVER guessed).
  Reviewed exact-printing additions live in data/jp-verified-map.json.  They
  may create a new JP market record; automatic joins may only refresh records
  that already exist.

SAFETY: per-set failure isolation; abort-on-empty (<70% of existing JP entries
matched); abnormal-move abort (>30% of changes beyond +-80%); atomic write;
unmatched entries keep old values; UTF-8 no BOM.

Env: JP_DRY_RUN=1 -> fetch+match+report only, do not write card-markets.json.
"""

import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MARKETS_PATH = "onepiece-catalog/card-markets.json"
# The SPA reads the per-game mirror (multi-game refactor), not the canonical
# file — every canonical write must be mirrored or the UI never sees it.
MIRROR_PATH  = "onepiece-catalog/data/one-piece/prices.json"
CARDS_PATH   = "onepiece-catalog/one_piece_OP01-OP16_with_prices.json"
MAP_PATH     = "onepiece-catalog/data/jp-yuyu-map.json"
VERIFIED_MAP_PATH = "onepiece-catalog/data/jp-verified-map.json"
REPORT_PATH  = "onepiece-catalog/data/jp-price-report.json"

TODAY   = date.today().isoformat()
DRY_RUN = os.environ.get("JP_DRY_RUN", "").lower() in ("1", "true", "yes")

TIMEOUT         = 45
UA              = "VoyageLog-PriceBot/1.0 (polite; daily; contact via site)"

# Throttle policy (CEO spec): conservative spacing with jitter; on 429 honor
# Retry-After (capped) else exponential backoff with jitter; a repeatedly
# throttled batch slows down and eventually STOPS rather than hammering.
BASE_SPACING    = 3.5
SPACING_JITTER  = 1.5
RETRY_BACKOFFS  = [15, 30, 60]          # seconds, + jitter, per 429 attempt
RETRY_AFTER_CAP = 120
CB_SLOWDOWN_AT  = 3                     # consecutive throttled sets -> double spacing
CB_STOP_AT      = 6                     # consecutive throttled sets -> stop batch

# Relay transport (Route A): set via GitHub Actions Secrets. When absent in
# Actions the JP stage FAILS the run (a green run must mean JP actually
# refreshed — the old soft-skip hid a 11-day freeze, KI-40). Explicit dev runs
# can set JP_ALLOW_SKIP=true to restore the old soft-skip behavior.
RELAY_URL    = os.environ.get("JP_RELAY_URL", "")
RELAY_SECRET = os.environ.get("JP_RELAY_SECRET", "")
IN_ACTIONS   = os.environ.get("GITHUB_ACTIONS", "") == "true"
ALLOW_SKIP   = os.environ.get("JP_ALLOW_SKIP", "").lower() in ("1", "true", "yes")

MIN_MATCH_RATIO   = 0.70   # abort whole run below this
SET_MIN_RATIO     = 0.50   # skip single set below this share of its expected entries
BIG_MOVE_PCT      = 80.0
BIG_MOVE_SHARE    = 0.30
BIG_MOVE_MIN_N    = 20


# ─── SOURCE ADAPTERS ─────────────────────────────────────────────────────────
class YuyuTeiAdapter:
    """Direct Yuyu-Tei sell pages — one server-rendered page per set."""
    name = "yuyu-tei"
    BASE = "https://yuyu-tei.jp/sell/opc/s/{slug}"

    def set_slug(self, set_code):
        # "OP-01"->op01, "EB-01"->eb01, "PRB-01"->prb01, "ST-13"->st13,
        # "OP14-EB04"/"OP15-EB04" -> eb04 (they are halves of the same page)
        c = set_code.upper()
        if "EB04" in c:
            return "eb04"
        m = re.fullmatch(r"(OP|EB|PRB|ST)-(\d{2})", c)
        return (m.group(1) + m.group(2)).lower() if m else None

    def fetch_page(self, slug):
        """Single attempt. Returns dict:
        {status:int|None, retry_after:float|None, listings:list|None, err:str|None}
        status 200 -> listings parsed; 404 -> set absent (not an error);
        429 -> throttled (caller backs off); anything else / exception -> err.
        Never logs HTML, URLs-with-secrets, or the secret itself."""
        try:
            if RELAY_URL and RELAY_SECRET:
                r = requests.post(RELAY_URL, timeout=TIMEOUT,
                                  json={"auth": RELAY_SECRET, "set": slug, "full": True},
                                  headers={"Content-Type": "application/json"})
                r.raise_for_status()                     # relay itself must be healthy
                j = r.json()
                if j.get("error"):
                    return {"status": None, "retry_after": None, "listings": None,
                            "err": "relay: " + str(j["error"])[:80]}
                st = int(j.get("status") or 0)
                ra = j.get("retry_after")
                if st == 200:
                    return {"status": 200, "retry_after": None,
                            "listings": self._parse(j.get("body") or ""), "err": None}
                return {"status": st, "retry_after": float(ra) if ra else None,
                        "listings": None, "err": None}
            # direct path (local development only)
            r = requests.get(self.BASE.format(slug=slug), timeout=TIMEOUT,
                             headers={"User-Agent": UA, "Cache-Control": "no-cache"})
            if r.status_code == 200:
                return {"status": 200, "retry_after": None,
                        "listings": self._parse(r.text), "err": None}
            ra = r.headers.get("Retry-After")
            return {"status": r.status_code,
                    "retry_after": float(ra) if ra else None,
                    "listings": None, "err": None}
        except Exception as e:               # noqa: BLE001 — per-set isolation
            return {"status": None, "retry_after": None, "listings": None,
                    "err": str(e)[:120]}

    def _parse(self, html):
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for block in soup.select("div.card-product"):
            try:
                cid_el = block.select_one("input.cart_cid")
                num_el = block.select_one("span.border")
                name_el = block.select_one("h4")
                price_el = block.select_one("strong")
                if not (cid_el and num_el and name_el and price_el):
                    continue
                kizu = block.select_one("input.cart_kizu")
                if kizu is not None and str(kizu.get("value", "0")) != "0":
                    continue                # damaged-condition listing — skip
                name = name_el.get_text(strip=True)
                if "傷" in name:
                    continue                # damaged marked in name
                price_txt = re.sub(r"[^\d]", "", price_el.get_text())
                if not price_txt:
                    continue
                rar_h3 = block.find_previous("h3")
                rarity = ""
                if rar_h3:
                    sp = rar_h3.find("span")
                    rarity = (sp.get_text(strip=True) if sp else rar_h3.get_text(strip=True)).split()[0]
                out.append({
                    "cid":        str(cid_el.get("value", "")).strip(),
                    "card_number": num_el.get_text(strip=True).upper(),
                    "rarity":     rarity.upper(),
                    "name":       name,
                    "price_jpy":  int(price_txt),
                    "in_stock":   "×" not in block.get_text(),
                })
            except Exception:               # noqa: BLE001 — one bad block never kills a set
                continue
        return [x for x in out if x["cid"] and x["price_jpy"] > 0 and re.match(r"^[A-Z]+\d", x["card_number"])]


ADAPTERS = {"yuyu-tei": YuyuTeiAdapter()}
ACTIVE = ADAPTERS["yuyu-tei"]


# ─── HELPERS ─────────────────────────────────────────────────────────────────
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch_fx(fallback):
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=JPY&to=THB",
                         timeout=15, headers={"User-Agent": UA})
        r.raise_for_status()
        rate = float(r.json()["rates"]["THB"])
        if 0.1 < rate < 0.5:
            return rate, "frankfurter " + TODAY
    except Exception:                        # noqa: BLE001
        pass
    return fallback, "fallback (previous rate)"


def derive_set_codes():
    """Canonical universe = group keys of the combined cards JSON — future
    sets join automatically when the EN pipeline adds them."""
    data = load_json(CARDS_PATH, {})
    return [k for k in data.keys() if isinstance(data.get(k), list)]


def all_card_ids():
    """Return the exact-printing ids that the public catalogue can render.

    Verified source mappings are allowed to create a new JP market record, but
    only for an id present in the canonical cards dataset.  This prevents a
    stale or mistyped mapping from silently creating orphan price data.
    """
    data = load_json(CARDS_PATH, {})
    return {
        str(card.get("card_image_id"))
        for cards in data.values() if isinstance(cards, list)
        for card in cards if card.get("card_image_id")
    }


def jp_entry(markets, image_id):
    for e in markets.get(image_id, []):
        if e.get("source_market") == "JP":
            return e
    return None


def dedupe_jp_records(markets):
    """Keep one freshest JP record per exact printing.

    A stale duplicate makes record counts wrong and leaves price resolution
    dependent on array order.  EN records and the freshest JP record are
    preserved verbatim; the cleanup is reported by card id.
    """
    cleaned = []
    for iid, entries in markets.items():
        jp_rows = [e for e in entries if e.get("source_market") == "JP"]
        if len(jp_rows) <= 1:
            continue
        keep = max(jp_rows, key=lambda e: str(e.get("last_updated") or ""))
        kept_once = False
        new_entries = []
        for entry in entries:
            if entry.get("source_market") != "JP":
                new_entries.append(entry)
            elif entry is keep and not kept_once:
                new_entries.append(entry); kept_once = True
        markets[iid] = new_entries
        cleaned.append(iid)
    return cleaned


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    t_start = time.time()
    print(f"[update_jp_prices] {TODAY} source={ACTIVE.name} dry_run={DRY_RUN} "
          f"transport={'relay' if (RELAY_URL and RELAY_SECRET) else 'direct'}")

    if IN_ACTIONS and not (RELAY_URL and RELAY_SECRET):
        if ALLOW_SKIP:
            print("  JP relay secrets not configured — skipping JP price stage "
                  "(soft; explicitly allowed via JP_ALLOW_SKIP)")
            return
        print("::error title=JP relay secrets missing::JP_RELAY_URL / "
              "JP_RELAY_SECRET are not available to this run. Failing instead "
              "of soft-skipping so JP prices cannot silently freeze (KI-40). "
              "Set JP_ALLOW_SKIP=true only for intentional dev runs.")
        sys.exit(1)

    mk = load_json(MARKETS_PATH, None)
    if not mk or "markets" not in mk:
        print("  ABORT: card-markets.json missing/invalid"); sys.exit(1)
    markets = mk["markets"]
    deduped_jp_records = dedupe_jp_records(markets)

    jp_ids = [k for k in markets if jp_entry(markets, k)]
    by_number = {}
    for iid in jp_ids:
        num = iid.split("_")[0].upper()
        by_number.setdefault(num, []).append(iid)
    print(f"  Existing JP entries: {len(jp_ids)} across {len(by_number)} card numbers")

    mp = load_json(MAP_PATH, {"_meta": {"source": ACTIVE.name, "version": 2},
                              "manual": {}, "map": {}, "unmapped": {}})
    # v1->v2 migration: keys changed from bare cid to set/cid (cids are only
    # unique per set). Legacy bare-cid entries are dropped — the bootstrap
    # joins re-create them automatically on this same run.
    dropped = [k for k in list(mp.get("map", {})) if "/" not in k]
    for k in dropped: mp["map"].pop(k, None)
    for k in [k for k in list(mp.get("unmapped", {})) if "/" not in k]:
        mp["unmapped"].pop(k, None)
    if dropped:
        print(f"  Map migration v1->v2: discarded {len(dropped)} legacy keys (will re-map this run)")
    mp["_meta"]["version"] = 2
    verified_doc = load_json(VERIFIED_MAP_PATH, {"map": {}})
    verified_map = verified_doc.get("map", {})
    if not isinstance(verified_map, dict):
        print(f"  ABORT: {VERIFIED_MAP_PATH} has invalid map object"); sys.exit(1)
    invalid_verified_keys = [
        key for key in verified_map
        if not re.fullmatch(r"[a-z0-9]+/\d+", str(key))
    ]
    if invalid_verified_keys:
        print(f"  ABORT: {len(invalid_verified_keys)} verified mappings have invalid source keys")
        sys.exit(1)
    if len(set(verified_map.values())) != len(verified_map):
        print("  ABORT: verified mappings are not one-to-one")
        sys.exit(1)
    valid_card_ids = all_card_ids()
    invalid_verified = {
        key: iid for key, iid in verified_map.items()
        if iid not in valid_card_ids
    }
    if invalid_verified:
        print(f"  ABORT: {len(invalid_verified)} verified mappings target unknown card ids")
        sys.exit(1)
    # Precedence: auto map < reviewed verified map < founder manual override.
    cid_map = {**mp.get("map", {}), **verified_map, **mp.get("manual", {})}

    # ── derive source pages (deduped); JP_SETS env = staged validation override
    set_codes = derive_set_codes()
    slugs, seen = [], set()
    for c in set_codes:
        s = ACTIVE.set_slug(c)
        if s and s not in seen:
            seen.add(s); slugs.append(s)
    stage_sel = [s.strip().lower() for s in os.environ.get("JP_SETS", "").split(",") if s.strip()]
    if stage_sel:
        slugs = [s for s in slugs if s in stage_sel]
        print(f"  STAGED RUN: restricted to {slugs}")
    print(f"  Derived {len(set_codes)} set codes -> {len(slugs)} source pages to fetch")

    # ── throttle-aware batch loop ─────────────────────────────────────────────
    listings = []
    sets_ok = sets_fail = sets_throttled = retries_total = 0
    sets_missing, consecutive_throttled, throttle_stopped = [], 0, False
    spacing = BASE_SPACING

    for idx, slug in enumerate(slugs):
        if throttle_stopped:
            sets_throttled += 1
            continue                     # counted; keeps previous prices
        result, attempts = None, 0
        while attempts <= len(RETRY_BACKOFFS):
            result = ACTIVE.fetch_page(slug)
            if result["status"] == 429:
                if attempts == len(RETRY_BACKOFFS):
                    break                # throttled out for this set
                wait = result["retry_after"] or RETRY_BACKOFFS[attempts]
                wait = min(float(wait), RETRY_AFTER_CAP) + random.uniform(0, 3)
                print(f"    {slug}: 429 — backing off {wait:.0f}s "
                      f"(attempt {attempts + 1}/{len(RETRY_BACKOFFS)})")
                time.sleep(wait)
                attempts += 1; retries_total += 1
                continue
            break

        if result["status"] == 200:
            rows = result["listings"] or []
            # Yuyu cids are PER-SET (each page numbers from ~10001): the stable
            # global key is set/cid — mirrors Yuyu's own /card/{set}/{cid} URLs.
            for L in rows:
                L["key"] = f"{slug}/{L['cid']}"
            if rows:
                sets_ok += 1; listings.extend(rows)
                print(f"    {slug}: ok {len(rows)} listings"
                      + (f" ({attempts} retries)" if attempts else ""))
            else:
                sets_fail += 1
                print(f"    {slug}: 200 but 0 parsed — keeps previous prices")
            consecutive_throttled = 0
        elif result["status"] == 404:
            sets_missing.append(slug); consecutive_throttled = 0
        elif result["status"] == 429:
            sets_throttled += 1; consecutive_throttled += 1
            print(f"    {slug}: THROTTLED after retries — keeps previous prices")
            if consecutive_throttled >= CB_STOP_AT:
                throttle_stopped = True
                print(f"    CIRCUIT BREAKER: {consecutive_throttled} consecutive throttles — stopping batch")
            elif consecutive_throttled >= CB_SLOWDOWN_AT and spacing == BASE_SPACING:
                spacing = BASE_SPACING * 2
                print(f"    CIRCUIT BREAKER: slowing to {spacing:.1f}s spacing")
        else:
            sets_fail += 1; consecutive_throttled = 0
            print(f"    {slug}: FAILED ({result['err'] or result['status']}) — keeps previous prices")

        if idx < len(slugs) - 1 and not throttle_stopped:
            time.sleep(spacing + random.uniform(0, SPACING_JITTER))

    # duplicate-key detection within this run (true dupes inside one set page)
    seen_keys, dup_cids = set(), set()
    for L in listings:
        (dup_cids if L["key"] in seen_keys else seen_keys).add(L["key"])
    listings = [L for L in listings if L["key"] not in dup_cids]

    # ── incremental auto-matching for unknown keys ────────────────────────────
    new_maps = 0
    unknown = [L for L in listings if L["key"] not in cid_map]
    # group unknown listings per (number, parallel-class)
    def klass(r): return "P" if r.startswith("P") else "B"
    grp = {}
    for L in unknown:
        grp.setdefault((L["card_number"], klass(L["rarity"])), []).append(L)
    mapped_ids = set(cid_map.values())
    for (num, kl), Ls in grp.items():
        cands = [i for i in by_number.get(num, []) if i not in mapped_ids]
        cands = [i for i in cands if (("_" in i) if kl == "P" else ("_" not in i))] or cands
        if len(Ls) == 1 and len(cands) == 1:                       # count-join
            cid_map[Ls[0]["key"]] = cands[0]; mp["map"][Ls[0]["key"]] = cands[0]
            mapped_ids.add(cands[0]); new_maps += 1; continue
        for L in Ls:                                               # price-join
            hits = [i for i in cands
                    if jp_entry(markets, i) and abs(jp_entry(markets, i)["source_price"] - L["price_jpy"]) < 0.5
                    and i not in mapped_ids]
            if len(hits) == 1:
                cid_map[L["key"]] = hits[0]; mp["map"][L["key"]] = hits[0]
                mapped_ids.add(hits[0]); new_maps += 1

    # record still-unknown cids in the living unmapped ledger
    new_unmapped = 0
    for L in listings:
        if L["key"] in cid_map:
            mp["unmapped"].pop(L["key"], None); continue
        u = mp["unmapped"].get(L["key"])
        if u:
            u["last_seen"] = TODAY; u["price_jpy"] = L["price_jpy"]
        else:
            mp["unmapped"][L["key"]] = {"card_number": L["card_number"], "rarity": L["rarity"],
                                        "name": L["name"], "price_jpy": L["price_jpy"],
                                        "first_seen": TODAY, "last_seen": TODAY}
            new_unmapped += 1

    # ── apply price updates ───────────────────────────────────────────────────
    fx_prev = float(mk.get("_meta", {}).get("fx", {}).get("jpy_to_thb", 0.2055))
    fx, fx_src = fetch_fx(fx_prev)

    matched = matched_existing = updated = created = big_moves = 0
    seen_existing = set()
    seen_this_run = set()
    for L in listings:
        iid = cid_map.get(L["key"])
        if not iid:
            continue
        e = jp_entry(markets, iid)
        existed = e is not None
        if not e:
            # New JP records are created only from the separately reviewed
            # mapping file.  Auto/count joins may refresh existing records but
            # can never invent a price for an ambiguous printing.
            if L["key"] not in verified_map or iid not in valid_card_ids:
                continue
            e = {
                "source_market": "JP",
                "source_name": "Yuyu-Tei",
                "source_currency": "JPY",
                "source_price": L["price_jpy"],
                "converted_currency": "THB",
                "converted_price": round(L["price_jpy"] * fx),
                "conversion_rate_used": fx,
                "last_updated": TODAY,
                "price_type": "Retail Reference",
                "confidence": "Medium",
            }
            markets.setdefault(iid, []).append(e)
            created += 1
        matched += 1
        if existed:
            seen_existing.add(iid)
        seen_this_run.add(iid)
        old = float(e.get("source_price") or 0)
        if old > 0:
            delta = abs(L["price_jpy"] - old) / old * 100
            if delta > BIG_MOVE_PCT:
                big_moves += 1
        if L["price_jpy"] != old or e.get("last_updated") != TODAY:
            e["source_price"] = L["price_jpy"]
            e["converted_price"] = round(L["price_jpy"] * fx)
            e["conversion_rate_used"] = fx
            e["last_updated"] = TODAY
            updated += 1
    # Scope-aware accounting: staged runs judge coverage only within the sets
    # actually fetched, otherwise a valid op01-only test would falsely abort.
    slug_set = set(slugs)
    # Scope follows the source-page key, not the printed card-number prefix.
    # Reprint/SP listings often live on a newer set page while retaining an
    # older OPxx card number, so prefix-based accounting could exceed 100%.
    scope_ids = {
        iid for key, iid in cid_map.items()
        if "/" in key and key.split("/", 1)[0] in slug_set and iid in jp_ids
    }
    missing_cids = [c for c, i in cid_map.items() if i in scope_ids and i not in seen_this_run]

    matched_existing = len(seen_existing & scope_ids)
    ratio = matched_existing / max(1, len(scope_ids))
    changes = updated
    report = {
        "run_date": TODAY, "source": ACTIVE.name, "dry_run": DRY_RUN,
        "transport": "relay" if (RELAY_URL and RELAY_SECRET) else "direct",
        "staged_selection": stage_sel or None,
        "sets_attempted": len(slugs),
        "sets_success": sets_ok,
        "sets_throttled": sets_throttled,
        "sets_failed": sets_fail,
        "sets_missing_on_source": sets_missing,
        "throttle_stopped": throttle_stopped,
        "retries_total": retries_total,
        "runtime_seconds": round(time.time() - t_start, 1),
        "listings_seen": len(listings),
        "matched": matched, "matched_existing": matched_existing,
        "match_ratio": round(ratio, 3), "updated": updated,
        "created": created, "verified_mappings": len(verified_map),
        "deduped_jp_records": deduped_jp_records,
        "new_mappings": new_maps, "new_unmapped": new_unmapped,
        "unmapped_total": len(mp["unmapped"]),
        "missing_cids": len(missing_cids),
        "duplicate_cids": sorted(dup_cids),
        "big_moves": big_moves,
        "fx": {"jpy_to_thb": fx, "source": fx_src},
        "aborted": False, "abort_reason": None,
    }

    # ── guards ────────────────────────────────────────────────────────────────
    if ratio < MIN_MATCH_RATIO:
        report["aborted"], report["abort_reason"] = True, f"match ratio {ratio:.2f} < {MIN_MATCH_RATIO}"
    elif changes >= BIG_MOVE_MIN_N and big_moves / max(1, changes) > BIG_MOVE_SHARE:
        report["aborted"], report["abort_reason"] = True, f"{big_moves}/{changes} moves beyond ±{BIG_MOVE_PCT}%"

    print("  ── MAPPING REPORT ──")
    for k in ("transport", "staged_selection", "sets_attempted", "sets_success",
              "sets_throttled", "sets_failed", "throttle_stopped", "retries_total",
              "runtime_seconds", "listings_seen", "matched", "matched_existing", "match_ratio",
              "updated", "created", "verified_mappings", "deduped_jp_records",
              "new_mappings", "new_unmapped", "unmapped_total",
              "missing_cids", "duplicate_cids", "big_moves", "aborted", "abort_reason"):
        print(f"    {k}: {report[k]}")

    mp["_meta"].update({"source": ACTIVE.name, "updated": TODAY,
                        "mapped_total": len(set(mp["map"]) | set(verified_map) |
                                            set(mp.get("manual", {})))})
    atomic_write(MAP_PATH, mp)          # living map + ledger always persisted
    atomic_write(REPORT_PATH, report)   # per-run report always persisted

    if report["aborted"]:
        print("  ABORT: card-markets.json NOT modified"); sys.exit(1)
    if DRY_RUN:
        print("  DRY RUN: card-markets.json NOT modified"); return

    mk.setdefault("_meta", {}).setdefault("fx", {})["jpy_to_thb"] = fx
    mk["_meta"]["jp_prices_updated"] = TODAY
    mk["_meta"]["jp_price_source"] = ACTIVE.name
    mk["_meta"]["jp_entries"] = sum(
        1 for entries in markets.values() for item in entries
        if item.get("source_market") == "JP" and float(item.get("source_price") or 0) > 0
    )
    mk["_meta"]["note"] = (
        "Keyed by exact card_image_id. JP retail-reference prices come from "
        "Yuyu-Tei through reviewed one-to-one source mappings; ambiguous or "
        "edition-exclusive treatments remain without a JP price. THB values "
        "are currency conversions, not verified Thai market prices."
    )
    atomic_write(MARKETS_PATH, mk)
    print(f"  Wrote {MARKETS_PATH}: {updated} entries refreshed (fx {fx} via {fx_src})")
    if os.path.exists(MIRROR_PATH):
        atomic_write(MIRROR_PATH, mk)
        print(f"  Mirrored -> {MIRROR_PATH} (the file the SPA actually reads)")


if __name__ == "__main__":
    main()
