"""Dialog for adding and removing custom NPC stations.

Top section: cascading Region → System → Station dropdowns to add a new station.
Bottom section: list of existing custom stations with per-station remove controls.
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

from core.config import TRADE_HUBS
from core.custom_stations import (
    add_custom_station, get_custom_hub_key, is_custom_hub,
    load_custom_stations, remove_custom_station, update_station_in_stockmarket,
)
from core import station_data
from gui.gui_async import run_async as _run_async
from gui.gui_window_utils import fit_window


class AddStationDialog(tk.Toplevel):
    """Combined add-new + manage-existing dialog for custom NPC stations."""

    def __init__(
        self,
        parent,
        get_client: Callable,
        on_station_added: Callable[[str], None],
        on_add_to_stockmarket: Callable[[str], None],
        on_station_removed: Callable[[str], None],
        on_remove_from_stockmarket: Callable[[str], None],
    ):
        super().__init__(parent)
        self.get_client = get_client
        self.on_station_added = on_station_added
        self.on_add_to_stockmarket = on_add_to_stockmarket
        self.on_station_removed = on_station_removed
        self.on_remove_from_stockmarket = on_remove_from_stockmarket

        self._regions: list[tuple[int, str]] = []
        self._systems: list[tuple[int, str]] = []
        self._stations: list[dict] = []
        self._selected_region_id: Optional[int] = None
        self._selected_system_id: Optional[int] = None
        self._selected_station: Optional[dict] = None

        self.title("Custom Stations")
        self.transient(parent)

        self._create_widgets()
        self._load_regions()
        fit_window(self, min_width=540)
        self.grab_set()

    # -------------------------------------------------------------------------
    # Layout

    def _create_widgets(self):
        # ── Add New section ──────────────────────────────────────────────────
        add_frame = ttk.LabelFrame(self, text="Add New Station", padding=10)
        add_frame.pack(fill=tk.X, padx=10, pady=(10, 5))
        add_frame.columnconfigure(1, weight=1)

        row_opts = {"sticky": tk.W, "padx": (0, 8), "pady": 3}
        cb_opts  = {"sticky": tk.EW, "pady": 3}

        ttk.Label(add_frame, text="Region:").grid(row=0, column=0, **row_opts)
        self.region_var = tk.StringVar()
        self.region_cb = ttk.Combobox(add_frame, textvariable=self.region_var,
                                      state="disabled", width=44)
        self.region_cb.grid(row=0, column=1, **cb_opts)
        self.region_cb.bind("<<ComboboxSelected>>", self._on_region_selected)

        ttk.Label(add_frame, text="System:").grid(row=1, column=0, **row_opts)
        self.system_var = tk.StringVar()
        self.system_cb = ttk.Combobox(add_frame, textvariable=self.system_var,
                                      state="disabled", width=44)
        self.system_cb.grid(row=1, column=1, **cb_opts)
        self.system_cb.bind("<<ComboboxSelected>>", self._on_system_selected)

        ttk.Label(add_frame, text="Station:").grid(row=2, column=0, **row_opts)
        self.station_var = tk.StringVar()
        self.station_cb = ttk.Combobox(add_frame, textvariable=self.station_var,
                                       state="disabled", width=44)
        self.station_cb.grid(row=2, column=1, **cb_opts)
        self.station_cb.bind("<<ComboboxSelected>>", self._on_station_selected)

        self.status_var = tk.StringVar(value="Loading regions from ESI…")
        ttk.Label(add_frame, textvariable=self.status_var,
                  font=("Segoe UI", 8), foreground="gray").grid(
            row=3, column=0, columnspan=2, sticky=tk.W, pady=(3, 0)
        )

        self.stock_market_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(add_frame, text="Also add to Stock Market",
                        variable=self.stock_market_var).grid(
            row=4, column=0, columnspan=2, sticky=tk.W, pady=(6, 0)
        )

        add_btn_frame = ttk.Frame(add_frame)
        add_btn_frame.grid(row=5, column=0, columnspan=2, sticky=tk.E, pady=(8, 0))
        self.add_btn = ttk.Button(add_btn_frame, text="Add Station",
                                  command=self._on_add, state=tk.DISABLED)
        self.add_btn.pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(add_btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)

        # ── Manage Existing section ──────────────────────────────────────────
        manage_outer = ttk.LabelFrame(self, text="Existing Custom Stations", padding=8)
        manage_outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Scrollable canvas for the station list
        canvas = tk.Canvas(manage_outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(manage_outer, orient=tk.VERTICAL, command=canvas.yview)
        self._manage_frame = ttk.Frame(canvas)

        self._manage_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self._manage_frame, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._rebuild_manage_list()

    # -------------------------------------------------------------------------
    # Manage section

    def _rebuild_manage_list(self):
        for w in self._manage_frame.winfo_children():
            w.destroy()

        stations = load_custom_stations()
        if not stations:
            ttk.Label(self._manage_frame, text="No custom stations added yet.",
                      foreground="gray", font=("Segoe UI", 8)).pack(
                anchor=tk.W, padx=4, pady=4
            )
            return

        for s in stations:
            hub_key = s["hub_key"]
            name = s["name"]
            in_sm = s.get("in_stock_market", False)

            row = ttk.Frame(self._manage_frame)
            row.pack(fill=tk.X, padx=4, pady=3)

            # Station name
            name_label = name if len(name) <= 48 else name[:45] + "…"
            ttk.Label(row, text=name_label, font=("Segoe UI", 9),
                      anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)

            # Remove from Stock Market (only if in SM)
            if in_sm:
                ttk.Button(
                    row,
                    text="Remove from Stock Market",
                    width=24,
                    command=lambda k=hub_key: self._remove_from_sm(k),
                ).pack(side=tk.RIGHT, padx=(3, 0))

            # Remove entirely (scanner + SM)
            ttk.Button(
                row,
                text="Remove from Scanner",
                width=20,
                command=lambda k=hub_key: self._remove_entirely(k),
            ).pack(side=tk.RIGHT, padx=(3, 0))

    # -------------------------------------------------------------------------
    # Remove actions

    def _remove_from_sm(self, hub_key: str):
        update_station_in_stockmarket(hub_key, False)
        self.on_remove_from_stockmarket(hub_key)
        self._rebuild_manage_list()

    def _remove_entirely(self, hub_key: str):
        """Remove the station from scanner (and stock market if it was there)."""
        # Check if it's currently in stock market so we know to fire that callback
        stations = load_custom_stations()
        in_sm = next((s.get("in_stock_market", False)
                      for s in stations if s["hub_key"] == hub_key), False)

        remove_custom_station(hub_key)
        self.on_station_removed(hub_key)
        if in_sm:
            self.on_remove_from_stockmarket(hub_key)
        self._rebuild_manage_list()

    # -------------------------------------------------------------------------
    # Add flow — Step 1: regions

    def _load_regions(self):
        _run_async(self.get_client, station_data.fetch_regions, self._on_regions_loaded)

    def _on_regions_loaded(self, result, err):
        if err or not result:
            self.status_var.set("Failed to load regions. Check your connection.")
            return
        self._regions = result
        self.region_cb.configure(values=[name for _, name in result], state="readonly")
        self.status_var.set("Select a region.")

    # ── Step 2: systems ───────────────────────────────────────────────────────

    def _on_region_selected(self, _event=None):
        idx = self.region_cb.current()
        if idx < 0 or idx >= len(self._regions):
            return
        self._selected_region_id, _ = self._regions[idx]
        self._selected_system_id = self._selected_station = None

        self.system_var.set("")
        self.system_cb.configure(values=[], state="disabled")
        self.station_var.set("")
        self.station_cb.configure(values=[], state="disabled")
        self.add_btn.configure(state=tk.DISABLED)
        self.status_var.set("Loading systems…")

        rid = self._selected_region_id
        _run_async(
            self.get_client,
            lambda c: station_data.fetch_systems_in_region(c, rid),
            self._on_systems_loaded,
        )

    def _on_systems_loaded(self, result, err):
        if err or not result:
            self.status_var.set("Failed to load systems.")
            return
        self._systems = result
        self.system_cb.configure(values=[name for _, name in result], state="readonly")
        self.status_var.set("Select a system.")

    # ── Step 3: stations ──────────────────────────────────────────────────────

    def _on_system_selected(self, _event=None):
        idx = self.system_cb.current()
        if idx < 0 or idx >= len(self._systems):
            return
        self._selected_system_id, _ = self._systems[idx]
        self._selected_station = None

        self.station_var.set("")
        self.station_cb.configure(values=[], state="disabled")
        self.add_btn.configure(state=tk.DISABLED)
        self.status_var.set("Loading stations…")

        sid = self._selected_system_id
        rid = self._selected_region_id
        _run_async(
            self.get_client,
            lambda c: station_data.fetch_stations_in_system(c, sid, rid),
            self._on_stations_loaded,
        )

    def _on_stations_loaded(self, result, err):
        if err:
            self.status_var.set("Failed to load stations.")
            return
        self._stations = result or []
        if not self._stations:
            self.status_var.set("No NPC stations in this system — try another.")
            return
        self.station_cb.configure(values=[s["name"] for s in result], state="readonly")
        self.status_var.set("Select a station.")

    def _on_station_selected(self, _event=None):
        idx = self.station_cb.current()
        if idx < 0 or idx >= len(self._stations):
            return
        self._selected_station = self._stations[idx]
        self.add_btn.configure(state=tk.NORMAL)
        self.status_var.set(f"Ready: {self._selected_station['name']}")

    # ── Confirm add ───────────────────────────────────────────────────────────

    def _on_add(self):
        if not self._selected_station:
            return

        station_id = self._selected_station["station_id"]
        hub_key = get_custom_hub_key(station_id)

        for key, cfg in TRADE_HUBS.items():
            if not is_custom_hub(key) and cfg.get("station_id") == station_id:
                messagebox.showinfo(
                    "Already Listed",
                    f"'{self._selected_station['name']}' is already in the hub list as '{cfg['name']}'.",
                    parent=self,
                )
                return

        if hub_key in TRADE_HUBS:
            messagebox.showinfo(
                "Already Added",
                f"'{self._selected_station['name']}' is already in your custom stations.",
                parent=self,
            )
            return

        in_sm = self.stock_market_var.get()
        added_key = add_custom_station(self._selected_station, in_stock_market=in_sm)

        self.on_station_added(added_key)
        if in_sm:
            self.on_add_to_stockmarket(added_key)

        # Reset add fields and refresh manage list
        self.region_var.set("")
        self.system_var.set("")
        self.station_var.set("")
        self.system_cb.configure(values=[], state="disabled")
        self.station_cb.configure(values=[], state="disabled")
        self.add_btn.configure(state=tk.DISABLED)
        self.status_var.set("Station added. Select a region to add another.")
        self._rebuild_manage_list()
