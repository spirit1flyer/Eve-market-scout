"""Migration dialogs for the market history database.

First-time archive import to SQLite. Network-resilient: if downloads fail,
app launches anyway with ESI fallback.

The data-side checks were extracted to history/history_checks.py
2026-07-24 (this file was over the 900-line limit); they are re-exported
below so existing `from gui.gui_migration import ...` sites still work.
The download/import pipeline itself lives in
history/history_reconciler.py.
"""

import tkinter as tk
from tkinter import ttk
from typing import List
import threading
import queue
import time
from datetime import date, timedelta

from history.market_history import MarketHistoryDB, REGION_IDS

# Re-exports: data-side checks (extracted; keep names importable here)
from history.history_checks import (  # noqa: F401
    ARCHIVE_FOLDER,
    EVEREF_LAG_DAYS,
    SCANNER_MIN_DAYS,
    get_archive_path,
    check_archive_exists,
    count_archive_files,
    get_archive_date_range,
    archive_has_full_history,
    archive_has_scanner_minimum,
    check_needs_migration,
    check_has_recent_data,
    _legacy_check_has_recent_data,
    get_scanner_missing_dates,
    check_has_full_history,
    get_days_short_of_full_history,
)

from history.background_import import (
    get_background_import_status,
    start_background_full_import,
    is_background_import_running
)

# run_daily_update_background is re-exported for main.py; it now
# delegates to history/history_reconciler.py (the one pipeline).
from history.daily_update import run_daily_update_background  # noqa: F401
from gui.gui_window_utils import fit_window


def _run_dialog_loop(root: tk.Tk, dialog):
    """Run a manual update loop for a startup dialog.
    
    Replaces wait_window() for dialogs that use background threads.
    Calls root.update() to process Tk events, then drains the dialog's
    message queue to update widgets. No after(), no wait_window().
    
    The dialog must have:
        _done: bool flag, set True when dialog should close
        _drain_queue(): method that reads queue and updates UI
    
    Args:
        root: The Tk root window
        dialog: A Toplevel dialog with _done and _drain_queue
    """
    while not dialog._done:
        try:
            root.update()
        except tk.TclError:
            break
        dialog._drain_queue()
        time.sleep(0.05)


# =============================================================================
# First Launch Choice Dialog
# =============================================================================

class FirstLaunchDialog(tk.Toplevel):
    """Dialog to choose scanner-only (60 days) or full (3 years) mode.
    
    This is a Toplevel dialog that uses the single app root window.
    """
    
    def __init__(self, parent: tk.Tk):
        print("[Debug] FirstLaunchDialog.__init__: starting")
        super().__init__(parent)
        print("[Debug] FirstLaunchDialog.__init__: super().__init__ done")
        self.parent = parent
        self.choice = None
        
        self.title("EVE Market Scout - First Launch Setup")
        print("[Debug] FirstLaunchDialog.__init__: basic setup done")

        # Only attach as transient when the parent is actually visible.
        # On Linux, transient() with a withdrawn/hidden parent can make the
        # dialog invisible or un-focusable on some window managers.
        if parent.winfo_viewable():
            self.transient(parent)
        self.deiconify()
        self.lift()
        self.focus_force()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        print("[Debug] FirstLaunchDialog.__init__: calling _build_ui")
        self._build_ui()
        fit_window(self, min_width=450)
        self.grab_set()
        print("[Debug] FirstLaunchDialog.__init__: complete")
    
    def _build_ui(self):
        print("[Debug] FirstLaunchDialog._build_ui: starting")
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(
            frame, text="Choose Setup Mode",
            font=("Segoe UI", 12, "bold")
        ).pack(pady=(0, 15))
        
        ttk.Label(
            frame,
            text="Scanner needs 60 days of market history.\n"
                 "Stock Market features need 3 years.\n\n"
                 "You can start scanning immediately with 60 days,\n"
                 "and 3-year data will download in the background.",
            justify=tk.CENTER
        ).pack(pady=(0, 20))
        
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X)
        
        ttk.Button(
            btn_frame, text="Scanner Only (60 days)\n~3-4 minutes",
            command=self._choose_scanner
        ).pack(side=tk.LEFT, expand=True, padx=5)
        
        ttk.Button(
            btn_frame, text="Full Download (3 years)\n~15-30 minutes",
            command=self._choose_full
        ).pack(side=tk.RIGHT, expand=True, padx=5)
        print("[Debug] FirstLaunchDialog._build_ui: complete")
    
    def _choose_scanner(self):
        print("[Debug] FirstLaunchDialog._choose_scanner called")
        self.choice = 'scanner'
        self._close()
    
    def _choose_full(self):
        print("[Debug] FirstLaunchDialog._choose_full called")
        self.choice = 'full'
        self._close()
    
    def _on_close(self):
        print("[Debug] FirstLaunchDialog._on_close called")
        self.choice = 'scanner'
        self._close()
    
    def _close(self):
        print(f"[Debug] FirstLaunchDialog._close: choice={self.choice}")
        self.grab_release()
        self.destroy()
        print("[Debug] FirstLaunchDialog._close: destroyed")


# =============================================================================
# Migration Dialog (Full blocking mode)
# =============================================================================

class MigrationDialog(tk.Toplevel):
    """Progress dialog for initial database migration.
    
    Uses queue.Queue + manual root.update() loop.
    Background thread NEVER calls Tk methods directly.
    """
    
    def __init__(self, parent: tk.Tk, db: MarketHistoryDB):
        super().__init__(parent)
        self.parent = parent
        self.db = db
        self.result = False
        self._msg_queue = queue.Queue()
        self._done = False
        self._close_at = None
        self._failed_waiting_close = False
        
        self.title("EVE Market Scout - Database Setup")

        self.transient(parent)
        self.deiconify()
        self.lift()
        self.focus_force()

        self.protocol("WM_DELETE_WINDOW", lambda: None)

        self._build_ui()
        fit_window(self, min_width=450)
        self.grab_set()
    
    def _build_ui(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(
            frame, text="Building Market History Database",
            font=("Segoe UI", 11, "bold")
        ).pack(pady=(0, 10))
        
        self.status_var = tk.StringVar(value="Preparing...")
        ttk.Label(frame, textvariable=self.status_var).pack(pady=(0, 10))
        
        self.progress = ttk.Progressbar(frame, length=400, mode='determinate')
        self.progress.pack(pady=(0, 10))
        
        self.count_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.count_var).pack(pady=(0, 10))
        
        ttk.Label(
            frame,
            text="This is a one-time setup. Future launches will be instant.",
            foreground="gray"
        ).pack()
    
    def _drain_queue(self):
        """Called from manual update loop. Reads queue, updates UI."""
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                msg_type = msg[0]
                
                if msg_type == 'progress':
                    _, status, current, total = msg
                    self.status_var.set(status)
                    if total > 0:
                        self.progress['value'] = (current / total) * 100
                        self.count_var.set(f"{current:,} / {total:,} files")
                
                elif msg_type == 'complete':
                    self.status_var.set("Import complete!")
                    self.progress['value'] = 100
                    try:
                        stats = self.db.get_stats()
                        self.count_var.set(
                            f"{stats.get('row_count', 0):,} records imported"
                        )
                    except Exception:
                        pass
                    self._close_at = time.time() + 1.5
                
                elif msg_type == 'failed':
                    _, error = msg
                    self.status_var.set(f"Import failed: {error}")
                    self.count_var.set(
                        "You can still use the app - "
                        "some features may be limited."
                    )
                    self.protocol("WM_DELETE_WINDOW", self._user_close)
                    ttk.Button(
                        self, text="Close", command=self._user_close
                    ).pack(pady=10)
                    self._failed_waiting_close = True
                    
        except queue.Empty:
            pass
        
        # Check timed close
        if self._close_at and time.time() >= self._close_at:
            self._finish()
    
    def _user_close(self):
        """Called when user clicks Close on failed import."""
        self._finish()
    
    def _progress_callback(self, status: str, current: int, total: int):
        """Called from import thread - queue only, never touches Tk."""
        self._msg_queue.put(('progress', status, current, total))
    
    def _run_import(self):
        """Runs in background thread. Never calls Tk methods."""
        archive_path = get_archive_path()
        # region_filter=None imports all regions present in the everef
        # daily files (entire EVE universe), not just the 5 hub regions.
        # See run_migration_if_needed for the matching backfill check.
        region_filter = None
        
        try:
            self.db.import_archive(
                archive_path,
                progress_callback=self._progress_callback,
                years=3,
                region_filter=region_filter
            )
            # Mark backfill complete so we don't trigger it again
            try:
                self.db.set_meta("all_regions_backfilled", "1")
            except Exception as e:
                print(f"[Migration] Could not set backfill flag: {e}")
            self.result = True
            self._msg_queue.put(('complete',))
        except Exception as e:
            print(f"[Migration] Import failed: {e}")
            self._msg_queue.put(('failed', str(e)))
    
    def _finish(self):
        print("[Debug] MigrationDialog finishing")
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        self._done = True
    
    def start_import(self):
        """Launch import thread."""
        threading.Thread(
            target=self._run_import, daemon=True
        ).start()


# =============================================================================
# Scanner Quick Setup Dialog
# =============================================================================

class ScannerSetupDialog(tk.Toplevel):
    """Progress dialog for downloading scanner minimum data.
    
    Uses queue.Queue + manual root.update() loop.
    Background thread NEVER calls Tk methods directly.
    """
    
    def __init__(self, parent: tk.Tk, db: MarketHistoryDB, 
                 missing_dates: List[str]):
        super().__init__(parent)
        self.parent = parent
        self.db = db
        self.missing_dates = missing_dates
        self.result = False
        self._msg_queue = queue.Queue()
        self._done = False
        self._close_at = None
        
        self.title("EVE Market Scout - Scanner Setup")

        self.transient(parent)
        self.deiconify()
        self.lift()
        self.focus_force()

        self.protocol("WM_DELETE_WINDOW", lambda: None)

        self._build_ui()
        fit_window(self, min_width=400)
        self.grab_set()
        print(f"[Debug] ScannerSetupDialog created for "
              f"{len(missing_dates)} dates")
    
    def _build_ui(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(
            frame, text="Downloading Scanner Data",
            font=("Segoe UI", 11, "bold")
        ).pack(pady=(0, 10))
        
        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(frame, textvariable=self.status_var).pack(pady=(0, 10))
        
        self.progress = ttk.Progressbar(
            frame, length=350, mode='determinate'
        )
        self.progress.pack(pady=(0, 10))
        
        self.count_var = tk.StringVar(
            value=f"0 / {len(self.missing_dates)} days"
        )
        ttk.Label(frame, textvariable=self.count_var).pack()
    
    def _drain_queue(self):
        """Called from manual update loop. Reads queue, updates UI."""
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                msg_type = msg[0]
                
                if msg_type == 'progress':
                    _, status, current, total = msg
                    self.status_var.set(status)
                    if total > 0:
                        self.progress['value'] = (current / total) * 100
                        self.count_var.set(f"{current} / {total} days")
                
                elif msg_type == 'complete':
                    _, records = msg
                    print(f"[Debug] ScannerSetupDialog: complete "
                          f"({records} days fetched)")
                    self.status_var.set("Scanner data ready!")
                    self.count_var.set(
                        f"{records} days fetched" if records
                        else "All days present")
                    self.progress['value'] = 100
                    self._close_at = time.time() + 1.5
                
                elif msg_type == 'no_data':
                    print("[Debug] ScannerSetupDialog: no data")
                    self.status_var.set(
                        "Network unavailable - using fallback"
                    )
                    self.count_var.set(
                        "Scanner will use ESI API directly"
                    )
                    self.progress['value'] = 100
                    self._close_at = time.time() + 2.0
                
                elif msg_type == 'failed':
                    _, error = msg
                    print(f"[Debug] ScannerSetupDialog: "
                          f"failed: {error}")
                    self.status_var.set(f"Setup issue: {error}")
                    self.count_var.set(
                        "Launching with ESI fallback..."
                    )
                    self._close_at = time.time() + 2.0
                    
        except queue.Empty:
            pass
        
        # Check timed close
        if self._close_at and time.time() >= self._close_at:
            self._finish()
    
    def _update_progress(self, status: str, current: int, total: int):
        """Called from download thread - queue only, never touches Tk."""
        self._msg_queue.put(('progress', status, current, total))
    
    def _run_download(self):
        """Runs in background thread. Never calls Tk methods.

        Observer (2026-08-01): polls the ledger predicate and the
        reconciler's live get_status() snapshot instead of joining the
        pass queue - the old version blocked on the pass lock for the
        WHOLE launch pass, including the Phase E prune (the 10-minute
        dialog hang). Now the dialog shows whatever the running pass is
        actually doing and closes the moment the ledger says the 60-day
        window is usable, while background maintenance carries on.
        Starts a pass of its own only if none is running.
        """
        print("[Debug] ScannerSetupDialog._run_download: "
              "observer starting")
        try:
            from history import history_reconciler as reconciler
            started_own_pass = False
            while True:
                if reconciler.check_scanner_ready(self.db, quiet=True):
                    print("[Debug] ScannerSetupDialog: ledger says "
                          "ready - closing")
                    self.result = True
                    st = reconciler.get_status() or {}
                    self._msg_queue.put(('complete',
                                         st.get("fetched", 0)))
                    return

                st = reconciler.get_status()
                if st is None:
                    if started_own_pass:
                        # Our pass ended (or crashed) and the window
                        # is still unusable - launch with ESI fallback.
                        self.result = True
                        self._msg_queue.put(
                            ('failed', 'history window incomplete'))
                        return
                    started_own_pass = True
                    print("[Debug] ScannerSetupDialog: no pass "
                          "running - starting scanner-gate pass")
                    threading.Thread(
                        target=reconciler.run_reconcile,
                        kwargs={"trigger": "scanner-gate"},
                        daemon=True,
                        name="reconciler-scanner-gate").start()
                    # Give the pass a moment to publish its status so
                    # the next loop doesn't mistake the gap for "ended"
                    for _ in range(20):
                        time.sleep(0.5)
                        if reconciler.get_status() is not None:
                            break
                    continue

                if st.get("gate_settled") and not st.get("gate_ready"):
                    # Phase A is done and the window is still blocked
                    # (everef down / 404s). Report and let the app
                    # launch; maintenance keeps running behind us.
                    self.result = True
                    if (st.get("fetched", 0) == 0
                            and st.get("failed", 0) > 0):
                        self._msg_queue.put(('no_data',))
                    else:
                        self._msg_queue.put(
                            ('failed', st.get("gate_reason", "")))
                    return

                self._msg_queue.put(('progress', st.get("status", ""),
                                     st.get("current", 0),
                                     st.get("total", 0)))
                time.sleep(0.5)

        except Exception as e:
            print(f"[Debug] ScannerSetupDialog._run_download "
                  f"error: {e}")
            import traceback
            traceback.print_exc()
            self.result = True
            self._msg_queue.put(('failed', str(e)))
    
    def _finish(self):
        print("[Debug] ScannerSetupDialog._finish")
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        self._done = True
    
    def start(self):
        """Launch download thread immediately."""
        print("[Debug] ScannerSetupDialog.start: "
              "launching download thread")
        threading.Thread(
            target=self._run_download, daemon=True
        ).start()


# =============================================================================
# Main Entry Point
# =============================================================================

def run_migration_if_needed(parent: tk.Tk, db: MarketHistoryDB) -> bool:
    """Check if migration is needed and run it.
    
    Args:
        parent: The single Tk root window (created in main.py)
        db: Market history database instance
    
    First run: Shows 60-day vs 3-year choice dialog.
    Subsequent runs: Skips if data exists, starts background import if needed.
    """
    print("[Debug] run_migration_if_needed: starting")
    
    if not check_needs_migration(db):
        print("[Migration] Database already populated, skipping migration")
        
        # Decide whether to trigger background full import:
        #   - days_short > 30: data is stale enough that daily update
        #     can't catch up incrementally
        #   - needs_backfill: DB was originally built with the 5-region
        #     filter and has never been refreshed to include the rest
        #     of the universe (Thera, low/null NPC stations, etc.)
        # In either case, start_background_full_import() rebuilds the
        # full DB with region_filter=None and queues a swap on next
        # launch.  Both conditions share the same path.
        days_short = get_days_short_of_full_history(db)
        needs_backfill = db.get_meta("all_regions_backfilled") != "1"
        
        if check_archive_exists() and (days_short > 30 or needs_backfill):
            if needs_backfill:
                print("[Migration] All-regions backfill not yet done - "
                      "triggering background import to populate full "
                      "universe")
            else:
                print(f"[Migration] {days_short} days short, "
                      "starting background import")
            start_background_full_import(db)
        elif days_short > 0:
            print(f"[Migration] {days_short} days short, daily update will handle")
        
        return True
    
    print("[Debug] run_migration_if_needed: calling init_db")
    db.init_db()

    has_archive = check_archive_exists()
    print(f"[Debug] run_migration_if_needed: has_archive={has_archive}")

    # Branch B: archive on disk already covers full 3-year window.
    # No choice for the user to make — both dialog options would just
    # import locally. Skip the prompt and run full migration directly.
    if has_archive and archive_has_full_history():
        print("[Migration] Local archive covers full 3-year history - "
              "skipping FirstLaunchDialog, running full migration")
        migration_dialog = MigrationDialog(parent, db)
        migration_dialog.start_import()
        _run_dialog_loop(parent, migration_dialog)
        return migration_dialog.result

    print("[Debug] run_migration_if_needed: showing FirstLaunchDialog")

    # FirstLaunchDialog is a Toplevel using the single app root
    dialog = FirstLaunchDialog(parent)
    
    # Wait for dialog to close (modal behavior via wait_window)
    parent.wait_window(dialog)
    
    choice = dialog.choice
    print(f"[Debug] run_migration_if_needed: user chose '{choice}'")
    
    result = True  # Default to success - app should launch
    
    if choice == 'full':
        if has_archive:
            file_count = count_archive_files()
            print(f"[Migration] Starting full migration of {file_count} archive files")
            
            migration_dialog = MigrationDialog(parent, db)
            migration_dialog.start_import()
            _run_dialog_loop(parent, migration_dialog)
            
            result = migration_dialog.result
        else:
            print("[Migration] No archive, downloading scanner data first")
            result = _run_scanner_setup_with_download(parent, db)
    else:
        # Scanner only mode
        if has_archive:
            result = _import_scanner_minimum(parent, db)
        else:
            result = _run_scanner_setup_with_download(parent, db)
        
        if result:
            start_background_full_import(db)
    
    print(f"[Debug] run_migration_if_needed: returning {result}")
    return result


def _import_scanner_minimum(parent: tk.Tk, db: MarketHistoryDB) -> bool:
    """Import just enough data for scanner (SCANNER_MIN_DAYS days)."""
    archive_path = get_archive_path()
    # Scanner-minimum keeps the 5-region filter to stay fast on first
    # launch.  The all-regions backfill is handled by the subsequent
    # start_background_full_import() call in run_migration_if_needed,
    # which builds market_history_full.db with region_filter=None and
    # swaps it in on the next launch.
    region_filter = set(REGION_IDS.values())
    
    today = date.today()
    available_date = today - timedelta(days=EVEREF_LAG_DAYS)
    start_date = available_date - timedelta(days=SCANNER_MIN_DAYS - 1)
    
    files_to_import = []
    check_date = start_date
    
    while check_date <= available_date:
        date_str = check_date.strftime('%Y-%m-%d')
        year = check_date.year
        
        csv_path = archive_path / str(year) / f"market-history-{date_str}.csv"
        bz2_path = archive_path / str(year) / f"market-history-{date_str}.csv.bz2"
        
        if csv_path.exists():
            files_to_import.append(csv_path)
        elif bz2_path.exists():
            files_to_import.append(bz2_path)
        
        check_date += timedelta(days=1)
    
    if not files_to_import:
        print("[Migration] No scanner files in archive, need to download")
        return _run_scanner_setup_with_download(parent, db)
    
    print(f"[Migration] Importing {len(files_to_import)} days for scanner")
    
    total = 0
    for f in files_to_import:
        try:
            records = db.import_file(f, region_filter=region_filter)
            total += records
        except Exception as e:
            print(f"[Migration] Error importing {f.name}: {e}")
    
    print(f"[Migration] Scanner import complete: {total:,} records")
    return True  # Always return True - app should launch


def _run_scanner_setup_with_download(parent: tk.Tk, db: MarketHistoryDB) -> bool:
    """Run scanner setup with download dialog.
    
    Args:
        parent: The single Tk root window
        db: Market history database
    """
    print("[Debug] _run_scanner_setup_with_download: starting")
    missing = get_scanner_missing_dates(db)
    
    if not missing:
        print("[Debug] _run_scanner_setup_with_download: no missing dates")
        return True
    
    print(f"[Debug] _run_scanner_setup_with_download: {len(missing)} dates to download")
    
    dialog = ScannerSetupDialog(parent, db, missing)
    dialog.start()
    
    # Manual update loop - no after(), no wait_window()
    _run_dialog_loop(parent, dialog)
    
    result = dialog.result
    print(f"[Debug] _run_scanner_setup_with_download: result={result}")
    
    if result:
        start_background_full_import(db)
    
    return result


def ensure_scanner_data(parent: tk.Tk, db: MarketHistoryDB) -> bool:
    """Ensure database has minimum data for scanner to work.
    
    Note: Assumes db.init_db() already called by run_migration_if_needed.
    
    Returns:
        True always - app should launch regardless of data availability
    """
    if check_has_recent_data(db):
        print("[ScannerSetup] Scanner data OK")
        return True
    
    missing = get_scanner_missing_dates(db)
    
    if not missing:
        print("[ScannerSetup] No missing dates")
        return True
    
    print(f"[ScannerSetup] Need to download {len(missing)} days for scanner")
    
    dialog = ScannerSetupDialog(parent, db, missing)
    dialog.start()
    _run_dialog_loop(parent, dialog)
    
    return True
