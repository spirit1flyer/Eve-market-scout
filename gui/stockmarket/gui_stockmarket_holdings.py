"""Stock Market Holdings panel for EVE Market Scout.

Tracks items the user is actively invested in with ESI transaction sync:
- Items manually added to watch
- Automatic buy/sell detection from ESI wallet
- Manual inventory entries
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from typing import Optional, Callable, Dict, List, TYPE_CHECKING

from core.config import get_hub_config
from sde.sde_manager import get_sde_manager
from scanning.scanner_common import parse_history_stats

# Import from split modules
from gui.stockmarket.gui_stockmarket_holdings_data import HoldingEntry, HoldingsManager
from gui.stockmarket.gui_stockmarket_holdings_dialogs import (
    RecordPurchaseDialog, RecordSaleDialog, HoldingDetailsDialog
)

if TYPE_CHECKING:
    from analytics.historical_profiles import ProfileManager
    from esi.esi_wallet import ESIWallet


class HoldingsPanel:
    """Holdings sub-panel within a hub tab with ESI transaction sync."""
    
    def __init__(
        self,
        parent: ttk.Frame,
        hub_key: str,
        profiles: "ProfileManager",
        get_client: Optional[Callable] = None,
        set_status: Optional[Callable[[str], None]] = None,
    ):
        self.parent = parent
        self.hub_key = hub_key
        self.profiles = profiles
        self.get_client = get_client
        self.set_status = set_status or (lambda s: None)
        
        self.hub_config = get_hub_config(hub_key)
        self.region_id = self.hub_config["region_id"]
        self.station_id = self.hub_config["station_id"]
        
        # Holdings manager
        self.holdings = HoldingsManager(hub_key)
        
        # Live prices
        self.live_prices: Dict[int, float] = {}

        # Coalesce repeated refresh_display requests within one mainloop tick.
        # ESI sync calls sync_from_orders + sync_from_esi_wallet back-to-back;
        # both used to refresh, costing ~440-530ms of duplicate work per cycle.
        # Also covers the holdings-freshen / ESI-sync race on the same drain.
        self._refresh_scheduled = False

        # Create UI
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill=tk.BOTH, expand=True)
        
        self._create_widgets()
        self.refresh_display()
    
    def _create_widgets(self):
        """Create panel widgets."""
        # Toolbar
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill=tk.X, padx=5, pady=5)
        
        ttk.Button(
            toolbar,
            text="Add Inventory",
            command=self._on_add_inventory
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(
            toolbar,
            text="Remove",
            command=self._on_remove
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Frame(toolbar).pack(side=tk.LEFT, expand=True)  # Spacer
        
        self.count_label = ttk.Label(toolbar, text="0 holdings")
        self.count_label.pack(side=tk.RIGHT, padx=5)
        
        # Treeview
        self._create_treeview()
        
        # Sort state
        self.sort_column = "name"
        self.sort_reverse = False
    
    def _create_treeview(self):
        """Create holdings treeview with sortable columns."""
        tree_frame = ttk.Frame(self.frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))
        
        columns = (
            "sig", "li", "name", "qty", "buying", "selling",
            "avg_cost", "current", "profit", "target_buy", "target_sell",
            "hist_min", "hist_max", "volume", "7d_day"
        )
        
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="extended"
        )
        
        # Configure columns with sort bindings
        col_config = [
            ("sig", "Signal", 50, tk.CENTER),
            ("li", "LI", 32, tk.CENTER),
            ("name", "Item Name", 180, tk.W),
            ("qty", "Qty", 50, tk.E),
            ("buying", "Buy", 45, tk.E),
            ("selling", "Sell", 45, tk.E),
            ("avg_cost", "Avg Cost", 75, tk.E),
            ("current", "Current", 75, tk.E),
            ("profit", "Profit", 80, tk.E),
            ("target_buy", "Buy Target", 80, tk.E),
            ("target_sell", "Sell Target", 80, tk.E),
            ("hist_min", "Hist Min", 75, tk.E),
            ("hist_max", "Hist Max", 75, tk.E),
            ("volume", "Volume", 70, tk.E),
            ("7d_day", "7d/Day", 70, tk.E),
        ]
        
        for col_id, heading, width, anchor in col_config:
            self.tree.heading(col_id, text=heading, command=lambda c=col_id: self._sort_by(c))
            self.tree.column(col_id, width=width, anchor=anchor)
        
        # Scrollbars
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        
        # Tags for trend-based row coloring (year-over-year floor comparison)
        self.tree.tag_configure("trend_down", background="#DC143C", foreground="white")
        self.tree.tag_configure("trend_up", background="#FFD700", foreground="black")
        self.tree.tag_configure("trend_stable", background="#228B22", foreground="white")
        self.tree.tag_configure("trend_none", background="#ffffff", foreground="black")
        
        # Double-click binding for details dialog
        self.tree.bind("<Double-1>", self._on_double_click)
        
        # Context menu
        self.context_menu = tk.Menu(self.tree, tearoff=0)
        self.context_menu.add_command(label="View Details", command=self._on_view_details)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Show Indicator Details", command=self._on_show_indicator_details)
        self.context_menu.add_command(label="Indicator Help", command=self._on_indicator_help)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Record Purchase", command=self._on_record_purchase)
        self.context_menu.add_command(label="Record Sale", command=self._on_record_sale)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Set Avg Cost", command=self._on_set_avg_cost)
        self.context_menu.add_command(label="Set Quantity", command=self._on_set_quantity)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Name", command=self._on_copy_name)
        self.context_menu.add_command(label="Remove", command=self._on_remove)
        
        self.tree.bind("<Button-3>", self._on_right_click)
    
    def _sort_by(self, column: str):
        """Sort treeview by column."""
        import time as _pt
        _pt0 = _pt.perf_counter()
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False

        self.refresh_display()
        _pt_total = _pt.perf_counter() - _pt0
        print(f"[PerfTimer] HoldingsPanel._sort_by column={column} total={_pt_total*1000:.0f}ms (includes refresh_display)")
    
    def _on_double_click(self, event):
        """Handle double-click to open price history graph."""
        selected = self.tree.selection()
        if not selected:
            return

        type_id = int(selected[0])
        entry = self.holdings.get_item(type_id)
        if not entry:
            return

        # Open price graph (same pattern as risk panels)
        from analytics.graphing import show_price_graph
        show_price_graph(
            self.frame,
            type_id=type_id,
            type_name=entry.type_name,
            region_id=self.region_id,
            profiles=self.profiles,
        )
    
    def _on_right_click(self, event):
        """Show context menu."""
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)
    
    # === ESI Transaction Sync ===
    
    def sync_from_esi_wallet(self, wallet: "ESIWallet") -> dict:
        """Sync holdings from ESI wallet transactions.
        
        Scans wallet.transactions for buy/sell matching items in holdings.
        Only processes transactions not already tracked.
        
        Args:
            wallet: ESIWallet instance with fetched transactions
            
        Returns:
            Dict with counts: {buys_synced, sales_synced}
        """
        results = {"buys_synced": 0, "sales_synced": 0}
        
        if not wallet or not wallet.transactions:
            return results
        
        sde = get_sde_manager()
        holdings_type_ids = set(self.holdings.get_type_ids())
        
        if not holdings_type_ids:
            return results
        
        # Filter to transactions at this hub's station
        hub_transactions = [
            t for t in wallet.transactions
            if t.location_id == self.station_id
        ]
        
        for tx in hub_transactions:
            if tx.type_id not in holdings_type_ids:
                continue
            
            type_name = sde.get_type_name(tx.type_id) or f"Type {tx.type_id}"
            
            if tx.is_buy:
                # Buy transaction
                entry = self.holdings.add_inventory(
                    type_id=tx.type_id,
                    type_name=type_name,
                    quantity=tx.quantity,
                    avg_cost=tx.unit_price,
                    transaction_id=tx.transaction_id
                )
                # Check if it was actually new (not already processed)
                if tx.transaction_id in entry.processed_buy_ids:
                    if entry.processed_buy_ids[-1] == tx.transaction_id:
                        # We just added it
                        results["buys_synced"] += 1
                        print(f"[Holdings] ESI buy: {type_name} x{tx.quantity} @ {tx.unit_price:.2f}")
            else:
                # Sell transaction
                entry = self.holdings.record_sale(
                    type_id=tx.type_id,
                    quantity=tx.quantity,
                    price_per_unit=tx.unit_price,
                    transaction_id=tx.transaction_id
                )
                if entry and tx.transaction_id in entry.processed_sell_ids:
                    if entry.processed_sell_ids[-1] == tx.transaction_id:
                        results["sales_synced"] += 1
                        print(f"[Holdings] ESI sale: {type_name} x{tx.quantity} @ {tx.unit_price:.2f}")
        
        if results["buys_synced"] > 0 or results["sales_synced"] > 0:
            self._schedule_refresh()

        return results
    
    def sync_from_orders(self, orders: List[dict]):
        """Sync holdings from ESI order data (active orders count)."""
        buy_counts: Dict[int, int] = {}
        sell_counts: Dict[int, int] = {}
        
        sde = get_sde_manager()
        
        for order in orders:
            type_id = order.get("type_id")
            if not type_id:
                continue
            
            if order.get("is_buy_order"):
                buy_counts[type_id] = buy_counts.get(type_id, 0) + 1
            else:
                sell_counts[type_id] = sell_counts.get(type_id, 0) + 1
        
        # Update holdings
        all_type_ids = set(buy_counts.keys()) | set(sell_counts.keys())
        
        for type_id in all_type_ids:
            type_name = sde.get_type_name(type_id) or f"Type {type_id}"
            self.holdings.update_from_orders(
                type_id,
                type_name,
                buy_orders=buy_counts.get(type_id, 0),
                sell_orders=sell_counts.get(type_id, 0)
            )

        self._schedule_refresh()

    # === Display ===

    def _schedule_refresh(self):
        """Queue one refresh_display for the next mainloop idle tick.

        Multiple callers within the same drain cycle coalesce to a single
        refresh. Safe to call from the UI thread only.
        """
        if self._refresh_scheduled:
            self._coalesce_count = getattr(self, "_coalesce_count", 1) + 1
            return
        try:
            if not self.frame.winfo_exists():
                print(f"[HoldingsCoalesce] hub={self.hub_key} schedule SKIP: frame destroyed")
                return
        except tk.TclError as e:
            print(f"[HoldingsCoalesce] hub={self.hub_key} schedule SKIP: TclError {e}")
            return
        self._refresh_scheduled = True
        self._coalesce_count = 1
        try:
            self.frame.after_idle(self._do_scheduled_refresh)
        except tk.TclError as e:
            # after_idle on a dying frame can throw; recover so we don't
            # leave _refresh_scheduled stuck True (which would block all
            # future refreshes silently).
            self._refresh_scheduled = False
            print(f"[HoldingsCoalesce] hub={self.hub_key} schedule FAIL: after_idle TclError {e}")
            return
        print(f"[HoldingsCoalesce] hub={self.hub_key} schedule: queued for idle")

    def _do_scheduled_refresh(self):
        coalesced = getattr(self, "_coalesce_count", 1)
        self._refresh_scheduled = False
        self._coalesce_count = 0
        try:
            if not self.frame.winfo_exists():
                print(f"[HoldingsCoalesce] hub={self.hub_key} flush SKIP: frame destroyed (coalesced={coalesced})")
                return
        except tk.TclError as e:
            print(f"[HoldingsCoalesce] hub={self.hub_key} flush SKIP: TclError {e} (coalesced={coalesced})")
            return
        print(f"[HoldingsCoalesce] hub={self.hub_key} flush: firing (coalesced={coalesced})")
        self.refresh_display()

    def refresh_display(self):
        """Refresh the holdings display.

        Material filter is applied via check_material_risk() at line 331
        which reads from the pre-populated session cache.  The cache is
        managed by HubPanel.apply_material_filter().
        """
        import time as _pt
        _pt0 = _pt.perf_counter()
        _ts = _pt.perf_counter()
        self.tree.delete(*self.tree.get_children())
        _step_tree_clear = _pt.perf_counter() - _ts

        _ts = _pt.perf_counter()
        holdings = self.holdings.get_all()
        self.count_label.configure(text=f"{len(holdings)} holdings")

        holdings = self.holdings.get_all()
        self.count_label.configure(text=f"{len(holdings)} holdings")
        _step_holdings_get = _pt.perf_counter() - _ts

        # Load leading indicators cache for this region (one query)
        _ts = _pt.perf_counter()
        try:
            from analytics import leading_indicators_storage
            li_cache = leading_indicators_storage.load_for_region(
                self.region_id
            )
        except Exception as e:
            print(f"[Holdings] LI cache load error: {e}")
            li_cache = {}
        _step_li_cache = _pt.perf_counter() - _ts

        # Prefetch 7d/30d history for ALL holdings in one SQLite query +
        # one ESI cache lookup. _calculate_trend reads from these dicts
        # instead of querying per-item. Mirrors gui_stockmarket_hub_refresh.
        from history.market_history import get_market_history_db
        holding_type_ids = [h.type_id for h in holdings]
        _ts = _pt.perf_counter()
        sqlite_hist = (
            get_market_history_db().get_history_bulk(
                self.region_id, holding_type_ids, days=30
            )
            if holding_type_ids else {}
        )
        _step_history_bulk = _pt.perf_counter() - _ts
        _ts = _pt.perf_counter()
        esi_cache = {}
        if self.get_client:
            client = self.get_client()
            if client:
                esi_cache = client.history_cache.get(self.region_id, {})
        _step_esi_cache = _pt.perf_counter() - _ts

        # Build list with sort keys
        items_to_show = []

        _ts = _pt.perf_counter()
        for entry in holdings:
            profile = self.profiles.get_computed_profile(entry.type_id, self.region_id)
            current_price = self.live_prices.get(entry.type_id, 0)
            yearly_stats = self.profiles.get_yearly_stats(entry.type_id, self.region_id) if profile else {}
            trend_tag = self._get_trend_tag(yearly_stats, entry.type_id) if yearly_stats else "trend_none"
            # Override color if material filter promotes to medium risk
            if trend_tag == "trend_stable":
                from analytics.stockmarket_filters import check_material_risk
                if check_material_risk(entry.type_id, self.region_id) == "medium":
                    trend_tag = "trend_up"
            trend_pct = self._calculate_trend(entry.type_id, sqlite_hist, esi_cache)
            
            items_to_show.append({
                "entry": entry,
                "profile": profile,
                "current_price": current_price,
                "trend_tag": trend_tag,
                "trend_pct": trend_pct,
            })
        _step_per_item = _pt.perf_counter() - _ts

        # Sort
        _ts = _pt.perf_counter()
        items_to_show = self._sort_holdings(items_to_show)
        _step_sort = _pt.perf_counter() - _ts

        # Populate tree
        _ts = _pt.perf_counter()
        for item in items_to_show:
            entry = item["entry"]
            profile = item["profile"]
            current_price = item["current_price"]
            trend_tag = item["trend_tag"]
            trend_pct = item.get("trend_pct")
            
            # Calculate signal
            sig = ""
            if current_price > 0 and profile:
                if current_price < profile.weighted_p_low:
                    sig = "[B]"
                elif current_price > profile.weighted_p_high:
                    sig = "[S]"
            
            # Calculate unrealized profit
            if entry.quantity_held > 0 and current_price > 0:
                unrealized = (current_price - entry.average_cost) * entry.quantity_held
                profit_str = self._format_isk(unrealized)
                if unrealized >= 0:
                    profit_str = "+" + profit_str
            else:
                unrealized = 0
                profit_str = "--"
            
            # Format 7d trend
            trend_str = f"{trend_pct:+.1f}%" if trend_pct is not None else "--"
            
            # Leading indicator letter (worst flag wins, blank if no data)
            from gui.gui_indicator_help import get_indicator_letter
            li_result = li_cache.get(entry.type_id)
            li_letter = (
                get_indicator_letter(li_result.flags)
                if li_result else ""
            )
            
            values = (
                sig,
                li_letter,
                entry.type_name,
                entry.quantity_held if entry.quantity_held > 0 else "--",
                entry.active_buy_orders if entry.active_buy_orders > 0 else "--",
                entry.active_sell_orders if entry.active_sell_orders > 0 else "--",
                self._format_isk(entry.average_cost) if entry.average_cost > 0 else "--",
                self._format_isk(current_price) if current_price > 0 else "--",
                profit_str,
                self._format_isk(profile.weighted_p_low) if profile else "--",
                self._format_isk(profile.weighted_p_high) if profile else "--",
                self._format_isk(profile.hist_min) if profile and profile.hist_min > 0 else "--",
                self._format_isk(profile.hist_max) if profile and profile.hist_max > 0 else "--",
                self._format_isk(profile.avg_daily_volume) if profile and profile.avg_daily_volume > 0 else "--",
                trend_str,
            )
            
            self.tree.insert(
                "",
                tk.END,
                iid=str(entry.type_id),
                values=values,
                tags=(trend_tag,)
            )
        _step_tree_insert = _pt.perf_counter() - _ts
        _pt_total = _pt.perf_counter() - _pt0
        print(
            f"[PerfTimer] HoldingsPanel.refresh_display hub_region={self.region_id} "
            f"total={_pt_total*1000:.0f}ms holdings={len(holdings)} "
            f"tree_clear={_step_tree_clear*1000:.0f}ms "
            f"holdings_get={_step_holdings_get*1000:.0f}ms "
            f"li_cache={_step_li_cache*1000:.0f}ms "
            f"history_bulk={_step_history_bulk*1000:.0f}ms "
            f"esi_cache={_step_esi_cache*1000:.0f}ms "
            f"per_item_loop={_step_per_item*1000:.0f}ms "
            f"sort={_step_sort*1000:.0f}ms "
            f"tree_insert={_step_tree_insert*1000:.0f}ms"
        )

    def update_prices_only(self):
        """Update only price-dependent columns without rebuilding the treeview.
        
        This is called on every scan (~5 min) to update current prices.
        Much faster than refresh_display() since it doesn't query the DB.
        """
        for iid in self.tree.get_children():
            type_id = int(iid)
            current_price = self.live_prices.get(type_id, 0)
            
            # Get existing values
            values = list(self.tree.item(iid, "values"))
            if len(values) < 15:
                continue
            
            # Get profile targets for signal calculation (already in treeview)
            try:
                p_low_str = values[9]  # target_buy column (after LI insertion)
                p_high_str = values[10]  # target_sell column
                p_low = self._parse_isk(p_low_str)
                p_high = self._parse_isk(p_high_str)
            except (ValueError, IndexError):
                p_low = 0
                p_high = 0
            
            # Get avg_cost and qty for profit calculation
            try:
                avg_cost = self._parse_isk(values[6]) if values[6] != "--" else 0
                qty_str = values[3]
                qty = int(qty_str) if qty_str != "--" else 0
            except (ValueError, IndexError):
                avg_cost = 0
                qty = 0
            
            # Calculate signal
            sig = ""
            if current_price > 0 and p_low > 0 and p_high > 0:
                if current_price < p_low:
                    sig = "[B]"
                elif current_price > p_high:
                    sig = "[S]"
            
            # Calculate unrealized profit
            profit_str = "--"
            if qty > 0 and current_price > 0 and avg_cost > 0:
                unrealized = (current_price - avg_cost) * qty
                profit_str = self._format_isk(unrealized)
                if unrealized >= 0:
                    profit_str = "+" + profit_str
            
            # Update only the columns that depend on live price
            values[0] = sig  # signal (col 0)
            values[7] = self._format_isk(current_price) if current_price > 0 else "--"  # current price (col 7)
            values[8] = profit_str  # profit (col 8 after LI insertion)
            
            self.tree.item(iid, values=values)
    
    def _parse_isk(self, value_str: str) -> float:
        """Parse ISK string back to float (e.g., '1.5M' -> 1500000)."""
        if not value_str or value_str == "--":
            return 0
        
        value_str = str(value_str).strip()
        multiplier = 1
        
        if value_str.endswith("B"):
            multiplier = 1_000_000_000
            value_str = value_str[:-1]
        elif value_str.endswith("M"):
            multiplier = 1_000_000
            value_str = value_str[:-1]
        elif value_str.endswith("K"):
            multiplier = 1_000
            value_str = value_str[:-1]
        
        try:
            return float(value_str) * multiplier
        except ValueError:
            return 0

    def _get_trend_tag(self, yearly_stats: dict, type_id: int = None) -> str:
        """Determine trend tag based on year-over-year floor comparison.
        
        Args:
            yearly_stats: Dict of year -> YearlyStats
            type_id: Optional type ID for material analysis
        """
        if len(yearly_stats) < 2:
            return "trend_none"
        
        years = sorted(yearly_stats.keys(), reverse=True)
        floors = [yearly_stats[y].p_low for y in years[:3]]
        
        if len(floors) < 2:
            return "trend_none"
        
        declining = all(floors[i] < floors[i + 1] for i in range(len(floors) - 1))
        if declining:
            return "trend_down"
        
        rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
        if rising:
            return "trend_up"
        
        if len(floors) >= 2:
            avg_floor = sum(floors) / len(floors)
            if avg_floor > 0:
                max_deviation = max(abs(f - avg_floor) / avg_floor * 100 for f in floors)
                if max_deviation <= 15:
                    return "trend_stable"
        
        return "trend_none"
    
    def _check_material_risk(self, type_id: int) -> str:
        """Check material correlation for risk adjustment.
        
        Returns 'low', 'medium', or 'skip'.
        """
        try:
            from analytics.stockmarket_filters import check_material_risk
            return check_material_risk(type_id, self.region_id)
        except ImportError:
            return "skip"
        except Exception as e:
            print(f"[Holdings] Material check error for {type_id}: {e}")
            return "skip"
    
    def _calculate_trend(
        self,
        type_id: int,
        sqlite_hist: dict,
        esi_cache: dict,
    ) -> float | None:
        """Calculate 7d vs 30d price trend percentage.

        Reads from caller-provided dicts (prefetched once per
        refresh_display call) so we don't run N SQLite queries per
        refresh. Merges everef SQLite history (covers all profiled
        items, lags 1-4 days) with ESI history_cache (fresh, but only
        scanner candidates). SQLite gives coverage; ESI fills the
        recency gap. Mirrors the merge in gui_stockmarket_hub_refresh.
        """
        sqlite_records = sqlite_hist.get(type_id, [])
        esi_records = esi_cache.get(type_id, [])

        if sqlite_records and esi_records:
            sqlite_dates = {r.get("date") for r in sqlite_records}
            merged = sqlite_records + [
                r for r in esi_records if r.get("date") not in sqlite_dates
            ]
        else:
            merged = sqlite_records or esi_records

        if not merged or len(merged) < 7:
            return None

        stats = parse_history_stats(merged)
        if stats.avg_price_7d <= 0 or stats.avg_price_30d <= 0:
            return None

        return ((stats.avg_price_7d - stats.avg_price_30d) / stats.avg_price_30d) * 100
    
    def _sort_holdings(self, items: List[dict]) -> List[dict]:
        """Sort holdings by current sort column."""
        def get_sort_key(item):
            entry = item["entry"]
            profile = item["profile"]
            current_price = item["current_price"]
            trend_pct = item.get("trend_pct")
            
            if self.sort_column == "sig":
                # Sort by signal: [B] first, then [S], then empty
                if current_price > 0 and profile:
                    if current_price < profile.weighted_p_low:
                        return 0  # [B] first
                    elif current_price > profile.weighted_p_high:
                        return 1  # [S] second
                return 2  # No signal last
            elif self.sort_column == "name":
                return entry.type_name.lower()
            elif self.sort_column == "qty":
                return entry.quantity_held
            elif self.sort_column == "buying":
                return entry.active_buy_orders
            elif self.sort_column == "selling":
                return entry.active_sell_orders
            elif self.sort_column == "avg_cost":
                return entry.average_cost
            elif self.sort_column == "current":
                return current_price
            elif self.sort_column == "profit":
                if entry.quantity_held > 0 and current_price > 0:
                    return (current_price - entry.average_cost) * entry.quantity_held
                return -9999999
            elif self.sort_column == "target_buy":
                return profile.weighted_p_low if profile else 0
            elif self.sort_column == "target_sell":
                return profile.weighted_p_high if profile else 0
            elif self.sort_column == "hist_min":
                return profile.hist_min if profile else 0
            elif self.sort_column == "hist_max":
                return profile.hist_max if profile else 0
            elif self.sort_column == "volume":
                return profile.avg_daily_volume if profile else 0
            elif self.sort_column == "7d_day":
                return trend_pct if trend_pct is not None else -9999
            else:
                return 0
        
        return sorted(items, key=get_sort_key, reverse=self.sort_reverse)
    
    # === Public API ===
    
    def add_watched_item(self, type_id: int, type_name: str):
        """Add an item to the watch list."""
        self.holdings.add_watched(type_id, type_name)
        self.refresh_display()
    
    def update_live_prices(self, prices: Dict[int, float]):
        """Update live prices and refresh."""
        self.live_prices.update(prices)
        self.refresh_display()
    
    # === Context Menu Actions ===
    
    def _on_view_details(self):
        """Show details dialog for selected item."""
        selected = self.tree.selection()
        if not selected:
            return
        
        type_id = int(selected[0])
        entry = self.holdings.get_item(type_id)
        if entry:
            HoldingDetailsDialog(self.frame, entry.type_name, entry)
    
    def _on_record_purchase(self):
        """Record a manual purchase."""
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Select Item", "Select an item to record a purchase.")
            return
        
        type_id = int(selected[0])
        entry = self.holdings.get_item(type_id)
        if not entry:
            return
        
        def on_save(tid: int, qty: int, price: float):
            self.holdings.add_inventory(tid, entry.type_name, qty, price)
            self.refresh_display()
            self.set_status(f"Recorded purchase: {qty}x {entry.type_name} @ {price:,.2f}")
        
        RecordPurchaseDialog(
            self.frame,
            type_id=type_id,
            type_name=entry.type_name,
            current_qty=entry.quantity_held,
            current_avg_cost=entry.average_cost,
            on_save=on_save
        )
    
    def _on_record_sale(self):
        """Record a manual sale."""
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Select Item", "Select an item to record a sale.")
            return
        
        type_id = int(selected[0])
        entry = self.holdings.get_item(type_id)
        if not entry:
            return
        
        if entry.quantity_held <= 0:
            messagebox.showinfo("No Holdings", f"You don't hold any {entry.type_name} to sell.")
            return
        
        def on_save(tid: int, qty: int, price: float):
            self.holdings.record_sale(tid, qty, price)
            self.refresh_display()
            self.set_status(f"Recorded sale: {qty}x {entry.type_name} @ {price:,.2f}")
        
        RecordSaleDialog(
            self.frame,
            type_id=type_id,
            type_name=entry.type_name,
            current_qty=entry.quantity_held,
            current_avg_cost=entry.average_cost,
            on_save=on_save
        )
    
    def _on_add_inventory(self):
        """Add inventory dialog."""
        selected = self.tree.selection()
        
        if selected:
            type_id = int(selected[0])
            entry = self.holdings.get_item(type_id)
            if entry:
                # Use the purchase dialog for existing items
                self._on_record_purchase()
                return
        
        # No selection - prompt for item
        self.set_status("Select an item first, or add via Discovery scanner")
    
    def _on_set_avg_cost(self):
        """Set average cost for selected item."""
        selected = self.tree.selection()
        if not selected:
            return
        
        type_id = int(selected[0])
        entry = self.holdings.get_item(type_id)
        if not entry:
            return
        
        cost_str = simpledialog.askstring(
            "Set Average Cost",
            f"New average cost for {entry.type_name}:",
            initialvalue=f"{entry.average_cost:.2f}",
            parent=self.frame
        )
        if not cost_str:
            return
        
        try:
            new_cost = float(cost_str.replace(",", ""))
        except ValueError:
            messagebox.showerror("Error", "Invalid cost")
            return
        
        self.holdings.set_average_cost(type_id, new_cost)
        self.refresh_display()
        self.set_status(f"Updated avg cost for {entry.type_name}")
    
    def _on_set_quantity(self):
        """Set quantity for selected item."""
        selected = self.tree.selection()
        if not selected:
            return
        
        type_id = int(selected[0])
        entry = self.holdings.get_item(type_id)
        if not entry:
            return
        
        qty_str = simpledialog.askstring(
            "Set Quantity",
            f"New quantity for {entry.type_name}:",
            initialvalue=str(entry.quantity_held),
            parent=self.frame
        )
        if not qty_str:
            return
        
        try:
            new_qty = int(qty_str.replace(",", ""))
        except ValueError:
            messagebox.showerror("Error", "Invalid quantity")
            return
        
        self.holdings.set_quantity(type_id, new_qty)
        self.refresh_display()
        self.set_status(f"Updated quantity for {entry.type_name}")
    
    def _on_remove(self):
        """Remove selected items."""
        selected = self.tree.selection()
        if not selected:
            return
        
        if len(selected) > 1:
            msg = f"Remove {len(selected)} items from holdings?"
        else:
            entry = self.holdings.get_item(int(selected[0]))
            name = entry.type_name if entry else "item"
            msg = f"Remove {name} from holdings?"
        
        if not messagebox.askyesno("Confirm Remove", msg):
            return
        
        for iid in selected:
            self.holdings.remove(int(iid))
        
        self.refresh_display()
        self.set_status(f"Removed {len(selected)} item(s)")
    
    def _on_copy_name(self):
        """Copy item name to clipboard."""
        selected = self.tree.selection()
        if not selected:
            return
        
        item = self.tree.item(selected[0])
        name = item["values"][2]  # Name is now at index 2 (after signal + LI)
        
        self.frame.clipboard_clear()
        self.frame.clipboard_append(name)
        self.set_status(f"Copied: {name}")
    
    def _on_show_indicator_details(self):
        """Show leading indicator details for the selected holding."""
        selected = self.tree.selection()
        if not selected:
            return
        
        type_id = int(selected[0])
        entry = self.holdings.get_item(type_id)
        type_name = entry.type_name if entry else f"Type {type_id}"
        
        try:
            from analytics import leading_indicators_storage
            cache = leading_indicators_storage.load_for_region(
                self.region_id
            )
            result = cache.get(type_id)
        except Exception as e:
            print(f"[Holdings] LI details lookup error: {e}")
            result = None
        
        from gui.gui_indicator_help import show_indicator_details_dialog
        show_indicator_details_dialog(self.frame, type_name, result)
    
    def _on_indicator_help(self):
        """Show the general indicator reference dialog."""
        from gui.gui_indicator_help import show_indicator_help_dialog
        show_indicator_help_dialog(self.frame)
    
    def _format_isk(self, value: float) -> str:
        """Format ISK value."""
        if value >= 1_000_000_000:
            return f"{value / 1_000_000_000:.2f}B"
        elif value >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        elif value >= 1_000:
            return f"{value / 1_000:.1f}K"
        else:
            return f"{value:.0f}"
