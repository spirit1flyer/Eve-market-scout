"""Profit & Loss tab for Stock Market holdings.

Displays financial summary for holdings at a specific hub:
- Total invested (buy cost + buy broker fees)
- Total sold (revenue before tax)
- Fee breakdown (buy/sell broker, modifications, sales tax)
- Net P&L
- Per-item breakdown with days held
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, Callable, Dict, List
from datetime import datetime

from analytics.stock_pnl import PnLManager, PnLEntry
from core.calculate import format_isk, load_cached_skills, get_broker_fee_rate, get_sales_tax_rate
from gui.gui_tree_utils import sort_treeview


class PnLPanel:
    """P&L sub-panel within a hub tab."""
    
    def __init__(
        self,
        parent: ttk.Frame,
        hub_key: str,
        set_status: Optional[Callable[[str], None]] = None,
    ):
        self.parent = parent
        self.hub_key = hub_key
        self.set_status = set_status or (lambda s: None)
        
        # P&L manager
        self.pnl = PnLManager(hub_key)
        
        # Sort state
        self.sort_column = "name"
        self.sort_reverse = False
        
        # Create UI
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill=tk.BOTH, expand=True)
        
        self._create_widgets()
        self.refresh_display()
    
    def _create_widgets(self):
        """Create panel widgets."""
        # Header note
        header = ttk.Label(
            self.frame,
            text="Holdings P&L - Tracks fees and profit for items in Holdings tab",
            font=("Segoe UI", 9, "italic"),
        )
        header.pack(anchor=tk.W, padx=10, pady=(10, 5))
        
        # Top section: Summary + Fee rates
        top_frame = ttk.Frame(self.frame)
        top_frame.pack(fill=tk.X, padx=10, pady=5)
        
        # Summary panel (left)
        self._create_summary_panel(top_frame)
        
        # Fee rates panel (right)
        self._create_rates_panel(top_frame)
        
        # Toolbar
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Button(
            toolbar,
            text="Refresh",
            command=self.refresh_display,
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(
            toolbar,
            text="Clear Selected",
            command=self._on_clear_selected,
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(
            toolbar,
            text="Clear All",
            command=self._on_clear_all,
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Frame(toolbar).pack(side=tk.LEFT, expand=True)  # Spacer
        
        self.count_label = ttk.Label(toolbar, text="0 items")
        self.count_label.pack(side=tk.RIGHT, padx=5)
        
        # Item breakdown treeview
        self._create_treeview()
    
    def _create_summary_panel(self, parent: ttk.Frame):
        """Create the summary stats panel."""
        summary_frame = ttk.LabelFrame(parent, text="Summary", padding=10)
        summary_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        
        # Stats rows
        stats = [
            ("Invested:", "invested_label"),
            ("Sold:", "sold_label"),
            ("Total Fees:", "fees_label"),
            ("Net P&L:", "pnl_label"),
        ]
        
        for text, attr in stats:
            row = ttk.Frame(summary_frame)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=text, width=12, anchor=tk.W).pack(side=tk.LEFT)
            label = ttk.Label(row, text="-", width=14, anchor=tk.E)
            label.pack(side=tk.RIGHT)
            setattr(self, attr, label)
        
        # Fee breakdown
        ttk.Separator(summary_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
        ttk.Label(summary_frame, text="Fee Breakdown:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        
        fee_stats = [
            ("Buy Broker:", "buy_fee_label"),
            ("Sell Broker:", "sell_fee_label"),
            ("Modifications:", "mod_fee_label"),
            ("Sales Tax:", "tax_label"),
        ]
        
        for text, attr in fee_stats:
            row = ttk.Frame(summary_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=text, width=12, anchor=tk.W).pack(side=tk.LEFT)
            label = ttk.Label(row, text="-", width=14, anchor=tk.E)
            label.pack(side=tk.RIGHT)
            setattr(self, attr, label)
    
    def _create_rates_panel(self, parent: ttk.Frame):
        """Create the fee rates display panel. Value labels are stored so
        _update_rates_panel can refresh them after the user runs Refresh Skills."""
        rates_frame = ttk.LabelFrame(parent, text="Current Fee Rates", padding=10)
        rates_frame.pack(side=tk.LEFT, fill=tk.Y)

        # Fee rate rows (label widgets stored for live update)
        self._rate_value_labels = {}
        for key, text in [
            ("broker", "Broker Fee:"),
            ("tax", "Sales Tax:"),
            ("total", "Total:"),
        ]:
            row = ttk.Frame(rates_frame)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=text, width=12, anchor=tk.W).pack(side=tk.LEFT)
            v = ttk.Label(row, text="-", width=10, anchor=tk.E)
            v.pack(side=tk.RIGHT)
            self._rate_value_labels[key] = v

        # Skill info (label widgets stored for live update)
        ttk.Separator(rates_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)

        self._skill_value_labels = {}
        for key, text in [
            ("broker_relations", "Broker Rel:"),
            ("accounting", "Accounting:"),
            ("advanced_broker_relations", "Adv Broker:"),
            ("station_standing", "Station:"),
            ("faction_standing", "Faction:"),
        ]:
            row = ttk.Frame(rates_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=text, width=12, anchor=tk.W, font=("Segoe UI", 8)).pack(side=tk.LEFT)
            v = ttk.Label(row, text="-", width=10, anchor=tk.E, font=("Segoe UI", 8))
            v.pack(side=tk.RIGHT)
            self._skill_value_labels[key] = v

        # Populate now
        self._update_rates_panel()

    def _update_rates_panel(self):
        """Re-read cached skills/standings and update the rates labels."""
        skills = load_cached_skills(self.hub_key, slot="seller")
        broker_rate = get_broker_fee_rate(skills)
        tax_rate = get_sales_tax_rate(skills)

        self._rate_value_labels["broker"].configure(text=f"{broker_rate:.2f}%")
        self._rate_value_labels["tax"].configure(text=f"{tax_rate:.2f}%")
        self._rate_value_labels["total"].configure(text=f"{broker_rate + tax_rate:.2f}%")

        self._skill_value_labels["broker_relations"].configure(text=str(skills.broker_relations))
        self._skill_value_labels["accounting"].configure(text=str(skills.accounting))
        self._skill_value_labels["advanced_broker_relations"].configure(text=str(skills.advanced_broker_relations))
        self._skill_value_labels["station_standing"].configure(text=f"{skills.station_standing:.2f}")
        self._skill_value_labels["faction_standing"].configure(text=f"{skills.faction_standing:.2f}")
    
    def _create_treeview(self):
        """Create the item breakdown treeview."""
        tree_frame = ttk.Frame(self.frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        
        columns = (
            "name", "days_held", "invested", "sold", 
            "buy_fees", "sell_fees", "mod_fees", "tax", "total_fees", "pnl"
        )
        
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        
        col_config = [
            ("name", "Item Name", 180, tk.W),
            ("days_held", "Days Held", 70, tk.E),
            ("invested", "Invested", 90, tk.E),
            ("sold", "Sold", 90, tk.E),
            ("buy_fees", "Buy Fees", 80, tk.E),
            ("sell_fees", "Sell Fees", 80, tk.E),
            ("mod_fees", "Mod Fees", 80, tk.E),
            ("tax", "Tax", 80, tk.E),
            ("total_fees", "Total Fees", 90, tk.E),
            ("pnl", "P&L", 100, tk.E),
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
        
        # Tags for P&L coloring
        self.tree.tag_configure("profit", foreground="#006400")
        self.tree.tag_configure("loss", foreground="#8B0000")
        
        # Context menu
        self.context_menu = tk.Menu(self.tree, tearoff=0)
        self.context_menu.add_command(label="Clear Entry", command=self._on_clear_selected)
        self.context_menu.add_command(label="Copy Name", command=self._on_copy_name)
        
        self.tree.bind("<Button-3>", self._on_right_click)
    
    def _sort_by(self, column: str):
        """Sort treeview by column."""
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        
        self.refresh_display()
    
    def _on_right_click(self, event):
        """Show context menu."""
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)
    
    def refresh_display(self):
        """Refresh the display with current P&L data."""
        # Re-read cached skills/standings so the rates panel reflects any
        # Refresh Skills the user has done since this panel was created.
        self._update_rates_panel()

        # Update summary
        summary = self.pnl.get_summary()
        
        self.invested_label.configure(text=format_isk(summary["total_invested"], short=True))
        self.sold_label.configure(text=format_isk(summary["total_sold"], short=True))
        self.fees_label.configure(text=format_isk(summary["total_fees"], short=True))
        
        net_pnl = summary["net_pnl"]
        self.pnl_label.configure(
            text=format_isk(net_pnl, short=True),
            foreground="#006400" if net_pnl >= 0 else "#8B0000"
        )
        
        # Fee breakdown
        self.buy_fee_label.configure(text=format_isk(summary["buy_broker_fees"], short=True))
        self.sell_fee_label.configure(text=format_isk(summary["sell_broker_fees"], short=True))
        self.mod_fee_label.configure(text=format_isk(summary["modification_fees"], short=True))
        self.tax_label.configure(text=format_isk(summary["sales_tax"], short=True))
        
        # Update count
        self.count_label.configure(text=f"{summary['item_count']} items")
        
        # Update treeview
        self.tree.delete(*self.tree.get_children())
        
        entries = self._get_sorted_entries()
        now = datetime.now()
        
        for entry in entries:
            # Calculate days held
            days_held = 0
            if entry.first_activity:
                try:
                    first = datetime.fromisoformat(entry.first_activity)
                    days_held = (now - first).days
                except (ValueError, TypeError):
                    pass
            
            # Per-item realized P&L: COGS-based (see PnLEntry.realized_pnl) —
            # unsold inventory is not a loss, escrow is not a cost.
            item_pnl = entry.realized_pnl
            
            tag = "profit" if item_pnl >= 0 else "loss"
            
            values = (
                entry.type_name,
                days_held,
                format_isk(entry.total_invested, short=True),
                format_isk(entry.total_sold_value, short=True),
                format_isk(entry.buy_broker_fees, short=True),
                format_isk(entry.sell_broker_fees, short=True),
                format_isk(entry.modification_fees, short=True),
                format_isk(entry.sales_tax_paid, short=True),
                format_isk(entry.total_fees, short=True),
                format_isk(item_pnl, short=True),
            )
            
            self.tree.insert("", tk.END, iid=str(entry.type_id), values=values, tags=(tag,))
    
    def _get_sorted_entries(self) -> List[PnLEntry]:
        """Get entries sorted by current column."""
        entries = self.pnl.get_all_entries()
        now = datetime.now()
        
        def get_sort_key(entry: PnLEntry):
            if self.sort_column == "name":
                return entry.type_name.lower()
            elif self.sort_column == "days_held":
                if entry.first_activity:
                    try:
                        first = datetime.fromisoformat(entry.first_activity)
                        return (now - first).days
                    except (ValueError, TypeError):
                        pass
                return 0
            elif self.sort_column == "invested":
                return entry.total_invested
            elif self.sort_column == "sold":
                return entry.total_sold_value
            elif self.sort_column == "buy_fees":
                return entry.buy_broker_fees
            elif self.sort_column == "sell_fees":
                return entry.sell_broker_fees
            elif self.sort_column == "mod_fees":
                return entry.modification_fees
            elif self.sort_column == "tax":
                return entry.sales_tax_paid
            elif self.sort_column == "total_fees":
                return entry.total_fees
            elif self.sort_column == "pnl":
                return entry.realized_pnl
            return 0
        
        return sorted(entries, key=get_sort_key, reverse=self.sort_reverse)
    
    def _on_clear_selected(self):
        """Clear P&L data for selected items."""
        selected = self.tree.selection()
        if not selected:
            return
        
        if len(selected) > 1:
            msg = f"Clear P&L data for {len(selected)} items?"
        else:
            item = self.tree.item(selected[0])
            name = item["values"][0]
            msg = f"Clear P&L data for {name}?"
        
        if not messagebox.askyesno("Confirm Clear", msg):
            return
        
        for iid in selected:
            self.pnl.clear_entry(int(iid))
        
        self.refresh_display()
        self.set_status(f"Cleared {len(selected)} P&L entries")
    
    def _on_clear_all(self):
        """Clear all P&L data for this hub."""
        summary = self.pnl.get_summary()
        if summary["item_count"] == 0:
            messagebox.showinfo("Nothing to Clear", "No P&L data to clear.")
            return
        
        msg = f"Clear ALL P&L data for this hub?\n\nThis will remove tracking for {summary['item_count']} items."
        if not messagebox.askyesno("Confirm Clear All", msg):
            return
        
        self.pnl.clear_all()
        self.refresh_display()
        self.set_status("Cleared all P&L data")
    
    def _on_copy_name(self):
        """Copy item name to clipboard."""
        selected = self.tree.selection()
        if not selected:
            return
        
        item = self.tree.item(selected[0])
        name = item["values"][0]
        
        self.frame.clipboard_clear()
        self.frame.clipboard_append(name)
        self.set_status(f"Copied: {name}")
    
    # === External API ===
    
    def get_pnl_manager(self) -> PnLManager:
        """Get the P&L manager for ESI integration."""
        return self.pnl
