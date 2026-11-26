"""Saved blueprint quote details, persistent overrides and feed settings."""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timezone

from gui.industry.gui_industry_details import CLR_MUTED, CLR_BAD, isk


def _date(value):
    return (datetime.fromtimestamp(value, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            if value else "unknown")


class IndustryPricingMixin:
    def _build_blueprint_contract_controls(self, parent):
        box = ttk.LabelFrame(parent, text="Jita blueprint prices", padding=6)
        box.pack(fill=tk.X, padx=8, pady=4)
        self.blueprint_feed_label = ttk.Label(box, foreground=CLR_MUTED, wraplength=420)
        self.blueprint_feed_label.pack(fill=tk.X)
        ttk.Label(box, text="Automatic daily refresh; overrides stay until cleared.",
                  foreground=CLR_MUTED).pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row, text="Keep observations:").pack(side=tk.LEFT)
        self._retention_choices = {"7 days": 7, "30 days": 30, "90 days": 90,
                                   "365 days": 365, "Forever": None}
        current = self.bpc_pricing.store.retention()
        label = next((k for k, v in self._retention_choices.items() if v == current),
                     f"{current} days")
        self._retention_choices[label] = current
        self.blueprint_retention_var = tk.StringVar(value=label)
        self._combo(row, self.blueprint_retention_var, list(self._retention_choices),
                    self._save_blueprint_retention, width=12)
        self._update_blueprint_feed_label()

    def _update_blueprint_feed_label(self):
        snapshot = self.bpc_pricing.store.latest()
        text = (f"Saved Jita snapshot; source checked {_date(snapshot['source_checked_at'])}"
                if snapshot else "No saved offers yet; daily Jita job is pending.")
        if snapshot and not snapshot["complete"]:
            text += " (cached coverage; full daily refresh pending)"
        self.blueprint_feed_label.configure(text=text)

    def _save_blueprint_retention(self):
        self.bpc_pricing.store.set_retention(
            self._retention_choices[self.blueprint_retention_var.get()])
        self.set_status("Industry: blueprint observation retention saved")

    def _on_blueprint_prices_changed(self):
        self._update_blueprint_feed_label()
        if self._computing:
            self._pending_pricing_recompute = True
            return
        self._compute(refetch=False)
        self.owned_panel.refresh()

    def _build_blueprint_pricing(self, tid, result):
        quote = result.get("_blueprint") or {}
        bid = quote.get("blueprint_id") or self.sde.get_blueprint_for_item(tid)
        if bid is None:
            return
        is_copy = quote.get("is_copy", not self.bpo_map.get(tid, True))
        frame = ttk.LabelFrame(self.detail, text="Blueprint copy cost" if is_copy
                               else "Original blueprint purchase estimate", padding=6)
        frame.pack(fill=tk.X, pady=(0, 6))
        source = quote.get("source", "unset")
        if source == "write-in":
            desc = f"Persistent override: {isk(quote['price'])} ISK"
        elif source == "contract":
            desc = (f"Saved Jita asking price: {isk(quote['price'])} ISK; "
                    f"{quote.get('offer_count', 0)} eligible offers")
        else:
            desc = ("No saved copy with enough runs; profit is unpriced."
                    if source == "insufficient" else "No saved blueprint price; profit is unpriced."
                    if is_copy else "No saved original blueprint price.")
        ttk.Label(frame, text=desc, wraplength=500,
                  foreground=CLR_BAD if source in ("unset", "insufficient") else CLR_MUTED).pack(
                      anchor="w")
        if is_copy and source in ("write-in", "contract"):
            self._kv(frame, f"{quote['runs']} runs; blueprint cost per run:",
                     f"{isk(quote['per_run'])} ISK")
            if not quote.get("sufficient"):
                ttk.Label(frame, text="Override has fewer runs than this batch; reduce the batch.",
                          foreground=CLR_BAD).pack(anchor="w")
        if source == "contract":
            ttk.Label(frame, text=f"Contract {quote['contract_id']}; source checked "
                      f"{_date(quote.get('source_checked_at'))}", foreground=CLR_MUTED).pack(anchor="w")
            ttk.Label(frame, text=f"Offer research: ME {quote.get('me')} / TE {quote.get('te')}",
                      foreground=CLR_MUTED).pack(anchor="w")
            if is_copy:
                ttk.Label(frame, text=f"Chosen by {quote.get('comparison', 'ISK/run')}",
                          foreground=CLR_MUTED).pack(anchor="w")
                if quote.get("me") is None:
                    ttk.Label(frame, text=f"ME unknown; build uses assumed ME {result['me']}.",
                              foreground=CLR_MUTED).pack(anchor="w")
        if not is_copy:
            ttk.Label(frame, text="One-time purchase value, separate from manufacturing cost.",
                      foreground=CLR_MUTED).pack(anchor="w")
        override = self.bpc_pricing.get_writein(bid, is_copy=is_copy) or {}
        row = ttk.Frame(frame)
        row.pack(fill=tk.X, pady=4)
        fields = {}
        for key, label, default, width in (
                ("price", "Override ISK:", override.get("price", ""), 12),
                ("runs", "Runs:", override.get("runs") or quote.get("runs") or 1, 5),
                ("me", "ME:", override.get("me") if override.get("me") is not None else result["me"], 4),
                ("te", "TE:", override.get("te") if override.get("te") is not None else
                 quote.get("te") if quote.get("te") is not None else self._te_value(), 4)):
            if not is_copy and key != "price":
                continue
            ttk.Label(row, text=label).pack(side=tk.LEFT, padx=(0, 2))
            fields[key] = tk.StringVar(value=str(default))
            ttk.Entry(row, textvariable=fields[key], width=width).pack(side=tk.LEFT, padx=(0, 6))
        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="Save override", command=lambda: self._save_blueprint_override(
            bid, is_copy, fields)).pack(side=tk.LEFT)
        if override:
            ttk.Button(actions, text="Clear override", command=lambda:
                       self.bpc_pricing.set_writein(bid, 0, 0, is_copy=is_copy)).pack(side=tk.LEFT, padx=4)

    def _save_blueprint_override(self, bid, is_copy, fields):
        try:
            price = float(fields["price"].get().replace(",", "").strip() or 0)
            runs = int(fields["runs"].get()) if is_copy else None
            me = float(fields["me"].get()) if is_copy else None
            te = float(fields["te"].get()) if is_copy else None
            self.bpc_pricing.set_writein(bid, price, runs, me, te, is_copy)
            self.set_status("Industry: persistent blueprint override saved")
        except Exception as exc:
            messagebox.showerror("Blueprint override", str(exc), parent=self.frame)
