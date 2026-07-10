"""Sell-side fee resolution for the Industry tab (PLAN_industry_settings.md
Stage 2, 2026-07-10).

The Industry tab's margins are net of broker fee + sales tax at the Sell-at
hub. Before this module those rates came invisibly from the trading SELLER
slot of character_skills.json; Caleb wanted the source visible and choosable.

Resolution order (locked by Caleb 2026-07-10):
  1. write-in overrides (broker %, tax %) — blank = auto, each side
     independent, so a lone broker write-in still resolves tax from the
     character source below;
  2. a selected industry-roster character: Broker Relations + Accounting from
     the full-sheet `IndustrySkills` cache, plus BASE standings vs the
     sell-hub owner corp/faction (`IndustryStandings.get_base` — the in-game
     broker fee ignores Connections/Diplomacy, verified 2026-07-03; station
     owner resolved via `StationLookup`, built-in hubs need no network);
  3. default: the trading SELLER slot's cached skills at the sell hub — the
     pre-Stage-2 behavior, byte-identical fallback.

No Tk in here. The compute worker calls with allow_fetch=True (may hit ESI
for a cold roster skills/standings cache, 1h TTL); UI-thread callers pass
allow_fetch=False (cache peeks only) and fall back to the trading-seller
default while a roster cache is still cold — the next compute warms it.

If a roster character's standings are unavailable but their skills are
cached, standings count as 0.0 (slightly overstates the broker fee — a
conservative estimate, never a fake profit).
"""

from typing import Optional, Tuple

from industry.industry_engine import SellFees

# ESI skill type_ids, cross-checked against esi/esi_skills.py SKILL_IDS.
SKILL_BROKER_RELATIONS = 3446
SKILL_ACCOUNTING = 16622


def _print(msg: str) -> None:
    print(f"[IndustryFees] {msg}")


def _as_float(value) -> Optional[float]:
    """None/blank/garbage -> None; numbers and numeric strings -> float."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def resolve_fees(hub_key: str, station_id: Optional[int],
                 choice: Optional[dict], skills, standings,
                 char_name: Optional[str] = None,
                 allow_fetch: bool = True) -> Tuple[SellFees, str]:
    """Resolve the effective sell-side fees + a human-readable source label.

    choice: persisted industry_settings.json key "fees" —
        {"char_id": int|None, "broker_override": float|None,
         "tax_override": float|None}. Missing/None keys mean "auto".
    skills / standings: the manager's IndustrySkills / IndustryStandings.
    char_name: display name for the label (caller resolves it from the
        roster; this module stays roster-agnostic).
    """
    choice = choice or {}
    b_ov = _as_float(choice.get("broker_override"))
    t_ov = _as_float(choice.get("tax_override"))
    char_id = choice.get("char_id")

    if b_ov is not None and t_ov is not None:
        return SellFees(broker_fee_pct=b_ov, sales_tax_pct=t_ov), "write-in"

    auto = None
    label = ""
    if char_id:
        auto = _roster_rates(int(char_id), station_id, skills, standings,
                             allow_fetch)
        if auto is not None:
            label = f"{char_name or char_id} (roster)"
    if auto is None:
        auto, label = _seller_rates(hub_key)
        if char_id:
            label += " — roster cache cold" if not allow_fetch else \
                     " — roster fetch failed"

    broker = b_ov if b_ov is not None else auto[0]
    tax = t_ov if t_ov is not None else auto[1]
    if b_ov is not None or t_ov is not None:
        label = f"write-in + {label}"
    return SellFees(broker_fee_pct=broker, sales_tax_pct=tax), label


def _roster_rates(char_id: int, station_id: Optional[int], skills, standings,
                  allow_fetch: bool) -> Optional[Tuple[float, float]]:
    """(broker %, tax %) for a roster character, or None to fall back
    (cold cache in peek mode, or the fetch itself failed)."""
    try:
        if allow_fetch:
            skills.get_skill_level(char_id, SKILL_BROKER_RELATIONS)
        # peek after any fetch: None means the sheet is genuinely unavailable
        # (no auth / network down) — get_skill_level alone can't tell "not
        # trained" (0) apart from "fetch failed" (also 0).
        br = skills.peek_skill_level(char_id, SKILL_BROKER_RELATIONS)
        acc = skills.peek_skill_level(char_id, SKILL_ACCOUNTING)
        if br is None or acc is None:
            return None

        corp_st = fac_st = 0.0
        base = standings.get_base(char_id, allow_fetch=allow_fetch)
        if base and station_id:
            from gui.gui_station_lookup import StationLookup
            info = StationLookup.singleton().lookup(int(station_id)) or {}
            corp_st = float(base.get("npc_corps", {})
                            .get(info.get("corp_id"), 0.0) or 0.0)
            fac_st = float(base.get("factions", {})
                           .get(info.get("faction_id"), 0.0) or 0.0)
        elif base is None:
            _print(f"standings unavailable for {char_id} - using 0.0 "
                   f"(broker fee slightly overstated)")

        from core.calculate import (TradingSkills, get_broker_fee_rate,
                                    get_sales_tax_rate)
        ts = TradingSkills(broker_relations=br, accounting=acc,
                           station_standing=corp_st, faction_standing=fac_st)
        return get_broker_fee_rate(ts), get_sales_tax_rate(ts)
    except Exception as e:
        _print(f"roster fee resolution failed for {char_id}: {e}")
        return None


def _seller_rates(hub_key: str) -> Tuple[Tuple[float, float], str]:
    """The pre-Stage-2 default: trading SELLER slot at the sell hub."""
    try:
        from core.calculate import (load_cached_skills, get_broker_fee_rate,
                                    get_sales_tax_rate,
                                    get_cached_skills_summary)
        ts = load_cached_skills(hub_key)
        summary = get_cached_skills_summary() or {}
        name = (summary.get("seller") or {}).get("name")
        label = (f"{name} (trading seller)" if name
                 else "trading seller defaults")
        return (get_broker_fee_rate(ts), get_sales_tax_rate(ts)), label
    except Exception as e:
        _print(f"seller fee lookup failed (defaults 3/4.5): {e}")
        return (3.0, 4.5), "defaults (no skills cache)"
