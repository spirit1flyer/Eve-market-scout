"""HistoryReconciler - the ONE pipeline that keeps market_history.db
correct (see PLAN_history_reconciler.md).

Replaces the two duplicate download+import pipelines (daily_update's
forward walk, ScannerSetupDialog's trailing window) with a single
ledger-driven pass:

    Phase A (blocking priority): trailing 60-day scanner window -
            fetch/import anything missing or partial, newest first.
            The scanner-gate dialog closes the moment the LEDGER says
            this window predicate passes, not when "files processed".
    Phase B: top-ups - re-fetch provisional dates younger than 5 days
            because everef keeps appending rows to young files (D6).
    Phase C: finalize aged provisionals.
    Phase D: rolling-horizon backfill (D7) - incomplete dates older
            than the scanner window but inside the HISTORY_YEARS
            horizon (legacy partial imports, long-absence gaps).
            Loops until the horizon is filled, yielding to scans
            between files (2026-08-01; was a 10-file/pass drip).
    Phase E: age-off - delete data past the horizon, mark it `pruned`
            in the ledger so Phase D never re-downloads it. One date
            per import-lock hold, loops until the overhang is gone.

Phases B-E yield to scans: the scan thread sets set_scan_active(),
and the pass finishes its current chunk (~15-40s) then waits. The
pass also publishes a live get_status() snapshot - the scanner-setup
dialog observes that instead of joining the pass queue (it closes the
moment the ledger predicate passes), and the main window shows it in
the top-right status label.

Downloads are atomic (D4): .part file, full bz2-decompress validation,
then rename. Imports run under history.import_lock (D1/D3) and are
verified by MEASURED post-commit row counts, never the importer's
return value (D5).

Single-flight: one pass at a time per process; a second caller blocks
(loudly) until the first finishes, then runs its own pass - which is
nearly free because the ledger says there's nothing left to do.

Every decision logs under [Reconciler] / [HistDL]. Permanent debug -
do not strip (Caleb 2026-07-24).
"""

import asyncio
import bz2
import threading
import time
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional

from history import history_ledger as ledger
from history import import_lock
from history.market_history import MarketHistoryDB

DOWNLOAD_TIMEOUT = 60  # seconds per file

# Phase D chunk size per ledger query. NOT a session cap: the pass
# loops until compute_backfill comes back empty (failed dates drop out
# via retry backoff), yielding to scans between files, so a fresh
# 3-year fill completes in one session (Caleb 2026-08-01).
BACKFILL_CHUNK = 10

# Single-flight guard
_pass_lock = threading.Lock()

ProgressCb = Callable[[str, int, int], None]  # (status, current, total)


# =============================================================================
# Live status + scan-active flag (2026-08-01)
# =============================================================================

_status_lock = threading.Lock()
_status: Optional[dict] = None


def _status_update(**fields):
    """Merge fields into the live status snapshot (pass holder only)."""
    global _status
    with _status_lock:
        if _status is None:
            _status = {"trigger": "", "status": "", "current": 0,
                       "total": 0, "gate_settled": False,
                       "gate_ready": False, "gate_reason": "",
                       "fetched": 0, "failed": 0}
        _status.update(fields)


def _status_clear():
    global _status
    with _status_lock:
        _status = None


def get_status() -> Optional[dict]:
    """Live 'what is the reconciler doing right now' snapshot, or None
    when idle. Polled by the scanner-setup dialog (which closes on the
    ledger predicate, not on pass end) and the main-window status bar.
    """
    with _status_lock:
        return dict(_status) if _status else None


# Set by the scan thread (gui_main_scan._run_scan_thread) for the
# duration of a scan; phases B-E finish their current chunk and then
# wait on this so downloads/DB writes never compete with a scan.
_scan_active = threading.Event()


def set_scan_active(active: bool) -> None:
    if active:
        _scan_active.set()
    else:
        _scan_active.clear()


def _yield_to_scans(what: str) -> None:
    """Finish-current-chunk politeness: called between chunks; sleeps
    while a scan is running and logs the pause duration."""
    waited = 0.0
    while _scan_active.is_set():
        if waited == 0:
            print(f"[Reconciler] pausing before {what} - scan in progress")
        time.sleep(2.0)
        waited += 2.0
    if waited:
        print(f"[Reconciler] resuming {what} after {waited:.0f}s scan pause")


def get_archive_path() -> Path:
    from core.sound_manager import get_data_dir
    return get_data_dir() / "history-archive"


# =============================================================================
# Atomic download (D4)
# =============================================================================

async def _download_date_file(session, date_str: str) -> Path:
    """Download one everef daily file atomically. Returns the final
    decompressed .csv path.

    Steps: GET -> write .csv.bz2.part -> full decompress to .csv.part
    (this IS the validation; bz2 raises on truncation) -> atomic rename
    to .csv -> delete the .part/.bz2 intermediates.

    Raises FileNotFoundError on 404, RuntimeError on other failures.
    """
    year = date_str.split("-")[0]
    year_dir = get_archive_path() / year
    year_dir.mkdir(parents=True, exist_ok=True)

    csv_path = year_dir / f"market-history-{date_str}.csv"
    bz2_part = year_dir / f"market-history-{date_str}.csv.bz2.part"
    csv_part = year_dir / f"market-history-{date_str}.csv.part"

    url = (f"https://data.everef.net/market-history/{year}/"
           f"market-history-{date_str}.csv.bz2")
    t0 = time.perf_counter()
    import aiohttp
    async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as resp:
        if resp.status == 404:
            print(f"[HistDL] {date_str}: 404 (not published yet)")
            raise FileNotFoundError(f"404 for {date_str}")
        if resp.status != 200:
            print(f"[HistDL] {date_str}: HTTP {resp.status}")
            raise RuntimeError(f"HTTP {resp.status} for {date_str}")
        content = await resp.read()
    dl_s = time.perf_counter() - t0

    with open(bz2_part, "wb") as f:
        f.write(content)

    # Validate by fully decompressing; count lines while at it.
    t1 = time.perf_counter()
    lines = 0
    try:
        with bz2.open(bz2_part, "rb") as src, open(csv_part, "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                lines += chunk.count(b"\n")
                dst.write(chunk)
    except (OSError, EOFError) as e:
        # Truncated/corrupt payload - leave nothing behind
        for p in (bz2_part, csv_part):
            p.unlink(missing_ok=True)
        print(f"[HistDL] {date_str}: INVALID bz2 payload "
              f"({len(content):,} bytes): {e} - deleted .part files")
        raise RuntimeError(f"corrupt bz2 for {date_str}: {e}")

    # Atomic swap into place (replace handles a pre-existing stale .csv)
    csv_part.replace(csv_path)
    bz2_part.unlink(missing_ok=True)
    # A stale .csv.bz2 from the old non-atomic pipeline would shadow
    # confusion later; remove it so the fresh .csv is the only copy.
    (year_dir / f"market-history-{date_str}.csv.bz2").unlink(missing_ok=True)

    print(f"[HistDL] {date_str}: OK {len(content):,} bytes in {dl_s:.1f}s, "
          f"validated {lines:,} csv lines in "
          f"{time.perf_counter() - t1:.1f}s -> {csv_path.name}")
    return csv_path


async def _download_many(dates: List[str],
                         progress_cb: Optional[ProgressCb],
                         phase: str,
                         total_units: int,
                         done_offset: int) -> dict:
    """Download several dates. Returns {date_str: Path | Exception}."""
    import aiohttp
    from core.ssl_context import make_connector
    from core.config import ESI_USER_AGENT

    results: dict = {}
    async with aiohttp.ClientSession(
            connector=make_connector(),
            headers={"User-Agent": ESI_USER_AGENT}) as session:
        for i, d in enumerate(dates):
            if progress_cb:
                progress_cb(f"{phase}: downloading {d}...",
                            done_offset + i, total_units)
            try:
                results[d] = await _download_date_file(session, d)
            except Exception as e:
                results[d] = e
    return results


def _download_dates_sync(dates: List[str],
                         progress_cb: Optional[ProgressCb],
                         phase: str,
                         total_units: int,
                         done_offset: int) -> dict:
    """Run the async downloader on a private loop (worker-thread safe)."""
    if not dates:
        return {}
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_download_many(
            dates, progress_cb, phase, total_units, done_offset))
    finally:
        loop.close()


# =============================================================================
# Import one date with measured verification (D5)
# =============================================================================

def _csv_line_count(path: Path) -> Optional[int]:
    try:
        with open(path, "rb") as f:
            n = 0
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    return max(n - 1, 0)  # minus header
                n += chunk.count(b"\n")
    except OSError:
        return None


def _import_and_record(db: MarketHistoryDB, date_str: str,
                       csv_path: Path) -> bool:
    """Import a date's file, verify by measured count, update ledger.

    Returns True if the date ended up complete (provisional or final).
    """
    csv_rows = _csv_line_count(csv_path)
    t0 = time.perf_counter()
    with import_lock.acquire(f"reconciler:{date_str}"):
        try:
            reported = db.import_file(csv_path, region_filter=None)
        except Exception as e:
            print(f"[Reconciler] {date_str}: import raised: {e}")
            ledger.record_failure(db, date_str, "error", str(e))
            return False
    measured = ledger.measure_date_rows(db, date_str)
    print(f"[Reconciler] {date_str}: import reported {reported:,}, "
          f"measured {measured:,}, csv had "
          f"{csv_rows if csv_rows is not None else '?'} rows "
          f"({time.perf_counter() - t0:.1f}s)")

    if measured <= 0:
        # D5 in the wild: importer said fine (or 0), DB says empty
        ledger.record_failure(
            db, date_str, "error",
            f"post-import measured 0 rows (reported {reported})")
        return False

    status = ledger.record_import(db, date_str, measured,
                                  csv_rows=csv_rows)
    return status in ledger.COMPLETE_STATUSES


# =============================================================================
# The pass
# =============================================================================

def run_reconcile(db: Optional[MarketHistoryDB] = None,
                  progress_cb: Optional[ProgressCb] = None,
                  trigger: str = "unknown") -> dict:
    """Run one full reconcile pass (single-flight). Thread-safe; uses
    the calling thread's own DB connection.

    Returns a summary dict:
        scanner_ready (bool), scanner_reason (str), fetched (int),
        failed (int), topups (int), finalized (int), backfilled (int),
        pruned_rows (int), elapsed (float), waited_for_other_pass (bool)
    """
    if db is None:
        db = MarketHistoryDB()

    waited = False
    t_wait = time.perf_counter()
    if not _pass_lock.acquire(blocking=False):
        waited = True
        print(f"[Reconciler] pass '{trigger}': another pass is running - "
              f"waiting (its work will likely leave us nothing to do)")
        if progress_cb:
            progress_cb("Waiting for background update...", 0, 1)
        _pass_lock.acquire()
        print(f"[Reconciler] pass '{trigger}': proceeding after "
              f"{time.perf_counter() - t_wait:.1f}s wait")

    t0 = time.perf_counter()
    summary = {"trigger": trigger, "scanner_ready": False,
               "scanner_reason": "", "fetched": 0, "failed": 0,
               "topups": 0, "finalized": 0, "backfilled": 0,
               "pruned_rows": 0, "elapsed": 0.0,
               "waited_for_other_pass": waited}
    def _report(status: str, current: int = 0, total: int = 0):
        """Publish to the live snapshot AND the caller's progress_cb."""
        _status_update(trigger=trigger, status=status,
                       current=current, total=total)
        if progress_cb:
            progress_cb(status, current, total)

    try:
        print(f"[Reconciler] ===== pass start (trigger={trigger}) =====")
        _report("Checking history ledger...")
        ledger.init_ledger(db)
        if not ledger.bootstrap_ledger(db):
            # Empty DB, nothing seeded - the scanner window fetch below
            # still runs and populates both data and ledger.
            print("[Reconciler] ledger not bootstrapped (empty DB) - "
                  "window fetch will seed it")

        work = ledger.compute_work(db)
        need_fetch = work["need_fetch"]
        topups = work["topups"]
        total_units = len(need_fetch) + len(topups)
        done = 0

        # ---- Phase A: scanner window (blocking priority, newest first)
        if need_fetch:
            print(f"[Reconciler] phase A: {len(need_fetch)} scanner-window "
                  f"dates to fetch")
            dl = _download_dates_sync(need_fetch, _report,
                                      "Scanner window", total_units, done)
            for d in need_fetch:
                res = dl.get(d)
                if isinstance(res, Path):
                    _report(f"Importing {d}...", done, total_units)
                    if _import_and_record(db, d, res):
                        summary["fetched"] += 1
                    else:
                        summary["failed"] += 1
                elif isinstance(res, FileNotFoundError):
                    ledger.record_failure(db, d, "404", "everef 404")
                    summary["failed"] += 1
                else:
                    ledger.record_failure(db, d, "error", str(res))
                    summary["failed"] += 1
                done += 1
                _report(f"Scanner window: {d} done", done, total_units)
        else:
            print("[Reconciler] phase A: scanner window has nothing "
                  "to fetch")

        ready, reason = ledger.scanner_ready(db)
        summary["scanner_ready"] = ready
        summary["scanner_reason"] = reason
        # Gate is settled - the setup dialog observer acts on this
        # immediately; everything after is background maintenance.
        _status_update(gate_settled=True, gate_ready=ready,
                       gate_reason=reason,
                       fetched=summary["fetched"],
                       failed=summary["failed"])

        # ---- Phase B: top-ups of young provisional dates (D6)
        if topups:
            print(f"[Reconciler] phase B: {len(topups)} provisional "
                  f"top-ups (files everef may still be growing)")
            _yield_to_scans("top-up downloads")
            dl = _download_dates_sync(topups, _report,
                                      "Top-up", total_units, done)
            for d in topups:
                res = dl.get(d)
                if isinstance(res, Path):
                    _yield_to_scans(f"top-up import {d}")
                    _report(f"Top-up: importing {d}...", done, total_units)
                    if _import_and_record(db, d, res):
                        summary["topups"] += 1
                    else:
                        summary["failed"] += 1
                else:
                    # Top-up failure is not critical - the date stays
                    # provisional and we try again next pass.
                    print(f"[Reconciler] top-up {d} failed ({res}) - "
                          f"stays provisional, retry next pass")
                done += 1
                _report(f"Top-up: {d} done", done, total_units)
        else:
            print("[Reconciler] phase B: no top-ups due")

        # ---- Phase C: finalize aged provisionals
        summary["finalized"] = ledger.finalize_aged_provisionals(db)

        # ---- Phase D: rolling-horizon backfill (D7). The gate is
        # already settled, so this is pure background work: loop until
        # the 3-year horizon is filled, one file at a time, yielding
        # to scans between files. Failed dates drop out of
        # compute_backfill via retry backoff, so the loop terminates
        # even with everef down.
        first_batch = True
        while True:
            backfill = ledger.compute_backfill(db, BACKFILL_CHUNK)
            if not backfill:
                if first_batch:
                    print("[Reconciler] phase D: no backfill needed")
                break
            first_batch = False
            chunk_ok = 0
            for d in backfill:
                _yield_to_scans(f"backfill {d}")
                _status_update(status=f"Backfilling history {d} "
                                      f"({summary['backfilled']} done)...")
                dl = _download_dates_sync([d], None, "Backfill", 1, 0)
                res = dl.get(d)
                if isinstance(res, Path):
                    if _import_and_record(db, d, res):
                        summary["backfilled"] += 1
                        chunk_ok += 1
                    else:
                        summary["failed"] += 1
                elif isinstance(res, FileNotFoundError):
                    ledger.record_failure(db, d, "404", "everef 404")
                    summary["failed"] += 1
                else:
                    ledger.record_failure(db, d, "error", str(res))
                    summary["failed"] += 1
            if chunk_ok == 0:
                # Whole chunk failed (everef down?) - backoff excludes
                # these dates next query, but stop hammering now.
                print("[Reconciler] phase D: chunk had no successes - "
                      "stopping backfill this pass")
                break

        # ---- Phase E: age-off past the rolling horizon. One date per
        # import-lock hold (each ~10-20s on the full DB) so nothing
        # ever waits behind a multi-minute delete; loops until the
        # whole overhang is gone, yielding to scans between dates.
        while True:
            _yield_to_scans("prune")
            _status_update(status=f"Pruning aged history "
                                  f"({summary['pruned_rows']:,} rows "
                                  f"cleared)...")
            with import_lock.acquire("reconciler:prune"):
                dates_pruned, rows = ledger.apply_prune(db, limit=1)
            if not dates_pruned:
                break
            summary["pruned_rows"] += rows

        summary["elapsed"] = time.perf_counter() - t0
        print(f"[Reconciler] ===== pass end (trigger={trigger}): "
              f"ready={ready} fetched={summary['fetched']} "
              f"topups={summary['topups']} failed={summary['failed']} "
              f"finalized={summary['finalized']} "
              f"backfilled={summary['backfilled']} "
              f"pruned_rows={summary['pruned_rows']} "
              f"elapsed={summary['elapsed']:.1f}s =====")
        return summary
    except Exception as e:
        import traceback
        summary["elapsed"] = time.perf_counter() - t0
        summary["scanner_reason"] = f"pass crashed: {e}"
        print(f"[Reconciler] pass '{trigger}' CRASHED after "
              f"{summary['elapsed']:.1f}s: {e}")
        traceback.print_exc()
        return summary
    finally:
        _status_clear()
        _pass_lock.release()


def run_reconcile_background(db: MarketHistoryDB,
                             callback: Optional[Callable] = None,
                             trigger: str = "launch") -> threading.Thread:
    """Run a reconcile pass on a daemon thread with its own DB handle.

    Drop-in replacement for the old run_daily_update_background -
    `callback`, if given, receives the rows-fetched count like before.
    """
    def _run():
        print(f"[Reconciler] background thread starting "
              f"(trigger={trigger})")
        thread_db = MarketHistoryDB(db.db_path)
        thread_db.init_db()
        try:
            summary = run_reconcile(thread_db, trigger=trigger)
        finally:
            thread_db.close()
        if callback:
            callback(summary.get("fetched", 0) + summary.get("topups", 0))

    thread = threading.Thread(target=_run, daemon=True,
                              name=f"reconciler-{trigger}")
    thread.start()
    return thread


def check_scanner_ready(db: MarketHistoryDB, quiet: bool = False) -> bool:
    """Fast ledger-backed scanner gate (ms - a handful of PK lookups).

    Falls back to the legacy endpoint-span check when the ledger hasn't
    been bootstrapped yet (first launch before the background pass
    finishes) - logged loudly so the fallback is visible.

    quiet=True suppresses the per-call [Ledger] verdict line - for
    pollers (the setup dialog checks every 0.5s; logging each check
    would flood eve_scout.log).
    """
    try:
        if not ledger.is_bootstrapped(db):
            if not quiet:
                print("[Reconciler] gate: ledger not bootstrapped yet - "
                      "falling back to legacy span check")
            from history.history_checks import _legacy_check_has_recent_data
            return _legacy_check_has_recent_data(db)
        ready, _ = ledger.scanner_ready(db, quiet=quiet)
        return ready
    except Exception as e:
        print(f"[Reconciler] gate: predicate crashed ({e}) - "
              f"falling back to legacy span check")
        from history.history_checks import _legacy_check_has_recent_data
        return _legacy_check_has_recent_data(db)
