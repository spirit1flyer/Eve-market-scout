"""Reusable panel components for the Trade Tracking tab."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, List

from core.calculate import (
    TradingSkills, DEFAULT_SKILLS, format_isk,
    get_sales_tax_rate, get_broker_fee_rate
)
from core.calculate_trades import calculate_trade_fees, get_profit_trends


def _trends_from_inventory(entries) -> dict:
    """Compute day/week/month/year net profit trends from InventoryEntry data.

    Per-sale realized profit (revenue - cost_basis - sales_tax) bucketed by
    sold_at, minus broker/listing fees bucketed by the time they were paid
    (listing creation + each relist). This matches the cash-flow definition
    used in the Totals panel: Net Profit = Revenue - Cost of Sold - All Fees.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cutoffs = {
        "day": now - timedelta(days=1),
        "week": now - timedelta(days=7),
        "month": now - timedelta(days=30),
        "year": now - timedelta(days=365),
    }
    totals = {k: 0.0 for k in cutoffs}

    def _parse(ts: str):
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return t
        except (ValueError, TypeError):
            return None

    for entry in entries:
        for sale in entry.sales:
            sold = _parse(sale.sold_at)
            if sold is None:
                continue
            profit = (sale.sell_price * sale.quantity) - sale.cost_basis - sale.sales_tax
            for period, cutoff in cutoffs.items():
                if sold >= cutoff:
                    totals[period] += profit
        # Initial broker fee per listing, bucketed by listed_at
        for listing in entry.active_listings:
            paid_at = _parse(listing.listed_at)
            if paid_at is None or not listing.broker_fee:
                continue
            for period, cutoff in cutoffs.items():
                if paid_at >= cutoff:
                    totals[period] -= listing.broker_fee
        # Relist fees, bucketed by their timestamp
        for relist in entry.relists:
            paid_at = _parse(relist.timestamp)
            if paid_at is None or not relist.fee:
                continue
            for period, cutoff in cutoffs.items():
                if paid_at >= cutoff:
                    totals[period] -= relist.fee
    return totals


class SummaryPanel:
    """Left panel showing wallet balance, totals, and profit trends."""
    
    def __init__(self, parent: ttk.Frame):
        """
        Args:
            parent: Parent frame to pack into
        """
        self.frame = ttk.LabelFrame(parent, text="Summary", padding=10)
        self.frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))
        
        self._create_widgets()
    
    def _create_widgets(self):
        """Create all summary panel widgets."""
        # Wallet balance
        ttk.Label(self.frame, text="Wallet:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        self.balance_label = ttk.Label(self.frame, text="- ISK")
        self.balance_label.pack(anchor=tk.W, pady=(0, 10))
        
        # Trade stats
        ttk.Label(self.frame, text="Totals:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        
        stats_frame = ttk.Frame(self.frame)
        stats_frame.pack(anchor=tk.W, pady=5)
        
        stats = [
            ("Revenue:", "revenue_label"),
            ("Expenses:", "expenses_label"),
            ("Total Fees:", "fees_label"),
            ("Net Profit:", "profit_label"),
        ]
        
        for text, attr in stats:
            row = ttk.Frame(stats_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=text, width=12, anchor=tk.W).pack(side=tk.LEFT)
            label = ttk.Label(row, text="-", width=12, anchor=tk.E)
            label.pack(side=tk.RIGHT)
            setattr(self, attr, label)
        
        # Profit Trends section
        ttk.Separator(self.frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        ttk.Label(self.frame, text="Profit Trends:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        
        trends_frame = ttk.Frame(self.frame)
        trends_frame.pack(anchor=tk.W, pady=5)
        
        trends = [
            ("Day:", "trend_day_label"),
            ("Week:", "trend_week_label"),
            ("Month:", "trend_month_label"),
            ("Year:", "trend_year_label"),
        ]
        
        for text, attr in trends:
            row = ttk.Frame(trends_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=text, width=12, anchor=tk.W).pack(side=tk.LEFT)
            label = ttk.Label(row, text="-", width=12, anchor=tk.E)
            label.pack(side=tk.RIGHT)
            setattr(self, attr, label)
    
    def update_from_inventory(self, entries: List, wallet_balance: float):
        """Update summary values from scanner InventoryEntry objects.

        Args:
            entries: List of InventoryEntry (all of them, not just active)
            wallet_balance: Current wallet balance (0 if unavailable)
        """
        # Cash-flow accounting:
        #   Revenue   = total sales income
        #   Expenses  = cost basis of items actually sold (matched to revenue)
        #   Fees      = ALL broker fees + ALL sales tax (every fee ever paid)
        #   Net       = Revenue - Expenses - Fees  (algebra balances)
        # Cost of sold = total_buy_cost - remaining_cost_basis (FIFO-consumed lots).
        total_revenue = sum(e.total_revenue for e in entries)
        total_cost_of_sold = sum(
            e.total_buy_cost - e.remaining_cost_basis for e in entries
        )
        total_listing_fees = sum(e.total_listing_fees for e in entries)
        total_sales_tax = sum(e.total_sales_tax for e in entries)
        total_fees = total_listing_fees + total_sales_tax

        total_expenses = total_cost_of_sold
        net_profit = total_revenue - total_expenses - total_fees

        if wallet_balance > 0:
            self.balance_label.configure(text=format_isk(wallet_balance) + " ISK")

        self.revenue_label.configure(text=format_isk(total_revenue, short=True))
        self.expenses_label.configure(text=format_isk(total_expenses, short=True))
        self.fees_label.configure(text=format_isk(total_fees, short=True))

        self.profit_label.configure(
            text=format_isk(net_profit, short=True),
            foreground="#006400" if net_profit >= 0 else "#8B0000"
        )

        # Profit trends from inventory sales (ISO timestamp + profit per sale).
        trends = _trends_from_inventory(entries)
        for period, attr in [("day", "trend_day_label"), ("week", "trend_week_label"),
                             ("month", "trend_month_label"), ("year", "trend_year_label")]:
            value = trends[period]
            label = getattr(self, attr)
            label.configure(
                text=format_isk(value, short=True),
                foreground="#006400" if value >= 0 else "#8B0000"
            )

    def update(self, sold_trades: List, listed_trades: List,
               wallet_balance: float, skills: TradingSkills):
        """
        Update all summary values.
        
        Args:
            sold_trades: List of trades with status='sold'
            listed_trades: List of trades with status='listed'
            wallet_balance: Current wallet balance (0 if unavailable)
            skills: Current trading skills for fee calculations
        """
        # Calculate totals
        total_revenue = sum(t.sell_revenue for t in sold_trades)
        total_buy_cost = sum(t.buy_price * t.buy_quantity for t in sold_trades)
        
        total_fees_sold = sum(calculate_trade_fees(t, skills) for t in sold_trades)
        total_fees_listed = sum(calculate_trade_fees(t, skills) for t in listed_trades)
        total_fees = total_fees_sold + total_fees_listed
        
        total_expenses = total_buy_cost + total_fees_sold
        net_profit = total_revenue - total_expenses
        
        # Update labels
        if wallet_balance > 0:
            self.balance_label.configure(text=format_isk(wallet_balance) + " ISK")
        
        self.revenue_label.configure(text=format_isk(total_revenue, short=True))
        self.expenses_label.configure(text=format_isk(total_expenses, short=True))
        self.fees_label.configure(text=format_isk(total_fees, short=True))
        
        self.profit_label.configure(
            text=format_isk(net_profit, short=True),
            foreground="#006400" if net_profit >= 0 else "#8B0000"
        )
        
        # Update profit trends
        trends = get_profit_trends(sold_trades)
        
        for period, attr in [("day", "trend_day_label"), ("week", "trend_week_label"),
                             ("month", "trend_month_label"), ("year", "trend_year_label")]:
            value = trends[period]
            label = getattr(self, attr)
            label.configure(
                text=format_isk(value, short=True),
                foreground="#006400" if value >= 0 else "#8B0000"
            )


class StandingsBar:
    """
    Bar showing station/faction standings with manual override entries.
    
    Displays current standings, calculated fees, and refresh button.
    Allows manual fee overrides when ESI is not connected.
    """
    
    def __init__(self, parent: ttk.Frame, on_standings_changed: Callable[[float, float], None],
                 on_fees_changed: Callable[[Optional[float], Optional[float]], None] = None):
        """
        Args:
            parent: Parent frame to pack into
            on_standings_changed: Callback(station, faction) when user edits standings
            on_fees_changed: Callback(broker_fee, sales_tax) when user edits fees (None = use calculated)
        """
        self.on_standings_changed = on_standings_changed
        self.on_fees_changed = on_fees_changed
        
        self.frame = ttk.Frame(parent, padding=(5, 0, 5, 5))
        self.frame.pack(fill=tk.X)
        
        self._create_widgets()
    
    def _create_widgets(self):
        """Create standings bar widgets."""
        # Standings label
        ttk.Label(self.frame, text="Standings:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        
        # Station standing entry
        ttk.Label(self.frame, text="Station:").pack(side=tk.LEFT, padx=(10, 2))
        self.station_var = tk.StringVar(value="0.00")
        self.station_entry = ttk.Entry(self.frame, textvariable=self.station_var, width=6)
        self.station_entry.pack(side=tk.LEFT)
        self.station_entry.bind("<FocusOut>", self._on_standings_entry_changed)
        self.station_entry.bind("<Return>", self._on_standings_entry_changed)
        
        # Faction standing entry
        ttk.Label(self.frame, text="Faction:").pack(side=tk.LEFT, padx=(10, 2))
        self.faction_var = tk.StringVar(value="0.00")
        self.faction_entry = ttk.Entry(self.frame, textvariable=self.faction_var, width=6)
        self.faction_entry.pack(side=tk.LEFT)
        self.faction_entry.bind("<FocusOut>", self._on_standings_entry_changed)
        self.faction_entry.bind("<Return>", self._on_standings_entry_changed)
        
        # Separator
        ttk.Label(self.frame, text="  |  ").pack(side=tk.LEFT, padx=2)
        
        # Fees section - manual entry fields
        ttk.Label(self.frame, text="Fees:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(5, 5))
        
        # Broker fee entry
        ttk.Label(self.frame, text="Broker:").pack(side=tk.LEFT, padx=(2, 2))
        self.broker_var = tk.StringVar(value="")
        self.broker_entry = ttk.Entry(self.frame, textvariable=self.broker_var, width=6)
        self.broker_entry.pack(side=tk.LEFT)
        self.broker_entry.bind("<FocusOut>", self._on_fees_entry_changed)
        self.broker_entry.bind("<Return>", self._on_fees_entry_changed)
        ttk.Label(self.frame, text="%").pack(side=tk.LEFT)
        
        # Sales tax entry
        ttk.Label(self.frame, text="Tax:").pack(side=tk.LEFT, padx=(10, 2))
        self.tax_var = tk.StringVar(value="")
        self.tax_entry = ttk.Entry(self.frame, textvariable=self.tax_var, width=6)
        self.tax_entry.pack(side=tk.LEFT)
        self.tax_entry.bind("<FocusOut>", self._on_fees_entry_changed)
        self.tax_entry.bind("<Return>", self._on_fees_entry_changed)
        ttk.Label(self.frame, text="%").pack(side=tk.LEFT)
        
        # Total fees display (calculated)
        ttk.Label(self.frame, text="Total:").pack(side=tk.LEFT, padx=(10, 2))
        self.total_label = ttk.Label(self.frame, text="--%", font=("Segoe UI", 9))
        self.total_label.pack(side=tk.LEFT)
    
    def _on_standings_entry_changed(self, event=None):
        """Handle manual standing entry changes."""
        try:
            station = float(self.station_var.get())
            faction = float(self.faction_var.get())
            
            # Clamp values
            station = max(-10.0, min(10.0, station))
            faction = max(-10.0, min(10.0, faction))
            
            self.on_standings_changed(station, faction)
            
        except ValueError:
            pass  # Invalid input, ignore
    
    def _on_fees_entry_changed(self, event=None):
        """Handle manual fee entry changes."""
        if not self.on_fees_changed:
            return
        
        try:
            # Parse broker fee (empty = None = use calculated)
            broker_str = self.broker_var.get().strip()
            broker_fee = float(broker_str) if broker_str else None
            
            # Parse sales tax (empty = None = use calculated)
            tax_str = self.tax_var.get().strip()
            sales_tax = float(tax_str) if tax_str else None
            
            # Clamp if set
            if broker_fee is not None:
                broker_fee = max(0.0, min(10.0, broker_fee))
            if sales_tax is not None:
                sales_tax = max(0.0, min(10.0, sales_tax))
            
            self.on_fees_changed(broker_fee, sales_tax)
            
        except ValueError:
            pass  # Invalid input, ignore
    
    def update(self, skills: TradingSkills):
        """
        Update display from skills.
        
        Args:
            skills: Current trading skills (includes standings and optional manual fees)
        """
        self.station_var.set(f"{skills.station_standing:.2f}")
        self.faction_var.set(f"{skills.faction_standing:.2f}")
        
        # Get effective fee rates (will use manual if set, otherwise calculated)
        broker = get_broker_fee_rate(skills)
        tax = get_sales_tax_rate(skills)
        total = broker + tax
        
        # Update fee entry fields
        # If manual override is set, show it; otherwise show calculated value
        if skills.manual_broker_fee is not None:
            self.broker_var.set(f"{skills.manual_broker_fee:.2f}")
        else:
            self.broker_var.set(f"{broker:.2f}")
        
        if skills.manual_sales_tax is not None:
            self.tax_var.set(f"{skills.manual_sales_tax:.2f}")
        else:
            self.tax_var.set(f"{tax:.2f}")
        
        self.total_label.configure(text=f"{total:.2f}%")
