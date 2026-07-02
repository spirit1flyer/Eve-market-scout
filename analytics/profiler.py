"""
Lightweight profiler for EVE Market Scout.
Drop checkpoints anywhere to find slowest code paths.

Usage:
    from analytics.profiler import profiler
    
    profiler.checkpoint("starting scan")
    # ... code ...
    profiler.checkpoint("fetched orders")
    # ... more code ...
    profiler.checkpoint("scan complete")
    
    profiler.summary()  # prints timing breakdown
"""

import time
from typing import List, Tuple


class Profiler:
    def __init__(self):
        self._checkpoints: List[Tuple[str, float]] = []
        self._start_time: float = 0.0
        self._enabled: bool = True
    
    def enable(self):
        """Enable profiling."""
        self._enabled = True
    
    def disable(self):
        """Disable profiling (checkpoints become no-ops)."""
        self._enabled = False
    
    def reset(self):
        """Clear all checkpoints."""
        self._checkpoints.clear()
        self._start_time = 0.0
    
    def checkpoint(self, label: str):
        """Record a checkpoint with timestamp."""
        if not self._enabled:
            return
        
        now = time.perf_counter()
        
        if not self._checkpoints:
            self._start_time = now
        
        self._checkpoints.append((label, now))
        
        # Print live so you see progress
        if len(self._checkpoints) == 1:
            print(f"[PROFILER] {label}")
        else:
            prev_time = self._checkpoints[-2][1]
            delta = now - prev_time
            print(f"[PROFILER] {label} (+{delta:.3f}s)")
    
    def summary(self):
        """Print summary of all checkpoints sorted by duration."""
        if len(self._checkpoints) < 2:
            print("[PROFILER] Not enough checkpoints for summary")
            return
        
        print("\n" + "=" * 60)
        print("PROFILER SUMMARY")
        print("=" * 60)
        
        # Calculate deltas between consecutive checkpoints
        deltas = []
        for i in range(1, len(self._checkpoints)):
            prev_label, prev_time = self._checkpoints[i - 1]
            curr_label, curr_time = self._checkpoints[i]
            delta = curr_time - prev_time
            segment = f"{prev_label} -> {curr_label}"
            deltas.append((segment, delta))
        
        # Sort by duration descending
        deltas.sort(key=lambda x: x[1], reverse=True)
        
        total = self._checkpoints[-1][1] - self._start_time
        
        print(f"\nTotal time: {total:.3f}s\n")
        print("Slowest segments:")
        print("-" * 60)
        
        for segment, delta in deltas[:10]:  # Top 10 slowest
            pct = (delta / total) * 100 if total > 0 else 0
            bar = "#" * int(pct / 2)
            print(f"{delta:7.3f}s ({pct:5.1f}%) {bar}")
            print(f"         {segment}\n")
        
        print("=" * 60)


# Global instance for easy access
profiler = Profiler()
