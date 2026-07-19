"""Unified price history graphing for EVE Market Scout.

Provides interactive price charts with:
- Time period tabs: 3 Year, 1 Year, 6 Months, 3 Months
- Filled price range area with average line
- Hover tooltips on data points
- Floor/ceiling overlay lines from profiles
- Volume bars subplot beneath the price chart
- [?] help button explaining the price/volume relationship

Works for both Stock Market and regular Scanner panels.
"""

import tkinter as tk
from tkinter import ttk
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from core.tk_queue import submit
from gui.gui_window_utils import fit_window

# Try to import matplotlib
try:
    import matplotlib
    matplotlib.use('TkAgg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    HAS_MATPLOTLIB = True
except Exception as e:
    print(f"[graphing] matplotlib import failed: {type(e).__name__}: {e}")
    HAS_MATPLOTLIB = False


# Time periods available
TIME_PERIODS = [
    ("3 Year", 365 * 3),
    ("1 Year", 365),
    ("6 Months", 182),
    ("3 Months", 91),
]


# Help text shown by the [?] button. ASCII only.
HELP_TEXT = (
    "Volume = units traded per day.\n"
    "\n"
    "Reading it against price:\n"
    "\n"
    "  Price up + volume up\n"
    "    = real demand absorbing supply (healthy)\n"
    "\n"
    "  Price up + volume flat or falling\n"
    "    = thin rally, often station traders\n"
    "      walking price between themselves\n"
    "\n"
    "  Volume collapsing near recent highs\n"
    "    = ceiling visits may not hold;\n"
    "      active trading band has narrowed\n"
    "\n"
    "If the price chart shows recent ceiling touches\n"
    "but volume on those days is well below the\n"
    "trailing average, the visible regime is tighter\n"
    "than the yearly profile suggests. Sell orders\n"
    "priced at the ceiling may sit unfilled."
)


def show_price_graph(
    parent: tk.Widget,
    type_id: int,
    type_name: str,
    region_id: int,
    profiles=None,
):
    """Show price history graph dialog.
    
    Args:
        parent: Parent widget for the dialog
        type_id: Item type ID
        type_name: Display name for the item
        region_id: Region ID for market data
        profiles: Optional ProfileManager for floor/ceiling overlays
    """
    if not HAS_MATPLOTLIB:
        from tkinter import messagebox
        messagebox.showerror(
            "Missing Dependency",
            "matplotlib is required for price graphs.\n"
            "Install with: pip install matplotlib"
        )
        return
    
    dialog = PriceGraphDialog(parent, type_id, type_name, region_id, profiles)
    dialog.show()


def _show_help_dialog(parent: tk.Widget):
    """Show the price-vs-volume help popup."""
    win = tk.Toplevel(parent)
    win.title("Price and Volume - How to Read")
    win.transient(parent)

    frame = ttk.Frame(win, padding=12)
    frame.pack(fill=tk.BOTH, expand=True)

    label = tk.Label(
        frame,
        text=HELP_TEXT,
        justify=tk.LEFT,
        anchor="nw",
        font=("Consolas", 9),
    )
    label.pack(fill=tk.BOTH, expand=True)

    btn = ttk.Button(frame, text="Close", command=win.destroy)
    btn.pack(pady=(8, 0))
    fit_window(win, min_width=440)


class PriceGraphDialog:
    """Price history graph dialog with time period tabs."""
    
    def __init__(
        self,
        parent: tk.Widget,
        type_id: int,
        type_name: str,
        region_id: int,
        profiles=None,
    ):
        self.parent = parent
        self.type_id = type_id
        self.type_name = type_name
        self.region_id = region_id
        self.profiles = profiles
        
        self.popup: Optional[tk.Toplevel] = None
        self.notebook: Optional[ttk.Notebook] = None
        self.panels: Dict[str, "GraphPanel"] = {}
        
        # Raw history data (loaded once)
        self.history: List[Dict[str, Any]] = []
        self.floor: Optional[float] = None
        self.ceiling: Optional[float] = None
    
    def show(self):
        """Show the dialog and load data."""
        self.popup = tk.Toplevel(self.parent)
        self.popup.title(f"Price History: {self.type_name}")
        
        # Main frame
        main_frame = ttk.Frame(self.popup)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Top bar with LI status strip + help button
        top_bar = ttk.Frame(main_frame)
        top_bar.pack(fill=tk.X, pady=(0, 4))

        # Leading indicator status (left side). Populated asynchronously
        # by _load_leading_indicator() once the popup is alive.
        self._li_status_var = tk.StringVar(
            value="Leading Indicator: loading..."
        )
        self._li_details_btn = ttk.Button(
            top_bar,
            text="Details",
            width=10,
            command=self._on_li_details_click,
            state=tk.DISABLED,
        )
        self._li_status_label = ttk.Label(
            top_bar,
            textvariable=self._li_status_var,
            anchor="w",
        )
        self._li_status_label.pack(side=tk.LEFT, padx=(4, 8))
        self._li_details_btn.pack(side=tk.LEFT)
        # Cached result for the Details button. Populated by
        # _load_leading_indicator(). None means "no data cached".
        self._li_result = None

        help_btn = ttk.Button(
            top_bar,
            text="[?] Help",
            width=10,
            command=lambda: _show_help_dialog(self.popup),
        )
        help_btn.pack(side=tk.RIGHT)

        # Notebook for time periods
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        # Create tab frames (content added after data loads)
        for label, days in TIME_PERIODS:
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=label)
            self.panels[label] = GraphPanel(frame, label)
            self.panels[label].show_loading()
        screen_w = self.popup.winfo_screenwidth()
        screen_h = self.popup.winfo_screenheight()
        w = min(1050, int(screen_w * 0.78))
        h = int(screen_h * 0.72)
        self.popup.geometry(f"{w}x{h}")
        self.popup.minsize(700, 400)
        self.popup.resizable(True, True)

        # Load data in background
        import threading
        thread = threading.Thread(target=self._load_data, daemon=True)
        thread.start()

        # Load leading indicator in parallel (independent of price data
        # so the LI strip populates even if history is slow)
        li_thread = threading.Thread(
            target=self._load_leading_indicator, daemon=True
        )
        li_thread.start()
    
    def _load_leading_indicator(self):
        """Load the cached leading indicator result for this item.
        
        Runs in a background thread. SQLite read is thread-safe so this
        doesn't need to defer to the main thread for the DB call - only
        for the UI update.
        """
        try:
            from analytics import leading_indicators_storage
            cache = leading_indicators_storage.load_for_region(
                self.region_id
            )
            result = cache.get(self.type_id)
        except Exception as e:
            print(f"[GraphDialog] LI load error: {e}")
            result = None
        
        if self.popup and self.popup.winfo_exists():
            submit(lambda r=result: self._apply_li_status(r))
    
    def _apply_li_status(self, result):
        """Update the LI status strip on the main thread."""
        self._li_result = result
        
        if not self.popup or not self.popup.winfo_exists():
            return
        
        if result is None:
            self._li_status_var.set(
                "Leading Indicator: no data (under ~60 days history)"
            )
            self._li_details_btn.configure(state=tk.DISABLED)
            return
        
        if not result.flags:
            self._li_status_var.set(
                "Leading Indicator: HEALTHY  (no divergence)"
            )
        else:
            from gui.gui_indicator_help import (
                get_indicator_letter, PRIORITY_ORDER,
            )
            letter = get_indicator_letter(result.flags) or "?"
            # Worst flag for display (matches the column letter)
            primary = next(
                (f for f in PRIORITY_ORDER if f in result.flags),
                result.flags[0],
            )
            extra = ""
            if len(result.flags) > 1:
                extra = f"  (+{len(result.flags) - 1} more)"
            self._li_status_var.set(
                f"Leading Indicator: [{letter}] {primary}{extra}"
            )
        
        self._li_details_btn.configure(state=tk.NORMAL)
    
    def _on_li_details_click(self):
        """Open the per-item indicator details dialog."""
        from gui.gui_indicator_help import show_indicator_details_dialog
        show_indicator_details_dialog(
            self.popup, self.type_name, self._li_result
        )
    
    def _load_data(self):
        """Load history data from database (runs in background thread)."""
        try:
            from history.market_history import get_market_history_db
            
            db = get_market_history_db()
            self.history = db.get_full_history(self.region_id, self.type_id, years=4)
            
            # Get floor/ceiling from profiles if available
            if self.profiles:
                profile = self.profiles.get_computed_profile(self.type_id, self.region_id)
                if profile:
                    self.floor = getattr(profile, 'weighted_floor', None) or getattr(profile, 'weighted_p_low', None)
                    self.ceiling = getattr(profile, 'weighted_ceiling', None) or getattr(profile, 'weighted_p_high', None)
            
            # Update UI on main thread
            if self.popup and self.popup.winfo_exists():
                submit(self._create_graphs)
                
        except Exception as e:
            if self.popup and self.popup.winfo_exists():
                submit(lambda: self._show_error(str(e)))
    
    def _create_graphs(self):
        """Create graphs for each time period."""
        import time as _pt
        _pt0 = _pt.perf_counter()
        if not self.history:
            self._show_error("No price history available")
            return

        # Parse dates once
        parsed_history = []
        for record in self.history:
            try:
                dt = datetime.strptime(record['date'], '%Y-%m-%d')
                parsed_history.append({
                    'date': dt,
                    'average': record.get('average', 0),
                    'lowest': record.get('lowest', 0),
                    'highest': record.get('highest', 0),
                    'volume': record.get('volume', 0),
                })
            except (ValueError, KeyError):
                continue

        if not parsed_history:
            self._show_error("No valid price data")
            return

        # Sort by date
        parsed_history.sort(key=lambda x: x['date'])
        _step_parse = _pt.perf_counter() - _pt0

        # Create each panel's graph
        now = datetime.now()
        _panel_timings = []
        for label, days in TIME_PERIODS:
            cutoff = now - timedelta(days=days)
            filtered = [r for r in parsed_history if r['date'] >= cutoff]

            panel = self.panels.get(label)
            if panel:
                if filtered:
                    _ts = _pt.perf_counter()
                    panel.create_graph(
                        filtered,
                        self.type_name,
                        self.floor,
                        self.ceiling
                    )
                    _panel_dur = _pt.perf_counter() - _ts
                    _panel_timings.append(f"{label}({len(filtered)}pts)={_panel_dur*1000:.0f}ms")
                else:
                    panel.show_no_data()
                    _panel_timings.append(f"{label}(empty)")
        _pt_total = _pt.perf_counter() - _pt0
        print(
            f"[PerfTimer] graphing._create_graphs item={self.type_name} "
            f"total={_pt_total*1000:.0f}ms history_records={len(parsed_history)} "
            f"parse={_step_parse*1000:.0f}ms panels=[{', '.join(_panel_timings)}]"
        )
    
    def _show_error(self, message: str):
        """Show error in all panels."""
        for panel in self.panels.values():
            panel.show_error(message)


class GraphPanel:
    """Single graph panel for a time period."""
    
    def __init__(self, frame: ttk.Frame, title: str):
        self.frame = frame
        self.title = title
        self.canvas: Optional[FigureCanvasTkAgg] = None
        self.fig: Optional[Figure] = None
        self.ax = None
        self.ax_vol = None
        self.annotation = None
        
        # Data for hover
        self.dates: List[datetime] = []
        self.averages: List[float] = []
        self.lows: List[float] = []
        self.highs: List[float] = []
        self.volumes: List[float] = []
        self.scatter_points = None
        self.annotation = None
        self.vol_annotation = None
    
    def show_loading(self):
        """Show loading message."""
        self.loading_label = ttk.Label(
            self.frame,
            text="Loading price history...",
            font=("Segoe UI", 10)
        )
        self.loading_label.pack(expand=True)
    
    def show_no_data(self):
        """Show no data message."""
        if hasattr(self, 'loading_label') and self.loading_label.winfo_exists():
            self.loading_label.config(text="No data available for this time period")
    
    def show_error(self, message: str):
        """Show error message."""
        if hasattr(self, 'loading_label') and self.loading_label.winfo_exists():
            self.loading_label.config(text=f"Error: {message}")
        else:
            ttk.Label(self.frame, text=f"Error: {message}").pack(expand=True)
    
    def create_graph(
        self,
        data: List[Dict[str, Any]],
        item_name: str,
        floor: Optional[float],
        ceiling: Optional[float],
    ):
        """Create the matplotlib graph."""
        import time as _pt
        _pt0 = _pt.perf_counter()
        self._perf_start = _pt0
        self._perf_module = _pt
        self._perf_points = len(data)
        # Remove loading label
        if hasattr(self, 'loading_label') and self.loading_label.winfo_exists():
            self.loading_label.destroy()

        if len(data) < 2:
            ttk.Label(self.frame, text="Insufficient data points").pack(expand=True)
            return
        
        # Extract data
        self.dates = [r['date'] for r in data]
        self.averages = [r['average'] for r in data]
        self.lows = [r['lowest'] for r in data]
        self.highs = [r['highest'] for r in data]
        self.volumes = [r.get('volume', 0) for r in data]
        
        # Create figure with two stacked subplots sharing x-axis
        self.fig = Figure(figsize=(11, 6.2), dpi=100)
        gs = self.fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08)
        self.ax = self.fig.add_subplot(gs[0])
        self.ax_vol = self.fig.add_subplot(gs[1], sharex=self.ax)
        
        # ---- Top subplot: price chart ----
        # Plot filled price range area
        self.ax.fill_between(
            self.dates, self.lows, self.highs,
            alpha=0.3, color='#4A90D9', label='Price Range'
        )
        
        # Plot average line
        self.ax.plot(
            self.dates, self.averages,
            color='#2E5C8A', linewidth=1.5, label='Average'
        )
        
        # Add invisible scatter points for hover detection
        self.scatter_points = self.ax.scatter(
            self.dates, self.averages,
            c='#2E5C8A', s=15, alpha=0, zorder=5  # invisible but hoverable
        )
        
        # Add floor/ceiling lines if available
        if floor and floor > 0:
            self.ax.axhline(
                y=floor, color='#228B22', linestyle='--', linewidth=2,
                label=f'Floor: {self._format_price(floor)}', alpha=0.8
            )
        
        if ceiling and ceiling > 0:
            self.ax.axhline(
                y=ceiling, color='#DC143C', linestyle='--', linewidth=2,
                label=f'Ceiling: {self._format_price(ceiling)}', alpha=0.8
            )
        
        # Set y-axis limits based on floor/ceiling to filter visual outliers
        if floor and ceiling and floor > 0 and ceiling > 0:
            y_min = floor * 0.8   # 20% below floor
            y_max = ceiling * 1.2  # 20% above ceiling
            self.ax.set_ylim(y_min, y_max)
        
        # Title and labels
        self.ax.set_title(f"{item_name} - {self.title}", fontsize=11, fontweight='bold')
        self.ax.set_ylabel("Price (ISK)", fontsize=9)
        
        # Format y-axis
        self.ax.yaxis.set_major_formatter(plt.FuncFormatter(self._price_formatter))
        
        # Legend and grid
        self.ax.legend(loc='upper left', fontsize=8)
        self.ax.grid(True, alpha=0.3)

        # Hide x-axis tick labels on top (shared with bottom subplot)
        plt.setp(self.ax.get_xticklabels(), visible=False)
        
        # ---- Bottom subplot: volume bars ----
        self.ax_vol.bar(
            self.dates, self.volumes,
            width=0.9, color='#6B8CAE', alpha=0.75,
            edgecolor='none', label='Volume'
        )
        self.ax_vol.set_ylabel("Volume", fontsize=9)
        self.ax_vol.set_xlabel("Date", fontsize=9)
        self.ax_vol.yaxis.set_major_formatter(plt.FuncFormatter(self._unit_formatter))
        self.ax_vol.grid(True, alpha=0.3, axis='y')
        self.ax_vol.set_ylim(bottom=0)

        # Tight layout
        self.fig.autofmt_xdate()
        self.fig.tight_layout()

        # Create canvas
        _pt = self._perf_module
        _ts_layout = _pt.perf_counter()
        _build_dur = _ts_layout - self._perf_start
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.frame)
        _ts_canvas = _pt.perf_counter()
        self.canvas.draw()
        _draw_dur = _pt.perf_counter() - _ts_canvas
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        _total_dur = _pt.perf_counter() - self._perf_start
        print(
            f"[PerfTimer] GraphPanel.create_graph title={self.title} "
            f"total={_total_dur*1000:.0f}ms points={self._perf_points} "
            f"figure_build={_build_dur*1000:.0f}ms canvas.draw={_draw_dur*1000:.0f}ms"
        )
        
        # Setup hover annotation for price chart
        self.annotation = self.ax.annotate(
            "",
            xy=(0, 0),
            xytext=(15, 15),
            textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.5", fc="white", ec="gray", alpha=0.95),
            fontsize=9,
            zorder=10
        )
        self.annotation.set_visible(False)

        # Setup hover annotation for volume chart
        self.vol_annotation = self.ax_vol.annotate(
            "",
            xy=(0, 0),
            xytext=(15, 15),
            textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.5", fc="white", ec="gray", alpha=0.95),
            fontsize=9,
            zorder=10
        )
        self.vol_annotation.set_visible(False)

        # Connect hover event
        self.canvas.mpl_connect("motion_notify_event", self._on_hover)
    
    def _on_hover(self, event):
        """Handle mouse hover to show data tooltip on either price or volume axis."""
        # Outside any axes - hide both
        if event.inaxes is None:
            self._hide_annotations()
            return

        # Hover over price chart
        if event.inaxes == self.ax and self.scatter_points is not None:
            # Hide volume annotation if shown
            if self.vol_annotation and self.vol_annotation.get_visible():
                self.vol_annotation.set_visible(False)

            cont, ind = self.scatter_points.contains(event)
            if cont and len(ind["ind"]) > 0:
                idx = ind["ind"][0]
                date = self.dates[idx]
                avg = self.averages[idx]
                low = self.lows[idx]
                high = self.highs[idx]
                vol = self.volumes[idx] if idx < len(self.volumes) else 0

                self.annotation.xy = (date, avg)
                text = (
                    f"{date.strftime('%Y-%m-%d')}\n"
                    f"High: {self._format_price(high)}\n"
                    f"Avg: {self._format_price(avg)}\n"
                    f"Low: {self._format_price(low)}\n"
                    f"Vol: {self._format_units(vol)}"
                )
                self.annotation.set_text(text)
                self.annotation.set_visible(True)
                self.canvas.draw_idle()
            else:
                if self.annotation and self.annotation.get_visible():
                    self.annotation.set_visible(False)
                    self.canvas.draw_idle()
            return

        # Hover over volume chart
        if event.inaxes == self.ax_vol and self.dates:
            # Hide price annotation if shown
            if self.annotation and self.annotation.get_visible():
                self.annotation.set_visible(False)

            # Find the bar nearest to cursor x by date
            idx = self._nearest_date_index(event.xdata)
            if idx is None:
                if self.vol_annotation and self.vol_annotation.get_visible():
                    self.vol_annotation.set_visible(False)
                    self.canvas.draw_idle()
                return

            date = self.dates[idx]
            vol = self.volumes[idx] if idx < len(self.volumes) else 0

            self.vol_annotation.xy = (date, vol)
            text = (
                f"{date.strftime('%Y-%m-%d')}\n"
                f"Vol: {self._format_units(vol)}"
            )
            self.vol_annotation.set_text(text)
            self.vol_annotation.set_visible(True)
            self.canvas.draw_idle()
            return

        # Some other axes - hide both
        self._hide_annotations()

    def _hide_annotations(self):
        """Hide both annotations and redraw if anything was visible."""
        changed = False
        if self.annotation and self.annotation.get_visible():
            self.annotation.set_visible(False)
            changed = True
        if self.vol_annotation and self.vol_annotation.get_visible():
            self.vol_annotation.set_visible(False)
            changed = True
        if changed:
            self.canvas.draw_idle()

    def _nearest_date_index(self, x_value):
        """Find the index of the date closest to the matplotlib x value.

        Matplotlib converts datetimes to floats internally, so we use the
        date2num conversion that matplotlib applies to bar charts.
        """
        if x_value is None or not self.dates:
            return None
        try:
            from matplotlib.dates import date2num
            target = float(x_value)
            best_idx = 0
            best_diff = abs(date2num(self.dates[0]) - target)
            for i, d in enumerate(self.dates):
                diff = abs(date2num(d) - target)
                if diff < best_diff:
                    best_diff = diff
                    best_idx = i
            # Reject hover if more than ~1 day away (no bar nearby)
            if best_diff > 1.5:
                return None
            return best_idx
        except Exception:
            return None
    
    def _price_formatter(self, x, pos):
        """Format price with K/M/B suffixes."""
        if x >= 1_000_000_000:
            return f'{x/1_000_000_000:.1f}B'
        elif x >= 1_000_000:
            return f'{x/1_000_000:.1f}M'
        elif x >= 1_000:
            return f'{x/1_000:.0f}K'
        return f'{x:.0f}'

    def _unit_formatter(self, x, pos):
        """Format unit counts with K/M/B suffixes (for volume axis)."""
        if x >= 1_000_000_000:
            return f'{x/1_000_000_000:.1f}B'
        elif x >= 1_000_000:
            return f'{x/1_000_000:.1f}M'
        elif x >= 1_000:
            return f'{x/1_000:.0f}K'
        return f'{x:.0f}'
    
    def _format_price(self, price: float) -> str:
        """Format price for display."""
        if price >= 1_000_000_000:
            return f'{price/1_000_000_000:.2f}B'
        elif price >= 1_000_000:
            return f'{price/1_000_000:.2f}M'
        elif price >= 1_000:
            return f'{price/1_000:.1f}K'
        return f'{price:,.0f}'

    def _format_units(self, n: float) -> str:
        """Format unit counts for tooltip display."""
        if n >= 1_000_000_000:
            return f'{n/1_000_000_000:.2f}B'
        elif n >= 1_000_000:
            return f'{n/1_000_000:.2f}M'
        elif n >= 1_000:
            return f'{n/1_000:.1f}K'
        return f'{n:,.0f}'


# Convenience alias for backward compatibility
def show_price_history_dialog(
    parent,
    type_id: int,
    type_name: str,
    region_id: int,
    profiles=None,
):
    """Backward-compatible alias for show_price_graph."""
    show_price_graph(parent, type_id, type_name, region_id, profiles)
