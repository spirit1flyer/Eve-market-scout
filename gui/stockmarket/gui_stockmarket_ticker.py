"""Stock Market scrolling ticker for EVE Market Scout.

Animated ticker widget showing items with % price change.
"""

import tkinter as tk
from tkinter import ttk
from typing import List


class ScrollingTicker(ttk.Frame):
    """Scrolling ticker showing items with % change."""
    
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        
        self.items = []  # List of (name, pct_change) tuples
        self.scroll_pos = 0
        self.paused = False
        self.scroll_speed = 2  # Pixels per tick
        self.tick_interval = 50  # ms between ticks
        
        # Canvas for scrolling text
        self.canvas = tk.Canvas(self, height=24, bg="#1a1a2e", highlightthickness=0)
        self.canvas.pack(fill=tk.X, expand=True)
        
        # Bind mouse events for pause on hover
        self.canvas.bind("<Enter>", lambda e: self._set_paused(True))
        self.canvas.bind("<Leave>", lambda e: self._set_paused(False))
        
        # Start scrolling
        self._tick()
    
    def _set_paused(self, paused: bool):
        """Set pause state."""
        self.paused = paused
    
    def update_items(self, items: List[tuple]):
        """Update ticker items. Each item is (name, pct_change)."""
        self.items = items
        self._redraw()
    
    def _redraw(self):
        """Redraw all ticker items."""
        self.canvas.delete("all")
        
        if not self.items:
            return
        
        x = -self.scroll_pos
        spacing = 80  # Space between items
        
        for name, pct in self.items:
            # Determine color
            if pct > 0:
                color = "#00ff00"  # Green for gain
                sign = "+"
            elif pct < 0:
                color = "#ff4444"  # Red for loss
                sign = ""
            else:
                color = "#888888"  # Gray for no change
                sign = ""
            
            # Draw item
            text = f"{name} {sign}{pct:.1f}%"
            self.canvas.create_text(x, 12, text=text, fill=color, anchor="w", font=("Segoe UI", 9))
            
            # Move x for next item
            x += len(text) * 7 + spacing
        
        # Store total width for wrapping
        self.total_width = x + self.scroll_pos
    
    def _tick(self):
        """Animation tick."""
        if not self.paused and self.items:
            self.scroll_pos += self.scroll_speed
            
            # Wrap around when all items have scrolled past
            if hasattr(self, 'total_width') and self.scroll_pos > self.total_width:
                self.scroll_pos = 0
            
            self._redraw()
        
        self.after(self.tick_interval, self._tick)
