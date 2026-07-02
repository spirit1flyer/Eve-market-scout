"""EVE Market Scout v1.0 - Optimized with Jita caching and skill-based calculations."""

import asyncio
import sys
import os
import logging
import tkinter as tk
from datetime import datetime
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# =============================================================================
# WORKAROUND: CPython 3.12 _wmi crash/hang (issues #112278, #125315)
#
# Python 3.12 added a _wmi C module for platform.system(), platform.uname(),
# platform.win32_ver(), and platform.machine(). This module has two confirmed
# bugs: it hangs for 5-10+ seconds when WMI is slow or permissions are missing,
# and it can crash the process outright via a thread race on the stack.
#
# Fixed in Python 3.13 but NOT backported to 3.12.0 (which lacks try/except
# around the WMI call in _win32_ver). Two patches applied:
#   1. platform.system() returns "Windows" directly (what aiohttp needs)
#   2. _wmi_query raises OSError for any other callers with fallback support
# =============================================================================
import platform
if sys.platform == "win32":
    platform.system = lambda: "Windows"
    def _wmi_disabled(*args, **kwargs):
        raise OSError("WMI disabled for stability")
    if hasattr(platform, '_wmi_query'):
        platform._wmi_query = _wmi_disabled


# =============================================================================
# LOGGING SETUP - must be early, before other imports that might print
# =============================================================================

def _setup_logging():
    """Set up file logging for packaged builds.
    
    Captures all print() output and exceptions to a log file.
    Log location: %APPDATA%/EVEMarketScout/eve_scout.log
    """
    # Get data directory
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        log_dir = Path(base) / "EVEMarketScout"
    elif sys.platform == "darwin":
        log_dir = Path.home() / "Library" / "Application Support" / "EVEMarketScout"
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        log_dir = Path(xdg_config) / "eve-market-scout"
    
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "eve_scout.log"
    
    # Rotate log if it's too big (> 5MB)
    MAX_LOG_SIZE = 5 * 1024 * 1024
    if log_file.exists() and log_file.stat().st_size > MAX_LOG_SIZE:
        # Keep one backup
        backup = log_dir / "eve_scout.log.old"
        if backup.exists():
            backup.unlink()
        log_file.rename(backup)
    
    # Set up logging
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
        ]
    )
    
    # Create a stream that writes to both console (if available) and log
    class TeeStream:
        """Stream that writes to both original stream and log file."""
        def __init__(self, original, log_func):
            self.original = original
            self.log_func = log_func
            self.encoding = getattr(original, 'encoding', 'utf-8')
        
        def write(self, text):
            if text.strip():  # Don't log empty lines
                self.log_func(text.rstrip())
            if self.original:
                try:
                    self.original.write(text)
                except Exception:
                    pass  # Console might not exist in packaged build
        
        def flush(self):
            if self.original:
                try:
                    self.original.flush()
                except Exception:
                    pass
    
    # Redirect stdout and stderr
    sys.stdout = TeeStream(sys.stdout, logging.info)
    sys.stderr = TeeStream(sys.stderr, logging.error)
    
    # Log startup
    logging.info("=" * 60)
    logging.info(f"EVE Market Scout starting - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info(f"Python: {sys.version}")
    logging.info(f"Platform: {sys.platform}")
    logging.info(f"Frozen: {getattr(sys, 'frozen', False)}")
    logging.info(f"Log file: {log_file}")
    logging.info("=" * 60)
    
    return log_file


def _setup_exception_handler():
    """Set up global exception handler to log uncaught exceptions."""
    def exception_handler(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            # Don't log keyboard interrupt
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        
        logging.error("Uncaught exception:", exc_info=(exc_type, exc_value, exc_tb))
        
        # Also show to user if possible
        try:
            from tkinter import messagebox
            
            error_msg = f"{exc_type.__name__}: {exc_value}"
            messagebox.showerror(
                "EVE Market Scout Error",
                f"An unexpected error occurred:\n\n{error_msg}\n\n"
                f"Details have been logged to:\neve_scout.log"
            )
        except Exception:
            pass  # Can't show dialog, just log
    
    sys.excepthook = exception_handler


# Initialize logging immediately
_LOG_FILE = _setup_logging()
_setup_exception_handler()


# =============================================================================
# IMPORTS (after logging setup so we capture any import errors)
# =============================================================================

from core import custom_stations  # noqa: F401 — populates TRADE_HUBS before GUI init
from core.api import ESIClient
from scanning.scanner import MarketScanner
from scanning.scanner_common import ScanResult
from gui.gui_main import MarketScoutGUI
from core.calculate import TradingSkills, DEFAULT_SKILLS
from core.config import DEFAULT_HUB, JITA_REGION_ID
from history.market_history import get_market_history_db
from gui.gui_migration import run_migration_if_needed, run_daily_update_background

# Local debug flag
DEBUG = False

# Shared client to preserve caches between scans
_client: ESIClient = None

# Market history database (singleton)
_market_history_db = None


def _check_sde_on_startup(root: tk.Tk):
    """Check if SDE database needs to be downloaded on startup.
    
    Args:
        root: The single Tk root window (withdrawn)
    """
    try:
        from sde.sde_manager import get_sde_manager
    except ImportError:
        # sde_manager.py not present yet, skip silently
        return
    except Exception:
        return
    
    try:
        from tkinter import messagebox
        
        sde = get_sde_manager()
        
        if not sde.is_available():
            # No SDE database - prompt to download.
            # Don't pass parent=root: root is withdrawn at this point, and on
            # Linux a messagebox parented to a hidden window can appear
            # invisible or behind other windows.
            result = messagebox.askyesno(
                "Download Item Database",
                "EVE Market Scout can download a local item database for faster scanning.\n\n"
                "This eliminates API calls for item names and enables future features "
                "like cargo volume filtering.\n\n"
                "Download now? (About 5MB, takes ~30 seconds)\n\n"
                "You can skip this and download later from Data Folder menu.",
                icon="question",
            )

            if result:
                _download_sde_with_progress(root)

        elif sde.is_stale():
            # SDE exists but is old
            age = sde.get_age_days()

            result = messagebox.askyesno(
                "Update Item Database",
                f"Your item database is {age} days old.\n\n"
                "Would you like to update it now?\n\n"
                "(New items from recent patches may be missing)",
                icon="question",
            )
            
            if result:
                _download_sde_with_progress(root)
                
    except Exception as e:
        print(f"SDE check failed: {e}")


def _download_sde_with_progress(root: tk.Tk):
    """Download SDE with a progress dialog.
    
    Uses threading + queue.Queue + manual root.update() loop.
    No after(), no wait_window() - fully independent of Tk event loop state.
    Thread NEVER calls Tk methods directly.
    
    Args:
        root: The single Tk root window
    """
    from tkinter import ttk, messagebox
    import threading
    import queue
    import time

    from sde.sde_manager import get_sde_manager
    from gui.gui_window_utils import fit_window

    msg_queue = queue.Queue()

    # Create progress dialog
    dialog = tk.Toplevel(root)
    dialog.title("Downloading Item Database")
    dialog.transient(root)

    frame = ttk.Frame(dialog, padding=20)
    frame.pack(fill=tk.BOTH, expand=True)

    status_label = ttk.Label(frame, text="Starting download...")
    status_label.pack()

    progress_var = tk.DoubleVar(value=0)
    ttk.Progressbar(
        frame, variable=progress_var, length=350, mode="determinate"
    ).pack(pady=10)
    fit_window(dialog, min_width=400)
    dialog.grab_set()
    
    def update_progress(text: str, pct: int):
        """Called from thread - queue only, never touches Tk."""
        msg_queue.put(('progress', text, pct))
    
    def do_download():
        """Run download in separate thread. Never calls Tk methods."""
        try:
            sde = get_sde_manager()
            
            async def download():
                return await sde.download_and_build(
                    progress_callback=update_progress
                )
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                success = loop.run_until_complete(download())
                msg_queue.put(('done', success))
            finally:
                loop.close()
        except Exception as e:
            print(f"[SDE] Download error: {e}")
            msg_queue.put(('error', str(e)))
    
    # Start download thread
    threading.Thread(target=do_download, daemon=True).start()
    
    # Manual poll loop - no after(), no wait_window()
    close_at = None
    while True:
        # Process Tk events (redraws, button clicks, etc.)
        try:
            root.update()
        except tk.TclError:
            break
        
        # Time to close?
        if close_at and time.time() >= close_at:
            break
        
        # Drain queue (only if not already finishing)
        if not close_at:
            try:
                while True:
                    msg = msg_queue.get_nowait()
                    msg_type = msg[0]
                    
                    if msg_type == 'progress':
                        _, text, pct = msg
                        status_label.configure(text=text)
                        progress_var.set(pct)
                    
                    elif msg_type == 'done':
                        _, success = msg
                        if success:
                            status_label.configure(text="Download complete!")
                            close_at = time.time() + 1.0
                        else:
                            messagebox.showerror(
                                "Download Failed",
                                "Failed to download item database.\n"
                                "Scanning will still work using "
                                "API calls.",
                                parent=dialog
                            )
                            close_at = time.time()
                    
                    elif msg_type == 'error':
                        _, error_msg = msg
                        messagebox.showerror(
                            "Download Error",
                            f"Error: {error_msg}\n\n"
                            "Scanning will still work using "
                            "API calls.",
                            parent=dialog
                        )
                        close_at = time.time()
                        
            except queue.Empty:
                pass
        
        time.sleep(0.05)
    
    # Clean up
    try:
        dialog.grab_release()
    except Exception:
        pass
    try:
        dialog.destroy()
    except Exception:
        pass


def get_client() -> ESIClient:
    """Get the shared client instance."""
    global _client
    if _client is None:
        _client = ESIClient()
    return _client


def _ensure_market_history():
    """
    Ensure market history database is initialized and attached to client.
    
    This is called before each scan. The database is already populated
    by run_migration_if_needed() at startup.
    """
    global _client, _market_history_db
    
    if _market_history_db is None:
        _market_history_db = get_market_history_db()
    
    # Attach to client if not already
    if _client.market_history is None:
        _client.market_history = _market_history_db
        
        # Log stats
        if DEBUG:
            try:
                stats = _market_history_db.get_stats()
                print(f"[MarketHistory] Database ready: {stats.get('row_count', 0):,} records")
            except Exception:
                pass


async def run_scan(
    progress_callback,
    min_profit_per_unit=None,
    min_total_profit=None,
    max_cost=None,
    min_margin_percent=None,
    min_daily_volume=None,
    refresh_jita=False,
    skills: TradingSkills = None,
    hub: str = None,
    # Cross-hub specific parameters
    crosshub_mode: bool = False,
    buy_station: str = None,
    sell_station: str = None,
    buyer_skills: TradingSkills = None,
    seller_skills: TradingSkills = None,
):
    """
    Execute a market scan and return deals + timing info.
    
    Args:
        refresh_jita: If True, force refresh Jita data. Otherwise use cache.
        skills: Character's trading skills for accurate fee calculations (same-station mode).
        hub: Trade hub key (e.g., 'amarr', 'jita'). Defaults to DEFAULT_HUB.
        crosshub_mode: If True, scan for cross-hub arbitrage instead of same-station.
        buy_station: Hub key where we buy (cross-hub mode).
        sell_station: Hub key where we sell (cross-hub mode).
        buyer_skills: Buyer character's skills (cross-hub mode).
        seller_skills: Seller character's skills (cross-hub mode).
    
    Returns: (ScanResult or CrossHubScanResult, seconds_until_refresh)
    """
    global _client
    import aiohttp
    from core.config import REQUEST_TIMEOUT, get_hub_config
    from core.ssl_context import make_connector

    # Use default hub if none provided
    if hub is None:
        hub = DEFAULT_HUB

    # Use default skills if none provided
    if skills is None:
        skills = DEFAULT_SKILLS

    # Preserve caches but create fresh async primitives each scan
    if _client is None:
        _client = ESIClient()

    # Reset semaphore and session for new event loop
    _client.reset_for_new_loop()
    # Clear previous expiry so we get fresh timing
    _client.market_expires = None
    
    session = aiohttp.ClientSession(
        connector=make_connector(),
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    )
    _client.session = session

    try:
        # Ensure market history database is attached to client
        _ensure_market_history()
        
        if crosshub_mode and buy_station and sell_station and buy_station != sell_station:
            # Cross-hub arbitrage scan
            scanner = MarketScanner(_client, skills, hub_key=sell_station)
            
            # Set buyer skills if provided
            if buyer_skills:
                scanner.set_buyer_skills(buyer_skills)
            
            scan_result = await scanner.scan_crosshub(
                buy_station_key=buy_station,
                sell_station_key=sell_station,
                progress_callback=progress_callback,
                min_profit_per_unit=min_profit_per_unit,
                min_total_profit=min_total_profit,
                max_cost=max_cost,
                min_margin_percent=min_margin_percent,
                min_daily_volume=min_daily_volume,
                buyer_skills=buyer_skills or skills,
                seller_skills=seller_skills or skills,
            )
            
            # Return CrossHubScanResult directly - gui_main will handle it
            # with the dual-row display format
        else:
            # Same-station scan (original behavior)
            scanner = MarketScanner(_client, skills, hub_key=hub)
            scan_result = await scanner.scan(
                progress_callback,
                min_profit_per_unit=min_profit_per_unit,
                min_total_profit=min_total_profit,
                max_cost=max_cost,
                min_margin_percent=min_margin_percent,
                min_daily_volume=min_daily_volume,
                refresh_jita=refresh_jita
            )
        
        # Get seconds until ESI cache expires
        seconds_until_refresh = _client.get_seconds_until_refresh()
        
        return scan_result, seconds_until_refresh
    finally:
        await session.close()


def main():
    """Main entry point."""
    print("[Startup] EVE Market Scout initializing...")
    
    # ==========================================================================
    # SIGINT GUARD - Suppress only during Tk root creation to avoid spurious
    # console events racing widget construction. Restored immediately after so
    # Ctrl+C works during any startup dialogs that wait for user input.
    # ==========================================================================
    import signal
    _original_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    # ==========================================================================
    # SINGLE TK ROOT - Created FIRST, before any threads or dialogs
    # ==========================================================================
    root = tk.Tk()
    root.withdraw()  # Hide until main GUI is ready

    # Restore SIGINT now that the root is stable. Startup dialogs (SDE check,
    # first-launch migration) block on user input and must be Ctrl+C-able.
    signal.signal(signal.SIGINT, _original_sigint)

    # ==========================================================================
    # LINUX CTRL+C FIX
    # Tkinter's event loop (mainloop / wait_window) is C code — Python signal
    # handlers never execute while it's blocked.  Two-part fix:
    #   1. Override SIGINT with a handler that calls os._exit() directly,
    #      bypassing the Python layer entirely.
    #   2. Register a recurring after() callback (every 200 ms) so the event
    #      loop regularly yields back to Python, giving the OS a chance to
    #      deliver the signal between C-level epoll/select calls.
    # Windows doesn't need this — its Tk build polls signals natively.
    # ==========================================================================
    if sys.platform != "win32":
        import os as _os
        def _ctrl_c_handler(_sig, _frame):
            print("\n[Shutdown] Ctrl+C — force-exiting")
            logging.info("Ctrl+C received, force-exiting (Linux signal fix)")
            _os._exit(130)  # 130 = killed by SIGINT by convention
        signal.signal(signal.SIGINT, _ctrl_c_handler)

        def _signal_wakeup():
            root.after(200, _signal_wakeup)
        root.after(200, _signal_wakeup)
    
    # Ensure data directory exists BEFORE any threads can race on it
    from core.sound_manager import get_data_dir
    get_data_dir()
    
    # Check for pending database swap BEFORE opening any connections
    from history.background_import import check_and_perform_startup_swap
    if check_and_perform_startup_swap():
        print("[Startup] Full history database activated")
    
    # Check SDE database on startup (prompt to download if missing)
    # Now uses the single root for dialogs
    _check_sde_on_startup(root)
    
    # Initialize market history database
    # This runs migration dialog if database is empty (one-time)
    db = get_market_history_db()
    run_migration_if_needed(root, db)
    
    # Initialize material risk cache storage (persists material filter
    # results across launches so the filter doesn't re-run every startup)
    from analytics import material_risk_storage
    material_risk_storage.init_table()
    material_risk_storage.purge_before()  # Drops rows older than retention window
    _preloaded_risk = material_risk_storage.load_all_today()
    if _preloaded_risk:
        from analytics.stockmarket_filters import _material_risk_cache
        _material_risk_cache.update(_preloaded_risk)
        print(f"[Startup] Preloaded {len(_preloaded_risk)} material risk "
              f"entries from today")

    # Initialize leading indicators storage (Phase 1: table + purge only,
    # GUI integration comes in Phase 2)
    from analytics import leading_indicators_storage
    leading_indicators_storage.init_table()
    leading_indicators_storage.purge_before()
    
    print("[Startup] Launching GUI...")

    # Eagerly initialize ESI client (and supplement cache) BEFORE constructing
    # MarketScoutGUI. HoldingsPanel.__init__ calls refresh_display, which calls
    # get_client(); without this pre-call, the lazy ESIClient construction
    # (~5.6s on cold start) runs inside widget construction and the first
    # paint freezes for that duration. Done while SIGINT is suppressed.
    print("[Startup] Pre-initializing ESI client...")
    get_client()

    # Pass the existing root to MarketScoutGUI instead of creating a new one
    gui = MarketScoutGUI(root, scan_callback=run_scan, get_client=get_client)

    # Start thread-safe task queue polling AFTER GUI init, before mainloop
    from core.tk_queue import start_polling
    start_polling(root)

    # Start daily update AFTER GUI init to avoid thread interference
    # during widget construction
    run_daily_update_background(db)
    
    gui.run()
    
    print("[Shutdown] EVE Market Scout closing")
    logging.info("EVE Market Scout shutdown complete")


if __name__ == "__main__":
    main()
