"""Leading indicator help dialogs and shared letter mapping.

Used by gui_stockmarket_risk.py and gui_stockmarket_holdings.py to:
  - Convert a list of flag tags into a single display letter
    (W / w / C / B / A / blank) using a worst-wins priority order.
  - Show a general reference popup ("Indicator Help") explaining
    every flag and what it means for a trader.
  - Show a per-item details popup ("Show Indicator Details") with
    the underlying labels and active flags for one specific item.

Both panels call the same dialogs so the help text is centralized.
"""

import tkinter as tk
from tkinter import ttk
from typing import List, Optional
from gui.gui_window_utils import fit_window, make_scrollable


# =============================================================================
# Letter mapping (worst-wins priority)
# =============================================================================

# Order: most concerning first. Display letter is whichever fires first.
PRIORITY_ORDER = [
    "UNDERCUT SPIRAL",
    "LIQUIDITY DRAIN",
    "STEALTH BLEED",
    "DISTRIBUTION",
    "CAPITULATION",
    "COILED",
    "BREAKOUT SETUP",
    "ACCUMULATION",
]

LETTER_MAP = {
    "UNDERCUT SPIRAL": "W",
    "LIQUIDITY DRAIN": "W",
    "STEALTH BLEED": "w",
    "DISTRIBUTION": "w",
    "CAPITULATION": "w",
    "COILED": "C",
    "BREAKOUT SETUP": "B",
    "ACCUMULATION": "A",
}


def get_indicator_letter(flags: List[str]) -> str:
    """Return the display letter for a list of flags (worst wins).

    Empty list or no recognised flags returns empty string.
    """
    if not flags:
        return ""
    for flag in PRIORITY_ORDER:
        if flag in flags:
            return LETTER_MAP[flag]
    return ""


# =============================================================================
# Help text (general reference)
# =============================================================================

HELP_TEXT = """Leading Indicators - Quick Reference

These are computed from recent market history and tell you what's
happening BEHIND the price chart - whether sellers are crowding in,
demand is drying up, or a setup is brewing for a price move.

Window: last 30 days vs prior 30 days for trends; last 90 days vs
last 365 days for range compression. Recomputed once per day per hub.


==========================================================
W  WARNING - AUTO-PROMOTES ONE TIER UP
==========================================================
These two patterns most often kill a sell order, so the item gets
bumped to a higher risk tier (Low -> Med, Med -> High).

UNDERCUT SPIRAL
  What's happening: Daily volume is falling, but the number of
  active sell orders is rising.
  What it means: More sellers are showing up to a shrinking pool
  of buyers. Each new seller undercuts the previous one to compete,
  dragging the floor down.
  Trader impact: If you post here, expect to be undercut within
  hours. Floor will keep falling. Don't trade this until volume
  recovers or sellers thin out.

LIQUIDITY DRAIN
  What's happening: Both volume AND order count are dropping
  together. Spread is also widening.
  What it means: The market is going dormant. Buyers AND sellers
  are walking away. Whatever price the chart shows is stale -
  nobody is actually transacting.
  Trader impact: Your sell order will sit unfilled for weeks. Even
  if you're patient, when activity returns the price could be very
  different. Avoid.


==========================================================
w  WARNING - FLAGGED BUT NOT PROMOTED
==========================================================
Real concerns, but not as immediately destructive as the W flags.
Still trade with caution.

STEALTH BLEED
  What's happening: Price is flat, but volume has been quietly
  declining.
  What it means: The price LOOKS stable on the chart, but each day
  fewer units actually trade. The flat price is a mirage held up
  by a few stubborn sellers, not real demand.
  Trader impact: When the chart eventually breaks, it breaks DOWN.
  You're holding inventory at a price the market doesn't really
  support anymore.

DISTRIBUTION
  What's happening: Price is rising, but volume is falling as it
  rises.
  What it means: A thin rally. Usually 2-3 traders walking the
  price up between themselves with small orders, no real buyer
  demand pushing it. Whoever buys at the top is the bagholder.
  Related warning: high volume + flat price (no further rise) is
  the same pattern - large players unloading into buyers near the
  top.
  Trader impact: Don't chase the rally. The "ceiling" on your
  chart is fake - it'll snap back the moment one of the
  price-walkers stops playing.

CAPITULATION
  What's happening: Price is falling AND volume is spiking at the
  same time.
  What it means: Panic selling. People dumping stock fast,
  accepting whatever price they can get. Often the END of a
  downtrend, not the start.
  Trader impact: Could be a buying opportunity if you're confident
  in long-term value, but DANGEROUS - you're catching a falling
  knife. Wait for volume to normalize before buying in.


==========================================================
C  COILED SPRING - POTENTIAL SETUP
==========================================================

COILED
  What's happening: The recent trading range has compressed -
  daily highs and lows are getting closer together, both compared
  to the last year and right now.
  What it means: Pressure is building. The market is squeezing
  into a narrow band. These setups usually resolve with a sharp
  move in one direction or the other within a few weeks.
  Trader impact: Don't post sell orders inside the compression
  range expecting normal flips - margins are tight. Watch for the
  breakout, then trade the direction it goes.


==========================================================
B / A  BULLISH SETUPS
==========================================================

BREAKOUT SETUP (B)
  What's happening: Range is expanding back out and volume is
  rising at the same time.
  What it means: The squeeze is resolving UPWARD. Buyers are
  stepping in and absorbing the compressed supply.
  Trader impact: Good buy candidate. Floor is likely to rise as
  the breakout confirms. Get in before the move is obvious on the
  price chart.

ACCUMULATION (A)
  What's happening: Price is flat, range is compressing, but
  volume is rising.
  What it means: Someone is quietly buying steady amounts without
  pushing the price up. Often a sign that a large player is
  loading up before a move.
  Trader impact: Bullish - the flat price is being defended by
  real demand. Floor should hold and likely rise. Reasonable buy,
  low downside.


==========================================================
HEALTHY (blank cell)
==========================================================
No notable divergence between price, volume, order count, spread,
and range. Market is behaving normally.


==========================================================
Notes
==========================================================
- Recomputed once per day per hub on first scan.
- Items with insufficient market history (under ~60 days) show
  blank instead of HEALTHY.
- An item can fire multiple flags. The cell shows the WORST flag
  by the priority order above (W beats w beats C beats B/A).
- Right-click any row and pick "Show Indicator Details" to see
  the underlying labels (price/volume/order/spread/compression
  trends) and every flag firing for that specific item.
"""


# =============================================================================
# Dialogs
# =============================================================================

def show_indicator_help_dialog(parent: tk.Misc):
    """Show a modal dialog with the general indicator reference."""
    dlg = tk.Toplevel(parent)
    dlg.title("Leading Indicators - Help")
    dlg.transient(parent)

    frame = ttk.Frame(dlg)
    frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    # Scrollable text area
    text_frame = ttk.Frame(frame)
    text_frame.pack(fill=tk.BOTH, expand=True)

    txt = tk.Text(
        text_frame,
        wrap=tk.WORD,
        font=("Consolas", 10),
        relief="flat",
        padx=8,
        pady=8,
    )
    vsb = ttk.Scrollbar(
        text_frame, orient=tk.VERTICAL, command=txt.yview
    )
    txt.configure(yscrollcommand=vsb.set)

    txt.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    text_frame.grid_rowconfigure(0, weight=1)
    text_frame.grid_columnconfigure(0, weight=1)

    txt.insert("1.0", HELP_TEXT)
    txt.configure(state=tk.DISABLED)

    btn_row = ttk.Frame(frame)
    btn_row.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(btn_row, text="Close", command=dlg.destroy).pack(
        side=tk.RIGHT
    )
    fit_window(dlg, min_width=720)


def show_indicator_details_dialog(
    parent: tk.Misc,
    item_name: str,
    result,
):
    """Show a modal with per-item indicator details.

    result: a LeadingIndicatorResult, or None if no data is cached.
    """
    dlg = tk.Toplevel(parent)
    dlg.title(f"Indicator Details: {item_name}")
    dlg.transient(parent)

    # Buttons pinned to window bottom (outside scroll area)
    btn_row = ttk.Frame(dlg)
    btn_row.pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=(8, 12))
    ttk.Button(
        btn_row, text="Indicator Help",
        command=lambda: show_indicator_help_dialog(dlg),
    ).pack(side=tk.LEFT)
    ttk.Button(
        btn_row, text="Close", command=dlg.destroy,
    ).pack(side=tk.RIGHT)
    ttk.Separator(dlg, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

    # Scrollable content area above the buttons.
    inner = make_scrollable(dlg)

    frame = ttk.Frame(inner)
    frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

    ttk.Label(
        frame, text=item_name,
        font=("Segoe UI", 13, "bold"),
    ).pack(anchor="w", pady=(0, 8))

    if result is None:
        ttk.Label(
            frame,
            text=("No leading indicator data cached for this item.\n\n"
                  "This usually means the item has under ~60 days of "
                  "market history,\nor the once-per-day computation "
                  "has not yet run for this hub."),
            justify="left",
        ).pack(anchor="w")
        fit_window(dlg, min_width=520)
        return

    # Section: underlying labels
    ttk.Label(
        frame, text="Underlying signals (last 30d vs prior 30d):",
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w", pady=(4, 4))

    labels_frame = ttk.Frame(frame)
    labels_frame.pack(fill=tk.X, pady=(0, 8))

    rows = [
        ("Price trend", result.price_label),
        ("Volume trend", result.volume_label),
        ("Order count trend", result.order_count_label),
        ("Spread trend", result.spread_label),
        ("Range compression (90d vs 365d)",
         result.compression_label),
    ]
    for i, (label, value) in enumerate(rows):
        ttk.Label(
            labels_frame, text=f"  {label}:",
        ).grid(row=i, column=0, sticky="w")
        ttk.Label(
            labels_frame, text=value,
            font=("Consolas", 10, "bold"),
        ).grid(row=i, column=1, sticky="w", padx=(12, 0))

    # Section: flags firing
    ttk.Separator(frame, orient="horizontal").pack(
        fill=tk.X, pady=(4, 8)
    )
    ttk.Label(
        frame, text="Flags firing:",
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w", pady=(0, 4))

    if not result.flags:
        ttk.Label(
            frame, text="  HEALTHY - no divergence flags",
            font=("Consolas", 10),
        ).pack(anchor="w")
    else:
        for flag in result.flags:
            letter = LETTER_MAP.get(flag, "?")
            ttk.Label(
                frame,
                text=f"  [{letter}] {flag}",
                font=("Consolas", 10),
            ).pack(anchor="w")

    fit_window(dlg, min_width=520)
