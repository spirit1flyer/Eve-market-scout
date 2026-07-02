"""Reprocess-or-Sell engine — pure calc, no GUI and no network.

Given a paste of EVE items, decides per item whether to SELL it at the lowest
sell order or REPROCESS it for material value, at the currently selected
scanner hub.

The engine is deliberately standalone-testable: callers inject an SDE manager
(local item / material / market-group lookups) and a `price_fn(type_id)` that
returns the lowest sell price for a type at the chosen hub (or None if no sell
order is on the book). No ESI / no Tk in here.

See project memory `project_reprocess_or_sell_module` for the agreed design.

v1 scope = general junk (modules / ammo / salvage) reprocessed with the
Scrapmetal Processing skill. Ore / ice use a different skill set (Reprocessing /
Reprocessing Efficiency + ore skills) and are flagged "not calculated" — that's
a later phase.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# Scrapmetal Processing — +2% yield per level on non-ore reprocessing.
# (General Reprocessing / Reprocessing Efficiency do NOT affect junk.)
SCRAPMETAL_PROCESSING_SKILL_ID = 12196

# Sensible defaults when the user hasn't overridden the settings row.
DEFAULT_STATION_BASE_RATE = 0.50   # fraction; some highsec stations are 0.25–0.30
DEFAULT_REPROCESS_TAX = 0.05       # fraction; 5% at 0 standing, 0% at 6.67


@dataclass
class ReprocessSettings:
    """Top-of-tab inputs. All fractions (0.50 == 50%), not percentages."""
    station_base_rate: float = DEFAULT_STATION_BASE_RATE
    scrap_level: int = 0
    reprocess_tax: float = DEFAULT_REPROCESS_TAX

    @property
    def effective_yield(self) -> float:
        """Junk reprocessing yield fraction: base × (1 + 0.02 × scrap level)."""
        return self.station_base_rate * (1.0 + 0.02 * self.scrap_level)


@dataclass
class ParsedLine:
    """One parsed paste line: an item name and a stack quantity."""
    name: str
    qty: int
    raw: str


@dataclass
class MaterialOutput:
    """One reprocessing output material for a single input item."""
    material_type_id: int
    name: str
    quantity: int                      # total units produced across all batches
    unit_sell_price: Optional[float]   # lowest sell at hub, None if no order
    value: float                       # quantity × unit_sell_price (0 if no price)


@dataclass
class ItemResult:
    """Full evaluation for one pasted item."""
    input_name: str
    requested_qty: int
    type_id: Optional[int] = None
    matched_name: Optional[str] = None
    suggestion: Optional[str] = None        # closest SDE name when unmatched
    unit_volume: float = 0.0
    total_volume: float = 0.0
    portion_size: int = 1
    batches: int = 0
    leftover: int = 0                       # units below one full batch (wasted)
    sell_unit_price: Optional[float] = None
    sell_value: Optional[float] = None      # lowest-sell × qty (None if no price)
    reprocess_gross: Optional[float] = None # material value before tax
    reprocess_tax_isk: float = 0.0
    reprocess_net: Optional[float] = None   # None => "not calculated"
    materials: list[MaterialOutput] = field(default_factory=list)
    verdict: str = "—"                      # "SELL" | "REPROCESS" | "—"
    flags: list[str] = field(default_factory=list)

    @property
    def reprocess_calculated(self) -> bool:
        return self.reprocess_net is not None


# ---------------------------------------------------------------- paste parsing

# A trailing or leading quantity token: bare integer with optional comma
# thousands separators and an optional 'x' multiplier glyph ("x123", "123x").
_QTY_TOKEN = r"x?\s*([\d][\d,]*)\s*x?"


def _to_qty(token: str) -> Optional[int]:
    """Parse an integer quantity, tolerating comma thousands and an x glyph.

    Rejects anything with a decimal point — that's almost always a volume /
    price column from an EVE inventory copy, not a stack count.
    """
    if token is None:
        return None
    s = token.strip().lower().strip("x").strip()
    if not s or "." in s:
        return None
    s = s.replace(",", "")
    if not s.isdigit():
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_line(raw: str) -> Optional[ParsedLine]:
    """Parse a single paste line into (name, qty). Returns None for blanks.

    Handles, in order:
      - Tab-delimited EVE inventory copy: `Name<TAB>Qty<TAB>Group<TAB>…`
        (first integer-looking column after the name is the quantity; the
        decimal volume column is skipped by _to_qty).
      - Plain `Name 1234`, `Name x1234`, `Name 1,234`.
      - Leading quantity `1234 Name`, `1234x Name`.
    Defaults qty to 1 when no quantity token is present (single item).
    """
    line = raw.strip()
    if not line:
        return None

    # Tab-delimited (EVE inventory / cargo-scan copy).
    if "\t" in line:
        parts = [p.strip() for p in line.split("\t")]
        name = parts[0]
        if not name:
            return None
        qty = 1
        for p in parts[1:]:
            n = _to_qty(p)
            if n is not None:
                qty = n
                break
        return ParsedLine(name, qty, raw)

    # Trailing quantity: "Name 1234" / "Name x1234" / "Name 1,234".
    m = re.match(rf"^(.+?)\s+{_QTY_TOKEN}$", line, re.IGNORECASE)
    if m:
        qty = _to_qty(m.group(2))
        if qty is not None:
            return ParsedLine(m.group(1).strip(), qty, raw)

    # Leading quantity: "1234 Name" / "1234x Name".
    m = re.match(rf"^{_QTY_TOKEN}\s+(.+?)$", line, re.IGNORECASE)
    if m:
        qty = _to_qty(m.group(1))
        if qty is not None and m.group(2).strip():
            return ParsedLine(m.group(2).strip(), qty, raw)

    # No quantity token — treat the whole line as a single item.
    return ParsedLine(line, 1, raw)


def parse_paste(text: str) -> list[ParsedLine]:
    """Parse a multi-line paste into ParsedLine rows, one per non-blank line."""
    if not text:
        return []
    out: list[ParsedLine] = []
    for raw in text.splitlines():
        parsed = _parse_line(raw)
        if parsed is not None:
            out.append(parsed)
    return out


# -------------------------------------------------------------- SDE helpers

def _match_type(sde, name: str):
    """Resolve a pasted name to a type_id via exact (case-insensitive) match.

    Returns (type_id, matched_name, suggestion). On no exact match, type_id is
    None and `suggestion` is the closest SDE name (if any) so the UI can hint
    at a typo / client-localisation mismatch.
    """
    rows = sde.search_types_by_name(name, limit=5, published_only=False)
    nlow = name.strip().lower()
    for r in rows:
        if r["name"].lower() == nlow:
            return r["type_id"], r["name"], None
    suggestion = rows[0]["name"] if rows else None
    return None, None, suggestion


def _is_ore_ice(sde, type_id: int) -> bool:
    """True if the type sits under an ore/ice market group.

    Ore / ice groups are named "Standard Ores", "Ice Ores", "Moon Ores", etc.
    — all end in "Ores". We match on that suffix rather than the bare token
    "ORE", which is the Outer Ring Excavations *manufacturer* brand (ships and
    modules), not asteroid ore.
    """
    info = sde.get_type_info(type_id)
    if not info or info.market_group_id is None:
        return False
    for mg_id in sde.get_market_group_ancestry(info.market_group_id):
        name = (sde.get_market_group_name(mg_id) or "").lower()
        if name.endswith("ores") or name == "ice ores":
            return True
    return False


# ------------------------------------------------------------- evaluation

def evaluate_item(
    line: ParsedLine,
    settings: ReprocessSettings,
    sde,
    price_fn: Callable[[int], Optional[float]],
) -> ItemResult:
    """Evaluate one pasted item: sell-as-is vs reprocess, with a verdict.

    `price_fn(type_id)` must return the lowest sell price for the type at the
    chosen hub, or None when there is no sell order on the book.
    """
    res = ItemResult(input_name=line.name, requested_qty=line.qty)

    type_id, matched, suggestion = _match_type(sde, line.name)
    if type_id is None:
        res.suggestion = suggestion
        res.flags.append("unmatched")
        return res  # reprocess + sell both "not calculated"

    res.type_id = type_id
    res.matched_name = matched

    info = sde.get_type_info(type_id)
    res.portion_size = (info.portion_size if info and info.portion_size else 1)
    res.unit_volume = (info.volume if info else 0.0) or 0.0
    res.total_volume = res.unit_volume * line.qty

    # Sell-as-is value (independent of reprocessing).
    sell_unit = price_fn(type_id)
    if sell_unit is not None:
        res.sell_unit_price = sell_unit
        res.sell_value = sell_unit * line.qty

    # Reprocess path — gated on matchable, reprocessable, non-ore, priced.
    res.batches = line.qty // res.portion_size
    res.leftover = line.qty - res.batches * res.portion_size

    if _is_ore_ice(sde, type_id):
        res.flags.append("ore_ice")
        _finalize_verdict(res)
        return res

    mats = sde.get_type_materials(type_id)
    if not mats:
        res.flags.append("no_materials")
        _finalize_verdict(res)
        return res

    if res.batches == 0:
        # Whole stack is below one portion — reprocessing yields nothing.
        res.flags.append("below_portion")
        _finalize_verdict(res)
        return res

    y = settings.effective_yield
    gross = 0.0
    any_priced = False
    for mat_id, base_qty in mats:
        units = int(math.floor(base_qty * res.batches * y))
        mat_price = price_fn(mat_id)
        mat_name = sde.get_type_name(mat_id) or f"#{mat_id}"
        if mat_price is not None:
            any_priced = True
            value = units * mat_price
        else:
            value = 0.0
        gross += value
        res.materials.append(MaterialOutput(
            material_type_id=mat_id,
            name=mat_name,
            quantity=units,
            unit_sell_price=mat_price,
            value=value,
        ))

    if not any_priced:
        # No output material has a sell price — can't value the reprocess path.
        res.flags.append("no_price")
        _finalize_verdict(res)
        return res

    res.reprocess_gross = gross
    res.reprocess_tax_isk = gross * settings.reprocess_tax
    res.reprocess_net = gross - res.reprocess_tax_isk
    _finalize_verdict(res)
    return res


def _finalize_verdict(res: ItemResult) -> None:
    """Pick SELL vs REPROCESS from whatever values are present."""
    sell = res.sell_value
    rep = res.reprocess_net
    if sell is None and rep is None:
        res.verdict = "—"
    elif rep is None:
        res.verdict = "SELL"
    elif sell is None:
        res.verdict = "REPROCESS"
    else:
        res.verdict = "REPROCESS" if rep > sell else "SELL"


@dataclass
class ReprocessReport:
    """Aggregate of an evaluated paste."""
    items: list[ItemResult]
    total_sell: float = 0.0          # sum of best-path-agnostic sell values
    total_best: float = 0.0          # sum of the higher of sell/reprocess
    total_volume: float = 0.0

    @property
    def reprocess_uplift(self) -> float:
        """How much choosing the best path beats selling everything."""
        return self.total_best - self.total_sell


def _merge_parsed(lines: list[ParsedLine], sde) -> list[ParsedLine]:
    """Collapse lines that resolve to the same type, summing quantities.

    EVE pastes routinely list one item as several stacks (different containers
    or hangars). Evaluating each separately not only double-rows the output but
    floors the reprocess batch math per-stack — two part-batches that would
    clear a portion boundary when combined each yield nothing. We merge on the
    matched type_id; unmatched lines collapse on their lowercased name so
    duplicate container labels fold together too. First-appearance order is
    preserved.
    """
    order: list = []
    merged: dict = {}
    for ln in lines:
        type_id, matched, _ = _match_type(sde, ln.name)
        if type_id is not None:
            key = ("id", type_id)
            display = matched
        else:
            key = ("name", ln.name.strip().lower())
            display = ln.name
        existing = merged.get(key)
        if existing is None:
            merged[key] = ParsedLine(display, ln.qty, ln.raw)
            order.append(key)
        else:
            existing.qty += ln.qty
    return [merged[k] for k in order]


def evaluate_paste(
    text: str,
    settings: ReprocessSettings,
    sde,
    price_fn: Callable[[int], Optional[float]],
) -> ReprocessReport:
    """Parse + evaluate a full paste, returning per-item results and totals."""
    items = [
        evaluate_item(line, settings, sde, price_fn)
        for line in _merge_parsed(parse_paste(text), sde)
    ]
    total_sell = 0.0
    total_best = 0.0
    total_volume = 0.0
    for it in items:
        total_volume += it.total_volume
        sell = it.sell_value or 0.0
        total_sell += sell
        rep = it.reprocess_net
        total_best += max(sell, rep) if rep is not None else sell
    return ReprocessReport(
        items=items,
        total_sell=total_sell,
        total_best=total_best,
        total_volume=total_volume,
    )
