# Industry Tab — Master Project Plan (Rev 7)

Status: **PHASE 3 (3.1–3.4) IMPLEMENTED (2026-06-27).** Stage 3.4 added BPC
pricing: engine `calc_full(blueprint_cost_per_run=...)` folds amortized BPC cost
into build cost (engine test 18/18); `industry_market_data.BpcPricing` resolves
write-in (price+runs, persisted to `industry_settings.json` key `bpc_prices`) >
cheapest cached contract offer (`find_bpc_offers` across hub regions) > unset;
Owned panel costs each BPC at its amortized cost/run, detail panel has a write-in
entry + source label, list flags unset BPC profit with `*`. Also (from the live
testing session): history now uses calendar-window 7d/30d averages with a
3-month baseline dropping sub-50% manipulation days; engine gained a **spot**
margin (current sell px) and the Owned list falls back 7d→30d→spot; chain
material rows are interactive (graph/copy) and zero-priced inputs flag red.
**IN PROGRESS 2026-06-28:** input-price fallback DONE (no live order → buy-region
market-history avg; `IndustryProvider.buy_region`, material history now fetched).
Stage 3.5 R8 code written (Register-structures button + `fetch_structure_meta` +
`get_structure_locations` + hub-selector refresh) — ONE open step before done:
filter BP `location_id`s to hangar `location_flag`s (a BP loc≥1T can be a
container/ship that 404s; real data = 829 Hangar + 1 Cargo across 20 structures).
Then docs + live-verify the Register button → Phase 3 complete. See memory
`project-industry-tab` for the precise resume point.

Status: **PHASE 3 (3.1–3.3) IMPLEMENTED (2026-06-27).** Owned BPO/BPC master
list: `industry_blueprints.py` (`IndustryBlueprintsDB` SQLite store keyed by
character_id + `BlueprintPuller` for `GET /characters/{id}/blueprints/`, X-Pages
paginated, per-page slow-pace pause), `sde_industry.get_product_for_blueprint`
(reverse lookup), `gui_industry_owned.py` (`OwnedBlueprintsPanel` sub-tab —
owned blueprints at their REAL ME/TE/runs, costed via
`IndustryTabManager.build_calc_context`, reuses the Phase 1 build breakdown), and
the Characters card's "Pull blueprints" button wired (worker thread, refreshes
the Owned panel). Backend unit-exercised + Owned panel functional-tested headless
with synthetic data; full tab constructs (3 sub-tabs). NOT yet verified in the
live Tk app, and the live ESI pull is untested (no display/auth on dev box).
**NEXT: Stage 3.4 (BPC pricing: write-in > `find_bpc_offers` > unset; amortize
price÷runs; runs-based batch cap), then 3.5 (R8: `location_id ≥ 1e12` →
register as industry hub).**

Status: **PHASE 2 IMPLEMENTED (2026-06-27).** Stages 2.1–2.5 landed:
`industry_characters.py` (`IndustryRoster`, ≤10-char ESI auth, write-in
implants), `industry_skills.py` (per-char skill pull), `industry_standings.py`
(per-char standings, display-only), `gui_industry_characters.py`
(`CharactersPanel` cards), and `gui_industry.py` now wraps an industry-level
sub-notebook (Top Profit — T1 + Characters). Headless-constructed OK (both
sub-tabs render, roster/cards build). **USER-VERIFIED LIVE 2026-06-27** — login,
Skills + Standings dialogs work; post-test fixes landed: dialogs re-fit after
async content (Linux clip fix), Standings shows factions+corps only (agents
dropped), ids→names via `/universe/names/`, click-to-sort columns. 2.5 ("Built
by" dropdown in the detail panel, persisted to `industry_built_by.json`, INERT
until P4) DONE. **NEXT: Phase 3 (Owned BPO/BPC master list), start at stage 3.1.**
Phases 3–7 TODO.

Status: **PHASE 1 IMPLEMENTED (2026-06-22).** Stages 0.3 + 1.1–1.6 landed:
`industry_engine.py` (+ `test_industry_engine.py`, 17/17 pass),
`industry_market_data.py`, `gui_industry.py`, `sde_industry.get_recipe`,
registered in `gui_main.py`. Headless-verified (prices/indices/history/category
mapping populate; full ~4,800-item pass ~7s refresh + 0.1s recompute). NOT yet
verified in the live Tk app (no display on dev box); APP_VERSION not yet bumped.
SCC surcharge / facility tax remain ⚠ CCP-tuned — defaults SCC 4% / tax 0.25%,
write-in-overridable on the facility row, MUST be confirmed in-game (Stage 0.1).
Phases 2–7 (characters/blueprints/research/T2/T3/reactions) still TODO. Public
feature (normal committed files, no `drug_` prefix). Research R1–R8 resolved
2026-06-21; EVE mechanics verified against EVE University / EVE Ref 2026-06-21.

Rev 7 (post-review fixes): job-cost constants flagged for live-verify + made
overridable (NPC facility tax 0.25% confirmed; **SCC surcharge is CCP-tuned —
do not hardcode blind**, it was raised again and changed in the July 2025
update); formula adds AlphaCloneTax (0 for Omega); engine designed with
recursion as an extension point from stage 1.1; BPO market-pull stays dropped
(Caleb's explicit call last session — manual write-in remains) and the orphaned
R5 sanity check removed with it; "Built by" dropdown explicitly inert until P4;
implants default 0 / advanced.

Rev 6 corrects Rev 5's over-simplification:
- **Skills ARE pulled via ESI per character** (`industry_skills.py` kept).
- **Standings ARE pulled via ESI per character** (`industry_standings.py`
  kept) — not needed for T1 math, kept anyway for future ("rather have it").
- **Only IMPLANTS are write-in** (a % per character) — avoids adding the
  `esi-clones.read_implants.v1` scope; nothing else gives implants cheaply.
- Per-character ESI roster (`industry_characters.py`) restored, ≤10 characters,
  separate from seller/buyer trading auth.
- Still dropped: BPO price pulling (amortizes to ~0; optional write-in).
- Implants fold into the roster character record; no separate profiles file.

Carried from earlier revs (unchanged): per-item "Built by" dropdown; owned
BPO/BPC master list; universal sort + mini search; write-in structure-rig
bonuses on the facility selector; T1 category chips.

---

## 0. Glossary — "patient" is overloaded

- **Patient input** — buy materials via buy orders (highest buy), wait. Cheaper.
  Controls **material cost**.
- **Impatient input** — buy materials now from lowest sell orders.
- **Patient-sell output** — **7-day average** transaction price; "list and wait"
  proxy. **Default sort/profit basis.** Controls **output revenue**.
- **Immediate-sell output** — highest buy order for the finished item (dump now).
Input and output axes are orthogonal; the top-bar toggle controls **input**.

---

## 1. Verified EVE mechanics (drive the whole design)

- **Material quantity** depends only on blueprint **ME** + structure/rig bonuses.
  **No character skill reduces materials** (Production Efficiency removed).
- **Job installation cost** = `EIV × (SCI × bonuses + FacilityTax + SCC +
  AlphaCloneTax)`, **EIV = Σ(base_qty × CCP adjusted_price)** (base/ME0
  quantities, adjusted prices). **No skill, no standing affects job cost.**
  Constants (⚠ CCP-tuned — verify live before hardcoding; expose as overridable
  settings): NPC **FacilityTax 0.25%** (verified current; player structures =
  owner-set); **SCC surcharge** a global flat value CCP has been raising (~4%
  mfg / 2% research as of mid-2025 — confirm at build time, it changes);
  **AlphaCloneTax 0.25%** for Alpha accounts, **0 for Omega**.
- **Skills affect TIME only:** Industry 4%/lvl, Advanced Industry 3%/lvl
  (mfg + research time); **Metallurgy** 5%/lvl (ME research time), **Research**
  5%/lvl (TE research time). **Implants** MY-70x (ME time) / RR-70x (TE time),
  slots 6–8, time only. **Nothing reduces research ISK cost.** Lab/Mass
  Production skills add parallel slots (throughput), not per-job numbers.
- **Standings**: broker fees + reprocessing only, **not** industry job cost.
- ⇒ **material cost + job cost + margins need ZERO character data.** Character
  data enters only via build **time** (→ batch cap), research **time** (→ popup),
  and reading owned **ME/TE/runs**.
- **BPO cost** amortizes to ~0 (bought once, runs ~forever) → not pulled;
  optional write-in.

---

## 2. Auth model — per-character ESI roster; implants write-in

- **One app to CCP** (one client_id) — multiple logins never look like two apps.
- **Industry roster** (`industry_characters.py`): up to 10 characters, separate
  from the seller/buyer trading auth (which stays untouched). Reuses esi_auth
  PKCE helpers + `CharacterAuth` by import; own JSON + own scope set:
  - `esi-skills.read_skills.v1` (skills → build/research time)
  - `esi-characters.read_standings.v1` (kept for future; no T1 effect)
  - `esi-characters.read_blueprints.v1` (owned ME/TE/runs/location/ownership)
  - `esi-universe.read_structures.v1` (structure names/locations, R8)
  - **NOT** `read_clones` — implants are write-in instead.
- **Implants: write-in %** per character (mfg-time %, ME-research-time %,
  TE-research-time %), stored on the roster record. Generic — no hardcoded
  implant list. **Default 0; treat as an advanced field** — implant bonuses are
  per-slot/multiplicative with no single headline %, so don't expect accurate
  entry (and they only affect time, never cost).
- Overlap (industry char also a trader): two supported grants of one app; normal.

---

## 3. Architecture

**New files (committed, public):**
- `gui_industry.py` — `IndustryTabManager`: tab shell + all sub-tabs + Characters
  tab (cards) + universal sort/search helpers.
- `industry_engine.py` — pure calc: ME-adjust, EIV, job cost, build time, batch
  cap, margins. No GUI/network.
- `industry_market_data.py` — own `/industry/systems/` + `/markets/prices/`
  fetch over shared `api.ESIClient` (manufacturing, research ME/TE, copying,
  invention, reverse_engineering, reaction). 6h TTL. Also holds the CCP-tuned
  job-cost constants (SCC surcharge, NPC facility tax, alpha tax) as named
  values with a "last verified <date> / <source>" comment and overridable
  settings — a stale constant silently corrupts every job cost, so keep it
  visible and user-correctable.
- `industry_characters.py` — `IndustryRoster`: ≤10 char auth + roster
  persistence (`industry_characters.json`), incl. write-in implant % per char.
- `industry_skills.py` — per-character ESI skill cache (keyed by character_id;
  lifts parse math from `esi_skills.py`). Reads Industry 3380, Advanced Industry
  3388, Research 3403, Metallurgy 3409.
- `industry_standings.py` — per-character ESI standings (kept for future; lifts
  Connections/Diplomacy modifier math from `esi_skills.py:ESIStandings`).
- `industry_blueprints.py` — `IndustryBlueprintsDB.singleton()` SQLite owned
  BPO/BPC store keyed by character_id (mirrors `contracts_db.py`).

**Reused unchanged:** `order_cache.peek_cached_orders`; `MarketHistoryDB`;
`structure_history`; `sde_industry.db`; `sde_manager`;
`contracts_db.find_bpc_offers`; `graphing.py`; `gui_station_lookup`,
`gui_jump_cache`; `gui_window_utils.fit_window`; esi_auth PKCE helpers (import).

**Untouched:** `esi_auth.py`, `esi_skills.py` (seller/buyer trading). No SDE
schema change (basePrice dropped with BPO pricing).

---

## 4. Sub-tabs + universal UI

```
[ Industry ]
  ├── Top Profit — T1   ← Phase 1   chips: All/Ships/Modules/Ammo/Components
  ├── Top Profit — T2   ← Phase 5 (TBA)
  ├── Top Profit — T3   ← Phase 6 (TBA)
  ├── Extra             ← Phase 7 (TBA)
  ├── Owned BPO/BPC     ← Phase 3   master list, each row → build breakdown
  └── Characters        ← Phase 2   roster cards
```

**Universal UI (every list):** click-to-sort, highest-value-to-top default;
mini search bar for quick item lookup.

**Characters card (minimal):** portrait + name; **[Skills]** opens a `fit_window`
dialog showing the **pulled** skills + write-in implant % fields + a **re-pull**
button; **[Standings]** opens a dialog with pulled standings (display only);
**[Blueprints: Login]** / **[Pull/Update]** run the roster auth + blueprint pull.
Portrait via `images.evetech.net/characters/{id}/portrait?size=64` (Pillow
12.1.1, cache PNG to AppData, keep a ref).

**Per-item "Built by" dropdown:** selects a roster character; applies that
character's pulled skills + write-in implants to build **time** / batch cap only
(skills/implants don't change cost). BPO owner ≠ builder; independent.

---

## 5. Phases

### Pre-Phase
- R5 one-call live sanity check (a blueprint type_id returns orders).

### Phase 1 — T1 Manufacturing Core (no character data)
Top bar: `[Buy at ▼] [Sell at ▼] [Facility ▼] [Input: Patient/Impatient ▼] [Refresh]`.
Facility selector supplies SCI + facility_tax + structure bonuses, incl.
**write-in structure-bonus fields (material %, time %, cost %)** for rigs.
Default generic NPC (0.25% tax, no bonus).
Top Profit T1: category chips, mini search, min-profit filter, global batch
default (integer ≥1; time cap → Phase 4). List `Item | Build cost | Profit |
Margin % | Vol/day`, sort Profit desc. Profit = patient-sell (7d) − build cost −
sales tax; toggle changes input side only. Output cases: ≥30d → 7d+30d; ≥7d<30d
→ 7d, 30d "—"; <7d → "—", excluded from ranked sort unless "show unpriced" on.
Detail panel: ME (default 10, write-in) × Batch; material rows (ME-adj qty ×
input px); Materials / Job cost / Total build; patient / immediate / 30d margins.
ME: `max(1, ceil(base × (100−ME)/100))` per material per run; EIV uses base qty ×
adjusted price. Double-click → `graphing.py`; right-click → copy name / type_id /
open info.

### Phase 2 — Characters tab (ESI roster + write-in implants)
`industry_characters.py` (≤10 auth roster), `industry_skills.py`,
`industry_standings.py`. Characters sub-tab with minimal cards (portrait, name,
Skills dialog w/ pulled skills + write-in implant %, Standings dialog,
blueprint login/pull). Slow-pace safety on all ESI pulls. Own scope set (§2).

### Phase 3 — Owned BPO/BPC master list
Deps: Phase 2 auth. `GET /characters/{id}/blueprints/` → `industry_blueprints.db`
(ME/TE/runs/location/ownership). One-time pull, cached, manual re-pull,
slow-pace safety. Master list (universal sort+search); each row → full build
breakdown. BPC pricing: **write-in on every BPC**; write-in > cached contract
offer (`find_bpc_offers`, flag if empty) > Not set (`*`, overstated). Amortized
= price ÷ runs; hard batch cap = runs left. R8 harvest: `location_id ≥ 1e12` →
"register as industry hub" via existing `resolve_region_for_system` +
`add_custom_station`.

### Phase 4 — Research popup + time-based batch cap
Deps: Phase 2 (pulled skills + write-in implants), Phase 3. Research popup
(`fit_window`): source→target ME (default cur→10), optional TE toggle (off),
facility selector, per-level **time** from pulled Metallurgy/Research/Advanced
Industry + write-in implant %, per-level **cost** from facility/system indices
(no character input), totals. On owned + planned items. Time-based batch cap:
total job time ≤ 30 days (single run > 30 days → max 1); selectable max. Build
time = base time × TE × Industry/Advanced Industry (selected "Built by" char) ×
write-in implant %.

### Phase 5 — T2 manufacturing
Deps: Phase 1, Phase 3, **SDE expand for activityID 8 (invention)**. T2 recipes
present (activity 1); components recurse to moon materials; invention via
datacores/decryptors. T2 sub-tab live.

### Phase 6 — T3 manufacturing
Deps: Phase 1/3, **SDE expand for activityID 7 (reverse engineering)**.

### Phase 7 — Reactions
Deps: Phase 5, **SDE expand for activityID 11 (reactions)**. Extra sub-tab live.

---

## 6. Deferred / future

- **Ignore option on list items (session + always).** Right-click an item in the
  Top Profit lists to ignore it. Two modes: **this session only** (in-memory set,
  cleared on restart) and **always** (persisted, mirrors the `ignored_items.json`
  pattern used elsewhere — own file e.g. `industry_ignored.json`). Ignored items
  drop out of all Top Profit lists; a way to view/un-ignore the permanent set.
  Applies across T1/T2/T3 lists.

- **Standings effects** — pulled and displayable now, but zero effect on T1 job
  cost. Wire into fee math only if a future activity needs it. "Current docked
  station" framing would need `esi-location.read_location.v1` — skipped; show
  general standings instead.
- **Full station-rig / structure model** — T1 uses write-in bonus fields; real
  rig/structure/security modeling is a later refinement.
- **Implant pull** — currently write-in; could become an ESI pull later if
  `read_clones` is ever added.

---

## 7. Dependency table

| Phase | Deliverable | Hard deps |
|------|-------------|-----------|
| Pre  | R5 check | — |
| 1    | T1 core + facility selector (write-in rig bonuses) | own market/industry fetch; NO char data |
| 2    | Characters tab: ESI roster (skills+standings) + write-in implants | esi_auth PKCE helpers (import) |
| 3    | Owned BPO/BPC master list + blueprint DB + R8 harvest | Phase 2 |
| 4    | Research popup + time-based batch cap | Phase 2, Phase 3 |
| 5    | T2 | Phase 1+3, SDE activity 8 |
| 6    | T3 | Phase 1+3, SDE activity 7 |
| 7    | Reactions | Phase 5, SDE activity 11 |

---

## 8. Build stages (execution roadmap)

Principle: build **bottom-up** within each phase — pure calc first, then data
wiring, then UI — so every stage compiles, runs, and is independently verifiable
before the next. Pure-calc stages get a `test_*.py` (pattern:
`test_inventory_replay.py`). "Ship?" = a sensible point to commit / bump version
(`config.py:10`; ask before bumping per CLAUDE.md).

### Stage 0 — Pre-flight (no app code)
- 0.1 Verify the live job-cost constants (SCC surcharge, NPC facility tax, alpha
  tax) against a current source and record them with a verify-date. (BPO market
  lookup was dropped, so the old R5 order-return check goes with it.)
- 0.2 Confirm `sde_industry.db` has the T1 product+material rows the engine needs
  (already verified: 4,847 products / 27,062 materials, activity 1).
- 0.3 Stub the tab: register a greyed "Industry" placeholder in `gui_main.py`
  (mirrors the Boosters try-import pattern) so the shell exists. **Ship.**

### Phase 1 — T1 Manufacturing Core
- 1.1 `industry_engine.py` pure calc: ME-adjust (`max(1, ceil(base×(100−ME)/100))`),
  EIV (Σ base_qty × adjusted_price), job cost (`EIV×(SCI×bonus+tax+SCC+alpha)`),
  margins (patient/immediate/30d). All inputs injected (prices, indices, recipe).
  **Design the material-cost path as a recursive node (cost = buy price OR
  sub-build), even though T1 is flat (terminal buys only)** — this is the
  extension point T2/reactions plug into at P5+, so there's no engine rewrite
  later (avoids the D1 refactor trap). → `test_industry_engine.py` with synthetic
  data incl. a one-level nested case to lock the recursion contract. **Verify:
  tests pass.**
- 1.2 `industry_market_data.py`: own `/industry/systems/` + `/markets/prices/`
  fetch over shared `api.ESIClient`, 6h TTL, AppData cache, all activities.
  **Verify: run headless, adjusted_prices + cost_indices populate.**
- 1.3 Headless compute path: T1 list from `sde_industry.db`, material prices from
  `order_cache.peek_cached_orders`, 7d/30d from `market_history`/`structure_history`,
  feed 1.1. **Verify: script prints build cost + margins for ~5 known items;
  spot-check against in-game/known values.**
- 1.4 `gui_industry.py` tab shell + Top Profit T1 list: columns, **universal
  sort + mini search**, category chips, min-profit filter, hub + **facility** +
  input-toggle controls. Wire to 1.3. **Verify: list renders, sorts, filters,
  toggles recompute. Ship.**
- 1.5 Detail panel: material breakdown, ME write-in, batch field, three margins,
  double-click → `graphing.py`, right-click copy/open. **Verify: select item,
  numbers reconcile with the list.**
- 1.6 Facility selector incl. write-in structure-bonus fields (material/time/cost
  %); fold into job cost. **Verify: changing bonus moves job cost. Ship + bump.**

### Phase 2 — Characters tab
- 2.1 `industry_characters.py` roster: ≤10 `CharacterAuth` records (reuse esi_auth
  PKCE helpers), own JSON + scope set, write-in implant % field. **Verify: log in
  a char, token persists across restart, seller/buyer auth unaffected.**
- 2.2 `industry_skills.py` per-char skill pull (Industry/Adv Ind/Research/Metallurgy),
  cache keyed by character_id. **Verify: pulled levels match in-game.**
- 2.3 `industry_standings.py` per-char standings (kept for future). **Verify:
  pulls without error.**
- 2.4 Characters sub-tab: minimal cards (portrait via `images.evetech.net`,
  cached; name), Skills dialog (pulled + write-in implants + re-pull), Standings
  dialog, blueprint login/pull buttons. **Verify: card renders, dialogs open,
  re-pull works. Ship.**
- 2.5 "Built by" dropdown on items: selection **persists but is intentionally
  inert until P4** (its only effect is build time, which doesn't exist yet) — the
  Phase 2 ship checkpoint must NOT treat the no-op as a bug.

### Phase 3 — Owned BPO/BPC master list
- 3.1 `industry_blueprints.py` SQLite store + schema (keyed by character_id).
- 3.2 Blueprint pull `GET /characters/{id}/blueprints/`, slow-pace safety, dedup,
  one-time + manual re-pull. **Verify: owned BPs land in DB with ME/TE/runs.**
- 3.3 Owned master list UI (universal sort+search); each row → reuse the Phase 1
  build breakdown. **Verify: select owned item, breakdown matches Top Profit.**
- 3.4 BPC pricing: write-in (every BPC) > `find_bpc_offers` (flag if empty) > unset
  `*`; amortize price ÷ runs; hard batch cap = runs left. **Verify: write-in and
  contract paths both reflect in cost. Ship + bump.**
- 3.5 R8 harvest: `location_id ≥ 1e12` → "register as industry hub" via existing
  `resolve_region_for_system` + `add_custom_station`. **Verify: structure appears
  as a hub.**

### Phase 4 — Research popup + time-based batch cap
- 4.1 Build-time calc in `industry_engine.py`: base time × TE × Industry/Adv
  Industry (Built-by char) × write-in implant %. → extend `test_industry_engine`.
- 4.2 Time-based batch cap wired into list/detail (≤30 days; single run >30d →
  max 1; selectable max). **Verify: cap tracks skills + run count.**
- 4.3 Research-cost popup (`fit_window`): ME0→target, optional TE, time from
  skills+implants, cost from indices, totals; on owned + planned. **Verify: ME10
  cost/time reconcile with in-game. Ship + bump.**

### Phases 5–7 — T2 / T3 / Reactions (each, same 3-stage shape)
- x.1 Expand `sde_industry.py` importer + schema for the activity (8 invention /
  7 reverse-eng / 11 reactions); re-download SDE. **Verify: recipes present.**
- x.2 Recursive chain costing in `industry_engine` for the new tier (components →
  terminal market buys; invention/RE/reaction job cost). → engine tests.
- x.3 Light up the TBA sub-tab + Top Profit ranking. **Ship + bump (ask: major?).**

### Cross-cutting (apply throughout)
- Map maintenance: update `CLAUDE.md` GUI/SDE/ESI sections as each `.py` lands.
- Version bumps: ask at each "Ship + bump" checkpoint, not silently.
- Slow-pace ESI safety on every authed pull (roster, skills, standings,
  blueprints) — shared `api.ESIClient` discipline.
