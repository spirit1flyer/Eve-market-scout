"""Stock Market toolbar action handlers - extracted as mixin.

Contains all toolbar button handlers and their supporting methods.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import asyncio
import threading
from typing import Dict, List, TYPE_CHECKING

from core.tk_queue import submit
from core.config import get_hub_config
from gui.stockmarket.gui_stockmarket_dialogs import AddStockItemDialog
from gui.stockmarket.gui_stockmarket_settings import StockMarketSettings, StockMarketSettingsDialog
from gui.gui_window_utils import fit_window

if TYPE_CHECKING:
    from gui.stockmarket.gui_stockmarket_hub import StockMarketHubPanel


class StockMarketActionsMixin:
    """Mixin providing toolbar action handlers for StockMarketTab.
    
    Expects the following attributes on self:
        - frame: ttk.Frame
        - settings: StockMarketSettings
        - profiles: ProfileManager
        - downloader: ArchiveDownloader
        - hub_panels: Dict[str, StockMarketHubPanel]
        - get_client: Callable
        - set_status: Callable
        - percentile_label: ttk.Label
        - sde_label: ttk.Label
    """
    
    # =========================================================================
    # Add Item
    # =========================================================================
    
    def _on_add_item(self):
        """Add item to current hub."""
        panel = self._get_current_hub_panel()
        if not panel:
            return
        
        # Get HoldingsManager for duplicate check (may be None if holdings tab not yet created)
        holdings = None
        if panel.holdings_panel is not None:
            holdings = panel.holdings_panel.holdings
        
        AddStockItemDialog(
            self.frame,
            self.get_client,
            lambda type_id, name, region_id, station_id: panel.add_item(type_id, name),
            holdings,
            self.profiles
        )
    
    # =========================================================================
    # Archive Download
    # =========================================================================
    
    # =========================================================================
    # SDE Download
    # =========================================================================
    
    def _on_download_sde(self):
        """Download SDE (Static Data Export) for item names and filtering."""
        from sde.sde_manager import get_sde_manager
        
        sde = get_sde_manager()
        
        # Check if already available
        if sde.is_available():
            age = sde.get_age_days()
            info = sde.get_version_info()
            count = info.get("record_count", "?")
            
            result = messagebox.askyesno(
                "SDE Already Downloaded",
                f"SDE database already exists:\n"
                f"  Items: {count:,}\n"
                f"  Age: {age} days\n\n"
                f"Re-download to update?"
            )
            if not result:
                return
        
        # Show progress dialog
        progress_win = tk.Toplevel(self.frame)
        progress_win.title("Downloading SDE")
        progress_win.transient(self.frame)
        progress_win.grab_set()

        frame = ttk.Frame(progress_win, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        status_label = ttk.Label(frame, text="Starting download...")
        status_label.pack(pady=(0, 10))

        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(frame, variable=progress_var, length=300, mode="determinate")
        progress_bar.pack()
        fit_window(progress_win, min_width=350)
        
        def update_progress(msg: str, pct: int):
            submit(lambda: status_label.configure(text=msg))
            submit(lambda: progress_var.set(pct))
        
        def do_download():
            async def download():
                return await sde.download_and_build(progress_callback=update_progress)
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                success = loop.run_until_complete(download())
            finally:
                loop.close()
            
            submit(lambda: self._on_sde_download_complete(progress_win, success))
        
        threading.Thread(target=do_download, daemon=True).start()
    
    def _on_sde_download_complete(self, progress_win: tk.Toplevel, success: bool):
        """Called when SDE download completes."""
        progress_win.destroy()
        
        if success:
            from sde.sde_manager import get_sde_manager
            sde = get_sde_manager()
            info = sde.get_version_info()
            count = info.get("record_count", 0)
            self.set_status(f"SDE downloaded: {count:,} items")
            messagebox.showinfo("SDE Downloaded", f"Successfully downloaded {count:,} item types.")
        else:
            self.set_status("SDE download failed")
            messagebox.showerror("Download Failed", "Failed to download SDE. Check your internet connection.")
        
        self._update_sde_status()
    
    # =========================================================================
    # Refresh Prices
    # =========================================================================
    
    def _on_refresh_prices(self):
        """Refresh live prices for current hub tab via independent ESI fetch."""
        hub_key = self._get_current_hub_key()
        if not hub_key:
            self.set_status("No hub selected")
            return
        
        # Validation checks
        if not self._validate_refresh_prerequisites(hub_key):
            return
        
        if not self.get_client:
            self.set_status("No ESI client available")
            return
        
        config = get_hub_config(hub_key)
        region_id = config["region_id"]
        
        self.set_status(f"Fetching {config['name']} market orders...")
        
        def fetch():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                client = self.get_client()
                if not client:
                    submit(lambda: self.set_status("No client available"))
                    return
                
                async def do_fetch():
                    client.ensure_session()
                    client.reset_for_new_loop()
                    # Manual refresh — bypass cache, always hit ESI
                    return await client.get_market_orders(region_id, force_refresh=True)
                
                orders = loop.run_until_complete(do_fetch())
                
                submit(lambda: self._on_region_orders_fetched(hub_key, orders, region_id))
                
            except Exception as e:
                print(f"[StockMarket] Price fetch error: {e}")
                err_msg = str(e)
                submit(lambda msg=err_msg: self.set_status(f"Error: {msg}"))
            finally:
                loop.close()
        
        threading.Thread(target=fetch, daemon=True).start()
    
    def _validate_refresh_prerequisites(self, hub_key: str) -> bool:
        """Validate prerequisites for price refresh. Returns True if OK."""
        
        # Check market_history.db exists and has data
        try:
            from history.market_history import get_market_history_db
            db = get_market_history_db()
            stats = db.get_stats()
            if stats.get('row_count', 0) == 0:
                messagebox.showwarning(
                    "Setup Required",
                    "Market history database is empty.\n\n"
                    "Run a Scanner scan first to download market data."
                )
                return False
        except Exception as e:
            messagebox.showwarning("Error", f"Cannot access market history: {e}")
            return False
        
        # Check if profiles are building
        if getattr(self, '_profiles_building', False):
            self.set_status("Profiles still building, please wait...")
            return False
        
        # Check profiles exist for this hub - build automatically if missing
        config = get_hub_config(hub_key)
        region_id = config["region_id"]
        profiles = self.profiles.get_profiles_for_region(region_id)
        
        if not profiles:
            # Trigger automatic profile building
            self._build_profiles_for_hub(hub_key, region_id, config["name"])
            return False  # Can't refresh yet, building in progress
        
        return True
    
    def _build_profiles_for_hub(self, hub_key: str, region_id: int, hub_name: str):
        """Build profiles for a hub from market history database."""
        self._profiles_building = True
        self.set_status(f"Building profiles for {hub_name}...")

        panel = self.hub_panels.get(hub_key)
        if panel:
            submit(lambda: panel._show_filter_overlay(
                f"Building profiles for {hub_name}…", total=0
            ))

        def build():
            try:
                from history.market_history import get_market_history_db
                db = get_market_history_db()

                region_item_count = len(db.get_items_in_region(region_id))
                if region_item_count == 0:
                    if panel:
                        submit(lambda: panel._hide_filter_overlay())
                    submit(lambda: self.set_status(
                        f"No history data for {hub_name} — import the everef archive first"
                    ))
                    submit(lambda: setattr(self, '_profiles_building', False))
                    return

                print(f"[StockMarket-{hub_key}] === PHASE: Profile Build === "
                      f"(region {region_id}, {region_item_count} items)")

                if panel:
                    submit(lambda c=region_item_count: panel._show_filter_overlay(
                        f"Building profiles: 0/{c:,}", total=c
                    ))

                import time as _time
                _build_start = _time.time()

                def progress(msg: str, current: int, total: int):
                    if current % 200 == 0 and total > 0:
                        elapsed = _time.time() - _build_start
                        pct = current * 100 // total
                        rate = current / elapsed if elapsed > 0 else 0
                        eta = int((total - current) / rate) if rate > 0 else 0
                        label = (f"Building profiles: "
                                 f"{current:,}/{total:,} ({pct}%) ~{eta}s left")
                        if panel:
                            submit(lambda s=label, c=current:
                                   panel._update_filter_overlay(c, s))
                        submit(lambda s=label: self.set_status(
                            f"[{hub_name}] {s}"
                        ))

                success, failed = self.profiles.extract_all_from_db(
                    region_id=region_id,
                    market_db=db,
                    progress_callback=progress
                )

                elapsed = _time.time() - _build_start
                print(f"[StockMarket-{hub_key}] Profile build complete: "
                      f"{success} ok, {failed} failed in {elapsed:.1f}s")

                submit(lambda: self._on_profiles_built(hub_key, success, failed))

            except Exception as e:
                print(f"[StockMarket] Profile build error: {e}")
                if panel:
                    submit(lambda: panel._hide_filter_overlay())
                submit(lambda: self._on_profiles_build_error(str(e)))

        threading.Thread(target=build, daemon=True).start()

    def _on_profiles_built(self, hub_key: str, success: int, failed: int):
        """Called when automatic profile building completes."""
        self._profiles_building = False

        panel = self.hub_panels.get(hub_key)
        config = get_hub_config(hub_key)
        hub_name = config["name"] if config else hub_key

        if panel:
            panel._hide_filter_overlay()

        self.set_status(f"[{hub_name}] Built {success:,} profiles "
                        f"({failed} failed) — refreshing…")

        if panel:
            panel.refresh_display_async()

    def _on_profiles_build_error(self, error: str):
        """Called when profile building fails."""
        self._profiles_building = False
        self.set_status(f"Profile build failed: {error}")
    
    def _on_region_orders_fetched(self, hub_key: str, orders: list, region_id: int):
        """Handle fetched region orders - update Stock Market panel.
        
        Note: get_market_orders() already populates the shared order cache
        with the parsed Expires header, so no manual cache write is needed
        here. Writing it again would lose the expires timestamp and break
        the scanner countdown sync.
        """
        if not orders:
            self.set_status("No orders received")
            return
        
        # Update Stock Market hub panel
        self.update_from_local_orders(orders, region_id)
        
        # Count sell orders for status
        sell_count = sum(1 for o in orders if not o.get("is_buy_order"))
        config = get_hub_config(hub_key)
        self.set_status(f"Updated {config['name']}: {sell_count} sell orders")
    
    def _on_prices_fetched(self, prices: Dict[int, float]):
        """Called when price fetch completes (legacy, kept for compatibility)."""
        panel = self._get_current_hub_panel()
        if panel:
            panel.update_live_prices(prices)
        self.set_status(f"Updated {len(prices)} prices")
    
    # =========================================================================
    # Reset Profiles
    # =========================================================================
    
    def _on_reset_profiles(self):
        """Reset profiles for current hub only."""
        hub_key = self._get_current_hub_key()
        if not hub_key:
            self.set_status("No hub selected")
            return
        
        config = get_hub_config(hub_key)
        region_id = config["region_id"]
        hub_name = config["name"]
        
        result = messagebox.askyesno(
            "Reset Profiles",
            f"Clear all cached profiles for {hub_name}?\n\n"
            "Profiles will rebuild automatically when needed."
        )
        
        if not result:
            return
        
        self.profiles.clear_region_profiles(region_id)
        self._build_profiles_for_hub(hub_key, region_id, hub_name)

    # =========================================================================
    # Settings
    # =========================================================================
    
    def _on_settings(self):
        """Open settings dialog."""
        StockMarketSettingsDialog(
            self.frame,
            self.settings,
            self._on_settings_saved
        )
    
    def _on_settings_saved(self, new_settings: StockMarketSettings, needs_rebuild: bool):
        """Called when settings are saved."""
        self.settings = new_settings
        
        # Update profile manager
        self.profiles.set_percentiles(new_settings.buy_percentile, new_settings.sell_percentile)
        self.profiles.archive_path = new_settings.get_archive_path()
        self.downloader.archive_path = new_settings.get_archive_path()
        
        # Update all hub panels
        for panel in self.hub_panels.values():
            panel.update_settings(new_settings)
        
        # Update UI
        self.percentile_label.configure(
            text=f"P{new_settings.buy_percentile}/P{new_settings.sell_percentile}"
        )
        
        if needs_rebuild:
            # Auto-rebuild current hub's profiles
            hub_key = self._get_current_hub_key()
            if hub_key:
                config = get_hub_config(hub_key)
                region_id = config["region_id"]
                
                # Clear old profiles and rebuild from archive at new percentiles
                self.profiles.clear_region_profiles(region_id)
                self._build_profiles_for_hub(hub_key, region_id, config["name"])
            else:
                self.set_status("Settings saved - profiles will rebuild on next access")
        else:
            self.set_status("Settings saved")
