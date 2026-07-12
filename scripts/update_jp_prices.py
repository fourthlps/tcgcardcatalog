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

REQUEST_DELAY   = 2.5
TIMEOUT         = 30
RETRIES         = 2
UA              = "VoyageLog-PriceBot/1.0 (polite; daily; contact via site)"

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

    def fetch_set(self, slug):
        url = self.BASE.format(slug=slug)
        last_err = None
        for attempt in range(RETRIES + 1):
            try:
                r = requests.get(url, timeout=TIMEOUT,
                                 headers={"User-Agent": UA, "Cache-Control": "no-cache"})
                if r.status_code == 404:
                    return None            # set not on source (yet) — not an error
                r.raise_for_status()
                return self._parse(r.text)
            except Exception as e:          # noqa: BLE001 — per-set isolation
                last_err = e
                time.sleep(2 * (attempt + 1))
        print(f"    ! fetch failed after retries: {last_err}")
        return []

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
    print(f"[update_jp_prices] {TODAY} source={ACTIVE.name} dry_run={DRY_RUN}")

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

    # ── fetch all sets ────────────────────────────────────────────────────────
    set_codes = derive_set_codes()
    slugs, seen = [], set()
    for c in set_codes:
        s = ACTIVE.set_slug(c)
        if s and s not in seen:
            seen.add(s); slugs.append(s)
    print(f"  Derived {len(set_codes)} set codes -> {len(slugs)} source pages")

    listings, sets_ok, sets_fail, sets_missing = [], 0, 0, []
    for slug in slugs:
        rows = ACTIVE.fetch_set(slug)
        if rows is None:
            sets_missing.append(slug)
        elif not rows:
            sets_fail += 1
            print(f"    {slug}: FAILED/empty — its cards keep previous prices")
        else:
            sets_ok += 1
            listings.extend(rows)
            print(f"    {slug}: {len(rows)} listings")
        time.sleep(REQUEST_DELAY)

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
    missing_cids = [c for c, i in cid_map.items() if i in set(jp_ids) and i not in seen_this_run]

    ratio = matched / max(1, len(jp_ids))
    changes = updated
    report = {
        "run_date": TODAY, "source": ACTIVE.name, "dry_run": DRY_RUN,
        "sets_fetched": sets_ok, "sets_failed": sets_fail, "sets_missing_on_source": sets_missing,
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
    for k in ("sets_fetched", "sets_failed", "listings_seen", "matched", "match_ratio",
              "updated", "new_mappings", "new_unmapped", "unmapped_total",
              "missing_cids", "big_moves", "aborted", "abort_reason"):
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
