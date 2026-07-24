# PLAN: History Reconciler — consolidate market-history import pipelines

Status: **READY TO IMPLEMENT — design agreed with Caleb 2026-07-24.**
Supersedes the parked 2026-07-23 draft. All open decisions from that draft are resolved
below except the two listed under "Still open (small)".

## Decisions locked 2026-07-24

- **Scanner is ALWAYS the priority.** Its window blocks; everything else yields.
- **Blocking scanner window: 60 days** (was 30). Gives leading-indicator headroom
  (60-day minimum for trend pairs) without waiting on deep history.
- **Single database.** The split-DB alternative (rolling 60-day scanner DB) was
  considered and rejected: it still needs verification on the 3-year side, doubles
  import work, and forces reader routing churn. Priority is delivered by *ordering*
  (scanner window imports first), not isolation.
- **Stock market verifies on access, not at launch.** When a stock feature needing
  deep history runs, ledger-check the 3-year range and backfill in background.
- **Trailing re-import (NEW, verified defect D6 below):** everef files keep growing
  for ~2–3 days after first publication. Every update pass re-fetches and re-imports
  any date younger than 5 days. Ledger tracks provisional→final per date.
- **Stock-market aspects DEFERRED (Caleb 2026-07-24):** D7 on-access verify and the
  3-year deep backfill are left for a later chunk — that side is mostly partitioned
  anyway. This chunk delivers the ledger, coordinator, scanner window, and top-ups.
  The writer lock still wraps the stockmarket dialog's import path (that's D3 safety,
  not a stock feature).
- **Debug instrumentation is a first-class requirement** — Caleb: "these parts are
  super critical... we need debug code everywhere on this." See "Instrumentation"
  section; it is part of every stage's definition of done, not a follow-up.

## The problem (verified against source 2026-07-23, DB evidence added 2026-07-24)

Two independently built download+import pipelines write to `market_history.db`:

1. **System 1 — `run_daily_update_background`** (`history/daily_update.py`), every launch,
   background, silent. Computes missing dates via `get_missing_dates_from_db()` — walks
   **forward from the single latest date only**. Uncapped range. No-ops on empty DB.
2. **System 2 — scanner setup gate** (`gui/gui_migration.py`): `check_has_recent_data()`
   gate fires from `_start_scan` (`gui_main_scan.py:134`, manual scans only). On failure,
   `ScannerSetupDialog` runs its **own duplicate** download+import of the trailing 30 days
   (`SCANNER_MIN_DAYS`), via a real set-diff (`get_scanner_missing_dates`).

### Verified defects

- **D1 — writer race.** After >7-day absence both systems fetch/import the same files
  concurrently. SQLite WAL = one writer; `import_file()` holds a per-file txn far longer
  than the 5 s default connect timeout → the losing thread logs `database is locked` and
  skips the file. Observed 2026-07-22/23 (28-day gap, 26 files, both threads ~half-failed;
  survived only because failure sets didn't overlap). Modal dialog looks hung throughout.
  *Caleb 2026-07-24: only bites after ~7+ day absences; dormant day-to-day.*
- **D2 — permanent interior holes.** System 1 never looks behind the latest date; its
  "will retry later" docstring is false. System 2 is hole-aware but only trailing-window
  and only when the gate fails — and the gate tests staleness + endpoint span only, so
  fresh data with an interior hole passes. Any hole >~1 week old is invisible forever.
  D1 manufactures D2 candidates (both threads failing the same file = permanent hole).
- **D3 — third writer.** Stock Market import dialog
  (`gui/stockmarket/gui_stockmarket_dialogs.py:345`) calls `db.import_archive()` on the
  main DB from its own thread. Coordination must wrap this path too. Cross-process
  risk is real (multiple checkouts share one AppData; no single-instance guard).
- **D4 — non-atomic downloads.** `daily_update.py:100` and `archive_downloader.py:345`
  write directly to the final filename; `daily_update.py:78` treats any existing file as
  complete. A truncated decompressed CSV imports partial rows silently.
- **D5 — false completion.** `import_file()` returns 0 on failure; callers sum and report
  success. `ScannerSetupDialog` closed "successfully" while 10/26 of its files had failed.
- **D6 — young everef files are permanently short (verified 2026-07-24).** Everef keeps
  appending rows to a date's file for ~2–3 days after first publication; nothing in the
  code ever re-fetches an imported date. Measured against the live DB:
  | date | DB rows | everef file now | short |
  |------|---------|-----------------|-------|
  | 2026-07-19 | 53,679 | 53,685 | ~0 (mature = complete; import itself is fine) |
  | 2026-07-20 | 47,131 | 49,331 | 2,200 (4.5%) |
  | 2026-07-21 | 46,090 | 48,536 | 2,446 (5%) |
  `EVEREF_LAG_DAYS = 2` keeps the modern gap to ~5%, but any historical import that ever
  grabbed a lag-0/1 file (e.g. the 3-year bulk path) left days that are 50–95% short,
  frozen forever. No prior "redownload the last few days" fix ever landed — grep confirms
  nothing re-fetches an existing date anywhere.
- **D7 — no stock-market verification.** `check_has_full_history`
  (`gui_migration.py:229`) tests ONLY that the earliest date is ≥3×365 days old. No
  staleness, no holes, no row counts. One row from 2023 satisfies it forever.
- **Perf landmine:** `get_imported_dates()` (`market_history.py:256`) is
  `SELECT DISTINCT date` with **no date-leading index** (PK is type_id,region_id,date;
  secondaries (region_id,date) and (type_id,region_id)) — full scan on 9 GB. Cannot be
  made hot-path as-is. The ledger replaces it; it runs exactly once more, at bootstrap.

### Reader requirements (traced 2026-07-24 — drives the window sizes)

- **Scanner pipeline: ≤30 days.** Everything flows through
  `get_history_for_hub(days=30)`; `parse_history_stats` uses 30d/7d windows; demand and
  cross-hub checks consume the same fetch. Industry tab: 7d/30d. Boosters: ≤30d.
- **Leading indicators** (stock-market tab only — burst/coldstart/hub_filters/compute):
  60-day hard minimum, 365-day compression/regime baselines, fetches `years=3`.
- **Material risk analysis** (stock-market filters): 180 days (90 recent vs 90–180
  baseline; the 365-day constant is defined but unused).
- **Graph popups** (`analytics/graphing.py:290`): fetch `years=4` for the 3-Year tab.
  Decision: backfill target stays 3 years; charts show whatever exists beyond that.
- **Stock profile extraction:** 3 years.

## Solution: one `HistoryReconciler`

New `history/history_reconciler.py`. ALL import paths route through one writer
coordinator. Components:

1. **`history_days` ledger table** — one row per date:
   `date, status, row_count, attempts, next_retry, first_imported_at, finalized_at, error`.
   Status: `complete_provisional` / `complete_final` / `partial` / `missing` /
   `unavailable_404`. A date is marked complete **in the same transaction** as its rows.
   Hole detection becomes O(days) (~1,100 rows for 3y) — cheap enough to run every
   launch and on every stock-tab access. 404s get tombstones + backoff via `next_retry`.
2. **Provisional→final lifecycle (fixes D6).** A newly imported date is
   `complete_provisional`. Every update pass re-fetches + re-imports (INSERT OR REPLACE
   on the (type_id, region_id, date) PK — tops up in place) any provisional date younger
   than 5 days, logging the row-count delta. At age ≥5 days it flips to `complete_final`
   and is never touched again. (Measured 2026-07-24: age-2 files are ~95% complete,
   age-5 matches everef within 6 rows; 5-day window costs ~2–3 s/file in background.)
3. **Atomic validated downloads (fixes D4).** Download to `.part`, validate bz2
   integrity + CSV header, atomic rename; delete-and-refetch invalid cached files.
   Prerequisite for trusting the ledger.
4. **Hole-aware missing-date computation from the ledger** (replaces both
   `get_missing_dates_from_db` and hot use of `get_imported_dates`).
5. **Priority order (scanner ALWAYS first):**
   a. trailing 60-day window — blocking requirement, imports newest-first so the modal
      closes ASAP; b. trailing re-imports of provisional dates; c. deep backfill /
      hole-healing, background only, per-launch time/file budget; leftover backlog
      persists in the ledger for next launch.
6. **Writer coordination (fixes D1/D3):** one in-process lock wrapping ALL main-DB
   import entry points (System 1, System 2, stockmarket dialog). Waiters log who holds
   the lock. Cross-process lockfile deferred (see "Still open").
7. **Stock-market on-access verify (fixes D7):** `check_has_full_history` replaced by a
   ledger predicate (staleness + 3-year completeness + no partials). Called where the
   old check is called today (`gui_stockmarket_dialogs.py`, `_overlay.py`). Failure
   triggers background backfill via the reconciler, never a blocking modal, and the
   feature reports degraded-data status meanwhile.
8. **Thin progress dialog:** `ScannerSetupDialog` becomes a subscriber to the one
   running reconcile; closes only when the **ledger confirms** the 60-day predicate,
   not when "files were processed." Progress via `core/tk_queue.py` as usual.
9. **Deletes:** `get_missing_dates_from_db`, `run_daily_update`'s pipeline,
   `ScannerSetupDialog._run_download`'s duplicate pipeline, `check_has_full_history`'s
   earliest-date heuristic.

**Ledger bootstrap:** one final full `SELECT date, COUNT(*)` scan over the existing DB
seeds the ledger. Dates with plausible row counts → `complete_final` if ≥3 days old.
Row-count sanity threshold: mature days run ~46–54k rows (measured); seed anything
below ~60% of the trailing-90-day median as `partial` so it gets re-fetched. This
retroactively heals the old lag-0/1 gutted days from D6.

## Instrumentation (REQUIRED — part of every stage's DoD)

Caleb 2026-07-24: this subsystem is super critical; debug code everywhere. Per the
repo convention, these `[Tag]` prints are permanent until he says otherwise — do not
strip them in later cleanups. All go through the standard `print` → `eve_scout.log`
path. Tags: `[Reconciler]`, `[Ledger]`, `[HistDL]`.

Log every decision and every state change, with numbers:
- **Pass start/end:** trigger (launch / scan gate / stock access / manual), computed
  work list sizes per phase (scanner-window missing, provisional re-imports, backfill
  backlog), budget, and at end: per-phase done/failed/skipped counts + elapsed.
- **Gate evaluations:** every scanner/stock predicate check logs its inputs and verdict
  (`[Ledger] scanner-60d: 58/60 complete, missing=[2026-07-22, 2026-07-23] → BLOCKED`).
- **Per-file:** download start/bytes/duration, validation result, import row count,
  ledger transition (`missing → complete_provisional (46,090 rows)`).
- **Re-imports (D6):** old count → new count delta explicitly
  (`[Reconciler] top-up 2026-07-21: 46,090 → 48,536 (+2,446) age=2d, stays provisional`).
- **Finalization:** every provisional→final flip with age and final count.
- **Lock:** acquire/release with holder name; any waiter logs who it waited on and
  how long. This is the D1 tripwire — a wait >5 s is the old race showing up again.
- **Failures:** full error + the ledger state written (partial/missing + next_retry),
  never a bare pass/skip. D5's silent-failure path must be impossible to reproduce
  without a loud log line.
- **Bootstrap:** total dates seeded per status, list of dates flagged partial by the
  row-count threshold.

## Staging

- **Stage 1:** ledger (+bootstrap) + atomic downloads + reconciler core + in-process
  coordinator wrapping all 3 writer paths + full instrumentation. D1–D5 dead.
- **Stage 2:** provisional/final lifecycle + trailing 3-day re-import (D6) +
  newest-first ordering + backfill budget + stock on-access predicate (D7).
- **Stage 3:** synthetic-DB replay tests (pattern of `test_inventory_replay.py`,
  IDE-run — no CLI pytest on this machine). Scenarios: simultaneous callers, interior
  hole, 300-day absence, interrupted download, 404 backoff, import rollback,
  manual-import contention, scanner-unblocks-before-deep-backfill, young-file top-up,
  bootstrap partial-flagging.
- **Cut for now:** cross-process lockfile (revisit if multi-checkout contention is
  ever actually observed in `eve_scout.log`).

## Still open (small)

- Per-launch backfill budget size (suggest: 10 files or 90 s, whichever first —
  confirm at Stage 2).
- Whether `EVEREF_LAG_DAYS` drops from 2 → 1 once trailing re-import exists (fresher
  scanner data at the cost of one extra provisional day; suggest yes, at Stage 2).

## Cleanup opportunity while in there

`gui/gui_migration.py` is over the 900-line hard limit; removing its duplicate pipeline
is the natural extraction. `daily_update.py` shrinks to downloader + shim.
