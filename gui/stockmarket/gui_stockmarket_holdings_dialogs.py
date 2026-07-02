"""Dialogs for Stock Market Holdings - manual purchase/sale recording."""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

from core.calculate import format_isk
from gui.gui_window_utils import fit_window


def parse_float(value: str, default: float = 0.0) -> float:
    """Parse a string to float, handling commas."""
    try:
        return float(value.strip().replace(",", ""))
    except ValueError:
        return default


def parse_int(value: str, default: int = 0) -> int:
    """Parse a string to int, handling commas."""
    try:
        return int(value.strip().replace(",", ""))
    except ValueError:
        return default


class RecordPurchaseDialog:
    """Dialog to manually record a purchase for a holding."""
    
    def __init__(
        self,
        parent: tk.Widget,
        type_id: int,
        type_name: str,
        current_qty: int,
        current_avg_cost: float,
        on_save: Callable[[int, int, float], None]
    ):
        """
        Args:
            parent: Parent widget
            type_id: Item type ID
            type_name: Item name
            current_qty: Current quantity held
            current_avg_cost: Current average cost
            on_save: Callback(type_id, quantity, price_per_unit)
        """
        self.type_id = type_id
        self.type_name = type_name
        self.current_qty = current_qty
        self.current_avg_cost = current_avg_cost
        self.on_save = on_save
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Record Purchase: {type_name}")
        self.dialog.transient(parent.winfo_toplevel())

        self._create_widgets()
        fit_window(self.dialog, min_width=320)
        self.dialog.grab_set()
    
    def _create_widgets(self):
        frame = ttk.Frame(self.dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Item name
        ttk.Label(
            frame, text=self.type_name,
            font=("Segoe UI", 11, "bold")
        ).pack(anchor=tk.W, pady=(0, 5))
        
        # Current holdings info
        info_text = f"Current: {self.current_qty:,} @ {format_isk(self.current_avg_cost)} avg"
        ttk.Label(frame, text=info_text, foreground="gray").pack(anchor=tk.W, pady=(0, 10))
        
        # Quantity
        qty_frame = ttk.Frame(frame)
        qty_frame.pack(fill=tk.X, pady=5)
        ttk.Label(qty_frame, text="Quantity:", width=12).pack(side=tk.LEFT)
        self.qty_entry = ttk.Entry(qty_frame, width=15)
        self.qty_entry.pack(side=tk.LEFT, padx=5)
        self.qty_entry.focus_set()
        
        # Price per unit
        price_frame = ttk.Frame(frame)
        price_frame.pack(fill=tk.X, pady=5)
        ttk.Label(price_frame, text="Price/Unit:", width=12).pack(side=tk.LEFT)
        self.price_entry = ttk.Entry(price_frame, width=15)
        self.price_entry.pack(side=tk.LEFT, padx=5)
        
        # Preview
        self.preview_label = ttk.Label(frame, text="", foreground="blue")
        self.preview_label.pack(anchor=tk.W, pady=10)
        
        # Bind for live preview
        self.qty_entry.bind("<KeyRelease>", self._update_preview)
        self.price_entry.bind("<KeyRelease>", self._update_preview)
        
        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Record", command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)
    
    def _update_preview(self, event=None):
        """Update preview of new average cost."""
        qty = parse_int(self.qty_entry.get())
        price = parse_float(self.price_entry.get())
        
        if qty > 0 and price > 0:
            # Calculate new weighted average
            old_value = self.current_qty * self.current_avg_cost
            new_value = qty * price
            total_qty = self.current_qty + qty
            new_avg = (old_value + new_value) / total_qty if total_qty > 0 else price
            
            total_cost = qty * price
            self.preview_label.configure(
                text=f"Total: {format_isk(total_cost)} | New avg: {format_isk(new_avg)}"
            )
        else:
            self.preview_label.configure(text="")
    
    def _save(self):
        qty = parse_int(self.qty_entry.get())
        price = parse_float(self.price_entry.get())
        
        if qty <= 0:
            messagebox.showwarning("Invalid Input", "Quantity must be positive.")
            return
        
        if price <= 0:
            messagebox.showwarning("Invalid Input", "Price must be positive.")
            return
        
        self.on_save(self.type_id, qty, price)
        self.dialog.destroy()


class RecordSaleDialog:
    """Dialog to manually record a sale for a holding."""
    
    def __init__(
        self,
        parent: tk.Widget,
        type_id: int,
        type_name: str,
        current_qty: int,
        current_avg_cost: float,
        on_save: Callable[[int, int, float], None]
    ):
        """
        Args:
            parent: Parent widget
            type_id: Item type ID
            type_name: Item name
            current_qty: Current quantity held
            current_avg_cost: Current average cost
            on_save: Callback(type_id, quantity, price_per_unit)
        """
        self.type_id = type_id
        self.type_name = type_name
        self.current_qty = current_qty
        self.current_avg_cost = current_avg_cost
        self.on_save = on_save
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Record Sale: {type_name}")
        self.dialog.transient(parent.winfo_toplevel())

        self._create_widgets()
        fit_window(self.dialog, min_width=320)
        self.dialog.grab_set()
    
    def _create_widgets(self):
        frame = ttk.Frame(self.dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Item name
        ttk.Label(
            frame, text=self.type_name,
            font=("Segoe UI", 11, "bold")
        ).pack(anchor=tk.W, pady=(0, 5))
        
        # Current holdings info
        info_text = f"Holding: {self.current_qty:,} @ {format_isk(self.current_avg_cost)} avg"
        ttk.Label(frame, text=info_text, foreground="gray").pack(anchor=tk.W, pady=(0, 10))
        
        # Quantity
        qty_frame = ttk.Frame(frame)
        qty_frame.pack(fill=tk.X, pady=5)
        ttk.Label(qty_frame, text="Quantity:", width=12).pack(side=tk.LEFT)
        self.qty_entry = ttk.Entry(qty_frame, width=15)
        self.qty_entry.pack(side=tk.LEFT, padx=5)
        self.qty_entry.insert(0, str(self.current_qty))  # Default to all
        self.qty_entry.focus_set()
        
        # Sale price per unit
        price_frame = ttk.Frame(frame)
        price_frame.pack(fill=tk.X, pady=5)
        ttk.Label(price_frame, text="Sale Price:", width=12).pack(side=tk.LEFT)
        self.price_entry = ttk.Entry(price_frame, width=15)
        self.price_entry.pack(side=tk.LEFT, padx=5)
        
        # Preview
        self.preview_label = ttk.Label(frame, text="", foreground="blue")
        self.preview_label.pack(anchor=tk.W, pady=5)
        self.profit_label = ttk.Label(frame, text="", font=("Segoe UI", 10, "bold"))
        self.profit_label.pack(anchor=tk.W, pady=2)
        
        # Bind for live preview
        self.qty_entry.bind("<KeyRelease>", self._update_preview)
        self.price_entry.bind("<KeyRelease>", self._update_preview)
        
        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Record", command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)
    
    def _update_preview(self, event=None):
        """Update preview of profit/loss."""
        qty = parse_int(self.qty_entry.get())
        price = parse_float(self.price_entry.get())
        
        if qty > 0 and price > 0:
            revenue = qty * price
            cost_basis = qty * self.current_avg_cost
            profit = revenue - cost_basis
            
            self.preview_label.configure(
                text=f"Revenue: {format_isk(revenue)} | Cost basis: {format_isk(cost_basis)}"
            )
            
            if profit >= 0:
                self.profit_label.configure(
                    text=f"Profit: +{format_isk(profit)}",
                    foreground="#006400"  # Green
                )
            else:
                self.profit_label.configure(
                    text=f"Loss: {format_isk(profit)}",
                    foreground="#8B0000"  # Red
                )
        else:
            self.preview_label.configure(text="")
            self.profit_label.configure(text="")
    
    def _save(self):
        qty = parse_int(self.qty_entry.get())
        price = parse_float(self.price_entry.get())
        
        if qty <= 0:
            messagebox.showwarning("Invalid Input", "Quantity must be positive.")
            return
        
        if qty > self.current_qty:
            messagebox.showwarning(
                "Invalid Quantity",
                f"Cannot sell more than you hold ({self.current_qty:,})."
            )
            return
        
        if price <= 0:
            messagebox.showwarning("Invalid Input", "Price must be positive.")
            return
        
        self.on_save(self.type_id, qty, price)
        self.dialog.destroy()


class HoldingDetailsDialog:
    """Dialog showing full holding details and transaction history."""
    
    def __init__(
        self,
        parent: tk.Widget,
        type_name: str,
        entry  # HoldingEntry
    ):
        self.type_name = type_name
        self.entry = entry
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Holding Details: {type_name}")
        self.dialog.transient(parent.winfo_toplevel())

        self._create_widgets()
        fit_window(self.dialog, min_width=350)
        self.dialog.grab_set()
    
    def _create_widgets(self):
        frame = ttk.Frame(self.dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Item name
        ttk.Label(
            frame, text=self.type_name,
            font=("Segoe UI", 12, "bold")
        ).pack(anchor=tk.W, pady=(0, 10))
        
        # Position info
        pos_frame = ttk.LabelFrame(frame, text="Current Position", padding=10)
        pos_frame.pack(fill=tk.X, pady=5)
        
        self._add_row(pos_frame, "Quantity Held:", f"{self.entry.quantity_held:,}")
        self._add_row(pos_frame, "Average Cost:", format_isk(self.entry.average_cost))
        position_value = self.entry.quantity_held * self.entry.average_cost
        self._add_row(pos_frame, "Position Value:", format_isk(position_value))
        
        # Cumulative stats
        stats_frame = ttk.LabelFrame(frame, text="Lifetime Stats", padding=10)
        stats_frame.pack(fill=tk.X, pady=5)
        
        self._add_row(stats_frame, "Total Bought:", f"{self.entry.total_bought:,}")
        self._add_row(stats_frame, "Total Sold:", f"{self.entry.total_sold:,}")
        self._add_row(stats_frame, "Total Buy Cost:", format_isk(self.entry.total_buy_cost))
        self._add_row(stats_frame, "Total Revenue:", format_isk(self.entry.total_sell_revenue))
        
        # Profit with color
        profit_row = ttk.Frame(stats_frame)
        profit_row.pack(fill=tk.X, pady=2)
        ttk.Label(profit_row, text="Realized Profit:", width=14).pack(side=tk.LEFT)
        
        profit_color = "#006400" if self.entry.realized_profit >= 0 else "#8B0000"
        profit_text = f"+{format_isk(self.entry.realized_profit)}" if self.entry.realized_profit >= 0 else format_isk(self.entry.realized_profit)
        ttk.Label(
            profit_row, text=profit_text,
            foreground=profit_color,
            font=("Segoe UI", 9, "bold")
        ).pack(side=tk.LEFT)
        
        # Timestamps
        if self.entry.date_added:
            ttk.Label(
                frame,
                text=f"Added: {self.entry.date_added[:10]}",
                foreground="gray"
            ).pack(anchor=tk.W, pady=(10, 0))
        
        # Close button
        ttk.Button(
            frame, text="Close",
            command=self.dialog.destroy
        ).pack(pady=15)
    
    def _add_row(self, parent, label: str, value: str):
        """Add a label-value row."""
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=label, width=14).pack(side=tk.LEFT)
        ttk.Label(row, text=value).pack(side=tk.LEFT)
