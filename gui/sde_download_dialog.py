"""Shared modal SDE-download progress dialog.

Factored out of the Stock Market tab so any surface that needs to rebuild the
local SDE can offer a re-download without duplicating the threading / progress
boilerplate. The Reprocess tab uses this for its "Download/Update SDE" button
(the SDE's `type_materials` table only exists after a fresh rebuild).

Every manual update rebuilds both the item/type database and the industry
recipe database. The download runs on a daemon thread with its own asyncio
loop; UI updates are marshalled back to the Tk thread via `tk_queue.submit`.
"""

import asyncio
import threading
import time
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
    from sde.sde_industry import get_sde_industry_db

    sde = get_sde_manager()
    industry_sde = get_sde_industry_db()
    old_type_info = sde.get_version_info()
    old_industry_info = industry_sde.get_version_info()

    if confirm_if_present and (sde.is_available() or industry_sde.is_available()):
        age = sde.get_age_days()
        count = old_type_info.get("record_count", "?")
        industry_age = industry_sde.get_age_days()
        product_count = old_industry_info.get("products_count", "?")
        try:
            count_str = f"{count:,}"
        except (ValueError, TypeError):
            count_str = str(count)
        try:
            product_count_str = f"{product_count:,}"
        except (ValueError, TypeError):
            product_count_str = str(product_count)
        if not messagebox.askyesno(
            "SDE Already Downloaded",
            f"SDE databases already exist:\n"
            f"  Item types: {count_str} ({age} days old)\n"
            f"  Manufacturing recipes: {product_count_str} "
            f"({industry_age} days old)\n\n"
            f"Re-download both to update?",
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

    outcome = {"types": False, "industry": False, "started": None}

    def finish(success: bool):
        try:
            progress_win.destroy()
        except Exception:
            pass
        new_type_info = sde.get_version_info()
        new_industry_info = industry_sde.get_version_info()
        elapsed = (time.perf_counter() - outcome["started"]
                   if outcome["started"] is not None else 0.0)
        print(
            f"[SDEIndustry] manual_update_done success={success} "
            f"seconds={elapsed:.2f} types_ok={outcome['types']} "
            f"recipes_ok={outcome['industry']} "
            f"new_types={new_type_info.get('record_count', 0)} "
            f"new_recipe_rows={new_industry_info.get('products_count', 0)} "
            f"new_unique_products="
            f"{new_industry_info.get('unique_products_count', 'unknown')}"
        )
        if success:
            count = new_type_info.get("record_count", 0)
            products = new_industry_info.get("products_count", 0)
            set_status(f"SDE downloaded: {count:,} item types, "
                       f"{products:,} manufacturing recipes")
            messagebox.showinfo(
                "SDE Downloaded",
                f"Successfully downloaded {count:,} item types and "
                f"{products:,} manufacturing recipes.",
            )
        else:
            partial = outcome["types"] and not outcome["industry"]
            set_status("Item types updated, but industry recipes failed"
                       if partial else "SDE download failed")
            messagebox.showerror(
                "Download Failed",
                ("Item types were updated, but the manufacturing recipe "
                 "download failed. The previous recipe database is still safe."
                 if partial else
                 "Failed to download SDE. Check your internet connection."),
            )
        if on_complete is not None:
            on_complete(success)

    def do_download():
        outcome["started"] = time.perf_counter()
        print(
            f"[SDEIndustry] manual_update_start "
            f"old_types={old_type_info.get('record_count', 0)} "
            f"old_recipe_rows={old_industry_info.get('products_count', 0)} "
            f"old_unique_products="
            f"{old_industry_info.get('unique_products_count', 'unknown')}"
        )

        async def _run():
            def type_progress(msg: str, pct: int):
                update_progress(f"Items: {msg}", int(pct * 0.55))

            def industry_progress(msg: str, pct: int):
                update_progress(f"Recipes: {msg}", 55 + int(pct * 0.45))

            outcome["types"] = await sde.download_and_build(
                progress_callback=type_progress)
            if not outcome["types"]:
                return False
            outcome["industry"] = await industry_sde.download_and_build(
                progress_callback=industry_progress)
            return outcome["industry"]

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
