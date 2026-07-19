"""Demand / Restock scanner.

Answers: "How much should I ship from source to destination to fill a real
demand gap?" Different lens than scanner_crosshub (which answers "where can I
exploit spreads?"). Selection logic deliberately drops the margin% floor that
killed every commodity material in the rolled-back toggle attempt.

Gates:
- dest_velocity >= min_velocity (real-demand gate, mandatory)
- days_of_stock <= max_days_of_stock (there is an actual gap)
- profit_per_unit >= 0 (no margin% floor, no min-total-profit)
- source must have stock to ship
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.calculate import TradingSkills, calculate_arbitrage_profit
from scanning.scanner_common import parse_history_stats


DEFAULT_MIN_VELOCITY = 1.0
DEFAULT_MAX_DAYS_OF_STOCK = 5.0
DEFAULT_HEALTHY_DAYS = 7.0
DEFAULT_MIN_MARGIN_PCT = 6.0    # margin floor at realistic sell price (covers broker + tax + 1% headroom)
DEFAULT_SORT_MODE = "total_profit"  # alt: "days_of_stock"

SETTINGS_FILENAME = "demand_settings.json"


# =============================================================================
# SETTINGS PERSISTENCE
# =============================================================================

_settings_cache: Optional[dict] = None
_settings_mtime: float = 0.0


def _settings_path() -> Path:
    from core.sound_manager import get_data_dir
    return Path(get_data_dir()) / SETTINGS_FILENAME


def _defaults() -> dict:
    return {
        "min_velocity": DEFAULT_MIN_VELOCITY,
        "max_days_of_stock": DEFAULT_MAX_DAYS_OF_STOCK,
        "healthy_days_target": DEFAULT_HEALTHY_DAYS,
        "min_margin_pct": DEFAULT_MIN_MARGIN_PCT,
        "sort_mode": DEFAULT_SORT_MODE,
    }


def load_demand_settings() -> dict:
    """Lazy-cached read of demand_settings.json (mtime-invalidated)."""
    global _settings_cache, _settings_mtime

    path = _settings_path()
    defaults = _defaults()

    if not path.exists():
        return defaults

    try:
        mtime = path.stat().st_mtime
        if _settings_cache is not None and mtime == _settings_mtime:
            return dict(_settings_cache)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = dict(defaults)
        out.update({k: v for k, v in data.items() if k in defaults})
        _settings_cache = out
        _settings_mtime = mtime
        return dict(out)
    except Exception as e:
        print(f"[Demand] settings load error: {e}")
        return defaults


def save_demand_settings(settings: dict):
    """Persist settings and invalidate the cache."""
    global _settings_cache, _settings_mtime
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    out = _defaults()
    out.update({k: v for k, v in settings.items() if k in out})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    _settings_cache = None
    _settings_mtime = 0.0


# =============================================================================
# DATA CLASS
# =============================================================================

@dataclass
class DemandRow:
    """One Demand/Restock opportunity at the destination hub."""
    type_id: int
    name: str

    buy_station: str
    sell_station: str

    source_price: float        # source lowest sell (what we pay)
    target_sell_price: float   # dest target sell

    source_velocity: float     # safe vel at source (sanity column)
    dest_velocity: float       # safe vel at dest (gate)
    dest_stock: int            # sum of dest sell-side qty
    source_available_qty: int  # sum of source sell-side qty

    days_of_stock: float       # dest_stock / dest_velocity
    restock_qty: int           # raw gap (healthy_days * dest_vel - dest_stock)
    ship_qty: int              # min(restock_qty, source_available)

    profit_per_unit: float
    total_profit: float        # profit_per_unit * ship_qty
    cargo_m3: float            # ship_qty * type_volume (0 if SDE unavailable)

    # Historical reference at destination — surfaces "is the listed target realistic?"
    dest_avg_7d: float = 0.0
    dest_avg_30d: float = 0.0
    # Realistic-sell sanity figures: profit/margin if the trade clears at the
    # *historical* dest price rather than the (possibly bogus) listed target.
    realistic_sell_price: float = 0.0
    realistic_margin_pct: float = 0.0

    target_uses_buy_order: bool = False  # True if dest buy order beat the undercut

    @property
    def target_over_avg_ratio(self) -> float:
        """target_sell_price divided by the higher of dest 7d/30d avg.
        Used to flag rows where the target sell price is far above the
        item's actual trading history (likely junk listing being undercut).
        Returns 0.0 if no historical reference is available.
        """
        ref = max(self.dest_avg_7d, self.dest_avg_30d)
        if ref <= 0:
            return 0.0
        return self.target_sell_price / ref


# =============================================================================
# TARGET SELL PRICE
# =============================================================================

def _calculate_target_sell(
    dest_data: dict,
    sell_history_stats,
) -> Optional[float]:
    """Pick a defensible target sell price at destination.

    Priority: undercut 2nd lowest sell, else undercut sole seller, else fall
    back to historical optimistic price (higher of 7d/30d). Returns None when
    no defensible target exists.
    """
    sell_2nd = dest_data.get("sell_2nd", float("inf"))
    sell_lowest = dest_data.get("sell", float("inf"))

    if sell_2nd < float("inf"):
        undercut = max(0.01, sell_2nd * 0.001)
        return sell_2nd - undercut
    if sell_lowest < float("inf"):
        undercut = max(0.01, sell_lowest * 0.001)
        return sell_lowest - undercut
    if sell_history_stats.optimistic_price > 0:
        return sell_history_stats.optimistic_price
    return None


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def build_demand_rows(
    buy_station_data: dict[int, dict],
    sell_station_data: dict[int, dict],
    names: dict[int, str],
    buy_station_history: dict[int, list[dict]],
    sell_station_history: dict[int, list[dict]],
    buy_station_key: str,
    sell_station_key: str,
    buy_skills: TradingSkills,
    sell_skills: TradingSkills,
    settings: Optional[dict] = None,
    reference_date: Optional[str] = None,
) -> list[DemandRow]:
    """Build the Demand/Restock row set for a source / dest pair.

    Inputs come from scanner._process_orders_crosshub (both sides) and the
    bulk-history calls already done by scan_crosshub — no extra ESI fetches.
    """
    if settings is None:
        settings = load_demand_settings()

    min_velocity = float(settings.get("min_velocity", DEFAULT_MIN_VELOCITY))
    max_days = float(settings.get("max_days_of_stock", DEFAULT_MAX_DAYS_OF_STOCK))
    healthy_days = float(settings.get("healthy_days_target", DEFAULT_HEALTHY_DAYS))
    min_margin_pct = float(settings.get("min_margin_pct", DEFAULT_MIN_MARGIN_PCT))

    # SDE for cargo volume is optional — quietly degrade if absent.
    sde = None
    try:
        from sde.sde_manager import get_sde_manager
        sde = get_sde_manager()
        if not sde.is_available():
            sde = None
    except Exception:
        sde = None

    rows: list[DemandRow] = []

    for type_id, sell_info in sell_station_data.items():
        sell_stats = parse_history_stats(
            sell_station_history.get(type_id, []), reference_date
        )
        dest_velocity = sell_stats.safe_velocity
        if dest_velocity < min_velocity:
            continue

        buy_info = buy_station_data.get(type_id)
        if not buy_info:
            continue
        source_price = buy_info.get("sell", float("inf"))
        if source_price == float("inf"):
            continue
        source_available = int(buy_info.get("total_sell_qty", 0))
        if source_available <= 0:
            continue

        dest_stock = int(sell_info.get("total_sell_qty", 0))
        days_of_stock = dest_stock / dest_velocity if dest_velocity > 0 else float("inf")
        if days_of_stock > max_days:
            continue

        target_sell = _calculate_target_sell(sell_info, sell_stats)
        if target_sell is None or target_sell <= 0:
            continue

        # Instant-sale path: if the dest buy order pays more than the undercut
        # target, prefer it — the seller can dump directly into the bid.
        dest_buy = sell_info.get("buy", 0) or 0
        target_uses_buy_order = False
        if dest_buy > target_sell:
            target_sell = dest_buy
            target_uses_buy_order = True

        restock_qty_raw = int(healthy_days * dest_velocity - dest_stock)
        if restock_qty_raw <= 0:
            # Possible if user pushed max_days >= healthy_days; no real gap.
            continue
        ship_qty = min(restock_qty_raw, source_available)
        if ship_qty <= 0:
            continue

        arb = calculate_arbitrage_profit(
            buy_price=source_price,
            sell_price=target_sell,
            quantity=ship_qty,
            buy_skills=buy_skills,
            sell_skills=sell_skills,
            buy_is_instant=True,
        )
        profit_per_unit = arb["profit_per_unit"]
        if profit_per_unit < 0:
            continue

        total_profit = profit_per_unit * ship_qty

        # Sanity gate: the listed target_sell may be a stale/junk order being
        # undercut (e.g. someone listed a Shield Hardener at 189M ISK). What
        # actually matters is whether the trade still clears at the item's
        # historical price. Recompute margin at the *realistic* sell price
        # (max of dest 7d/30d avg) and drop the row if it can't make
        # min_margin_pct there.
        realistic_sell = max(sell_stats.avg_price_7d, sell_stats.avg_price_30d)
        if realistic_sell > 0:
            realistic_arb = calculate_arbitrage_profit(
                buy_price=source_price,
                sell_price=realistic_sell,
                quantity=max(1, ship_qty),
                buy_skills=buy_skills,
                sell_skills=sell_skills,
                buy_is_instant=True,
            )
            realistic_margin = realistic_arb["margin_percent"]
            if realistic_margin < min_margin_pct:
                continue
        else:
            # No history at all — can't sanity-check; skip rather than guess.
            continue

        buy_stats = parse_history_stats(
            buy_station_history.get(type_id, []), reference_date
        )
        source_velocity = buy_stats.safe_velocity

        cargo_m3 = 0.0
        if sde is not None:
            try:
                vol = sde.get_type_volume(type_id)
                if vol is not None:
                    cargo_m3 = float(vol) * ship_qty
            except Exception:
                pass

        name = names.get(type_id, f"Unknown ({type_id})")

        rows.append(DemandRow(
            type_id=type_id,
            name=name,
            buy_station=buy_station_key,
            sell_station=sell_station_key,
            source_price=source_price,
            target_sell_price=target_sell,
            source_velocity=source_velocity,
            dest_velocity=dest_velocity,
            dest_stock=dest_stock,
            source_available_qty=source_available,
            days_of_stock=days_of_stock,
            restock_qty=restock_qty_raw,
            ship_qty=ship_qty,
            profit_per_unit=profit_per_unit,
            total_profit=total_profit,
            cargo_m3=cargo_m3,
            dest_avg_7d=sell_stats.avg_price_7d,
            dest_avg_30d=sell_stats.avg_price_30d,
            realistic_sell_price=realistic_sell,
            realistic_margin_pct=realistic_margin,
            target_uses_buy_order=target_uses_buy_order,
        ))

    return rows


def sort_demand_rows(rows: list[DemandRow], mode: str) -> list[DemandRow]:
    """Sort according to the user's chosen mode."""
    if mode == "days_of_stock":
        return sorted(rows, key=lambda r: r.days_of_stock)
    return sorted(rows, key=lambda r: r.total_profit, reverse=True)
