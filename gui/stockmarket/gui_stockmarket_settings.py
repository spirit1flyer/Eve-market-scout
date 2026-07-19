"""Stock Market settings for EVE Market Scout.

Handles persistence and UI for stock market configuration.
Settings are stored in the user's Roaming/AppData folder.
"""

import json
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from core.sound_manager import get_data_dir
from core.tk_queue import submit
from gui.gui_window_utils import fit_window, make_scrollable


# Settings file location
SETTINGS_FILE = get_data_dir() / "stockmarket_settings.json"

# Defaults
DEFAULT_BUY_PERCENTILE = 15
DEFAULT_SELL_PERCENTILE = 90
DEFAULT_FLOOR_OFFSET_PCT = 5.0
DEFAULT_PEAK_OFFSET_PCT = -5.0
DEFAULT_MIN_DAILY_VOLUME = 100  # Below this, skip material/LI compute entirely


@dataclass
class StockMarketSettings:
    """Stock market configuration settings."""

    # Archive location (None = use default in Roaming)
    archive_path: Optional[str] = None

    # Percentiles for profile calculation (requires rebuild)
    buy_percentile: int = DEFAULT_BUY_PERCENTILE
    sell_percentile: int = DEFAULT_SELL_PERCENTILE

    # Offsets for buy/sell targets (no rebuild needed)
    floor_offset_pct: float = DEFAULT_FLOOR_OFFSET_PCT
    peak_offset_pct: float = DEFAULT_PEAK_OFFSET_PCT

    # Compute-time volume gate. Items with avg_daily_volume below this are
    # excluded from material filter and leading indicators passes (saves time;
    # data is preserved, just not analyzed). Requires next scan to take effect.
    min_daily_volume: int = DEFAULT_MIN_DAILY_VOLUME

    # Last active hub tab (restored on launch)
    active_hub_key: Optional[str] = None

    # Master on/off switch for all Stock Market auto-processing (cold-start
    # builds, ESI hub bursts, holdings freshen, material filter, leading
    # indicators). When False the tab shows an "off" overlay and skips every
    # automatic data pull/compute. Persists across sessions so a disabled
    # install launches fast (no cold-start worker). Scanner + contracts are
    # unaffected. Default True = current behavior.
    stock_market_enabled: bool = True

    def get_archive_path(self) -> Path:
        """Get archive path, using default if not set."""
        if self.archive_path:
            return Path(self.archive_path)
        return get_data_dir() / "history-archive"
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "StockMarketSettings":
        """Create from dictionary."""
        return cls(
            archive_path=data.get("archive_path"),
            buy_percentile=data.get("buy_percentile", DEFAULT_BUY_PERCENTILE),
            sell_percentile=data.get("sell_percentile", DEFAULT_SELL_PERCENTILE),
            floor_offset_pct=data.get("floor_offset_pct", DEFAULT_FLOOR_OFFSET_PCT),
            peak_offset_pct=data.get("peak_offset_pct", DEFAULT_PEAK_OFFSET_PCT),
            min_daily_volume=data.get("min_daily_volume", DEFAULT_MIN_DAILY_VOLUME),
            active_hub_key=data.get("active_hub_key"),
            stock_market_enabled=data.get("stock_market_enabled", True),
        )


def load_settings() -> StockMarketSettings:
    """Load settings from disk, or return defaults."""
    if not SETTINGS_FILE.exists():
        return StockMarketSettings()
    
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return StockMarketSettings.from_dict(data)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[Settings] Error loading settings: {e}")
        return StockMarketSettings()


def save_settings(settings: StockMarketSettings) -> bool:
    """Save settings to disk.
    
    Returns:
        True if saved successfully, False otherwise.
    """
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, indent=2)
        return True
    except IOError as e:
        print(f"[Settings] Error saving settings: {e}")
        return False


class StockMarketSettingsDialog(tk.Toplevel):
    """Dialog for editing stock market settings."""
    
    def __init__(
        self,
        parent,
        settings: StockMarketSettings,
        on_save: Callable[[StockMarketSettings, bool], None]
    ):
        """Initialize settings dialog.
        
        Args:
            parent: Parent window
            settings: Current settings
            on_save: Callback when saved. Args: (new_settings, needs_rebuild)
        """
        super().__init__(parent)
        self.title("Stock Market Settings")
        self.transient(parent)

        self.settings = settings
        self.on_save = on_save
        self.original_buy_pct = settings.buy_percentile
        self.original_sell_pct = settings.sell_percentile

        self._create_widgets()
        fit_window(self, min_width=480)
        self.grab_set()
    
    def _create_widgets(self):
        """Create dialog widgets."""
        # Buttons pinned to window bottom (outside scroll area)
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Reset Defaults", command=self._reset_defaults).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Save", command=self._on_save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        # Scrollable content area above the buttons.
        inner = make_scrollable(self)

        # Archive section
        archive_frame = ttk.LabelFrame(inner, text="Archive Location", padding=10)
        archive_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(
            archive_frame,
            text="Location of everef history archive files.",
            font=("Segoe UI", 8),
            foreground="gray"
        ).pack(anchor=tk.W)

        path_row = ttk.Frame(archive_frame)
        path_row.pack(fill=tk.X, pady=5)

        self.archive_var = tk.StringVar(value=self.settings.archive_path or "(Default: Roaming folder)")
        self.archive_entry = ttk.Entry(path_row, textvariable=self.archive_var, width=45)
        self.archive_entry.pack(side=tk.LEFT, padx=(0, 5))

        ttk.Button(path_row, text="Browse", command=self._browse_archive).pack(side=tk.LEFT)
        ttk.Button(path_row, text="Reset", command=self._reset_archive).pack(side=tk.LEFT, padx=5)

        # Material Data section (for material cost analysis)
        material_frame = ttk.LabelFrame(inner, text="Material Data (Blueprint Analysis)", padding=10)
        material_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(
            material_frame,
            text="Blueprint/material data for cost analysis. Download once, update after patches.",
            font=("Segoe UI", 8),
            foreground="gray"
        ).pack(anchor=tk.W)

        material_row = ttk.Frame(material_frame)
        material_row.pack(fill=tk.X, pady=5)

        self.btn_update_materials = ttk.Button(
            material_row,
            text="Update Material Data",
            command=self._update_materials,
            width=20
        )
        self.btn_update_materials.pack(side=tk.LEFT, padx=(0, 10))

        self.material_status_var = tk.StringVar(value="")
        self.material_status_label = ttk.Label(
            material_row,
            textvariable=self.material_status_var,
            font=("Segoe UI", 8)
        )
        self.material_status_label.pack(side=tk.LEFT)

        # Material progress bar (hidden by default)
        self.material_progress_var = tk.DoubleVar(value=0)
        self.material_progress = ttk.Progressbar(
            material_frame,
            variable=self.material_progress_var,
            maximum=100
        )

        # Initialize material status
        self._update_material_status()

        # Percentiles section (requires rebuild)
        pct_frame = ttk.LabelFrame(inner, text="Percentiles (Requires Rebuild)", padding=10)
        pct_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(
            pct_frame,
            text="Changing these requires rebuilding profiles from archive data.",
            font=("Segoe UI", 8),
            foreground="#CC6600"
        ).pack(anchor=tk.W)

        pct_grid = ttk.Frame(pct_frame)
        pct_grid.pack(fill=tk.X, pady=5)

        # Buy percentile
        ttk.Label(pct_grid, text="Buy Percentile:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.buy_pct_var = tk.StringVar(value=str(self.settings.buy_percentile))
        buy_spin = ttk.Spinbox(pct_grid, from_=5, to=45, width=8, textvariable=self.buy_pct_var)
        buy_spin.grid(row=0, column=1, padx=10, pady=3)
        ttk.Label(pct_grid, text="(5-45, default 15)", foreground="gray").grid(row=0, column=2, sticky=tk.W)

        # Sell percentile
        ttk.Label(pct_grid, text="Sell Percentile:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.sell_pct_var = tk.StringVar(value=str(self.settings.sell_percentile))
        sell_spin = ttk.Spinbox(pct_grid, from_=55, to=95, width=8, textvariable=self.sell_pct_var)
        sell_spin.grid(row=1, column=1, padx=10, pady=3)
        ttk.Label(pct_grid, text="(55-95, default 90)", foreground="gray").grid(row=1, column=2, sticky=tk.W)

        # Offsets section (no rebuild)
        offset_frame = ttk.LabelFrame(inner, text="Target Offsets", padding=10)
        offset_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(
            offset_frame,
            text="Adjust buy/sell targets relative to calculated percentiles.",
            font=("Segoe UI", 8),
            foreground="gray"
        ).pack(anchor=tk.W)

        offset_grid = ttk.Frame(offset_frame)
        offset_grid.pack(fill=tk.X, pady=5)

        # Floor offset
        ttk.Label(offset_grid, text="Floor Offset %:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.floor_var = tk.StringVar(value=str(self.settings.floor_offset_pct))
        floor_spin = ttk.Spinbox(offset_grid, from_=-20, to=20, increment=0.5, width=8, textvariable=self.floor_var)
        floor_spin.grid(row=0, column=1, padx=10, pady=3)
        ttk.Label(offset_grid, text="(+ = above floor, - = below)", foreground="gray").grid(row=0, column=2, sticky=tk.W)

        # Peak offset
        ttk.Label(offset_grid, text="Peak Offset %:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.peak_var = tk.StringVar(value=str(self.settings.peak_offset_pct))
        peak_spin = ttk.Spinbox(offset_grid, from_=-20, to=20, increment=0.5, width=8, textvariable=self.peak_var)
        peak_spin.grid(row=1, column=1, padx=10, pady=3)
        ttk.Label(offset_grid, text="(+ = above peak, - = below)", foreground="gray").grid(row=1, column=2, sticky=tk.W)

        # Volume filter (compute-time gate)
        volume_frame = ttk.LabelFrame(inner, text="Volume Filter (Requires Rescan)", padding=10)
        volume_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(
            volume_frame,
            text="Items below this avg daily volume are skipped from material "
                 "filter and leading indicators. Data is preserved, just not "
                 "analyzed. Takes effect on next scan.",
            font=("Segoe UI", 8),
            foreground="gray",
            wraplength=440,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        volume_grid = ttk.Frame(volume_frame)
        volume_grid.pack(fill=tk.X, pady=5)

        ttk.Label(volume_grid, text="Min Daily Volume:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.min_volume_var = tk.StringVar(value=str(self.settings.min_daily_volume))
        ttk.Entry(volume_grid, textvariable=self.min_volume_var, width=10).grid(row=0, column=1, padx=10, pady=3)
        ttk.Label(volume_grid, text=f"(units/day, default {DEFAULT_MIN_DAILY_VOLUME})",
                  foreground="gray").grid(row=0, column=2, sticky=tk.W)

        # Filters note
        filter_note = ttk.LabelFrame(inner, text="Filtering", padding=10)
        filter_note.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(
            filter_note,
            text="Profitability and trend filters are in the Stock Market 'Filters' dialog.",
            font=("Segoe UI", 8),
            foreground="gray"
        ).pack(anchor=tk.W)

        # Legend
        legend_frame = ttk.Frame(filter_note)
        legend_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(legend_frame, text="Row colors: ", font=("Segoe UI", 8)).pack(side=tk.LEFT)

        # Red indicator
        red_lbl = tk.Label(legend_frame, text=" Down ", bg="#DC143C", fg="white", font=("Segoe UI", 8))
        red_lbl.pack(side=tk.LEFT, padx=2)

        # Yellow indicator
        yellow_lbl = tk.Label(legend_frame, text=" Up ", bg="#FFD700", fg="black", font=("Segoe UI", 8))
        yellow_lbl.pack(side=tk.LEFT, padx=2)

        # Green indicator
        green_lbl = tk.Label(legend_frame, text=" Stable ", bg="#228B22", fg="white", font=("Segoe UI", 8))
        green_lbl.pack(side=tk.LEFT, padx=2)

        ttk.Label(legend_frame, text="  |  Signal col: B=Buy, S=Sell", font=("Segoe UI", 8)).pack(side=tk.LEFT)
    
    def _browse_archive(self):
        """Browse for archive folder."""
        folder = filedialog.askdirectory(title="Select Archive Folder")
        if folder:
            # Validate it looks like an archive (either .csv or .csv.bz2)
            from pathlib import Path
            path = Path(folder)
            valid = False
            for item in path.iterdir():
                if item.is_dir() and item.name.isdigit():
                    if list(item.glob("market-history-*.csv.bz2")) or list(item.glob("market-history-*.csv")):
                        valid = True
                        break
            
            if valid:
                self.archive_var.set(folder)
            else:
                messagebox.showwarning(
                    "Invalid Folder",
                    "The selected folder doesn't appear to contain valid archive files.\n\n"
                    "Expected structure:\n"
                    "  folder/2024/market-history-2024-01-01.csv.bz2\n"
                    "  folder/2025/market-history-2025-01-01.csv.bz2\n"
                    "  ..."
                )
    
    def _reset_archive(self):
        """Reset archive path to default."""
        self.archive_var.set("(Default: Roaming folder)")
    
    def _reset_defaults(self):
        """Reset all settings to defaults."""
        self.archive_var.set("(Default: Roaming folder)")
        self.buy_pct_var.set(str(DEFAULT_BUY_PERCENTILE))
        self.sell_pct_var.set(str(DEFAULT_SELL_PERCENTILE))
        self.floor_var.set(str(DEFAULT_FLOOR_OFFSET_PCT))
        self.peak_var.set(str(DEFAULT_PEAK_OFFSET_PCT))
        self.min_volume_var.set(str(DEFAULT_MIN_DAILY_VOLUME))

    def _on_save(self):
        """Validate and save settings."""
        try:
            buy_pct = int(self.buy_pct_var.get())
            sell_pct = int(self.sell_pct_var.get())
            floor_offset = float(self.floor_var.get())
            peak_offset = float(self.peak_var.get())
            min_volume = int(self.min_volume_var.get())
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter valid numbers for all fields.")
            return

        if min_volume < 0:
            messagebox.showerror("Invalid", "Min Daily Volume must be 0 or greater.")
            return
        
        # Validate ranges
        if buy_pct < 5 or buy_pct > 45:
            messagebox.showerror("Invalid", "Buy percentile must be between 5 and 45.")
            return
        if sell_pct < 55 or sell_pct > 95:
            messagebox.showerror("Invalid", "Sell percentile must be between 55 and 95.")
            return
        if buy_pct >= sell_pct:
            messagebox.showerror("Invalid", "Buy percentile must be less than sell percentile.")
            return
        
        # Get archive path
        archive_val = self.archive_var.get()
        if archive_val.startswith("(Default"):
            archive_path = None
        else:
            archive_path = archive_val
        
        # Check if percentiles changed (requires rebuild)
        needs_rebuild = (buy_pct != self.original_buy_pct or sell_pct != self.original_sell_pct)
        
        if needs_rebuild:
            result = messagebox.askyesno(
                "Rebuild Required",
                "You changed the percentile settings.\n\n"
                "Existing profiles will need to be rebuilt for this to take effect.\n"
                "You can rebuild individual items or use 'Build Index' for all.\n\n"
                "Save anyway?"
            )
            if not result:
                return
        
        # Create new settings (preserve any fields not editable in this dialog)
        new_settings = StockMarketSettings(
            archive_path=archive_path,
            buy_percentile=buy_pct,
            sell_percentile=sell_pct,
            floor_offset_pct=floor_offset,
            peak_offset_pct=peak_offset,
            min_daily_volume=min_volume,
            active_hub_key=self.settings.active_hub_key,
            stock_market_enabled=self.settings.stock_market_enabled,
        )

        # Save to disk
        if save_settings(new_settings):
            self.on_save(new_settings, needs_rebuild)
            self.destroy()
        else:
            messagebox.showerror("Error", "Failed to save settings to disk.")
    
    def _get_current_archive_path(self) -> Path:
        """Get the archive path from current UI state."""
        archive_val = self.archive_var.get()
        if archive_val.startswith("(Default"):
            return get_data_dir() / "history-archive"
        return Path(archive_val)
    
    def _update_material_status(self):
        """Update material data status display."""
        try:
            from sde.sde_industry import get_sde_industry_db
            
            db = get_sde_industry_db()
            if db.is_available():
                info = db.get_version_info()
                age = db.get_age_days()
                mat_count = info.get("materials_count", 0)
                prod_count = info.get("products_count", 0)
                
                if age is not None:
                    self.material_status_var.set(
                        f"Loaded: {mat_count:,} materials, {prod_count:,} products ({age}d old)"
                    )
                    if age > 30:
                        self.material_status_label.configure(foreground="#CC6600")
                    else:
                        self.material_status_label.configure(foreground="green")
                else:
                    self.material_status_var.set(f"Loaded: {mat_count:,} materials, {prod_count:,} products")
                    self.material_status_label.configure(foreground="green")
            else:
                self.material_status_var.set("Not downloaded - click Update to download")
                self.material_status_label.configure(foreground="#CC6600")
        except ImportError:
            self.material_status_var.set("Module not available")
            self.material_status_label.configure(foreground="gray")
        except Exception as e:
            self.material_status_var.set(f"Error: {e}")
            self.material_status_label.configure(foreground="red")
    
    def _update_materials(self):
        """Download/update material data from Fuzzwork."""
        self.btn_update_materials.configure(state=tk.DISABLED)
        self.material_progress.pack(fill=tk.X, pady=5)
        self.material_progress_var.set(0)
        
        def do_update():
            try:
                from sde.sde_industry import get_sde_industry_db
                
                db = get_sde_industry_db()
                
                def progress(status, pct):
                    self.material_progress_var.set(pct)
                    self.material_status_var.set(status)
                
                success = db.refresh(progress_callback=progress)
                
                submit(lambda: self._material_update_complete(success))
                
            except Exception as e:
                submit(lambda: self._material_update_error(str(e)))
        
        threading.Thread(target=do_update, daemon=True).start()
    
    def _material_update_complete(self, success: bool):
        """Handle material update completion."""
        self.material_progress.pack_forget()
        self.btn_update_materials.configure(state=tk.NORMAL)
        self._update_material_status()
        
        if success:
            messagebox.showinfo(
                "Update Complete",
                "Material data updated successfully."
            )
        else:
            messagebox.showerror(
                "Update Failed",
                "Failed to download material data. Check your internet connection."
            )
    
    def _material_update_error(self, error: str):
        """Handle material update error."""
        self.material_progress.pack_forget()
        self.btn_update_materials.configure(state=tk.NORMAL)
        self.material_status_var.set(f"Error: {error}")
        self.material_status_label.configure(foreground="red")
        
        messagebox.showerror("Update Error", f"Failed to update material data:\n{error}")
