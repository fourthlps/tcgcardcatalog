#!/usr/bin/env python3
"""JP validation gate + workflow enforcement (KI-40 hardening, stage 3).

Validation-BEFORE-push (founder ruling 2026-07-19): the fetch step writes a JP
candidate only to a work dir outside the git tree; this gate validates the
candidate and promotes it into tracked paths ONLY when every hard check
passes. A rejected candidate leaves every JP tracked file at its pre-run
state, EN updates still commit, the rejection is written to status.json, and
the post-push enforce step turns the workflow red.

  python scripts/jp_sentinel.py --gate     # after JP fetch, before commit:
                                           #   validate candidate; promote on
                                           #   pass (canonical, map, report,
                                           #   mirror, history, trends);
                                           #   restore snapshots on any
                                           #   promotion-phase failure; write
                                           #   the jp status block; exit 0
                                           #   (unexpected crashes stay !=0)
  python scripts/jp_sentinel.py --enforce  # after push: red unless the block
                                           #   is this run's and its
                                           #   promotion_state is promoted or
                                           #   skipped

Test hooks (never set in the workflow): JP_SENTINEL_TODAY pins the date;
JP_GATE_FAULT=mirror|history|trends|integrity injects promotion-phase faults.
"""

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import collections

MARKETS_PATH = "onepiece-catalog/card-markets.json"
MIRROR_PATH  = "onepiece-catalog/data/one-piece/prices.json"
MAP_PATH     = "onepiece-catalog/data/jp-yuyu-map.json"
REPORT_PATH  = "onepiece-catalog/data/jp-price-report.json"
HISTORY_PATH = "onepiece-catalog/data/jp-price-history.json"
GAINERS_PATH = "onepiece-catalog/data/jp-top-gainers.json"
LOSERS_PATH  = "onepiece-catalog/data/jp-top-losers.json"
STATUS_PATH  = "onepiece-catalog/data/status.json"

# Every tracked file the JP pipeline can produce. Snapshot before promotion,
# restore all of them on any promotion-phase failure so a rejected run leaves
# zero JP changes in the tree (EN edits to other files are untouched).
JP_TRACKED = [MARKETS_PATH, MIRROR_PATH, MAP_PATH, REPORT_PATH,
              HISTORY_PATH, GAINERS_PATH, LOSERS_PATH]

WORK_ROOT = (os.environ.get("JP_WORK_DIR") or os.environ.get("RUNNER_TEMP")
             or tempfile.gettempdir())
JP_RUN_DIR         = os.path.join(WORK_ROOT, "jp-run")
CANDIDATE_PATH     = os.path.join(JP_RUN_DIR, "card-markets.candidate.json")
CANDIDATE_MAP_PATH = os.path.join(JP_RUN_DIR, "jp-yuyu-map.candidate.json")
WORK_REPORT_PATH   = os.path.join(JP_RUN_DIR, "jp-fetch-report.json")
BACKUP_DIR         = os.path.join(JP_RUN_DIR, "pre-promotion-backup")

FRESH_MAX_AGE_DAYS = 1      # newest JP date may be today or yesterday (≈36h at 20:30Z)
COVERAGE_FLOOR     = 2000   # catastrophic absolute minimum refreshed count
COVERAGE_RATIO     = 0.85   # relative check: report-only until founder-approved
COVERAGE_DEFAULT_BASELINE = 2306

RUN_ID = os.environ.get("GITHUB_RUN_ID") or ""


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def atomic_write(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def today_utc():
    override = os.environ.get("JP_SENTINEL_TODAY", "")
    if override:
        return datetime.date.fromisoformat(override)
    return datetime.datetime.now(datetime.timezone.utc).date()


def parse_day(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def jp_stats(markets):
    """Published-side stats from a markets dict."""
    total, newest = 0, None
    by_day = {}
    for iid, entries in markets.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if e.get("source_market") != "JP":
                continue
            total += 1
            d = parse_day(e.get("last_updated"))
            if d and (newest is None or d > newest):
                newest = d
    stale = 0
    stale_by_set = collections.Counter()
    refreshed_by_set = collections.Counter()
    for iid, entries in markets.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if e.get("source_market") != "JP":
                continue
            d = parse_day(e.get("last_updated"))
            if d and newest and d < newest:
                stale += 1
                stale_by_set[iid.split("-")[0]] += 1
            elif d and newest and d == newest:
                refreshed_by_set[iid.split("-")[0]] += 1
    return {"newest": newest, "total": total, "stale": stale,
            "stale_by_set": dict(stale_by_set.most_common()),
            "refreshed_by_set": dict(refreshed_by_set.most_common())}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_script(name):
    r = subprocess.run([sys.executable, os.path.join("scripts", name)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        return f"{name} exited {r.returncode}: {' | '.join(tail)}"
    return None


def gate():
    today = today_utc()
    fault = os.environ.get("JP_GATE_FAULT", "")            # tests only
    dry_run    = os.environ.get("JP_DRY_RUN", "").lower() in ("1", "true", "yes")
    staged     = bool(os.environ.get("JP_SETS", "").strip())
    allow_skip = os.environ.get("JP_ALLOW_SKIP", "").lower() in ("1", "true", "yes")
    fetch_outcome = os.environ.get("JP_FETCH_OUTCOME", "success")

    status = load_json(STATUS_PATH, {})
    prev = status.get("jp") or {}
    reasons = []

    report = load_json(WORK_REPORT_PATH, None)
    report_state = "ok" if isinstance(report, dict) else (
        "missing" if not os.path.exists(WORK_REPORT_PATH) else "malformed")
    candidate = load_json(CANDIDATE_PATH, None)
    candidate_state = "ok" if isinstance(candidate, dict) and "markets" in candidate else (
        "missing" if not os.path.exists(CANDIDATE_PATH) else "malformed")

    skipped = dry_run or staged or allow_skip

    if not skipped:
        # ── C. pre-promotion hard checks ─────────────────────────────────────
        if fetch_outcome != "success":
            reasons.append(f"fetch step outcome: {fetch_outcome}")
        if report_state != "ok":
            reasons.append(f"fetch report {report_state}")
        else:
            if parse_day(report.get("run_date")) != today:
                reasons.append(f"report run_date {report.get('run_date')} is not today")
            if report.get("aborted"):
                reasons.append(f"business rejection: {report.get('abort_reason')}")
        if candidate_state != "ok":
            reasons.append(f"candidate {candidate_state}")
        if report_state == "ok" and candidate_state == "ok":
            rid_r = report.get("run_id")
            rid_c = (candidate.get("_meta") or {}).get("run_id")
            if rid_r != rid_c or (RUN_ID and rid_r != RUN_ID):
                reasons.append(f"run id mismatch (report={rid_r} candidate={rid_c} env={RUN_ID or '-'})")
            want = report.get("candidate_sha256")
            if not want or sha256_file(CANDIDATE_PATH) != want:
                reasons.append("candidate checksum mismatch")
        if report_state == "ok" and not report.get("aborted"):
            refreshed = report.get("updated", 0) + report.get("created", 0)
            if report.get("listings_seen", 0) == 0 or refreshed == 0:
                reasons.append("no usable relay results")
            if refreshed < COVERAGE_FLOOR:
                reasons.append(f"refreshed {refreshed} below catastrophic floor {COVERAGE_FLOOR}")
            if report.get("sets_failed", 0) > 0:
                reasons.append(f"{report['sets_failed']} set(s) failed")
            if report.get("sets_throttled", 0) > 0:
                reasons.append(f"{report['sets_throttled']} set(s) left incomplete by throttling")
        if candidate_state == "ok":
            cand_stats = jp_stats(candidate.get("markets", {}))
            age = (today - cand_stats["newest"]).days if cand_stats["newest"] else None
            if age is None or age > FRESH_MAX_AGE_DAYS:
                reasons.append(f"candidate newest JP date {cand_stats['newest']} too old (age {age}d)")
        # (verified-map identity rules attach here when that flag is enabled)

    # ── D/E. promotion with snapshot rollback ────────────────────────────────
    promotion_state = "skipped" if skipped else ("rejected" if reasons else "pending")
    if promotion_state == "pending":
        shutil.rmtree(BACKUP_DIR, ignore_errors=True)
        os.makedirs(BACKUP_DIR, exist_ok=True)
        for p in JP_TRACKED:
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(BACKUP_DIR, p.replace("/", "__")))
        try:
            shutil.copyfile(CANDIDATE_PATH, MARKETS_PATH)
            shutil.copyfile(CANDIDATE_MAP_PATH, MAP_PATH)
            atomic_write(REPORT_PATH, report)
            if fault == "mirror":
                raise RuntimeError("injected mirror fault")
            shutil.copyfile(MARKETS_PATH, MIRROR_PATH)   # machine-written mirror
            err = "injected history fault" if fault == "history" else run_script("append_jp_history.py")
            if err:
                raise RuntimeError(err)
            err = "injected trends fault" if fault == "trends" else run_script("compute_jp_trends.py")
            if err:
                raise RuntimeError(err)
            # final artifact-integrity check across everything that would commit
            for p in JP_TRACKED:
                if load_json(p, None) is None:
                    raise RuntimeError(f"integrity: {p} unparseable")
            if sha256_file(MIRROR_PATH) != sha256_file(MARKETS_PATH):
                raise RuntimeError("integrity: mirror does not match canonical")
            promoted_stats = jp_stats(load_json(MARKETS_PATH, {}).get("markets", {}))
            if promoted_stats["newest"] != today:
                raise RuntimeError("integrity: promoted canonical is not today's data")
            if fault == "integrity":
                raise RuntimeError("injected integrity fault")
            promotion_state = "promoted"
        except Exception as e:                     # noqa: BLE001 — rollback path
            reasons.append(f"promotion failed: {e}")
            for p in JP_TRACKED:
                b = os.path.join(BACKUP_DIR, p.replace("/", "__"))
                if os.path.exists(b):
                    shutil.copy2(b, p)
                elif os.path.exists(p):
                    os.remove(p)
            promotion_state = "rejected"
            print("  promotion failed — all JP tracked files restored to pre-run state")

    # ── status block: attempted vs published ─────────────────────────────────
    pub = jp_stats(load_json(MARKETS_PATH, {}).get("markets", {}))
    attempted_refreshed = (report.get("updated", 0) + report.get("created", 0)) \
        if isinstance(report, dict) else 0
    published_refreshed = attempted_refreshed if promotion_state == "promoted" else 0

    baseline = (prev.get("coverage") or {}).get("baseline") or COVERAGE_DEFAULT_BASELINE
    coverage_min = int(baseline * COVERAGE_RATIO)
    coverage_ok = published_refreshed >= coverage_min
    state_ok = promotion_state == "promoted" and coverage_ok
    block = {
        "attempted_run_id": (report or {}).get("run_id") if isinstance(report, dict) else None,
        "attempted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "workflow_run_id": RUN_ID or None,
        "fetch_outcome": fetch_outcome,
        "candidate_state": candidate_state,
        "promotion_state": promotion_state,
        "rejection_reasons": reasons,
        "attempted": {
            "listings_seen": (report or {}).get("listings_seen") if isinstance(report, dict) else None,
            "matched": (report or {}).get("matched") if isinstance(report, dict) else None,
            "refreshed": attempted_refreshed,
        },
        "published": {
            "newest_jp_date": pub["newest"].isoformat() if pub["newest"] else None,
            "refreshed_last_run": published_refreshed,
            "jp_entries_total": pub["total"],
            "stale_count": pub["stale"],
            "stale_by_set": pub["stale_by_set"],
            "refreshed_by_set": pub["refreshed_by_set"] if promotion_state == "promoted" else {},
        },
        "last_successful_run_id": (report or {}).get("run_id") if promotion_state == "promoted"
            else prev.get("last_successful_run_id"),
        "last_successful_refresh_at": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds") if promotion_state == "promoted"
            else prev.get("last_successful_refresh_at"),
        "coverage": {
            "floor_enforced": COVERAGE_FLOOR,
            "relative_mode": "report-only",
            "baseline": published_refreshed if state_ok else baseline,   # no-ratchet
            "relative_threshold": coverage_min,
            "relative_pass": coverage_ok,
        },
    }
    status["jp"] = block
    atomic_write(STATUS_PATH, status)
    print(f"[jp_gate] promotion_state={promotion_state} "
          f"attempted_refreshed={attempted_refreshed} "
          f"published_newest={block['published']['newest_jp_date']} "
          f"reasons={reasons or 'none'}")


def enforce():
    block = (load_json(STATUS_PATH, {}) or {}).get("jp") or {}
    state = block.get("promotion_state")
    if RUN_ID and block.get("workflow_run_id") != RUN_ID:
        print(f"::error title=JP gate::status block is not from this run "
              f"(gate crashed before writing status?) — treating as rejected")
        sys.exit(1)
    if state in ("promoted", "skipped"):
        print(f"[jp_gate] enforce: {state} — pass")
        return
    print(f"::error title=JP pipeline rejected::promotion_state={state}; "
          f"reasons: {'; '.join(block.get('rejection_reasons') or ['unknown'])} "
          f"(JP production data unchanged; EN updates preserved)")
    sys.exit(1)


if __name__ == "__main__":
    if "--enforce" in sys.argv:
        enforce()
    else:
        gate()
