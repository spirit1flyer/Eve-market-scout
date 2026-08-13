"""Per-date ledger for market_history.db - the source of truth for
"which days of history do we actually, verifiably have?"

Table `history_days` lives inside market_history.db (so it travels with
the data through the background full-import swap): one row per date.

Statuses:
    complete_final       - imported, >= FINALIZE_AGE_DAYS old; never touched again
    complete_provisional - imported, but everef may still be appending rows
                           to this date's file (D6); re-imported each pass
    partial              - rows exist but measured count is suspicious
                           (failed import, or a gutted lag-0/1 legacy import)
    missing              - download/import failed; retried with backoff
    unavailable_404      - everef says no file; tombstoned with backoff
    pruned               - intentionally deleted by the rolling-horizon
                           age-off; never re-downloaded

Row counts stored here are MEASURED (SELECT COUNT(*) after commit via
idx_date), never trusted from an importer's return value - that return
lies on failure (D5).

Hole detection over any window is O(rows in this table), replacing the
9 GB `SELECT DISTINCT date` scan (which now runs exactly once, at
bootstrap).

All decisions log under [Ledger]. These debug lines are permanent -
this subsystem is critical (Caleb 2026-07-24); do not strip them.
"""

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

# Everef publishes a date's file ~2 days behind, and keeps appending to
# it for a few days after that (measured 2026-07-24: age-2 file ~95%
# complete, age-5 complete to within 6 rows).
EVEREF_LAG_DAYS = 2
FINALIZE_AGE_DAYS = 5      # calendar age at which a date stops changing
SCANNER_WINDOW_DAYS = 60   # the blocking requirement (Caleb 2026-07-24)

# Rolling deep-history horizon: how far back the DB is kept populated
# (Phase D backfill) and past which data is aged off (prune). Backfill
# wants date >= horizon, prune deletes date < horizon - same constant,
# so the two can never fight over a date.
HISTORY_YEARS = 3

# Bootstrap: dates whose measured count is below this fraction of the
# all-dates median get seeded `partial` so they are re-fetched. Catches
# the legacy gutted lag-0/1 imports (500-5,000 rows vs ~50k mature).
PARTIAL_MEDIAN_FRACTION = 0.5

STATUS_FINAL = "complete_final"
STATUS_PROVISIONAL = "complete_provisional"
STATUS_PARTIAL = "partial"
STATUS_MISSING = "missing"
STATUS_404 = "unavailable_404"
STATUS_PRUNED = "pruned"
COMPLETE_STATUSES = (STATUS_FINAL, STATUS_PROVISIONAL)

BOOTSTRAP_META_KEY = "ledger_bootstrapped"


def available_date(today: Optional[date] = None) -> date:
    """Newest date everef should have a file for."""
    return (today or date.today()) - timedelta(days=EVEREF_LAG_DAYS)


def horizon_date(today: Optional[date] = None) -> date:
    """Oldest date we keep: backfill floor and prune cutoff."""
    return (today or date.today()) - timedelta(days=HISTORY_YEARS * 365)


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_ledger(db) -> None:
    """Create the history_days table if needed. Cheap, idempotent."""
    conn = db._get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history_days (
            date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry TEXT,
            first_imported_at TEXT,
            finalized_at TEXT,
            updated_at TEXT,
            error TEXT
        )
    """)
    conn.commit()


def is_bootstrapped(db) -> bool:
    return db.get_meta(BOOTSTRAP_META_KEY) == "1"


def bootstrap_ledger(db) -> bool:
    """Seed the ledger from the existing daily_history table.

    Runs the one-and-only full GROUP BY date scan (uses idx_date;
    background thread; logged with duration). Skips silently if already
    bootstrapped. If daily_history is empty, does nothing and leaves the
    bootstrap flag unset so a later pass retries after first import.

    Returns True if the ledger is usable (bootstrapped now or before).
    """
    import time as _t

    if is_bootstrapped(db):
        return True

    init_ledger(db)
    conn = db._get_conn()

    print("[Ledger] bootstrap: starting one-time date/count scan "
          "of daily_history (this can take a minute on the full DB)")
    t0 = _t.perf_counter()
    rows = conn.execute(
        "SELECT date, COUNT(*) FROM daily_history GROUP BY date"
    ).fetchall()
    scan_s = _t.perf_counter() - t0
    print(f"[Ledger] bootstrap: scan found {len(rows)} dates "
          f"in {scan_s:.1f}s")

    if not rows:
        print("[Ledger] bootstrap: daily_history is empty - deferring "
              "(flag stays unset, will retry after first import)")
        return False

    counts = sorted(r[1] for r in rows)
    median = counts[len(counts) // 2]
    partial_threshold = int(median * PARTIAL_MEDIAN_FRACTION)
    today = date.today()
    now = _now_iso()

    seeded = {STATUS_FINAL: 0, STATUS_PROVISIONAL: 0, STATUS_PARTIAL: 0}
    partial_dates: List[str] = []
    batch = []
    for date_str, count in rows:
        try:
            age = (today - date.fromisoformat(date_str)).days
        except ValueError:
            print(f"[Ledger] bootstrap: skipping garbage date "
                  f"'{date_str}' ({count} rows)")
            continue
        if count < partial_threshold:
            status = STATUS_PARTIAL
            partial_dates.append(f"{date_str}({count})")
        elif age >= FINALIZE_AGE_DAYS:
            status = STATUS_FINAL
        else:
            status = STATUS_PROVISIONAL
        seeded[status] += 1
        batch.append((date_str, status, count, now,
                      now if status == STATUS_FINAL else None))

    conn.executemany(
        "INSERT OR REPLACE INTO history_days "
        "(date, status, row_count, attempts, updated_at, finalized_at) "
        "VALUES (?, ?, ?, 0, ?, ?)", batch)
    conn.commit()
    db.set_meta(BOOTSTRAP_META_KEY, "1")

    print(f"[Ledger] bootstrap: seeded {len(batch)} dates - "
          f"{seeded[STATUS_FINAL]} final, "
          f"{seeded[STATUS_PROVISIONAL]} provisional, "
          f"{seeded[STATUS_PARTIAL]} partial "
          f"(median={median:,} rows, partial threshold "
          f"<{partial_threshold:,})")
    if partial_dates:
        print(f"[Ledger] bootstrap: partial (will re-fetch when in "
              f"scope): {', '.join(partial_dates[:20])}"
              + (f" ...+{len(partial_dates) - 20} more"
                 if len(partial_dates) > 20 else ""))
    return True


def get_rows(db, dates: List[str]) -> Dict[str, dict]:
    """Fetch ledger rows for specific dates. {} entries for absent ones."""
    if not dates:
        return {}
    conn = db._get_conn()
    qmarks = ",".join("?" * len(dates))
    out = {}
    for r in conn.execute(
            f"SELECT * FROM history_days WHERE date IN ({qmarks})", dates):
        out[r["date"]] = dict(r)
    return out


def record_import(db, date_str: str, measured_count: int,
                  csv_rows: Optional[int] = None) -> str:
    """Record an import outcome from a MEASURED post-commit row count.

    Classifies: partial if the DB got materially fewer rows than the CSV
    held (import died mid-file), else provisional/final by age.
    Returns the status written.
    """
    today = date.today()
    age = (today - date.fromisoformat(date_str)).days

    if measured_count <= 0:
        status = STATUS_MISSING
    elif csv_rows and measured_count < csv_rows * 0.98:
        status = STATUS_PARTIAL
    elif age >= FINALIZE_AGE_DAYS:
        status = STATUS_FINAL
    else:
        status = STATUS_PROVISIONAL

    now = _now_iso()
    conn = db._get_conn()
    prev = conn.execute(
        "SELECT row_count, first_imported_at FROM history_days "
        "WHERE date=?", (date_str,)).fetchone()
    first_at = (prev["first_imported_at"] if prev and
                prev["first_imported_at"] else now)
    conn.execute(
        "INSERT OR REPLACE INTO history_days "
        "(date, status, row_count, attempts, next_retry, "
        " first_imported_at, finalized_at, updated_at, error) "
        "VALUES (?, ?, ?, 0, NULL, ?, ?, ?, NULL)",
        (date_str, status, measured_count, first_at,
         now if status == STATUS_FINAL else None, now))
    conn.commit()

    prev_count = prev["row_count"] if prev else 0
    delta = measured_count - prev_count
    print(f"[Ledger] {date_str}: {prev_count:,} -> {measured_count:,} "
          f"rows ({'+' if delta >= 0 else ''}{delta:,}) age={age}d "
          f"csv={csv_rows if csv_rows is not None else '?'} "
          f"-> {status}")
    return status


def record_failure(db, date_str: str, kind: str, error: str) -> None:
    """Record a download/import failure with retry backoff.

    kind: '404' or 'error'. Backoff: 404 -> 6h * attempts (cap 48h);
    transient error -> 30min * attempts (cap 6h).
    """
    conn = db._get_conn()
    prev = conn.execute(
        "SELECT attempts, row_count, status, first_imported_at "
        "FROM history_days WHERE date=?", (date_str,)).fetchone()
    attempts = (prev["attempts"] if prev else 0) + 1

    if kind == "404":
        status = STATUS_404
        delay = min(timedelta(hours=6 * attempts), timedelta(hours=48))
    else:
        # Keep partial rows classified partial; a date with no rows is missing
        status = (STATUS_PARTIAL if prev and prev["row_count"] > 0
                  else STATUS_MISSING)
        delay = min(timedelta(minutes=30 * attempts), timedelta(hours=6))

    next_retry = (datetime.now() + delay).strftime("%Y-%m-%d %H:%M:%S")
    row_count = prev["row_count"] if prev else 0
    first_at = prev["first_imported_at"] if prev else None
    conn.execute(
        "INSERT OR REPLACE INTO history_days "
        "(date, status, row_count, attempts, next_retry, "
        " first_imported_at, finalized_at, updated_at, error) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (date_str, status, row_count, attempts, next_retry,
         first_at, _now_iso(), error[:300]))
    conn.commit()
    print(f"[Ledger] {date_str}: FAILURE ({kind}) attempt={attempts} "
          f"-> {status}, next_retry={next_retry}, error={error[:120]}")


def finalize_aged_provisionals(db) -> int:
    """Flip provisional dates aged >= FINALIZE_AGE_DAYS to final."""
    cutoff = (date.today()
              - timedelta(days=FINALIZE_AGE_DAYS)).isoformat()
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT date, row_count FROM history_days "
        "WHERE status=? AND date<=?",
        (STATUS_PROVISIONAL, cutoff)).fetchall()
    if not rows:
        return 0
    now = _now_iso()
    conn.executemany(
        "UPDATE history_days SET status=?, finalized_at=?, updated_at=? "
        "WHERE date=?",
        [(STATUS_FINAL, now, now, r["date"]) for r in rows])
    conn.commit()
    for r in rows:
        age = (date.today() - date.fromisoformat(r["date"])).days
        print(f"[Ledger] {r['date']}: provisional -> final "
              f"(age={age}d, {r['row_count']:,} rows)")
    return len(rows)


def scanner_window_dates(today: Optional[date] = None) -> List[str]:
    """The SCANNER_WINDOW_DAYS expected dates, newest first."""
    avail = available_date(today)
    return [(avail - timedelta(days=i)).isoformat()
            for i in range(SCANNER_WINDOW_DAYS)]


def compute_work(db) -> dict:
    """Compute this pass's window work from the ledger. Scanner window
    only - deep-history holes are Phase D's job (compute_backfill).

    Returns dict:
        need_fetch:   dates in the scanner window with no complete rows
                      (missing/partial/absent), newest first, retry-due only
        deferred:     window dates skipped because next_retry is in the future
        topups:       provisional dates younger than FINALIZE_AGE_DAYS whose
                      file everef may still be growing (skipped if already
                      re-imported today), newest first
    """
    today = date.today()
    window = scanner_window_dates(today)
    rows = get_rows(db, window)
    now_str = _now_iso()
    today_str = today.isoformat()

    need_fetch: List[str] = []
    deferred: List[str] = []
    for d in window:  # already newest first
        row = rows.get(d)
        if row and row["status"] in COMPLETE_STATUSES:
            continue
        if row and row["next_retry"] and row["next_retry"] > now_str:
            deferred.append(f"{d}({row['status']},retry@{row['next_retry']})")
            continue
        need_fetch.append(d)

    # Top-ups: any provisional younger than the finalize age, anywhere
    # in the ledger (normally inside the window anyway).
    cutoff = (today - timedelta(days=FINALIZE_AGE_DAYS)).isoformat()
    conn = db._get_conn()
    topups = []
    for r in conn.execute(
            "SELECT date, row_count, updated_at FROM history_days "
            "WHERE status=? AND date>? ORDER BY date DESC",
            (STATUS_PROVISIONAL, cutoff)):
        if r["date"] in need_fetch:
            continue  # already being fetched fresh
        if r["updated_at"] and r["updated_at"][:10] == today_str:
            print(f"[Ledger] top-up skip {r['date']}: already "
                  f"re-imported today ({r['updated_at']})")
            continue
        topups.append(r["date"])

    print(f"[Ledger] work: scanner-window need_fetch={len(need_fetch)} "
          f"{need_fetch[:10]}{'...' if len(need_fetch) > 10 else ''} | "
          f"deferred={deferred if deferred else 0} | "
          f"topups={topups}")
    return {"need_fetch": need_fetch, "deferred": deferred,
            "topups": topups}


def compute_backfill(db, limit: int) -> List[str]:
    """Phase D work: dates older than the scanner window but inside the
    HISTORY_YEARS horizon that are not complete, not pruned, and not in
    retry backoff - i.e. interior holes (legacy partial imports) and
    long-absence gaps (dates that aged out of the trailing window before
    a pass ever saw them). Never-attempted dates have no ledger row at
    all, so this enumerates the expected calendar rather than querying
    existing rows. Newest first, capped at `limit` per pass.
    """
    today = date.today()
    window_start = (available_date(today)
                    - timedelta(days=SCANNER_WINDOW_DAYS - 1))
    floor = horizon_date(today)
    conn = db._get_conn()
    rows = {r["date"]: r for r in conn.execute(
        "SELECT date, status, next_retry FROM history_days "
        "WHERE date >= ? AND date < ?",
        (floor.isoformat(), window_start.isoformat()))}
    now_str = _now_iso()

    out: List[str] = []
    skipped_backoff = 0
    d = window_start - timedelta(days=1)
    while d >= floor and len(out) < limit:
        ds = d.isoformat()
        row = rows.get(ds)
        if row is None:
            out.append(ds)
        elif row["status"] in COMPLETE_STATUSES or row["status"] == STATUS_PRUNED:
            pass
        elif row["next_retry"] and row["next_retry"] > now_str:
            skipped_backoff += 1
        else:
            out.append(ds)
        d -= timedelta(days=1)

    if out or skipped_backoff:
        print(f"[Ledger] backfill: {len(out)} dates queued (cap {limit})"
              f"{': ' + ', '.join(out[:10]) if out else ''}"
              f"{'...' if len(out) > 10 else ''}"
              f"{f' | {skipped_backoff} in backoff' if skipped_backoff else ''}")
    return out


# Default age-off cap per call: one date = one short transaction
# (~50k rows), so concurrent readers never sit behind a multi-minute
# delete. The reconciler calls with limit=1 in a loop until the
# overhang is gone (each date is ~10-20s of scattered-page deletes on
# the full DB - the 2026-08-01 startup hang was 30 of these in one
# uninterruptible import-lock hold).
PRUNE_DATES_PER_PASS = 30


def apply_prune(db, limit: int = PRUNE_DATES_PER_PASS) -> Tuple[int, int]:
    """Age-off: delete daily_history rows past the rolling horizon and
    mark their ledger rows `pruned` so backfill never re-downloads what
    was deliberately dropped. Freed pages are reused, so the DB file
    stops growing (no VACUUM - that would rewrite the multi-GB file).
    Returns (dates_pruned, rows_deleted).
    """
    import time as _t
    cutoff = horizon_date().isoformat()
    conn = db._get_conn()
    dates = [r["date"] for r in conn.execute(
        "SELECT date FROM history_days WHERE date < ? AND status != ? "
        "ORDER BY date LIMIT ?",
        (cutoff, STATUS_PRUNED, limit))]
    if not dates:
        return 0, 0

    now = _now_iso()
    deleted = 0
    t0 = _t.perf_counter()
    for ds in dates:
        deleted += conn.execute(
            "DELETE FROM daily_history WHERE date=?", (ds,)).rowcount
        conn.execute(
            "UPDATE history_days SET status=?, row_count=0, updated_at=? "
            "WHERE date=?", (STATUS_PRUNED, now, ds))
        conn.commit()

    remaining = conn.execute(
        "SELECT COUNT(*) FROM history_days WHERE date < ? AND status != ?",
        (cutoff, STATUS_PRUNED)).fetchone()[0]
    print(f"[Ledger] prune: deleted {deleted:,} rows across "
          f"{len(dates)} dates in {_t.perf_counter() - t0:.1f}s "
          f"(date < {cutoff})"
          f"{f', {remaining} dates still overhanging' if remaining else ''}")
    return len(dates), deleted


def coverage(db, years: int = HISTORY_YEARS) -> Tuple[int, int]:
    """(complete_days, expected_days) over the rolling horizon - the
    stock-market data-sufficiency number (D7)."""
    today = date.today()
    end = available_date(today)
    start = today - timedelta(days=years * 365)
    expected = (end - start).days + 1
    conn = db._get_conn()
    complete = conn.execute(
        "SELECT COUNT(*) FROM history_days "
        "WHERE date >= ? AND date <= ? AND status IN (?, ?)",
        (start.isoformat(), end.isoformat(),
         STATUS_FINAL, STATUS_PROVISIONAL)).fetchone()[0]
    return complete, expected


def scanner_ready(db, quiet: bool = False) -> Tuple[bool, str]:
    """The scanner gate predicate: is the 60-day window usable?

    Ready when every window date is complete, OR the only incomplete
    dates are ones we cannot currently do anything about (404-tombstoned
    or in retry backoff after >=2 attempts) - that mirrors the old
    "launch anyway with ESI fallback" resilience instead of re-nagging
    a modal every scan while everef is down.

    Returns (ready, reason). Callers must handle a non-bootstrapped
    ledger themselves (fall back to the legacy span check).
    quiet=True suppresses the verdict print (sub-second pollers).
    """
    window = scanner_window_dates()
    rows = get_rows(db, window)
    now_str = _now_iso()

    hard_missing: List[str] = []
    tolerated: List[str] = []
    for d in window:
        row = rows.get(d)
        if row and row["status"] in COMPLETE_STATUSES:
            continue
        if (row and row["attempts"] >= 2 and row["next_retry"]
                and row["next_retry"] > now_str):
            tolerated.append(f"{d}({row['status']}x{row['attempts']})")
            continue
        hard_missing.append(d)

    if hard_missing:
        reason = (f"{len(hard_missing)}/{len(window)} window dates "
                  f"incomplete: {hard_missing[:5]}"
                  f"{'...' if len(hard_missing) > 5 else ''}")
        if not quiet:
            print(f"[Ledger] scanner-{SCANNER_WINDOW_DAYS}d: "
                  f"BLOCKED - {reason}")
        return False, reason

    if tolerated:
        if not quiet:
            print(f"[Ledger] scanner-{SCANNER_WINDOW_DAYS}d: READY with "
                  f"tolerated gaps (unfetchable right now): {tolerated}")
        return True, f"ready ({len(tolerated)} tolerated gaps)"

    if not quiet:
        print(f"[Ledger] scanner-{SCANNER_WINDOW_DAYS}d: READY "
              f"({len(window)}/{len(window)} complete)")
    return True, "ready"


def measure_date_rows(db, date_str: str) -> int:
    """Measured row count for a date (fast via idx_date)."""
    conn = db._get_conn()
    return conn.execute(
        "SELECT COUNT(*) FROM daily_history WHERE date=?",
        (date_str,)).fetchone()[0]
