"""Dialog classes for Trade Tracking tab - record buy/list/sale, details."""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

from analytics.trade_tracker import TrackedTrade
from core.calculate import format_isk, get_skill_summary, calculate_sales_tax, TradingSkills
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


class RecordBuyDialog:
    """Dialog to manually record buy info for a pending trade."""
    
    def __init__(self, parent: tk.Widget, trade: TrackedTrade, 
                 on_save: Callable[[str, float, int], None]):
        self.trade = trade
        self.on_save = on_save
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Record Buy: {trade.type_name}")
        self.dialog.transient(parent.winfo_toplevel())

        self._create_widgets()
        fit_window(self.dialog, min_width=350)
        self.dialog.grab_set()

    def _create_widgets(self):
        ttk.Label(self.dialog, text=self.trade.type_name,
                  font=("Segoe UI", 11, "bold")).pack(pady=10)

        # Price
        price_frame = ttk.Frame(self.dialog)
        price_frame.pack(fill=tk.X, padx=20, pady=5)
        ttk.Label(price_frame, text="Buy Price:", width=12).pack(side=tk.LEFT)
        self.price_entry = ttk.Entry(price_frame, width=20)
        self.price_entry.pack(side=tk.LEFT, padx=5)
        if self.trade.buy_price:
            self.price_entry.insert(0, f"{self.trade.buy_price:,.2f}")
        
        # Quantity
        qty_frame = ttk.Frame(self.dialog)
        qty_frame.pack(fill=tk.X, padx=20, pady=5)
        ttk.Label(qty_frame, text="Quantity:", width=12).pack(side=tk.LEFT)
        self.qty_entry = ttk.Entry(qty_frame, width=20)
        self.qty_entry.pack(side=tk.LEFT, padx=5)
        self.qty_entry.insert(0, str(self.trade.quantity))
        
        # Hub selection
        hub_frame = ttk.Frame(self.dialog)
        hub_frame.pack(fill=tk.X, padx=20, pady=5)
        ttk.Label(hub_frame, text="Buy Hub:", width=12).pack(side=tk.LEFT)
        self.hub_var = tk.StringVar(value=self.trade.buy_hub or "amarr")
        hub_combo = ttk.Combobox(hub_frame, textvariable=self.hub_var, width=17,
                                  values=["amarr", "jita", "dodixie", "hek", "rens"])
        hub_combo.pack(side=tk.LEFT, padx=5)
        
        # Buttons
        btn_frame = ttk.Frame(self.dialog)
        btn_frame.pack(pady=15)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)
    
    def _save(self):
        price = parse_float(self.price_entry.get())
        qty = parse_int(self.qty_entry.get())
        hub = self.hub_var.get()
        
        if price > 0 and qty > 0:
            self.on_save(hub, price, qty)
            self.dialog.destroy()
        else:
            messagebox.showwarning("Invalid Input", "Please enter valid price and quantity.")


class RecordListingDialog:
    """Dialog to record listing a trade for sale."""
    
    def __init__(self, parent: tk.Widget, trade: TrackedTrade, 
                 on_save: Callable[[str, float, int], None],
                 skills: TradingSkills = None):
        self.trade = trade
        self.on_save = on_save
        self.skills = skills
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"List for Sale: {trade.type_name}")
        self.dialog.transient(parent.winfo_toplevel())

        self._create_widgets()
        fit_window(self.dialog, min_width=350)
        self.dialog.grab_set()

    def _create_widgets(self):
        ttk.Label(self.dialog, text=self.trade.type_name,
                  font=("Segoe UI", 11, "bold")).pack(pady=10)

        # Show buy info
        info_text = f"Bought: {self.trade.quantity:,} @ {format_isk(self.trade.buy_price)}"
        ttk.Label(self.dialog, text=info_text).pack(pady=5)
        
        # List Price
        price_frame = ttk.Frame(self.dialog)
        price_frame.pack(fill=tk.X, padx=20, pady=5)
        ttk.Label(price_frame, text="List Price:", width=12).pack(side=tk.LEFT)
        self.price_entry = ttk.Entry(price_frame, width=20)
        self.price_entry.pack(side=tk.LEFT, padx=5)
        if self.trade.target_sell:
            self.price_entry.insert(0, f"{self.trade.target_sell:,.2f}")
        
        # Quantity
        qty_frame = ttk.Frame(self.dialog)
        qty_frame.pack(fill=tk.X, padx=20, pady=5)
        ttk.Label(qty_frame, text="Quantity:", width=12).pack(side=tk.LEFT)
        self.qty_entry = ttk.Entry(qty_frame, width=20)
        self.qty_entry.pack(side=tk.LEFT, padx=5)
        self.qty_entry.insert(0, str(self.trade.quantity))
        
        # Hub selection
        hub_frame = ttk.Frame(self.dialog)
        hub_frame.pack(fill=tk.X, padx=20, pady=5)
        ttk.Label(hub_frame, text="Sell Hub:", width=12).pack(side=tk.LEFT)
        self.hub_var = tk.StringVar(value=self.trade.sell_hub or self.trade.buy_hub or "amarr")
        hub_combo = ttk.Combobox(hub_frame, textvariable=self.hub_var, width=17,
                                  values=["amarr", "jita", "dodixie", "hek", "rens"])
        hub_combo.pack(side=tk.LEFT, padx=5)
        
        # Buttons
        btn_frame = ttk.Frame(self.dialog)
        btn_frame.pack(pady=15)
        ttk.Button(btn_frame, text="List", command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)
    
    def _save(self):
        price = parse_float(self.price_entry.get())
        qty = parse_int(self.qty_entry.get())
        hub = self.hub_var.get()
        
        if price > 0 and qty > 0:
            self.on_save(hub, price, qty)
            self.dialog.destroy()
        else:
            messagebox.showwarning("Invalid Input", "Please enter valid price and quantity.")


class RecordRelistDialog:
    """Dialog to record relisting at a new price."""
    
    def __init__(self, parent: tk.Widget, trade: TrackedTrade, 
                 on_save: Callable[[float], None],
                 skills: TradingSkills = None):
        self.trade = trade
        self.on_save = on_save
        self.skills = skills
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Relist: {trade.type_name}")
        self.dialog.transient(parent.winfo_toplevel())

        self._create_widgets()
        fit_window(self.dialog, min_width=320)
        self.dialog.grab_set()

    def _create_widgets(self):
        ttk.Label(self.dialog, text=self.trade.type_name,
                  font=("Segoe UI", 11, "bold")).pack(pady=10)

        # Show current listing
        info_text = f"Currently listed: {self.trade.listed_quantity:,} @ {format_isk(self.trade.list_price)}"
        ttk.Label(self.dialog, text=info_text).pack(pady=5)
        
        # New Price
        price_frame = ttk.Frame(self.dialog)
        price_frame.pack(fill=tk.X, padx=20, pady=5)
        ttk.Label(price_frame, text="New Price:", width=12).pack(side=tk.LEFT)
        self.price_entry = ttk.Entry(price_frame, width=20)
        self.price_entry.pack(side=tk.LEFT, padx=5)
        self.price_entry.insert(0, f"{self.trade.list_price:,.2f}")
        
        # Buttons
        btn_frame = ttk.Frame(self.dialog)
        btn_frame.pack(pady=15)
        ttk.Button(btn_frame, text="Relist", command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)
    
    def _save(self):
        price = parse_float(self.price_entry.get())
        
        if price > 0:
            self.on_save(price)
            self.dialog.destroy()
        else:
            messagebox.showwarning("Invalid Input", "Please enter a valid price.")


class RecordSaleDialog:
    """Dialog to record a completed sale."""
    
    def __init__(self, parent: tk.Widget, trade: TrackedTrade, 
                 on_save: Callable[[float, int], None],
                 skills: TradingSkills = None):
        self.trade = trade
        self.on_save = on_save
        self.skills = skills
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Record Sale: {trade.type_name}")
        self.dialog.transient(parent.winfo_toplevel())

        self._create_widgets()
        fit_window(self.dialog, min_width=350)
        self.dialog.grab_set()

    def _create_widgets(self):
        ttk.Label(self.dialog, text=self.trade.type_name,
                  font=("Segoe UI", 11, "bold")).pack(pady=10)

        # Show listing info
        info_text = f"Listed: {self.trade.listed_quantity:,} @ {format_isk(self.trade.list_price)}"
        ttk.Label(self.dialog, text=info_text).pack(pady=5)
        
        # Sale Price
        price_frame = ttk.Frame(self.dialog)
        price_frame.pack(fill=tk.X, padx=20, pady=5)
        ttk.Label(price_frame, text="Sale Price:", width=12).pack(side=tk.LEFT)
        self.price_entry = ttk.Entry(price_frame, width=20)
        self.price_entry.pack(side=tk.LEFT, padx=5)
        self.price_entry.insert(0, f"{self.trade.list_price:,.2f}")
        
        # Quantity Sold
        qty_frame = ttk.Frame(self.dialog)
        qty_frame.pack(fill=tk.X, padx=20, pady=5)
        ttk.Label(qty_frame, text="Qty Sold:", width=12).pack(side=tk.LEFT)
        self.qty_entry = ttk.Entry(qty_frame, width=20)
        self.qty_entry.pack(side=tk.LEFT, padx=5)
        self.qty_entry.insert(0, str(self.trade.listed_quantity))
        
        # Buttons
        btn_frame = ttk.Frame(self.dialog)
        btn_frame.pack(pady=15)
        ttk.Button(btn_frame, text="Record Sale", command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)
    
    def _save(self):
        price = parse_float(self.price_entry.get())
        qty = parse_int(self.qty_entry.get())
        
        if price > 0 and qty > 0:
            self.on_save(price, qty)
            self.dialog.destroy()
        else:
            messagebox.showwarning("Invalid Input", "Please enter valid price and quantity.")


class TradeDetailsDialog:
    """Dialog showing full trade details and history."""
    
    def __init__(self, parent: tk.Widget, trade: TrackedTrade, skills: TradingSkills = None):
        self.trade = trade
        self.skills = skills
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Trade Details: {trade.type_name}")
        self.dialog.transient(parent.winfo_toplevel())

        self._create_widgets()
        fit_window(self.dialog, min_width=500)
        self.dialog.grab_set()
    
    def _create_widgets(self):
        # Header
        header = ttk.Frame(self.dialog)
        header.pack(fill=tk.X, padx=20, pady=10)
        
        ttk.Label(header, text=self.trade.type_name, 
                  font=("Segoe UI", 14, "bold")).pack(anchor=tk.W)
        ttk.Label(header, text=f"Status: {self.trade.status.value}",
                  font=("Segoe UI", 10)).pack(anchor=tk.W)
        
        # Notebook for sections
        notebook = ttk.Notebook(self.dialog)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Overview tab
        overview_frame = ttk.Frame(notebook)
        notebook.add(overview_frame, text="Overview")
        self._create_overview(overview_frame)
        
        # Fees tab
        fees_frame = ttk.Frame(notebook)
        notebook.add(fees_frame, text="Fees")
        self._create_fees(fees_frame)
        
        # History tab
        history_frame = ttk.Frame(notebook)
        notebook.add(history_frame, text="History")
        self._create_history(history_frame)
        
        # Close button
        ttk.Button(self.dialog, text="Close", command=self.dialog.destroy).pack(pady=10)
    
    def _create_overview(self, parent):
        """Create overview section."""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        # Buy info
        ttk.Label(frame, text="Purchase", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(0, 5))
        
        buy_info = ttk.Frame(frame)
        buy_info.pack(fill=tk.X, pady=2)
        
        if self.trade.buy_price:
            ttk.Label(buy_info, text=f"Price: {format_isk(self.trade.buy_price)}").pack(anchor=tk.W)
            ttk.Label(buy_info, text=f"Quantity: {self.trade.quantity:,}").pack(anchor=tk.W)
            total_cost = self.trade.buy_price * self.trade.quantity
            ttk.Label(buy_info, text=f"Total Cost: {format_isk(total_cost)}").pack(anchor=tk.W)
            if self.trade.buy_hub:
                ttk.Label(buy_info, text=f"Hub: {self.trade.buy_hub.title()}").pack(anchor=tk.W)
        else:
            ttk.Label(buy_info, text="Not yet purchased").pack(anchor=tk.W)
        
        # Separator
        ttk.Separator(frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        
        # Sell info
        ttk.Label(frame, text="Sale", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(0, 5))
        
        sell_info = ttk.Frame(frame)
        sell_info.pack(fill=tk.X, pady=2)
        
        if self.trade.list_price:
            ttk.Label(sell_info, text=f"List Price: {format_isk(self.trade.list_price)}").pack(anchor=tk.W)
            ttk.Label(sell_info, text=f"Listed Qty: {self.trade.listed_quantity:,}").pack(anchor=tk.W)
            if self.trade.sell_hub:
                ttk.Label(sell_info, text=f"Hub: {self.trade.sell_hub.title()}").pack(anchor=tk.W)
        
        if self.trade.sold_quantity > 0:
            ttk.Label(sell_info, text=f"Sold: {self.trade.sold_quantity:,} @ {format_isk(self.trade.actual_sell_price or self.trade.list_price)}").pack(anchor=tk.W)
        
        if self.trade.target_sell:
            ttk.Label(sell_info, text=f"Target: {format_isk(self.trade.target_sell)}").pack(anchor=tk.W)
        
        # Separator
        ttk.Separator(frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        
        # Profit summary
        ttk.Label(frame, text="Profit", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(0, 5))
        
        profit_info = ttk.Frame(frame)
        profit_info.pack(fill=tk.X, pady=2)
        
        if self.trade.realized_profit is not None:
            ttk.Label(profit_info, text=f"Realized: {format_isk(self.trade.realized_profit)}").pack(anchor=tk.W)
        
        if self.trade.projected_profit is not None:
            ttk.Label(profit_info, text=f"Projected: {format_isk(self.trade.projected_profit)}").pack(anchor=tk.W)
    
    def _create_fees(self, parent):
        """Create fees breakdown section."""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        # Skills info
        if self.skills:
            ttk.Label(frame, text="Character Skills", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(0, 5))
            ttk.Label(frame, text=get_skill_summary(self.skills)).pack(anchor=tk.W)
            ttk.Separator(frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        
        # Fee breakdown
        ttk.Label(frame, text="Fee Breakdown", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(0, 5))
        
        fee_info = ttk.Frame(frame)
        fee_info.pack(fill=tk.X, pady=2)
        
        if self.trade.broker_fee_buy:
            ttk.Label(fee_info, text=f"Buy Broker Fee: {format_isk(self.trade.broker_fee_buy)}").pack(anchor=tk.W)
        
        if self.trade.broker_fee_sell:
            ttk.Label(fee_info, text=f"Sell Broker Fee: {format_isk(self.trade.broker_fee_sell)}").pack(anchor=tk.W)
        
        if self.trade.sales_tax:
            ttk.Label(fee_info, text=f"Sales Tax: {format_isk(self.trade.sales_tax)}").pack(anchor=tk.W)
        
        total_fees = (self.trade.broker_fee_buy or 0) + (self.trade.broker_fee_sell or 0) + (self.trade.sales_tax or 0)
        if self.trade.relist_count > 0:
            relist_fees = self.trade.total_relist_fees or 0
            ttk.Label(fee_info, text=f"Relist Fees ({self.trade.relist_count}x): {format_isk(relist_fees)}").pack(anchor=tk.W)
            total_fees += relist_fees
        
        ttk.Separator(fee_info, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=5)
        ttk.Label(fee_info, text=f"Total Fees: {format_isk(total_fees)}", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
    
    def _create_history(self, parent):
        """Create history section."""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # History listbox with scrollbar
        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        listbox = tk.Listbox(frame, yscrollcommand=scrollbar.set, font=("Consolas", 9))
        listbox.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)
        
        # Add history entries
        for entry in self.trade.history:
            timestamp = entry.get("timestamp", "")
            action = entry.get("action", "")
            details = entry.get("details", "")
            
            # Format timestamp
            if timestamp:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(timestamp)
                    timestamp = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    pass
            
            line = f"{timestamp} | {action}"
            if details:
                line += f" | {details}"
            
            listbox.insert(tk.END, line)
