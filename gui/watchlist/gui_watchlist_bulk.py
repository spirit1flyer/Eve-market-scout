"""Dialog for bulk adding items to watchlist from pasted text."""

import tkinter as tk
from tkinter import ttk, messagebox
import asyncio
import threading
import re
from typing import Callable

from gui.watchlist.gui_watchlist_search import SearchMatchDialog
from core.tk_queue import submit
from gui.gui_window_utils import fit_window, make_scrollable


class BulkAddDialog(tk.Toplevel):
    """Dialog to bulk add items from pasted fitting/market text."""

    def __init__(self, parent, callback: Callable, get_client: Callable, prefill_text: str = None):
        super().__init__(parent)
        self.callback = callback
        self.get_client = get_client
        self.parsed_items = []  # List of {name, type_id, status}
        self.resolved_items = []  # Successfully matched items

        self.title("Bulk Add to Watchlist")
        self.transient(parent)

        self._create_widgets()
        fit_window(self, min_width=600)
        self.grab_set()

        # Pre-fill text area and auto-parse if provided
        if prefill_text:
            self.text_area.insert("1.0", prefill_text)
            # Defer auto-parse so dialog finishes rendering first
            self.after(150, self._parse_and_match)

    def _create_widgets(self):
        """Create dialog widgets."""
        # Buttons pinned to window bottom (outside scroll area)
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Add Matched Items", command=self._on_add).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        # Scrollable content area above the buttons.
        inner = make_scrollable(self)

        # Instructions
        ttk.Label(
            inner,
            text="Paste item list (EVE fitting format, market export, or one item per line):",
            font=("Segoe UI", 9)
        ).pack(anchor=tk.W, padx=10, pady=(10, 5))

        # Text area for pasting
        text_frame = ttk.Frame(inner)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.text_area = tk.Text(text_frame, height=10, wrap=tk.NONE)
        self.text_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        text_sb = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.text_area.yview)
        text_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_area.configure(yscrollcommand=text_sb.set)

        # Parse button
        btn_row = ttk.Frame(inner)
        btn_row.pack(fill=tk.X, padx=10, pady=5)

        ttk.Button(btn_row, text="Parse & Match Items", command=self._parse_and_match).pack(side=tk.LEFT)
        self.status_label = ttk.Label(btn_row, text="", font=("Segoe UI", 9))
        self.status_label.pack(side=tk.LEFT, padx=10)

        # Results treeview
        ttk.Label(inner, text="Parsed Items:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, padx=10, pady=(10, 2))

        results_frame = ttk.Frame(inner)
        results_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        columns = ("name", "status", "action")
        self.results_tree = ttk.Treeview(results_frame, columns=columns, show="headings", height=8)
        self.results_tree.heading("name", text="Item Name")
        self.results_tree.heading("status", text="Status")
        self.results_tree.heading("action", text="Action")

        self.results_tree.column("name", width=250)
        self.results_tree.column("status", width=150)
        self.results_tree.column("action", width=100)

        self.results_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        results_sb = ttk.Scrollbar(results_frame, orient=tk.VERTICAL, command=self.results_tree.yview)
        results_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.results_tree.configure(yscrollcommand=results_sb.set)

        # Tags
        self.results_tree.tag_configure("matched", foreground="green")
        self.results_tree.tag_configure("unmatched", foreground="orange")
        self.results_tree.tag_configure("error", foreground="red")

        # Context menu for unmatched items
        self.result_menu = tk.Menu(self, tearoff=0)
        self.result_menu.add_command(label="Search for match...", command=self._search_for_match)
        self.result_menu.add_command(label="Remove from list", command=self._remove_from_list)

        self.results_tree.bind("<Button-3>", self._show_result_menu)
        self.results_tree.bind("<Double-1>", lambda e: self._search_for_match())

    def _parse_text(self, text: str) -> list[str]:
        """Parse pasted text into item names."""
        names = []
        lines = text.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Skip "Total:" lines
            if line.lower().startswith("total"):
                continue
            
            # Try to extract item name from various formats:
            # Format 1: "Item Name\t2\t3,298,000.00\t6,596,000.00" (EVE fitting/market)
            # Format 2: "Item Name x5" (quantity suffix)
            # Format 3: "5x Item Name" (quantity prefix)
            # Format 4: Just "Item Name"
            
            # Split by tab first (EVE format)
            parts = line.split('\t')
            name = parts[0].strip()
            
            # Remove quantity patterns
            # "Item Name x5" or "Item Name x 5"
            name = re.sub(r'\s*x\s*\d+\s*$', '', name, flags=re.IGNORECASE)
            # "5x Item Name" or "5 x Item Name"  
            name = re.sub(r'^\d+\s*x\s*', '', name, flags=re.IGNORECASE)
            
            # Clean up any remaining artifacts
            name = name.strip()
            
            if name and len(name) >= 3:
                names.append(name)
        
        return names

    def _parse_and_match(self):
        """Parse text and match items against ESI."""
        text = self.text_area.get("1.0", tk.END)
        names = self._parse_text(text)
        
        if not names:
            self.status_label.configure(text="No items found in text")
            return
        
        self.status_label.configure(text=f"Matching {len(names)} items...")
        self.parsed_items = [{"name": n, "type_id": None, "status": "pending"} for n in names]
        
        # Clear results
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        
        # Run matching in thread
        def match_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                client = self.get_client() if self.get_client else None
                if client:
                    import aiohttp
                    from core.config import REQUEST_TIMEOUT
                    from core.ssl_context import make_connector
                    
                    async def do_match():
                        client.reset_for_new_loop()
                        async with aiohttp.ClientSession(connector=make_connector(), timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
                            client.session = session
                            
                            for item in self.parsed_items:
                                # Try exact match first via ESI
                                try:
                                    results = await client.search_item_by_name(item["name"])
                                    
                                    # Look for exact match
                                    exact = None
                                    for r in results:
                                        if r["name"].lower() == item["name"].lower():
                                            exact = r
                                            break
                                    
                                    if exact:
                                        item["type_id"] = exact["type_id"]
                                        item["matched_name"] = exact["name"]
                                        item["status"] = "matched"
                                    elif results:
                                        # No exact match but have suggestions
                                        item["suggestions"] = results[:5]
                                        item["status"] = "unmatched"
                                    else:
                                        item["status"] = "not_found"
                                except Exception as e:
                                    item["status"] = "error"
                                    item["error"] = str(e)
                    
                    loop.run_until_complete(do_match())
            except Exception as e:
                print(f"Bulk match error: {e}")
            finally:
                loop.close()
            
            # Update UI on main thread
            submit(self._display_match_results)
        
        threading.Thread(target=match_thread, daemon=True).start()

    def _display_match_results(self):
        """Display match results in treeview."""
        # Clear tree
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        
        matched = 0
        unmatched = 0
        
        for i, item in enumerate(self.parsed_items):
            if item["status"] == "matched":
                tag = "matched"
                status = f"(OK) {item.get('matched_name', item['name'])}"
                action = "Will add"
                matched += 1
            elif item["status"] == "unmatched":
                tag = "unmatched"
                suggestions = item.get("suggestions", [])
                if suggestions:
                    status = f"? Similar: {suggestions[0]['name'][:20]}..."
                else:
                    status = "? No exact match"
                action = "Double-click"
                unmatched += 1
            elif item["status"] == "not_found":
                tag = "error"
                status = "(X) Not found"
                action = "Remove"
                unmatched += 1
            else:
                tag = "error"
                status = "(X) Error"
                action = "Remove"
                unmatched += 1
            
            self.results_tree.insert("", tk.END, iid=str(i), values=(
                item["name"], status, action
            ), tags=(tag,))
        
        self.status_label.configure(text=f"Matched: {matched}, Need review: {unmatched}")
        
        # Store resolved items
        self.resolved_items = [
            {"type_id": item["type_id"], "name": item.get("matched_name", item["name"])}
            for item in self.parsed_items
            if item["status"] == "matched"
        ]

    def _show_result_menu(self, event):
        """Show context menu for result items."""
        item = self.results_tree.identify_row(event.y)
        if item:
            self.results_tree.selection_set(item)
            self.result_menu.post(event.x_root, event.y_root)

    def _search_for_match(self):
        """Open search dialog for unmatched item."""
        selection = self.results_tree.selection()
        if not selection:
            return
        
        idx = int(selection[0])
        item = self.parsed_items[idx]
        
        # Open a mini search dialog
        SearchMatchDialog(
            self,
            item["name"],
            item.get("suggestions", []),
            self.get_client,
            lambda type_id, name: self._on_match_selected(idx, type_id, name)
        )

    def _on_match_selected(self, idx: int, type_id: int, name: str):
        """Callback when user selects a match for an item."""
        self.parsed_items[idx]["type_id"] = type_id
        self.parsed_items[idx]["matched_name"] = name
        self.parsed_items[idx]["status"] = "matched"
        self._display_match_results()

    def _remove_from_list(self):
        """Remove selected item from list."""
        selection = self.results_tree.selection()
        if not selection:
            return
        
        idx = int(selection[0])
        del self.parsed_items[idx]
        self._display_match_results()

    def _on_add(self):
        """Add all matched items to watchlist."""
        if not self.resolved_items:
            messagebox.showwarning("No Items", "No matched items to add.")
            return
        
        self.callback(self.resolved_items)
        self.destroy()
