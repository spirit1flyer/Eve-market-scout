"""CSV-based profile extraction fallback.

This module provides CSV file scanning methods used when market_history.db
is not available. These are the "slow path" methods that scan archive files.

Used as a mixin by ProfileExtractionMixin in profile_extraction.py.
"""

import bz2
import csv
import sqlite3
from datetime import date
from typing import Optional, Dict, List, Callable
from pathlib import Path

from analytics.historical_profiles import YearlyStats, MAX_YEARS_BACK


class ProfileCSVMixin:
    """Mixin providing CSV-based extraction methods.
    
    Expects the following attributes on self:
        - archive_path: Path
        - index_db_path: str
        - buy_percentile: int
        - sell_percentile: int
    
    Expects the following methods on self:
        - _calculate_yearly_stats(year, records) -> YearlyStats
        - _save_yearly_stats(type_id, region_id, stats)
        - _save_yearly_stats_batch(stats_list)
        - _save_computed_profile(profile)
        - _save_computed_profiles_batch(profiles)
        - _calculate_weighted_profile(type_id, region_id)
        - _calculate_weighted_profile_from_stats(type_id, region_id, stats_list)
    """
    
    # =========================================================================
    # CSV File Extraction
    # =========================================================================
    
    def _extract_item_from_csv(
        self,
        type_id: int,
        region_id: int,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> bool:
        """Extract historical data for a single item from archive files.
        
        Scans relevant archive files, extracts price data, calculates
        yearly stats, and computes weighted profile.
        
        Args:
            type_id: Item type ID
            region_id: Region ID
            progress_callback: Optional callback(status_msg, files_done, files_total)
            
        Returns:
            True if extraction successful with data found.
        """
        print(f"[Profile-CSV] Extracting type_id={type_id}, region_id={region_id}")
        
        current_year = date.today().year
        years_to_scan = [current_year - i for i in range(MAX_YEARS_BACK + 1)]
        
        # Collect all price/volume data by year
        yearly_data: Dict[int, List[dict]] = {year: [] for year in years_to_scan}
        
        # Check if we have an index for this item
        indexed_dates = self.get_files_for_item(type_id, region_id)
        
        if indexed_dates:
            print(f"[Profile-CSV] Using index: {len(indexed_dates)} files")
            files_to_scan = []
            for file_date in indexed_dates:
                clean_date = file_date.replace(".csv", "")
                year = int(clean_date[:4])
                if year in years_to_scan:
                    csv_path = self.archive_path / str(year) / f"market-history-{clean_date}.csv"
                    bz2_path = self.archive_path / str(year) / f"market-history-{clean_date}.csv.bz2"
                    if csv_path.exists():
                        files_to_scan.append(csv_path)
                    elif bz2_path.exists():
                        files_to_scan.append(bz2_path)
            
            if not files_to_scan:
                print(f"[Profile-CSV] Index stale - falling back to full scan")
                files_to_scan = self._get_archive_files(years_to_scan)
        else:
            # No index - scan all files (slow)
            print(f"[Profile-CSV] No index available - scanning all files (slow)")
            files_to_scan = self._get_archive_files(years_to_scan)
        
        if not files_to_scan:
            print(f"[Profile-CSV] ERROR: No archive files found")
            return False
        
        print(f"[Profile-CSV] Scanning {len(files_to_scan)} files...")
        
        total_records_found = 0
        total_files = len(files_to_scan)
        
        for i, file_path in enumerate(files_to_scan):
            if progress_callback and i % 50 == 0:
                progress_callback(f"Scanning {file_path.name}", i, total_files)
            
            records = self._extract_from_file(file_path, type_id, region_id)
            total_records_found += len(records)
            
            for record in records:
                year = int(record["date"][:4])
                if year in yearly_data:
                    yearly_data[year].append(record)
        
        print(f"[Profile-CSV] Found {total_records_found} records")
        
        # Calculate and store yearly stats
        years_with_data = 0
        for year, records in yearly_data.items():
            if records:
                years_with_data += 1
                stats = self._calculate_yearly_stats(year, records)
                self._save_yearly_stats(type_id, region_id, stats)
        
        if years_with_data == 0:
            print(f"[Profile-CSV] No data found")
            return False
        
        # Calculate and store weighted profile
        profile = self._calculate_weighted_profile(type_id, region_id)
        if profile:
            self._save_computed_profile(profile)
        else:
            print(f"[Profile-CSV] Failed to calculate profile")
            return False
        
        if progress_callback:
            progress_callback("Complete", total_files, total_files)
        
        return True
    
    def refresh_current_year(
        self,
        type_id: int,
        region_id: int,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> bool:
        """Refresh only current year data and recalculate profile.
        
        Faster than full extraction when we just want to update with recent data.
        """
        from datetime import date
        
        current_year = date.today().year
        
        # Get files for current year only
        files = self._get_archive_files([current_year])
        
        if not files:
            return False
        
        # Extract data
        yearly_data = []
        total_files = len(files)
        
        for i, file_path in enumerate(files):
            if progress_callback and i % 50 == 0:
                progress_callback(f"Scanning {file_path.name}", i, total_files)
            
            records = self._extract_from_file(file_path, type_id, region_id)
            yearly_data.extend(records)
        
        if not yearly_data:
            return False
        
        # Calculate and save stats for current year
        stats = self._calculate_yearly_stats(current_year, yearly_data)
        self._save_yearly_stats(type_id, region_id, stats)
        
        # Recalculate weighted profile
        profile = self._calculate_weighted_profile(type_id, region_id)
        if profile:
            self._save_computed_profile(profile)
            return True
        
        return False
    
    def _get_archive_files(self, years: List[int]) -> List[Path]:
        """Get list of archive files for the given years.
        
        Prefers .csv files over .csv.bz2 (faster to read).
        """
        files = []
        
        for year in sorted(years, reverse=True):
            year_path = self.archive_path / str(year)
            if not year_path.exists():
                continue
            
            # Build a dict of date -> file path, preferring csv
            date_files = {}
            
            # First pass: collect bz2 files
            for f in year_path.glob("market-history-*.csv.bz2"):
                import re
                match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", f.name)
                if match:
                    date_files[match.group(1)] = f
            
            # Second pass: override with csv files (preferred)
            for f in year_path.glob("market-history-*.csv"):
                if not f.name.endswith(".bz2"):
                    import re
                    match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", f.name)
                    if match:
                        date_files[match.group(1)] = f
            
            # Sort by date and add to list
            for file_date in sorted(date_files.keys()):
                files.append(date_files[file_date])
        
        return files
    
    def _extract_from_file(self, file_path: Path, type_id: int, region_id: int) -> List[dict]:
        """Extract records for a specific item from an archive file."""
        records = []
        
        try:
            # Choose open method based on file extension
            if file_path.name.endswith(".bz2"):
                f = bz2.open(file_path, "rt", encoding="utf-8")
            else:
                f = open(file_path, "r", encoding="utf-8")
            
            with f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    if (int(row.get("type_id", 0)) == type_id and 
                        int(row.get("region_id", 0)) == region_id):
                        records.append({
                            "date": row.get("date", ""),
                            "average": float(row.get("average", 0)),
                            "highest": float(row.get("highest", 0)),
                            "lowest": float(row.get("lowest", 0)),
                            "volume": int(row.get("volume", 0)),
                            "order_count": int(row.get("order_count", 0)),
                        })
        
        except Exception as e:
            print(f"[Profile-CSV] Error reading {file_path}: {e}")
        
        return records
    
    def _extract_all_from_csv(
        self,
        region_id: int,
        type_ids: Optional[List[int]] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None
    ) -> tuple:
        """Extract profiles for all items by scanning archive files once.
        
        MUCH faster than item-by-item extraction because each file is read
        only once, extracting data for ALL items in a single pass.
        
        Args:
            region_id: Region ID to extract
            type_ids: Optional list of type_ids to extract (None = all found)
            progress_callback: Optional callback(status_msg, files_done, files_total)
            cancel_check: Optional callable that returns True if cancelled
            
        Returns:
            (success_count, fail_count)
        """
        import time
        from collections import defaultdict
        
        current_year = date.today().year
        years_to_scan = [current_year - i for i in range(MAX_YEARS_BACK + 1)]
        
        # Get all archive files
        files = self._get_archive_files(years_to_scan)
        total_files = len(files)
        
        if not files:
            print(f"[Profile-CSV Batch] No archive files found")
            return 0, 0
        
        print(f"[Profile-CSV Batch] Starting extraction for region {region_id}")
        print(f"[Profile-CSV Batch] Scanning {total_files} files...")
        if type_ids:
            print(f"[Profile-CSV Batch] Filtering to {len(type_ids)} specific items")
            type_id_set = set(type_ids)
        else:
            type_id_set = None
        
        start_time = time.time()
        
        # Data structure: {type_id: {year: [records]}}
        all_data = defaultdict(lambda: defaultdict(list))
        
        # Scan all files
        for file_idx, file_path in enumerate(files):
            if cancel_check and cancel_check():
                print(f"[Profile-CSV Batch] Cancelled at file {file_idx}/{total_files}")
                break
            
            if progress_callback:
                progress_callback(f"Reading {file_path.name}", file_idx, total_files)
            
            # Progress logging every 50 files
            if file_idx > 0 and file_idx % 50 == 0:
                elapsed = time.time() - start_time
                rate = file_idx / elapsed
                remaining = (total_files - file_idx) / rate if rate > 0 else 0
                items_found = len(all_data)
                print(f"[Profile-CSV Batch] Files: {file_idx}/{total_files}, {items_found} items, ~{remaining:.0f}s remaining")
            
            # Read and parse file
            try:
                if file_path.name.endswith('.bz2'):
                    f = bz2.open(file_path, 'rt', encoding='utf-8')
                else:
                    f = open(file_path, 'r', encoding='utf-8')
                
                with f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        try:
                            row_region = int(row.get('region_id', 0))
                            if row_region != region_id:
                                continue
                            
                            row_type_id = int(row.get('type_id', 0))
                            if type_id_set and row_type_id not in type_id_set:
                                continue
                            
                            row_date = row.get('date', '')
                            if not row_date:
                                continue
                            
                            year = int(row_date[:4])
                            if year not in years_to_scan:
                                continue
                            
                            all_data[row_type_id][year].append({
                                'date': row_date,
                                'average': float(row.get('average', 0)),
                                'highest': float(row.get('highest', 0)),
                                'lowest': float(row.get('lowest', 0)),
                                'volume': int(row.get('volume', 0)),
                                'order_count': int(row.get('order_count', 0)),
                            })
                        except (ValueError, KeyError):
                            continue
            except Exception as e:
                print(f"[Profile-CSV Batch] Error reading {file_path}: {e}")
                continue
        
        scan_elapsed = time.time() - start_time
        print(f"[Profile-CSV Batch] Scan complete: {len(all_data)} items in {scan_elapsed:.1f}s")
        
        if not all_data:
            return 0, 0
        
        # Calculate stats and profiles
        print(f"[Profile-CSV Batch] Calculating profiles...")
        
        all_yearly_stats = []
        all_profiles = []
        success_count = 0
        fail_count = 0
        
        type_ids_to_process = list(all_data.keys())
        total_items = len(type_ids_to_process)
        
        for item_idx, type_id in enumerate(type_ids_to_process):
            if cancel_check and cancel_check():
                break
            
            if item_idx > 0 and item_idx % 100 == 0:
                print(f"[Profile-CSV Batch] Calculating: {item_idx}/{total_items}")
            
            yearly_data = all_data[type_id]
            
            # Calculate yearly stats
            item_stats = []
            for year, records in yearly_data.items():
                if records:
                    stats = self._calculate_yearly_stats(year, records)
                    item_stats.append((type_id, region_id, stats))
            
            if not item_stats:
                fail_count += 1
                continue
            
            all_yearly_stats.extend(item_stats)
            
            # Calculate profile
            profile = self._calculate_weighted_profile_from_stats(
                type_id, region_id, [s[2] for s in item_stats]
            )
            
            if profile:
                all_profiles.append(profile)
                success_count += 1
            else:
                fail_count += 1
        
        # Batch save
        print(f"[Profile-CSV Batch] Saving {len(all_profiles)} profiles...")
        
        if progress_callback:
            progress_callback("Saving to database...", total_files, total_files)
        
        self._save_yearly_stats_batch(all_yearly_stats)
        self._save_computed_profiles_batch(all_profiles)
        
        total_elapsed = time.time() - start_time
        print(f"[Profile-CSV Batch] Complete: {success_count} profiles in {total_elapsed:.1f}s")
        
        return success_count, fail_count
    
    # =========================================================================
    # Index Management
    # =========================================================================
    
    def _parse_file_for_index(self, file_path: Path) -> tuple:
        """Parse a single file and return (file_date, set of (type_id, region_id) pairs).
        
        Uses positional CSV reader for speed. Returns empty set on error.
        """
        import re
        match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", file_path.name)
        if not match:
            return None, set()
        file_date = match.group(1)
        
        pairs_found = set()
        
        try:
            if file_path.name.endswith(".bz2"):
                f = bz2.open(file_path, "rt", encoding="utf-8")
            else:
                f = open(file_path, "r", encoding="utf-8")
            
            with f:
                # Read header to find column positions
                header = f.readline().strip().split(',')
                try:
                    type_idx = header.index('type_id')
                    region_idx = header.index('region_id')
                except ValueError:
                    print(f"[Index] Missing columns in {file_path.name}")
                    return file_date, set()
                
                # Fast positional reader
                for line in f:
                    parts = line.split(',')
                    if len(parts) > max(type_idx, region_idx):
                        try:
                            type_id = int(parts[type_idx])
                            region_id = int(parts[region_idx])
                            if type_id and region_id:
                                pairs_found.add((type_id, region_id))
                        except (ValueError, IndexError):
                            continue
        
        except Exception as e:
            print(f"[Index] Error parsing {file_path}: {e}")
            return file_date, set()
        
        return file_date, pairs_found
    
    def build_index_for_file(self, file_path: Path) -> int:
        """Build index of type_ids present in an archive file.
        
        Legacy single-file method. For bulk indexing, use build_full_index().
        """
        file_date, pairs_found = self._parse_file_for_index(file_path)
        
        if not file_date or not pairs_found:
            return 0
        
        conn = sqlite3.connect(self.index_db_path)
        cursor = conn.cursor()
        
        # Batch insert
        rows = [(type_id, file_date, region_id) for type_id, region_id in pairs_found]
        cursor.executemany(
            "INSERT OR IGNORE INTO archive_index (type_id, file_date, region_id) VALUES (?, ?, ?)",
            rows
        )
        
        conn.commit()
        conn.close()
        
        return len(pairs_found)
    
    def get_files_for_item(self, type_id: int, region_id: int) -> List[str]:
        """Get list of file dates that contain data for an item."""
        conn = sqlite3.connect(self.index_db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT file_date FROM archive_index
            WHERE type_id = ? AND region_id = ?
            ORDER BY file_date
        """, (type_id, region_id))
        
        dates = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        return dates
    
    def _get_indexed_dates(self) -> set:
        """Get set of file_dates already in the index."""
        conn = sqlite3.connect(self.index_db_path)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='archive_index'")
        if not cursor.fetchone():
            conn.close()
            return set()
        
        cursor.execute("SELECT DISTINCT file_date FROM archive_index")
        dates = {row[0] for row in cursor.fetchall()}
        conn.close()
        return dates
    
    def build_full_index(
        self,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        incremental: bool = True
    ) -> int:
        """Build index for all archive files.
        
        Optimized version using:
        - Single DB connection
        - Batch inserts with executemany
        - Multi-threaded file parsing
        - Incremental mode (skip already-indexed files)
        
        Args:
            progress_callback: Optional callback(status, files_done, files_total)
            incremental: If True, skip files already in index. If False, rebuild from scratch.
            
        Returns:
            Number of unique type_ids indexed
        """
        import time
        import re
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        start_time = time.time()
        print(f"[Index] Starting {'incremental' if incremental else 'full'} index build...")
        
        # Get list of files to process
        current_year = date.today().year
        years = [current_year - i for i in range(MAX_YEARS_BACK + 1)]
        all_files = self._get_archive_files(years)
        
        if not all_files:
            print(f"[Index] No archive files found")
            return 0
        
        # Open single connection for entire operation
        conn = sqlite3.connect(self.index_db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        cursor = conn.cursor()
        
        # Ensure table exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS archive_index (
                type_id INTEGER,
                file_date TEXT,
                region_id INTEGER,
                PRIMARY KEY (type_id, file_date, region_id)
            )
        """)
        conn.commit()
        
        if incremental:
            # Get already-indexed dates
            indexed_dates = self._get_indexed_dates()
            
            # Filter to only unindexed files
            files_to_process = []
            for f in all_files:
                match = re.search(r"market-history-(\d{4}-\d{2}-\d{2})", f.name)
                if match and match.group(1) not in indexed_dates:
                    files_to_process.append(f)
            
            print(f"[Index] {len(all_files)} total files, {len(files_to_process)} need indexing")
        else:
            # Full rebuild - clear existing index
            cursor.execute("DELETE FROM archive_index")
            conn.commit()
            files_to_process = all_files
            print(f"[Index] Rebuilding index for {len(files_to_process)} files")
        
        if not files_to_process:
            cursor.execute("SELECT COUNT(DISTINCT type_id) FROM archive_index")
            unique_count = cursor.fetchone()[0]
            conn.close()
            print(f"[Index] Index already complete: {unique_count} unique type_ids")
            return unique_count
        
        total_files = len(files_to_process)
        files_done = 0
        total_pairs = 0
        batch_rows = []
        batch_size = 50000  # Commit every N rows
        
        # Use thread pool for parallel file parsing
        # Limit workers to avoid memory pressure from too many open files
        max_workers = min(4, (total_files + 9) // 10)  # At least 1, max 4
        
        if progress_callback:
            progress_callback("Parsing archive files...", 0, total_files)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all parsing tasks
            future_to_file = {
                executor.submit(self._parse_file_for_index, f): f 
                for f in files_to_process
            }
            
            for future in as_completed(future_to_file):
                file_path = future_to_file[future]
                files_done += 1
                
                try:
                    file_date, pairs = future.result()
                    
                    if file_date and pairs:
                        # Add to batch
                        for type_id, region_id in pairs:
                            batch_rows.append((type_id, file_date, region_id))
                        total_pairs += len(pairs)
                        
                        # Commit batch if large enough
                        if len(batch_rows) >= batch_size:
                            cursor.executemany(
                                "INSERT OR IGNORE INTO archive_index (type_id, file_date, region_id) VALUES (?, ?, ?)",
                                batch_rows
                            )
                            conn.commit()
                            batch_rows = []
                    
                except Exception as e:
                    print(f"[Index] Error processing {file_path.name}: {e}")
                
                # Progress update every 10 files
                if files_done % 10 == 0 or files_done == total_files:
                    elapsed = time.time() - start_time
                    rate = files_done / elapsed if elapsed > 0 else 0
                    remaining = (total_files - files_done) / rate if rate > 0 else 0
                    
                    print(f"[Index] Progress: {files_done}/{total_files} files ({total_pairs:,} pairs) - {remaining:.0f}s remaining")
                    
                    if progress_callback:
                        progress_callback(
                            f"Indexing... {files_done}/{total_files}",
                            files_done,
                            total_files
                        )
        
        # Final batch commit
        if batch_rows:
            cursor.executemany(
                "INSERT OR IGNORE INTO archive_index (type_id, file_date, region_id) VALUES (?, ?, ?)",
                batch_rows
            )
            conn.commit()
        
        # Get final count
        cursor.execute("SELECT COUNT(DISTINCT type_id) FROM archive_index")
        unique_count = cursor.fetchone()[0]
        
        conn.close()
        
        elapsed = time.time() - start_time
        print(f"[Index] Complete: {unique_count} unique type_ids from {total_pairs:,} pairs in {elapsed:.1f}s")
        
        if progress_callback:
            progress_callback("Index complete", total_files, total_files)
        
        return unique_count
    
    def has_index(self) -> bool:
        """Check if index has been built."""
        conn = sqlite3.connect(self.index_db_path)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='archive_index'")
        if not cursor.fetchone():
            conn.close()
            return False
        
        cursor.execute("SELECT COUNT(*) FROM archive_index")
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    
    def get_index_stats(self) -> dict:
        """Get statistics about the index."""
        conn = sqlite3.connect(self.index_db_path)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='archive_index'")
        if not cursor.fetchone():
            conn.close()
            return {
                "type_count": 0,
                "file_count": 0,
                "total_entries": 0,
            }
        
        cursor.execute("SELECT COUNT(DISTINCT type_id) FROM archive_index")
        type_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(DISTINCT file_date) FROM archive_index")
        file_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM archive_index")
        total_entries = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "type_count": type_count,
            "file_count": file_count,
            "total_entries": total_entries,
        }
    
    # =========================================================================
    # Batch Extraction (CSV-based)
    # =========================================================================
    
    def extract_items_batch(
        self,
        type_ids: List[int],
        region_id: int,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None
    ) -> tuple:
        """Extract historical data for multiple items with batched DB writes.
        
        Args:
            type_ids: List of item type IDs to extract
            region_id: Region ID
            progress_callback: Optional callback(status_msg, items_done, items_total)
            cancel_check: Optional callable that returns True if cancelled
            
        Returns:
            (success_count, fail_count)
        """
        import time
        
        current_year = date.today().year
        years_to_scan = [current_year - i for i in range(MAX_YEARS_BACK + 1)]
        
        total_items = len(type_ids)
        success_count = 0
        fail_count = 0
        
        print(f"[Profile Batch] Starting batch extraction: {total_items} items")
        start_time = time.time()
        
        all_yearly_stats = []
        all_profiles = []
        
        for item_idx, type_id in enumerate(type_ids):
            if cancel_check and cancel_check():
                print(f"[Profile Batch] Cancelled at item {item_idx}/{total_items}")
                break
            
            if progress_callback:
                progress_callback(f"Extracting {type_id}", item_idx, total_items)
            
            if item_idx > 0 and item_idx % 10 == 0:
                elapsed = time.time() - start_time
                rate = item_idx / elapsed
                remaining = (total_items - item_idx) / rate if rate > 0 else 0
                print(f"[Profile Batch] Progress: {item_idx}/{total_items} - ~{remaining:.0f}s remaining")
            
            # Get files for this item
            indexed_dates = self.get_files_for_item(type_id, region_id)
            
            if indexed_dates:
                files_to_scan = []
                for file_date in indexed_dates:
                    clean_date = file_date.replace(".csv", "")
                    year = int(clean_date[:4])
                    if year in years_to_scan:
                        csv_path = self.archive_path / str(year) / f"market-history-{clean_date}.csv"
                        bz2_path = self.archive_path / str(year) / f"market-history-{clean_date}.csv.bz2"
                        
                        if csv_path.exists():
                            files_to_scan.append(csv_path)
                        elif bz2_path.exists():
                            files_to_scan.append(bz2_path)
            else:
                files_to_scan = self._get_archive_files(years_to_scan)
            
            if not files_to_scan:
                fail_count += 1
                continue
            
            # Extract data from files
            yearly_data = {year: [] for year in years_to_scan}
            
            for file_path in files_to_scan:
                records = self._extract_from_file(file_path, type_id, region_id)
                for record in records:
                    year = int(record["date"][:4])
                    if year in yearly_data:
                        yearly_data[year].append(record)
            
            # Calculate yearly stats
            item_stats = []
            for year, records in yearly_data.items():
                if records:
                    stats = self._calculate_yearly_stats(year, records)
                    item_stats.append((type_id, region_id, stats))
            
            if not item_stats:
                fail_count += 1
                continue
            
            all_yearly_stats.extend(item_stats)
            
            profile = self._calculate_weighted_profile_from_stats(
                type_id, region_id, [s[2] for s in item_stats]
            )
            
            if profile:
                all_profiles.append(profile)
                success_count += 1
            else:
                fail_count += 1
            
            # Checkpoint every 50 items
            if len(all_profiles) >= 50:
                print(f"[Profile Batch] Saving checkpoint: {len(all_profiles)} profiles")
                self._save_yearly_stats_batch(all_yearly_stats)
                self._save_computed_profiles_batch(all_profiles)
                all_yearly_stats = []
                all_profiles = []
        
        # Final save
        if all_yearly_stats or all_profiles:
            print(f"[Profile Batch] Saving final batch: {len(all_profiles)} profiles")
            self._save_yearly_stats_batch(all_yearly_stats)
            self._save_computed_profiles_batch(all_profiles)
        
        elapsed = time.time() - start_time
        print(f"[Profile Batch] Complete: {success_count} success, {fail_count} failed in {elapsed:.1f}s")
        
        return success_count, fail_count
