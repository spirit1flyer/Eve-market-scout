"""Stock Market dialogs for EVE Market Scout."""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import asyncio
import threading
from typing import Callable, TYPE_CHECKING

from core.config import TRADE_HUBS, get_hub_config, DEFAULT_HUB, ESI_USER_AGENT
from core.tk_queue import submit
from gui.gui_window_utils import fit_window, make_scrollable

if TYPE_CHECKING:
    from gui.stockmarket.gui_stockmarket_holdings_data import HoldingsManager
    from analytics.historical_profiles import ProfileManager
    from history.archive_downloader import ArchiveDownloader


class AddStockItemDialog(tk.Toplevel):
    """Dialog to add item to stock holdings."""
    
    def __init__(
        self,
        parent,
        get_client: Callable,
        callback: Callable,
        holdings: "HoldingsManager",
        profiles: "ProfileManager"
    ):
        super().__init__(parent)
        self.get_client = get_client
        self.callback = callback
        self.holdings = holdings
        self.profiles = profiles
        
        self.selected_item = None
        self.search_results = []
        
        # Default to Amarr
        self.region_id = get_hub_config(DEFAULT_HUB)["region_id"]
        self.station_id = get_hub_config(DEFAULT_HUB)["station_id"]
        
        self.title("Add to Stock Holdings")
        self.transient(parent)

        self._create_widgets()
        fit_window(self, min_width=450)
        self.grab_set()
    
    def _create_widgets(self):
        """Create dialog widgets."""
        # Buttons pinned to window bottom (outside scroll area)
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Add", command=self._on_add).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        # Scrollable content area above the buttons.
        inner = make_scrollable(self)

        # Search section
        search_frame = ttk.LabelFrame(inner, text="Search for Item", padding=10)
        search_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(search_frame, text="Item Name:").pack(anchor=tk.W)

        search_row = ttk.Frame(search_frame)
        search_row.pack(fill=tk.X, pady=5)

        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var, width=30)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 5))
        self.search_entry.bind("<Return>", lambda e: self._do_search())

        ttk.Button(search_row, text="Search", command=self._do_search).pack(side=tk.LEFT)

        # Results
        ttk.Label(search_frame, text="Results:").pack(anchor=tk.W, pady=(10, 0))

        results_frame = ttk.Frame(search_frame)
        results_frame.pack(fill=tk.BOTH, expand=True)

        self.results_listbox = tk.Listbox(results_frame, height=6)
        self.results_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(results_frame, orient=tk.VERTICAL, command=self.results_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.results_listbox.configure(yscrollcommand=scrollbar.set)

        self.results_listbox.bind("<<ListboxSelect>>", self._on_select)

        # Selected item
        self.selected_label = ttk.Label(search_frame, text="Selected: None", font=("Segoe UI", 9, "bold"))
        self.selected_label.pack(anchor=tk.W, pady=(10, 0))

        # Station selection
        station_frame = ttk.LabelFrame(inner, text="Trading Station", padding=10)
        station_frame.pack(fill=tk.X, padx=10, pady=5)

        self.station_var = tk.StringVar(value=DEFAULT_HUB)
        for hub_key, config in TRADE_HUBS.items():
            if config.get("enabled"):
                ttk.Radiobutton(
                    station_frame,
                    text=config["name"],
                    variable=self.station_var,
                    value=hub_key,
                    command=self._on_station_change
                ).pack(side=tk.LEFT, padx=5)
    
    def _do_search(self):
        """Search for items."""
        query = self.search_var.get().strip()
        if not query or len(query) < 2:
            return
        
        if not self.get_client:
            return
        
        self.results_listbox.delete(0, tk.END)
        self.results_listbox.insert(tk.END, "Searching...")
        
        def search():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = []
            error_msg = None
            
            try:
                client = self.get_client()
                if client:
                    import aiohttp
                    from core.config import REQUEST_TIMEOUT
                    from core.ssl_context import make_connector
                    
                    async def do_search():
                        client.reset_for_new_loop()
                        async with aiohttp.ClientSession(connector=make_connector(), headers={"User-Agent": ESI_USER_AGENT}, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
                            client.session = session
                            return await client.search_item_by_name(query)
                    
                    results = loop.run_until_complete(do_search())
                else:
                    error_msg = "No client available"
            except Exception as e:
                error_msg = str(e)
                print(f"Search error: {e}")
            finally:
                loop.close()
            
            # Update UI on main thread
            if error_msg:
                submit(lambda: self._show_error(error_msg))
            else:
                submit(lambda: self._show_results(results))
        
        threading.Thread(target=search, daemon=True).start()
    
    def _show_error(self, msg: str):
        """Show error in results list."""
        self.results_listbox.delete(0, tk.END)
        self.results_listbox.insert(tk.END, f"(Error: {msg[:40]})")
    
    def _show_results(self, results: list):
        """Show search results."""
        self.results_listbox.delete(0, tk.END)
        self.search_results = results
        
        for item in results[:20]:  # Limit to 20
            self.results_listbox.insert(tk.END, item.get("name", f"Type {item.get('type_id')}"))
    
    def _on_select(self, event):
        """Handle result selection."""
        selection = self.results_listbox.curselection()
        if not selection:
            return
        
        idx = selection[0]
        if idx < len(self.search_results):
            self.selected_item = self.search_results[idx]
            name = self.selected_item.get("name", "Unknown")
            self.selected_label.configure(text=f"Selected: {name}")
    
    def _on_station_change(self):
        """Handle station selection change."""
        hub_key = self.station_var.get()
        config = get_hub_config(hub_key)
        self.region_id = config["region_id"]
        self.station_id = config["station_id"]
    
    def _on_add(self):
        """Add selected item to holdings."""
        if not self.selected_item:
            messagebox.showwarning("No Selection", "Please search and select an item first.")
            return
        
        type_id = self.selected_item.get("type_id")
        type_name = self.selected_item.get("name", "")
        
        # Check if already in holdings for this hub (HoldingsManager is per-hub)
        if self.holdings is not None and self.holdings.has_item(type_id):
            messagebox.showinfo("Already Exists", f"{type_name} is already in your holdings for this hub.")
            return
        
        # Call callback and close
        self.callback(type_id, type_name, self.region_id, self.station_id)
        self.destroy()


class ArchiveDownloadDialog(tk.Toplevel):
    """Dialog for downloading/managing archive data."""
    
    def __init__(
        self,
        parent,
        downloader: "ArchiveDownloader",
        on_complete: Callable
    ):
        super().__init__(parent)
        self.downloader = downloader
        self.on_complete = on_complete
        self._downloading = False
        self._importing = False
        
        self.title("Archive Manager")
        self.transient(parent)

        self._create_widgets()
        self._update_status()
        self._update_import_status()
        fit_window(self, min_width=500)
        self.grab_set()
    
    def _create_widgets(self):
        """Create dialog widgets."""
        # Buttons pinned to window bottom (outside scroll area)
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        self.download_btn = ttk.Button(btn_frame, text="Download All", command=self._start_download)
        self.download_btn.pack(side=tk.LEFT, padx=5)
        self.pause_btn = ttk.Button(btn_frame, text="Pause", command=self._toggle_pause, state=tk.DISABLED)
        self.pause_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Locate Existing", command=self._locate_existing).pack(side=tk.LEFT, padx=5)
        self.import_btn = ttk.Button(btn_frame, text="Import to DB", command=self._start_import)
        self.import_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Close", command=self._on_close).pack(side=tk.RIGHT)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        # Scrollable content area above the buttons.
        inner = make_scrollable(self)

        # Status section
        status_frame = ttk.LabelFrame(inner, text="Archive Status", padding=10)
        status_frame.pack(fill=tk.X, padx=10, pady=5)

        self.status_tree = ttk.Treeview(
            status_frame,
            columns=("year", "downloaded", "expected", "percent"),
            show="headings",
            height=4
        )
        self.status_tree.heading("year", text="Year")
        self.status_tree.heading("downloaded", text="Downloaded")
        self.status_tree.heading("expected", text="Expected")
        self.status_tree.heading("percent", text="Complete")

        self.status_tree.column("year", width=80)
        self.status_tree.column("downloaded", width=100)
        self.status_tree.column("expected", width=100)
        self.status_tree.column("percent", width=80)

        self.status_tree.pack(fill=tk.X)

        # Progress section
        progress_frame = ttk.LabelFrame(inner, text="Download Progress", padding=10)
        progress_frame.pack(fill=tk.X, padx=10, pady=5)

        self.progress_label = ttk.Label(progress_frame, text="Ready to download")
        self.progress_label.pack(anchor=tk.W)

        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=5)

        self.bytes_label = ttk.Label(progress_frame, text="")
        self.bytes_label.pack(anchor=tk.W)

        # Import section
        import_frame = ttk.LabelFrame(inner, text="Database Import", padding=10)
        import_frame.pack(fill=tk.X, padx=10, pady=5)

        self.import_status_label = ttk.Label(import_frame, text="Checking database status...")
        self.import_status_label.pack(anchor=tk.W)

        self.import_progress = ttk.Progressbar(import_frame, mode="determinate")
        self.import_progress.pack(fill=tk.X, pady=5)

        self.import_count_label = ttk.Label(import_frame, text="")
        self.import_count_label.pack(anchor=tk.W)
    
    def _update_import_status(self):
        """Update import status display."""
        from history.market_history import get_market_history_db
        from gui.gui_migration import check_has_full_history, get_archive_path
        
        try:
            db = get_market_history_db()
            stats = db.get_stats()
            row_count = stats.get('row_count', 0)
            earliest = stats.get('earliest_date', 'N/A')
            latest = stats.get('latest_date', 'N/A')
            
            if row_count == 0:
                self.import_status_label.configure(text="Database is empty - import needed")
                self.import_btn.configure(state=tk.NORMAL)
            elif check_has_full_history(db):
                self.import_status_label.configure(
                    text=f"Database OK: {row_count:,} records ({earliest} to {latest})"
                )
                self.import_btn.configure(state=tk.DISABLED)
            else:
                self.import_status_label.configure(
                    text=f"Partial data: {row_count:,} records ({earliest} to {latest}) - import needed"
                )
                self.import_btn.configure(state=tk.NORMAL)
        except Exception as e:
            self.import_status_label.configure(text=f"Error: {e}")
            self.import_btn.configure(state=tk.NORMAL)
    
    def _start_import(self):
        """Start importing archive to database."""
        from history.market_history import get_market_history_db, REGION_IDS
        from gui.gui_migration import get_archive_path
        
        # Check archive exists
        archive_path = self.downloader.archive_path
        if not archive_path.exists():
            messagebox.showerror("No Archive", "No archive files found. Download first.")
            return
        
        self._importing = True
        self.import_btn.configure(state=tk.DISABLED)
        self.download_btn.configure(state=tk.DISABLED)
        
        def do_import():
            try:
                db = get_market_history_db()
                db.init_db()
                
                region_filter = set(REGION_IDS.values())
                
                db.import_archive(
                    archive_path,
                    progress_callback=self._import_progress_callback,
                    years=3,
                    region_filter=region_filter
                )
                
                submit(self._on_import_done)
                
            except Exception as e:
                print(f"[Import] Error: {e}")
                submit(lambda: self._on_import_error(str(e)))
        
        threading.Thread(target=do_import, daemon=True).start()
    
    def _import_progress_callback(self, status: str, current: int, total: int):
        """Handle import progress updates."""
        def update():
            self.import_status_label.configure(text=status)
            if total > 0:
                pct = (current / total) * 100
                self.import_progress.configure(value=pct)
                self.import_count_label.configure(text=f"{current:,} / {total:,} files")
        
        submit(update)
    
    def _on_import_done(self):
        """Called when import completes."""
        self._importing = False
        self.download_btn.configure(state=tk.NORMAL)
        self._update_import_status()
        self.import_count_label.configure(text="Import complete!")
        messagebox.showinfo("Import Complete", "Archive data imported to database successfully.")
    
    def _on_import_error(self, error: str):
        """Called when import fails."""
        self._importing = False
        self.import_btn.configure(state=tk.NORMAL)
        self.download_btn.configure(state=tk.NORMAL)
        self.import_status_label.configure(text=f"Import failed: {error}")
        messagebox.showerror("Import Error", f"Failed to import archive: {error}")
    
    def _update_status(self):
        """Update status display."""
        # Clear existing
        for item in self.status_tree.get_children():
            self.status_tree.delete(item)
        
        summary = self.downloader.get_download_summary()
        
        for year in sorted(summary.keys(), reverse=True):
            data = summary[year]
            self.status_tree.insert("", tk.END, values=(
                year,
                data["downloaded"],
                data["expected"],
                f"{data['percent']:.0f}%"
            ))
    
    def _start_download(self):
        """Start downloading all years."""
        self._downloading = True
        self.download_btn.configure(state=tk.DISABLED)
        self.pause_btn.configure(state=tk.NORMAL)
        
        def download():
            async def do_download():
                await self.downloader.download_all_years(self._progress_callback)
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(do_download())
            finally:
                loop.close()
                submit(self._on_download_done)
        
        threading.Thread(target=download, daemon=True).start()
    
    def _progress_callback(self, status: str, files_done: int, files_total: int, bytes_done: int, bytes_total: int):
        """Handle progress updates."""
        def update():
            self.progress_label.configure(text=status)
            
            if files_total > 0:
                pct = files_done / files_total * 100
                self.progress_bar.configure(value=pct)
            
            mb_done = bytes_done / (1024 * 1024)
            mb_total = bytes_total / (1024 * 1024)
            self.bytes_label.configure(text=f"{mb_done:.1f} MB / ~{mb_total:.1f} MB")
            
            self._update_status()
        
        submit(update)
    
    def _on_download_done(self):
        """Called when download completes."""
        self._downloading = False
        self.download_btn.configure(state=tk.NORMAL)
        self.pause_btn.configure(state=tk.DISABLED)
        self.progress_label.configure(text="Download complete")
        self._update_status()
        self._update_import_status()
        
        # Prompt to import
        result = messagebox.askyesno(
            "Download Complete",
            "Archive download complete.\n\n"
            "Import to database now?\n"
            "(Required for Stock Market features)"
        )
        if result:
            self._start_import()
    
    def _toggle_pause(self):
        """Toggle pause/resume."""
        if self.downloader.is_paused():
            self.downloader.resume()
            self.pause_btn.configure(text="Pause")
        else:
            self.downloader.pause()
            self.pause_btn.configure(text="Resume")
    
    def _locate_existing(self):
        """Let user point to existing archive folder."""
        from pathlib import Path
        
        folder = filedialog.askdirectory(title="Select Archive Folder")
        if not folder:
            return
        
        if self.downloader.set_archive_path(Path(folder)):
            messagebox.showinfo("Success", "Archive folder set successfully.")
            self._update_status()
            self._update_import_status()
            # Trigger sync callback so profiles manager gets the new path
            self.on_complete()
            
            # Check if import is needed
            from history.market_history import get_market_history_db
            from gui.gui_migration import check_has_full_history
            
            db = get_market_history_db()
            if not check_has_full_history(db):
                result = messagebox.askyesno(
                    "Import Needed",
                    "Archive located but database needs import.\n\n"
                    "Import to database now?\n"
                    "(Required for Stock Market features)"
                )
                if result:
                    self._start_import()
        else:
            messagebox.showerror("Invalid Folder", "The selected folder doesn't appear to contain valid archive files.")
    
    def _on_close(self):
        """Close dialog."""
        if self._downloading:
            self.downloader.pause()
        self.on_complete()
        self.destroy()
