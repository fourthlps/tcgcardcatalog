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

import hashlib
import json
import os
import random
import re
import sys
import tempfile
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
# Data model v2 (founder step-6, 2026-07-23): founder-owned publication policy
# and the machine-written source-evidence layer. Both act ONLY under
# JP_VERIFIED_STRICT (default off) — nightly behavior is unchanged until the
# separately-approved activation.
POLICY_PATH   = "onepiece-catalog/data/jp-publication-policy.json"
EVIDENCE_PATH = "onepiece-catalog/data/jp-source-evidence.json"

# Validation-before-push (founder ruling 2026-07-19): this script never writes
# tracked files. Everything goes to a work dir OUTSIDE the git tree; the gate
# (jp_sentinel.py --gate) validates the candidate and promotes it only when
# every hard check passes.
WORK_ROOT = (os.environ.get("JP_WORK_DIR") or os.environ.get("RUNNER_TEMP")
             or tempfile.gettempdir())
JP_RUN_DIR         = os.path.join(WORK_ROOT, "jp-run")
CANDIDATE_PATH     = os.path.join(JP_RUN_DIR, "card-markets.candidate.json")
CANDIDATE_MAP_PATH = os.path.join(JP_RUN_DIR, "jp-yuyu-map.candidate.json")
CANDIDATE_EVIDENCE_PATH = os.path.join(JP_RUN_DIR, "jp-source-evidence.candidate.json")
WORK_REPORT_PATH   = os.path.join(JP_RUN_DIR, "jp-fetch-report.json")
RUN_ID = os.environ.get("GITHUB_RUN_ID") or \
    "local-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

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
    # ── JP_VERIFIED_STRICT (default OFF; ladder step 3, founder 2026-07-21) ──
    # exclusivity + tombstones + fail-loud identity validation for verified
    # mappings. The scheduled workflow never sets this env; it activates only
    # in explicitly-flagged dry-runs until founder-approved for production.
    STRICT = os.environ.get("JP_VERIFIED_STRICT", "").lower() in ("1", "true", "yes")
    # Tombstones: dict {id: reason} (preferred) or legacy list of ids.
    raw_tombs = verified_doc.get("tombstones") or {}
    if isinstance(raw_tombs, list):
        raw_tombs = {t: "unspecified" for t in raw_tombs}
    tombstones = set(raw_tombs)
    bad_tombs = [t for t in tombstones if t not in valid_card_ids]
    if bad_tombs:
        print(f"  ABORT: {len(bad_tombs)} tombstones target unknown card ids"); sys.exit(1)
    contradictions = tombstones & set(verified_map.values())
    if contradictions:
        print(f"  ABORT: ids both tombstoned and verified-mapped: {sorted(contradictions)}")
        sys.exit(1)
    # Temporary out-of-scope holds: {id: reason}. Same removal semantics as a
    # tombstone, but signals "evidence exists, correct listing unreachable in
    # the current fetch scope". Cannot coexist with a positive verified mapping
    # or a tombstone (founder G2 ruling 2026-07-23).
    holds = verified_doc.get("holds") or {}
    if not isinstance(holds, dict):
        print(f"  ABORT: {VERIFIED_MAP_PATH} holds must be an id->reason object"); sys.exit(1)
    bad_holds = [h for h in holds if h not in valid_card_ids]
    if bad_holds:
        print(f"  ABORT: {len(bad_holds)} holds target unknown card ids"); sys.exit(1)
    hold_conflicts = set(holds) & (set(verified_map.values()) | tombstones)
    if hold_conflicts:
        print(f"  ABORT: ids held AND verified-mapped/tombstoned: {sorted(hold_conflicts)}")
        sys.exit(1)
    policy = load_json(POLICY_PATH, {})
    policy_threshold = policy.get("high_value_threshold_jpy", 100000)
    approved_hv = policy.get("approved_high_value", {}) or {}
    approved_legacy = policy.get("approved_legacy_reference", {}) or {}
    # Auditable in-stock provenance (founder item-1 ruling 2026-07-24): a
    # retained stale price requires EVIDENCE of a prior in-stock observation
    # backing it — price equality with a frozen OOS ask proves nothing (could
    # be a seed value, an import, or coincidence). The evidence layer carries
    # last_instock_observed_at/last_instock_price durably across OOS
    # transitions for exactly this purpose.
    ev_prev = load_json(EVIDENCE_PATH, {"observations": {}}).get("observations", {})
    instock_provenance = {}          # (card_image_id, listing_key) -> (price, observed_at)
    for o in ev_prev.values():
        if o.get("last_instock_price"):
            instock_provenance[(o.get("card_image_id"), o.get("listing_key"))] = \
                (o["last_instock_price"], o.get("last_instock_observed_at"))
        elif o.get("stock_state") == "in_stock" and o.get("price"):
            instock_provenance[(o.get("card_image_id"), o.get("listing_key"))] = \
                (o["price"], o.get("observed_at"))

    # Stale retention policy (founder 2026-07-24): a provenance-backed stale
    # price stays displayable for up to 7 calendar days from its last verified
    # in-stock observation; beyond that the compact state becomes unavailable
    # (value stays in evidence + audit history). An OOS sighting never extends
    # the period. Enforced HERE in state generation, not in the UI.
    STALE_MAX_AGE_DAYS = 7
    def stale_age_ok(observed_at):
        try:
            age = (date.fromisoformat(TODAY) -
                   date.fromisoformat(str(observed_at)[:10])).days
        except (TypeError, ValueError):
            return False                       # unknown timestamp = no stale claim
        return 0 <= age <= STALE_MAX_AGE_DAYS
    if STRICT:
        # Exclusivity: a verified target id (or tombstoned id) may not keep or
        # gain ANY automatic mapping — the wrong auto key can never coexist
        # and overwrite the verified result later in page order.
        blocked_ids = set(verified_map.values()) | tombstones | set(holds)
        dropped_auto = [k for k, v in mp.get("map", {}).items() if v in blocked_ids]
        for k in dropped_auto:
            mp["map"].pop(k, None)
        if dropped_auto:
            print(f"  STRICT: evicted {len(dropped_auto)} auto keys claiming "
                  f"verified/tombstoned ids: {', '.join(sorted(dropped_auto))}")
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
    ok_slugs = set()
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
                sets_ok += 1; ok_slugs.add(slug); listings.extend(rows)
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
    if STRICT:
        mapped_ids |= tombstones | set(holds)   # never auto-mapped
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
    observations = {}     # STRICT: latest observation per card|source|listing key
    state_stamps = {}     # STRICT: iid -> (market_state, state_reason), applied post-loop
    strict_removals = []  # STRICT: logged removals (all categories)

    def disposition(iid, L):
        """STRICT publication policy for one matched listing.
        Returns (write_price, market_state, state_reason, exclusion_reason).
        A verified mapping never automatically implies its price is publishable."""
        is_verified = L["key"] in verified_map
        if not L["in_stock"]:
            if is_verified:
                return (False, "unavailable", "verified_source_out_of_stock",
                        "out_of_stock")
            return (False, "stale", "no_current_instock_observation", "out_of_stock")
        if L["price_jpy"] > policy_threshold and iid not in approved_hv:
            return (False, "under_review",
                    "high_value_requires_completed_sale_or_founder_approval",
                    "over_threshold_pending_review")
        return (True, "live", None, None)

    for L in listings:
        iid = cid_map.get(L["key"])
        if not iid:
            continue
        write_price, mstate, mreason, excl = True, "live", None, None
        if STRICT:
            write_price, mstate, mreason, excl = disposition(iid, L)
            prev_obs = ev_prev.get(f"{iid}|yuyu-tei|{L['key']}", {})
            if L["in_stock"]:
                last_instock_at, last_instock_price = TODAY, L["price_jpy"]
            else:
                last_instock_at = prev_obs.get("last_instock_observed_at") or (
                    prev_obs.get("observed_at") if prev_obs.get("stock_state") == "in_stock" else None)
                last_instock_price = prev_obs.get("last_instock_price") or (
                    prev_obs.get("price") if prev_obs.get("stock_state") == "in_stock" else None)
            observations[f"{iid}|yuyu-tei|{L['key']}"] = {
                "last_instock_observed_at": last_instock_at,
                "last_instock_price": last_instock_price,
                "card_image_id": iid, "source": "yuyu-tei",
                "listing_key": L["key"], "price": L["price_jpy"],
                "currency": "JPY",
                "stock_state": "in_stock" if L["in_stock"] else "out_of_stock",
                "observed_at": TODAY,
                "mapping_method": ("manual" if L["key"] in mp.get("manual", {})
                                   else "verified_map" if L["key"] in verified_map
                                   else "automatic"),
                "identity_status": "verified" if L["key"] in verified_map else "probable",
                "confidence": "high" if L["key"] in verified_map else "medium",
                "publish_eligible": write_price,
                "exclusion_reason": excl,
            }
        e = jp_entry(markets, iid)
        existed = e is not None
        if not e:
            # New JP records are created only from the separately reviewed
            # mapping file.  Auto/count joins may refresh existing records but
            # can never invent a price for an ambiguous printing.
            if L["key"] not in verified_map or iid not in valid_card_ids:
                continue
            if STRICT and not write_price:
                # Identity binds, but publication policy fails: stateful
                # no-price entry only (e.g. under_review, verified-source-OOS).
                state_stamps[iid] = (mstate, mreason)
                matched += 1; seen_this_run.add(iid)
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
        if STRICT and not write_price:
            is_verified = L["key"] in verified_map
            if not L["in_stock"] and old > 0:
                # Founder item-1/3 ruling (2026-07-24): mutually exclusive
                # OOS classification. Price equality is NOT provenance.
                prov = instock_provenance.get((iid, L["key"]))
                if prov and prov[0] == old and stale_age_ok(prov[1]):
                    # auditable in-stock observation backs this value AND is
                    # within the 7-day retention window
                    state_stamps[iid] = ("stale", "verified_instock_provenance")
                elif prov and prov[0] == old:
                    strict_removals.append({
                        "id": iid, "action": "removed",
                        "previous_value": e.get("source_price"),
                        "previous_last_updated": e.get("last_updated"),
                        "category": "stale_expired",
                        "reason": "stale_expired_no_recent_instock"})
                    updated += 1
                    state_stamps[iid] = ("unavailable", "stale_expired_no_recent_instock")
                elif iid in approved_legacy:
                    state_stamps[iid] = ("stale", "founder_approved_legacy_reference")
                else:
                    strict_removals.append({
                        "id": iid, "action": "removed",
                        "previous_value": e.get("source_price"),
                        "previous_last_updated": e.get("last_updated"),
                        "category": ("verified_identity_supersedes_legacy_price"
                                     if is_verified else
                                     "legacy_price_unknown_provenance"),
                        "reason": mreason})
                    updated += 1
                    state_stamps[iid] = ("unavailable",
                                         "no_auditable_instock_provenance"
                                         if not is_verified else mreason)
            else:
                # In-stock but policy-failed (under_review) — applies to ANY
                # mapping method: the publication gate is general policy, so
                # an existing price stops being publishable until approved.
                if old > 0:
                    strict_removals.append({
                        "id": iid, "action": "removed",
                        "previous_value": e.get("source_price"),
                        "previous_last_updated": e.get("last_updated"),
                        "category": ("verified_identity_supersedes_legacy_price"
                                     if is_verified else "high_value_pending_review"),
                        "reason": mreason})
                    updated += 1
                state_stamps[iid] = (mstate, mreason)
            continue
        if L["price_jpy"] != old or e.get("last_updated") != TODAY:
            e["source_price"] = L["price_jpy"]
            e["converted_price"] = round(L["price_jpy"] * fx)
            e["conversion_rate_used"] = fx
            e["last_updated"] = TODAY
            updated += 1
        if STRICT:
            e["market_state"] = "live"
            e["state_reason"] = None
            e["verified_in_stock_at"] = TODAY
            e["qualifying_source_count"] = 1

    # STRICT: apply deferred state stamps (unavailable/under_review strip the
    # price; stale keeps the retained value and only labels it).
    if STRICT:
        for iid, (mstate, mreason) in state_stamps.items():
            e = jp_entry(markets, iid)
            if e is None:
                e = {"source_market": "JP", "source_name": "Yuyu-Tei",
                     "source_currency": "JPY"}
                markets.setdefault(iid, []).append(e)
            if mstate == "stale" and e.get("source_price"):
                e["market_state"] = "stale"
                e["state_reason"] = mreason
            else:
                for f in ("source_price", "converted_price", "conversion_rate_used",
                          "last_updated"):
                    e.pop(f, None)
                # a price-less entry can never be "stale" — stale means a
                # retained last-known-good price is being displayed
                e["market_state"] = mstate if mstate != "stale" else "unavailable"
                e["state_reason"] = mreason
                e["verified_in_stock_at"] = None
                e["qualifying_source_count"] = 0
    # ── STRICT tombstone value-removal (founder step-5 ruling 2026-07-21) ────
    # A tombstoned id's publishable JP price is removed from the CANDIDATE
    # (mirror follows at promotion since it is regenerated from the canonical).
    # Card master, history, research evidence and past trend files are never
    # touched — this hides nothing retroactively, it stops publishing a value
    # known to be wrong. Idempotent: already-absent prices log a no-op. Only
    # ids in the validated tombstone list are ever removed, and the gate's
    # snapshot rollback covers these removals like any other candidate change.
    tombstone_removals = []
    if STRICT:
        for tid, treason, tcat, tstate_reason in (
            [(t, raw_tombs.get(t, "unspecified"), "tombstone", None)
             for t in sorted(tombstones)]
            + [(h, holds[h], "hold", "correct_verified_listing_outside_current_fetch_scope")
               for h in sorted(holds)]):
            e = jp_entry(markets, tid)
            if e is None or not e.get("source_price"):
                tombstone_removals.append({"id": tid, "action": "no-op",
                                           "category": tcat,
                                           "note": "no publishable JP price present",
                                           "reason": treason})
            else:
                tombstone_removals.append({"id": tid, "action": "removed",
                                           "category": tcat,
                                           "previous_value": e.get("source_price"),
                                           "previous_last_updated": e.get("last_updated"),
                                           "reason": treason})
                updated += 1
            # Stateful no-price entry: the site shows "JP price unavailable"
            # while state/reason keep the record honest (nothing is hidden —
            # evidence and history remain untouched).
            if e is None:
                e = {"source_market": "JP", "source_name": "Yuyu-Tei",
                     "source_currency": "JPY"}
                markets.setdefault(tid, []).append(e)
            for f in ("source_price", "converted_price", "conversion_rate_used",
                      "last_updated"):
                e.pop(f, None)
            e["market_state"] = "unavailable"
            e["state_reason"] = tstate_reason or ("tombstone: " + treason)
            e["verified_in_stock_at"] = None
            e["qualifying_source_count"] = 0
        for r in tombstone_removals:
            print(f"  STRICT {r['category']}: {r['id']} -> {r['action']}"
                  + (f" (was ¥{r['previous_value']:,} @ {r['previous_last_updated']})"
                     if r["action"] == "removed" else ""))
        for r in strict_removals:
            print(f"  STRICT removal [{r['category']}]: {r['id']} "
                  f"(was ¥{r['previous_value']:,} @ {r['previous_last_updated']})")

    # ── STRICT unseen-legacy classification (founder item-3, 2026-07-24) ─────
    # Full (non-staged) runs only: every JP entry the fetch did NOT observe
    # this run must still end with an honest market state — an old number may
    # not keep looking live because the scope missed it. Stale requires
    # auditable in-stock provenance, exactly like the observed path.
    legacy_sweep = []
    if STRICT and not stage_sel:
        prov_prices = {}          # id -> {price: observed_at}
        for (pid, _k), (price, at) in instock_provenance.items():
            prov_prices.setdefault(pid, {})[price] = at
        mapped_targets = set(cid_map.values())
        processed = seen_this_run | tombstones | set(holds) | set(state_stamps)
        for iid in list(markets.keys()):
            if iid in processed:
                continue
            e = jp_entry(markets, iid)
            if e is None:
                continue
            old = float(e.get("source_price") or 0)
            if old <= 0:
                continue                      # already price-less / stateful
            reason = ("no_current_source_observation" if iid in mapped_targets
                      else "correct_source_outside_current_fetch_scope")
            prov_at = prov_prices.get(iid, {}).get(old)
            if prov_at is not None and stale_age_ok(prov_at):
                e["market_state"] = "stale"
                e["state_reason"] = reason
                legacy_sweep.append({"id": iid, "outcome": "stale_retained",
                                     "value": old, "reason": reason})
                continue
            if prov_at is not None:
                reason = "stale_expired_no_recent_instock"
            legacy_sweep.append({"id": iid, "outcome": "removed",
                                 "previous_value": e.get("source_price"),
                                 "previous_last_updated": e.get("last_updated"),
                                 "category": "legacy_unseen_no_provenance",
                                 "reason": reason})
            for f in ("source_price", "converted_price", "conversion_rate_used",
                      "last_updated"):
                e.pop(f, None)
            e["market_state"] = "unavailable"
            e["state_reason"] = reason
            e["verified_in_stock_at"] = None
            e["qualifying_source_count"] = 0
            updated += 1
        if legacy_sweep:
            n_rm = sum(1 for x in legacy_sweep if x["outcome"] == "removed")
            print(f"  STRICT legacy sweep: {len(legacy_sweep)} unseen entries "
                  f"classified ({n_rm} removed, {len(legacy_sweep) - n_rm} stale-retained)")

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
        "tombstone_removals": tombstone_removals,
        "strict_removals": strict_removals,
        "legacy_sweep": legacy_sweep,
        "observations_recorded": len(observations),
    }

    # ── guards ────────────────────────────────────────────────────────────────
    # STRICT fail-loud identity validation: for every verified key whose source
    # page WAS fetched this run, the listing must exist and its card number
    # must equal the target variant's number. Never a silent fallback to the
    # automatic mapper — a violation rejects the whole candidate.
    verified_violations = []
    if STRICT:
        listing_by_key = {L["key"]: L for L in listings}
        for key, iid in verified_map.items():
            slug = key.split("/", 1)[0]
            if slug not in ok_slugs:
                continue                      # page not fetched (staged run) — no judgment
            L = listing_by_key.get(key)
            exp_num = iid.split("_")[0].upper()
            if L is None:
                verified_violations.append(f"{key}->{iid}: listing disappeared from source")
            elif L["card_number"].upper() != exp_num:
                verified_violations.append(
                    f"{key}->{iid}: identity mismatch (listing is {L['card_number']})")
    report["verified_violations"] = verified_violations
    if verified_violations:
        report["aborted"] = True
        report["abort_reason"] = ("verified-map identity validation failed: "
                                  + "; ".join(verified_violations[:5]))
    elif ratio < MIN_MATCH_RATIO:
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
    # All outputs go to the work dir OUTSIDE the git tree. The gate promotes
    # them into tracked paths only after every hard check passes.
    os.makedirs(JP_RUN_DIR, exist_ok=True)
    report["run_id"] = RUN_ID
    atomic_write(CANDIDATE_MAP_PATH, mp)
    if STRICT:
        # Source-evidence candidate: latest observation per key merged over the
        # tracked evidence file (layer A of the v2 model). Promoted by the gate.
        ev_doc = load_json(EVIDENCE_PATH, {"_meta": {"schema": 1}, "observations": {}})
        ev_doc.setdefault("observations", {}).update(observations)
        ev_doc["_meta"] = {"schema": 1, "updated": TODAY, "run_id": RUN_ID,
                           "retention": "latest observation per card|source|listing key"}
        atomic_write(CANDIDATE_EVIDENCE_PATH, ev_doc)
    atomic_write(WORK_REPORT_PATH, report)

    if report["aborted"]:
        # Expected business rejection: structured report for the gate, exit 0.
        # (Unexpected exceptions still propagate non-zero — never swallowed.)
        print(f"  BUSINESS REJECTION: {report['abort_reason']} — no candidate written")
        return
    if DRY_RUN:
        print("  DRY RUN: no candidate written"); return

    mk.setdefault("_meta", {}).setdefault("fx", {})["jpy_to_thb"] = fx
    mk["_meta"]["jp_prices_updated"] = TODAY
    mk["_meta"]["jp_price_source"] = ACTIVE.name
    mk["_meta"]["run_id"] = RUN_ID
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
    atomic_write(CANDIDATE_PATH, mk)
    with open(CANDIDATE_PATH, "rb") as f:
        report["candidate_sha256"] = hashlib.sha256(f.read()).hexdigest()
    atomic_write(WORK_REPORT_PATH, report)
    print(f"  Candidate written to work dir: {updated} entries refreshed "
          f"(fx {fx} via {fx_src}) run_id={RUN_ID}")
    print("  Tracked files NOT touched — promotion is the gate's job")


if __name__ == "__main__":
    main()
