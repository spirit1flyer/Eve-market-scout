"""Treeview sorting utilities for EVE Market Scout GUI."""

from tkinter import ttk
from typing import Dict, Optional, Tuple, Callable, Any, List
from dataclasses import dataclass


def parse_isk_value(val: str, reverse: bool = False) -> float:
    """
    Parse ISK-formatted string to float for sorting.
    
    Handles: commas, parentheses (projected), tildes (~), M/B/K suffixes.
    
    Args:
        val: ISK-formatted string like "1,234.56", "~500M", "(1.2B)"
        reverse: If True, missing values sort to -inf, else +inf
    
    Returns:
        Float value for sorting
    """
    if val == "-" or val == "--" or not val:
        return float("-inf") if reverse else float("inf")
    
    # Strip formatting characters
    val = val.replace(",", "").replace("(", "").replace(")", "").replace("~", "").replace("%", "")
    
    # Handle K/M/B suffixes
    if val.endswith("K"):
        try:
            return float(val[:-1]) * 1_000
        except ValueError:
            pass
    if val.endswith("M"):
        try:
            return float(val[:-1]) * 1_000_000
        except ValueError:
            pass
    if val.endswith("B"):
        try:
            return float(val[:-1]) * 1_000_000_000
        except ValueError:
            pass
    
    try:
        return float(val)
    except ValueError:
        return float("-inf") if reverse else float("inf")


@dataclass
class SortLevel:
    """A single sort level (column + direction)."""
    column: str
    reverse: bool = False


class NestedSortManager:
    """Manages nested (two-level) sorting for Treeview widgets.
    
    Usage:
        sorter = NestedSortManager(numeric_columns={'price', 'volume', 'profit'})
        
        # In column click handler:
        sorter.on_column_click('price')
        sorter.apply_sort(tree, items, get_sort_value)
        sorter.update_headers(tree, base_titles)
    
    Sort behavior:
        - First click on column: becomes primary sort [1]
        - Click same primary: toggles direction
        - Click different column when primary exists: becomes secondary [2]
        - Click same secondary: toggles its direction  
        - Click third different column: resets, becomes new primary
    """
    
    def __init__(self, numeric_columns: set = None):
        """
        Args:
            numeric_columns: Set of column keys that should sort numerically
        """
        self.numeric_columns = numeric_columns or set()
        self.primary: Optional[SortLevel] = None
        self.secondary: Optional[SortLevel] = None
    
    def on_column_click(self, column: str) -> None:
        """Handle column header click, updating sort state.
        
        Args:
            column: The column key that was clicked
        """
        if self.primary is None:
            # No sort yet - this becomes primary
            self.primary = SortLevel(column, False)
            self.secondary = None
        
        elif self.primary.column == column:
            # Clicked primary - toggle direction
            self.primary.reverse = not self.primary.reverse
        
        elif self.secondary is not None and self.secondary.column == column:
            # Clicked secondary - toggle its direction
            self.secondary.reverse = not self.secondary.reverse
        
        elif self.secondary is None:
            # Have primary, no secondary - this becomes secondary
            self.secondary = SortLevel(column, False)
        
        else:
            # Already have primary + secondary, clicked third column
            # Reset: this becomes new primary
            self.primary = SortLevel(column, False)
            self.secondary = None
    
    def get_sort_key(self, value_getter: Callable[[str], Any]) -> Tuple:
        """Get compound sort key for an item.
        
        Args:
            value_getter: Function that takes column name and returns value
        
        Returns:
            Tuple of (primary_key, secondary_key) for sorting
        """
        primary_key = 0
        secondary_key = 0
        
        if self.primary:
            raw_val = value_getter(self.primary.column)
            primary_key = self._parse_value(raw_val, self.primary.column, self.primary.reverse)
            if self.primary.reverse:
                primary_key = self._negate_key(primary_key)
        
        if self.secondary:
            raw_val = value_getter(self.secondary.column)
            secondary_key = self._parse_value(raw_val, self.secondary.column, self.secondary.reverse)
            if self.secondary.reverse:
                secondary_key = self._negate_key(secondary_key)
        
        return (primary_key, secondary_key)
    
    def _parse_value(self, val: Any, column: str, reverse: bool) -> Any:
        """Parse a value for sorting."""
        if column in self.numeric_columns:
            if isinstance(val, (int, float)):
                return val
            return parse_isk_value(str(val), reverse)
        else:
            # String sort
            return str(val).lower() if val else ("" if reverse else "zzzzz")
    
    def _negate_key(self, key: Any) -> Any:
        """Negate a sort key for reverse sorting."""
        if isinstance(key, (int, float)):
            return -key
        elif isinstance(key, str):
            # For strings, we can't negate, so we handle reverse in apply_sort
            return key
        return key
    
    def apply_sort(self, tree: ttk.Treeview, get_item_values: Callable[[str], Dict[str, Any]] = None) -> None:
        """Sort the treeview using current sort state.
        
        Args:
            tree: The Treeview widget to sort
            get_item_values: Optional function to get values dict for an item ID.
                            If None, uses tree.set() to get displayed values.
        """
        if not self.primary:
            return
        
        items = list(tree.get_children(""))
        
        def get_value(item_id: str, column: str) -> Any:
            if get_item_values:
                values = get_item_values(item_id)
                return values.get(column, "")
            return tree.set(item_id, column)
        
        # Sort with compound key
        def sort_key(item_id: str) -> Tuple:
            return self.get_sort_key(lambda col: get_value(item_id, col))
        
        # For string columns with reverse, we need special handling
        primary_is_string = self.primary and self.primary.column not in self.numeric_columns
        secondary_is_string = self.secondary and self.secondary.column not in self.numeric_columns
        
        if primary_is_string or secondary_is_string:
            # Complex case: need to handle string reversal properly
            items_with_keys = [(item, sort_key(item)) for item in items]
            
            # Custom comparator
            def compare(a, b):
                key_a, key_b = a[1], b[1]
                
                # Compare primary
                if self.primary:
                    if self.primary.column in self.numeric_columns:
                        cmp1 = (key_a[0] > key_b[0]) - (key_a[0] < key_b[0])
                    else:
                        cmp1 = (key_a[0] > key_b[0]) - (key_a[0] < key_b[0])
                        if self.primary.reverse:
                            cmp1 = -cmp1
                    if cmp1 != 0:
                        return cmp1
                
                # Compare secondary
                if self.secondary:
                    if self.secondary.column in self.numeric_columns:
                        cmp2 = (key_a[1] > key_b[1]) - (key_a[1] < key_b[1])
                    else:
                        cmp2 = (key_a[1] > key_b[1]) - (key_a[1] < key_b[1])
                        if self.secondary.reverse:
                            cmp2 = -cmp2
                    return cmp2
                
                return 0
            
            from functools import cmp_to_key
            items_with_keys.sort(key=cmp_to_key(compare))
            sorted_items = [item for item, _ in items_with_keys]
        else:
            # Simple case: all numeric, negation handles reversal
            sorted_items = sorted(items, key=sort_key)
        
        # Rearrange items in tree
        for idx, item in enumerate(sorted_items):
            tree.move(item, "", idx)
    
    def update_headers(self, tree: ttk.Treeview, base_titles: Dict[str, str]) -> None:
        """Update column headers to show sort indicators.
        
        Args:
            tree: The Treeview widget
            base_titles: Dict mapping column keys to base display titles
        """
        for col in tree["columns"]:
            base = base_titles.get(col, col)
            
            if self.primary and self.primary.column == col:
                arrow = "v" if self.primary.reverse else "^"
                tree.heading(col, text=f"[1]{arrow} {base}")
            elif self.secondary and self.secondary.column == col:
                arrow = "v" if self.secondary.reverse else "^"
                tree.heading(col, text=f"[2]{arrow} {base}")
            else:
                tree.heading(col, text=base)
    
    def clear(self) -> None:
        """Reset sort state."""
        self.primary = None
        self.secondary = None


def sort_treeview(tree: ttk.Treeview, col: str, sort_state: Dict[str, bool],
                  col_titles: Dict[str, str], numeric_cols: set):
    """
    Sort a treeview by column when header is clicked (legacy single-column sort).
    
    Args:
        tree: The treeview widget to sort
        col: Column key to sort by
        sort_state: Dict tracking sort direction per column (modified in place)
        col_titles: Dict mapping column keys to display titles
        numeric_cols: Set of column keys that should be sorted numerically
    """
    # Toggle sort direction
    reverse = sort_state.get(col, False)
    sort_state[col] = not reverse
    
    # Get all items with their values
    items = [(tree.set(item, col), item) for item in tree.get_children("")]
    
    if col in numeric_cols:
        items.sort(key=lambda x: parse_isk_value(x[0], reverse), reverse=reverse)
    else:
        items.sort(key=lambda x: x[0].lower(), reverse=reverse)
    
    # Rearrange items
    for idx, (_, item) in enumerate(items):
        tree.move(item, "", idx)
    
    # Reset all headers to base titles
    for c in tree["columns"]:
        tree.heading(c, text=col_titles.get(c, c))
    
    # Add arrow to sorted column
    arrow = " v" if reverse else " ^"
    tree.heading(col, text=col_titles.get(col, col) + arrow)
