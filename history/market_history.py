"""Unified market history database for EVE Market Scout.

Stores 3+ years of daily market data from everef archives in SQLite.
Provides fast queries for both Scanner (30-day safety checks) and 
Stock Market (multi-year profiles, trends, signals).

Data flow:
    Everef CSV -> import_file() -> SQLite -> query methods

CSV format (from everef.net):
    region_id,type_id,date,average,highest,lowest,order_count,volume

Database schema:
    daily_history(type_id, region_id, date, average, lowest, highest, volume, order_count)
    Primary key: (type_id, region_id, date)
"""

import bz2
import csv
import sqlite3
import re
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any

from core.sound_manager import get_data_dir


# Database filename
DB_FILENAME = "market_history.db"

# Trade hub regions
REGION_IDS = {
    "the_forge": 10000002,      # Jita
    "domain": 10000043,         # Amarr
    "sinq_laison": 10000032,    # Dodixie
    "metropolis": 10000042,     # Hek
    "heimatar": 10000030,       # Rens
}

REGION_NAMES = {v: k for k, v in REGION_IDS.items()}


from history.market_history_import import MarketHistoryImportMixin


class MarketHistoryDB(MarketHistoryImportMixin):
    """Unified market history database.
    
    Stores daily market data from everef archives.
    Provides O(1) indexed queries for scanner and stock market.
    
    Import methods provided by MarketHistoryImportMixin.
    """
    
    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection.
        
        Args:
            db_path: Path to database file. Defaults to get_data_dir()/market_history.db
        """
        if db_path is None:
            db_path = get_data_dir() / DB_FILENAME
        
        self.db_path = db_path
        # Thread-local storage for connections (SQLite connections can't be shared)
        self._local = threading.local()
        self._exclusive_mode = False
        
        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
    
    # =========================================================================
    # Connection Management
    # =========================================================================
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get or create database connection for current thread.
        
        Uses thread-local storage so each thread gets its own connection.
        """
        # Block new connections during bulk import
        if self._exclusive_mode:
            raise RuntimeError("Database is in exclusive mode for bulk import")
        
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path))
            self._local.conn.row_factory = sqlite3.Row
            # Performance tuning
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        return self._local.conn
    
    def close(self):
        """Close database connection for current thread."""
        if hasattr(self._local, 'conn') and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
    
    def close_all(self):
        """Close all connections and mark DB for exclusive access.
        
        Used before bulk import to ensure no other connections interfere.
        """
        self.close()  # Close current thread's connection
        self._exclusive_mode = True  # Flag to prevent new connections
    
    def end_exclusive(self):
        """End exclusive mode, allow normal connections again."""
        self._exclusive_mode = False
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    # =========================================================================
    # Schema Management
    # =========================================================================
    
    def init_db(self):
        """Create tables and indexes if needed."""
        conn = self._get_conn()
        
        conn.execute("""
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
        
        # Index for region+date queries (scanner batch lookups)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_region_date 
            ON daily_history(region_id, date)
        """)
        
        # Index for type+region queries (item history lookups)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_type_region 
            ON daily_history(type_id, region_id)
        """)
        
        # Index for date-only queries (MIN/MAX date lookups)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_date 
            ON daily_history(date)
        """)
        
        # Metadata table for tracking imports
        conn.execute("""
            CREATE TABLE IF NOT EXISTS import_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        conn.commit()
        print("[MarketHistory] Database initialized")
    
    def is_initialized(self) -> bool:
        """Check if database has been initialized with data."""
        if not self.db_path.exists():
            return False
        
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='daily_history'"
        )
        return cursor.fetchone()[0] > 0

    # =========================================================================
    # Import meta (key/value flags persisted alongside the data)
    # =========================================================================

    def get_meta(self, key: str) -> Optional[str]:
        """Read a value from import_meta. Returns None if key absent or
        the table doesn't exist yet (fresh DB before init_db()).
        """
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT value FROM import_meta WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            # Table missing - DB never initialized
            return None

    def set_meta(self, key: str, value: str) -> bool:
        """Write a value to import_meta. Creates the table on demand
        so callers don't need to worry about init_db() ordering.
        """
        try:
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS import_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute(
                "INSERT OR REPLACE INTO import_meta (key, value) VALUES (?, ?)",
                (key, value)
            )
            conn.commit()
            return True
        except Exception as e:
            print(f"[MarketHistory] set_meta failed for {key}: {e}")
            return False
    
    # =========================================================================
    # Query Methods (Date ranges)
    # =========================================================================
    
    def get_latest_date(self) -> Optional[str]:
        """Get most recent date in database.
        
        Returns:
            Date string 'YYYY-MM-DD' or None if empty
        """
        conn = self._get_conn()
        cursor = conn.execute("SELECT MAX(date) FROM daily_history")
        result = cursor.fetchone()
        return result[0] if result and result[0] else None
    
    def get_earliest_date(self) -> Optional[str]:
        """Get earliest date in database.
        
        Returns:
            Date string 'YYYY-MM-DD' or None if empty
        """
        conn = self._get_conn()
        cursor = conn.execute("SELECT MIN(date) FROM daily_history")
        result = cursor.fetchone()
        return result[0] if result and result[0] else None
    
    def get_imported_dates(self) -> set:
        """Get set of all dates that have been imported.
        
        Returns:
            Set of date strings 'YYYY-MM-DD'
        """
        conn = self._get_conn()
        cursor = conn.execute("SELECT DISTINCT date FROM daily_history")
        return {row[0] for row in cursor.fetchall()}
    
    def get_missing_dates(self, archive_path: Path, years: int = 3) -> List[str]:
        """Compare archive files to database, return missing dates.
        
        Args:
            archive_path: Root archive folder
            years: How many years back to check
            
        Returns:
            List of date strings that exist in archive but not in database
        """
        # Get dates in database
        imported = self.get_imported_dates()
        
        # Get dates in archive
        archive_dates = set()
        current_year = date.today().year
        
        for year in range(current_year - years, current_year + 1):
            year_path = archive_path / str(year)
            if not year_path.exists():
                continue
            
            for f in year_path.iterdir():
                match = re.search(r'market-history-(\d{4}-\d{2}-\d{2})', f.name)
                if match:
                    archive_dates.add(match.group(1))
        
        # Return missing
        missing = sorted(archive_dates - imported)
        return missing
    
    # =========================================================================
    # Query Methods (Scanner)
    # =========================================================================
    
    def get_history(self, region_id: int, type_id: int, 
                    days: int = 30) -> List[Dict[str, Any]]:
        """Get recent history for one item.
        
        Args:
            region_id: Region ID
            type_id: Item type ID
            days: Number of days of history (default 30)
            
        Returns:
            List of daily records, newest first.
            Each record: {date, average, lowest, highest, volume, order_count}
        """
        conn = self._get_conn()
        
        cutoff = (date.today() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        cursor = conn.execute("""
            SELECT date, average, lowest, highest, volume, order_count
            FROM daily_history
            WHERE region_id = ? AND type_id = ? AND date >= ?
            ORDER BY date DESC
        """, (region_id, type_id, cutoff))
        
        return [dict(row) for row in cursor.fetchall()]
    
    def get_history_bulk(self, region_id: int, type_ids: List[int],
                         days: int = 30) -> Dict[int, List[Dict[str, Any]]]:
        """Get recent history for multiple items.
        
        Single query with results grouped by type_id.
        Used by scanner for batch lookups.
        
        Args:
            region_id: Region ID
            type_ids: List of item type IDs
            days: Number of days of history (default 30)
            
        Returns:
            Dict mapping type_id to list of daily records.
            Missing items get empty lists.
        """
        if not type_ids:
            return {}
        
        conn = self._get_conn()
        cutoff = (date.today() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        # Build result dict with empty lists
        result = {tid: [] for tid in type_ids}
        
        # Query with IN clause
        placeholders = ','.join('?' * len(type_ids))
        cursor = conn.execute(f"""
            SELECT type_id, date, average, lowest, highest, volume, order_count
            FROM daily_history
            WHERE region_id = ? AND type_id IN ({placeholders}) AND date >= ?
            ORDER BY type_id, date DESC
        """, [region_id] + list(type_ids) + [cutoff])
        
        for row in cursor.fetchall():
            tid = row['type_id']
            result[tid].append({
                'date': row['date'],
                'average': row['average'],
                'lowest': row['lowest'],
                'highest': row['highest'],
                'volume': row['volume'],
                'order_count': row['order_count']
            })
        
        return result
    
    # =========================================================================
    # Query Methods (Stock Market)
    # =========================================================================
    
    def get_full_history(self, region_id: int, type_id: int,
                         years: int = 3) -> List[Dict[str, Any]]:
        """Get full history for profile calculation.
        
        Args:
            region_id: Region ID
            type_id: Item type ID
            years: Years of history to retrieve
            
        Returns:
            List of daily records, oldest first (for profile calculations).
        """
        conn = self._get_conn()
        
        cutoff = (date.today() - timedelta(days=years * 365)).strftime('%Y-%m-%d')
        
        cursor = conn.execute("""
            SELECT date, average, lowest, highest, volume, order_count
            FROM daily_history
            WHERE region_id = ? AND type_id = ? AND date >= ?
            ORDER BY date ASC
        """, (region_id, type_id, cutoff))
        
        return [dict(row) for row in cursor.fetchall()]

    def get_full_history_bulk(
        self, region_id: int, type_ids: List[int], years: int = 3
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Get full history for multiple items in batched IN-clause queries.

        Replaces N individual get_full_history() calls with a small number
        of queries batched at 500 items each.  Returns dict keyed by
        type_id; items with no history get empty lists.
        """
        if not type_ids:
            return {}
        conn = self._get_conn()
        cutoff = (date.today() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
        result: Dict[int, List[Dict[str, Any]]] = {tid: [] for tid in type_ids}
        BATCH = 500
        for i in range(0, len(type_ids), BATCH):
            batch = type_ids[i : i + BATCH]
            placeholders = ",".join("?" * len(batch))
            cursor = conn.execute(
                f"""
                SELECT type_id, date, average, lowest, highest, volume, order_count
                FROM daily_history
                WHERE region_id = ? AND type_id IN ({placeholders}) AND date >= ?
                ORDER BY type_id, date ASC
                """,
                [region_id] + batch + [cutoff],
            )
            for row in cursor.fetchall():
                tid = row["type_id"]
                if tid in result:
                    result[tid].append(dict(row))
        return result

    def get_yearly_data(self, region_id: int, type_id: int,
                        year: int) -> List[Dict[str, Any]]:
        """Get all records for a specific year.
        
        Args:
            region_id: Region ID
            type_id: Item type ID
            year: Year (e.g., 2024)
            
        Returns:
            List of daily records for that year, oldest first.
        """
        conn = self._get_conn()
        
        start_date = f"{year}-01-01"
        end_date = f"{year}-12-31"
        
        cursor = conn.execute("""
            SELECT date, average, lowest, highest, volume, order_count
            FROM daily_history
            WHERE region_id = ? AND type_id = ? AND date BETWEEN ? AND ?
            ORDER BY date ASC
        """, (region_id, type_id, start_date, end_date))
        
        return [dict(row) for row in cursor.fetchall()]
    
    def get_items_in_region(self, region_id: int) -> List[int]:
        """Get list of all type_ids that have history in a region.
        
        Args:
            region_id: Region ID
            
        Returns:
            List of type_ids
        """
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT DISTINCT type_id FROM daily_history WHERE region_id = ?
        """, (region_id,))
        return [row[0] for row in cursor.fetchall()]
    
    # =========================================================================
    # Statistics / Diagnostics
    # =========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics.
        
        Returns:
            Dict with row_count, date_range, regions, size_mb, etc.
        """
        conn = self._get_conn()
        
        stats = {}
        
        # Row count
        cursor = conn.execute("SELECT COUNT(*) FROM daily_history")
        stats['row_count'] = cursor.fetchone()[0]
        
        # Date range
        stats['earliest_date'] = self.get_earliest_date()
        stats['latest_date'] = self.get_latest_date()
        
        # Distinct dates
        cursor = conn.execute("SELECT COUNT(DISTINCT date) FROM daily_history")
        stats['date_count'] = cursor.fetchone()[0]
        
        # Regions
        cursor = conn.execute("SELECT DISTINCT region_id FROM daily_history")
        stats['regions'] = [row[0] for row in cursor.fetchall()]
        
        # Type count
        cursor = conn.execute("SELECT COUNT(DISTINCT type_id) FROM daily_history")
        stats['type_count'] = cursor.fetchone()[0]
        
        # File size
        if self.db_path.exists():
            stats['size_mb'] = self.db_path.stat().st_size / (1024 * 1024)
        else:
            stats['size_mb'] = 0
        
        return stats
    
    def get_region_stats(self, region_id: int) -> Dict[str, Any]:
        """Get statistics for a specific region.
        
        Args:
            region_id: Region ID
            
        Returns:
            Dict with row_count, type_count, date_range for that region
        """
        conn = self._get_conn()
        
        stats = {'region_id': region_id}
        
        cursor = conn.execute(
            "SELECT COUNT(*) FROM daily_history WHERE region_id = ?",
            (region_id,)
        )
        stats['row_count'] = cursor.fetchone()[0]
        
        cursor = conn.execute(
            "SELECT COUNT(DISTINCT type_id) FROM daily_history WHERE region_id = ?",
            (region_id,)
        )
        stats['type_count'] = cursor.fetchone()[0]
        
        cursor = conn.execute(
            "SELECT MIN(date), MAX(date) FROM daily_history WHERE region_id = ?",
            (region_id,)
        )
        row = cursor.fetchone()
        stats['earliest_date'] = row[0]
        stats['latest_date'] = row[1]
        
        return stats
    
    # =========================================================================
    # Maintenance
    # =========================================================================
    
    def vacuum(self):
        """Compact database after large deletes."""
        conn = self._get_conn()
        conn.execute("VACUUM")
        print("[MarketHistory] Database vacuumed")
    
    def prune_old_data(self, keep_years: int = 3) -> int:
        """Delete data older than keep_years.
        
        Args:
            keep_years: Years of data to keep
            
        Returns:
            Number of rows deleted
        """
        conn = self._get_conn()
        
        cutoff = (date.today() - timedelta(days=keep_years * 365)).strftime('%Y-%m-%d')
        
        cursor = conn.execute(
            "DELETE FROM daily_history WHERE date < ?",
            (cutoff,)
        )
        deleted = cursor.rowcount
        conn.commit()
        
        if deleted > 0:
            print(f"[MarketHistory] Pruned {deleted:,} old records")
        
        return deleted


# =============================================================================
# Module-level singleton
# =============================================================================

_instance: Optional[MarketHistoryDB] = None


def get_market_history_db() -> MarketHistoryDB:
    """Get or create the singleton MarketHistoryDB instance.
    
    Note: Does NOT call init_db() automatically. Migration or bulk import
    will create the table. This avoids opening a connection that would
    conflict with bulk import's exclusive access.
    """
    global _instance
    if _instance is None:
        _instance = MarketHistoryDB()
    return _instance


def close_market_history_db():
    """Close the singleton instance (current thread only)."""
    global _instance
    if _instance is not None:
        _instance.close()
        _instance = None


def close_market_history_db_all():
    """Close current thread's connection and clear singleton.
    
    Called from scan thread at end of scan before database swap.
    Since the scan thread is the one that opened connections during
    the scan, closing here releases the file handles.
    
    Also clears the singleton so the next access creates a fresh
    instance pointing to the (potentially swapped) database file.
    """
    global _instance
    if _instance is not None:
        # Close current thread's connection
        _instance.close()
        
        # Give SQLite a moment to checkpoint WAL
        import time
        time.sleep(0.1)
        
        # Clear singleton so next access creates fresh instance
        _instance = None
