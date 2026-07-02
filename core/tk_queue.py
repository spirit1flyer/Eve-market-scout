"""Thread-safe task queue for Tk UI updates.

All background threads use submit() to schedule UI work on the main thread.
Main thread polls via root.after() during mainloop.

Usage from any thread:
    from core.tk_queue import submit
    submit(lambda: my_label.configure(text="Done"))

Setup in main.py (once, before mainloop):
    from core.tk_queue import start_polling
    start_polling(root)
"""

import queue
import time


_queue = queue.Queue()
_root = None


def submit(func):
    """Submit a function to run on the main thread.

    Thread-safe. Can be called from any thread.
    The function will execute during the next poll cycle (~50ms).
    """
    _queue.put(func)


def schedule_idle(callback):
    """Schedule callback for the next Tk idle tick.

    Use for coalescing repeated work: multiple callers within one mainloop
    iteration cooperate via their own dirty/scheduled flag, and the actual
    work runs once when Tk becomes idle. Must be called from the UI thread.

    Falls back to immediate execution if start_polling has not been called
    yet (e.g. during module import or in tests).
    """
    if _root is None:
        cb_name = getattr(callback, "__qualname__", None) or getattr(callback, "__name__", None) or repr(callback)
        print(f"[TkQueue] schedule_idle FALLBACK (no root yet): running {cb_name} inline")
        callback()
        return
    _root.after_idle(callback)


def drain():
    """Drain and execute all pending tasks. Call from main thread only."""
    _cycle_t0 = time.perf_counter()
    _count = 0
    _slowest_name = None
    _slowest_dur = 0.0
    while True:
        try:
            func = _queue.get_nowait()
            _t0 = time.perf_counter()
            try:
                func()
            except Exception as e:
                print(f"[TkQueue] Error executing task: {e}")
            _dur = time.perf_counter() - _t0
            _count += 1
            if _dur > _slowest_dur:
                _slowest_dur = _dur
                _slowest_name = getattr(func, "__qualname__", None) or getattr(func, "__name__", None) or repr(func)
        except queue.Empty:
            break
    _cycle_dur = time.perf_counter() - _cycle_t0
    # Only log notable cycles to avoid 20-per-second log spam from idle drains
    if _cycle_dur > 0.050 or _count > 5:
        print(f"[PerfTimer] tk_queue.drain count={_count} total={_cycle_dur*1000:.0f}ms slowest={_slowest_name}({_slowest_dur*1000:.0f}ms)")


def start_polling(root, interval_ms=50):
    """Start polling the queue from root's after() loop.

    Call once from main thread, after root is created but before mainloop().
    The poll runs inside mainloop via root.after(), which is safe because
    root.after() is called FROM the main thread.
    """
    global _root
    _root = root

    def _poll():
        drain()
        root.after(interval_ms, _poll)

    root.after(interval_ms, _poll)
    print("[TkQueue] Polling started")
