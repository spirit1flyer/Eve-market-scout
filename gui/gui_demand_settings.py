"""Demand/Restock thresholds dialog."""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

from scanning.scanner_demand import (
    load_demand_settings, save_demand_settings,
    DEFAULT_MIN_VELOCITY, DEFAULT_MAX_DAYS_OF_STOCK, DEFAULT_HEALTHY_DAYS,
    DEFAULT_MIN_MARGIN_PCT, DEFAULT_SORT_MODE,
)
from gui.gui_window_utils import fit_window, make_scrollable


class DemandSettingsDialog:
    """Modal popup for Demand/Restock thresholds."""

    def __init__(
        self,
        parent: tk.Misc,
        on_saved: Optional[Callable[[dict], None]] = None,
    ):
        self.parent = parent
        self.on_saved = on_saved

        current = load_demand_settings()

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Demand / Restock Thresholds")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        # Buttons pinned to window bottom (outside scroll area)
        btn_frame = ttk.Frame(self.dialog)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Reset to defaults", command=self._reset_defaults).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(btn_frame, text="Cancel", command=self.dialog.destroy).pack(side=tk.RIGHT)
        ttk.Separator(self.dialog, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        # Scrollable content area above the buttons.
        inner = make_scrollable(self.dialog)

        frame = ttk.Frame(inner, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        self.min_vel_var = tk.StringVar(value=str(current.get("min_velocity", DEFAULT_MIN_VELOCITY)))
        self.max_days_var = tk.StringVar(value=str(current.get("max_days_of_stock", DEFAULT_MAX_DAYS_OF_STOCK)))
        self.healthy_var = tk.StringVar(value=str(current.get("healthy_days_target", DEFAULT_HEALTHY_DAYS)))
        self.min_margin_var = tk.StringVar(value=str(current.get("min_margin_pct", DEFAULT_MIN_MARGIN_PCT)))
        self.sort_var = tk.StringVar(value=current.get("sort_mode", DEFAULT_SORT_MODE))

        self._row(frame, 0, "Min destination velocity (per day):", self.min_vel_var,
                  "Items selling fewer than this per day at dest are skipped.")
        self._row(frame, 1, "Max days of stock (gap gate):", self.max_days_var,
                  "Items with more than this many days of dest stock are skipped.")
        self._row(frame, 2, "Healthy days target (restock qty):", self.healthy_var,
                  "Restock qty fills dest up to this many days of stock.")
        self._row(frame, 3, "Min margin % at historical price:", self.min_margin_var,
                  "Drops rows that can't clear this margin at dest 7d/30d avg "
                  "(catches junk listings inflating target_sell).")

        ttk.Label(frame, text="Default sort:").grid(row=4, column=0, sticky="w", pady=(8, 0))
        sort_combo = ttk.Combobox(
            frame,
            textvariable=self.sort_var,
            state="readonly",
            values=["total_profit", "days_of_stock"],
            width=18,
        )
        sort_combo.grid(row=4, column=1, sticky="w", pady=(8, 0))

        frame.columnconfigure(1, weight=1)
        fit_window(self.dialog, min_width=380)

    def _row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar, tooltip: str):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=var, width=10)
        entry.grid(row=row, column=1, sticky="w", pady=2)
        # tooltip-ish help text under each row would crowd; rely on labels.
        _ = tooltip

    def _reset_defaults(self):
        self.min_vel_var.set(str(DEFAULT_MIN_VELOCITY))
        self.max_days_var.set(str(DEFAULT_MAX_DAYS_OF_STOCK))
        self.healthy_var.set(str(DEFAULT_HEALTHY_DAYS))
        self.min_margin_var.set(str(DEFAULT_MIN_MARGIN_PCT))
        self.sort_var.set(DEFAULT_SORT_MODE)

    def _save(self):
        try:
            min_vel = float(self.min_vel_var.get())
            max_days = float(self.max_days_var.get())
            healthy = float(self.healthy_var.get())
            min_margin = float(self.min_margin_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "All numeric fields must be numeric.", parent=self.dialog)
            return

        if min_vel < 0 or max_days < 0 or healthy <= 0 or min_margin < 0:
            messagebox.showerror("Invalid input",
                                 "Velocity / days / margin must be non-negative; "
                                 "healthy days must be > 0.",
                                 parent=self.dialog)
            return

        settings = {
            "min_velocity": min_vel,
            "max_days_of_stock": max_days,
            "healthy_days_target": healthy,
            "min_margin_pct": min_margin,
            "sort_mode": self.sort_var.get() or DEFAULT_SORT_MODE,
        }
        try:
            save_demand_settings(settings)
        except Exception as e:
            messagebox.showerror("Save failed", f"Could not save settings:\n{e}", parent=self.dialog)
            return

        if self.on_saved:
            try:
                self.on_saved(settings)
            except Exception as e:
                print(f"[DemandSettings] on_saved callback error: {e}")

        self.dialog.destroy()
