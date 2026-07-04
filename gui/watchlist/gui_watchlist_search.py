"""Dialogs for searching/matching items and editing watchlist entries."""

import tkinter as tk
from tkinter import ttk
import asyncio
import threading
from typing import Callable, TYPE_CHECKING
from core.tk_queue import submit
from gui.watchlist.gui_watchlist_calc_mixin import MaxBuyCalcMixin
from gui.gui_window_utils import fit_window, make_scrollable

if TYPE_CHECKING:
    from gui.watchlist.gui_watchlist_dialogs import WatchlistItem


class SearchMatchDialog(tk.Toplevel):
    """Mini dialog to search and select a match for an unmatched item."""

    def __init__(self, parent, original_name: str, suggestions: list[dict], get_client: Callable, callback: Callable):
        super().__init__(parent)
        self.original_name = original_name
        self.suggestions = suggestions
        self.get_client = get_client
        self.callback = callback
        self.selected_item = None
        self.search_results = []

        self.title(f"Find Match: {original_name[:30]}")
        self.transient(parent)

        self._create_widgets()
        fit_window(self, min_width=400)
        self.grab_set()

    def _create_widgets(self):
        """Create dialog widgets."""
        ttk.Label(self, text=f"Original: {self.original_name}", font=("Segoe UI", 9, "bold")).pack(pady=5)

        # Search
        search_frame = ttk.Frame(self)
        search_frame.pack(fill=tk.X, padx=10, pady=5)

        self.search_var = tk.StringVar(value=self.original_name)
        ttk.Entry(search_frame, textvariable=self.search_var, width=30).pack(side=tk.LEFT)
        ttk.Button(search_frame, text="Search", command=self._do_search).pack(side=tk.LEFT, padx=5)

        # Results
        ttk.Label(self, text="Select correct item:").pack(anchor=tk.W, padx=10)

        self.listbox = tk.Listbox(self, height=8)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        # Pre-populate with suggestions
        if self.suggestions:
            self.search_results = self.suggestions
            for item in self.suggestions:
                self.listbox.insert(tk.END, item["name"])

        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Select", command=self._on_confirm).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)

    def _do_search(self):
        """Search ESI for item."""
        search_term = self.search_var.get().strip()
        if not search_term:
            return

        self.listbox.delete(0, tk.END)
        self.listbox.insert(tk.END, "Searching...")

        def search_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = []

            try:
                client = self.get_client() if self.get_client else None
                if client:
                    import aiohttp
                    from core.config import REQUEST_TIMEOUT, ESI_USER_AGENT

                    async def do_search():
                        client.reset_for_new_loop()
                        async with aiohttp.ClientSession(headers={"User-Agent": ESI_USER_AGENT}, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
                            client.session = session
                            return await client.search_item_by_name(search_term)

                    results = loop.run_until_complete(do_search())
            except Exception as e:
                print(f"Search error: {e}")
            finally:
                loop.close()

            submit(lambda: self._display_results(results))

        threading.Thread(target=search_thread, daemon=True).start()

    def _display_results(self, results: list[dict]):
        """Display search results."""
        self.listbox.delete(0, tk.END)
        self.search_results = results

        if not results:
            self.listbox.insert(tk.END, "(No results)")
        else:
            for item in results:
                self.listbox.insert(tk.END, item["name"])

    def _on_select(self, event):
        """Handle selection."""
        selection = self.listbox.curselection()
        if selection and self.search_results:
            idx = selection[0]
            if idx < len(self.search_results):
                self.selected_item = self.search_results[idx]

    def _on_confirm(self):
        """Confirm selection."""
        if self.selected_item:
            self.callback(self.selected_item["type_id"], self.selected_item["name"])
            self.destroy()


class EditItemDialog(MaxBuyCalcMixin, tk.Toplevel):
    """Dialog to edit an existing watchlist item.
    
    When show_max_buy_calc=True (used by NPC Orders tab), the Max Buy Price
    Calculator section is included so the user can recalc with current skills/
    standings without removing and re-adding the item.
    """

    def __init__(self, parent, item: "WatchlistItem", callback: Callable,
                 get_client: Callable = None, get_skills: Callable = None,
                 region_id: int = None, show_max_buy_calc: bool = False,
                 nearest_station_mode: bool = False,
                 get_origin_system: Callable = None,
                 get_esi_standings: Callable = None):
        super().__init__(parent)
        self.item = item
        self.callback = callback
        self.get_client = get_client
        self.get_skills = get_skills
        self.region_id = region_id
        self.show_max_buy_calc = show_max_buy_calc

        # MaxBuyCalcMixin expects selected_item like AddItemDialog uses
        self.selected_item = {"type_id": item.type_id, "name": item.name}
        self._init_calc_state()
        # Nearest-station overrides applied after _init_calc_state and before
        # _create_widgets so the new labels get built when needed.
        self.nearest_station_mode = nearest_station_mode
        self.get_origin_system = get_origin_system
        self.get_esi_standings = get_esi_standings

        self.title(f"Edit: {item.name}")
        self.transient(parent)

        self._create_widgets()
        fit_window(self, min_width=500 if show_max_buy_calc else 400)
        self.grab_set()

    def _create_widgets(self):
        """Create dialog widgets."""
        # Buttons always pinned to the window bottom so they survive screen-height
        # clamping. The NPC Orders flow (show_max_buy_calc=True) adds a scrollable
        # canvas above them; the plain watchlist edit packs directly into self.
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Save", command=self._on_save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        # Use a scrollable canvas for the tall NPC Orders flow; plain packing for
        # the small personal-watchlist edit that doesn't need it.
        parent = make_scrollable(self) if self.show_max_buy_calc else self

        # Item name (read-only)
        ttk.Label(parent, text=f"Item: {self.item.name}", font=("Segoe UI", 10, "bold")).pack(pady=10)

        # --- Max Buy Price Calculator section (provided by MaxBuyCalcMixin) ---
        self._build_max_buy_calc_section(parent=parent)

        # Conditions
        cond_frame = ttk.LabelFrame(parent, text="Alert Conditions", padding=10)
        cond_frame.pack(fill=tk.X, padx=10, pady=5)

        # Price under
        row1 = ttk.Frame(cond_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Alert if price UNDER:", width=20).pack(side=tk.LEFT)
        self.price_under_var = tk.StringVar(value=str(int(self.item.price_under)) if self.item.price_under else "")
        ttk.Entry(row1, textvariable=self.price_under_var, width=15).pack(side=tk.LEFT)
        ttk.Label(row1, text="ISK").pack(side=tk.LEFT, padx=5)

        # Price over
        row2 = ttk.Frame(cond_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Alert if price OVER:", width=20).pack(side=tk.LEFT)
        self.price_over_var = tk.StringVar(value=str(int(self.item.price_over)) if self.item.price_over else "")
        ttk.Entry(row2, textvariable=self.price_over_var, width=15).pack(side=tk.LEFT)
        ttk.Label(row2, text="ISK").pack(side=tk.LEFT, padx=5)

        # Notes
        row3 = ttk.Frame(cond_frame)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="Notes:", width=20).pack(side=tk.LEFT)
        self.notes_var = tk.StringVar(value=self.item.notes)
        ttk.Entry(row3, textvariable=self.notes_var, width=30).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _on_save(self):
        """Save changes."""
        conditions = {}
        
        try:
            val = self.price_under_var.get().strip().replace(",", "")
            conditions["price_under"] = float(val) if val else None
        except ValueError:
            conditions["price_under"] = None

        try:
            val = self.price_over_var.get().strip().replace(",", "")
            conditions["price_over"] = float(val) if val else None
        except ValueError:
            conditions["price_over"] = None

        conditions["notes"] = self.notes_var.get().strip()

        self.callback(self.item.type_id, conditions)
        self.destroy()
