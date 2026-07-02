"""Contracts tab (Step 4) — manual, specific-item public-contract search.

Mirrors EVE's in-game Contract Search window: a filter panel on the left
(item name, scope, contract type, max price) and a results grid on the right.
This is NOT a browse-everything view — the user must resolve a typed item name
to a real type_id before searching (which also blocks an accidental "search
everything"). The contract DB/engine does the heavy lifting; this module is the
view + the manual Search trigger.

Two sub-tabs:
  - Search   — the manual search surface (this step).
  - Watchlist — saved searches + passive new-contract matches (Step 6;
    placeholder here so the layout is final and Step 6 only fills it in).

Flow on Search: resolve the chosen type_id, sync the selected scope via
ContractsEngine (off-thread, conditional on ETag so it's cheap when fresh),
then read the cache and paint the grid. All diagnostics carry `[ContractDiag]`.
"""

import tkinter as tk
from tkinter import ttk
from gui.gui_window_utils import fit_window
from datetime import datetime, timezone
from typing import Callable, Optional

from core.config import get_enabled_hubs, get_hub_config
from contracts.contracts_engine import ContractsEngine


CONTRACT_COLUMNS = ("title", "qty", "ctype", "price", "location", "issuer", "expires")
COLUMN_TITLES = {
    "title": "Contract / Title",
    "qty": "Qty",
    "ctype": "Type",
    "price": "Price",
    "location": "Location",
    "issuer": "Issuer",
    "expires": "Time Left",
}
COLUMN_WIDTHS = {
    "title": 240, "qty": 60, "ctype": 110, "price": 120,
    "location": 200, "issuer": 150, "expires": 90,
}
NUMERIC_COLUMNS = {"qty", "price"}

CONTRACT_TYPE_CHOICES = ["Any", "item_exchange", "auction", "courier"]


def _print(msg: str) -> None:
    print(f"[ContractDiag] {msg}")


def _fmt_isk(v) -> str:
    if v is None or v == 0:
        return "-"
    if abs(v) >= 1_000_000_000:
        return f"{v/1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{v/1_000:.1f}K"
    return f"{v:.0f}"


def _fmt_time_left(date_expired: Optional[str]) -> str:
    if not date_expired:
        return "-"
    try:
        exp = datetime.fromisoformat(date_expired.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "-"
    delta = exp - datetime.now(timezone.utc)
    secs = delta.total_seconds()
    if secs <= 0:
        return "expired"
    days = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    if days > 0:
        return f"{days}d {hours}h"
    mins = int((secs % 3600) // 60)
    return f"{hours}h {mins}m"


def _resolve_location_name(station_id: Optional[int]) -> str:
    """Best-effort station/structure name. Player structures (unresolvable
    here) display as 'Unknown Station' per the design."""
    if not station_id:
        return "Unknown Station"
    try:
        from gui.gui_station_lookup import StationLookup
        info = StationLookup.singleton().lookup(int(station_id))
        if info and info.get("name"):
            return info["name"]
    except Exception:
        pass
    return f"Unknown Station ({station_id})"


class ContractsTabManager:
    """Owns the Contracts tab — search filter panel, results grid, sub-tabs."""

    def __init__(self, notebook: ttk.Notebook, get_client: Callable,
                 set_status: Callable[[str], None],
                 root: Optional[tk.Tk] = None):
        self.notebook = notebook
        self.get_client = get_client
        self.set_status = set_status
        self.root = root

        # Exclusion list drives both the engine (drops excluded contracts from
        # the items-fetch worklist + search) and the right-click action.
        from contracts.contracts_lists import ExcludeList
        self.excludes = ExcludeList.singleton()
        self.engine = ContractsEngine(
            get_client, exclude_ids_provider=self.excludes.ids)

        # Current resolved search target.
        self.selected_type_id: Optional[int] = None
        self.selected_type_name: Optional[str] = None
        self._suggest_after = None  # debounce handle for the type-ahead list

        # tree-item-id -> contract row dict, for context actions.
        self._item_to_row: dict[str, dict] = {}
        self._searching = False

        # Watchlist sub-tab state: accumulated matches + saved-search row maps.
        self._wl_match_to_row: dict[str, dict] = {}
        self._wl_entry_to_meta: dict[str, dict] = {}

        self._build_tab()

        # Passive hourly pull + watchlist matching (Steps 6/7).
        from contracts.contracts_scheduler import ContractsScheduler
        self.scheduler = ContractsScheduler(
            self.engine,
            on_matches=self._on_watchlist_matches,
            on_backfill_progress=self._on_backfill_progress,
        )
        self.scheduler.start()
        self._refresh_saved_searches()

    # =========================================================================
    # Construction
    # =========================================================================

    def _build_tab(self):
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text="Contracts")
        self.frame = outer

        # Inner notebook: Search | Watchlist
        self.sub_notebook = ttk.Notebook(outer)
        self.sub_notebook.pack(fill=tk.BOTH, expand=True)

        self._build_search_tab()
        self._build_watchlist_tab()
        self._build_context_menu()

    def _build_search_tab(self):
        tab = ttk.Frame(self.sub_notebook)
        self.sub_notebook.add(tab, text="Search")

        # Left: filter panel. Right: results.
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        filters = ttk.LabelFrame(tab, text="Contract Search", padding=8)
        filters.grid(row=0, column=0, sticky="ns", padx=(6, 4), pady=6)

        # --- Item name with a live type-ahead match list ---
        ttk.Label(filters, text="Item name:").pack(anchor=tk.W)
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(filters, textvariable=self.name_var, width=28)
        self.name_entry.pack(anchor=tk.W, fill=tk.X)
        self.name_entry.bind("<KeyRelease>", self._on_name_keyrelease)

        self.resolved_label = ttk.Label(filters,
                                        text="(type a name to see matches)",
                                        foreground="gray", wraplength=200)
        self.resolved_label.pack(anchor=tk.W, pady=(0, 6))

        # Type-ahead match list — appears as you type, and HIDES once you pick
        # one (or when the box is empty). Picking fills the box and sets the
        # target; the actual contract search still waits for the Search button.
        self.suggest_frame = ttk.Frame(filters)
        self.suggest_box = tk.Listbox(self.suggest_frame, height=6, width=28,
                                      exportselection=False)
        self.suggest_box.pack(anchor=tk.W, fill=tk.X, pady=(2, 2))
        self.suggest_box.bind("<<ListboxSelect>>", self._on_suggestion_pick)
        self._suggestions: list[dict] = []

        # --- Scope (station-level trade hubs; regional opt-in is Step 7) ---
        self.scope_label = ttk.Label(filters, text="Scope (station):")
        self.scope_label.pack(anchor=tk.W)
        self.scope_var = tk.StringVar()
        self.scope_choices: dict[str, dict] = {}
        for key, name in get_enabled_hubs():
            cfg = get_hub_config(key)
            if cfg.get("type") == "structure":
                continue  # public contract list is NPC-region only
            self.scope_choices[name] = {
                "region_id": cfg["region_id"],
                "station_id": cfg["station_id"],
                "regional": False,
            }
            # Region-wide variant is opt-in and DEFERRED: selecting it does not
            # crawl live — it queues the region for the next background sync.
            self.scope_choices[f"{name} — whole region (opt-in)"] = {
                "region_id": cfg["region_id"],
                "station_id": None,
                "regional": True,
            }
        scope_names = list(self.scope_choices.keys())
        scope_combo = ttk.Combobox(filters, textvariable=self.scope_var,
                                   values=scope_names, state="readonly", width=26)
        scope_combo.pack(anchor=tk.W, fill=tk.X, pady=(0, 6))
        # Default to Jita if present, else first hub.
        if "Jita" in self.scope_choices:
            self.scope_var.set("Jita")
        elif scope_names:
            self.scope_var.set(scope_names[0])

        # --- Contract type ---
        ttk.Label(filters, text="Contract type:").pack(anchor=tk.W)
        self.ctype_var = tk.StringVar(value="Any")
        ttk.Combobox(filters, textvariable=self.ctype_var,
                     values=CONTRACT_TYPE_CHOICES, state="readonly",
                     width=26).pack(anchor=tk.W, fill=tk.X, pady=(0, 6))

        # --- Max price ---
        ttk.Label(filters, text="Max price (ISK, optional):").pack(anchor=tk.W)
        self.max_price_var = tk.StringVar()
        ttk.Entry(filters, textvariable=self.max_price_var,
                  width=28).pack(anchor=tk.W, fill=tk.X, pady=(0, 8))

        # --- Search button ---
        self.search_btn = ttk.Button(filters, text="Search",
                                     command=self._on_search)
        self.search_btn.pack(anchor=tk.W, fill=tk.X)

        self.search_status = ttk.Label(filters, text="", foreground="gray",
                                       wraplength=200)
        self.search_status.pack(anchor=tk.W, pady=(6, 0))

        # --- Right: results grid ---
        right = ttk.Frame(tab)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 6), pady=6)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(right, columns=CONTRACT_COLUMNS, show="headings")
        for col in CONTRACT_COLUMNS:
            self.tree.heading(col, text=COLUMN_TITLES[col],
                              command=lambda c=col: self._sort_by_column(c))
            anchor = tk.E if col in NUMERIC_COLUMNS else tk.W
            self.tree.column(col, width=COLUMN_WIDTHS[col], anchor=anchor)

        vsb = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(right, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.tree.bind("<Button-3>", self._on_right_click)
        self._set_empty_hint("Pick an item and click Search.")

    WL_ENTRY_COLUMNS = ("item", "scope", "max_price", "added")
    WL_ENTRY_TITLES = {"item": "Item", "scope": "Scope",
                       "max_price": "Max Price", "added": "Added"}
    WL_MATCH_COLUMNS = ("item", "location", "price", "expires")
    WL_MATCH_TITLES = {"item": "Item", "location": "Location",
                       "price": "Price", "expires": "Time Left"}

    def _build_watchlist_tab(self):
        """Saved searches (top) + passive new-contract matches (bottom).

        The hourly pull (ContractsScheduler) refreshes cached hub scopes and
        diffs each saved search against the cache; new hits land in Matches.
        """
        tab = ttk.Frame(self.sub_notebook)
        self.sub_notebook.add(tab, text="Watchlist")
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(3, weight=2)
        tab.columnconfigure(0, weight=1)

        ttk.Label(tab, text="Saved Searches", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w", padx=6, pady=(6, 0))

        ent_frame = ttk.Frame(tab)
        ent_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 4))
        ent_frame.rowconfigure(0, weight=1)
        ent_frame.columnconfigure(0, weight=1)
        self.wl_entries_tree = ttk.Treeview(
            ent_frame, columns=self.WL_ENTRY_COLUMNS, show="headings", height=5)
        for col in self.WL_ENTRY_COLUMNS:
            self.wl_entries_tree.heading(col, text=self.WL_ENTRY_TITLES[col])
            self.wl_entries_tree.column(
                col, width=160, anchor=(tk.E if col == "max_price" else tk.W))
        ent_vsb = ttk.Scrollbar(ent_frame, orient=tk.VERTICAL,
                                command=self.wl_entries_tree.yview)
        self.wl_entries_tree.configure(yscrollcommand=ent_vsb.set)
        self.wl_entries_tree.grid(row=0, column=0, sticky="nsew")
        ent_vsb.grid(row=0, column=1, sticky="ns")
        self.wl_entries_tree.bind("<Button-3>", self._on_wl_entry_right_click)

        ttk.Label(tab, text="New Matches", font=("Segoe UI", 9, "bold")).grid(
            row=2, column=0, sticky="w", padx=6, pady=(6, 0))

        match_frame = ttk.Frame(tab)
        match_frame.grid(row=3, column=0, sticky="nsew", padx=6, pady=(2, 6))
        match_frame.rowconfigure(0, weight=1)
        match_frame.columnconfigure(0, weight=1)
        self.wl_matches_tree = ttk.Treeview(
            match_frame, columns=self.WL_MATCH_COLUMNS, show="headings")
        for col in self.WL_MATCH_COLUMNS:
            self.wl_matches_tree.heading(col, text=self.WL_MATCH_TITLES[col])
            self.wl_matches_tree.column(
                col, width=180, anchor=(tk.E if col == "price" else tk.W))
        m_vsb = ttk.Scrollbar(match_frame, orient=tk.VERTICAL,
                              command=self.wl_matches_tree.yview)
        self.wl_matches_tree.configure(yscrollcommand=m_vsb.set)
        self.wl_matches_tree.grid(row=0, column=0, sticky="nsew")
        m_vsb.grid(row=0, column=1, sticky="ns")
        self.wl_matches_tree.bind("<Button-3>", self._on_wl_match_right_click)

        # Context menus for the two trees.
        self.wl_entry_menu = tk.Menu(self.notebook, tearoff=0)
        self.wl_entry_menu.add_command(label="Change max price",
                                       command=self._wl_change_price)
        self.wl_entry_menu.add_command(label="Remove", command=self._wl_remove)

        self.wl_match_menu = tk.Menu(self.notebook, tearoff=0)
        self.wl_match_menu.add_command(label="Copy item name",
                                       command=self._wl_match_copy)

    def _build_context_menu(self):
        self.context_menu = tk.Menu(self.notebook, tearoff=0)
        self.context_menu.add_command(label="Copy item name",
                                      command=self._cm_copy_name)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Add to watchlist",
                                      command=self._cm_add_watchlist)
        self.context_menu.add_command(label="Add to exclusion list",
                                      command=self._cm_add_exclusion)

    # =========================================================================
    # Item-name resolution (SDE)
    # =========================================================================

    def _on_name_keyrelease(self, _event=None):
        """Debounced type-ahead — refresh the match list shortly after you stop
        typing (one SQLite lookup per pause, not per keystroke)."""
        widget = self.root or self.notebook
        if self._suggest_after is not None:
            try:
                widget.after_cancel(self._suggest_after)
            except Exception:
                pass
        self._suggest_after = widget.after(300, self._refresh_suggestions)

    def _refresh_suggestions(self):
        self._suggest_after = None
        query = self.name_var.get().strip()
        if len(query) < 3:
            self._hide_suggestions()
            return
        try:
            from sde.sde_manager import get_sde_manager
            self._suggestions = get_sde_manager().search_types_by_name(
                query, limit=30)
        except Exception as e:
            _print(f"SDE search failed: {e}")
            self._suggestions = []
        if not self._suggestions:
            self._hide_suggestions()
            return
        self.suggest_box.delete(0, tk.END)
        for m in self._suggestions:
            self.suggest_box.insert(tk.END, m["name"])
        # Reveal the list just above the scope selector.
        self.suggest_frame.pack(anchor=tk.W, fill=tk.X, pady=(0, 6),
                                before=self.scope_label)

    def _hide_suggestions(self):
        self.suggest_frame.pack_forget()
        self.suggest_box.delete(0, tk.END)
        self._suggestions = []

    def _on_suggestion_pick(self, _event=None):
        """Pick from the type-ahead: fill the box, set the target, CLOSE the
        list. The contract search still waits for the Search button."""
        sel = self.suggest_box.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self._suggestions):
            return
        chosen = self._suggestions[idx]
        self.selected_type_id = int(chosen["type_id"])
        self.selected_type_name = chosen["name"]
        self.name_var.set(chosen["name"])          # fill the search bar
        self.resolved_label.configure(
            text=f"Selected: {chosen['name']} (type {chosen['type_id']})",
            foreground="#003a8c")
        self._hide_suggestions()                   # collapse the dropdown
        _print(f"search target set: {chosen['name']} ({chosen['type_id']})")

    # =========================================================================
    # Search
    # =========================================================================

    def _on_search(self):
        if self._searching:
            return
        if not self.selected_type_id:
            self.search_status.configure(
                text="Pick an item from the list first.", foreground="#a40000")
            return
        scope = self.scope_choices.get(self.scope_var.get())
        if not scope:
            self.search_status.configure(text="Pick a scope.",
                                         foreground="#a40000")
            return
        self._run_contract_search(scope)

    def _run_contract_search(self, scope: dict):
        """Run the contract search for the already-resolved type_id + scope."""
        if not self.selected_type_id:
            return

        # Region-wide opt-in: do NOT crawl live (tens of thousands of contracts
        # → 520 risk). Queue it for the next background sync and show whatever's
        # already cached for the region in the meantime.
        if scope.get("regional"):
            self.scheduler.queue_regional_optin(scope["region_id"])
            rows = self.engine.search_cached(self.selected_type_id,
                                             scope["region_id"], None)
            rows = self._apply_local_filters(rows)
            self._display_rows(rows)
            self.search_status.configure(
                text=f"Whole region queued — will be added at next background "
                     f"sync. Showing {len(rows)} already-cached contract(s).",
                foreground="#8a6d00")
            _print(f"regional opt-in queued for region {scope['region_id']}; "
                   f"showing {len(rows)} cached")
            return

        self._searching = True
        self.search_btn.configure(state=tk.DISABLED)
        self.search_status.configure(
            text=f"Checking {self.scope_var.get()} contract list…",
            foreground="gray")
        _print(f"search started: type={self.selected_type_id} "
               f"scope={self.scope_var.get()} region={scope['region_id']} "
               f"station={scope['station_id']}")

        # Two-phase: refresh the LIST first (cheap / conditional), then count how
        # many contracts still need their contents fetched and warn before a big
        # crawl. Avoids silently kicking off a 30k-contract pull.
        self.engine.run_sync_in_thread(
            region_id=scope["region_id"],
            item_fetch_locations=set(),  # list only, no items
            done_cb=lambda summary: self._after_list_phase(summary, scope),
        )

    # Warn before crawling more than this many contracts' contents.
    LARGE_CRAWL_THRESHOLD = 500

    def _after_list_phase(self, summary, scope):
        if not summary or not summary.get("ok"):
            self._searching = False
            self.search_btn.configure(state=tk.NORMAL)
            self.search_status.configure(
                text=f"List refresh failed: {(summary or {}).get('reason','?')}",
                foreground="#a40000")
            return

        pending = self.engine.pending_station_count(
            scope["region_id"], scope["station_id"])

        if pending == 0:
            # Everything at this station is already cached — just show results.
            self._on_sync_done({"ok": True, "new": 0}, scope)
            return

        if pending > self.LARGE_CRAWL_THRESHOLD:
            from tkinter import messagebox
            ok = messagebox.askyesno(
                "Large contract pull",
                f"{pending:,} contracts at {self.scope_var.get()} still need "
                f"their contents fetched — one ESI call each, so this can take "
                f"several minutes (busy stations like Jita are huge).\n\n"
                f"Progress is saved continuously and resumes if you close the "
                f"app mid-pull.\n\nFetch them now?",
                parent=self.root or self.notebook,
            )
            if not ok:
                self._searching = False
                self.search_btn.configure(state=tk.NORMAL)
                rows = self._apply_local_filters(self.engine.search_cached(
                    self.selected_type_id, scope["region_id"],
                    scope["station_id"]))
                self._display_rows(rows)
                self.search_status.configure(
                    text=f"Skipped fetch — showing {len(rows)} already-cached "
                         f"contract(s).", foreground="#8a6d00")
                return

        # Phase 2: fetch the contents (streamed to disk as it goes).
        self.search_status.configure(
            text=f"Fetching contents at {self.scope_var.get()}… "
                 f"(saved as it goes)", foreground="gray")
        self.engine.run_sync_in_thread(
            region_id=scope["region_id"],
            item_fetch_locations={scope["station_id"]},
            progress_cb=self._on_sync_progress,
            done_cb=lambda s: self._on_sync_done(s, scope),
        )

    def _on_sync_progress(self, done, total):
        self.search_status.configure(
            text=f"Fetching contract contents… {done}/{total}",
            foreground="gray")

    def _on_sync_done(self, summary, scope):
        self._searching = False
        self.search_btn.configure(state=tk.NORMAL)
        if not summary or not summary.get("ok"):
            reason = (summary or {}).get("reason", "unknown")
            self.search_status.configure(
                text=f"Sync failed: {reason}", foreground="#a40000")
            return

        rows = self.engine.search_cached(
            self.selected_type_id, scope["region_id"], scope["station_id"])
        rows = self._apply_local_filters(rows)
        self._display_rows(rows)
        self.search_status.configure(
            text=f"{len(rows)} contract(s) — "
                 f"{summary.get('new',0)} new this sync",
            foreground="gray")

    def _apply_local_filters(self, rows: list[dict]) -> list[dict]:
        """Apply the cheap client-side filters (contract type, max price)."""
        ctype = self.ctype_var.get()
        max_price = None
        raw = self.max_price_var.get().strip().replace(",", "")
        if raw:
            try:
                max_price = float(raw)
            except ValueError:
                max_price = None

        out = []
        for r in rows:
            if ctype != "Any" and (r.get("type") or "") != ctype:
                continue
            if max_price is not None and (r.get("price") or 0) > max_price:
                continue
            out.append(r)
        return out

    # =========================================================================
    # Results grid
    # =========================================================================

    def _display_rows(self, rows: list[dict]):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._item_to_row = {}
        if not rows:
            self._set_empty_hint("No contracts matched.")
            return
        for r in rows:
            iid = self.tree.insert("", tk.END, values=self._format_row(r))
            self._item_to_row[iid] = r

    def _format_row(self, r: dict) -> tuple:
        title = r.get("title") or self.selected_type_name or "-"
        return (
            title,
            r.get("matched_qty") or "-",
            r.get("type") or "-",
            _fmt_isk(r.get("price")),
            _resolve_location_name(r.get("start_location_id")),
            r.get("issuer_name") or "Unknown",
            _fmt_time_left(r.get("date_expired")),
        )

    def _set_empty_hint(self, text: str):
        for item in self.tree.get_children():
            self.tree.delete(item)
        blank = ("",) * (len(CONTRACT_COLUMNS) - 1)
        self.tree.insert("", tk.END, values=(text, *blank))

    def _sort_by_column(self, col: str):
        rows = list(self._item_to_row.values())
        if not rows:
            return
        if not hasattr(self, "_col_dir"):
            self._col_dir = {}
        reverse = self._col_dir.get(col, False)
        self._col_dir[col] = not reverse
        keys = {
            "title": lambda r: (r.get("title") or "").lower(),
            "qty": lambda r: r.get("matched_qty") or 0,
            "ctype": lambda r: r.get("type") or "",
            "price": lambda r: r.get("price") or 0,
            "location": lambda r: _resolve_location_name(r.get("start_location_id")),
            "issuer": lambda r: (r.get("issuer_name") or "").lower(),
            "expires": lambda r: r.get("date_expired") or "",
        }
        key = keys.get(col)
        if key is None:
            return
        rows.sort(key=key, reverse=reverse)
        self._display_rows(rows)

    # =========================================================================
    # Context menu
    # =========================================================================

    def _on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item or item not in self._item_to_row:
            return
        self.tree.selection_set(item)
        self.context_menu.post(event.x_root, event.y_root)

    def _selected_row(self) -> Optional[dict]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._item_to_row.get(sel[0])

    def _cm_copy_name(self):
        """Copy text usable for both in-app paste and EVE's in-game search box."""
        name = self.selected_type_name
        if not name or not self.root:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(name)
            self.set_status(f"Copied: {name}")
        except Exception as e:
            _print(f"copy failed: {e}")

    def _cm_add_watchlist(self):
        row = self._selected_row()
        if not row or not self.selected_type_id:
            return
        region_id = row.get("region_id")
        station_id = row.get("start_location_id")
        if region_id is None:
            self.set_status("Contract missing region — cannot watch.")
            return

        # Prompt for the optional "cost per" / max-price threshold.
        default_price = row.get("price")
        max_price = self._prompt_max_price(default_price)
        if max_price is False:  # user cancelled
            return

        from contracts.contracts_lists import ContractWatchlist
        wl = ContractWatchlist.for_region(region_id)
        wl.add(self.selected_type_id, self.selected_type_name,
               station_id=station_id, max_price=max_price)
        self.set_status(f"Watching {self.selected_type_name} in region {region_id}")
        self._refresh_saved_searches()

    def _prompt_max_price(self, default_price):
        """Modal popup for the watchlist 'cost per' threshold.

        Returns a float, None (no threshold), or False (cancelled).
        """
        dlg = tk.Toplevel(self.root or self.notebook)
        dlg.title("Watch contract")
        dlg.transient(self.root or self.notebook)
        dlg.grab_set()
        ttk.Label(dlg, text="Alert when contract price is at or below:",
                  padding=10).pack(anchor=tk.W)
        var = tk.StringVar(value=str(int(default_price)) if default_price else "")
        ttk.Entry(dlg, textvariable=var, width=24).pack(padx=10, anchor=tk.W)
        ttk.Label(dlg, text="(leave blank for no price threshold)",
                  foreground="gray", padding=(10, 0)).pack(anchor=tk.W)

        result = {"value": False}

        def _ok():
            raw = var.get().strip().replace(",", "")
            if not raw:
                result["value"] = None
            else:
                try:
                    result["value"] = float(raw)
                except ValueError:
                    result["value"] = None
            dlg.destroy()

        def _cancel():
            result["value"] = False
            dlg.destroy()

        btns = ttk.Frame(dlg, padding=10)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Watch", command=_ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=_cancel).pack(side=tk.RIGHT)
        fit_window(dlg, min_width=320)
        dlg.wait_window()
        return result["value"]

    # =========================================================================
    # Watchlist sub-tab — saved searches + passive matches
    # =========================================================================

    def _watched_region_ids(self) -> list[int]:
        """Regions that may hold watchlist entries — the trade hubs for now."""
        regions = []
        for key, _name in get_enabled_hubs():
            cfg = get_hub_config(key)
            if cfg.get("type") == "structure":
                continue
            if cfg["region_id"] not in regions:
                regions.append(cfg["region_id"])
        return regions

    def _refresh_saved_searches(self):
        from contracts.contracts_lists import ContractWatchlist
        for item in self.wl_entries_tree.get_children():
            self.wl_entries_tree.delete(item)
        self._wl_entry_to_meta = {}
        for region_id in self._watched_region_ids():
            wl = ContractWatchlist.for_region(region_id)
            for entry in wl.entries():
                station = entry.get("station_id")
                scope = (_resolve_location_name(station) if station
                         else f"Region {region_id}")
                mp = entry.get("max_price")
                iid = self.wl_entries_tree.insert("", tk.END, values=(
                    entry.get("type_name") or entry.get("type_id"),
                    scope,
                    _fmt_isk(mp) if mp else "any",
                    (entry.get("date_added") or "")[:10],
                ))
                self._wl_entry_to_meta[iid] = {
                    "region_id": region_id,
                    "type_id": entry.get("type_id"),
                    "station_id": station,
                    "type_name": entry.get("type_name"),
                }

    def _on_watchlist_matches(self, matches: list[dict]):
        """Scheduler callback (Tk thread): append new passive matches."""
        for m in matches:
            iid = self.wl_matches_tree.insert("", 0, values=(
                m.get("type_name") or m.get("type_id"),
                _resolve_location_name(m.get("start_location_id")),
                _fmt_isk(m.get("price")),
                _fmt_time_left(m.get("date_expired")),
            ))
            self._wl_match_to_row[iid] = m
        if matches:
            self.set_status(f"{len(matches)} new contract match(es) — see "
                            f"Contracts ▸ Watchlist")

    def _on_backfill_progress(self, region_id, done, total):
        self.set_status(f"Region {region_id} contract backfill: {done}/{total}")

    def _on_wl_entry_right_click(self, event):
        item = self.wl_entries_tree.identify_row(event.y)
        if not item or item not in self._wl_entry_to_meta:
            return
        self.wl_entries_tree.selection_set(item)
        self.wl_entry_menu.post(event.x_root, event.y_root)

    def _on_wl_match_right_click(self, event):
        item = self.wl_matches_tree.identify_row(event.y)
        if not item or item not in self._wl_match_to_row:
            return
        self.wl_matches_tree.selection_set(item)
        self.wl_match_menu.post(event.x_root, event.y_root)

    def _selected_wl_entry(self) -> Optional[dict]:
        sel = self.wl_entries_tree.selection()
        if not sel:
            return None
        return self._wl_entry_to_meta.get(sel[0])

    def _wl_change_price(self):
        meta = self._selected_wl_entry()
        if not meta:
            return
        new_price = self._prompt_max_price(None)
        if new_price is False:
            return
        from contracts.contracts_lists import ContractWatchlist
        wl = ContractWatchlist.for_region(meta["region_id"])
        wl.update_price(meta["type_id"], meta["station_id"], new_price)
        self._refresh_saved_searches()

    def _wl_remove(self):
        meta = self._selected_wl_entry()
        if not meta:
            return
        from contracts.contracts_lists import ContractWatchlist
        wl = ContractWatchlist.for_region(meta["region_id"])
        wl.remove(meta["type_id"], meta["station_id"])
        self._refresh_saved_searches()

    def _wl_match_copy(self):
        sel = self.wl_matches_tree.selection()
        if not sel or not self.root:
            return
        m = self._wl_match_to_row.get(sel[0])
        if not m:
            return
        name = m.get("type_name")
        if not name:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(name)
            self.set_status(f"Copied: {name}")
        except Exception as e:
            _print(f"copy failed: {e}")

    def _cm_add_exclusion(self):
        row = self._selected_row()
        if not row:
            return
        cid = row.get("contract_id")
        if cid is None:
            return
        self.excludes.add(
            cid, title=row.get("title"), item_name=self.selected_type_name)
        self.set_status(f"Excluded contract {cid}")
        # Drop it from the current view immediately.
        remaining = [r for r in self._item_to_row.values()
                     if r.get("contract_id") != cid]
        self._display_rows(remaining)
