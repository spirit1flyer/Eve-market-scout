"""Data-presence checks for market_history.db and the local archive.

Extracted from gui/gui_migration.py 2026-07-24 (file was over the
900-line hard limit; these are pure data-side checks with no Tk
dependency). gui_migration re-exports every public name so existing
`from gui.gui_migration import ...` call sites keep working.

The scanner gate here is ledger-backed (see history/history_ledger.py
and PLAN_history_reconciler.md); the legacy endpoint-span check remains
only as the fallback while the ledger isn't bootstrapped yet.
"""

from datetime import date, timedelta
from pathlib import Path
from typing import List

from core.sound_manager import get_data_dir
from history.history_ledger import EVEREF_LAG_DAYS, SCANNER_WINDOW_DAYS
from history.market_history import MarketHistoryDB

ARCHIVE_FOLDER = "history-archive"
SCANNER_MIN_DAYS = SCANNER_WINDOW_DAYS  # 60 as of 2026-07-24 (was 30)


def get_archive_path() -> Path:
    return get_data_dir() / ARCHIVE_FOLDER


# =============================================================================
# Archive (raw CSV folder) checks
# =============================================================================

def check_archive_exists() -> bool:
    """Check if archive folder exists with files."""
    archive_path = get_archive_path()
    if not archive_path.exists():
        return False

    for year_dir in archive_path.iterdir():
        if year_dir.is_dir() and year_dir.name.isdigit():
            files = list(year_dir.glob("market-history-*"))
            if files:
                return True
    return False


def count_archive_files() -> int:
    """Count total files in archive for progress estimation."""
    archive_path = get_archive_path()
    count = 0

    for year_dir in archive_path.iterdir():
        if year_dir.is_dir() and year_dir.name.isdigit():
            count += len(list(year_dir.glob("market-history-*")))

    return count


def get_archive_date_range() -> tuple:
    """Get (earliest_date, latest_date) present in the archive.

    Returns (None, None) if archive is empty or missing.
    """
    archive_path = get_archive_path()
    if not archive_path.exists():
        return (None, None)

    earliest = None
    latest = None

    for year_dir in archive_path.iterdir():
        if not (year_dir.is_dir() and year_dir.name.isdigit()):
            continue
        for f in year_dir.glob("market-history-*"):
            stem = f.name
            if stem.endswith('.bz2'):
                stem = stem[:-4]
            if stem.endswith('.csv'):
                stem = stem[:-4]
            date_str = stem.replace('market-history-', '')
            try:
                d = date.fromisoformat(date_str)
            except ValueError:
                continue
            if earliest is None or d < earliest:
                earliest = d
            if latest is None or d > latest:
                latest = d

    return (earliest, latest)


def archive_has_full_history(years: int = 3) -> bool:
    """True iff local archive covers `years` of recent data with no
    further network download needed.

    Requires both:
      - latest date is within EVEREF_LAG_DAYS+5 of today (recent enough)
      - earliest date is at or before today - years*365 (deep enough)
    """
    earliest, latest = get_archive_date_range()
    if not earliest or not latest:
        return False

    today = date.today()
    if latest < today - timedelta(days=EVEREF_LAG_DAYS + 5):
        return False
    if earliest > today - timedelta(days=years * 365):
        return False
    return True


def archive_has_scanner_minimum(min_days: int = SCANNER_MIN_DAYS) -> bool:
    """True iff local archive covers the scanner's recent-day window."""
    _, latest = get_archive_date_range()
    if not latest:
        return False

    today = date.today()
    if latest < today - timedelta(days=EVEREF_LAG_DAYS + 5):
        return False

    # Count actual files inside the recent window, not just date span
    archive_path = get_archive_path()
    available_date = today - timedelta(days=EVEREF_LAG_DAYS)
    start_date = available_date - timedelta(days=min_days - 1)

    days_present = 0
    check_date = start_date
    while check_date <= available_date:
        date_str = check_date.strftime('%Y-%m-%d')
        year_dir = archive_path / str(check_date.year)
        csv_path = year_dir / f"market-history-{date_str}.csv"
        bz2_path = year_dir / f"market-history-{date_str}.csv.bz2"
        if csv_path.exists() or bz2_path.exists():
            days_present += 1
        check_date += timedelta(days=1)

    # Allow small gaps but require near-full coverage
    return days_present >= min_days - 2


# =============================================================================
# Database checks
# =============================================================================

def check_needs_migration(db: MarketHistoryDB) -> bool:
    """Check if database needs initial migration."""
    print("[Debug] check_needs_migration: checking...")
    if not db.db_path.exists():
        print("[Debug] check_needs_migration: db file doesn't exist")
        return True

    try:
        conn = db._get_conn()
        cursor = conn.execute("SELECT 1 FROM daily_history LIMIT 1")
        row = cursor.fetchone()
        result = row is None
        print(f"[Debug] check_needs_migration: has data = {not result}")
        return result
    except Exception as e:
        print(f"[Debug] check_needs_migration: error {e}")
        return True


def check_has_recent_data(db: MarketHistoryDB, min_days: int = SCANNER_MIN_DAYS) -> bool:
    """Scanner gate: is the trailing window verifiably complete?

    Ledger-backed (hole-aware, ms-fast) with automatic fallback to the
    legacy endpoint-span check while the ledger isn't bootstrapped yet.
    """
    from history.history_reconciler import check_scanner_ready
    return check_scanner_ready(db)


def _legacy_check_has_recent_data(db: MarketHistoryDB,
                                  min_days: int = SCANNER_MIN_DAYS) -> bool:
    """Pre-ledger gate: staleness + endpoint span ONLY - blind to
    interior holes (defect D2). Kept solely as the fallback for a
    not-yet-bootstrapped ledger; do not call directly."""
    try:
        latest = db.get_latest_date()
        earliest = db.get_earliest_date()

        if not latest or not earliest:
            return False

        latest_date = date.fromisoformat(latest)
        earliest_date = date.fromisoformat(earliest)
        days_covered = (latest_date - earliest_date).days + 1

        today = date.today()
        days_stale = (today - latest_date).days

        if days_stale > 7:
            print(f"[Scanner] Data is {days_stale} days old, needs update")
            return False

        if days_covered < min_days:
            print(f"[Scanner] Only {days_covered} days of data, need {min_days}")
            return False

        return True

    except Exception as e:
        print(f"[Scanner] Error checking data: {e}")
        return False


def get_scanner_missing_dates(db: MarketHistoryDB) -> List[str]:
    """Dates the scanner window still needs, newest first.

    Ledger-backed when bootstrapped (hole- and partial-aware). Fallback
    for a fresh/unbootstrapped DB is the old set-diff against
    get_imported_dates() - a full scan, but the DB is small in exactly
    that situation.
    """
    from history import history_ledger as ledger

    try:
        if ledger.is_bootstrapped(db):
            work = ledger.compute_work(db)
            return work["need_fetch"]
    except Exception as e:
        print(f"[ScannerSetup] ledger work computation failed ({e}) - "
              f"falling back to legacy set-diff")

    today = date.today()
    available_date = today - timedelta(days=EVEREF_LAG_DAYS)
    start_date = available_date - timedelta(days=SCANNER_MIN_DAYS - 1)

    try:
        existing_dates = db.get_imported_dates()
    except Exception:
        existing_dates = set()

    missing = []
    check_date = start_date

    while check_date <= available_date:
        date_str = check_date.strftime('%Y-%m-%d')
        if date_str not in existing_dates:
            missing.append(date_str)
        check_date += timedelta(days=1)

    print(f"[ScannerSetup] legacy missing-dates: {len(missing)} of "
          f"{SCANNER_MIN_DAYS}")
    return missing


# Stock-market gate: fraction of the rolling horizon that must be
# complete in the ledger. Not 100% - a handful of 404/unfetchable days
# out of ~1,100 shouldn't lock the whole tab.
STOCK_COVERAGE_MIN = 0.95

# Overlay polls this every few seconds - only log when the answer moves.
_last_coverage_msg: str = ""


def check_has_full_history(db: MarketHistoryDB, years: int = 3) -> bool:
    """Stock-market gate (D7): ledger coverage over the rolling horizon
    - hole- and staleness-aware, ms-fast. Falls back to the legacy
    earliest-date heuristic while the ledger isn't bootstrapped.
    """
    global _last_coverage_msg
    from history import history_ledger as ledger

    try:
        if ledger.is_bootstrapped(db):
            complete, expected = ledger.coverage(db, years)
            frac = complete / expected if expected else 0.0
            ok = frac >= STOCK_COVERAGE_MIN
            msg = (f"[StockMarket] history coverage: {complete}/{expected} "
                   f"days ({frac:.1%}) -> "
                   f"{'OK' if ok else 'INSUFFICIENT'}")
            if msg != _last_coverage_msg:
                print(msg)
                _last_coverage_msg = msg
            return ok
    except Exception as e:
        print(f"[StockMarket] ledger coverage check failed ({e}) - "
              f"falling back to legacy earliest-date check")
    return get_days_short_of_full_history(db, years) == 0


def get_days_short_of_full_history(db: MarketHistoryDB, years: int = 3) -> int:
    """Get how many days short of full history the database is.

    Returns:
        0 if full history exists, otherwise number of days missing.
        Returns 9999 if database is empty or error occurs.
    """
    try:
        earliest = db.get_earliest_date()

        if not earliest:
            return 9999  # No data at all

        earliest_date = date.fromisoformat(earliest)
        required_date = date.today() - timedelta(days=years * 365)

        if earliest_date > required_date:
            days_short = (earliest_date - required_date).days
            return days_short

        return 0  # Full history exists

    except Exception as e:
        print(f"[StockMarket] Error checking history: {e}")
        return 9999
