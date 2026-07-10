"""Industry tab — openable Settings window (2026-07-10 declutter).

Holds the set-once controls that used to crowd the Top Profit page on a
1366x768 screen: the manufacturing + reaction facility rows, the ME/TE/Batch
defaults and the view-extras filters. Every control is bound to the SAME tk
variables the IndustryTabManager created in _build_tab, so the rest of the
tab (_params(), _rebuild_list(), the compute worker) reads identical state
whether this window has ever been opened or not; controls apply on change
exactly like the old inline rows did (facility edits recompute on Return,
view toggles re-filter live). Non-modal on purpose — flip a setting and
watch the list re-rank without closing anything.

The window is a per-manager singleton: opening it again focuses the existing
one. Closing it persists both facility sections (the compute path also saves
them, so values entered without a recompute aren't lost).
"""

import tkinter as tk
from tkinter import ttk

from core.config import get_enabled_hubs
from gui.gui_window_utils import fit_window, make_scrollable

CLR_MUTED = "#666666"


def open_settings(mgr):
    """Open (or focus) the Industry Settings window for *mgr* (singleton)."""
    existing = getattr(mgr, "_settings_win", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_set()
                return existing
        except tk.TclError:
            pass
    win = IndustrySettingsWindow(mgr)
    mgr._settings_win = win
    return win


class IndustrySettingsWindow(tk.Toplevel):
    """Non-modal settings window for the Industry tab."""

    def __init__(self, mgr):
        super().__init__(mgr.frame)
        self.mgr = mgr
        self.title("Industry settings")
        # transient keeps it above the main window; deliberately NO grab_set —
        # the whole point is changing a value while watching the list react.
        self.transient(mgr.frame.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        body = make_scrollable(self)
        self._build_facility(body)
        self._build_reaction(body)
        self._build_defaults(body)
        self._build_fees(body)
        self._build_invention(body)
        self._build_view(body)

        btns = ttk.Frame(self)
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=8)
        ttk.Button(btns, text="Close", command=self._on_close).pack(
            side=tk.RIGHT)
        fit_window(self, min_width=620, min_height=520)

    # ------------------------------------------------------------- helpers

    def _recompute(self):
        self.mgr._compute(refetch=False)

    def _entry(self, parent, row, col, label, var, ret_cb):
        """A label + small entry pair in *parent*'s grid; Return fires ret_cb."""
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w",
                                           padx=(0, 4), pady=2)
        e = ttk.Entry(parent, textvariable=var, width=7)
        e.grid(row=row, column=col + 1, sticky="w", padx=(0, 16), pady=2)
        e.bind("<Return>", lambda ev: ret_cb())
        return e

    def _system_combo(self, parent, row, label, var):
        """Hub-system selector; changing it recomputes like the old inline row."""
        mgr = self.mgr
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=(0, 4), pady=2)
        hub_names = [name for _k, name in get_enabled_hubs()]
        combo = ttk.Combobox(parent, values=hub_names, textvariable=var,
                             width=14, state="readonly")
        combo.grid(row=row, column=1, columnspan=3, sticky="w", pady=2)
        combo.bind("<<ComboboxSelected>>", lambda e: mgr._on_input_change())
        return combo

    # ------------------------------------------------------------ sections

    def _build_facility(self, body):
        mgr = self.mgr
        f = ttk.LabelFrame(body, text="Manufacturing facility", padding=(8, 6))
        f.pack(fill=tk.X, padx=10, pady=(10, 4))
        mgr.facility_combo = self._system_combo(
            f, 0, "System (cost index):", mgr.facility_var)
        self._entry(f, 1, 0, "Facility tax %:", mgr.fac_tax_var, self._recompute)
        self._entry(f, 1, 2, "Cost bonus %:", mgr.cost_bonus_var, self._recompute)
        self._entry(f, 2, 0, "Material bonus %:", mgr.mat_bonus_var,
                    self._recompute)
        # Structure/rig TIME reduction — feeds build + research time only
        # (never cost); a recompute is still harmless and keeps one behavior.
        self._entry(f, 2, 2, "Time bonus %:", mgr.time_bonus_var,
                    self._recompute)
        self._entry(f, 3, 0, "SCC %:", mgr.scc_var, self._recompute)
        ttk.Label(f, text="(SCC/tax are CCP-tuned — verify in-game)",
                  foreground=CLR_MUTED).grid(row=3, column=2, columnspan=2,
                                             sticky="w")

    def _build_reaction(self, body):
        mgr = self.mgr
        f = ttk.LabelFrame(body, text="Reaction facility (T2 input chains)",
                           padding=(8, 6))
        f.pack(fill=tk.X, padx=10, pady=4)
        mgr.rx_system_combo = self._system_combo(
            f, 0, "System (reaction cost index):", mgr.rx_system_var)
        self._entry(f, 1, 0, "Facility tax %:", mgr.rx_tax_var, self._recompute)
        self._entry(f, 1, 2, "Cost bonus %:", mgr.rx_cost_bonus_var,
                    self._recompute)
        self._entry(f, 2, 0, "Material bonus %:", mgr.rx_mat_bonus_var,
                    self._recompute)
        ttk.Label(f, text="(material bonus = reaction rigs only)",
                  foreground=CLR_MUTED).grid(row=2, column=2, columnspan=2,
                                             sticky="w")

    def _build_defaults(self, body):
        mgr = self.mgr
        f = ttk.LabelFrame(body, text="Defaults", padding=(8, 6))
        f.pack(fill=tk.X, padx=10, pady=4)
        self._entry(f, 0, 0, "ME:", mgr.me_var, self._recompute)
        # TE only affects build TIME, not cost — refresh the open detail
        # instead of recomputing (same behavior as the old filter-row field).
        self._entry(f, 0, 2, "TE:", mgr.te_var,
                    mgr._refresh_detail_if_selected)
        self._entry(f, 0, 4, "Batch:", mgr.batch_var, self._recompute)
        ttk.Button(f, text="Apply ME/Batch", command=self._recompute).grid(
            row=0, column=6, padx=(6, 0))

    def _build_fees(self, body):
        """Stage 2: WHO the sell-side broker/tax rates come from. Hierarchy:
        write-in (blank = auto) > selected roster character (skills + base
        standings at the sell hub) > the trading SELLER slot (default)."""
        mgr = self.mgr
        f = ttk.LabelFrame(body, text="Sell fees (broker + sales tax)",
                           padding=(8, 6))
        f.pack(fill=tk.X, padx=10, pady=4)
        choice = mgr.fees_choice

        roster_chars = list(mgr.roster.characters)
        self._fee_char_ids = {c.character_name: c.character_id
                              for c in roster_chars}
        cur_name = "(trading seller)"
        cid = choice.get("char_id")
        if cid:
            match = next((c.character_name for c in roster_chars
                          if c.character_id == cid), None)
            if match:
                cur_name = match
            else:
                # character left the roster — silently back to the default
                choice["char_id"] = None
                mgr._save_setting("fees", choice)
        ttk.Label(f, text="Character:").grid(row=0, column=0, sticky="w",
                                             padx=(0, 4), pady=2)
        self._fee_char_var = tk.StringVar(value=cur_name)
        combo = ttk.Combobox(
            f, values=["(trading seller)"] + sorted(self._fee_char_ids),
            textvariable=self._fee_char_var, width=22, state="readonly")
        combo.grid(row=0, column=1, columnspan=3, sticky="w", pady=2)
        combo.bind("<<ComboboxSelected>>", lambda e: self._on_fee_change())

        def _fmt(v):
            return "" if v is None else f"{v:g}"
        self._fee_broker_var = tk.StringVar(
            value=_fmt(choice.get("broker_override")))
        self._fee_tax_var = tk.StringVar(
            value=_fmt(choice.get("tax_override")))
        e1 = self._entry(f, 1, 0, "Broker % (blank = auto):",
                         self._fee_broker_var, self._on_fee_change)
        e2 = self._entry(f, 1, 2, "Tax % (blank = auto):",
                         self._fee_tax_var, self._on_fee_change)
        # write-ins should also land without an explicit Return
        e1.bind("<FocusOut>", lambda ev: self._on_fee_change())
        e2.bind("<FocusOut>", lambda ev: self._on_fee_change())

        self._fee_preview = ttk.Label(f, foreground=CLR_MUTED)
        self._fee_preview.grid(row=2, column=0, columnspan=4, sticky="w",
                               pady=(4, 0))
        self._refresh_fee_preview()

    def _on_fee_change(self):
        """Persist the fee choice and recompute (fees re-rank every margin)."""
        from industry.industry_fees import _as_float
        mgr = self.mgr
        old = dict(mgr.fees_choice)
        mgr.fees_choice["char_id"] = self._fee_char_ids.get(
            self._fee_char_var.get())
        mgr.fees_choice["broker_override"] = _as_float(
            self._fee_broker_var.get())
        mgr.fees_choice["tax_override"] = _as_float(self._fee_tax_var.get())
        if mgr.fees_choice == old:
            return   # FocusOut fires liberally — only react to real changes
        mgr._save_setting("fees", mgr.fees_choice)
        self._refresh_fee_preview()
        mgr._compute(refetch=False)

    def _refresh_fee_preview(self):
        mgr = self.mgr
        fees, label = mgr._resolve_fees(allow_fetch=False)
        self._fee_preview.configure(
            text=f"Effective at {mgr.sell_var.get()}: broker "
                 f"{fees.broker_fee_pct:.2f}% + tax {fees.sales_tax_pct:.2f}%"
                 f"  ({label})")
        mgr._update_legend()

    def _build_invention(self, body):
        """Stage 3: WHO the T2/T3 invention-probability skills come from.
        Hierarchy: fill-in level (overrides everything) > per-item 'Built by'
        pick > the character selected here > the assumed level."""
        mgr = self.mgr
        f = ttk.LabelFrame(body, text="Invention skills (T2/T3 probability)",
                           padding=(8, 6))
        f.pack(fill=tk.X, padx=10, pady=4)
        choice = mgr.inv_choice

        roster_chars = list(mgr.roster.characters)
        self._inv_char_ids = {c.character_name: c.character_id
                              for c in roster_chars}
        cur_name = "(assumed level)"
        cid = choice.get("char_id")
        if cid:
            match = next((c.character_name for c in roster_chars
                          if c.character_id == cid), None)
            if match:
                cur_name = match
            else:
                choice["char_id"] = None
                mgr._save_setting("invention_skills", choice)
        ttk.Label(f, text="Character:").grid(row=0, column=0, sticky="w",
                                             padx=(0, 4), pady=2)
        self._inv_char_var = tk.StringVar(value=cur_name)
        combo = ttk.Combobox(
            f, values=["(assumed level)"] + sorted(self._inv_char_ids),
            textvariable=self._inv_char_var, width=22, state="readonly")
        combo.grid(row=0, column=1, columnspan=3, sticky="w", pady=2)
        combo.bind("<<ComboboxSelected>>", lambda e: self._on_inv_change())

        ov = choice.get("level_override")
        self._inv_level_var = tk.StringVar(
            value="" if ov is None else str(int(ov)))
        e = self._entry(f, 1, 0, "Fill-in level (overrides all, blank = auto):",
                        self._inv_level_var, self._on_inv_change)
        e.bind("<FocusOut>", lambda ev: self._on_inv_change())
        self._entry(f, 1, 2, "Assumed level (fallback):", mgr.inv_skill_var,
                    mgr._on_assumed_skill)
        ttk.Label(f, text="A per-item 'Built by' pick still beats the global "
                          "character; the detail panel names the source used.",
                  foreground=CLR_MUTED).grid(row=2, column=0, columnspan=4,
                                             sticky="w", pady=(4, 0))

    def _on_inv_change(self):
        mgr = self.mgr
        old = dict(mgr.inv_choice)
        mgr.inv_choice["char_id"] = self._inv_char_ids.get(
            self._inv_char_var.get())
        raw = self._inv_level_var.get().strip()
        lvl = None
        if raw:
            try:
                lvl = max(0, min(5, int(float(raw))))
            except ValueError:
                lvl = None
        mgr.inv_choice["level_override"] = lvl
        if mgr.inv_choice == old:
            return
        mgr._save_setting("invention_skills", mgr.inv_choice)
        mgr._update_legend()
        mgr._compute(refetch=False)

    def _build_view(self, body):
        mgr = self.mgr
        f = ttk.LabelFrame(body, text="View filters", padding=(8, 6))
        f.pack(fill=tk.X, padx=10, pady=4)

        def check(row, col, text, var):
            ttk.Checkbutton(f, text=text, variable=var,
                            command=mgr._rebuild_list).grid(
                row=row, column=col, sticky="w", padx=(0, 16), pady=2)

        check(0, 0, "Show unpriced", mgr.show_unpriced_var)
        # Capitals are hidden by default — hugely expensive and need months of
        # BPO ME research to be worth building. Upwell + POS structures are
        # reasonable builds, so they stay visible unless the user opts out.
        check(0, 1, "Sub-cap only", mgr.subcap_only_var)
        check(1, 0, "Hide Upwell", mgr.hide_upwell_var)
        check(1, 1, "Hide POS", mgr.hide_pos_var)
        # Blueprint-source filter: buyable BPO vs BPC-only (drops/invented).
        ttk.Label(f, text="Blueprint:").grid(row=2, column=0, sticky="w",
                                             pady=(6, 0))
        combo = ttk.Combobox(f, values=["All", "BPO only", "BPC-only"],
                             textvariable=mgr.bp_filter_var, width=10,
                             state="readonly")
        combo.grid(row=2, column=1, sticky="w", pady=(6, 0))
        combo.bind("<<ComboboxSelected>>", lambda e: mgr._rebuild_list())
        mgr.ignored_btn = ttk.Button(f, text="Ignored…",
                                     command=mgr._manage_ignored)
        mgr.ignored_btn.grid(row=2, column=2, sticky="w", padx=(16, 0),
                             pady=(6, 0))
        mgr._update_ignored_btn()

    # -------------------------------------------------------------- close

    def _on_close(self):
        # Persist anything typed without a Return/recompute, then drop the
        # manager's references to widgets that are about to be destroyed
        # (their tk variables live on — only the widgets go away).
        try:
            self.mgr._save_facility_settings()
            self.mgr._save_rx_settings()
        finally:
            self.mgr.facility_combo = None
            self.mgr.rx_system_combo = None
            self.mgr.ignored_btn = None
            self.mgr._settings_win = None
            self.destroy()
