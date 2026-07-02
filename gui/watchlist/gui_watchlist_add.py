"""Dialog for adding a single item to watchlist with search and max buy calculator."""

import tkinter as tk
from tkinter import ttk
import asyncio
import threading
from typing import Callable, Optional, TYPE_CHECKING

from core.config import DEFAULT_HUB, get_hub_config
from core.tk_queue import submit
from gui.watchlist.gui_watchlist_calc_mixin import MaxBuyCalcMixin
from gui.gui_window_utils import fit_window, make_scrollable

if TYPE_CHECKING:
    from gui.watchlist.gui_watchlist_dialogs import WatchlistItem


class AddItemDialog(MaxBuyCalcMixin, tk.Toplevel):
    """Dialog to add a new item to watchlist with search."""

    def __init__(self, parent, callback: Callable, get_client: Callable,
                prefill: "WatchlistItem" = None, get_skills: Callable = None,
                region_id: int = None, show_max_buy_calc: bool = True,
                nearest_station_mode: bool = False,
                get_origin_system: Callable = None,
                get_esi_standings: Callable = None):
        super().__init__(parent)
        self.callback = callback
        self.get_client = get_client
        self.get_skills = get_skills
        self.prefill = prefill
        self.selected_item = None  # {type_id, name}
        self.region_id = region_id or get_hub_config(DEFAULT_HUB)["region_id"]
        self.show_max_buy_calc = show_max_buy_calc

        # Max buy calculator state (provided by MaxBuyCalcMixin)
        self._init_calc_state()
        # Nearest-station overrides applied after _init_calc_state and before
        # _create_widgets so the new labels get built when needed.
        self.nearest_station_mode = nearest_station_mode
        self.get_origin_system = get_origin_system
        self.get_esi_standings = get_esi_standings

        self.title("Add to Watchlist")
        self.transient(parent)

        self._create_widgets()
        fit_window(self, min_width=500)
        self.grab_set()

        # Pre-fill if provided (coming from deals context menu)
        if prefill:
            self.selected_item = {"type_id": prefill.type_id, "name": prefill.name}
            self.search_var.set(prefill.name)
            self.selected_label.configure(text=f"[OK] Selected: {prefill.name}", foreground="green")
            self.results_listbox.delete(0, tk.END)
            self.results_listbox.insert(tk.END, f"[OK] {prefill.name} (from scan)")
            self.search_results = [{"type_id": prefill.type_id, "name": prefill.name}]
            if prefill.current_price:
                self.price_under_var.set(str(int(prefill.current_price)))
            # Auto-trigger max buy calculation for prefilled items
            if self.show_max_buy_calc:
                self.after(100, self._calculate_max_buy)

    def _create_widgets(self):
        """Create dialog widgets."""
        # Add/Cancel buttons pinned to the window bottom (outside the scroll area)
        # so they stay visible regardless of content height.
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Add", command=self._on_add).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        # Scrollable content area above the buttons.
        inner = make_scrollable(self)

        # Search section
        search_frame = ttk.LabelFrame(inner, text="Search for Item", padding=10)
        search_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(search_frame, text="Item Name:").pack(anchor=tk.W)

        search_row = ttk.Frame(search_frame)
        search_row.pack(fill=tk.X, pady=5)

        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var, width=30)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 5))

        ttk.Button(search_row, text="Search", command=self._do_search).pack(side=tk.LEFT, padx=5)
        ttk.Button(search_row, text="Local", command=self._do_local_search).pack(side=tk.LEFT)

        # Tip label
        ttk.Label(search_frame, text="Tip: 'Local' finds items from previous scans. 'Search' queries ESI.",
                  font=("Segoe UI", 8), foreground="gray").pack(anchor=tk.W, pady=(2, 0))

        # Results listbox
        ttk.Label(search_frame, text="Results (click to select):").pack(anchor=tk.W, pady=(10, 0))

        results_frame = ttk.Frame(search_frame)
        results_frame.pack(fill=tk.BOTH, expand=True)

        self.results_listbox = tk.Listbox(results_frame, height=5)
        self.results_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        results_sb = ttk.Scrollbar(results_frame, orient=tk.VERTICAL, command=self.results_listbox.yview)
        results_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.results_listbox.configure(yscrollcommand=results_sb.set)

        self.results_listbox.bind("<<ListboxSelect>>", self._on_select_result)

        self.search_results = []

        # Selected item display
        self.selected_label = ttk.Label(search_frame, text="Selected: None", font=("Segoe UI", 9, "bold"))
        self.selected_label.pack(anchor=tk.W, pady=(10, 0))

        # --- Max Buy Price Calculator section (provided by MaxBuyCalcMixin) ---
        self._build_max_buy_calc_section(parent=inner)

        # Conditions section
        cond_frame = ttk.LabelFrame(inner, text="Alert Conditions (leave empty to disable)", padding=10)
        cond_frame.pack(fill=tk.X, padx=10, pady=5)

        row1 = ttk.Frame(cond_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Alert if price UNDER:", width=20).pack(side=tk.LEFT)
        self.price_under_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.price_under_var, width=15).pack(side=tk.LEFT)
        ttk.Label(row1, text="ISK").pack(side=tk.LEFT, padx=5)

        row2 = ttk.Frame(cond_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Alert if price OVER:", width=20).pack(side=tk.LEFT)
        self.price_over_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.price_over_var, width=15).pack(side=tk.LEFT)
        ttk.Label(row2, text="ISK").pack(side=tk.LEFT, padx=5)

        row3 = ttk.Frame(cond_frame)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="Alert if margin OVER:", width=20).pack(side=tk.LEFT)
        self.margin_over_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.margin_over_var, width=15).pack(side=tk.LEFT)
        ttk.Label(row3, text="%").pack(side=tk.LEFT, padx=5)

        row4 = ttk.Frame(cond_frame)
        row4.pack(fill=tk.X, pady=2)
        ttk.Label(row4, text="Notes:", width=20).pack(side=tk.LEFT)
        self.notes_var = tk.StringVar()
        ttk.Entry(row4, textvariable=self.notes_var, width=30).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Bind Enter key to search
        self.search_entry.bind("<Return>", lambda e: self._do_search())

    def _do_search(self):
        """Perform ESI search for item."""
        search_term = self.search_var.get().strip()
        if not search_term or len(search_term) < 3:
            self.results_listbox.delete(0, tk.END)
            self.results_listbox.insert(tk.END, "(Enter at least 3 characters)")
            return

        self.results_listbox.delete(0, tk.END)
        self.results_listbox.insert(tk.END, "Searching ESI...")
        
        # Run async search in thread
        def search_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = []
            error_msg = None
            
            try:
                client = self.get_client() if self.get_client else None
                if client:
                    import aiohttp
                    from core.config import REQUEST_TIMEOUT
                    
                    async def do_search():
                        client.reset_for_new_loop()
                        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
                            client.session = session
                            return await client.search_item_by_name(search_term)
                    
                    results = loop.run_until_complete(do_search())
                else:
                    error_msg = "No client available"
            except Exception as e:
                error_msg = str(e)
                print(f"Search error: {e}")
            finally:
                loop.close()
            
            # Update UI on main thread
            if error_msg:
                submit(lambda: self._display_error(error_msg))
            else:
                submit(lambda: self._display_results(results))
        
        threading.Thread(target=search_thread, daemon=True).start()

    def _display_error(self, msg: str):
        """Display error message in results list."""
        self.results_listbox.delete(0, tk.END)
        self.results_listbox.insert(tk.END, f"(Error: {msg[:40]})")

    def _do_local_search(self):
        """Search local cache (instant)."""
        search_term = self.search_var.get().strip()
        if not search_term:
            return

        client = self.get_client() if self.get_client else None
        if client:
            results = client.search_cached_items(search_term)
            self._display_results(results)
        else:
            self._display_results([])

    def _display_results(self, results: list[dict]):
        """Display search results in listbox."""
        self.results_listbox.delete(0, tk.END)
        self.search_results = results
        
        if not results:
            self.results_listbox.insert(tk.END, "(No results)")
        else:
            for item in results:
                self.results_listbox.insert(tk.END, item["name"])

    def _on_select_result(self, event):
        """Handle selection from results list."""
        selection = self.results_listbox.curselection()
        if selection and self.search_results:
            idx = selection[0]
            if idx < len(self.search_results):
                self.selected_item = self.search_results[idx]
                self.selected_label.configure(
                    text=f"[OK] Selected: {self.selected_item['name']}",
                    foreground="green"
                )
                # Auto-trigger max buy calculation
                self._calculate_max_buy()

    def _on_add(self):
        """Add item to watchlist."""
        if not self.selected_item:
            return

        # Parse conditions
        conditions = {}
        
        try:
            val = self.price_under_var.get().strip().replace(",", "")
            if val:
                conditions["price_under"] = float(val)
        except ValueError:
            pass

        try:
            val = self.price_over_var.get().strip().replace(",", "")
            if val:
                conditions["price_over"] = float(val)
        except ValueError:
            pass

        try:
            val = self.margin_over_var.get().strip().replace("%", "")
            if val:
                conditions["margin_over"] = float(val)
        except ValueError:
            pass

        conditions["notes"] = self.notes_var.get().strip()

        # Call callback
        self.callback(
            self.selected_item["type_id"],
            self.selected_item["name"],
            conditions
        )
        
        self.destroy()

