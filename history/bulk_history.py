"""Bulk market history manager using data.everef.net.

Downloads daily CSV files containing complete market history,
loads into memory for instant lookups during scans.

This replaces per-item ESI calls with a single bulk download per region.
"""

import os
import bz2
import csv
import asyncio
import aiohttp
from datetime import datetime, timezone, date
from typing import Optional, Callable
from pathlib import Path

from core.ssl_context import make_connector

# Regions we care about
REGION_IDS = {
    "the_forge": 10000002,      # Jita
    "domain": 10000043,          # Amarr
    "sinq_laison": 10000032,     # Dodixie
    "metropolis": 10000042,      # Hek
    "heimatar": 10000030,        # Rens
}

# Reverse lookup
REGION_NAMES = {v: k for k, v in REGION_IDS.items()}

# Cache directory
CACHE_DIR = Path("cache/history")

# everef.net base URL for market history
EVEREF_BASE_URL = "https://data.everef.net/market-history"

# File extension - everef uses bzip2 compression
FILE_EXTENSION = ".csv.bz2"


class BulkHistoryManager:
    """
    Manages bulk market history downloads from everef.net.
    
    Downloads compressed CSV files once per day, loads into memory
    for O(1) lookups during market scans.
    """
    
    def __init__(self):
        # In-memory cache: {region_id: {type_id: [history_records]}}
        self.history_data: dict[int, dict[int, list[dict]]] = {}
        
        # Track what's loaded
        self.loaded_regions: set[int] = set()
        self.load_date: Optional[date] = None
        
        # Stats
        self.total_records = 0
        self.download_time = 0.0
        self.parse_time = 0.0
    
    def ensure_cache_dir(self):
        """Create cache directory if it doesn't exist."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    
    def get_cache_path(self, region_id: int, file_date: date) -> Path:
        """Get local cache path for a region's history file."""
        date_str = file_date.strftime("%Y-%m-%d")
        return CACHE_DIR / f"history_{region_id}_{date_str}.csv.gz"
    
    def get_latest_file_url(self, region_id: int) -> str:
        """
        Get URL for the latest history file for a region.
        
        everef.net structure: /market-history/YYYY/market-history-YYYY-MM-DD.csv.bz2
        Files contain ALL regions, not per-region files.
        """
        today = date.today()
        year = today.year
        date_str = today.strftime("%Y-%m-%d")
        return f"{EVEREF_BASE_URL}/{year}/market-history-{date_str}{FILE_EXTENSION}"
    
    def get_yesterday_file_url(self) -> str:
        """Get URL for yesterday's file (in case today's isn't ready yet)."""
        from datetime import timedelta
        yesterday = date.today() - timedelta(days=1)
        year = yesterday.year
        date_str = yesterday.strftime("%Y-%m-%d")
        return f"{EVEREF_BASE_URL}/{year}/market-history-{date_str}{FILE_EXTENSION}"
    
    async def download_file(
        self,
        url: str,
        dest_path: Path,
        progress_callback: Optional[Callable[[str, int], None]] = None
    ) -> bool:
        """
        Download a file from URL to local path.
        
        Returns True on success, False on failure.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=300)  # 5 min timeout for large files
            async with aiohttp.ClientSession(connector=make_connector(), timeout=timeout) as session:
                if progress_callback:
                    progress_callback(f"Downloading {url.split('/')[-1]}...", 10)
                
                print(f"[BULK] Starting download: {url}")
                async with session.get(url) as response:
                    print(f"[BULK] Response status: {response.status}")
                    if response.status == 404:
                        print(f"[BULK] 404 Not Found: {url}")
                        return False
                    response.raise_for_status()
                    
                    # Get file size for progress
                    total_size = int(response.headers.get('content-length', 0))
                    print(f"[BULK] Content-Length: {total_size} bytes ({total_size/1024/1024:.1f} MB)")
                    
                    # Check minimum size - complete files are usually 400KB+
                    # Skip incomplete files that are still being built
                    MIN_COMPLETE_SIZE = 100 * 1024  # 100 KB minimum
                    if total_size < MIN_COMPLETE_SIZE:
                        print(f"[BULK] File too small ({total_size} bytes) - likely incomplete, skipping")
                        return False
                    
                    downloaded = 0
                    
                    # Stream to file
                    self.ensure_cache_dir()
                    with open(dest_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if progress_callback and total_size > 0:
                                pct = int(10 + (downloaded / total_size) * 30)
                                mb = downloaded / (1024 * 1024)
                                progress_callback(f"Downloading... {mb:.1f} MB", pct)
                    
                    print(f"[BULK] Download complete: {downloaded} bytes to {dest_path}")
                    return True
                    
        except aiohttp.ClientResponseError as e:
            print(f"[BULK] HTTP error: {e.status} {e.message}")
            return False
        except aiohttp.ClientError as e:
            print(f"[BULK] Client error: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            print(f"[BULK] Download error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def parse_csv_file(
        self,
        file_path: Path,
        region_ids: set[int],
        progress_callback: Optional[Callable[[str, int], None]] = None
    ) -> dict[int, dict[int, list[dict]]]:
        """
        Parse a bz2-compressed CSV file and extract history for specified regions.
        
        CSV format (from everef.net):
        region_id,type_id,date,average,highest,lowest,order_count,volume
        
        Returns: {region_id: {type_id: [history_records]}}
        """
        result: dict[int, dict[int, list[dict]]] = {rid: {} for rid in region_ids}
        
        if progress_callback:
            progress_callback("Parsing history data...", 45)
        
        record_count = 0
        
        try:
            with bz2.open(file_path, 'rt', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    try:
                        region_id = int(row['region_id'])
                        
                        # Skip regions we don't care about
                        if region_id not in region_ids:
                            continue
                        
                        type_id = int(row['type_id'])
                        
                        # Build history record (same format as ESI)
                        record = {
                            'date': row['date'],
                            'average': float(row['average']),
                            'highest': float(row['highest']),
                            'lowest': float(row['lowest']),
                            'order_count': int(row['order_count']),
                            'volume': int(row['volume'])
                        }
                        
                        # Add to result
                        if type_id not in result[region_id]:
                            result[region_id][type_id] = []
                        result[region_id][type_id].append(record)
                        
                        record_count += 1
                        
                        # Progress update every 100k records
                        if progress_callback and record_count % 100000 == 0:
                            pct = min(45 + (record_count // 100000) * 5, 90)
                            progress_callback(f"Parsed {record_count:,} records...", pct)
                    
                    except (ValueError, KeyError) as e:
                        # Skip malformed rows
                        continue
        
        except Exception as e:
            print(f"Parse error: {e}")
            return result
        
        if progress_callback:
            progress_callback(f"Parsed {record_count:,} records", 90)
        
        self.total_records = record_count
        return result
    
    def _find_recent_cache_file(self, max_age_hours: int = 72) -> Optional[Path]:
        """
        Find the most recent cached history file within max_age_hours.
        
        Returns the path if found, None otherwise.
        Note: We skip today and yesterday since those files are typically incomplete.
        """
        from datetime import timedelta
        
        self.ensure_cache_dir()
        
        # Check 2 days ago and 3 days ago (skip today/yesterday - incomplete)
        for days_ago in range(2, 4):
            check_date = date.today() - timedelta(days=days_ago)
            cache_path = CACHE_DIR / f"market-history-{check_date.strftime('%Y-%m-%d')}{FILE_EXTENSION}"
            
            if cache_path.exists():
                # Check file age
                file_mtime = datetime.fromtimestamp(cache_path.stat().st_mtime)
                age_hours = (datetime.now() - file_mtime).total_seconds() / 3600
                
                if age_hours < max_age_hours:
                    return cache_path
        
        return None

    async def load_history(
        self,
        region_ids: list[int],
        progress_callback: Optional[Callable[[str, int], None]] = None,
        force_refresh: bool = False
    ) -> bool:
        """
        Load market history for specified regions.
        
        Downloads from everef.net if needed, parses into memory.
        Reuses cached files if less than 24 hours old.
        
        Args:
            region_ids: List of region IDs to load
            progress_callback: Optional (status, percent) callback
            force_refresh: If True, re-download even if cached
        
        Returns:
            True if history loaded successfully
        """
        import time
        from datetime import timedelta
        start_time = time.time()
        
        print(f"[BULK] load_history called for regions: {region_ids}")
        
        region_set = set(region_ids)
        today = date.today()
        
        # Check if we already have data loaded in memory for these regions
        if not force_refresh and self.load_date is not None:
            if region_set.issubset(self.loaded_regions):
                print(f"[BULK] Already loaded in memory, skipping")
                if progress_callback:
                    progress_callback("History already loaded in memory", 100)
                return True
        
        # Look for a recent cached file (< 24 hours old)
        cache_path = self._find_recent_cache_file(max_age_hours=24)
        print(f"[BULK] Recent cache file: {cache_path}")
        
        if cache_path is None or force_refresh:
            # Need to download - try today, yesterday, and a few more days back
            cache_path = None
            
            for days_ago in range(2, 9):  # Start from 2 days ago, try up to 8 days back
                check_date = today - timedelta(days=days_ago)
                year = check_date.year
                date_str = check_date.strftime("%Y-%m-%d")
                url = f"{EVEREF_BASE_URL}/{year}/market-history-{date_str}{FILE_EXTENSION}"
                file_path = CACHE_DIR / f"market-history-{date_str}{FILE_EXTENSION}"
                
                if days_ago == 2:
                    print(f"[BULK] Downloading from 2 days ago: {url}")
                    if progress_callback:
                        progress_callback("Downloading market history from everef.net...", 5)
                else:
                    print(f"[BULK] Trying {days_ago} days ago: {url}")
                
                download_start = time.time()
                success = await self.download_file(url, file_path, progress_callback)
                
                if success:
                    cache_path = file_path
                    self.download_time = time.time() - download_start
                    print(f"[BULK] Download successful: {file_path}")
                    break
            
            if cache_path is None:
                print(f"[BULK] All download attempts failed - bulk history unavailable")
                if progress_callback:
                    progress_callback("Bulk history unavailable, using ESI", 100)
                return False
        else:
            # Using cached file
            self.download_time = 0.0
            print(f"[BULK] Using existing cache file: {cache_path}")
            if progress_callback:
                file_date = cache_path.stem.replace("market-history-", "")
                progress_callback(f"Using cached history from {file_date}...", 40)
        
        # Parse the file
        if progress_callback:
            progress_callback("Loading history into memory...", 40)
        
        parse_start = time.time()
        parsed_data = self.parse_csv_file(cache_path, region_set, progress_callback)
        self.parse_time = time.time() - parse_start
        
        # Merge into our cache
        for region_id, type_data in parsed_data.items():
            if region_id not in self.history_data:
                self.history_data[region_id] = {}
            self.history_data[region_id].update(type_data)
            self.loaded_regions.add(region_id)
        
        self.load_date = today
        
        total_time = time.time() - start_time
        
        if progress_callback:
            types_loaded = sum(len(types) for types in self.history_data.values())
            progress_callback(
                f"Loaded {self.total_records:,} records for {types_loaded:,} items in {total_time:.1f}s",
                100
            )
        
        print(f"Bulk history loaded: {self.total_records:,} records, "
              f"download={self.download_time:.1f}s, parse={self.parse_time:.1f}s")
        
        return True
    
    def get_history(self, region_id: int, type_id: int) -> list[dict]:
        """
        Get history for a specific item in a region.
        
        Returns empty list if not found.
        This is O(1) - just a dict lookup.
        """
        region_data = self.history_data.get(region_id, {})
        return region_data.get(type_id, [])
    
    def get_history_bulk(self, region_id: int, type_ids: list[int]) -> dict[int, list[dict]]:
        """
        Get history for multiple items in a region.
        
        Returns dict mapping type_id to history list.
        Missing items get empty lists.
        """
        region_data = self.history_data.get(region_id, {})
        return {tid: region_data.get(tid, []) for tid in type_ids}
    
    def has_history(self, region_id: int, type_id: int) -> bool:
        """Check if we have history for an item."""
        return type_id in self.history_data.get(region_id, {})
    
    def get_stats(self) -> str:
        """Get human-readable stats about loaded data."""
        if not self.loaded_regions:
            return "No data loaded"
        
        types_count = sum(len(types) for types in self.history_data.values())
        regions_str = ", ".join(REGION_NAMES.get(rid, str(rid)) for rid in self.loaded_regions)
        
        return f"{types_count:,} items, {self.total_records:,} records ({regions_str})"
    
    def clear(self):
        """Clear all loaded data."""
        self.history_data = {}
        self.loaded_regions = set()
        self.load_date = None
        self.total_records = 0


# Singleton instance for app-wide use
_manager: Optional[BulkHistoryManager] = None


def get_bulk_history_manager() -> BulkHistoryManager:
    """Get or create the singleton BulkHistoryManager."""
    global _manager
    if _manager is None:
        _manager = BulkHistoryManager()
    return _manager
