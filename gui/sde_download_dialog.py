"""Shared modal SDE-download progress dialog.

Factored out of the Stock Market tab so any surface that needs to rebuild the
local SDE can offer a re-download without duplicating the threading / progress
boilerplate. The Reprocess tab uses this for its "Download/Update SDE" button
(the SDE's `type_materials` table only exists after a fresh rebuild).

The download itself runs on a daemon thread with its own asyncio loop; UI
updates are marshalled back to the Tk thread via `tk_queue.submit`.
"""

import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

from core.tk_queue import submit
from gui.gui_window_utils import fit_window


def download_sde_with_progress(
    parent: tk.Misc,
    set_status: Callable[[str], None],
    on_complete: Optional[Callable[[bool], None]] = None,
    confirm_if_present: bool = True,
) -> None:
    """Rebuild the local SDE behind a modal progress dialog.

    Args:
        parent: a Tk widget the dialog is transient to / centred on.
        set_status: status-line setter (called on the Tk thread).
        on_complete: optional callback(success) run on the Tk thread when done.
        confirm_if_present: if True and an SDE already exists, ask before
            re-downloading.
    """
    from sde.sde_manager import get_sde_manager

    sde = get_sde_manager()

    if confirm_if_present and sde.is_available():
        age = sde.get_age_days()
        info = sde.get_version_info()
        count = info.get("record_count", "?")
        try:
            count_str = f"{count:,}"
        except (ValueError, TypeError):
            count_str = str(count)
        if not messagebox.askyesno(
            "SDE Already Downloaded",
            f"SDE database already exists:\n"
            f"  Items: {count_str}\n"
            f"  Age: {age} days\n\n"
            f"Re-download to update?",
        ):
            return

    progress_win = tk.Toplevel(parent)
    progress_win.title("Downloading SDE")
    progress_win.transient(parent)
    progress_win.grab_set()

    frame = ttk.Frame(progress_win, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    status_label = ttk.Label(frame, text="Starting download...")
    status_label.pack(pady=(0, 10))

    progress_var = tk.DoubleVar(value=0)
    progress_bar = ttk.Progressbar(
        frame, variable=progress_var, length=300, mode="determinate"
    )
    progress_bar.pack()
    fit_window(progress_win, min_width=350)

    def update_progress(msg: str, pct: int):
        submit(lambda: status_label.configure(text=msg))
        submit(lambda: progress_var.set(pct))

    def finish(success: bool):
        try:
            progress_win.destroy()
        except Exception:
            pass
        if success:
            info = sde.get_version_info()
            count = info.get("record_count", 0)
            set_status(f"SDE downloaded: {count:,} items")
            messagebox.showinfo(
                "SDE Downloaded",
                f"Successfully downloaded {count:,} item types.",
            )
        else:
            set_status("SDE download failed")
            messagebox.showerror(
                "Download Failed",
                "Failed to download SDE. Check your internet connection.",
            )
        if on_complete is not None:
            on_complete(success)

    def do_download():
        async def _run():
            return await sde.download_and_build(progress_callback=update_progress)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            success = loop.run_until_complete(_run())
        except Exception:
            success = False
        finally:
            loop.close()
        submit(lambda: finish(success))

    threading.Thread(target=do_download, daemon=True).start()
