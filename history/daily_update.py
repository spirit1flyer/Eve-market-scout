"""Daily update shim - the real pipeline is history/history_reconciler.py.

The old duplicate pipeline (get_missing_dates_from_db forward walk +
non-atomic downloads + import_daily_files) was deleted 2026-07 per
PLAN_history_reconciler.md: it could never see interior holes (D2),
wrote downloads non-atomically (D4), and raced the scanner setup
dialog's twin pipeline for the SQLite writer (D1).

Kept here for backward compatibility:
    EVEREF_LAG_DAYS               (canonical copy: history_ledger)
    run_daily_update_background   (delegates to the reconciler)
"""

from typing import Callable, Optional

from history.history_ledger import EVEREF_LAG_DAYS  # noqa: F401 (re-export)
from history.market_history import MarketHistoryDB


def run_daily_update_background(db: MarketHistoryDB,
                                callback: Optional[Callable] = None):
    """Start a background reconcile pass (launch-time entry point)."""
    from history.history_reconciler import run_reconcile_background
    print("[Debug] run_daily_update_background: delegating to "
          "HistoryReconciler")
    return run_reconcile_background(db, callback=callback,
                                    trigger="daily-update")
