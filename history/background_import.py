"""Background full history import for EVE Market Scout.

Runs 3-year import in background thread while app is usable.
Builds into market_history_full.db, writes marker file when done.
Swap happens at next app startup (before any DB connections open).

Called from gui_migration.py after scanner-only mode starts.
"""

import shutil
import threading
from pathlib import Path
from typing import Optional, Callable

from core.sound_manager import get_data_dir
from history.market_history import MarketHistoryDB, REGION_IDS


# Archive location
ARCHIVE_FOLDER = "history-archive"

# Marker file written when import completes, signals swap needed at startup
SWAP_MARKER_FILE = "swap_pending.flag"

# Callback for when profiles finish building (set by gui_stockmarket.py)
_on_profiles_ready: Optional[Callable] = None


def set_profiles_ready_callback(callback: Callable):
    """Register a callback to run (on main thread) when profiles are ready.
    
    Called from gui_stockmarket.py during init.
    """
    global _on_profiles_ready
    _on_profiles_ready = callback


def get_archive_path() -> Path:
    """Get path to history archive folder."""
    return get_data_dir() / ARCHIVE_FOLDER


def _get_swap_marker_path() -> Path:
    """Get path to swap marker file."""
    return get_data_dir() / SWAP_MARKER_FILE


def _get_full_db_path() -> Path:
    """Get path to full history temp database."""
    return get_data_dir() / "market_history_full.db"


def _get_main_db_path() -> Path:
    """Get path to main market history database."""
    return get_data_dir() / "market_history.db"


# =============================================================================
# Startup Swap Check
# =============================================================================

def check_and_perform_startup_swap() -> bool:
    """Check for pending swap and perform it at startup.
    
    Called from main.py BEFORE any database connections are opened.
    This is the clean moment to swap files.
    
    Returns:
        True if swap was performed, False if no swap needed or error
    """
    marker_path = _get_swap_marker_path()
    full_db_path = _get_full_db_path()
    main_db_path = _get_main_db_path()
    
    # Check if swap is pending
    if not marker_path.exists():
        return False
    
    if not full_db_path.exists():
        print("[BackgroundImport] Swap marker found but full DB missing, cleaning up")
        marker_path.unlink()
        return False
    
    print("[BackgroundImport] Performing startup database swap...")
    
    try:
        # Backup old DB (just in case)
        backup_path = main_db_path.parent / "market_history_old.db"
        if main_db_path.exists():
            shutil.move(str(main_db_path), str(backup_path))
        
        # Remove WAL and SHM files if they exist
        for suffix in ['-wal', '-shm']:
            wal_path = Path(str(main_db_path) + suffix)
            if wal_path.exists():
                try:
                    wal_path.unlink()
                except Exception:
                    pass
        
        # Move full DB to main path
        shutil.move(str(full_db_path), str(main_db_path))
        
        # Remove marker file
        marker_path.unlink()
        
        # Delete backup
        if backup_path.exists():
            try:
                backup_path.unlink()
            except Exception:
                pass
        
        print("[BackgroundImport] Database swap complete!")
        return True
        
    except Exception as e:
        print(f"[BackgroundImport] Startup swap failed: {e}")
        # Leave marker in place, will retry next startup
        return False


def is_restart_required() -> bool:
    """Check if app restart is needed to activate full history.
    
    Returns True if background import completed and swap marker exists.
    """
    return _get_swap_marker_path().exists() and _get_full_db_path().exists()


# =============================================================================
# Background Full Import Manager
# =============================================================================

# Global state for background import
_background_import_thread: Optional[threading.Thread] = None
_background_import_progress: dict = {
    'running': False,
    'status': '',
    'current': 0,
    'total': 0,
    'complete': False,
    'restart_required': False,
}


def get_background_import_status() -> dict:
    """Get current background import status for status bar.
    
    Returns dict with:
        running: bool - is import in progress
        status: str - current status message
        current: int - files processed
        total: int - total files
        complete: bool - has import finished
        restart_required: bool - needs restart to activate
    """
    # Check marker file for restart_required (survives app restart)
    status = _background_import_progress.copy()
    status['restart_required'] = is_restart_required()
    return status


def is_background_import_running() -> bool:
    """Check if background import is currently running."""
    return _background_import_progress['running']


def start_background_full_import(main_db: MarketHistoryDB, 
                                  on_complete: Optional[Callable] = None):
    """Start background thread to build full 3-year database.
    
    Builds into market_history_full.db. When complete, writes marker file.
    The actual swap happens at next app startup.
    
    Args:
        main_db: The main MarketHistoryDB instance (for path reference)
        on_complete: Optional callback when import finishes
    """
    global _background_import_thread, _background_import_progress
    
    if _background_import_progress['running']:
        print("[BackgroundImport] Already running")
        return
    
    _background_import_progress = {
        'running': True,
        'status': 'Starting full import...',
        'current': 0,
        'total': 0,
        'complete': False,
        'restart_required': False,
    }
    
    def _run():
        global _background_import_progress
        
        try:
            _do_background_import(on_complete)
        except Exception as e:
            print(f"[BackgroundImport] Error: {e}")
            _background_import_progress['status'] = f'Error: {e}'
        finally:
            _background_import_progress['running'] = False
    
    _background_import_thread = threading.Thread(target=_run, daemon=True)
    _background_import_thread.start()
    print("[BackgroundImport] Started background full import thread")


def _do_background_import(on_complete: Optional[Callable]):
    """Actually run the background import.
    
    Downloads missing archive files, then creates market_history_full.db,
    imports 3 years, writes marker file. Swap happens at next app startup.
    """
    global _background_import_progress
    import asyncio
    
    full_db_path = _get_full_db_path()
    marker_path = _get_swap_marker_path()
    archive_path = get_archive_path()
    
    def progress_callback(status: str, current: int, total: int):
        global _background_import_progress
        _background_import_progress['status'] = status
        _background_import_progress['current'] = current
        _background_import_progress['total'] = total
    
    try:
        # Phase 1: Download missing archive files
        _background_import_progress['status'] = 'Downloading 3-year archive...'
        print("[BackgroundImport] Phase 1: Downloading missing archive files")
        
        from history.archive_downloader import ArchiveDownloader
        downloader = ArchiveDownloader(archive_path=archive_path, auto_decompress=True)
        
        # Download each year
        years = downloader.get_years_to_download()
        total_downloaded = 0
        
        for year in years:
            missing = downloader.get_missing_dates(year)
            if not missing:
                print(f"[BackgroundImport] Year {year}: complete")
                continue
            
            print(f"[BackgroundImport] Year {year}: {len(missing)} files to download")
            _background_import_progress['status'] = f'Downloading {year}...'
            
            def year_progress(status, files_done, files_total, bytes_done, bytes_total):
                progress_callback(f"Downloading {year}: {status}", files_done, files_total)
            
            # Run async download in this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                completed = loop.run_until_complete(
                    downloader.download_year(year, progress_callback=year_progress)
                )
                if completed:
                    total_downloaded += len(missing)
            finally:
                loop.close()
        
        print(f"[BackgroundImport] Download complete: {total_downloaded} files")
        
        # Phase 2: Import to database
        _background_import_progress['status'] = 'Importing to database...'
        print("[BackgroundImport] Phase 2: Importing to database")
        
        # Delete any existing temp file
        if full_db_path.exists():
            try:
                full_db_path.unlink()
            except Exception as e:
                print(f"[BackgroundImport] Could not delete old temp file: {e}")
        
        # Create new DB for full import
        full_db = MarketHistoryDB(full_db_path)
        full_db.init_db()
        
        # region_filter=None imports every region present in the everef
        # daily files (the entire EVE universe).  The 5-region filter
        # was a legacy shortcut from when only the trade hubs were used;
        # going all-regions enables Thera, low/null NPC stations, and
        # any future station picked via custom_stations.json.
        region_filter = None
        
        full_db.import_archive(
            archive_path,
            progress_callback=progress_callback,
            years=3,
            region_filter=region_filter
        )

        # Mark the backfill complete on the new DB.  The flag survives
        # the swap (since we're swapping the file in-place at next
        # launch), so subsequent run_migration_if_needed() calls won't
        # re-trigger this import.
        try:
            full_db.set_meta("all_regions_backfilled", "1")
        except Exception as e:
            print(f"[BackgroundImport] Could not set backfill flag: {e}")
        
        # Phase 3: Build profiles from the temp DB
        # Profiles go to stock_profiles.db (separate file), so we can
        # build them now without waiting for the DB swap on restart.
        _background_import_progress['status'] = 'Building profiles...'
        print("[BackgroundImport] Phase 3: Building profiles from temp DB")
        
        try:
            from analytics.historical_profiles import ProfileManager
            from core.config import TRADE_HUBS
            
            profiles = ProfileManager()
            
            # Build Jita first, then the rest
            hub_order = ['jita'] + [k for k in TRADE_HUBS if k != 'jita']
            
            for hub_key in hub_order:
                hub_config = TRADE_HUBS.get(hub_key)
                if not hub_config or not hub_config.get('enabled', True):
                    continue
                
                region_id = hub_config['region_id']
                hub_name = hub_config['name']
                
                _background_import_progress['status'] = (
                    f'Building profiles: {hub_name}...'
                )
                print(f"[BackgroundImport] Building profiles for "
                      f"{hub_name} (region {region_id})")
                
                def profile_progress(msg, current, total,
                                     _name=hub_name):
                    if current % 500 == 0:
                        _background_import_progress['status'] = (
                            f'Profiles {_name}: '
                            f'{current}/{total}'
                        )
                
                success, failed = profiles.extract_all_from_db(
                    region_id=region_id,
                    market_db=full_db,
                    progress_callback=profile_progress
                )
                
                print(f"[BackgroundImport] {hub_name}: "
                      f"{success} profiles built, {failed} failed")
            
            # Signal UI to refresh (if callback registered)
            if _on_profiles_ready:
                try:
                    from core.tk_queue import submit
                    submit(_on_profiles_ready)
                    print("[BackgroundImport] Submitted UI refresh")
                except Exception as e:
                    print(f"[BackgroundImport] Could not submit "
                          f"UI refresh: {e}")
            
        except Exception as e:
            print(f"[BackgroundImport] Profile building failed: {e}")
            import traceback
            traceback.print_exc()
            # Not fatal - profiles can be built manually later
        
        full_db.close()
        
        # Write marker file to signal swap needed at startup
        marker_path.write_text("pending")
        
        # Update status
        _background_import_progress['status'] = 'Complete - profiles built, restart to merge databases'
        _background_import_progress['complete'] = True
        _background_import_progress['restart_required'] = True
        
        print("[BackgroundImport] Import complete, restart required to activate")
        
        if on_complete:
            on_complete()
            
    except Exception as e:
        print(f"[BackgroundImport] Import failed: {e}")
        import traceback
        traceback.print_exc()
        _background_import_progress['status'] = f'Failed: {e}'
        
        # Cleanup
        if full_db_path.exists():
            try:
                full_db_path.unlink()
            except Exception:
                pass


# =============================================================================
# Legacy compatibility - remove hot swap functions
# =============================================================================

def is_swap_pending() -> bool:
    """Legacy - now use is_restart_required()."""
    return False


def perform_pending_swap() -> bool:
    """Legacy - swap now happens at startup."""
    return False
