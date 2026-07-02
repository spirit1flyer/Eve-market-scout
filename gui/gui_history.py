"""Trade history and wallet tracking tab for EVE Market Scout.

Future features:
- Record actual trade outcomes (buy price, sell price, fees)
- Track profit/loss per item and overall
- ESI wallet integration for automatic tracking
- Compare theoretical vs actual returns
"""

import tkinter as tk
from tkinter import ttk


class HistoryTabManager:
    """Manages the trade history and wallet tracking tab."""

    def __init__(self, notebook: ttk.Notebook):
        self.notebook = notebook
        self._create_tab()

    def _create_tab(self):
        """Create the history tab with placeholder content."""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="History")

        # Placeholder
        ttk.Label(
            frame,
            text="Trade History & Wallet Tracking\n\nComing Soon:\n- Record buy/sell transactions\n- Track actual profits vs projected\n- ESI wallet integration",
            font=("Segoe UI", 12),
            justify=tk.CENTER
        ).pack(expand=True)
