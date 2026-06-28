"""Characters sub-tab for the Industry tab (Phase 2.4).

Minimal roster cards: portrait + name, with [Skills] / [Standings] / [Remove]
buttons and an [Add character] login. Skills dialog shows the pulled industry
skill levels plus the write-in implant % fields (time-only, never cost — see
PLAN_industry_tab.md S1) and a re-pull button. Standings dialog is display-only
(kept for future; zero effect on T1 job cost).

All ESI work (login, skill/standing pulls, portrait download) runs in worker
threads and marshals back to the UI via `tk_queue.submit`. Backend lives in
`industry_characters` / `industry_skills` / `industry_standings`.
"""

import os
import threading
import tkinter as tk
from tkinter import ttk

import requests

from tk_queue import submit
from gui_window_utils import fit_window, make_scrollable
from sound_manager import get_data_dir
from industry_skills import INDUSTRY_SKILL_IDS

PORTRAIT_URL = "https://images.evetech.net/characters/{cid}/portrait?size=64"
CLR_MUTED = "#666666"
CLR_GOOD = "#1a7f37"

# Skills shown on the card's Skills dialog (the four that affect industry time).
SKILL_LABELS = [
    ("industry", "Industry"),
    ("advanced_industry", "Advanced Industry"),
    ("research", "Research"),
    ("metallurgy", "Metallurgy"),
]


def _portrait_dir():
    d = get_data_dir() / "industry_portraits"
    d.mkdir(parents=True, exist_ok=True)
    return d


class CharactersPanel:
    """Builds + manages the Characters sub-tab inside the Industry notebook."""

    def __init__(self, parent, roster, skills, standings, set_status, root=None,
                 bp_puller=None, bp_db=None, on_blueprints_pulled=None):
        self.parent = parent
        self.roster = roster
        self.skills = skills
        self.standings = standings
        self.set_status = set_status or (lambda m: None)
        self.root = root
        self.bp_puller = bp_puller          # Phase 3: BlueprintPuller (or None)
        self.bp_db = bp_db                  # Phase 3: IndustryBlueprintsDB (or None)
        self.on_blueprints_pulled = on_blueprints_pulled
        self._portraits = {}   # character_id -> PhotoImage (keep refs alive)
        self._pulling = set()  # character_ids with a blueprint pull in flight
        self._build()

    # ----------------------------------------------------------------- layout

    def _build(self):
        bar = ttk.Frame(self.parent, padding=(8, 6))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Industry character roster",
                  font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        self.count_label = ttk.Label(bar, text="", foreground=CLR_MUTED)
        self.count_label.pack(side=tk.LEFT, padx=10)
        self.add_btn = ttk.Button(bar, text="Add character (EVE login)",
                                  command=self._add_character)
        self.add_btn.pack(side=tk.RIGHT)
        ttk.Label(self.parent,
                  text="Skills/standings/implants here affect build & research "
                       "TIME only (Phase 4) — never material or job cost.",
                  foreground=CLR_MUTED, padding=(8, 0)).pack(fill=tk.X)

        host = ttk.Frame(self.parent, padding=8)
        host.pack(fill=tk.BOTH, expand=True)
        self.cards = make_scrollable(host)
        self.refresh_cards()

    def refresh_cards(self):
        for w in self.cards.winfo_children():
            w.destroy()
        chars = self.roster.characters
        self.count_label.configure(text=f"{len(chars)}/10 characters")
        self.add_btn.state(["disabled"] if self.roster.is_full else ["!disabled"])

        if not chars:
            ttk.Label(self.cards,
                      text="No industry characters yet. Click “Add character” to "
                           "log one in (separate from your trading login).",
                      foreground=CLR_MUTED).pack(anchor="w", pady=20)
            return

        for char in chars:
            self._build_card(char)

    def _build_card(self, char):
        card = ttk.LabelFrame(self.cards, padding=8)
        card.pack(fill=tk.X, pady=4)

        portrait = ttk.Label(card, text="…", width=8, anchor="center")
        portrait.pack(side=tk.LEFT, padx=(0, 10))
        self._load_portrait(char.character_id, portrait)

        info = ttk.Frame(card)
        info.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(info, text=char.character_name or f"({char.character_id})",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(info, text=f"ID {char.character_id}",
                  foreground=CLR_MUTED).pack(anchor="w")

        btns = ttk.Frame(card)
        btns.pack(side=tk.RIGHT)
        ttk.Button(btns, text="Skills",
                   command=lambda c=char: self._open_skills(c)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Standings",
                   command=lambda c=char: self._open_standings(c)).pack(side=tk.LEFT, padx=2)
        pulling = char.character_id in self._pulling
        bp = ttk.Button(btns, text="Pulling…" if pulling else "Pull blueprints",
                        state="disabled" if (pulling or not self.bp_puller) else "normal",
                        command=lambda c=char: self._pull_blueprints(c))
        bp.pack(side=tk.LEFT, padx=2)
        # Show last-pull summary next to the button (count + age).
        if self.bp_db is not None:
            meta = self.bp_db.get_pull_meta(char.character_id)
            if meta and meta.get("last_pulled"):
                ttk.Label(info, text=f"{meta.get('count', 0)} blueprints pulled",
                          foreground=CLR_MUTED).pack(anchor="w")
        ttk.Button(btns, text="Remove",
                   command=lambda c=char: self._remove(c)).pack(side=tk.LEFT, padx=2)

    # ----------------------------------------------------------------- portrait

    def _load_portrait(self, cid, label):
        if cid in self._portraits:
            label.configure(image=self._portraits[cid], text="")
            return

        def work():
            path = _portrait_dir() / f"{cid}.png"
            try:
                if not path.exists():
                    resp = requests.get(PORTRAIT_URL.format(cid=cid), timeout=20)
                    resp.raise_for_status()
                    path.write_bytes(resp.content)
                from PIL import Image
                img = Image.open(str(path))
                img.load()
                submit(lambda: self._set_portrait(label, img, cid))
            except Exception as e:
                print(f"[IndustryChars] portrait load failed for {cid}: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _set_portrait(self, label, img, cid):
        try:
            from PIL import ImageTk
            photo = ImageTk.PhotoImage(img)
            self._portraits[cid] = photo  # keep a ref or Tk drops the image
            label.configure(image=photo, text="")
        except Exception as e:
            print(f"[IndustryChars] portrait render failed for {cid}: {e}")

    # ----------------------------------------------------------------- add/remove

    def _add_character(self):
        if self.roster.is_full:
            self.set_status("Industry: roster full (10 characters max)")
            return
        self.set_status("Industry: opening EVE login in your browser…")
        self.roster.start_auth_flow(
            callback=lambda ok, msg: submit(lambda: self._on_auth_done(ok, msg)))

    def _on_auth_done(self, ok, msg):
        self.set_status(f"Industry: {msg}")
        if ok:
            self.refresh_cards()

    def _remove(self, char):
        self.roster.remove(char.character_id)
        self.skills.clear(char.character_id)
        self.standings.clear(char.character_id)
        if self.bp_db is not None:
            self.bp_db.clear_character(char.character_id)
        if self.on_blueprints_pulled:
            self.on_blueprints_pulled()
        self.refresh_cards()

    # ----------------------------------------------------------------- blueprints

    def _pull_blueprints(self, char):
        """Pull this character's owned blueprints via ESI (worker thread)."""
        if not self.bp_puller or char.character_id in self._pulling:
            return
        self._pulling.add(char.character_id)
        self.set_status(f"Industry: pulling blueprints for {char.character_name}…")
        self.refresh_cards()  # reflect the disabled "Pulling…" state

        cid = char.character_id

        def work():
            try:
                ok, msg = self.bp_puller.pull(cid)
            except Exception as e:
                ok, msg = False, str(e)
            submit(lambda: self._on_pull_done(cid, ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def _on_pull_done(self, cid, ok, msg):
        self._pulling.discard(cid)
        self.set_status(f"Industry: blueprints — {msg}")
        if ok and self.on_blueprints_pulled:
            self.on_blueprints_pulled()
        self.refresh_cards()

    def _refit(self, widget, min_width):
        """Re-run fit_window on a dialog after async content lands, so it sizes
        to the real content instead of the loading placeholder. Safe if the
        dialog was already closed."""
        try:
            fit_window(widget.winfo_toplevel(), min_width=min_width)
        except tk.TclError:
            pass

    # ----------------------------------------------------------------- skills dlg

    def _open_skills(self, char):
        dlg = tk.Toplevel(self.parent)
        dlg.title(f"Skills — {char.character_name}")
        dlg.transient(self.parent.winfo_toplevel())

        wrap = ttk.Frame(dlg, padding=10)
        wrap.pack(fill=tk.BOTH, expand=True)

        ttk.Label(wrap, text=char.character_name or str(char.character_id),
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")

        skills_box = ttk.LabelFrame(wrap, text="Industry skills (affect time)",
                                    padding=8)
        skills_box.pack(fill=tk.X, pady=(8, 6))
        self._render_skills(skills_box, char)

        # Write-in implants — advanced, default 0, time-only.
        imp = ttk.LabelFrame(wrap, text="Implant bonuses % (write-in, time-only)",
                             padding=8)
        imp.pack(fill=tk.X, pady=(0, 6))
        mfg_v = tk.StringVar(value=f"{char.implant_mfg_pct:g}")
        me_v = tk.StringVar(value=f"{char.implant_me_pct:g}")
        te_v = tk.StringVar(value=f"{char.implant_te_pct:g}")
        for lbl, var in (("Manufacturing time %:", mfg_v),
                         ("ME research time %:", me_v),
                         ("TE research time %:", te_v)):
            row = ttk.Frame(imp)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=lbl, width=22).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=var, width=8).pack(side=tk.LEFT)
        ttk.Label(imp, text="Advanced: implant bonuses are per-slot with no single "
                            "headline %. Leave 0 unless you know the value.",
                  foreground=CLR_MUTED, wraplength=320).pack(anchor="w", pady=(4, 0))

        def _save():
            self.roster.set_implants(
                char.character_id,
                mfg_pct=_f(mfg_v), me_pct=_f(me_v), te_pct=_f(te_v))
            self.set_status(f"Industry: saved implants for {char.character_name}")
            dlg.destroy()

        actions = ttk.Frame(wrap)
        actions.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(actions, text="Re-pull skills",
                   command=lambda: self._render_skills(skills_box, char, force=True)
                   ).pack(side=tk.LEFT)
        ttk.Button(actions, text="Save", command=_save).pack(side=tk.RIGHT)
        ttk.Button(actions, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT, padx=4)

        fit_window(dlg, min_width=380)

    def _render_skills(self, container, char, force=False):
        for w in container.winfo_children():
            w.destroy()
        loading = ttk.Label(container, text="Loading skills…", foreground=CLR_MUTED)
        loading.pack(anchor="w")

        def work():
            self.skills.fetch(char.character_id, force_refresh=force)
            levels = self.skills.get_levels(char.character_id)
            submit(lambda: self._fill_skills(container, levels))

        threading.Thread(target=work, daemon=True).start()

    def _fill_skills(self, container, levels):
        for w in container.winfo_children():
            w.destroy()
        any_trained = any(levels.get(k, 0) for k, _ in SKILL_LABELS)
        if not any_trained:
            ttk.Label(container,
                      text="All zero — not pulled yet, not authenticated, or "
                           "untrained. Try Re-pull.",
                      foreground=CLR_MUTED).pack(anchor="w")
        for key, label in SKILL_LABELS:
            lvl = levels.get(key, 0)
            row = ttk.Frame(container)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=label, width=20).pack(side=tk.LEFT)
            ttk.Label(row, text=f"Level {lvl}",
                      foreground=CLR_GOOD if lvl else CLR_MUTED).pack(side=tk.LEFT)
        # Re-fit: the dialog opened sized to the "Loading…" placeholder, so
        # resize to the real content now (cross-platform: Linux fonts clip
        # otherwise — see project-linux-window-fix).
        self._refit(container, 380)

    # ----------------------------------------------------------------- standings dlg

    def _open_standings(self, char):
        dlg = tk.Toplevel(self.parent)
        dlg.title(f"Standings — {char.character_name}")
        dlg.transient(self.parent.winfo_toplevel())
        wrap = ttk.Frame(dlg, padding=10)
        wrap.pack(fill=tk.BOTH, expand=True)
        ttk.Label(wrap, text=char.character_name or str(char.character_id),
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(wrap, text="Display-only — standings have no effect on industry "
                             "job cost (kept for future).",
                  foreground=CLR_MUTED, wraplength=360).pack(anchor="w", pady=(0, 6))

        box = ttk.LabelFrame(wrap, text="Standings", padding=8)
        box.pack(fill=tk.BOTH, expand=True)
        status = ttk.Label(box, text="Loading standings…", foreground=CLR_MUTED)
        status.pack(anchor="w")

        cols = ("kind", "name", "standing")
        tree = ttk.Treeview(box, columns=cols, show="headings", height=12)
        tree._sort_col = "standing"
        tree._sort_reverse = True
        tree._rows = []
        for c, t, w in (("kind", "Type", 80), ("name", "Name", 180),
                        ("standing", "Standing", 80)):
            tree.heading(c, text=t,
                         command=lambda cc=c: self._sort_standings(tree, cc))
            tree.column(c, width=w, anchor="w" if c != "standing" else "e")

        def work():
            data = self.standings.get(char.character_id)
            # Only factions + corps are relevant; agents are dropped.
            ids = [i for kind in ("factions", "npc_corps")
                   for i in data.get(kind, {})]
            self.standings.resolve_names(ids)
            submit(lambda: self._fill_standings(status, tree, box, data))

        threading.Thread(target=work, daemon=True).start()

        ttk.Button(wrap, text="Close", command=dlg.destroy).pack(side=tk.RIGHT, pady=(6, 0))
        fit_window(dlg, min_width=340)

    def _fill_standings(self, status, tree, box, data):
        # Factions + corps only (agents dropped). Keep standing as a float so
        # the Standing column sorts numerically, not lexically.
        rows = []
        for kind, label in (("factions", "faction"), ("npc_corps", "corp")):
            for fid, val in data.get(kind, {}).items():
                name = self.standings.name_for(fid) or str(fid)
                rows.append((label, name, float(val)))
        if not rows:
            status.configure(text="No standings pulled (not authenticated, or none).")
            self._refit(box, 340)
            return
        status.configure(text=f"{len(rows)} standings pulled.")
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(4, 0))
        sb.pack(side=tk.LEFT, fill=tk.Y, pady=(4, 0))
        tree._rows = rows
        self._render_standings(tree)
        # Re-fit now the tree + scrollbar are present (see _fill_skills note).
        self._refit(box, 340)

    def _render_standings(self, tree):
        """(Re)draw the standings tree honouring the current sort column/dir."""
        tree.delete(*tree.get_children())
        col = tree._sort_col
        rev = tree._sort_reverse
        idx = {"kind": 0, "name": 1, "standing": 2}[col]
        if col == "standing":
            key = lambda r: r[2]
        else:
            key = lambda r: r[idx].lower()
        for kind, name, val in sorted(tree._rows, key=key, reverse=rev):
            tree.insert("", "end", values=(kind, name, f"{val:+.2f}"))
        for c, base in (("kind", "Type"), ("name", "Name"), ("standing", "Standing")):
            arrow = (" ▼" if rev else " ▲") if c == col else ""
            tree.heading(c, text=base + arrow)

    def _sort_standings(self, tree, col):
        if tree._sort_col == col:
            tree._sort_reverse = not tree._sort_reverse
        else:
            tree._sort_col = col
            tree._sort_reverse = (col == "standing")  # numbers high→low by default
        self._render_standings(tree)


def _f(var, default=0.0):
    try:
        return float(var.get().strip())
    except (ValueError, AttributeError):
        return default
