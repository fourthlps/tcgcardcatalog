#!/usr/bin/env python3
"""JP freshness & coverage sentinel (KI-40 hardening, stage 2).

Runs in two phases around the workflow's commit step so that a failing
sentinel still lets the day's EN prices and its own status block reach
production before the run turns red:

  python scripts/jp_sentinel.py            # phase 1 (before commit):
                                           #   compute checks, merge a "jp"
                                           #   block into status.json, exit 0
  python scripts/jp_sentinel.py --enforce  # phase 2 (after commit):
                                           #   re-read the block, exit 1 when
                                           #   the state is a failing one

States: ok | warn | skipped | no_results | stale | sets_incomplete | low_coverage
  - no_results       relay fetch produced nothing usable today       -> FAIL
  - stale            newest JP entry is 2+ days old (~36h at the
                     20:30Z schedule, date-only price timestamps)    -> FAIL
  - sets_incomplete  one or more sets ended failed, or throttling
                     left a set incomplete (retries that succeed
                     and complete the set stay a mere warning)       -> FAIL
  - low_coverage     refreshed count under the catastrophic floor    -> FAIL
  - warn             refreshed below the RELATIVE coverage threshold
                     — REPORT-ONLY until founder-approved            -> pass
  - skipped          dry-run / staged JP_SETS / JP_ALLOW_SKIP runs   -> pass

Baseline rule (founder ruling 2026-07-19): the last-known-good baseline is
persisted only from a run whose final state is 'ok' — never from warning,
degraded, partial, skipped or failed runs, so it cannot ratchet downward.
"""

import json, os, sys, datetime, collections, tempfile

MARKETS_PATH = "onepiece-catalog/card-markets.json"
REPORT_PATH  = "onepiece-catalog/data/jp-price-report.json"
STATUS_PATH  = "onepiece-catalog/data/status.json"

FRESH_MAX_AGE_DAYS = 1      # newest JP date may be today or yesterday; 2+ days fails
COVERAGE_FLOOR     = 2000   # ENFORCED: catastrophic absolute minimum refreshed count
COVERAGE_RATIO     = 0.85   # report-only until OP14/OP15 land + 3 healthy runs
COVERAGE_DEFAULT_BASELINE = 2306   # 2026-07-18 run, used until a baseline is recorded

FAILING_STATES = ("no_results", "stale", "sets_incomplete", "low_coverage")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def atomic_write(path, obj):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def today_utc():
    override = os.environ.get("JP_SENTINEL_TODAY", "")   # tests only
    if override:
        return datetime.date.fromisoformat(override)
    return datetime.datetime.now(datetime.timezone.utc).date()


def parse_day(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def compute():
    today = today_utc()
    dry_run    = os.environ.get("JP_DRY_RUN", "").lower() in ("1", "true", "yes")
    staged     = bool(os.environ.get("JP_SETS", "").strip())
    allow_skip = os.environ.get("JP_ALLOW_SKIP", "").lower() in ("1", "true", "yes")

    report = load_json(REPORT_PATH, {})
    status = load_json(STATUS_PATH, {})
    prev   = status.get("jp") or {}

    mk = load_json(MARKETS_PATH, {})
    markets = mk.get("markets", {})
    total = 0
    newest = None
    by_day = collections.Counter()
    for iid, entries in markets.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if e.get("source_market") != "JP":
                continue
            total += 1
            d = parse_day(e.get("last_updated"))
            if d:
                by_day[d] += 1
                if newest is None or d > newest:
                    newest = d

    stale_count = 0
    stale_by_set = collections.Counter()
    if newest:
        for iid, entries in markets.items():
            if not isinstance(entries, list):
                continue
            for e in entries:
                if e.get("source_market") != "JP":
                    continue
                d = parse_day(e.get("last_updated"))
                if d and d < newest:
                    stale_count += 1
                    stale_by_set[iid.split("-")[0]] += 1

    report_is_todays = (parse_day(report.get("run_date")) == today
                        and not report.get("dry_run") and not report.get("aborted"))
    refreshed = (report.get("updated", 0) + report.get("created", 0)) if report_is_todays else 0
    age_days = (today - newest).days if newest else None

    # per-set counts of entries refreshed by today's run (for future per-set
    # thresholds; founder ruling 2026-07-19)
    refreshed_by_set = collections.Counter()
    if report_is_todays:
        for iid, entries in markets.items():
            if not isinstance(entries, list):
                continue
            for e in entries:
                if e.get("source_market") == "JP" and parse_day(e.get("last_updated")) == today:
                    refreshed_by_set[iid.split("-")[0]] += 1

    # Last-known-good baseline: carried forward untouched unless THIS run ends
    # 'ok' (never updated from warn/degraded/partial/skipped/failed runs).
    baseline = (prev.get("coverage_check") or {}).get("baseline") or 0
    if baseline <= 0:
        baseline = COVERAGE_DEFAULT_BASELINE
    coverage_min = int(baseline * COVERAGE_RATIO)
    coverage_ok = refreshed >= coverage_min

    # sets that ended failed, or that throttling left incomplete (a throttled
    # request that succeeded on retry lands in sets_success and stays a warning)
    sets_failed    = report.get("sets_failed") or 0
    sets_throttled = report.get("sets_throttled") or 0

    if dry_run or staged or allow_skip:
        state = "skipped"
    elif not report_is_todays or report.get("listings_seen", 0) == 0 or refreshed == 0:
        state = "no_results"
    elif age_days is None or age_days > FRESH_MAX_AGE_DAYS:
        state = "stale"
    elif sets_failed > 0 or sets_throttled > 0:
        state = "sets_incomplete"
    elif refreshed < COVERAGE_FLOOR:
        state = "low_coverage"
    elif not coverage_ok:
        state = "warn"   # relative threshold stays report-only until approved
    else:
        state = "ok"

    if state == "ok":
        baseline_out = refreshed          # this run becomes the last-known-good
    else:
        baseline_out = baseline           # never ratchet from a non-ok run

    block = {
        "pipeline_state": state,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "last_refresh": newest.isoformat() if newest else None,
        "age_days": age_days,
        "jp_entries_total": total,
        "refreshed_last_run": refreshed,
        "refreshed_by_set": dict(refreshed_by_set.most_common()),
        "stale_count": stale_count,
        "stale_by_set": dict(stale_by_set.most_common()),
        "fetch": {
            "listings_seen": report.get("listings_seen"),
            "matched": report.get("matched"),
            "unmapped_total": report.get("unmapped_total"),
            "sets_attempted": report.get("sets_attempted"),
            "sets_success": report.get("sets_success"),
            "sets_failed": report.get("sets_failed"),
            "sets_throttled": report.get("sets_throttled"),
        },
        "coverage_check": {
            "floor_enforced": COVERAGE_FLOOR,
            "relative_mode": "report-only",
            "baseline": baseline_out,
            "relative_threshold": coverage_min,
            "relative_pass": coverage_ok,
        },
    }
    status["jp"] = block
    atomic_write(STATUS_PATH, status)
    print(f"[jp_sentinel] state={state} last_refresh={block['last_refresh']} "
          f"refreshed={refreshed} total={total} stale={stale_count} "
          f"floor>={COVERAGE_FLOOR} relative {'OK' if coverage_ok else 'BELOW'} "
          f"(>= {coverage_min}, report-only) baseline={baseline_out}")
    return state


def enforce():
    state = (load_json(STATUS_PATH, {}).get("jp") or {}).get("pipeline_state")
    if state in FAILING_STATES:
        print(f"::error title=JP pipeline sentinel::JP price pipeline state is "
              f"'{state}' — see the jp block in data/status.json (KI-40 sentinel).")
        sys.exit(1)
    print(f"[jp_sentinel] enforce: state={state or 'missing'} — pass")


if __name__ == "__main__":
    if "--enforce" in sys.argv:
        enforce()
    else:
        compute()
