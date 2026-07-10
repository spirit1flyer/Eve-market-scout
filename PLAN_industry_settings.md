# Industry tab: Settings window + visible/flexible skill & fee sources

> **STATUS 2026-07-10: ALL 4 STAGES CODE-COMPLETE** on `industry-phase5`.
> Tests: engine 54/54, sde invention 69/69, provider t2 69/69, NEW
> `tests/test_industry_fees.py` 22/22. 29-check headless Tk smoke green on
> the live caches; compute-from-cache identical to the pre-change baseline
> (4944 results, all=2089/t2t3=845/extra=173, 1.2s). Remaining: Caleb's live
> click-through (§ Verification item 4) + the roster-char fee hand-audit
> against a real character (item 3 — the math is unit-reconciled, the live
> ESI pull path isn't).
> (Plan recovered from the 2026-07-10 session transcript after a /clear —
> now saved as this file.)

## Context

Live click-through (2026-07-10) surfaced two intertwined problems:

1. **Invisible "who"**: three different characters silently drive the numbers —
   fees come from the trading *seller* char at the Sell-at hub
   (`gui_industry._fees()` → `calculate.load_cached_skills(hub, "seller")`),
   invention probability in the ranked list uses a flat assumed level 4
   (`InventionPricing._skill_level`, no character at all), and only a per-item
   "Built by" pick swaps in real skills. Caleb wants both sources visible and
   user-selectable, with fill-in overrides.
2. **Cramped UI**: on Caleb's 1366×768 screen the list + detail panes feel small
   while ~half the screen is set-once controls (top bar, Facility row, Reaction
   facility row, overflowing filter row, tech row). Caleb: "we are settling on a
   new openable window for settings."

Decisions locked by Caleb (AskUserQuestion, 2026-07-10):
- Invention skills: a **fill-in level that overrides character selection**, plus
  a **selectable character**; owned blueprints should default to the character
  holding them.
- Fees: **character selector from the industry-authed roster** (not
  seller/buyer slots), with **write-in override as the default behavior**
  (blank write-ins = auto from the selected source).
- Placement: **a new openable Settings window**, working toward "much of our
  settings shown" being editable there.
- GUI wrap-in-place is superseded by this direction. Main tab keeps only the
  interactive filters; list + detail get the freed space.

## Constraints / facts to honor

- Screen 1366×768; nothing new on the already-clipping inline rows.
- `industry_settings.json` is the existing persistence file (keys:
  JobCostConstants overrides, `bpc_prices`, `reaction_facility`,
  `assumed_invention_skill`). New keys join it.
- Fees need **base** standings (broker fee ignores Connections/Diplomacy —
  verified 2026-07-03) + Broker Relations / Accounting / Adv Broker Relations
  levels. For roster chars these exist already:
  `industry_skills.IndustrySkills.get_skill_level/peek_skill_level` (full-sheet
  cache) and `industry_standings.IndustryStandings` (pulls base standings;
  applies the social modifier only for display). Sell-hub station owner
  corp/faction comes from `gui_station_lookup.StationLookup.singleton()` (hubs
  resolve from built-in TRADE_HUBS without network).
- Reuse `gui/gui_window_utils.fit_window` + `make_scrollable` for the window
  (project-linux-window-fix pattern).
- All of today's earlier work (logging, positive-only checkbox, warm_cache,
  paint-from-cache) is uncommitted on `industry-phase5` — commit it first as
  its own chunk before starting this (ask Caleb).

## Plan

### Stage 1 — Settings window shell + facility rows move into it
File: new `gui/industry/gui_industry_settings.py` + edits in
`gui/industry/gui_industry.py`.

- `IndustrySettingsWindow` — **non-modal** `tk.Toplevel` (singleton per tab;
  re-open focuses existing), `fit_window`, `make_scrollable` body, sections as
  `ttk.LabelFrame`s. Every control writes through to the manager immediately
  (Apply-on-change like the current inline fields) and persists.
- Move INTO it (delete the two inline rows):
  - **Manufacturing facility** (system / facility tax / cost bonus / material
    bonus / time bonus / SCC). NEW: persist to `industry_settings.json` key
    `"facility"` (currently the mfg row resets every launch; reaction row
    already persists — mirror `_load_rx_settings`/`_save_rx_settings`).
  - **Reaction facility** (existing persisted values).
  - **Defaults**: ME / TE / Batch write-ins (move off the filter row).
  - **View extras** (moves off the clipped filter row): Show unpriced,
    Sub-cap only, Hide Upwell, Hide POS, Blueprint filter combo, Ignored…
    button. These re-filter live (non-modal window makes that usable).
- Main tab keeps: top bar (Refresh / Update SDE / Buy / Sell / Input / status),
  ONE slim filter row (category chips, Search, Min profit, Positive-only
  checkbox), tech chip row (chips + tech note), and a **"Settings…" button** on
  the top bar. Changed-settings recompute exactly as today (`_compute(False)`
  or `_rebuild_list` per control).
- **List/detail get the space**: replace the fixed `left`/`right` pack with a
  `ttk.PanedWindow(orient=horizontal)` so Caleb can drag the list/detail split;
  tree `height` grows since two rows are gone.

### Stage 2 — Fees: visible + flexible
Files: `gui/industry/gui_industry.py`, `gui/industry/gui_industry_settings.py`,
small helper (new) `industry/industry_fees.py`.

- `industry_fees.resolve_fees(sell_hub_cfg, choice, skills, standings) ->
  (SellFees, source_label)`:
  - `choice` = persisted `industry_settings.json` key `"fees"`:
    `{"char_id": int|None, "broker_override": float|None,
    "tax_override": float|None}`.
  - Write-ins win when set. Else if `char_id` set: Broker Relations (3446),
    Accounting (16622) from `IndustrySkills` full-sheet cache (fetch-if-cold in
    worker; peek on UI thread) + base standings vs the sell-hub owner
    corp/faction via `IndustryStandings` + `StationLookup`; feed
    `calculate.TradingSkills` → `get_broker_fee_rate`/`get_sales_tax_rate`.
    Else (default): current behavior — trading seller cache
    (`load_cached_skills(sell_hub)`), labeled with the char name from
    `calculate.get_cached_skills_summary()`.
  - Verify the two skill type_ids against `esi_skills` constants at
    implementation time.
- Settings window "Fees" section: character dropdown ("(trading seller)" +
  roster names) + Broker %/Tax % write-ins (blank = auto) + live preview line
  of the effective rates at the current Sell-at hub.
- **Visibility on the main tab**: extend `_update_legend()` to a two-line
  assumptions label: existing profit line + new
  `Fees: <source> <broker>%+<tax>% @ <hub> · Inv. skills: <source>`.
  Update on hub change / settings change / compute done.

### Stage 3 — Invention skills: global source + fill-in override
Files: `gui/industry/gui_industry.py`, `industry/industry_market_data.py`
(minor), `gui/industry/gui_industry_owned.py`.

- Persisted `industry_settings.json` key `"invention_skills"`:
  `{"char_id": int|None, "level_override": int|None}` (the existing
  `assumed_invention_skill` stays as the last-resort fallback).
- Effective per-skill resolution order (document in one place,
  `_invention_skill_fn(tid)` on the manager):
  1. fill-in `level_override` (flat, overrides everything — Caleb's spec),
  2. per-item "Built by" char (existing `_built_by`),
  3. global selected char,
  4. assumed level (existing behavior).
  Implemented as the `skill_level_fn` passed to `InventionPricing.resolve` —
  worker pass (`_calc_t2` call site in `_work`) now passes it too, not just the
  inline recompute; pre-warm the selected char's sheet once in the worker via
  `IndustrySkills.get_skill_level` (1 ESI call, 1h cache) so peeks hit.
- Move the "Inv. skills" write-in off the tech row into the Settings window
  "Invention" section: char dropdown + level fill-in + assumed-level field.
- **Owned panel default-to-owner**: when costing/building an owned row, if no
  per-item Built-by is set, use the row's owning `character_id` (each blueprint
  row already carries it) for build time AND invention skills. Wire via the
  existing `build_time_for`/calc-context callbacks — add an optional
  `default_char_id` parameter.
- **Detail visibility**: replace the buried footnote in
  `_build_invention_section` (gui_industry.py ~line 1489) with an explicit
  `Skills: <value>` line naming the effective source:
  `"fill-in level 5"` / `"<name> (Built by)"` / `"<name> (global)"` /
  `"assumed level 4"`.

### Stage 4 — Docs + logging
- `[IndustryDiag]` lines: log the resolved fee source + invention-skill source
  at compute start (one line each).
- CLAUDE.md map: gui_industry bullet (new settings window file, rows moved,
  paned split), sde section untouched. Memory `project_industry_tab.md`:
  record the settings-window direction as THE declutter resolution
  (supersedes wrap-in-place) + the skill/fee source hierarchy.

## Verification

1. Suites: `python tests/test_industry_engine.py`,
   `tests/test_industry_provider_t2.py`, `tests/test_sde_industry_invention.py`
   (all currently green; provider tests may gain fee-resolution cases if
   `industry_fees.py` warrants them — resolution order table test).
2. Headless Tk smokes on `DISPLAY=:0` (pattern from today's
   `cache_paint_smoke.py`): tab constructs with 2 inline rows gone + Settings
   button; window opens non-modal, every moved control present, facility values
   persist across a reconstruct; fee resolution: write-in beats char beats
   seller-default (assert `_fees()`/legend text); invention skill_fn order
   (fill-in > built-by > global > assumed) asserted by stubbing
   `peek_skill_level`; full compute-from-cache still ~1s with identical result
   counts; Endurance's invention section shows the explicit `Skills:` source
   line.
3. Hand-audit one fee case from a roster character against
   `get_broker_fee_rate` math (like today's Metallofullerene audit).
4. Caleb live click-through: open Settings, change facility/fees/invention
   char, watch list re-rank; drag the paned split; confirm the cramped feel is
   fixed at 1366×768.

## Out of scope (parked by Caleb)
- Industry audit tool section ("we haven't worked out what all to audit yet").
- The ±5% numeric verify vs Adam4EVE (separate batched-verify item).
- Version bump / merge (not until rollout).
