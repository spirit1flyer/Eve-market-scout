"""Raw market viewer for a single player structure (dev/debug tool).

Two tabs:
  - Current Orders: bypasses every scanner filter, dumps the entire live order
    book so the user can confirm "is the fetch working?" / "does this structure
    have a market?"
  - History (observed): per-item event trail (listing / partial fill / full fill
    / expire) reconstructed from snapshots in structure_history.db, plus a
    summary line for the selected item. This is where Phase 1 collection
    becomes visible — sanity-check what we're inferring before anything
    consumes it.

Each Refresh fetch also calls StructureHistoryDB.record_snapshot, so opening
Browse Orders on a structure contributes data to the same collection as
scanner runs.
"""

import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime, timezone

from core.tk_queue import submit
from esi.esi_auth import ESIAuth
from esi.esi_structures import fetch_structure_orders, StructureAccessError
from gui.gui_browse_orders_filters import BrowseOrdersFilterMixin
from gui.gui_window_utils import fit_window


_KIND_LABELS = {
    "listing": "New listing",
    "partial_fill": "Partial fill",
    "full_fill": "Full fill",
    "expire": "Expired",
}


class BrowseStructureOrdersDialog(BrowseOrdersFilterMixin, tk.Toplevel):
    def __init__(self, parent, structure_id: int, structure_name: str,
                 slot: str = "seller"):
        super().__init__(parent)
        self.structure_id = structure_id
        self.structure_name = structure_name
        self.slot = slot
        self.auth = ESIAuth()
        self._type_names: dict[int, str] = {}

        self.title(f"Browse Orders — {structure_name}")
        self.transient(parent)

        # Owned by BrowseOrdersFilterMixin: chips, taxonomy, available cats/groups.
        self._init_filter_state()

        self._build()
        fit_window(self, min_width=960)
        self._kick_fetch()

    # ------------------------------------------------------------------ build

    def _build(self):
        header = ttk.Frame(self, padding=10)
        header.pack(fill=tk.X)

        # Structure picker — Combobox lets the user hop between any registered
        # player structure without closing the dialog. Always rendered (even
        # with a single structure) so the affordance is visible from day one.
        from core.config import TRADE_HUBS
        self._structures = [
            (k, cfg) for k, cfg in TRADE_HUBS.items()
            if cfg.get("type") == "structure"
        ]
        names = [cfg.get("name", str(cfg["station_id"]))
                 for _, cfg in self._structures]

        self._structure_var = tk.StringVar(value=self.structure_name)
        self._structure_combo = ttk.Combobox(
            header, textvariable=self._structure_var,
            values=names, state="readonly", width=40,
            font=("Segoe UI", 10, "bold"),
        )
        self._structure_combo.pack(side=tk.LEFT)
        self._structure_combo.bind(
            "<<ComboboxSelected>>", self._on_structure_changed
        )

        self._structure_id_var = tk.StringVar(
            value=f"  (id {self.structure_id}, slot: {self.slot})"
        )
        ttk.Label(header, textvariable=self._structure_id_var,
                  foreground="gray").pack(side=tk.LEFT)
        ttk.Button(header, text="Refresh", command=self._kick_fetch).pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="Fetching orders…")
        ttk.Label(self, textvariable=self.status_var,
                  font=("Segoe UI", 8), foreground="gray").pack(
            fill=tk.X, padx=10, pady=(0, 4)
        )

        self._build_filter_row(self)

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.tab_current = ttk.Frame(notebook)
        self.tab_history = ttk.Frame(notebook)
        notebook.add(self.tab_current, text="Current Orders")
        notebook.add(self.tab_history, text="History (observed)")
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._notebook = notebook

        self._build_current_tab(self.tab_current)
        self._build_history_tab(self.tab_history)

    def _build_current_tab(self, parent):
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("side", "name", "type_id", "price", "qty", "issued")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        self.tree.heading("side", text="Side")
        self.tree.heading("name", text="Item")
        self.tree.heading("type_id", text="Type ID")
        self.tree.heading("price", text="Price")
        self.tree.heading("qty", text="Qty")
        self.tree.heading("issued", text="Issued (UTC)")
        self.tree.column("side", width=50, anchor=tk.CENTER)
        self.tree.column("name", width=300, anchor=tk.W)
        self.tree.column("type_id", width=80, anchor=tk.E)
        self.tree.column("price", width=130, anchor=tk.E)
        self.tree.column("qty", width=90, anchor=tk.E)
        self.tree.column("issued", width=140, anchor=tk.W)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.tag_configure("sell", foreground="#aa4444")
        self.tree.tag_configure("buy", foreground="#3a7a3a")

    def _build_history_tab(self, parent):
        self.history_summary_var = tk.StringVar(value="(no snapshots yet)")
        ttk.Label(parent, textvariable=self.history_summary_var,
                  font=("Segoe UI", 9)).pack(fill=tk.X, padx=4, pady=(6, 4))

        paned = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        # Top pane: items observed
        items_frame = ttk.LabelFrame(paned, text="Items observed at this structure")
        cols = ("type_id", "name", "fills", "volume", "avg", "range", "days")
        self.items_tree = ttk.Treeview(items_frame, columns=cols, show="headings",
                                       selectmode="browse", height=8)
        for col, label, w, anchor in [
            ("type_id", "Type ID", 80, tk.E),
            ("name", "Item", 260, tk.W),
            ("fills", "Fills", 70, tk.E),
            ("volume", "Vol Sold", 110, tk.E),
            ("avg", "Avg Price", 110, tk.E),
            ("range", "Min – Max", 160, tk.E),
            ("days", "Days", 60, tk.E),
        ]:
            self.items_tree.heading(col, text=label)
            self.items_tree.column(col, width=w, anchor=anchor)
        items_vsb = ttk.Scrollbar(items_frame, orient=tk.VERTICAL,
                                  command=self.items_tree.yview)
        self.items_tree.configure(yscrollcommand=items_vsb.set)
        self.items_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        items_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.items_tree.bind("<<TreeviewSelect>>", self._on_item_selected)
        paned.add(items_frame, weight=1)

        # Bottom pane: event trail + per-item summary
        trail_frame = ttk.LabelFrame(paned, text="Event trail (selected item)")
        self.trail_summary_var = tk.StringVar(value="Select an item above.")
        ttk.Label(trail_frame, textvariable=self.trail_summary_var,
                  font=("Segoe UI", 8), foreground="gray").pack(
            fill=tk.X, padx=4, pady=(4, 2)
        )

        trail_tree_frame = ttk.Frame(trail_frame)
        trail_tree_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        tcols = ("at", "kind", "order_id", "qty", "price")
        self.trail_tree = ttk.Treeview(trail_tree_frame, columns=tcols,
                                       show="headings", height=8)
        for col, label, w, anchor in [
            ("at", "Time (UTC)", 150, tk.W),
            ("kind", "Event", 110, tk.W),
            ("order_id", "Order ID", 130, tk.E),
            ("qty", "Qty", 100, tk.E),
            ("price", "Price", 120, tk.E),
        ]:
            self.trail_tree.heading(col, text=label)
            self.trail_tree.column(col, width=w, anchor=anchor)
        trail_vsb = ttk.Scrollbar(trail_tree_frame, orient=tk.VERTICAL,
                                  command=self.trail_tree.yview)
        self.trail_tree.configure(yscrollcommand=trail_vsb.set)
        self.trail_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        trail_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.trail_tree.tag_configure("listing", foreground="#777777")
        self.trail_tree.tag_configure("partial_fill", foreground="#3a7a3a")
        self.trail_tree.tag_configure("full_fill", foreground="#2c5d2c")
        self.trail_tree.tag_configure("expire", foreground="#aa4444")

        paned.add(trail_frame, weight=2)

    # ----------------------------------------------------------------- fetch

    def _kick_fetch(self):
        self.status_var.set("Fetching orders…")
        for row in self.tree.get_children():
            self.tree.delete(row)

        def worker():
            try:
                orders, _expires = fetch_structure_orders(
                    self.structure_id, self.auth, slot=self.slot
                )
                err = None
            except StructureAccessError as e:
                orders, err = None, e
            except Exception as e:
                orders, err = None, e

            # Phase 1 hook: contribute this fetch to the observed-history DB
            # so opening Browse Orders also feeds collection. Failures are
            # logged but never block the GUI update.
            if orders:
                try:
                    from history.structure_history import StructureHistoryDB
                    StructureHistoryDB.singleton().record_snapshot(
                        self.structure_id, orders
                    )
                except Exception as e:
                    print(f"[BrowseOrders] record_snapshot failed: {e}")

            submit(lambda o=orders, e=err: self._on_result(o, e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_result(self, orders, err):
        if err is not None:
            self.status_var.set(f"Fetch failed: {err}")
            self.set_current_orders([])
            self._refresh_history_view()
            self._apply_filters_and_render()
            return
        if not orders:
            self.status_var.set(
                "Fetch succeeded but returned zero orders — structure market is empty."
            )
            self.set_current_orders([])
            self._refresh_history_view()
            self._apply_filters_and_render()
            return

        type_ids = sorted({o["type_id"] for o in orders})
        if self._sde:
            self._type_names.update(self._sde.get_type_names_bulk(type_ids))

        self.set_current_orders(orders)

        n_sell = sum(1 for o in orders if not o.get("is_buy_order"))
        n_buy = len(orders) - n_sell
        self.status_var.set(
            f"{len(orders)} orders — {n_sell} sell, {n_buy} buy, "
            f"across {len(type_ids)} item types."
        )

        self._refresh_history_view()
        self._apply_filters_and_render()

    # --------------------------------------------------------------- history

    def _on_tab_changed(self, _event):
        # Lazy refresh — re-pull history view whenever user opens the tab so it
        # reflects the snapshot just recorded by the Refresh button.
        if self._notebook.index(self._notebook.select()) == 1:
            self._refresh_history_view()

    def _on_structure_changed(self, _event):
        """User picked a different structure from the header dropdown.

        Swaps the active structure_id/name, updates the window title and the
        gray "(id …, slot …)" sub-label, clears both trees, then kicks a fresh
        fetch. Filter chips persist — same player browsing, same intent.
        """
        chosen = self._structure_var.get()
        match = next(
            (cfg for _, cfg in self._structures
             if cfg.get("name", str(cfg["station_id"])) == chosen),
            None,
        )
        if not match or match["station_id"] == self.structure_id:
            return

        self.structure_id = match["station_id"]
        self.structure_name = match.get("name", str(self.structure_id))
        self.title(f"Browse Orders — {self.structure_name}")
        self._structure_id_var.set(
            f"  (id {self.structure_id}, slot: {self.slot})"
        )

        # Drop cached per-structure state so we don't leak data from the
        # previous structure into the new view. Filter chips intentionally
        # survive — selections like "Ammunition & Charges" are still
        # meaningful at the new structure.
        self._type_names = {}
        self.set_current_orders([])
        self.set_history_items([])
        self._apply_filters_and_render()

        self._kick_fetch()

    def _refresh_history_view(self):
        try:
            from history.structure_history import StructureHistoryDB
            db = StructureHistoryDB.singleton()
            summary = db.get_structure_summary(self.structure_id)
            items = db.get_items_observed(self.structure_id)
        except Exception as e:
            self.history_summary_var.set(f"History unavailable: {e}")
            return

        self.history_summary_var.set(_fmt_structure_summary(summary))

        # Resolve names for any type_ids we haven't already looked up.
        missing = [it["type_id"] for it in items
                   if it["type_id"] not in self._type_names]
        if missing and self._sde:
            self._type_names.update(self._sde.get_type_names_bulk(missing))

        self.set_history_items(items)
        self._apply_filters_and_render()

    def _on_item_selected(self, _event):
        sel = self.items_tree.selection()
        if not sel:
            return
        self._load_trail_for(int(sel[0]))

    def _load_trail_for(self, type_id: int):
        try:
            from history.structure_history import StructureHistoryDB
            events = StructureHistoryDB.singleton().get_event_trail(
                self.structure_id, type_id
            )
        except Exception as e:
            self.trail_summary_var.set(f"Trail unavailable: {e}")
            return

        for row in self.trail_tree.get_children():
            self.trail_tree.delete(row)

        for e in events:
            self.trail_tree.insert(
                "", tk.END,
                values=(
                    _fmt_at(e["at"]),
                    _KIND_LABELS.get(e["kind"], e["kind"]),
                    e["order_id"],
                    f"{e['qty']:,}",
                    f"{e['price']:,.2f}",
                ),
                tags=(e["kind"],),
            )

        self.trail_summary_var.set(
            _fmt_trail_summary(self._type_names.get(type_id, f"type {type_id}"),
                               events)
        )

    @staticmethod
    def _fmt_issued(raw):
        if not raw:
            return ""
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d %H:%M"
            )
        except Exception:
            return str(raw)[:16]


# =====================================================================
# Module-level formatters
# =====================================================================


def _fmt_at(raw: str) -> str:
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return raw[:16]


def _fmt_structure_summary(s: dict) -> str:
    snaps = s.get("snapshots", 0)
    types = s.get("types_observed", 0)
    days = s.get("days_with_fills", 0)
    if not snaps:
        return "(no snapshots yet — Refresh to record one)"
    first = _fmt_at(s.get("first_at") or "")
    last = _fmt_at(s.get("last_at") or "")
    return (
        f"{snaps:,} snapshots · {types:,} types observed · "
        f"{days} day(s) with inferred fills · "
        f"first {first} → last {last}"
    )


def _fmt_trail_summary(item_name: str, events: list[dict]) -> str:
    if not events:
        return f"{item_name} — no events recorded yet (need ≥2 snapshots)."

    listings = sum(1 for e in events if e["kind"] == "listing")
    partials = sum(1 for e in events if e["kind"] == "partial_fill")
    fulls = sum(1 for e in events if e["kind"] == "full_fill")
    expires = sum(1 for e in events if e["kind"] == "expire")

    prices = [e["price"] for e in events
              if e["kind"] in ("partial_fill", "full_fill")]
    price_range = ""
    if prices:
        price_range = f" · price range {min(prices):,.2f} – {max(prices):,.2f}"

    ttf_parts = []
    for e in events:
        if e["kind"] != "full_fill":
            continue
        try:
            issued_dt = datetime.fromisoformat(
                (e["issued"] or "").replace("Z", "+00:00")
            )
            at_dt = datetime.fromisoformat(e["at"])
            if issued_dt.tzinfo is None:
                issued_dt = issued_dt.replace(tzinfo=timezone.utc)
            if at_dt.tzinfo is None:
                at_dt = at_dt.replace(tzinfo=timezone.utc)
            ttf_parts.append((at_dt - issued_dt).total_seconds())
        except Exception:
            continue
    ttf_str = ""
    if ttf_parts:
        mean_s = sum(ttf_parts) / len(ttf_parts)
        ttf_str = f" · mean time-to-fill {_fmt_duration(mean_s)}"

    return (
        f"{item_name} — {listings} listings, {partials} partial fills, "
        f"{fulls} full fills, {expires} expires{price_range}{ttf_str}"
    )


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    if seconds < 86400:
        h = seconds / 3600
        return f"{h:.1f}h"
    d = seconds / 86400
    return f"{d:.1f}d"
