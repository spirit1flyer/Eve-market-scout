"""Shared in-process writer lock for market_history.db imports.

Fixes D1/D3 (see PLAN_history_reconciler.md): SQLite WAL allows one
writer; before this lock, concurrent import threads (daily update,
scanner setup dialog, stock market import dialog) hit `database is
locked` after the 5 s connect timeout and silently skipped files.

Every import entry point (import_file / import_archive / reconciler)
must hold this lock. RLock so import_archive -> import_file nesting is
fine. A wait longer than WAIT_WARN_SECONDS is the old race showing up
again - it gets a loud [ImportLock] line either way, with holder names,
so contention is always visible in eve_scout.log.

Standalone module (no history-package imports) to avoid import cycles.
"""

import threading
import time
from contextlib import contextmanager

_LOCK = threading.RLock()
_holder_name: str = ""
_holder_since: float = 0.0

# Waits beyond this are suspicious (imports hold the lock for seconds,
# not minutes) and get a WARN-level log line.
WAIT_WARN_SECONDS = 5.0


@contextmanager
def acquire(name: str):
    """Acquire the import writer lock, logging any contention.

    Args:
        name: Who is asking (e.g. "import_file:market-history-2026-07-22.csv",
              "reconciler:launch"). Shown in logs on both sides of a wait.
    """
    global _holder_name, _holder_since

    t0 = time.perf_counter()
    acquired_immediately = _LOCK.acquire(blocking=False)
    if not acquired_immediately:
        blocker = _holder_name
        print(f"[ImportLock] '{name}' waiting - held by '{blocker}' "
              f"for {time.perf_counter() - _holder_since:.1f}s so far")
        _LOCK.acquire()
        waited = time.perf_counter() - t0
        level = "WARN " if waited > WAIT_WARN_SECONDS else ""
        print(f"[ImportLock] {level}'{name}' acquired after waiting "
              f"{waited:.1f}s (was held by '{blocker}')")

    prev_name, prev_since = _holder_name, _holder_since
    _holder_name, _holder_since = name, time.perf_counter()
    try:
        yield
    finally:
        held = time.perf_counter() - _holder_since
        # Only log holds long enough to have made someone wait noticeably
        if held > 1.0:
            print(f"[ImportLock] '{name}' released after {held:.1f}s")
        _holder_name, _holder_since = prev_name, prev_since
        _LOCK.release()
