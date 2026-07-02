"""Historical price profiles for EVE Market Scout stock market.

Extracts and stores price profiles from everef archive data.
Calculates weighted percentiles for buy/sell target generation.

Year weighting:
    Current year:  40%
    1 year ago:    30%
    2 years ago:   20%
    3 years ago:   10%
    4+ years ago:  Excluded
"""

import sqlite3
from datetime import datetime, date
from typing import Optional, Dict, List
from dataclasses import dataclass
from pathlib import Path
from statistics import stdev

from core.sound_manager import get_data_dir


# File locations
PROFILES_DB = str(get_data_dir() / "stock_profiles.db")
INDEX_DB = str(get_data_dir() / "archive_index.db")
ARCHIVE_PATH = get_data_dir() / "history-archive"

# Default percentiles for buy/sell targets
DEFAULT_BUY_PERCENTILE = 15   # P15 = buy target (lower = more conservative)
DEFAULT_SELL_PERCENTILE = 90  # P90 = sell target (higher = catches more spikes)

# Year weights for profile calculation
YEAR_WEIGHTS = {
    0: 0.40,  # Current year
    1: 0.30,  # 1 year ago
    2: 0.20,  # 2 years ago
    3: 0.10,  # 3 years ago
}
MAX_YEARS_BACK = 3


@dataclass
class YearlyStats:
    """Statistics for one year of an item's price history."""
    year: int
    p_low: float   # Lower percentile price (buy target)
    p_high: float  # Higher percentile price (sell target)
    avg_price: float
    avg_volume: float
    min_price: float
    max_price: float
    data_points: int  # Number of days with data
    # Store which percentiles were used
    low_pct: int = DEFAULT_BUY_PERCENTILE
    high_pct: int = DEFAULT_SELL_PERCENTILE


@dataclass
class ComputedProfile:
    """Weighted profile used for buy/sell targets."""
    type_id: int
    region_id: int
    weighted_p_low: float   # Buy target (below this is good)
    weighted_p_high: float  # Sell target (above this is good)
    band_width: float  # p_high - p_low, wider = more opportunity
    band_percent: float  # band_width / weighted_p_low * 100
    stability_score: float  # 0-100, higher = more consistent pricing
    avg_daily_volume: float
    years_of_data: int
    last_updated: str
    hist_min: float = 0.0  # Historical minimum price
    hist_max: float = 0.0  # Historical maximum price
    # Store which percentiles were used
    low_pct: int = DEFAULT_BUY_PERCENTILE
    high_pct: int = DEFAULT_SELL_PERCENTILE


# Import mixin after dataclasses are defined (it imports from this module)
from analytics.profile_extraction import ProfileExtractionMixin


class ProfileManager(ProfileExtractionMixin):
    """Manages historical price profiles using SQLite storage."""
    
    def __init__(
        self,
        db_path: str = PROFILES_DB,
        index_db_path: str = INDEX_DB,
        archive_path: Path = ARCHIVE_PATH,
        buy_percentile: int = DEFAULT_BUY_PERCENTILE,
        sell_percentile: int = DEFAULT_SELL_PERCENTILE
    ):
        self.db_path = db_path
        self.index_db_path = index_db_path
        self.archive_path = archive_path
        self.buy_percentile = buy_percentile
        self.sell_percentile = sell_percentile
        self._ensure_dirs()
        self._init_db()
        self._init_index_db()
        
        # Index cache: {type_id: [list of file dates with data]}
        self._index: Dict[int, List[str]] = {}
        self._index_loaded = False
    
    def set_percentiles(self, buy_pct: int, sell_pct: int):
        """Update percentile settings."""
        self.buy_percentile = buy_pct
        self.sell_percentile = sell_pct
    
    def _ensure_dirs(self):
        """Create directories if needed."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.index_db_path).parent.mkdir(parents=True, exist_ok=True)
        self.archive_path.mkdir(parents=True, exist_ok=True)
    
    def _init_db(self):
        """Initialize SQLite database with profile tables."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Yearly statistics per item+region
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS yearly_stats (
                type_id INTEGER,
                region_id INTEGER,
                year INTEGER,
                p_low REAL,
                p_high REAL,
                avg_price REAL,
                avg_volume REAL,
                min_price REAL,
                max_price REAL,
                data_points INTEGER,
                low_pct INTEGER DEFAULT 15,
                high_pct INTEGER DEFAULT 90,
                PRIMARY KEY (type_id, region_id, year)
            )
        """)
        
        # Computed weighted profiles
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS computed_profiles (
                type_id INTEGER,
                region_id INTEGER,
                weighted_p_low REAL,
                weighted_p_high REAL,
                band_width REAL,
                band_percent REAL,
                stability_score REAL,
                avg_daily_volume REAL,
                years_of_data INTEGER,
                last_updated TEXT,
                hist_min REAL DEFAULT 0,
                hist_max REAL DEFAULT 0,
                low_pct INTEGER DEFAULT 15,
                high_pct INTEGER DEFAULT 90,
                PRIMARY KEY (type_id, region_id)
            )
        """)
        
        conn.commit()
        conn.close()
    
    def _init_index_db(self):
        """Initialize separate SQLite database for archive index."""
        conn = sqlite3.connect(self.index_db_path)
        cursor = conn.cursor()
        
        # Index of which files contain which type_ids
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS archive_index (
                type_id INTEGER,
                file_date TEXT,
                region_id INTEGER,
                PRIMARY KEY (type_id, file_date, region_id)
            )
        """)
        
        conn.commit()
        conn.close()
        
        # Defer index creation to background (can be slow on large tables)
        import threading
        def create_indexes():
            try:
                conn = sqlite3.connect(self.index_db_path)
                cursor = conn.cursor()
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_archive_file_date ON archive_index(file_date)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_archive_type_id ON archive_index(type_id)")
                conn.commit()
                conn.close()
            except Exception:
                pass  # Non-critical
        threading.Thread(target=create_indexes, daemon=True).start()
    
    # =========================================================================
    # Row Mapping Helpers
    # =========================================================================
    
    def _row_to_profile(self, row: tuple) -> ComputedProfile:
        """Convert a database row to ComputedProfile."""
        return ComputedProfile(
            type_id=row[0],
            region_id=row[1],
            weighted_p_low=row[2],
            weighted_p_high=row[3],
            band_width=row[4],
            band_percent=row[5],
            stability_score=row[6],
            avg_daily_volume=row[7],
            years_of_data=row[8],
            last_updated=row[9],
            hist_min=row[10] if row[10] else 0.0,
            hist_max=row[11] if row[11] else 0.0,
            low_pct=row[12] if len(row) > 12 and row[12] else DEFAULT_BUY_PERCENTILE,
            high_pct=row[13] if len(row) > 13 and row[13] else DEFAULT_SELL_PERCENTILE,
        )
    
    def _row_to_yearly_stats(self, row: tuple) -> YearlyStats:
        """Convert a database row to YearlyStats."""
        return YearlyStats(
            year=row[0],
            p_low=row[1],
            p_high=row[2],
            avg_price=row[3],
            avg_volume=row[4],
            min_price=row[5],
            max_price=row[6],
            data_points=row[7],
            low_pct=row[8] if len(row) > 8 and row[8] else DEFAULT_BUY_PERCENTILE,
            high_pct=row[9] if len(row) > 9 and row[9] else DEFAULT_SELL_PERCENTILE,
        )
    
    def _empty_yearly_stats(self, year: int) -> YearlyStats:
        """Create empty YearlyStats for a year with no data."""
        return YearlyStats(
            year=year, p_low=0, p_high=0, avg_price=0, avg_volume=0,
            min_price=0, max_price=0, data_points=0,
            low_pct=self.buy_percentile, high_pct=self.sell_percentile
        )
    
    # =========================================================================
    # Profile Retrieval
    # =========================================================================
    
    def has_profile(self, type_id: int, region_id: int) -> bool:
        """Check if we have a computed profile for an item."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM computed_profiles WHERE type_id = ? AND region_id = ?",
            (type_id, region_id)
        )
        result = cursor.fetchone() is not None
        conn.close()
        return result
    
    def get_computed_profile(self, type_id: int, region_id: int) -> Optional[ComputedProfile]:
        """Get the weighted profile for an item."""
        import time
        
        for attempt in range(1, 4):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT type_id, region_id, weighted_p_low, weighted_p_high, band_width,
                           band_percent, stability_score, avg_daily_volume, years_of_data,
                           last_updated, hist_min, hist_max, low_pct, high_pct
                    FROM computed_profiles
                    WHERE type_id = ? AND region_id = ?
                """, (type_id, region_id))
                
                row = cursor.fetchone()
                conn.close()
                
                if not row:
                    return None
                return self._row_to_profile(row)
            except (KeyboardInterrupt, sqlite3.OperationalError) as e:
                print(f"[Profiles] get_computed_profile({type_id}, {region_id}) attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    print(f"[Profiles] Retrying in 30 seconds...")
                    time.sleep(30)
                else:
                    print(f"[Profiles] get_computed_profile failed after 3 attempts - returning None")
                    return None
        return None
    
    def get_yearly_stats(self, type_id: int, region_id: int) -> Dict[int, YearlyStats]:
        """Get per-year statistics for an item."""
        import time
        
        for attempt in range(1, 4):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT year, p_low, p_high, avg_price, avg_volume, min_price, max_price,
                           data_points, low_pct, high_pct
                    FROM yearly_stats
                    WHERE type_id = ? AND region_id = ?
                    ORDER BY year DESC
                """, (type_id, region_id))
                
                results = {}
                for row in cursor.fetchall():
                    results[row[0]] = self._row_to_yearly_stats(row)
                
                conn.close()
                return results
            except (KeyboardInterrupt, sqlite3.OperationalError) as e:
                print(f"[Profiles] get_yearly_stats({type_id}, {region_id}) attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    print(f"[Profiles] Retrying in 30 seconds...")
                    time.sleep(30)
                else:
                    print(f"[Profiles] get_yearly_stats failed after 3 attempts - returning empty")
                    return {}
        return {}
    
    def get_all_yearly_stats_for_region(
        self, region_id: int, context_label: str = ""
    ) -> Dict[int, Dict[int, YearlyStats]]:
        """Batched fetch of yearly stats for every item in a region.
        
        Replaces N per-item calls to get_yearly_stats() with a single
        SQL query.  Used by refresh_display_async() and the material
        filter, which previously opened thousands of short-lived SQLite
        connections per hub — the dominant cost when 5 hubs refresh
        concurrently.
        
        Args:
            region_id: Region to fetch stats for.
            context_label: Optional caller identifier (typically hub_key)
                for log tagging so concurrent hub runs are
                distinguishable in interleaved console output.
            
        Returns:
            Nested dict shaped like:
                { type_id: { year: YearlyStats } }
            Items with no stats simply won't appear in the outer dict;
            callers should use `.get(type_id, {})`.
        """
        import time

        tag = f"[Profiles{':' + context_label if context_label else ''}]"

        for attempt in range(1, 4):
            try:
                conn = sqlite3.connect(self.db_path, timeout=30.0)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT type_id, year, p_low, p_high, avg_price, avg_volume,
                           min_price, max_price, data_points, low_pct, high_pct
                    FROM yearly_stats
                    WHERE region_id = ?
                    ORDER BY type_id, year DESC
                """, (region_id,))
                
                results: Dict[int, Dict[int, YearlyStats]] = {}
                for row in cursor.fetchall():
                    type_id = row[0]
                    # Slice off type_id so _row_to_yearly_stats sees the
                    # same shape it does in the per-item path
                    stats_row = row[1:]
                    if type_id not in results:
                        results[type_id] = {}
                    results[type_id][stats_row[0]] = self._row_to_yearly_stats(stats_row)
                
                conn.close()
                print(f"{tag} get_all_yearly_stats_for_region({region_id}): "
                      f"{len(results)} items loaded")
                return results
            except (KeyboardInterrupt, sqlite3.OperationalError) as e:
                print(f"{tag} get_all_yearly_stats_for_region({region_id}) "
                      f"attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    print(f"{tag} Retrying in 30 seconds...")
                    time.sleep(30)
                else:
                    print(f"{tag} get_all_yearly_stats_for_region failed "
                          f"after 3 attempts - returning empty")
                    return {}
        return {}
    
    def get_all_profiles(self) -> List[ComputedProfile]:
        """Get all computed profiles."""
        import time
        
        for attempt in range(1, 4):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT type_id, region_id, weighted_p_low, weighted_p_high, band_width,
                           band_percent, stability_score, avg_daily_volume, years_of_data,
                           last_updated, hist_min, hist_max, low_pct, high_pct
                    FROM computed_profiles
                """)
                
                results = [self._row_to_profile(row) for row in cursor.fetchall()]
                conn.close()
                return results
            except (KeyboardInterrupt, sqlite3.OperationalError) as e:
                print(f"[Profiles] get_all_profiles attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    print(f"[Profiles] Retrying in 30 seconds...")
                    time.sleep(30)
                else:
                    print(f"[Profiles] get_all_profiles failed after 3 attempts - returning empty")
                    return []
        return []

    def clear_region_profiles(self, region_id: int) -> int:
        """Delete all profiles for a region. Returns count of computed_profiles deleted."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM computed_profiles WHERE region_id = ?", (region_id,))
        count = cursor.rowcount
        cursor.execute("DELETE FROM yearly_stats WHERE region_id = ?", (region_id,))
        conn.commit()
        conn.close()
        return count

    def get_profiles_for_region(self, region_id: int) -> List[ComputedProfile]:
        """Get all computed profiles for a specific region.
        
        Args:
            region_id: Region ID to filter by
            
        Returns:
            List of ComputedProfile for that region
        """
        import time
        
        for attempt in range(1, 4):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT type_id, region_id, weighted_p_low, weighted_p_high, band_width,
                           band_percent, stability_score, avg_daily_volume, years_of_data,
                           last_updated, hist_min, hist_max, low_pct, high_pct
                    FROM computed_profiles
                    WHERE region_id = ?
                """, (region_id,))
                
                results = [self._row_to_profile(row) for row in cursor.fetchall()]
                conn.close()
                return results
            except (KeyboardInterrupt, sqlite3.OperationalError) as e:
                print(f"[Profiles] get_profiles_for_region({region_id}) attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    print(f"[Profiles] Retrying in 30 seconds...")
                    time.sleep(30)
                else:
                    print(f"[Profiles] get_profiles_for_region failed after 3 attempts - returning empty")
                    return []
        return []
    
    # =========================================================================
    # Profile Calculation
    # =========================================================================
    
    def _calculate_weighted_profile(self, type_id: int, region_id: int) -> Optional[ComputedProfile]:
        """Calculate weighted profile from yearly stats."""
        yearly_stats = self.get_yearly_stats(type_id, region_id)
        
        if not yearly_stats:
            return None
        
        current_year = date.today().year
        
        # Calculate weighted averages
        weighted_p_low = 0.0
        weighted_p_high = 0.0
        weighted_volume = 0.0
        total_weight = 0.0
        
        # Collect p_low/p_high values for stability calculation
        p_low_values = []
        p_high_values = []
        
        for years_ago, weight in YEAR_WEIGHTS.items():
            year = current_year - years_ago
            stats = yearly_stats.get(year)
            
            if stats and stats.data_points > 0:
                weighted_p_low += stats.p_low * weight
                weighted_p_high += stats.p_high * weight
                weighted_volume += stats.avg_volume * weight
                total_weight += weight
                
                p_low_values.append(stats.p_low)
                p_high_values.append(stats.p_high)
        
        if total_weight == 0:
            return None
        
        # Normalize by actual weight used
        weighted_p_low /= total_weight
        weighted_p_high /= total_weight
        weighted_volume /= total_weight
        
        # Calculate band metrics
        band_width = weighted_p_high - weighted_p_low
        band_percent = (band_width / weighted_p_low * 100) if weighted_p_low > 0 else 0
        
        # Calculate stability score (lower variance = more stable)
        stability = self._calculate_stability(p_low_values, p_high_values)
        
        # Calculate overall historic min/max across all years
        hist_min = float('inf')
        hist_max = 0.0
        for stats in yearly_stats.values():
            if stats.min_price > 0 and stats.min_price < hist_min:
                hist_min = stats.min_price
            if stats.max_price > hist_max:
                hist_max = stats.max_price
        
        if hist_min == float('inf'):
            hist_min = 0.0
        
        return ComputedProfile(
            type_id=type_id,
            region_id=region_id,
            weighted_p_low=weighted_p_low,
            weighted_p_high=weighted_p_high,
            band_width=band_width,
            band_percent=band_percent,
            stability_score=stability,
            avg_daily_volume=weighted_volume,
            years_of_data=len(yearly_stats),
            last_updated=datetime.now().isoformat(),
            hist_min=hist_min,
            hist_max=hist_max,
            low_pct=self.buy_percentile,
            high_pct=self.sell_percentile,
        )
    
    def _calculate_stability(self, p_low_values: List[float], p_high_values: List[float]) -> float:
        """Calculate stability score from year-over-year variance.
        
        Returns 0-100 where 100 is most stable (low variance).
        """
        if len(p_low_values) < 2:
            return 50.0  # Not enough data, neutral score
        
        try:
            # Calculate coefficient of variation for both percentiles
            p_low_mean = sum(p_low_values) / len(p_low_values)
            p_high_mean = sum(p_high_values) / len(p_high_values)
            
            if p_low_mean == 0 or p_high_mean == 0:
                return 50.0
            
            p_low_cv = stdev(p_low_values) / p_low_mean
            p_high_cv = stdev(p_high_values) / p_high_mean
            
            # Average CV, then convert to 0-100 score
            # CV of 0 = 100 score, CV of 1.0 = 0 score
            avg_cv = (p_low_cv + p_high_cv) / 2
            score = max(0, min(100, 100 * (1 - avg_cv)))
            
            return round(score, 1)
        
        except Exception:
            return 50.0
    
    # =========================================================================
    # Profile Storage
    # =========================================================================
    
    def _save_yearly_stats(self, type_id: int, region_id: int, stats: YearlyStats):
        """Save yearly stats to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO yearly_stats
            (type_id, region_id, year, p_low, p_high, avg_price, avg_volume,
             min_price, max_price, data_points, low_pct, high_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            type_id, region_id, stats.year, stats.p_low, stats.p_high,
            stats.avg_price, stats.avg_volume, stats.min_price,
            stats.max_price, stats.data_points, stats.low_pct, stats.high_pct
        ))
        
        conn.commit()
        conn.close()
    
    def _save_yearly_stats_batch(self, stats_list: List[tuple]):
        """Save multiple yearly stats in a single transaction.
        
        Args:
            stats_list: List of (type_id, region_id, YearlyStats) tuples
        """
        if not stats_list:
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for type_id, region_id, stats in stats_list:
            cursor.execute("""
                INSERT OR REPLACE INTO yearly_stats
                (type_id, region_id, year, p_low, p_high, avg_price, avg_volume,
                 min_price, max_price, data_points, low_pct, high_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                type_id, region_id, stats.year, stats.p_low, stats.p_high,
                stats.avg_price, stats.avg_volume, stats.min_price,
                stats.max_price, stats.data_points, stats.low_pct, stats.high_pct
            ))
        
        conn.commit()
        conn.close()
    
    def _save_computed_profile(self, profile: ComputedProfile):
        """Save computed profile to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO computed_profiles
            (type_id, region_id, weighted_p_low, weighted_p_high, band_width,
             band_percent, stability_score, avg_daily_volume, years_of_data,
             last_updated, hist_min, hist_max, low_pct, high_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            profile.type_id, profile.region_id, profile.weighted_p_low,
            profile.weighted_p_high, profile.band_width, profile.band_percent,
            profile.stability_score, profile.avg_daily_volume,
            profile.years_of_data, profile.last_updated,
            profile.hist_min, profile.hist_max,
            profile.low_pct, profile.high_pct
        ))
        
        conn.commit()
        conn.close()
    
    def _save_computed_profiles_batch(self, profiles: List[ComputedProfile]):
        """Save multiple computed profiles in a single transaction."""
        if not profiles:
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for profile in profiles:
            cursor.execute("""
                INSERT OR REPLACE INTO computed_profiles
                (type_id, region_id, weighted_p_low, weighted_p_high, band_width,
                 band_percent, stability_score, avg_daily_volume, years_of_data,
                 last_updated, hist_min, hist_max, low_pct, high_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                profile.type_id, profile.region_id, profile.weighted_p_low,
                profile.weighted_p_high, profile.band_width, profile.band_percent,
                profile.stability_score, profile.avg_daily_volume,
                profile.years_of_data, profile.last_updated,
                profile.hist_min, profile.hist_max,
                profile.low_pct, profile.high_pct
            ))
        
        conn.commit()
        conn.close()
    
    def delete_profile(self, type_id: int, region_id: int):
        """Delete all data for an item (yearly stats and computed profile)."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "DELETE FROM yearly_stats WHERE type_id = ? AND region_id = ?",
            (type_id, region_id)
        )
        cursor.execute(
            "DELETE FROM computed_profiles WHERE type_id = ? AND region_id = ?",
            (type_id, region_id)
        )
        
        conn.commit()
        conn.close()
