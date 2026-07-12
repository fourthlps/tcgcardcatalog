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

MARKETS_PATH = "onepiece-catalog/card-markets.json"
CARDS_PATH   = "onepiece-catalog/one_piece_OP01-OP16_with_prices.json"
MAP_PATH     = "onepiece-catalog/data/jp-yuyu-map.json"
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
# Actions, the JP stage soft-skips so the EN pipeline is never blocked.
RELAY_URL    = os.environ.get("JP_RELAY_URL", "")
RELAY_SECRET = os.environ.get("JP_RELAY_SECRET", "")
IN_ACTIONS   = os.environ.get("GITHUB_ACTIONS", "") == "true"

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


def jp_entry(markets, image_id):
    for e in markets.get(image_id, []):
        if e.get("source_market") == "JP":
            return e
    return None


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    t_start = time.time()
    print(f"[update_jp_prices] {TODAY} source={ACTIVE.name} dry_run={DRY_RUN} "
          f"transport={'relay' if (RELAY_URL and RELAY_SECRET) else 'direct'}")

    if IN_ACTIONS and not (RELAY_URL and RELAY_SECRET):
        # Soft-skip: never block the EN pipeline when the relay isn't configured.
        print("  JP relay secrets not configured — skipping JP price stage (soft)")
        return

    mk = load_json(MARKETS_PATH, None)
    if not mk or "markets" not in mk:
        print("  ABORT: card-markets.json missing/invalid"); sys.exit(1)
    markets = mk["markets"]

    jp_ids = [k for k in markets if jp_entry(markets, k)]
    by_number = {}
    for iid in jp_ids:
        num = iid.split("_")[0].upper()
        by_number.setdefault(num, []).append(iid)
    print(f"  Existing JP entries: {len(jp_ids)} across {len(by_number)} card numbers")

    mp = load_json(MAP_PATH, {"_meta": {"source": ACTIVE.name, "version": 1},
                              "manual": {}, "map": {}, "unmapped": {}})
    cid_map = {**mp.get("map", {}), **mp.get("manual", {})}   # manual wins

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

    # duplicate-cid detection within this run
    seen_cids, dup_cids = set(), set()
    for L in listings:
        (dup_cids if L["cid"] in seen_cids else seen_cids).add(L["cid"])
    listings = [L for L in listings if L["cid"] not in dup_cids]

    # ── incremental auto-matching for unknown cids ────────────────────────────
    new_maps = 0
    unknown = [L for L in listings if L["cid"] not in cid_map]
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
            cid_map[Ls[0]["cid"]] = cands[0]; mp["map"][Ls[0]["cid"]] = cands[0]
            mapped_ids.add(cands[0]); new_maps += 1; continue
        for L in Ls:                                               # price-join
            hits = [i for i in cands
                    if jp_entry(markets, i) and abs(jp_entry(markets, i)["source_price"] - L["price_jpy"]) < 0.5
                    and i not in mapped_ids]
            if len(hits) == 1:
                cid_map[L["cid"]] = hits[0]; mp["map"][L["cid"]] = hits[0]
                mapped_ids.add(hits[0]); new_maps += 1

    # record still-unknown cids in the living unmapped ledger
    new_unmapped = 0
    for L in listings:
        if L["cid"] in cid_map:
            mp["unmapped"].pop(L["cid"], None); continue
        u = mp["unmapped"].get(L["cid"])
        if u:
            u["last_seen"] = TODAY; u["price_jpy"] = L["price_jpy"]
        else:
            mp["unmapped"][L["cid"]] = {"card_number": L["card_number"], "rarity": L["rarity"],
                                        "name": L["name"], "price_jpy": L["price_jpy"],
                                        "first_seen": TODAY, "last_seen": TODAY}
            new_unmapped += 1

    # ── apply price updates ───────────────────────────────────────────────────
    fx_prev = float(mk.get("_meta", {}).get("fx", {}).get("jpy_to_thb", 0.2055))
    fx, fx_src = fetch_fx(fx_prev)

    matched = updated = big_moves = 0
    seen_this_run = set()
    for L in listings:
        iid = cid_map.get(L["cid"])
        if not iid:
            continue
        e = jp_entry(markets, iid)
        if not e:
            continue
        matched += 1; seen_this_run.add(iid)
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
    def _id_slug(i):
        p = i.split("-")[0].lower()
        return "eb04" if p in ("op14", "op15") and "eb04" in slug_set else p
    scope_ids = {i for i in jp_ids if _id_slug(i) in slug_set}
    missing_cids = [c for c, i in cid_map.items() if i in scope_ids and i not in seen_this_run]

    ratio = matched / max(1, len(scope_ids))
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
        "matched": matched, "match_ratio": round(ratio, 3), "updated": updated,
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
              "runtime_seconds", "listings_seen", "matched", "match_ratio",
              "updated", "new_mappings", "new_unmapped", "unmapped_total",
              "missing_cids", "duplicate_cids", "big_moves", "aborted", "abort_reason"):
        print(f"    {k}: {report[k]}")

    mp["_meta"].update({"source": ACTIVE.name, "updated": TODAY,
                        "mapped_total": len(mp["map"]) + len(mp.get("manual", {}))})
    atomic_write(MAP_PATH, mp)          # living map + ledger always persisted
    atomic_write(REPORT_PATH, report)   # per-run report always persisted

    if report["aborted"]:
        print("  ABORT: card-markets.json NOT modified"); sys.exit(1)
    if DRY_RUN:
        print("  DRY RUN: card-markets.json NOT modified"); return

    mk.setdefault("_meta", {}).setdefault("fx", {})["jpy_to_thb"] = fx
    mk["_meta"]["jp_prices_updated"] = TODAY
    mk["_meta"]["jp_price_source"] = ACTIVE.name
    atomic_write(MARKETS_PATH, mk)
    print(f"  Wrote {MARKETS_PATH}: {updated} entries refreshed (fx {fx} via {fx_src})")


if __name__ == "__main__":
    main()
