"""Daily update functions for market history database.

Downloads missing date files from everef and imports them.
Called from gui_migration.py and runs in background thread.

Network-resilient: skips files that fail to download.
"""

import asyncio
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, List, Callable

from history.market_history import MarketHistoryDB, REGION_IDS


# Everef is usually 2 days behind
EVEREF_LAG_DAYS = 2

# Network timeout per file (seconds)
DOWNLOAD_TIMEOUT = 30


def get_missing_dates_from_db(db: MarketHistoryDB) -> List[str]:
    """Get list of dates missing from database."""
    latest = db.get_latest_date()
    
    if latest is None:
        return []
    
    try:
        year, month, day = map(int, latest.split('-'))
        latest_date = date(year, month, day)
    except (ValueError, AttributeError):
        return []
    
    available_date = date.today() - timedelta(days=EVEREF_LAG_DAYS)
    
    missing = []
    check_date = latest_date + timedelta(days=1)
    
    while check_date <= available_date:
        missing.append(check_date.strftime('%Y-%m-%d'))
        check_date += timedelta(days=1)
    
    return missing


async def download_missing_dates(missing_dates: List[str], 
                                  progress_callback: Optional[Callable] = None) -> List[Path]:
    """Download missing date files from everef.
    
    Network-resilient: skips files that fail to download.
    Daily update will retry later.
    """
    from history.archive_downloader import ArchiveDownloader, ARCHIVE_PATH
    
    downloader = ArchiveDownloader(auto_decompress=True)
    downloaded = []
    skipped = 0
    total = len(missing_dates)
    
    print(f"[Debug] download_missing_dates: {total} dates to process")
    
    for i, date_str in enumerate(missing_dates):
        if progress_callback:
            progress_callback(f"Downloading {date_str}...", i, total)
        
        year = date_str.split('-')[0]
        year_path = ARCHIVE_PATH / year
        year_path.mkdir(parents=True, exist_ok=True)
        
        csv_path = year_path / f"market-history-{date_str}.csv"
        bz2_path = year_path / f"market-history-{date_str}.csv.bz2"
        
        # Check if already exists
        if csv_path.exists():
            downloaded.append(csv_path)
            continue
        elif bz2_path.exists():
            if downloader.decompress_file(bz2_path):
                downloaded.append(csv_path)
            continue
        
        # Try to download
        try:
            import aiohttp
            from core.ssl_context import make_connector
            url = f"https://data.everef.net/market-history/{year}/market-history-{date_str}.csv.bz2"
            
            print(f"[Debug] Downloading: {url}")
            
            timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            async with aiohttp.ClientSession(connector=make_connector(), timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(bz2_path, 'wb') as f:
                            f.write(content)
                        if downloader.decompress_file(bz2_path):
                            downloaded.append(csv_path)
                            print(f"[Debug] Downloaded: {date_str}")
                    elif resp.status == 404:
                        print(f"[DailyUpdate] File not available yet: {date_str}")
                        skipped += 1
                    else:
                        print(f"[DailyUpdate] HTTP {resp.status} for {date_str}")
                        skipped += 1
                        
        except asyncio.TimeoutError:
            print(f"[DailyUpdate] Timeout downloading {date_str}, skipping")
            skipped += 1
        except Exception as e:
            print(f"[DailyUpdate] Error downloading {date_str}: {e}, skipping")
            skipped += 1
    
    print(f"[Debug] download_missing_dates: {len(downloaded)} downloaded, {skipped} skipped")
    return downloaded


def import_daily_files(db: MarketHistoryDB, files: List[Path]) -> int:
    """Import downloaded daily files into database."""
    # region_filter=None imports every region present in the everef
    # daily file.  Was previously the 5-hub filter; the all-regions
    # switch is part of the custom-stations groundwork (see
    # gui_migration.run_migration_if_needed for the matching backfill).
    region_filter = None
    total = 0
    
    for csv_path in files:
        try:
            records = db.import_file(csv_path, region_filter=region_filter)
            total += records
        except Exception as e:
            print(f"[DailyUpdate] Error importing {csv_path.name}: {e}")
    
    return total


def run_daily_update(db: MarketHistoryDB) -> int:
    """Run daily update - download and import missing dates."""
    missing = get_missing_dates_from_db(db)
    
    if not missing:
        return 0
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        downloaded = loop.run_until_complete(download_missing_dates(missing))
    finally:
        loop.close()
    
    if not downloaded:
        return 0
    
    return import_daily_files(db, downloaded)


def run_daily_update_background(db: MarketHistoryDB, callback: Optional[Callable] = None):
    """Run daily update in background thread."""
    def _run():
        print("[Debug] run_daily_update_background: thread starting")
        thread_db = MarketHistoryDB(db.db_path)
        thread_db.init_db()
        records = run_daily_update(thread_db)
        thread_db.close()
        print(f"[Debug] run_daily_update_background: imported {records} records")
        if callback:
            callback(records)
    
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread
