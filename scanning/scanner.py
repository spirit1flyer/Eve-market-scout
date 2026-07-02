"""Market scanning orchestrator for EVE Market Scout.

Gathers market data and dispatches to category processors:
- scanner_steals.py: Fat finger mistakes (same-station only)
- scanner_lowrisk.py: Safe deals with good velocity (same-station)
- scanner_highrisk.py: Profitable but risky deals (same-station)
- scanner_jita.py: Jita-specific processors (no external hub caps)
- scanner_crosshub.py: Cross-hub arbitrage (different stations)
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

from core.config import (
    MIN_PROFIT_PER_UNIT, MIN_TOTAL_PROFIT, SCAM_THRESHOLD,
    JITA_REGION_ID, MIN_MARGIN_PERCENT, MIN_DAILY_VOLUME,
    get_hub_config, DEFAULT_HUB
)
from core.api import ESIClient
from core.calculate import TradingSkills, DEFAULT_SKILLS

from scanning.scanner_common import Candidate, ScanResult, Deal
from scanning.scanner_steals import process_steals
from scanning.scanner_lowrisk import process_low_risk
from scanning.scanner_highrisk import process_high_risk
from scanning.scanner_jita import process_jita_steals, process_jita_low_risk, process_jita_high_risk


# Import cross-hub scanner (may not exist yet)
try:
    from scanning.scanner_crosshub import (
        CrossHubDeal, build_crosshub_candidates, process_crosshub
    )
    CROSSHUB_AVAILABLE = True
except ImportError:
    CROSSHUB_AVAILABLE = False
    CrossHubDeal = None


@dataclass
class CrossHubScanResult:
    """Result of a cross-hub market scan."""
    low_risk: list  # CrossHubDeal list
    high_risk: list  # CrossHubDeal list
    buy_station_orders: list[dict]
    sell_station_orders: list[dict]
    demand_rows: list = None  # scanner_demand.DemandRow list

    def __post_init__(self):
        if self.demand_rows is None:
            self.demand_rows = []


class MarketScanner:
    """Scans market and dispatches to category processors."""

    def __init__(
        self,
        client: ESIClient,
        skills: Optional[TradingSkills] = None,
        hub_key: str = None
    ):
        self.client = client
        self.skills = skills or DEFAULT_SKILLS
        self.hub_key = hub_key or DEFAULT_HUB
        self.hub_config = get_hub_config(self.hub_key)
        
        # For cross-hub: separate skills for buyer character
        self.buyer_skills: Optional[TradingSkills] = None

    def set_skills(self, skills: TradingSkills):
        """Update skills for fee calculations (seller/primary)."""
        self.skills = skills
    
    def set_buyer_skills(self, skills: TradingSkills):
        """Update buyer skills for cross-hub fee calculations."""
        self.buyer_skills = skills

    async def scan(
        self,
        progress_callback=None,
        min_profit_per_unit=None,
        min_total_profit=None,
        max_cost=None,
        min_margin_percent=None,
        min_daily_volume=None,
        refresh_jita: bool = False
    ) -> ScanResult:
        """
        Scan market and categorize deals (same-station mode).
        
        Args:
            progress_callback: Function(status_text, percent) for UI updates
            min_profit_per_unit: Minimum ISK profit per unit
            min_total_profit: Minimum total ISK profit
            max_cost: Maximum total cost (wallet limit)
            min_margin_percent: Minimum margin percentage
            min_daily_volume: Minimum daily volume for Low Risk
            refresh_jita: Force refresh Jita data
        
        Returns:
            ScanResult with steals, low_risk, high_risk lists
        """
        def update(text, pct):
            if progress_callback:
                progress_callback(text, pct)

        # Get filter values with defaults
        profit_threshold = min_profit_per_unit if min_profit_per_unit is not None else MIN_PROFIT_PER_UNIT
        total_threshold = min_total_profit if min_total_profit is not None else MIN_TOTAL_PROFIT
        margin_threshold = min_margin_percent if min_margin_percent is not None else MIN_MARGIN_PERCENT
        volume_threshold = min_daily_volume if min_daily_volume is not None else MIN_DAILY_VOLUME

        # === PHASE 1: Fetch market data ===
        
        hub_name = self.hub_config["name"]
        local_region_id = self.hub_config["region_id"]
        is_jita = (self.hub_key == "jita")
        
        update(f"Fetching {hub_name} orders...", 5)
        local_orders = await self.client.get_orders_for_hub(self.hub_key)
        print(f"{hub_name} orders: {len(local_orders)}, unique types: {len(set(o['type_id'] for o in local_orders))}")

        update("Checking system security...", 15)
        system_ids = list(set(o.get("system_id") for o in local_orders if o.get("system_id")))
        await self.client.build_valid_systems_cache(progress_callback, system_ids)

        update("Filtering to high-sec...", 25)
        valid_systems = set(self.client.valid_systems)
        # Structures are user-chosen hubs; respect their home system regardless
        # of security (player structures commonly live in low/null/WH space).
        if self.hub_config.get("type") == "structure":
            sys_id = self.hub_config.get("system_id")
            if sys_id:
                valid_systems.add(sys_id)
        local_orders_filtered = [o for o in local_orders if o.get("system_id") in valid_systems]

        # Jita orders - when scanning Jita, reuse local orders as reference
        if is_jita:
            jita_orders = local_orders
        else:
            use_jita_cache = not refresh_jita and self.client.has_jita_cache()
            if use_jita_cache:
                update(f"Using cached Jita data ({self.client.get_jita_cache_age()} old)...", 30)
                jita_orders = self.client.jita_orders_cache
            else:
                update("Fetching Jita region orders...", 30)
                jita_orders = await self.client.get_market_orders(JITA_REGION_ID, use_cache=False)

        # === PHASE 2: Process orders into candidates ===
        
        update("Processing orders...", 40)
        local_data = self._process_orders(local_orders_filtered)
        jita_data = self._process_orders(jita_orders)

        update("Building candidates...", 50)
        candidates = self._build_candidates(local_data, jita_data, max_cost, skip_scam_check=is_jita)

        if not candidates:
            update("No candidates found", 100)
            return ScanResult(steals=[], low_risk=[], high_risk=[], local_orders=local_orders, local_orders_filtered=local_orders_filtered)

        print(f"Candidates after first pass: {len(candidates)}")

        # === PHASE 3: Fetch item names and history ===
        
        update("Fetching item names...", 55)
        type_ids = [c.type_id for c in candidates]
        names = await self.client.get_type_names_bulk(type_ids)

        update(f"Fetching history for {len(type_ids)} items...", 60)
        from history.history_source import get_history_for_hub
        jita_task = self.client.get_market_history_bulk(JITA_REGION_ID, type_ids, use_cache=True)
        # local_task dispatches: NPC hub → regional history, structure hub →
        # observed history (once it has ≥7 days of snapshots; regional otherwise).
        local_task = get_history_for_hub(self.client, self.hub_key, type_ids)
        jita_history, local_history = await asyncio.gather(jita_task, local_task)

        # Get reference date from market history db for accurate date filtering
        reference_date = None
        if self.client.market_history:
            reference_date = self.client.market_history.get_latest_date()

        # === PHASE 4: Dispatch to category processors ===
        
        if is_jita:
            # Use Jita-specific processors (no external hub caps)
            update("Processing steals (Jita)...", 75)
            steals = process_jita_steals(
                candidates=candidates,
                names=names,
                system_cache=self.client.system_cache,
                local_history=local_history,
                min_profit_per_unit=profit_threshold,
                min_total_profit=total_threshold,
                min_margin_percent=margin_threshold,
                min_velocity=volume_threshold,
                skills=self.skills,
                reference_date=reference_date
            )
            print(f"Steals found: {len(steals)}")

            update("Processing low risk (Jita)...", 85)
            low_risk = process_jita_low_risk(
                candidates=candidates,
                names=names,
                system_cache=self.client.system_cache,
                local_history=local_history,
                min_profit_per_unit=profit_threshold,
                min_total_profit=total_threshold,
                min_margin_percent=margin_threshold,
                min_velocity=volume_threshold,
                skills=self.skills,
                reference_date=reference_date
            )
            print(f"Low risk found: {len(low_risk)}")

            update("Processing high risk (Jita)...", 95)
            high_risk = process_jita_high_risk(
                candidates=candidates,
                names=names,
                system_cache=self.client.system_cache,
                local_history=local_history,
                min_profit_per_unit=profit_threshold,
                min_total_profit=total_threshold,
                min_margin_percent=margin_threshold,
                min_velocity=volume_threshold,
                skills=self.skills,
                reference_date=reference_date
            )
            print(f"High risk found: {len(high_risk)}")
        else:
            # Use standard processors (with Jita caps)
            update("Processing steals...", 75)
            steals = process_steals(
                candidates=candidates,
                names=names,
                system_cache=self.client.system_cache,
                local_history=local_history,
                jita_history=jita_history,
                min_profit_per_unit=profit_threshold,
                min_total_profit=total_threshold,
                min_margin_percent=margin_threshold,
                min_velocity=volume_threshold,
                skills=self.skills,
                reference_date=reference_date
            )
            print(f"Steals found: {len(steals)}")

            update("Processing low risk...", 85)
            low_risk = process_low_risk(
                candidates=candidates,
                names=names,
                system_cache=self.client.system_cache,
                local_history=local_history,
                jita_history=jita_history,
                min_profit_per_unit=profit_threshold,
                min_total_profit=total_threshold,
                min_margin_percent=margin_threshold,
                min_velocity=volume_threshold,
                skills=self.skills,
                reference_date=reference_date
            )
            print(f"Low risk found: {len(low_risk)}")

            update("Processing high risk...", 95)
            high_risk = process_high_risk(
                candidates=candidates,
                names=names,
                system_cache=self.client.system_cache,
                local_history=local_history,
                jita_history=jita_history,
                min_profit_per_unit=profit_threshold,
                min_total_profit=total_threshold,
                min_margin_percent=margin_threshold,
                min_velocity=volume_threshold,
                skills=self.skills,
                reference_date=reference_date
            )
            print(f"High risk found: {len(high_risk)}")

        update("Complete!", 100)
        
        return ScanResult(
            steals=steals,
            low_risk=low_risk,
            high_risk=high_risk,
            local_orders=local_orders,
            local_orders_filtered=local_orders_filtered
        )

    async def scan_crosshub(
        self,
        buy_station_key: str,
        sell_station_key: str,
        progress_callback=None,
        min_profit_per_unit=None,
        min_total_profit=None,
        max_cost=None,
        min_margin_percent=None,
        min_daily_volume=None,
        min_guaranteed_volume: int = 1,
        refresh_data: bool = False,
        buyer_skills: TradingSkills = None,
        seller_skills: TradingSkills = None
    ) -> CrossHubScanResult:
        """
        Scan for cross-hub arbitrage opportunities.
        
        Buy cheap at buy_station, sell high at sell_station.
        
        Args:
            buy_station_key: Hub key where we buy (e.g., "jita")
            sell_station_key: Hub key where we sell (e.g., "amarr")
            progress_callback: Function(status_text, percent) for UI updates
            min_profit_per_unit: Minimum ISK profit per unit
            min_total_profit: Minimum total ISK profit
            max_cost: Maximum total cost (wallet limit)
            min_margin_percent: Minimum margin percentage
            min_daily_volume: Minimum daily volume
            min_guaranteed_volume: Minimum guaranteed volume from buy orders
            refresh_data: Force refresh market data
        
        Returns:
            CrossHubScanResult with low_risk, high_risk lists
        """
        if not CROSSHUB_AVAILABLE:
            raise RuntimeError("Cross-hub scanner not available")
        
        def update(text, pct):
            if progress_callback:
                progress_callback(text, pct)
        
        # Get filter values with defaults
        profit_threshold = min_profit_per_unit if min_profit_per_unit is not None else MIN_PROFIT_PER_UNIT
        total_threshold = min_total_profit if min_total_profit is not None else MIN_TOTAL_PROFIT
        margin_threshold = min_margin_percent if min_margin_percent is not None else MIN_MARGIN_PERCENT
        volume_threshold = min_daily_volume if min_daily_volume is not None else MIN_DAILY_VOLUME
        
        # Get hub configs
        buy_config = get_hub_config(buy_station_key)
        sell_config = get_hub_config(sell_station_key)
        buy_region_id = buy_config["region_id"]
        sell_region_id = sell_config["region_id"]
        
        # Use passed skills, fall back to instance skills
        buy_skills = buyer_skills or self.buyer_skills or self.skills
        sell_skills = seller_skills or self.skills
        
        # === PHASE 1: Fetch market data ===
        
        update(f"Fetching {buy_config['name']} orders...", 5)
        buy_orders = await self.client.get_orders_for_hub(
            buy_station_key, use_cache=not refresh_data
        )

        update(f"Fetching {sell_config['name']} orders...", 15)
        sell_orders = await self.client.get_orders_for_hub(
            sell_station_key, use_cache=not refresh_data
        )
        
        # === PHASE 2: Check system security ===
        
        update("Checking system security...", 25)
        all_system_ids = list(set(
            o.get("system_id") for o in buy_orders + sell_orders if o.get("system_id")
        ))
        await self.client.build_valid_systems_cache(progress_callback, all_system_ids)
        
        # Filter to high-sec, but never drop a user-chosen structure's home
        # system — most player structures live outside high-sec.
        valid_systems = set(self.client.valid_systems)
        for cfg in (buy_config, sell_config):
            if cfg.get("type") == "structure":
                sys_id = cfg.get("system_id")
                if sys_id:
                    valid_systems.add(sys_id)
        buy_orders_filtered = [o for o in buy_orders if o.get("system_id") in valid_systems]
        sell_orders_filtered = [o for o in sell_orders if o.get("system_id") in valid_systems]
        
        # === PHASE 3: Process orders ===
        
        update("Processing orders...", 35)
        buy_data = self._process_orders_crosshub(buy_orders_filtered)
        sell_data = self._process_orders_crosshub(sell_orders_filtered)
        
        # === PHASE 4: Build candidates ===
        
        update("Finding arbitrage candidates...", 45)
        candidates = build_crosshub_candidates(
            buy_station_data=buy_data,
            sell_station_data=sell_data,
            max_cost=max_cost
        )
        
        if not candidates:
            update("No candidates found", 100)
            return CrossHubScanResult(
                low_risk=[],
                high_risk=[],
                buy_station_orders=buy_orders,
                sell_station_orders=sell_orders
            )
        
        print(f"Cross-hub candidates: {len(candidates)}")
        
        update("Fetching item names...", 55)
        type_ids = [c.type_id for c in candidates]
        names = await self.client.get_type_names_bulk(type_ids)
        
        update(f"Fetching sell station history...", 65)
        from history.history_source import get_history_for_hub
        # Dispatcher routes structure hubs to observed history, NPC hubs to
        # regional. Result shape matches MarketHistoryDB.get_history_bulk
        # exactly so process_crosshub / parse_history_stats / HistoryStats
        # all work unchanged.
        sell_history = await get_history_for_hub(
            self.client, sell_station_key, type_ids
        )

        update(f"Fetching buy station history...", 72)
        buy_history = await get_history_for_hub(
            self.client, buy_station_key, type_ids
        )
        
        # Fetch Jita history for ceiling cap and price validation
        # Skip if either buy or sell station IS Jita (we already have it)
        update(f"Fetching Jita history for validation...", 78)
        if buy_region_id == JITA_REGION_ID:
            jita_history = buy_history
        elif sell_region_id == JITA_REGION_ID:
            jita_history = sell_history
        else:
            jita_history = await self.client.get_market_history_bulk(
                JITA_REGION_ID, type_ids, use_cache=True
            )
        
        # === PHASE 5: Process deals ===
        
        update("Finding guaranteed profits...", 80)
        from scanning.scanner_crosshub import process_crosshub
        low_risk, high_risk = process_crosshub(
            buy_station_data=buy_data,
            sell_station_data=sell_data,
            names=names,
            system_cache=self.client.system_cache,
            sell_station_history=sell_history,
            buy_station_history=buy_history,
            jita_history=jita_history,
            buy_station_key=buy_station_key,
            sell_station_key=sell_station_key,
            buy_skills=buy_skills,
            sell_skills=sell_skills,
            min_profit_per_unit=profit_threshold,
            min_total_profit=total_threshold,
            min_margin_percent=margin_threshold,
            min_velocity=volume_threshold,
            max_cost=max_cost,
            min_guaranteed_volume=min_guaranteed_volume,
        )
        
        print(f"Cross-hub Low Risk: {len(low_risk)}")
        print(f"Cross-hub High Risk: {len(high_risk)}")

        # Demand / Restock pass — reuses the data we already have, no extra ESI.
        # Different lens than arbitrage: "how much to ship to fill demand."
        demand_rows = []
        try:
            from scanning.scanner_demand import build_demand_rows
            update("Computing demand / restock rows...", 92)
            demand_rows = build_demand_rows(
                buy_station_data=buy_data,
                sell_station_data=sell_data,
                names=names,
                buy_station_history=buy_history,
                sell_station_history=sell_history,
                buy_station_key=buy_station_key,
                sell_station_key=sell_station_key,
                buy_skills=buy_skills,
                sell_skills=sell_skills,
                reference_date=(self.client.market_history.get_latest_date()
                                if self.client.market_history else None),
            )
            print(f"Demand/Restock rows: {len(demand_rows)}")
        except Exception as e:
            print(f"[Demand] build_demand_rows failed: {e}")
            import traceback
            traceback.print_exc()

        update("Complete!", 100)

        return CrossHubScanResult(
            low_risk=low_risk,
            high_risk=high_risk,
            buy_station_orders=buy_orders,
            sell_station_orders=sell_orders,
            demand_rows=demand_rows,
        )

    def _process_orders(self, orders: list[dict]) -> dict[int, dict]:
        """
        Process raw orders into aggregated buy/sell data per type.
        Tracks lowest sell, 2nd lowest sell, highest buy, and volume.
        """
        data = {}

        for order in orders:
            type_id = order["type_id"]
            price = order["price"]
            is_buy = order["is_buy_order"]
            volume = order["volume_remain"]
            system_id = order.get("system_id", 0)

            if type_id not in data:
                data[type_id] = {
                    "sell": float("inf"),
                    "sell_2nd": float("inf"),
                    "buy": 0,
                    "volume": 0,
                    "system_id": 0
                }

            if is_buy:
                if price > data[type_id]["buy"]:
                    data[type_id]["buy"] = price
            else:
                if price < data[type_id]["sell"]:
                    data[type_id]["sell_2nd"] = data[type_id]["sell"]
                    data[type_id]["sell"] = price
                    data[type_id]["volume"] = volume
                    data[type_id]["system_id"] = system_id
                elif price == data[type_id]["sell"]:
                    data[type_id]["volume"] += volume
                elif price < data[type_id]["sell_2nd"]:
                    data[type_id]["sell_2nd"] = price

        return data

    def _process_orders_crosshub(self, orders: list[dict]) -> dict[int, dict]:
        """
        Process orders for cross-hub scanning.

        Tracks:
        - Lowest sell, 2nd lowest sell, sell volume at floor
        - Highest buy AND buy volume (needed for guaranteed profit calc)
        - total_sell_qty / total_buy_qty aggregates (consumed by Demand/Restock
          to compute days-of-stock and source availability)
        """
        data = {}

        for order in orders:
            type_id = order["type_id"]
            price = order["price"]
            is_buy = order["is_buy_order"]
            volume = order["volume_remain"]
            system_id = order.get("system_id", 0)

            if type_id not in data:
                data[type_id] = {
                    "sell": float("inf"),
                    "sell_2nd": float("inf"),
                    "buy": 0,
                    "buy_volume": 0,
                    "volume": 0,
                    "total_sell_qty": 0,
                    "total_buy_qty": 0,
                    "system_id": 0
                }

            if is_buy:
                data[type_id]["total_buy_qty"] += volume
                if price > data[type_id]["buy"]:
                    data[type_id]["buy"] = price
                    data[type_id]["buy_volume"] = volume
                elif price == data[type_id]["buy"]:
                    data[type_id]["buy_volume"] += volume
            else:
                data[type_id]["total_sell_qty"] += volume
                if price < data[type_id]["sell"]:
                    data[type_id]["sell_2nd"] = data[type_id]["sell"]
                    data[type_id]["sell"] = price
                    data[type_id]["volume"] = volume
                    data[type_id]["system_id"] = system_id
                elif price == data[type_id]["sell"]:
                    data[type_id]["volume"] += volume
                elif price < data[type_id]["sell_2nd"]:
                    data[type_id]["sell_2nd"] = price

        return data

    def _build_candidates(
        self,
        local_hub: dict[int, dict],
        jita: dict[int, dict],
        max_cost: float = None,
        skip_scam_check: bool = False
    ) -> list[Candidate]:
        """
        Build candidate list with minimal filtering.
        
        Only filters:
        - Must have valid sell orders (lowest and 2nd lowest)
        - Scam check: local price not >5% above Jita (skipped when hub IS Jita)
        - Max cost filter
        
        All profit/margin/velocity filtering happens in category processors.
        """
        candidates = []
        is_structure = self.hub_config.get("type") == "structure"

        for type_id, local_info in local_hub.items():
            local_sell = local_info["sell"]
            local_sell_2nd = local_info.get("sell_2nd", float("inf"))
            local_buy = local_info.get("buy", 0)
            volume = local_info["volume"]
            system_id = local_info["system_id"]

            # Must have valid sell orders
            if local_sell == float("inf"):
                continue

            # Get Jita data
            jita_info = jita.get(type_id, {"sell": float("inf"), "buy": 0})
            jita_sell = jita_info["sell"] if jita_info["sell"] != float("inf") else 0

            # Need a competition reference for the ceiling math. On NPC hubs
            # we require a real on-station 2nd-lowest. On structures most items
            # have a single listing, so we synthesize the 2nd from the live
            # Jita price — the "next best alternative" the buyer would chase.
            # If Jita has no price either, we still can't price the deal.
            if local_sell_2nd == float("inf"):
                if is_structure and jita_sell > 0:
                    local_sell_2nd = jita_sell
                else:
                    continue

            # SCAM CHECK: local price way above Jita = suspicious
            # Skip when scanning Jita itself (would incorrectly filter everything)
            if not skip_scam_check and jita_sell > 0:
                overpriced_ratio = (local_sell - jita_sell) / jita_sell
                if overpriced_ratio > SCAM_THRESHOLD:
                    continue

            # Max cost filter (applied early to reduce processing)
            total_cost = local_sell * volume
            if max_cost is not None and total_cost > max_cost:
                continue

            candidates.append(Candidate(
                type_id=type_id,
                system_id=system_id,
                local_sell=local_sell,
                local_sell_2nd=local_sell_2nd,
                local_buy=local_buy,
                jita_sell=jita_sell,
                volume=volume
            ))

        return candidates
