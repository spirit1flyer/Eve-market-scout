"""Small, side-effect-free helpers for Industry terminal diagnostics.

The GUI surfaces own when a diagnostic is emitted and which tag it uses. This
module only derives aggregate counts from engine results so Top Profit and the
Owned Blueprint panel report the same price and missing-input semantics.
"""

from collections import Counter
from typing import Dict, Iterable, Mapping


def _zero_input_count(entries: Iterable[dict]) -> int:
    """Count terminal market inputs that resolved to a zero/negative price."""
    count = 0
    for entry in entries:
        node = entry.get("node") or {}
        children = node.get("inputs") or []
        if children:
            count += _zero_input_count(children)
        elif node.get("kind") == "market" and (node.get("unit_cost") or 0) <= 0:
            count += 1
    return count


def summarize_results(results: Mapping[object, dict]) -> Dict[str, object]:
    """Return stable aggregate diagnostics for calc results."""
    price_sources = Counter()
    bpc_sources = Counter()
    zero_input_rows = 0
    zero_input_products = 0

    for result in results.values():
        price_sources[str(result.get("patient_source") or "unpriced")] += 1
        bpc = result.get("_bpc") or {}
        if bpc:
            bpc_sources[str(bpc.get("source") or "unknown")] += 1
        missing = _zero_input_count(result.get("inputs") or [])
        zero_input_rows += missing
        zero_input_products += int(missing > 0)

    for source in ("7d", "30d", "90d", "unpriced"):
        price_sources.setdefault(source, 0)

    return {
        "price_sources": dict(price_sources),
        "bpc_sources": dict(bpc_sources),
        "zero_input_rows": zero_input_rows,
        "zero_input_products": zero_input_products,
    }


def format_counts(counts: Mapping[str, int]) -> str:
    """Format a counter deterministically for one-line terminal output."""
    return ",".join(f"{key}:{counts[key]}" for key in sorted(counts)) or "none"
