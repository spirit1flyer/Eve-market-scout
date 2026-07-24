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

Deep (3-year) backfill and the stock-market on-access verify are
DEFERRED to a later chunk (Caleb 2026-07-24).

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

# Single-flight guard
_pass_lock = threading.Lock()

ProgressCb = Callable[[str, int, int], None]  # (status, current, total)


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
        failed (int), topups (int), finalized (int), elapsed (float),
        waited_for_other_pass (bool)
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
               "topups": 0, "finalized": 0, "elapsed": 0.0,
               "waited_for_other_pass": waited}
    try:
        print(f"[Reconciler] ===== pass start (trigger={trigger}) =====")
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
            dl = _download_dates_sync(need_fetch, progress_cb,
                                      "Scanner window", total_units, done)
            for d in need_fetch:
                res = dl.get(d)
                if isinstance(res, Path):
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
                if progress_cb:
                    progress_cb(f"Scanner window: {d} done",
                                done, total_units)
        else:
            print("[Reconciler] phase A: scanner window has nothing "
                  "to fetch")

        ready, reason = ledger.scanner_ready(db)
        summary["scanner_ready"] = ready
        summary["scanner_reason"] = reason

        # ---- Phase B: top-ups of young provisional dates (D6)
        if topups:
            print(f"[Reconciler] phase B: {len(topups)} provisional "
                  f"top-ups (files everef may still be growing)")
            dl = _download_dates_sync(topups, progress_cb,
                                      "Top-up", total_units, done)
            for d in topups:
                res = dl.get(d)
                if isinstance(res, Path):
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
                if progress_cb:
                    progress_cb(f"Top-up: {d} done", done, total_units)
        else:
            print("[Reconciler] phase B: no top-ups due")

        # ---- Phase C: finalize aged provisionals
        summary["finalized"] = ledger.finalize_aged_provisionals(db)

        summary["elapsed"] = time.perf_counter() - t0
        print(f"[Reconciler] ===== pass end (trigger={trigger}): "
              f"ready={ready} fetched={summary['fetched']} "
              f"topups={summary['topups']} failed={summary['failed']} "
              f"finalized={summary['finalized']} "
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


def check_scanner_ready(db: MarketHistoryDB) -> bool:
    """Fast ledger-backed scanner gate (ms - a handful of PK lookups).

    Falls back to the legacy endpoint-span check when the ledger hasn't
    been bootstrapped yet (first launch before the background pass
    finishes) - logged loudly so the fallback is visible.
    """
    try:
        if not ledger.is_bootstrapped(db):
            print("[Reconciler] gate: ledger not bootstrapped yet - "
                  "falling back to legacy span check")
            from history.history_checks import _legacy_check_has_recent_data
            return _legacy_check_has_recent_data(db)
        ready, _ = ledger.scanner_ready(db)
        return ready
    except Exception as e:
        print(f"[Reconciler] gate: predicate crashed ({e}) - "
              f"falling back to legacy span check")
        from history.history_checks import _legacy_check_has_recent_data
        return _legacy_check_has_recent_data(db)
