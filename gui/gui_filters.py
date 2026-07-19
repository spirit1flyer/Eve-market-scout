"""Filter controls and item management for EVE Market Scout."""

import tkinter as tk
from tkinter import ttk
import os
import json
import threading
from typing import Callable, Union

from scanning.scanner_common import Deal
from core.config import MIN_PROFIT_PER_UNIT, MIN_TOTAL_PROFIT, MIN_MARGIN_PERCENT, MIN_DAILY_VOLUME, get_hub_config, DEFAULT_HUB
from core.sound_manager import get_data_dir


def _check_thread(context: str):
    """Debug helper - warn if not on main thread when accessing Tk vars."""
    current = threading.current_thread()
    if current is not threading.main_thread():
        print(f"[THREAD WARNING] {context} called from {current.name} (not main thread!)")
        import traceback
        traceback.print_stack(limit=8)

# Import CrossHubDeal for type checking
try:
    from scanning.scanner_crosshub import CrossHubDeal
    CROSSHUB_AVAILABLE = True
except ImportError:
    CrossHubDeal = None
    CROSSHUB_AVAILABLE = False

# Risk thresholds (used by deals tab for classification)
MAX_DAYS_TO_SELL = 2.0
CAPITAL_THRESHOLD = 0.5


# =============================================================================
# Item-category keyword filter — shared between the top filter bar (deals
# tabs) and the Demand/Restock tab's inline checkboxes. Pure function so
# both call sites point at the same keyword lists.
# =============================================================================
_APPAREL_KEYWORDS = (
    "pants", "jacket", "coat", "shirt", "boots", "gloves",
    "goggles", "dress", "skirt", "vest", "suit", "headwear",
    "monocle", "glasses", "eyewear", " top", "mittens",
    "men's", "women's", "male", "female",
)
_SKILLBOOK_KEYWORDS = ("skill", "certificate", "training")


def passes_category_filters_for_name(
    name: str,
    *,
    show_blueprints: bool,
    show_skins: bool,
    show_skillbooks: bool,
    show_apparel: bool,
    show_limited: bool,
    show_unlimited: bool,
) -> bool:
    """Return True if ``name`` survives the category toggles.

    ``True`` for a Show flag means "let items of that category through";
    ``False`` filters them out. Caller can lowercase or not — done here.
    """
    n = name.lower()
    if not show_blueprints and n.endswith("blueprint"):
        return False
    if not show_skins and (" skin" in n or n.startswith("skin")):
        return False
    if not show_skillbooks and any(kw in n for kw in _SKILLBOOK_KEYWORDS):
        return False
    if not show_apparel and any(kw in n for kw in _APPAREL_KEYWORDS):
        return False
    if not show_limited and "limited" in n:
        return False
    if not show_unlimited and "unlimited" in n:
        return False
    return True

# File for persistent ignore list - use centralized data directory
IGNORE_FILE = str(get_data_dir() / "ignored_items.json")


class FilterManager:
    """Manages filter controls, ignore lists, and buy flags."""

    def __init__(self, root: tk.Tk):
        self.root = root
        
        # Selected hub (updated by gui_main when dropdown changes)
        self.selected_hub = DEFAULT_HUB
        
        # Default filter values
        self.default_min_profit = 10_000
        self.default_min_total = MIN_TOTAL_PROFIT
        self.default_max_cost = 100_000_000  # 100M (also wallet size)
        self.default_min_margin = 12
        self.default_min_volume = MIN_DAILY_VOLUME

        # Ignore lists: persistent (always) and session-only (this session)
        self.ignored_type_ids: set[int] = self._load_ignore_list()  # Always ignored
        self.session_ignored_type_ids: set[int] = set()  # Session only, cleared on restart
        self.buy_flagged_type_ids: set[int] = set()
        self.buy_flagged_deals: dict[int, Deal] = {}

        # Variables for UI controls (initialized in create_widgets)
        self.min_profit_var = None
        self.min_total_var = None
        self.max_cost_var = None
        self.min_margin_var = None
        self.min_volume_var = None
        self.est_sell_pct_var = None
        
        # Calculator variables
        self.calc_buy_var = None
        self.calc_sell_var = None
        self.calc_be_var = None
        self.calc_profit_var = None
        self.calc_margin_var = None
        
        # Category toggles
        self.show_blueprints_var = None
        self.show_skins_var = None
        self.show_skillbooks_var = None
        self.show_apparel_var = None
        self.show_limited_var = None
        self.show_unlimited_var = None
        self.show_ignored_var = None
        self.hub_only_var = None
    
    def set_selected_hub(self, hub_key: str):
        """Update the selected hub for filtering."""
        self.selected_hub = hub_key

    def _load_ignore_list(self) -> set[int]:
        """Load ignored item IDs from file."""
        try:
            if os.path.exists(IGNORE_FILE):
                with open(IGNORE_FILE, "r") as f:
                    data = json.load(f)
                    return set(data.get("ignored_type_ids", []))
        except Exception as e:
            print(f"Error loading ignore list: {e}")
        return set()

    def _save_ignore_list(self):
        """Save ignored item IDs to file."""
        try:
            with open(IGNORE_FILE, "w") as f:
                json.dump({"ignored_type_ids": list(self.ignored_type_ids)}, f)
        except Exception as e:
            print(f"Error saving ignore list: {e}")

    def create_widgets(self, parent: tk.Widget):
        """Create filter control widgets."""
        filter_container = ttk.Frame(parent, padding=(10, 5))
        filter_container.pack(fill=tk.X)
        
        self._create_numeric_filters(filter_container)
        self._create_category_toggles(filter_container)

    def _create_numeric_filters(self, parent: ttk.Frame):
        """Create numeric filter entry fields."""
        filter_row1 = ttk.Frame(parent)
        filter_row1.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(filter_row1, text="Filters:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 10))

        # Min Profit Per Unit
        ttk.Label(filter_row1, text="Min Profit/Unit:").pack(side=tk.LEFT)
        self.min_profit_var = tk.StringVar(value=str(self.default_min_profit))
        ttk.Entry(filter_row1, textvariable=self.min_profit_var, width=12).pack(side=tk.LEFT, padx=(2, 15))

        # Min Total Profit
        ttk.Label(filter_row1, text="Min Total Profit:").pack(side=tk.LEFT)
        self.min_total_var = tk.StringVar(value=str(self.default_min_total))
        ttk.Entry(filter_row1, textvariable=self.min_total_var, width=12).pack(side=tk.LEFT, padx=(2, 15))

        # Max Cost / Wallet
        ttk.Label(filter_row1, text="Max Cost/Wallet:").pack(side=tk.LEFT)
        self.max_cost_var = tk.StringVar(value=str(self.default_max_cost))
        ttk.Entry(filter_row1, textvariable=self.max_cost_var, width=12).pack(side=tk.LEFT, padx=(2, 15))

        # Min Margin %
        ttk.Label(filter_row1, text="Min Margin %:").pack(side=tk.LEFT)
        self.min_margin_var = tk.StringVar(value=str(self.default_min_margin))
        ttk.Entry(filter_row1, textvariable=self.min_margin_var, width=6).pack(side=tk.LEFT, padx=(2, 15))

        # Min Daily Volume
        ttk.Label(filter_row1, text="Min Vol:").pack(side=tk.LEFT)
        self.min_volume_var = tk.StringVar(value=str(self.default_min_volume))
        ttk.Entry(filter_row1, textvariable=self.min_volume_var, width=6).pack(side=tk.LEFT, padx=(2, 15))

        # Est Sell % (adjusts displayed profit/unit assuming you sell at X% of ceiling)
        ttk.Label(filter_row1, text="Est Sell %:").pack(side=tk.LEFT)
        self.est_sell_pct_var = tk.StringVar(value="100")
        self.est_sell_pct_entry = ttk.Entry(filter_row1, textvariable=self.est_sell_pct_var, width=5)
        self.est_sell_pct_entry.pack(side=tk.LEFT, padx=(2, 15))
        self.est_sell_pct_entry.bind("<KeyRelease>", self._on_est_sell_pct_changed)
        
        # Callback for refreshing display when est_sell_pct changes
        self._est_sell_pct_callback = None

    def _create_category_toggles(self, parent: ttk.Frame):
        """Create category filter checkboxes and calculator widget."""
        # Category toggles row
        filter_row2 = ttk.Frame(parent)
        filter_row2.pack(fill=tk.X)

        ttk.Label(filter_row2, text="Show:", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 5))

        self.show_blueprints_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_row2, text="Blueprints", variable=self.show_blueprints_var).pack(side=tk.LEFT, padx=2)

        self.show_skins_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_row2, text="SKINs", variable=self.show_skins_var).pack(side=tk.LEFT, padx=2)

        self.show_skillbooks_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_row2, text="Skillbooks", variable=self.show_skillbooks_var).pack(side=tk.LEFT, padx=2)

        self.show_apparel_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_row2, text="Apparel", variable=self.show_apparel_var).pack(side=tk.LEFT, padx=2)

        self.show_limited_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_row2, text="Limited", variable=self.show_limited_var).pack(side=tk.LEFT, padx=2)

        self.show_unlimited_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_row2, text="Unlimited", variable=self.show_unlimited_var).pack(side=tk.LEFT, padx=2)

        self.show_ignored_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_row2, text="Ignored", variable=self.show_ignored_var).pack(side=tk.LEFT, padx=2)

        self.hub_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(filter_row2, text="Hub Only", variable=self.hub_only_var).pack(side=tk.LEFT, padx=2)

        # Separator
        ttk.Label(filter_row2, text="|").pack(side=tk.LEFT, padx=(10, 10))

        # Manual break-even calculator (inline after Hub Only)
        ttk.Label(filter_row2, text="Calc:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        
        ttk.Label(filter_row2, text="Buy:").pack(side=tk.LEFT)
        self.calc_buy_var = tk.StringVar()
        calc_buy_entry = ttk.Entry(filter_row2, textvariable=self.calc_buy_var, width=10)
        calc_buy_entry.pack(side=tk.LEFT, padx=(2, 5))
        calc_buy_entry.bind("<KeyRelease>", self._update_break_even_calc)
        
        ttk.Label(filter_row2, text="Sell:").pack(side=tk.LEFT)
        self.calc_sell_var = tk.StringVar()
        calc_sell_entry = ttk.Entry(filter_row2, textvariable=self.calc_sell_var, width=10)
        calc_sell_entry.pack(side=tk.LEFT, padx=(2, 5))
        calc_sell_entry.bind("<KeyRelease>", self._update_break_even_calc)
        
        ttk.Label(filter_row2, text="->").pack(side=tk.LEFT, padx=(3, 3))
        
        ttk.Label(filter_row2, text="BE:").pack(side=tk.LEFT)
        self.calc_be_var = tk.StringVar(value="-")
        ttk.Label(filter_row2, textvariable=self.calc_be_var, width=10, font=("Consolas", 9)).pack(side=tk.LEFT, padx=(2, 5))
        
        ttk.Label(filter_row2, text="Profit:").pack(side=tk.LEFT)
        self.calc_profit_var = tk.StringVar(value="-")
        ttk.Label(filter_row2, textvariable=self.calc_profit_var, width=10, font=("Consolas", 9)).pack(side=tk.LEFT, padx=(2, 5))
        
        ttk.Label(filter_row2, text="Margin:").pack(side=tk.LEFT)
        self.calc_margin_var = tk.StringVar(value="-")
        ttk.Label(filter_row2, textvariable=self.calc_margin_var, width=6, font=("Consolas", 9)).pack(side=tk.LEFT, padx=(2, 5))

    def get_filter_values(self) -> tuple:
        """Get current filter values from entry fields."""
        _check_thread("FilterManager.get_filter_values")
        try:
            min_profit = float(self.min_profit_var.get().replace(",", ""))
        except ValueError:
            min_profit = self.default_min_profit

        try:
            min_total = float(self.min_total_var.get().replace(",", ""))
        except ValueError:
            min_total = self.default_min_total

        try:
            max_cost = float(self.max_cost_var.get().replace(",", ""))
        except ValueError:
            max_cost = self.default_max_cost

        try:
            min_margin = float(self.min_margin_var.get().replace(",", ""))
        except ValueError:
            min_margin = self.default_min_margin

        try:
            min_volume = float(self.min_volume_var.get().replace(",", ""))
        except ValueError:
            min_volume = self.default_min_volume

        return min_profit, min_total, max_cost, min_margin, min_volume

    def get_wallet_size(self) -> float:
        """Get wallet size from Max Cost field for risk calculations."""
        _check_thread("FilterManager.get_wallet_size")
        try:
            return float(self.max_cost_var.get().replace(",", ""))
        except ValueError:
            return self.default_max_cost

    def get_est_sell_pct(self) -> float:
        """Get estimated sell percentage (0-100) for profit display adjustment."""
        _check_thread("FilterManager.get_est_sell_pct")
        try:
            pct = float(self.est_sell_pct_var.get().replace(",", ""))
            return max(0, min(100, pct))  # Clamp to 0-100
        except (ValueError, AttributeError):
            return 100.0

    def set_est_sell_pct_callback(self, callback):
        """Set callback to be called when est_sell_pct changes."""
        self._est_sell_pct_callback = callback

    def _on_est_sell_pct_changed(self, event=None):
        """Handle est_sell_pct value change - refresh display."""
        if self._est_sell_pct_callback:
            self._est_sell_pct_callback()

    def _update_break_even_calc(self, event=None):
        """Update the manual break-even calculator display."""
        from core.calculate import calculate_break_even, calculate_profit_per_unit, calculate_margin_percent, DEFAULT_SKILLS
        
        try:
            buy_str = self.calc_buy_var.get().replace(",", "")
            sell_str = self.calc_sell_var.get().replace(",", "")
            
            if not buy_str or not sell_str:
                self.calc_be_var.set("-")
                self.calc_profit_var.set("-")
                self.calc_margin_var.set("-")
                return
            
            buy_price = float(buy_str)
            sell_price = float(sell_str)
            
            if buy_price <= 0 or sell_price <= 0:
                self.calc_be_var.set("-")
                self.calc_profit_var.set("-")
                self.calc_margin_var.set("-")
                return
            
            # Use skills from tracking manager if available, otherwise defaults
            skills = DEFAULT_SKILLS
            if hasattr(self, '_skills_getter') and self._skills_getter:
                skills = self._skills_getter()
            
            be = calculate_break_even(buy_price, 1, 0.0, skills)
            profit = calculate_profit_per_unit(buy_price, sell_price, skills)
            margin = calculate_margin_percent(buy_price, sell_price, skills)
            
            self.calc_be_var.set(f"{be:,.0f}")
            self.calc_profit_var.set(f"{profit:,.0f}")
            self.calc_margin_var.set(f"{margin:.1f}%")
            
        except ValueError:
            self.calc_be_var.set("-")
            self.calc_profit_var.set("-")
            self.calc_margin_var.set("-")

    def set_skills_getter(self, getter):
        """Set a function to get current trading skills for calculations."""
        self._skills_getter = getter

    def should_show_deal(self, deal: Deal) -> bool:
        """Check if deal should be shown based on category filters."""
        _check_thread("FilterManager.should_show_deal")
        name = deal.name.lower()

        # Hub Only filter - match by id, never by display name: custom
        # stations register `name` as the full ESI station name ("Ashab IV -
        # Moon 1 - ..."), which never equals a system name, so name compares
        # silently hid every deal at custom stations/structures.
        # For CrossHubDeal, check buy station (where you pick up items)
        # For regular Deal, check system_id (where the deal is)
        if self.hub_only_var.get():
            hub_config = get_hub_config(self.selected_hub)

            # Check if this is a CrossHubDeal
            if CROSSHUB_AVAILABLE and CrossHubDeal and isinstance(deal, CrossHubDeal):
                # deal.buy_station holds the hub KEY (scanner_crosshub passes
                # buy_station_key through), so compare keys directly
                if deal.buy_station != self.selected_hub:
                    return False
            else:
                hub_system_id = hub_config.get("system_id")
                if hub_system_id:
                    if deal.system_id != hub_system_id:
                        return False
                elif deal.system_name.lower() != hub_config["name"].lower():
                    # Legacy custom-station entry without system_id
                    return False

        return self.passes_category_filters(name)

    def passes_category_filters(self, name: str) -> bool:
        """Return True if the item name passes this manager's category toggles."""
        return passes_category_filters_for_name(
            name,
            show_blueprints=self.show_blueprints_var.get(),
            show_skins=self.show_skins_var.get(),
            show_skillbooks=self.show_skillbooks_var.get(),
            show_apparel=self.show_apparel_var.get(),
            show_limited=self.show_limited_var.get(),
            show_unlimited=self.show_unlimited_var.get(),
        )

    def is_high_risk(self, deal: Deal) -> bool:
        """
        Determine if a deal is high risk based on:
        1. Slow exit: days_to_sell > 2.0
        2. Capital hog: total_cost > 50% of wallet
        3. Low liquidity: conservative daily volume < 5
        4. Sporadic trading: traded fewer than 15 of last 30 days
        5. Price crashing: 7d avg is 8%+ below 30d avg
        """
        wallet_size = self.get_wallet_size()
        
        # Conservative velocity = lower of 7d or 30d
        safe_velocity = min(deal.avg_volume_7d, deal.avg_volume_30d) if deal.avg_volume_7d > 0 and deal.avg_volume_30d > 0 else max(deal.avg_volume_7d, deal.avg_volume_30d)
        
        is_capital_hog = deal.total_cost > (wallet_size * CAPITAL_THRESHOLD)
        is_slow_exit = deal.days_to_sell > MAX_DAYS_TO_SELL
        is_low_volume = safe_velocity < 5
        is_sporadic = deal.trading_days_30d < 15
        
        # Price crash detection
        is_price_crashing = False
        if deal.avg_price_7d > 0 and deal.avg_price_30d > 0:
            pct_change = ((deal.avg_price_7d - deal.avg_price_30d) / deal.avg_price_30d) * 100
            is_price_crashing = pct_change < -8
        
        return is_capital_hog or is_slow_exit or is_low_volume or is_sporadic or is_price_crashing

    # === Buy Flag Management ===

    def mark_for_buy(self, type_id: int, deal: Deal):
        """Mark an item for buying."""
        self.buy_flagged_type_ids.add(type_id)
        self.buy_flagged_deals[type_id] = deal

    def unmark_buy(self, type_id: int):
        """Remove buy flag from an item."""
        self.buy_flagged_type_ids.discard(type_id)
        self.buy_flagged_deals.pop(type_id, None)

    def is_buy_flagged(self, type_id: int) -> bool:
        """Check if item is flagged for buying."""
        return type_id in self.buy_flagged_type_ids

    # === Ignore List Management ===

    def ignore_item_always(self, type_id: int):
        """Add item to permanent ignore list (persists across sessions)."""
        self.ignored_type_ids.add(type_id)
        # Remove from session list if present (promoted to always)
        self.session_ignored_type_ids.discard(type_id)
        self._save_ignore_list()

    def ignore_item_session(self, type_id: int):
        """Add item to session-only ignore list (cleared on restart)."""
        # Don't add if already in permanent list
        if type_id not in self.ignored_type_ids:
            self.session_ignored_type_ids.add(type_id)

    def ignore_item(self, type_id: int):
        """Add item to permanent ignore list. Legacy method for compatibility."""
        self.ignore_item_always(type_id)

    def unignore_item(self, type_id: int):
        """Remove item from both ignore lists."""
        removed_from = []
        if type_id in self.ignored_type_ids:
            self.ignored_type_ids.discard(type_id)
            self._save_ignore_list()
            removed_from.append("always")
        if type_id in self.session_ignored_type_ids:
            self.session_ignored_type_ids.discard(type_id)
            removed_from.append("session")
        return removed_from

    def is_ignored(self, type_id: int) -> bool:
        """Check if item is ignored (either session or always)."""
        return type_id in self.ignored_type_ids or type_id in self.session_ignored_type_ids

    def get_ignore_type(self, type_id: int) -> str | None:
        """Get the ignore type for an item: 'always', 'session', or None."""
        if type_id in self.ignored_type_ids:
            return "always"
        if type_id in self.session_ignored_type_ids:
            return "session"
        return None

    def clear_session_ignores(self):
        """Clear all session-only ignores."""
        self.session_ignored_type_ids.clear()

    def show_ignored(self) -> bool:
        """Check if ignored items should be shown."""
        return self.show_ignored_var.get()
