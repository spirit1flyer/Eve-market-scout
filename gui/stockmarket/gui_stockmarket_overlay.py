"""Stock Market locked overlay mixin for EVE Market Scout.

Shows overlay when 3-year history not ready, with progress during import
and restart prompt when complete.

Expected parent attributes:
    frame: ttk.Frame - main Stock Market frame
"""

import tkinter as tk
from tkinter import ttk, messagebox

from history.background_import import get_background_import_status


class StockMarketOverlayMixin:
    """Mixin providing locked overlay for Stock Market tab."""
    
    def _create_locked_overlay(self):
        """Create overlay that covers tab when full history not ready."""
        # Overlay frame - placed over hub_notebook when locked
        self.locked_overlay = ttk.Frame(self.frame)
        
        # Center content
        center = ttk.Frame(self.locked_overlay)
        center.place(relx=0.5, rely=0.4, anchor=tk.CENTER)
        
        # Title
        self.overlay_title = ttk.Label(
            center,
            text="Preparing Stock Market Data",
            font=("Segoe UI", 14, "bold")
        )
        self.overlay_title.pack(pady=(0, 15))
        
        # Status message
        self.overlay_status = ttk.Label(
            center,
            text="Building 3-year price history...",
            font=("Segoe UI", 10)
        )
        self.overlay_status.pack(pady=(0, 10))
        
        # Progress bar
        self.overlay_progress_var = tk.DoubleVar(value=0)
        self.overlay_progress = ttk.Progressbar(
            center,
            variable=self.overlay_progress_var,
            length=300,
            mode="determinate"
        )
        self.overlay_progress.pack(pady=(0, 10))
        
        # Progress text (e.g., "450/1189 files")
        self.overlay_progress_text = ttk.Label(
            center,
            text="",
            font=("Segoe UI", 9),
            foreground="gray"
        )
        self.overlay_progress_text.pack(pady=(0, 20))
        
        # Restart button (hidden until needed)
        self.overlay_restart_btn = ttk.Button(
            center,
            text="Restart App to Activate",
            command=self._on_restart_prompt
        )
        # Not packed initially - shown when restart needed

        # Turn-on button (shown only by the "off" overlay). Drives set_enabled,
        # the same path as the main control-bar checkbutton.
        self.overlay_enable_btn = ttk.Button(
            center,
            text="Turn On Stock Market",
            command=self._on_turn_on_clicked
        )
        # Not packed initially - shown when the master switch is off

        # Track lock state
        self._is_locked = False
        self._lock_poll_job = None

    def _on_turn_on_clicked(self):
        """Overlay 'Turn On Stock Market' button — flips the master switch on.
        set_enabled mirrors the new state back to the main-window checkbutton
        via the registered enabled-changed callback."""
        self.set_enabled(True)
    
    def _poll_lock_state(self):
        """Poll background import status and update overlay.

        Cold-start orchestrator (StockMarketColdStartMixin) takes priority:
        if it's mid-phase the overlay reflects phase_state.  When it's
        in phase 0 (detection) or has finished, the legacy logic below
        decides what to show.  Falls through cleanly during the scaffold
        period when only phase 0 is implemented.
        """
        try:
            # Master switch off → show the "off" overlay and keep polling
            # at a slow cadence so flipping it back on is picked up promptly.
            if not getattr(self.settings, "stock_market_enabled", True):
                self._show_off_overlay()
                self._lock_poll_job = self.frame.after(1000, self._poll_lock_state)
                return

            ps = getattr(self, "phase_state", None)
            if ps is not None:
                if ps.error:
                    self._show_phase_error_overlay(ps.error)
                    self._lock_poll_job = self.frame.after(
                        5000, self._poll_lock_state
                    )
                    return
                if not ps.done:
                    # Tab is locked whenever the orchestrator hasn't
                    # signalled completion — including the brief phase 0
                    # detection window before phase 3 (or future phases)
                    # starts.  done=True is the only "ready" signal.
                    self._show_phase_progress_overlay(ps)
                    self._lock_poll_job = self.frame.after(
                        500, self._poll_lock_state
                    )
                    return

            status = get_background_import_status()

            # Check if profiles exist - if so, Stock Market is usable
            # even if restart is still needed (for scanner DB merge).
            # Keep polling at a slow rate so we still pick up
            # orchestrator phase transitions (e.g. user deleting
            # profiles between phases).
            if self._has_profiles():
                self._hide_overlay()
                self._lock_poll_job = self.frame.after(2000, self._poll_lock_state)
                return

            # Check if restart required (import complete, waiting for swap)
            if status.get('restart_required', False):
                self._show_restart_overlay()
                self._lock_poll_job = self.frame.after(10000, self._poll_lock_state)
                return

            # Check if background import is running
            if status.get('running', False):
                self._show_progress_overlay(status)
                self._lock_poll_job = self.frame.after(1000, self._poll_lock_state)
                return

            # Check if we have full history
            from history.market_history import get_market_history_db
            from gui.gui_migration import check_has_full_history

            db = get_market_history_db()
            if check_has_full_history(db):
                self._hide_overlay()
                self._lock_poll_job = self.frame.after(2000, self._poll_lock_state)
                return
            
            # No full history and not importing - show waiting message
            self._show_waiting_overlay()
            self._lock_poll_job = self.frame.after(5000, self._poll_lock_state)
            
        except Exception as e:
            print(f"[StockMarket] Lock state poll error: {e}")
            self._hide_overlay()
    
    def _has_profiles(self):
        """Check if any profiles have been built."""
        try:
            profiles = self.profiles.get_all_profiles()
            return len(profiles) > 0
        except Exception:
            return False
    
    def _show_progress_overlay(self, status: dict):
        """Show overlay with import progress."""
        if not self._is_locked:
            self._is_locked = True
            self.locked_overlay.place(
                in_=self.frame, relx=0, rely=0,
                relwidth=1.0, relheight=1.0
            )
            self.locked_overlay.lift()
        
        self.overlay_title.configure(text="Preparing Stock Market Data")
        
        current = status.get('current', 0)
        total = status.get('total', 0)
        
        if total > 0:
            pct = (current / total) * 100
            self.overlay_progress_var.set(pct)
            self.overlay_progress_text.configure(text=f"{current}/{total} files")
            self.overlay_status.configure(text=status.get('status', 'Importing...'))
        else:
            self.overlay_status.configure(text=status.get('status', 'Starting import...'))
            self.overlay_progress_text.configure(text="")
        
        # Ensure progress bar visible, restart button hidden
        self.overlay_progress.pack(pady=(0, 10))
        self.overlay_progress_text.pack(pady=(0, 20))
        self.overlay_restart_btn.pack_forget()
        self.overlay_enable_btn.pack_forget()
    
    def _show_restart_overlay(self):
        """Show overlay prompting for restart."""
        if not self._is_locked:
            self._is_locked = True
            self.locked_overlay.place(
                in_=self.frame, relx=0, rely=0,
                relwidth=1.0, relheight=1.0
            )
            self.locked_overlay.lift()
        
        self.overlay_title.configure(text="Full History Ready!")
        self.overlay_status.configure(
            text="3-year price history has been built.\n"
                 "Restart the app to activate Stock Market features."
        )
        
        self.overlay_progress.pack_forget()
        self.overlay_progress_text.pack_forget()
        self.overlay_enable_btn.pack_forget()
        self.overlay_restart_btn.pack(pady=(10, 0))
    
    def _show_waiting_overlay(self):
        """Show overlay when no full history and not importing."""
        if not self._is_locked:
            self._is_locked = True
            self.locked_overlay.place(
                in_=self.frame, relx=0, rely=0,
                relwidth=1.0, relheight=1.0
            )
            self.locked_overlay.lift()
        
        self.overlay_title.configure(text="Stock Market Setup Required")
        self.overlay_status.configure(
            text="Stock Market features require 3-year price history.\n"
                 "Use the scanner - full history will download in background."
        )

        self.overlay_progress.pack_forget()
        self.overlay_progress_text.pack_forget()
        self.overlay_restart_btn.pack_forget()
        self.overlay_enable_btn.pack_forget()
    
    def _show_off_overlay(self):
        """Show overlay for the master 'Stock Market off' state."""
        if not self._is_locked:
            self._is_locked = True
            self.locked_overlay.place(
                in_=self.frame, relx=0, rely=0,
                relwidth=1.0, relheight=1.0
            )
            self.locked_overlay.lift()

        try:
            self.overlay_progress.stop()
        except Exception:
            pass
        self.overlay_title.configure(text="Stock Market is Off")
        self.overlay_status.configure(
            text="Automatic data builds and ESI pulls are paused.\n"
                 "Turn it back on to resume profile builds, hub pulls,\n"
                 "material filter and leading indicators."
        )
        self.overlay_progress.pack_forget()
        self.overlay_progress_text.pack_forget()
        self.overlay_restart_btn.pack_forget()
        self.overlay_enable_btn.pack(pady=(10, 0))

    def _hide_overlay(self):
        """Hide the locked overlay - tab is usable."""
        self.overlay_enable_btn.pack_forget()
        if self._is_locked:
            self._is_locked = False
            self.locked_overlay.place_forget()

    # =========================================================================
    # Cold-start orchestrator overlays (phase_state driven)
    # =========================================================================

    _PHASE_TOTAL = 7  # phases 1..7; phase 0 (detect) doesn't show overlay

    def _show_phase_progress_overlay(self, ps):
        """Show overlay reflecting cold-start orchestrator phase_state."""
        if not self._is_locked:
            self._is_locked = True
            self.locked_overlay.place(
                in_=self.frame, relx=0, rely=0,
                relwidth=1.0, relheight=1.0
            )
            self.locked_overlay.lift()

        self.overlay_title.configure(text=ps.phase_name or "Preparing Stock Market Data")

        if ps.total > 0:
            try:
                self.overlay_progress.stop()
            except Exception:
                pass
            pct = (ps.current / ps.total) * 100
            self.overlay_progress_var.set(pct)
            self.overlay_progress.configure(mode="determinate", maximum=100)
            counter = f"{ps.current:,}/{ps.total:,}"
        else:
            if str(self.overlay_progress.cget("mode")) != "indeterminate":
                self.overlay_progress.configure(mode="indeterminate")
                try:
                    self.overlay_progress.start(80)
                except Exception:
                    pass
            counter = ""

        # Phase 0 (detection) doesn't have a meaningful "phase X of N"
        # number — it's the orchestrator inspecting state before any
        # real work.  Suppress the phase tag in that case.
        if ps.current_phase >= 1:
            phase_tag = f"Phase {ps.current_phase} of {self._PHASE_TOTAL}"
        else:
            phase_tag = ""
        bits = [b for b in (phase_tag, counter, ps.detail) if b]
        self.overlay_progress_text.configure(text=" — ".join(bits))

        self.overlay_status.configure(text="")
        self.overlay_progress.pack(pady=(0, 10))
        self.overlay_progress_text.pack(pady=(0, 20))
        self.overlay_restart_btn.pack_forget()
        self.overlay_enable_btn.pack_forget()

    def _show_phase_error_overlay(self, error: str):
        """Show overlay when cold-start orchestrator hits a fatal error."""
        if not self._is_locked:
            self._is_locked = True
            self.locked_overlay.place(
                in_=self.frame, relx=0, rely=0,
                relwidth=1.0, relheight=1.0
            )
            self.locked_overlay.lift()

        try:
            self.overlay_progress.stop()
        except Exception:
            pass
        self.overlay_title.configure(text="Stock Market Setup Failed")
        self.overlay_status.configure(
            text=f"Cold-start error:\n{error}\n\n"
                 "Check the log for details and restart the app."
        )
        self.overlay_progress.pack_forget()
        self.overlay_progress_text.pack_forget()
        self.overlay_restart_btn.pack_forget()
        self.overlay_enable_btn.pack_forget()

    def _on_restart_prompt(self):
        """Handle restart button click."""
        result = messagebox.askokcancel(
            "Restart Required",
            "The app needs to restart to activate full price history.\n\n"
            "Click OK to close the app. Then relaunch manually.",
            icon="info"
        )
        if result:
            try:
                self.frame.winfo_toplevel().destroy()
            except Exception:
                pass
