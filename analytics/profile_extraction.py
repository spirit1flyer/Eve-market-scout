"""Profile extraction mixin - SQLite fast path with CSV fallback.

This module provides the ProfileExtractionMixin class which handles:
- Extracting historical data from market_history.db (fast path)
- Fallback: extracting from everef archive files via ProfileCSVMixin

Used as a mixin by ProfileManager in historical_profiles.py.
"""

from datetime import date
from typing import Optional, Dict, List, Callable
from pathlib import Path

from analytics.historical_profiles import YearlyStats, MAX_YEARS_BACK, YEAR_WEIGHTS, ComputedProfile
from analytics.profile_csv_fallback import ProfileCSVMixin

# Try to import market history database
try:
    from history.market_history import get_market_history_db, MarketHistoryDB
    _HAS_MARKET_HISTORY_DB = True
except ImportError:
    _HAS_MARKET_HISTORY_DB = False


class ProfileExtractionMixin(ProfileCSVMixin):
    """Mixin providing profile extraction for ProfileManager.
    
    Tries SQLite database first (fast), falls back to CSV scanning (slow).
    
    Expects the following attributes on self:
        - db_path: str (profiles database)
        - index_db_path: str (archive index database)
        - archive_path: Path
        - buy_percentile: int
        - sell_percentile: int
    
    Expects the following methods on self:
        - _save_yearly_stats(type_id, region_id, stats)
        - _save_yearly_stats_batch(stats_list)
        - _save_computed_profile(profile)
        - _save_computed_profiles_batch(profiles)
        - _calculate_weighted_profile(type_id, region_id)
    """
    
    # =========================================================================
    # Profile Extraction - SQLite Fast Path
    # =========================================================================
    
    def extract_item_from_db(
        self,
        type_id: int,
        region_id: int,
        market_db: "MarketHistoryDB" = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> bool:
        """Extract historical data for a single item from market_history.db.
        
        This is the FAST path - queries SQLite instead of scanning CSV files.
        Typically completes in <100ms vs 30+ seconds for CSV scanning.
        
        Args:
            type_id: Item type ID
            region_id: Region ID
            market_db: MarketHistoryDB instance (or None to get singleton)
            progress_callback: Optional callback(status_msg, current, total)
            
        Returns:
            True if extraction successful with data found.
        """
        print(f"[Profile-DB] Extracting type_id={type_id}, region_id={region_id}")
        
        if market_db is None:
            if not _HAS_MARKET_HISTORY_DB:
                print(f"[Profile-DB] market_history module not available")
                return False
            market_db = get_market_history_db()
        
        if progress_callback:
            progress_callback("Querying database...", 0, 100)
        
        # Get full history from SQLite
        history = market_db.get_full_history(region_id, type_id, years=MAX_YEARS_BACK + 1)
        
        if not history:
            print(f"[Profile-DB] No history found for type_id={type_id}")
            return False
        
        print(f"[Profile-DB] Found {len(history)} records")
        
        if progress_callback:
            progress_callback("Calculating stats...", 50, 100)
        
        # Group by year
        current_year = date.today().year
        years_to_process = [current_year - i for i in range(MAX_YEARS_BACK + 1)]
        yearly_data: Dict[int, List[dict]] = {year: [] for year in years_to_process}
        
        for record in history:
            year = int(record['date'][:4])
            if year in yearly_data:
                yearly_data[year].append(record)
        
        # Calculate and store yearly stats
        years_with_data = 0
        for year, records in yearly_data.items():
            if records:
                years_with_data += 1
                print(f"[Profile-DB] Year {year}: {len(records)} records")
                stats = self._calculate_yearly_stats(year, records)
                print(f"[Profile-DB]   -> P{self.buy_percentile}={stats.p_low:.2f}, P{self.sell_percentile}={stats.p_high:.2f}")
                self._save_yearly_stats(type_id, region_id, stats)
        
        if years_with_data == 0:
            print(f"[Profile-DB] No data found for type_id={type_id}")
            return False
        
        # Calculate and store weighted profile
        profile = self._calculate_weighted_profile(type_id, region_id)
        if profile:
            self._save_computed_profile(profile)
            print(f"[Profile-DB] SUCCESS - Profile saved")
        else:
            print(f"[Profile-DB] Failed to calculate weighted profile")
            return False
        
        if progress_callback:
            progress_callback("Complete", 100, 100)
        
        return True
    
    def extract_all_from_db(
        self,
        region_id: int,
        type_ids: Optional[List[int]] = None,
        market_db: "MarketHistoryDB" = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None
    ) -> tuple:
        """Extract profiles for multiple items from market_history.db.
        
        FAST batch extraction using SQLite queries.
        
        Args:
            region_id: Region ID to extract
            type_ids: Optional list of type_ids (None = all items in region)
            market_db: MarketHistoryDB instance (or None to get singleton)
            progress_callback: Optional callback(status_msg, items_done, items_total)
            cancel_check: Optional callable that returns True if cancelled
            
        Returns:
            (success_count, fail_count)
        """
        import time
        
        print(f"[Profile-DB Batch] Starting extraction for region {region_id}")
        start_time = time.time()
        
        if market_db is None:
            if not _HAS_MARKET_HISTORY_DB:
                print(f"[Profile-DB Batch] market_history module not available")
                return 0, 0
            market_db = get_market_history_db()
        
        # Get list of items if not provided
        if type_ids is None:
            type_ids = market_db.get_items_in_region(region_id)
            print(f"[Profile-DB Batch] Found {len(type_ids)} items in region")
        
        if not type_ids:
            return 0, 0
        
        total_items = len(type_ids)
        success_count = 0
        fail_count = 0
        
        all_yearly_stats = []
        all_profiles = []
        
        current_year = date.today().year
        years_to_process = [current_year - i for i in range(MAX_YEARS_BACK + 1)]
        
        PRINT_EVERY = max(1, total_items // 10)  # ~10 console milestones

        for idx, type_id in enumerate(type_ids):
            if cancel_check and cancel_check():
                print(f"[Profile-DB Batch] Cancelled at item {idx}/{total_items}")
                break

            if idx > 0 and idx % PRINT_EVERY == 0:
                elapsed = time.time() - start_time
                pct = idx * 100 // total_items
                rate = idx / elapsed if elapsed > 0 else 0
                eta = (total_items - idx) / rate if rate > 0 else 0
                print(f"[Profile-DB Batch] {pct}% — {idx}/{total_items} "
                      f"({success_count} ok, {fail_count} skip) "
                      f"{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining")

            if progress_callback and idx % 100 == 0:
                progress_callback(f"Processing item {idx}/{total_items}", idx, total_items)

            # Get history for this item
            history = market_db.get_full_history(region_id, type_id, years=MAX_YEARS_BACK + 1)
            
            if not history:
                fail_count += 1
                continue
            
            # Group by year
            yearly_data: Dict[int, List[dict]] = {year: [] for year in years_to_process}
            for record in history:
                year = int(record['date'][:4])
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
        if progress_callback:
            progress_callback("Saving to database...", total_items, total_items)
        print(f"[Profile-DB Batch] Saving {len(all_yearly_stats)} yearly-stat rows "
              f"and {len(all_profiles)} profiles to DB...")
        if all_yearly_stats:
            self._save_yearly_stats_batch(all_yearly_stats)
        if all_profiles:
            self._save_computed_profiles_batch(all_profiles)
        
        elapsed = time.time() - start_time
        print(f"[Profile-DB Batch] Complete: {success_count} profiles, {fail_count} failed in {elapsed:.1f}s")
        
        return success_count, fail_count
    
    # =========================================================================
    # Main Entry Points (try SQLite, fallback to CSV)
    # =========================================================================
    
    def extract_item(
        self,
        type_id: int,
        region_id: int,
        progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> bool:
        """Extract historical data for a single item.
        
        Tries SQLite database first (fast), falls back to CSV scanning (slow).
        """
        # Try SQLite fast path first
        if _HAS_MARKET_HISTORY_DB:
            try:
                market_db = get_market_history_db()
                stats = market_db.get_stats()
                if stats.get('row_count', 0) > 0:
                    result = self.extract_item_from_db(type_id, region_id, market_db, progress_callback)
                    if result:
                        return True
                    print(f"[Profile] No data in SQLite, trying CSV fallback...")
            except Exception as e:
                print(f"[Profile] SQLite extraction failed: {e}, trying CSV fallback...")
        
        # Fallback to CSV scanning
        return self._extract_item_from_csv(type_id, region_id, progress_callback)
    
    def extract_all_from_archive(
        self,
        region_id: int,
        type_ids: Optional[List[int]] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None
    ) -> tuple:
        """Extract profiles for all items.
        
        Tries SQLite database first (instant), falls back to CSV file scanning.
        """
        # Try SQLite fast path first
        if _HAS_MARKET_HISTORY_DB:
            try:
                market_db = get_market_history_db()
                stats = market_db.get_stats()
                if stats.get('row_count', 0) > 0:
                    return self.extract_all_from_db(
                        region_id, type_ids, market_db, 
                        progress_callback, cancel_check
                    )
            except Exception as e:
                print(f"[Profile] SQLite batch extraction failed: {e}, trying CSV fallback...")
        
        # Fallback to CSV scanning
        return self._extract_all_from_csv(region_id, type_ids, progress_callback, cancel_check)
    
    # =========================================================================
    # Statistics Calculation (shared by both paths)
    # =========================================================================
    
    def _calculate_yearly_stats(self, year: int, records: List[dict]) -> YearlyStats:
        """Calculate statistics for one year of price data."""
        if not records:
            return self._empty_yearly_stats(year)
        
        prices = [r["average"] for r in records if r.get("average", 0) > 0]
        volumes = [r["volume"] for r in records if r.get("volume", 0) > 0]
        
        if not prices:
            return self._empty_yearly_stats(year)
        
        # Sort prices for percentile calculation
        sorted_prices = sorted(prices)
        n = len(sorted_prices)
        
        # Calculate percentiles
        low_idx = int(n * self.buy_percentile / 100)
        high_idx = int(n * self.sell_percentile / 100)
        
        # Clamp indices
        low_idx = max(0, min(low_idx, n - 1))
        high_idx = max(0, min(high_idx, n - 1))
        
        return YearlyStats(
            year=year,
            p_low=sorted_prices[low_idx],
            p_high=sorted_prices[high_idx],
            avg_price=sum(prices) / len(prices),
            avg_volume=sum(volumes) / len(volumes) if volumes else 0,
            min_price=min(prices),
            max_price=max(prices),
            data_points=len(records),
            low_pct=self.buy_percentile,
            high_pct=self.sell_percentile,
        )
    
    def _calculate_weighted_profile_from_stats(
        self, 
        type_id: int, 
        region_id: int, 
        stats_list: List[YearlyStats]
    ) -> Optional[ComputedProfile]:
        """Calculate weighted profile directly from stats list (no DB read)."""
        from statistics import stdev
        
        if not stats_list:
            return None
        
        current_year = date.today().year
        
        # Calculate weighted averages
        total_weight = 0
        weighted_p_low = 0
        weighted_p_high = 0
        weighted_avg = 0
        weighted_volume = 0
        
        hist_min = float('inf')
        hist_max = 0
        
        for stats in stats_list:
            years_ago = current_year - stats.year
            weight = YEAR_WEIGHTS.get(years_ago, 0)
            
            if weight > 0:
                total_weight += weight
                weighted_p_low += stats.p_low * weight
                weighted_p_high += stats.p_high * weight
                weighted_avg += stats.avg_price * weight
                weighted_volume += stats.avg_volume * weight
            
            hist_min = min(hist_min, stats.min_price)
            hist_max = max(hist_max, stats.max_price)
        
        if total_weight == 0:
            return None
        
        # Normalize
        weighted_p_low /= total_weight
        weighted_p_high /= total_weight
        weighted_avg /= total_weight
        weighted_volume /= total_weight
        
        # Calculate band metrics
        band_width = weighted_p_high - weighted_p_low
        band_percent = (band_width / weighted_p_low * 100) if weighted_p_low > 0 else 0
        
        # Calculate stability score
        try:
            avg_prices = [s.avg_price for s in stats_list if s.avg_price > 0]
            if len(avg_prices) >= 2:
                price_stdev = stdev(avg_prices)
                mean_price = sum(avg_prices) / len(avg_prices)
                cv = (price_stdev / mean_price) if mean_price > 0 else 1
                stability = max(0, min(100, 100 * (1 - cv)))
            else:
                stability = 50
        except Exception:
            stability = 50
        
        if hist_min == float('inf'):
            hist_min = 0
        
        return ComputedProfile(
            type_id=type_id,
            region_id=region_id,
            weighted_p_low=weighted_p_low,
            weighted_p_high=weighted_p_high,
            band_width=band_width,
            band_percent=band_percent,
            stability_score=stability,
            avg_daily_volume=weighted_volume,
            years_of_data=len(stats_list),
            last_updated=date.today().isoformat(),
            hist_min=hist_min,
            hist_max=hist_max,
            low_pct=self.buy_percentile,
            high_pct=self.sell_percentile,
        )
