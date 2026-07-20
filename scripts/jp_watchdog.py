#!/usr/bin/env python3
"""JP freshness watchdog (PROPOSED — founder approval required before commit).

Independent of the update workflow by design: its job is to detect an update
workflow that never started, silently froze, or published inconsistent data.
READ-ONLY: fetches deployed production files over HTTPS and reads the Actions
API; never touches canonical, mirror, history, trend or mapping files.

Fails (red workflow -> GitHub failure notification) when ANY of:
  W1 deployed canonical missing or malformed
  W2 deployed status.json missing/malformed or jp block absent
  W3 published newest JP refresh older than 36 hours
  W4 status.json claims a newer published refresh than the canonical contains
  W5 no successful scheduled update run within the last 26 hours
"""
import datetime
import json
import os
import sys
import urllib.request

# Test hooks (never set in the workflow): URL/time injection for fixtures.
BASE = os.environ.get("JP_WATCHDOG_BASE",
                      "https://fourthlps.github.io/tcgcardcatalog/onepiece-catalog")
CANONICAL_URL = f"{BASE}/card-markets.json"
STATUS_URL    = f"{BASE}/data/status.json"
RUNS_API = os.environ.get("JP_WATCHDOG_RUNS_API",
    "https://api.github.com/repos/fourthlps/tcgcardcatalog/actions/"
    "workflows/update-prices.yml/runs?event=schedule&per_page=5")
TIMEOUT = int(os.environ.get("JP_WATCHDOG_TIMEOUT", "60"))

MAX_AGE_HOURS = 36
RUN_WINDOW_HOURS = 26

errors = []

def fetch_json(url, what):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "voyage-jp-watchdog"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except Exception as e:                                    # noqa: BLE001
        errors.append(f"{what} missing/unreachable/malformed: {e}")
        return None

_now_override = os.environ.get("JP_WATCHDOG_NOW", "")
now = (datetime.datetime.fromisoformat(_now_override)
       if _now_override else datetime.datetime.now(datetime.timezone.utc))

canonical = fetch_json(CANONICAL_URL, "W1 deployed canonical")
newest = None
if canonical is not None:
    markets = canonical.get("markets")
    if not isinstance(markets, dict):
        errors.append("W1 deployed canonical malformed: no markets object")
    else:
        for entries in markets.values():
            if not isinstance(entries, list):
                continue
            for e in entries:
                if e.get("source_market") == "JP":
                    try:
                        d = datetime.date.fromisoformat(str(e.get("last_updated"))[:10])
                    except ValueError:
                        continue
                    if newest is None or d > newest:
                        newest = d
        if newest is None:
            errors.append("W1 deployed canonical contains no dated JP entries")

status = fetch_json(STATUS_URL, "W2 deployed status.json")
jp = (status or {}).get("jp")
if status is not None and not isinstance(jp, dict):
    errors.append("W2 status.json has no jp block")

if newest is not None:
    # Date-only timestamps: treat the entry as written at 20:30Z on its date
    # (the schedule time), the pipeline's actual write moment.
    written = datetime.datetime.combine(
        newest, datetime.time(20, 30), tzinfo=datetime.timezone.utc)
    age_h = (now - written).total_seconds() / 3600
    if age_h > MAX_AGE_HOURS:
        errors.append(f"W3 published JP data stale: newest {newest} is "
                      f"{age_h:.0f}h old (limit {MAX_AGE_HOURS}h)")

if isinstance(jp, dict) and newest is not None:
    claimed = str((jp.get("published") or {}).get("newest_jp_date") or "")[:10]
    if claimed and claimed > newest.isoformat():
        errors.append(f"W4 status claims newest {claimed} but canonical newest is {newest}")

runs = None
token = os.environ.get("GITHUB_TOKEN", "")
try:
    req = urllib.request.Request(RUNS_API, headers={
        "User-Agent": "voyage-jp-watchdog",
        **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        runs = json.load(r).get("workflow_runs", [])
except Exception as e:                                        # noqa: BLE001
    errors.append(f"W5 could not read update-workflow runs: {e}")
if runs is not None:
    ok = [r for r in runs
          if r.get("conclusion") == "success"
          and (now - datetime.datetime.fromisoformat(
              r["run_started_at"].replace("Z", "+00:00"))).total_seconds()
              <= RUN_WINDOW_HOURS * 3600]
    if not ok:
        errors.append(f"W5 no successful scheduled update run in the last "
                      f"{RUN_WINDOW_HOURS}h (schedule missed, failed, or frozen)")

if errors:
    for e in errors:
        print(f"::error title=JP freshness watchdog::{e}")
    sys.exit(1)
print(f"[jp_watchdog] OK — newest JP {newest}, scheduled run healthy")
