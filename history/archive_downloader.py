"""Archive downloader for EVE Market Scout stock market.

Downloads raw market history files from data.everef.net.
Supports pause/resume, progress tracking, and locating existing archives.

Archive structure:
    cache/history-archive/
        2023/
            market-history-2023-01-01.csv.bz2
            market-history-2023-01-02.csv.bz2
            ...
        2024/
        2025/
        2026/
"""

import os
import json
import asyncio
import aiohttp
import re
from datetime import date, timedelta
from typing import Optional, Callable, List, Dict, Set
from pathlib import Path
from dataclasses import dataclass, asdict

from core.sound_manager import get_data_dir
from core.ssl_context import make_connector
from core.config import ESI_USER_AGENT


# Constants
EVEREF_BASE_URL = "https://data.everef.net/market-history"
ARCHIVE_PATH = get_data_dir() / "history-archive"
STATE_FILE = str(get_data_dir() / "archive_download_state.json")

# How many years back to download (current year + N prior)
YEARS_TO_DOWNLOAD = 3


@dataclass
class DownloadState:
    """Tracks download progress for pause/resume."""
    year: int = 0
    files_completed: List[str] = None  # List of completed file dates
    files_remaining: List[str] = None  # List of remaining file dates
    bytes_downloaded: int = 0
    paused: bool = False
    
    def __post_init__(self):
        if self.files_completed is None:
            self.files_completed = []
        if self.files_remaining is None:
            self.files_remaining = []


class ArchiveDownloader:
    """Downloads and manages everef archive files."""
    
    def __init__(self, archive_path: Path = ARCHIVE_PATH, auto_decompress: bool = True):
        self.archive_path = archive_path
        self.auto_decompress = auto_decompress
        self._paused = False
        self._state: Optional[DownloadState] = None
        self._session: Optional[aiohttp.ClientSession] = None
        
        self._ensure_dirs()
    
    def _ensure_dirs(self):
        """Create archive directory if needed."""
        self.archive_path.mkdir(parents=True, exist_ok=True)
    
    # =========================================================================
    # Discovery
    # =========================================================================
    
    def get_years_to_download(self) -> List[int]:
        """Get list of years we want to download (current + N prior)."""
        current_year = date.today().year
        return [current_year - i for i in range(YEARS_TO_DOWNLOAD + 1)]
    
    def get_downloaded_years(self) -> List[int]:
        """Get list of years that have local folders."""
        years = []
        for item in self.archive_path.iterdir():
            if item.is_dir() and item.name.isdigit():
                years.append(int(item.name))
        return sorted(years, reverse=True)
    
    def get_year_file_count(self, year: int) -> int:
        """Get expected number of files for a year.
        
        Full year = 365 (or 366 for leap year).
        Current year = days elapsed so far.
        """
        today = date.today()
        
        if year < today.year:
            # Past year - check if leap year
            start = date(year, 1, 1)
            end = date(year, 12, 31)
            return (end - start).days + 1
        elif year == today.year:
            # Current year - days so far (minus a couple for processing delay)
            start = date(year, 1, 1)
            # everef usually 1-2 days behind
            effective_end = today - timedelta(days=2)
            if effective_end < start:
                return 0
            return (effective_end - start).days + 1
        else:
            return 0  # Future year
    
    def get_downloaded_file_count(self, year: int) -> int:
        """Get number of files actually downloaded for a year.
        
        Counts unique dates - a date with .csv OR .csv.bz2 counts as present.
        """
        year_path = self.archive_path / str(year)
        if not year_path.exists():
            return 0
        
        # Collect unique dates from both formats
        dates = set()
        for f in year_path.glob("market-history-*.csv.bz2"):
            match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", f.name)
            if match:
                dates.add(match.group(1))
        for f in year_path.glob("market-history-*.csv"):
            # Exclude .csv.bz2 (already matched above)
            if not f.name.endswith(".bz2"):
                match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", f.name)
                if match:
                    dates.add(match.group(1))
        
        return len(dates)
    
    def get_missing_dates(self, year: int) -> List[str]:
        """Get list of dates missing for a year.
        
        A date is present if either .csv or .csv.bz2 exists.
        Returns list of date strings like '2024-01-15'.
        """
        year_path = self.archive_path / str(year)
        
        # Get all expected dates
        expected = self._get_expected_dates(year)
        
        # Get downloaded dates (either format counts)
        downloaded = set()
        if year_path.exists():
            for f in year_path.glob("market-history-*.csv.bz2"):
                match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", f.name)
                if match:
                    downloaded.add(match.group(1))
            for f in year_path.glob("market-history-*.csv"):
                if not f.name.endswith(".bz2"):
                    match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", f.name)
                    if match:
                        downloaded.add(match.group(1))
        
        # Return missing
        return [d for d in expected if d not in downloaded]
    
    def _get_expected_dates(self, year: int) -> List[str]:
        """Get list of expected date strings for a year."""
        today = date.today()
        
        start = date(year, 1, 1)
        
        if year < today.year:
            end = date(year, 12, 31)
        elif year == today.year:
            # everef usually 1-2 days behind
            end = today - timedelta(days=2)
            if end < start:
                return []
        else:
            return []
        
        dates = []
        current = start
        while current <= end:
            dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        
        return dates
    
    def get_download_summary(self) -> Dict[int, Dict]:
        """Get summary of download status for all years.
        
        Returns:
            {year: {expected: N, downloaded: N, missing: N, percent: X}}
        """
        summary = {}
        
        for year in self.get_years_to_download():
            expected = self.get_year_file_count(year)
            downloaded = self.get_downloaded_file_count(year)
            missing = expected - downloaded
            percent = (downloaded / expected * 100) if expected > 0 else 0
            
            summary[year] = {
                "expected": expected,
                "downloaded": downloaded,
                "missing": missing,
                "percent": round(percent, 1),
            }
        
        return summary
    
    # =========================================================================
    # Download Operations
    # =========================================================================
    
    async def download_year(
        self,
        year: int,
        progress_callback: Optional[Callable[[str, int, int, int, int], None]] = None
    ) -> bool:
        """Download all missing files for a year.
        
        Args:
            year: Year to download
            progress_callback: Optional callback(status, files_done, files_total, bytes_done, bytes_total)
            
        Returns:
            True if completed (not paused), False if paused or error.
        """
        year_path = self.archive_path / str(year)
        year_path.mkdir(parents=True, exist_ok=True)
        
        # Get missing dates
        missing = self.get_missing_dates(year)
        
        if not missing:
            if progress_callback:
                progress_callback(f"{year} complete", 0, 0, 0, 0)
            return True
        
        # Initialize state for pause/resume
        self._state = DownloadState(
            year=year,
            files_completed=[],
            files_remaining=missing.copy(),
            bytes_downloaded=0,
            paused=False,
        )
        
        total_files = len(missing)
        files_done = 0
        
        # Estimate ~500KB per file based on typical sizes
        bytes_total_est = total_files * 500 * 1024
        
        async with aiohttp.ClientSession(connector=make_connector(), headers={"User-Agent": ESI_USER_AGENT}) as session:
            self._session = session

            for date_str in missing:
                if self._paused:
                    self._state.paused = True
                    self.save_state()
                    return False
                
                url = f"{EVEREF_BASE_URL}/{year}/market-history-{date_str}.csv.bz2"
                dest = year_path / f"market-history-{date_str}.csv.bz2"
                
                if progress_callback:
                    progress_callback(
                        f"Downloading {date_str}",
                        files_done,
                        total_files,
                        self._state.bytes_downloaded,
                        bytes_total_est
                    )
                
                success, bytes_dl = await self._download_file(session, url, dest)
                
                if success:
                    files_done += 1
                    self._state.bytes_downloaded += bytes_dl
                    self._state.files_completed.append(date_str)
                    self._state.files_remaining.remove(date_str)
                    
                    # Auto-decompress if enabled
                    if self.auto_decompress:
                        self.decompress_file(dest)
                else:
                    # File might not exist yet (too recent) - skip
                    self._state.files_remaining.remove(date_str)
            
            self._session = None
        
        if progress_callback:
            progress_callback(
                f"{year} complete",
                files_done,
                total_files,
                self._state.bytes_downloaded,
                bytes_total_est
            )
        
        # Clear state on completion
        self._state = None
        self.clear_state()
        
        return True
    
    async def download_all_years(
        self,
        progress_callback: Optional[Callable[[str, int, int, int, int], None]] = None
    ) -> bool:
        """Download all missing files for all years.
        
        Downloads oldest years first so newest data comes last.
        """
        years = sorted(self.get_years_to_download())  # Oldest first
        
        for year in years:
            if self._paused:
                return False
            
            success = await self.download_year(year, progress_callback)
            if not success and self._paused:
                return False
        
        return True
    
    async def _download_file(
        self,
        session: aiohttp.ClientSession,
        url: str,
        dest: Path
    ) -> tuple[bool, int]:
        """Download a single file.
        
        Returns:
            (success, bytes_downloaded)
        """
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    
                    with open(dest, "wb") as f:
                        f.write(content)
                    
                    return True, len(content)
                elif resp.status == 404:
                    # File doesn't exist (date too recent)
                    return False, 0
                else:
                    print(f"[Archive] HTTP {resp.status} for {url}")
                    return False, 0
        
        except asyncio.TimeoutError:
            print(f"[Archive] Timeout downloading {url}")
            return False, 0
        except Exception as e:
            print(f"[Archive] Error downloading {url}: {e}")
            return False, 0
    
    # =========================================================================
    # Pause/Resume
    # =========================================================================
    
    def pause(self):
        """Pause current download."""
        self._paused = True
    
    def resume(self):
        """Clear pause flag."""
        self._paused = False
    
    def is_paused(self) -> bool:
        """Check if download is paused."""
        return self._paused
    
    def save_state(self):
        """Save download state to disk for resume."""
        if not self._state:
            return
        
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(asdict(self._state), f, indent=2)
        except IOError as e:
            print(f"[Archive] Error saving state: {e}")
    
    def load_state(self) -> Optional[DownloadState]:
        """Load saved download state."""
        if not os.path.exists(STATE_FILE):
            return None
        
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return DownloadState(**data)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[Archive] Error loading state: {e}")
            return None
    
    def clear_state(self):
        """Clear saved download state."""
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    
    def has_incomplete_download(self) -> bool:
        """Check if there's a paused download to resume."""
        state = self.load_state()
        return state is not None and state.paused
    
    async def resume_download(
        self,
        progress_callback: Optional[Callable[[str, int, int, int, int], None]] = None
    ) -> bool:
        """Resume a paused download."""
        state = self.load_state()
        if not state or not state.paused:
            return False
        
        self._paused = False
        
        year = state.year
        year_path = self.archive_path / str(year)
        year_path.mkdir(parents=True, exist_ok=True)
        
        remaining = state.files_remaining
        if not remaining:
            self.clear_state()
            return True
        
        total_files = len(state.files_completed) + len(remaining)
        files_done = len(state.files_completed)
        bytes_total_est = total_files * 500 * 1024
        
        self._state = state
        self._state.paused = False
        
        async with aiohttp.ClientSession(connector=make_connector(), headers={"User-Agent": ESI_USER_AGENT}) as session:
            self._session = session

            for date_str in remaining.copy():
                if self._paused:
                    self._state.paused = True
                    self.save_state()
                    return False
                
                url = f"{EVEREF_BASE_URL}/{year}/market-history-{date_str}.csv.bz2"
                dest = year_path / f"market-history-{date_str}.csv.bz2"
                
                if progress_callback:
                    progress_callback(
                        f"Downloading {date_str}",
                        files_done,
                        total_files,
                        self._state.bytes_downloaded,
                        bytes_total_est
                    )
                
                success, bytes_dl = await self._download_file(session, url, dest)
                
                if success:
                    files_done += 1
                    self._state.bytes_downloaded += bytes_dl
                    self._state.files_completed.append(date_str)
                    
                    # Auto-decompress if enabled
                    if self.auto_decompress:
                        self.decompress_file(dest)
                
                self._state.files_remaining.remove(date_str)
            
            self._session = None
        
        self._state = None
        self.clear_state()
        
        return True
    
    # =========================================================================
    # Archive Location
    # =========================================================================
    
    def set_archive_path(self, path: Path) -> bool:
        """Set archive path to an existing folder.
        
        Validates the folder looks like an everef archive.
        Accepts either .csv.bz2 or .csv files.
        
        Returns:
            True if path is valid and set, False otherwise.
        """
        if not path.exists() or not path.is_dir():
            return False
        
        # Check for year folders with archive files (either format)
        valid = False
        for item in path.iterdir():
            if item.is_dir() and item.name.isdigit():
                # Check for at least one archive file in either format
                if list(item.glob("market-history-*.csv.bz2")) or list(item.glob("market-history-*.csv")):
                    valid = True
                    break
        
        if valid:
            self.archive_path = path
            return True
        
        return False
    
    def verify_archive_integrity(self, year: int) -> tuple[int, int]:
        """Verify archive files for a year can be read.
        
        Returns:
            (good_files, bad_files)
        """
        import bz2
        
        year_path = self.archive_path / str(year)
        if not year_path.exists():
            return 0, 0
        
        good = 0
        bad = 0
        
        for f in year_path.glob("market-history-*.csv.bz2"):
            try:
                # Try to decompress first few bytes
                with bz2.open(f, "rt", encoding="utf-8") as fp:
                    fp.read(1024)
                good += 1
            except Exception:
                bad += 1
        
        return good, bad
    
    def get_archive_size_mb(self) -> float:
        """Get total size of archive in MB (both compressed and decompressed)."""
        total = 0
        for year_dir in self.archive_path.iterdir():
            if year_dir.is_dir():
                for f in year_dir.glob("*.bz2"):
                    total += f.stat().st_size
                for f in year_dir.glob("*.csv"):
                    if not f.name.endswith(".bz2"):
                        total += f.stat().st_size
        
        return total / (1024 * 1024)
    
    # =========================================================================
    # Decompression
    # =========================================================================
    
    def decompress_file(self, bz2_path: Path) -> bool:
        """Decompress a single .csv.bz2 file to .csv.
        
        Creates the .csv alongside the .bz2 (does not delete original).
        
        Returns:
            True if successful, False on error.
        """
        import bz2
        
        if not bz2_path.exists():
            return False
        
        # market-history-2024-01-15.csv.bz2 -> market-history-2024-01-15.csv
        csv_path = bz2_path.with_suffix("").with_suffix(".csv")
        
        # Skip if already decompressed
        if csv_path.exists():
            return True
        
        try:
            with bz2.open(bz2_path, "rb") as f_in:
                content = f_in.read()
            
            # Write to temp file then rename for atomicity
            temp_path = csv_path.with_suffix(".csv.tmp")
            with open(temp_path, "wb") as f_out:
                f_out.write(content)
            
            temp_path.rename(csv_path)
            return True
            
        except Exception as e:
            print(f"[Archive] Error decompressing {bz2_path}: {e}")
            # Clean up temp file if exists
            temp_path = csv_path.with_suffix(".csv.tmp")
            if temp_path.exists():
                temp_path.unlink()
            return False
    
    def decompress_year(
        self,
        year: int,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> tuple[int, int]:
        """Decompress all .csv.bz2 files for a year.
        
        Args:
            year: Year to decompress
            progress_callback: Optional callback(status, done, total)
            
        Returns:
            (success_count, error_count)
        """
        year_path = self.archive_path / str(year)
        if not year_path.exists():
            return 0, 0
        
        bz2_files = list(year_path.glob("market-history-*.csv.bz2"))
        total = len(bz2_files)
        success = 0
        errors = 0
        
        for i, bz2_path in enumerate(bz2_files):
            if progress_callback:
                progress_callback(f"Decompressing {bz2_path.name}", i, total)
            
            if self.decompress_file(bz2_path):
                success += 1
            else:
                errors += 1
        
        if progress_callback:
            progress_callback(f"Year {year} complete", total, total)
        
        return success, errors
    
    def decompress_all(
        self,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> tuple[int, int]:
        """Decompress all .csv.bz2 files in the archive.
        
        Args:
            progress_callback: Optional callback(status, done, total)
            
        Returns:
            (success_count, error_count)
        """
        # Count total files first
        total_files = 0
        bz2_files = []
        
        for year in self.get_years_to_download():
            year_path = self.archive_path / str(year)
            if year_path.exists():
                files = list(year_path.glob("market-history-*.csv.bz2"))
                bz2_files.extend(files)
                total_files += len(files)
        
        if total_files == 0:
            return 0, 0
        
        success = 0
        errors = 0
        
        for i, bz2_path in enumerate(bz2_files):
            if progress_callback:
                progress_callback(f"Decompressing {bz2_path.name}", i, total_files)
            
            if self.decompress_file(bz2_path):
                success += 1
            else:
                errors += 1
        
        if progress_callback:
            progress_callback("Decompression complete", total_files, total_files)
        
        return success, errors
    
    def get_decompression_status(self) -> Dict[int, Dict]:
        """Get decompression status for all years.
        
        Returns:
            {year: {compressed: N, decompressed: N, pending: N}}
        """
        status = {}
        
        for year in self.get_years_to_download():
            year_path = self.archive_path / str(year)
            if not year_path.exists():
                status[year] = {"compressed": 0, "decompressed": 0, "pending": 0}
                continue
            
            # Count .csv.bz2 files
            bz2_files = set()
            for f in year_path.glob("market-history-*.csv.bz2"):
                match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", f.name)
                if match:
                    bz2_files.add(match.group(1))
            
            # Count .csv files (not .csv.bz2)
            csv_files = set()
            for f in year_path.glob("market-history-*.csv"):
                if not f.name.endswith(".bz2"):
                    match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", f.name)
                    if match:
                        csv_files.add(match.group(1))
            
            # Pending = has .bz2 but no .csv
            pending = bz2_files - csv_files
            
            status[year] = {
                "compressed": len(bz2_files),
                "decompressed": len(csv_files),
                "pending": len(pending),
            }
        
        return status
    
    def is_fully_decompressed(self) -> bool:
        """Check if all archive files have been decompressed."""
        status = self.get_decompression_status()
        return all(s["pending"] == 0 for s in status.values())
    
    def delete_compressed_files(self, year: Optional[int] = None) -> int:
        """Delete .csv.bz2 files that have been decompressed.
        
        Only deletes .bz2 files where the corresponding .csv exists.
        
        Args:
            year: Specific year, or None for all years
            
        Returns:
            Number of files deleted
        """
        years = [year] if year else self.get_years_to_download()
        deleted = 0
        
        for y in years:
            year_path = self.archive_path / str(y)
            if not year_path.exists():
                continue
            
            for bz2_path in year_path.glob("market-history-*.csv.bz2"):
                csv_path = bz2_path.with_suffix("").with_suffix(".csv")
                if csv_path.exists():
                    try:
                        bz2_path.unlink()
                        deleted += 1
                    except Exception as e:
                        print(f"[Archive] Error deleting {bz2_path}: {e}")
        
        return deleted

