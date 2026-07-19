"""Discover Player Structures dialog.

Lets the user enumerate every player structure their character has active
orders at (per slot), see system + name + ID, and register the picked one
as a custom scanner hub in one click. The "Add to Scanner" path resolves
the parent region via public ESI (no auth) so order-fetch + history calls
both work afterward.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
from typing import Callable, Optional

from core.tk_queue import submit
from core.custom_stations import add_custom_station, get_custom_hub_key
from core.config import TRADE_HUBS
from esi.esi_auth import ESIAuth
from esi.esi_structures import (
    discover_accessible_structures, resolve_region_for_system,
    StructureAccessError,
)
from gui.gui_window_utils import fit_window


class DiscoverStructuresDialog(tk.Toplevel):
    def __init__(
        self,
        parent,
        on_station_added: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self.auth = ESIAuth.singleton()
        self.on_station_added = on_station_added

        self.title("Find Player Structures")
        self.transient(parent)

        self._create_widgets()
        fit_window(self, min_width=640)

    def _create_widgets(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Character:").pack(side=tk.LEFT, padx=(0, 6))

        self.slot_var = tk.StringVar(value="seller")
        seller_name = self.auth.seller.character_name if self.auth.seller else "(not logged in)"
        buyer_name = self.auth.buyer.character_name if self.auth.buyer else "(not logged in)"
        ttk.Radiobutton(
            top, text=f"Seller — {seller_name}",
            variable=self.slot_var, value="seller",
            state=tk.NORMAL if self.auth.seller else tk.DISABLED,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(
            top, text=f"Buyer — {buyer_name}",
            variable=self.slot_var, value="buyer",
            state=tk.NORMAL if self.auth.buyer else tk.DISABLED,
        ).pack(side=tk.LEFT, padx=4)

        self.discover_btn = ttk.Button(top, text="Discover", command=self._on_discover)
        self.discover_btn.pack(side=tk.RIGHT)

        # Status line
        self.status_var = tk.StringVar(value="Pick a character and click Discover.")
        ttk.Label(self, textvariable=self.status_var,
                  font=("Segoe UI", 8), foreground="gray").pack(
            fill=tk.X, padx=10, pady=(0, 4)
        )

        # Results tree
        tree_frame = ttk.Frame(self, padding=(10, 0, 10, 6))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("system_id", "name", "structure_id")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        self.tree.heading("system_id", text="System ID")
        self.tree.heading("name", text="Structure")
        self.tree.heading("structure_id", text="Structure ID")
        self.tree.column("system_id", width=90, anchor=tk.CENTER)
        self.tree.column("name", width=320, anchor=tk.W)
        self.tree.column("structure_id", width=160, anchor=tk.W)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # Bottom action bar
        bottom = ttk.Frame(self, padding=(10, 0, 10, 10))
        bottom.pack(fill=tk.X)
        self.add_btn = ttk.Button(
            bottom, text="Add to Scanner", command=self._on_add, state=tk.DISABLED
        )
        self.add_btn.pack(side=tk.LEFT)
        self.browse_btn = ttk.Button(
            bottom, text="Browse Orders (dev)",
            command=self._on_browse, state=tk.DISABLED,
        )
        self.browse_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.copy_btn = ttk.Button(
            bottom, text="Copy ID", command=self._on_copy, state=tk.DISABLED
        )
        self.copy_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side=tk.RIGHT)

    def _on_discover(self):
        slot = self.slot_var.get()
        self.discover_btn.configure(state=tk.DISABLED)
        self.status_var.set(f"Fetching {slot}'s active orders and resolving structures…")
        for row in self.tree.get_children():
            self.tree.delete(row)

        def worker():
            try:
                structures = discover_accessible_structures(self.auth, slot=slot)
                err = None
            except StructureAccessError as e:
                structures, err = None, e
            except Exception as e:
                structures, err = None, e
            submit(lambda s=structures, e=err: self._on_results(s, e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_results(self, structures, err):
        self.discover_btn.configure(state=tk.NORMAL)
        if err is not None:
            self.status_var.set(f"Failed: {err}")
            return
        if not structures:
            self.status_var.set(
                "No player structures found in this character's active orders."
            )
            return
        for info in structures:
            self.tree.insert(
                "", tk.END,
                values=(info.solar_system_id, info.name, info.structure_id),
            )
        self.status_var.set(f"Found {len(structures)} structure(s). Click a row, then Copy ID.")

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        enabled = tk.NORMAL if sel else tk.DISABLED
        self.copy_btn.configure(state=enabled)
        self.add_btn.configure(state=enabled)
        self.browse_btn.configure(state=enabled)

    def _on_browse(self):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        if len(values) < 3:
            return
        try:
            structure_id = int(values[2])
        except (TypeError, ValueError):
            self.status_var.set("Selected row has invalid structure ID.")
            return
        name = str(values[1])
        from gui.gui_browse_orders import BrowseStructureOrdersDialog
        BrowseStructureOrdersDialog(
            parent=self, structure_id=structure_id,
            structure_name=name, slot=self.slot_var.get(),
        )

    def _on_copy(self):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        if len(values) < 3:
            return
        structure_id = str(values[2])
        self.clipboard_clear()
        self.clipboard_append(structure_id)
        self.update()  # ensures clipboard is committed before dialog might close
        self.status_var.set(f"Copied {structure_id} to clipboard.")

    def _on_add(self):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        if len(values) < 3:
            return
        try:
            system_id = int(values[0])
            structure_id = int(values[2])
        except (TypeError, ValueError):
            self.status_var.set("Selected row has invalid IDs.")
            return
        name = str(values[1])

        hub_key = get_custom_hub_key(structure_id)
        if hub_key in TRADE_HUBS:
            messagebox.showinfo(
                "Already Added",
                f"'{name}' is already in your scanner hubs.",
                parent=self,
            )
            return

        self.add_btn.configure(state=tk.DISABLED)
        self.copy_btn.configure(state=tk.DISABLED)
        self.status_var.set(f"Resolving region for {name}…")

        def worker():
            try:
                region_id = resolve_region_for_system(system_id)
            except Exception as e:
                submit(lambda err=e: self._on_add_failed(err))
                return
            submit(lambda r=region_id: self._on_add_resolved(
                structure_id, name, system_id, r
            ))

        threading.Thread(target=worker, daemon=True).start()

    def _on_add_resolved(self, structure_id, name, system_id, region_id):
        try:
            added_key = add_custom_station(
                {
                    "station_id": structure_id,
                    "name": name,
                    "system_id": system_id,
                    "region_id": region_id,
                    "corp_id": None,
                },
                in_stock_market=False,
                station_type="structure",
            )
        except Exception as e:
            self._on_add_failed(e)
            return

        if self.on_station_added:
            try:
                self.on_station_added(added_key)
            except Exception as e:
                print(f"[DiscoverStructures] on_station_added callback raised: {e}")

        self.status_var.set(f"Added '{name}' to scanner hubs.")
        self._on_select()

    def _on_add_failed(self, err):
        self.status_var.set(f"Add failed: {err}")
        self._on_select()
