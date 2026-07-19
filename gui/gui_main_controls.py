"""Control bar mixin for MarketScoutGUI.

Extracts control bar creation and toggle handlers from gui_main.py.

Expected parent attributes:
    root: tk.Tk - main window
    auto_refresh_enabled: bool - auto-refresh state
    sound_enabled: bool - sound alert state
    buy_station: str - current buy station key
    sell_station: str - current sell station key
    is_scanning: bool - scan in progress flag
    next_refresh_seconds: int - ESI cache timing
    auto_refresh_job: Optional[int] - scheduled refresh job ID
    
    Methods expected:
    _start_scan(is_auto: bool) - trigger market scan
    _update_jita_status() - refresh Jita cache label
    _auto_refresh() - auto-refresh callback
"""

import tkinter as tk
from tkinter import ttk

from core.config import AUTO_REFRESH_INTERVAL, get_enabled_hubs, get_hub_config
from core.sound_manager import open_data_folder
from gui.gui_window_utils import fit_window


class MainControlsMixin:
    """Mixin providing control bar creation and toggle handlers."""

    def _create_control_bar(self):
        """Create top control bar with buttons and status (two rows)."""
        outer = ttk.Frame(self.root, padding=(10, 5, 10, 0))
        outer.pack(fill=tk.X)

        # ── ROW 1: station selectors, scan/jita, toggles, status ──────────────
        row1 = ttk.Frame(outer)
        row1.pack(fill=tk.X)

        # Right-side items must be packed first so pack(LEFT) doesn't crowd them.

        # === Character Display (right) ===
        char_frame = ttk.Frame(row1)
        char_frame.pack(side=tk.RIGHT, padx=(10, 0))

        self.seller_label = ttk.Label(
            char_frame, text="Seller: -", font=("Segoe UI", 8), foreground="darkgreen"
        )
        self.seller_label.pack(side=tk.LEFT, padx=(0, 5))

        self.buyer_label = ttk.Label(
            char_frame, text="Buyer: -", font=("Segoe UI", 8), foreground="darkblue"
        )
        self.buyer_label.pack(side=tk.LEFT, padx=(0, 5))

        # Status label (right)
        self.status_label = ttk.Label(row1, text="Ready", font=("Segoe UI", 9))
        self.status_label.pack(side=tk.RIGHT)

        # === Station Selection (Buy / Sell) ===
        station_frame = ttk.Frame(row1)
        station_frame.pack(side=tk.LEFT)

        ttk.Label(station_frame, text="Buy:", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 2))

        hub_choices = get_enabled_hubs()
        hub_display_names = [name for key, name in hub_choices]
        self.hub_keys = [key for key, name in hub_choices]

        self.buy_station_var = tk.StringVar()
        self.buy_station_dropdown = ttk.Combobox(
            station_frame,
            textvariable=self.buy_station_var,
            values=hub_display_names,
            state="readonly",
            width=10,
        )
        for key, name in hub_choices:
            if key == self.buy_station:
                self.buy_station_var.set(name)
                break
        self.buy_station_dropdown.pack(side=tk.LEFT, padx=(0, 5))
        self.buy_station_dropdown.bind("<<ComboboxSelected>>", self._on_buy_station_changed)

        ttk.Label(station_frame, text="Sell:", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(5, 2))

        self.sell_station_var = tk.StringVar()
        self.sell_station_dropdown = ttk.Combobox(
            station_frame,
            textvariable=self.sell_station_var,
            values=hub_display_names,
            state="readonly",
            width=10,
        )
        for key, name in hub_choices:
            if key == self.sell_station:
                self.sell_station_var.set(name)
                break
        self.sell_station_dropdown.pack(side=tk.LEFT, padx=(0, 10))
        self.sell_station_dropdown.bind("<<ComboboxSelected>>", self._on_sell_station_changed)

        self.mode_label = ttk.Label(
            station_frame, text="[Same Station]", font=("Segoe UI", 8), foreground="gray"
        )
        self.mode_label.pack(side=tk.LEFT, padx=(0, 10))

        # Scan / Jita buttons
        self.scan_btn = ttk.Button(row1, text="Scan Market", command=self._start_scan)
        self.scan_btn.pack(side=tk.LEFT, padx=5)

        self.jita_btn = ttk.Button(row1, text="Refresh Jita", command=self._refresh_jita)
        self.jita_btn.pack(side=tk.LEFT, padx=5)

        self.jita_status_label = ttk.Label(
            row1, text="Jita: No cache", font=("Segoe UI", 9), foreground="gray"
        )
        self.jita_status_label.pack(side=tk.LEFT, padx=5)

        # Toggles
        self.auto_refresh_var = tk.BooleanVar(value=self.auto_refresh_enabled)
        self.auto_refresh_cb = ttk.Checkbutton(
            row1, text="Auto-refresh",
            variable=self.auto_refresh_var, command=self._toggle_auto_refresh
        )
        self.auto_refresh_cb.pack(side=tk.LEFT, padx=10)

        self.sound_var = tk.BooleanVar(value=self.sound_enabled)
        self.sound_cb = ttk.Checkbutton(
            row1, text="Sound alerts",
            variable=self.sound_var, command=self._toggle_sound
        )
        self.sound_cb.pack(side=tk.LEFT, padx=5)

        # Countdown
        self.countdown_label = ttk.Label(
            row1, text="", font=("Segoe UI", 9), foreground="gray"
        )
        self.countdown_label.pack(side=tk.LEFT, padx=10)

        # ── ROW 2: utility buttons ─────────────────────────────────────────────
        row2 = ttk.Frame(outer)
        row2.pack(fill=tk.X, pady=(2, 4))

        # Stock Market master on/off switch. Lives here (not on the SM tab's
        # own toolbar) because that toolbar is covered by the cold-start lock
        # overlay during load — this spot stays reachable so the user can turn
        # SM off mid-load. Initial value + sync callback are wired in gui_main
        # once stock_market_tab exists (it's created after the control bar).
        self.sm_master_var = tk.BooleanVar(value=True)
        self.sm_master_cb = ttk.Checkbutton(
            row2, text="Stock Market",
            variable=self.sm_master_var, command=self._toggle_stock_market_master
        )
        self.sm_master_cb.pack(side=tk.LEFT, padx=(0, 10))

        self.data_folder_btn = ttk.Button(
            row2, text="Data Folder", command=self._open_data_folder, width=10
        )
        self.data_folder_btn.pack(side=tk.LEFT, padx=5)

        self.add_station_btn = ttk.Button(
            row2, text="Add Station", command=self._on_add_station, width=11
        )
        self.add_station_btn.pack(side=tk.LEFT, padx=5)

        self.find_structures_btn = ttk.Button(
            row2, text="Find Structures", command=self._on_find_structures, width=14
        )
        self.find_structures_btn.pack(side=tk.LEFT, padx=5)

        self.browse_orders_btn = ttk.Button(
            row2, text="Browse Orders", command=self._on_browse_orders, width=13
        )
        self.browse_orders_btn.pack(side=tk.LEFT, padx=5)

    # =========================================================================
    # TOGGLE HANDLERS
    # =========================================================================

    def _refresh_jita(self):
        """Set flag to refresh Jita cache on next scan and trigger scan."""
        self.force_jita_refresh = True
        self._set_status("Will refresh Jita prices on next scan...")
        self._start_scan()

    def _update_jita_status(self):
        """Update Jita cache status label."""
        try:
            client = self.get_client() if self.get_client else None
            if client and client.has_jita_cache():
                age_str = client.get_jita_cache_age()
                self.jita_status_label.configure(
                    text=f"Jita: {age_str}",
                    foreground="green"
                )
            else:
                self.jita_status_label.configure(
                    text="Jita: No cache",
                    foreground="gray"
                )
        except Exception as e:
            print(f"Jita status update error: {e}")
            self.jita_status_label.configure(
                text="Jita: No cache",
                foreground="gray"
            )

    def _toggle_auto_refresh(self):
        """Toggle auto-refresh on/off."""
        self.auto_refresh_enabled = self.auto_refresh_var.get()
        if self.auto_refresh_enabled:
            self._schedule_auto_refresh()
        else:
            if self.auto_refresh_job:
                self.root.after_cancel(self.auto_refresh_job)
                self.auto_refresh_job = None
            self.countdown_label.configure(text="")

    def _toggle_sound(self):
        """Toggle sound alerts on/off."""
        self.sound_enabled = self.sound_var.get()

    def _toggle_stock_market_master(self):
        """Toggle the Stock Market master switch (all SM auto-processing)."""
        if hasattr(self, "stock_market_tab") and self.stock_market_tab:
            self.stock_market_tab.set_enabled(self.sm_master_var.get())

    def _open_data_folder(self):
        """Open the data folder in system file manager."""
        open_data_folder()

    def _on_find_structures(self):
        """Open the discovery dialog for player-owned structures."""
        from gui.gui_discover_structures import DiscoverStructuresDialog
        DiscoverStructuresDialog(
            parent=self.root,
            on_station_added=self._on_custom_station_added,
        )

    def _on_browse_orders(self):
        """Browse raw orders at a registered structure hub (dev/debug)."""
        from tkinter import messagebox
        from core.config import TRADE_HUBS
        from gui.gui_browse_orders import BrowseStructureOrdersDialog

        structures = [
            (k, cfg) for k, cfg in TRADE_HUBS.items()
            if cfg.get("type") == "structure"
        ]
        if not structures:
            messagebox.showinfo(
                "No Structures",
                "No player structures registered. Use Find Structures → Add to Scanner first.",
                parent=self.root,
            )
            return

        if len(structures) == 1:
            _, cfg = structures[0]
            BrowseStructureOrdersDialog(
                parent=self.root,
                structure_id=cfg["station_id"],
                structure_name=cfg.get("name", str(cfg["station_id"])),
            )
            return

        # 2+ structures — small picker
        picker = tk.Toplevel(self.root)
        picker.title("Browse Orders — pick structure")
        picker.transient(self.root)
        picker.grab_set()

        ttk.Label(picker, text="Structure:").pack(anchor=tk.W, padx=10, pady=(10, 2))
        names = [cfg.get("name", str(cfg["station_id"])) for _, cfg in structures]
        var = tk.StringVar(value=names[0])
        ttk.Combobox(picker, textvariable=var, values=names,
                     state="readonly", width=50).pack(padx=10, fill=tk.X)

        def on_open():
            idx = names.index(var.get())
            _, cfg = structures[idx]
            picker.destroy()
            BrowseStructureOrdersDialog(
                parent=self.root,
                structure_id=cfg["station_id"],
                structure_name=cfg.get("name", str(cfg["station_id"])),
            )

        btn_row = ttk.Frame(picker, padding=10)
        btn_row.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btn_row, text="Open", command=on_open).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="Cancel", command=picker.destroy).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        fit_window(picker, min_width=420)

    def _on_add_station(self):
        """Open the Add / Manage Custom Stations dialog."""
        from gui.gui_add_station import AddStationDialog
        AddStationDialog(
            parent=self.root,
            get_client=self.get_client,
            on_station_added=self._on_custom_station_added,
            on_add_to_stockmarket=self._on_custom_station_to_stockmarket,
            on_station_removed=self._on_custom_station_removed,
            on_remove_from_stockmarket=self._on_custom_station_removed_from_sm,
        )

    def _on_custom_station_added(self, hub_key: str):
        self.refresh_station_dropdowns()

    def _on_custom_station_to_stockmarket(self, hub_key: str):
        if hasattr(self, "stock_market_tab"):
            self.stock_market_tab.add_hub_tab(hub_key)

    def _on_custom_station_removed(self, hub_key: str):
        self.refresh_station_dropdowns()

    def _on_custom_station_removed_from_sm(self, hub_key: str):
        if hasattr(self, "stock_market_tab"):
            self.stock_market_tab.remove_hub_tab(hub_key)

    def refresh_station_dropdowns(self):
        """Rebuild buy/sell combobox values to include any new custom stations."""
        from core.config import get_enabled_hubs
        hub_choices = get_enabled_hubs()
        hub_display_names = [name for key, name in hub_choices]
        self.hub_keys = [key for key, name in hub_choices]
        self.buy_station_dropdown.configure(values=hub_display_names)
        self.sell_station_dropdown.configure(values=hub_display_names)

    def _schedule_auto_refresh(self):
        """Schedule the next auto-refresh based on ESI cache expiry."""
        if self.auto_refresh_job:
            self.root.after_cancel(self.auto_refresh_job)
            self.auto_refresh_job = None

        if self.auto_refresh_enabled and not self.is_scanning:
            wait_seconds = self.next_refresh_seconds if self.next_refresh_seconds > 0 else AUTO_REFRESH_INTERVAL
            self._start_countdown(int(wait_seconds))

    def _start_countdown(self, seconds_left):
        """Update countdown display and trigger refresh when done."""
        if not self.auto_refresh_enabled or self.is_scanning:
            self.countdown_label.configure(text="")
            return

        if seconds_left <= 0:
            self.countdown_label.configure(text="Refreshing...")
            self._auto_refresh()
        else:
            sync_text = "ESI sync" if self.next_refresh_seconds > 0 else "Next scan"
            self.countdown_label.configure(text=f"{sync_text}: {seconds_left}s")
            self.auto_refresh_job = self.root.after(1000, lambda s=seconds_left-1: self._start_countdown(s))
