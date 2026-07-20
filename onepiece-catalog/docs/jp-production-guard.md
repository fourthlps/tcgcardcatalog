# JP Production Guard — Design & Verification Evidence

Status: ACTIVE on `main` since 2026-07-19 (`7ced0f6`; prev-status fix `8704868`;
independent freshness watchdog `308a49e`). Founder-accepted 2026-07-21.

## Design (validation-before-push)

The nightly JP stage can never write tracked files directly:

1. `scripts/update_jp_prices.py` fetches into `RUNNER_TEMP` only — candidate
   canonical, candidate map, and a structured report carrying `run_id` and the
   candidate's SHA-256. Business rejections (match-ratio / big-move guards)
   exit 0 with `aborted` in the report; unexpected exceptions stay non-zero.
   The workflow step uses `continue-on-error` so EN work is never discarded.
2. `scripts/jp_sentinel.py --gate` validates: fetch-step outcome, report and
   candidate presence + parseability, run-id consistency, checksum, usable
   result counts, freshness, catastrophic floor (2,000), set completion.
   Only on full pass it promotes canonical + map + report, regenerates the
   mirror, runs history + trends, and integrity-checks every JP artifact.
   ANY promotion-phase failure restores all seven JP tracked files from
   pre-promotion snapshots (snapshots, not git-HEAD, so same-run EN updates
   to shared files survive).
3. `status.json` gets a `jp` block separating ATTEMPTED from PUBLISHED data;
   a rejected candidate publishes `refreshed: 0` and never advances the
   published newest date. The previous block (last_successful_*, coverage
   baseline) is read from `git show HEAD:` because the EN step regenerates
   the working-tree file.
4. Post-push, `--enforce` turns the workflow red on any rejection or on a
   gate crash (status run-id mismatch).
5. `jp-freshness-watchdog.yml` (22:30 & 10:30 UTC + dispatch, read-only)
   independently checks the DEPLOYED site: canonical/status present and
   parseable, published age ≤ 36h, status-vs-canonical consistency, and a
   successful scheduled run within 26h. Inability to verify = red.

The seven JP-produced tracked files: `card-markets.json`,
`data/one-piece/prices.json` (machine-written mirror — never hand-edit),
`data/jp-yuyu-map.json`, `data/jp-price-report.json`,
`data/jp-price-history.json`, `data/jp-top-gainers.json`,
`data/jp-top-losers.json`.

## Verification evidence

**Promote path (production):** scheduled run
[29702628359](https://github.com/fourthlps/tcgcardcatalog/actions/runs/29702628359)
(2026-07-19 20:30 UTC) — candidate of 2,306 refreshed entries validated,
promoted, integrity-checked, committed; run green.

**Rejection path (controlled test):** branch `jp-gate-rejection-test`
(commit `d4640b8`, fault `JP_GATE_FAULT=integrity`; branch deleted after this
document — the fault-injection commit was never merged), run
[29766825738](https://github.com/fourthlps/tcgcardcatalog/actions/runs/29766825738)
(2026-07-20 18:13 UTC) — a real 2,306-entry candidate was promoted, the
injected integrity fault fired, all JP files rolled back, the run's commit
(`9879431`) contained only the seven EN-produced files plus `status.json`
(`promotion_state: "rejected"`, reason `promotion failed: injected integrity
fault`, `published.refreshed_last_run: 0`, published newest unchanged at
2026-07-19), the workflow finished red, and no GitHub Pages deployment was
triggered (Pages builds only from `main`).

Pre-run vs post-run blob hashes — byte-identical for all seven JP files:

| File | Blob (pre == post) |
|---|---|
| onepiece-catalog/card-markets.json | `c53e31df71b289f6b74cbfd71bc7b6cf7da70be2` |
| onepiece-catalog/data/one-piece/prices.json | `c53e31df71b289f6b74cbfd71bc7b6cf7da70be2` |
| onepiece-catalog/data/jp-yuyu-map.json | `937187c7214b4fa3be5f10682966a72a9ee0560f` |
| onepiece-catalog/data/jp-price-report.json | `ba4aa87771c5ad07a981c7fb314b026a17409bec` |
| onepiece-catalog/data/jp-price-history.json | `77503b55eed76eb1d08950edc2576451064aa5bb` |
| onepiece-catalog/data/jp-top-gainers.json | `33632c1b55f66e2649788d68399f5cad5abc7e60` |
| onepiece-catalog/data/jp-top-losers.json | `717d09c530c081b04a42b1c36681369da4802cea` |

**Sandbox matrices:** gate 15/15 (secrets missing, fetch death pre-report,
candidate missing/malformed, checksum mismatch, no results, failed set,
throttled-incomplete set, floor, mirror/history/trends fault rollback,
run-id mismatch, promotion, dry-run skip); watchdog 9/9 (happy path + eight
failure paths incl. API-unreachable → red); prev-status fix 2/2.
