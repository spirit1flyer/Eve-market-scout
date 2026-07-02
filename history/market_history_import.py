"""Import methods for MarketHistoryDB.

Extracted as mixin to keep market_history.py under 700 lines.

Provides:
- import_file() - single file import
- import_archive() - bulk archive import with auto mode selection
- _bulk_import() - optimized for 100+ files
- _incremental_import() - safe mode for daily updates
"""

import bz2
import re
import sqlite3
from pathlib import Path
from typing import Optional, Callable, List, Set


class MarketHistoryImportMixin:
    """Mixin providing import methods for MarketHistoryDB.
    
    Expects the following attributes on self:
        - db_path: Path to database file
        - _get_conn() -> sqlite3.Connection
    """
    
    def import_file(self, csv_path: Path, 
                    region_filter: Optional[Set[int]] = None,
                    progress_callback: Optional[Callable[[str, int, int], None]] = None) -> int:
        """Import a single CSV file into database.
        
        Handles both .csv and .csv.bz2 files.
        Uses INSERT OR REPLACE for idempotent imports.
        
        Args:
            csv_path: Path to CSV file (or .csv.bz2)
            region_filter: Optional set of region IDs to import. None = all regions.
            progress_callback: Optional callback(status, current, total)
            
        Returns:
            Number of records imported
        """
        import csv
        
        if not csv_path.exists():
            print(f"[MarketHistory] File not found: {csv_path}")
            return 0
        
        # Determine if compressed
        is_compressed = str(csv_path).endswith('.bz2') or not str(csv_path).endswith('.csv')
        
        # Extract date from filename for logging
        match = re.search(r'market-history-(\d{4}-\d{2}-\d{2})', csv_path.name)
        file_date = match.group(1) if match else csv_path.name
        
        if progress_callback:
            progress_callback(f"Importing {file_date}...", 0, 100)
        
        conn = self._get_conn()
        records = 0
        batch = []
        batch_size = 10000
        
        try:
            # Open file (compressed or not)
            if is_compressed:
                f = bz2.open(csv_path, 'rt', encoding='utf-8')
            else:
                f = open(csv_path, 'r', encoding='utf-8')
            
            with f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    try:
                        region_id = int(row['region_id'])
                        
                        # Skip if not in filter
                        if region_filter and region_id not in region_filter:
                            continue
                        
                        batch.append((
                            int(row['type_id']),
                            region_id,
                            row['date'],
                            float(row['average']),
                            float(row['lowest']),
                            float(row['highest']),
                            int(row['volume']),
                            int(row['order_count'])
                        ))
                        
                        # Batch insert
                        if len(batch) >= batch_size:
                            conn.executemany("""
                                INSERT OR REPLACE INTO daily_history 
                                (type_id, region_id, date, average, lowest, highest, volume, order_count)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, batch)
                            records += len(batch)
                            batch = []
                            
                            if progress_callback:
                                progress_callback(f"Importing {file_date}... {records:,} records", 50, 100)
                    
                    except (KeyError, ValueError):
                        continue
                
                # Insert remaining
                if batch:
                    conn.executemany("""
                        INSERT OR REPLACE INTO daily_history 
                        (type_id, region_id, date, average, lowest, highest, volume, order_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, batch)
                    records += len(batch)
                
                conn.commit()
        
        except Exception as e:
            print(f"[MarketHistory] Error importing {csv_path}: {e}")
            conn.rollback()
            return 0
        
        if progress_callback:
            progress_callback(f"Imported {file_date}: {records:,} records", 100, 100)
        
        return records
    
    def import_archive(self, archive_path: Path,
                       progress_callback: Optional[Callable[[str, int, int], None]] = None,
                       years: int = 3,
                       region_filter: Optional[Set[int]] = None) -> int:
        """Import full archive from everef folder structure.
        
        Automatically selects bulk or incremental mode:
        - Bulk mode (100+ files): Speed optimizations, single transaction
        - Incremental mode (<100 files): Safe mode with normal commits
        
        Args:
            archive_path: Root archive folder (e.g., history-archive/)
            progress_callback: Optional callback(status, files_done, files_total)
            years: How many years back to import (default 3)
            region_filter: Optional set of region IDs. None = all regions.
            
        Returns:
            Total records imported
        """
        if not archive_path.exists():
            print(f"[MarketHistory] Archive path not found: {archive_path}")
            return 0
        
        # Collect all files to import
        files_to_import = self._collect_archive_files(archive_path, years)
        
        if not files_to_import:
            print("[MarketHistory] No files found to import")
            return 0
        
        print(f"[MarketHistory] Found {len(files_to_import)} files to import")
        
        # Choose import mode based on file count
        if len(files_to_import) >= 100:
            return self._bulk_import(files_to_import, region_filter, progress_callback)
        else:
            return self._incremental_import(files_to_import, region_filter, progress_callback)
    
    def _collect_archive_files(self, archive_path: Path, years: int) -> List[Path]:
        """Collect all archive files to import."""
        from datetime import date
        
        files_to_import = []
        current_year = date.today().year
        
        for year in range(current_year - years, current_year + 1):
            year_path = archive_path / str(year)
            if not year_path.exists():
                continue
            
            # Prefer .csv files, fall back to .bz2
            date_files = {}
            
            for f in year_path.glob("market-history-*.csv"):
                if f.name.endswith('.bz2'):
                    continue
                match = re.search(r'market-history-(\d{4}-\d{2}-\d{2})', f.name)
                if match:
                    date_files[match.group(1)] = f
            
            # Add bz2 files for dates we don't have csv
            for f in year_path.glob("market-history-*.csv.bz2"):
                match = re.search(r'market-history-(\d{4}-\d{2}-\d{2})', f.name)
                if match and match.group(1) not in date_files:
                    date_files[match.group(1)] = f
            
            files_to_import.extend(sorted(date_files.values(), key=lambda p: p.name))
        
        return files_to_import
    
    def _parse_csv_fast(self, csv_path: Path, region_filter: Optional[Set[int]]) -> List[tuple]:
        """Parse CSV file using fast positional reader.
        
        Returns list of tuples ready for executemany().
        """
        is_compressed = str(csv_path).endswith('.bz2') or not str(csv_path).endswith('.csv')
        
        rows = []
        try:
            if is_compressed:
                f = bz2.open(csv_path, 'rt', encoding='utf-8')
            else:
                f = open(csv_path, 'r', encoding='utf-8')
            
            with f:
                # Read header to find column positions
                header = f.readline().strip().split(',')
                try:
                    type_idx = header.index('type_id')
                    region_idx = header.index('region_id')
                    date_idx = header.index('date')
                    avg_idx = header.index('average')
                    low_idx = header.index('lowest')
                    high_idx = header.index('highest')
                    vol_idx = header.index('volume')
                    order_idx = header.index('order_count')
                except ValueError as e:
                    print(f"[MarketHistory] Missing column in {csv_path.name}: {e}")
                    return []
                
                max_idx = max(type_idx, region_idx, date_idx, avg_idx, low_idx, high_idx, vol_idx, order_idx)
                
                for line in f:
                    parts = line.strip().split(',')
                    if len(parts) <= max_idx:
                        continue
                    
                    try:
                        region_id = int(parts[region_idx])
                        
                        if region_filter and region_id not in region_filter:
                            continue
                        
                        rows.append((
                            int(parts[type_idx]),
                            region_id,
                            parts[date_idx],
                            float(parts[avg_idx]),
                            float(parts[low_idx]),
                            float(parts[high_idx]),
                            int(parts[vol_idx]),
                            int(parts[order_idx])
                        ))
                    except (ValueError, IndexError):
                        continue
        
        except Exception as e:
            print(f"[MarketHistory] Error parsing {csv_path}: {e}")
        
        return rows
    
    def _bulk_import(self, files: List[Path], region_filter: Optional[Set[int]],
                     progress_callback: Optional[Callable]) -> int:
        """Bulk import mode - optimized for 100+ files.
        
        Uses aggressive SQLite optimizations:
        - synchronous = OFF
        - journal_mode = MEMORY  
        - Drop indexes during import
        - Single transaction for all files
        - Large cache size
        """
        import time
        start_time = time.time()
        
        print(f"[MarketHistory] BULK IMPORT MODE: {len(files)} files")
        
        if progress_callback:
            progress_callback("Preparing bulk import...", 0, len(files))
        
        # Close any existing singleton connection to avoid lock conflicts
        # Call the parent's close method which handles thread-local cleanup
        self.close()
        
        # Create fresh connection with speed optimizations
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Speed optimizations (DANGEROUS - no crash recovery)
        cursor.execute("PRAGMA synchronous = OFF")
        cursor.execute("PRAGMA journal_mode = MEMORY")
        cursor.execute("PRAGMA cache_size = -1048576")  # 1GB cache
        cursor.execute("PRAGMA temp_store = MEMORY")
        cursor.execute("PRAGMA mmap_size = 268435456")  # 256MB mmap hint
        
        # Ensure table exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_history (
                type_id INTEGER NOT NULL,
                region_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                average REAL NOT NULL,
                lowest REAL NOT NULL,
                highest REAL NOT NULL,
                volume INTEGER NOT NULL,
                order_count INTEGER NOT NULL,
                PRIMARY KEY (type_id, region_id, date)
            )
        """)
        
        # Metadata table for tracking imports
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS import_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        # Drop indexes for faster inserts
        cursor.execute("DROP INDEX IF EXISTS idx_region_date")
        cursor.execute("DROP INDEX IF EXISTS idx_type_region")
        conn.commit()
        
        total_records = 0
        
        try:
            # Single transaction for everything
            cursor.execute("BEGIN TRANSACTION")
            
            for i, csv_path in enumerate(files):
                if progress_callback:
                    progress_callback(f"Importing {csv_path.name}...", i, len(files))
                
                rows = self._parse_csv_fast(csv_path, region_filter)
                
                if rows:
                    cursor.executemany("""
                        INSERT OR REPLACE INTO daily_history 
                        (type_id, region_id, date, average, lowest, highest, volume, order_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, rows)
                    total_records += len(rows)
                
                # Progress logging every 50 files
                if (i + 1) % 50 == 0:
                    elapsed = time.time() - start_time
                    rate = (i + 1) / elapsed
                    remaining = (len(files) - i - 1) / rate if rate > 0 else 0
                    print(f"[MarketHistory] Progress: {i+1}/{len(files)} files, {total_records:,} records, ~{remaining:.0f}s remaining")
            
            # Commit everything at once
            conn.commit()
            
            if progress_callback:
                progress_callback("Rebuilding indexes...", len(files), len(files))
            
            # Recreate indexes
            print("[MarketHistory] Rebuilding indexes...")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_region_date ON daily_history(region_id, date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_type_region ON daily_history(type_id, region_id)")
            conn.commit()
            
        except Exception as e:
            print(f"[MarketHistory] Bulk import error: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
        
        elapsed = time.time() - start_time
        print(f"[MarketHistory] Bulk import complete: {total_records:,} records in {elapsed:.1f}s")
        
        if progress_callback:
            progress_callback(f"Import complete: {total_records:,} records", len(files), len(files))
        
        return total_records
    
    def _incremental_import(self, files: List[Path], region_filter: Optional[Set[int]],
                            progress_callback: Optional[Callable]) -> int:
        """Incremental import mode - safe for small updates.
        
        Uses normal SQLite settings with per-file commits.
        Suitable for daily updates (1-30 files).
        """
        print(f"[MarketHistory] INCREMENTAL IMPORT MODE: {len(files)} files")
        
        if progress_callback:
            progress_callback(f"Importing {len(files)} files...", 0, len(files))
        
        total_records = 0
        
        for i, csv_path in enumerate(files):
            if progress_callback:
                progress_callback(f"Importing {csv_path.name}...", i, len(files))
            
            records = self.import_file(csv_path, region_filter=region_filter)
            total_records += records
        
        if progress_callback:
            progress_callback(f"Import complete: {total_records:,} records", len(files), len(files))
        
        print(f"[MarketHistory] Incremental import complete: {total_records:,} records from {len(files)} files")
        return total_records
