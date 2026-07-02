"""Mixin providing the Max Buy Price calculator UI + logic for watchlist dialogs.

Used by AddItemDialog (gui_watchlist_add.py) and EditItemDialog (gui_watchlist_search.py).
The calc section is only built when self.show_max_buy_calc is True; calc methods
early-return when the section was not built, so they are safe no-ops in that case.

Host class contract (attributes the mixin reads):
    self.show_max_buy_calc    : bool
    self.get_client           : Callable
    self.get_skills           : Optional[Callable]
    self.region_id            : int
    self.selected_item        : dict with keys {"type_id", "name"}
    self.price_under_var      : tk.StringVar

  Optional, only consulted when nearest_station_mode is True (NPC Orders flow):
    self.nearest_station_mode : bool  -- enables jump-filter + buyer-station rep
    self.get_origin_system    : () -> int system_id (origin for jump filter)
    self.get_esi_standings    : () -> ESIStandings (rep against any corp/faction)
    self.max_jumps            : int  -- defaults to 6
"""

import tkinter as tk
from tkinter import ttk
import asyncio
import threading

from core.calculate import (
    get_broker_fee_rate, get_sales_tax_rate, TradingSkills, DEFAULT_SKILLS,
)
from core.config import REQUEST_TIMEOUT
from core.tk_queue import submit


class MaxBuyCalcMixin:
    """Shared Max Buy Price calculator section + handlers."""

    def _init_calc_state(self):
        """Initialize calc state. Call from host __init__."""
        self.best_buy_price = None
        self.calculated_max_buy = None
        # Per-block max-buy values for the nearest-mode dual-pick UI.
        self.closest_max_buy = None
        self.max_rep_max_buy = None
        # Nearest-station mode (NPC Orders flow). Host overrides AFTER
        # _init_calc_state and BEFORE _build_max_buy_calc_section. Defaults
        # keep the original watchlist behavior unchanged.
        self.nearest_station_mode = False
        self.get_origin_system = None
        self.get_esi_standings = None
        self.max_jumps = 6
        # UI handles set in _build_max_buy_calc_section; left as None so
        # _calc_ui_ready() can detect "section not built" cleanly.
        self.calc_btn = None
        self.calc_status_label = None
        self.best_buy_label = None
        self.max_buy_label = None
        self.use_price_btn = None
        # Dual-pick blocks for nearest mode (closest + max-rep). Each is a
        # dict of widget refs returned by _build_pick_block().
        self.closest_block = None
        self.max_rep_block = None

    def _calc_ui_ready(self) -> bool:
        """True only when the calc UI has actually been built."""
        return getattr(self, "show_max_buy_calc", False) and self.calc_btn is not None

    def _build_max_buy_calc_section(self, parent=None):
        """Build the Max Buy Price Calculator UI inside `parent` (defaults to self).
        
        No-op when self.show_max_buy_calc is False.
        """
        if not getattr(self, "show_max_buy_calc", False):
            return

        if parent is None:
            parent = self

        calc_frame = ttk.LabelFrame(parent, text="Max Buy Price Calculator", padding=10)
        calc_frame.pack(fill=tk.X, padx=10, pady=5)

        calc_btn_row = ttk.Frame(calc_frame)
        calc_btn_row.pack(fill=tk.X)

        self.calc_btn = ttk.Button(calc_btn_row, text="Calculate Max Buy", command=self._calculate_max_buy)
        self.calc_btn.pack(side=tk.LEFT)

        self.calc_status_label = ttk.Label(calc_btn_row, text="", font=("Segoe UI", 8), foreground="gray")
        self.calc_status_label.pack(side=tk.LEFT, padx=10)

        calc_results = ttk.Frame(calc_frame)
        calc_results.pack(fill=tk.X, pady=(5, 0))

        ttk.Label(calc_results, text="Best Regional Buy:").pack(anchor=tk.W)
        self.best_buy_label = ttk.Label(calc_results, text="--", font=("Segoe UI", 9))
        self.best_buy_label.pack(anchor=tk.W, padx=(15, 0))

        if self.nearest_station_mode:
            # Two pick blocks: closest (jump-tiebreak) and max-rep (lowest-tax
            # tiebreak). Both rank among buyers tied at the top regional price.
            # Max-rep block is hidden until results show distinct stations.
            self.closest_block = self._build_pick_block(calc_results, self._use_closest_price)
            self.max_rep_block = self._build_pick_block(calc_results, self._use_max_rep_price)
            self.max_rep_block["frame"].pack_forget()
        else:
            ttk.Label(calc_results, text="Max Buy Price (1% profit):").pack(anchor=tk.W, pady=(3, 0))
            self.max_buy_label = ttk.Label(calc_results, text="--", font=("Segoe UI", 10, "bold"), foreground="green")
            self.max_buy_label.pack(anchor=tk.W, padx=(15, 0))

            self.use_price_btn = ttk.Button(
                calc_frame, text="Use as Alert Price",
                command=self._use_calculated_price, state=tk.DISABLED
            )
            self.use_price_btn.pack(anchor=tk.W, pady=(5, 0))

    def _build_pick_block(self, parent, use_callback):
        """Build one nearest-mode result block: station / distance / standings /
        tax / max-buy / Use button. Returns a dict of widget refs."""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=(8, 0))

        header = ttk.Label(frame, text="", font=("Segoe UI", 9, "bold"))
        header.pack(anchor=tk.W)

        ttk.Label(frame, text="Best buy station:").pack(anchor=tk.W, pady=(3, 0))
        station = ttk.Label(frame, text="—", font=("Segoe UI", 9))
        station.pack(anchor=tk.W, padx=(15, 0))

        ttk.Label(frame, text="Distance:").pack(anchor=tk.W, pady=(3, 0))
        distance = ttk.Label(frame, text="—", font=("Segoe UI", 9))
        distance.pack(anchor=tk.W, padx=(15, 0))

        ttk.Label(frame, text="Standings @ sell station:").pack(anchor=tk.W, pady=(3, 0))
        standings = ttk.Label(frame, text="—", font=("Segoe UI", 9))
        standings.pack(anchor=tk.W, padx=(15, 0))

        ttk.Label(frame, text="Tax @ that rep:").pack(anchor=tk.W, pady=(3, 0))
        tax = ttk.Label(frame, text="—", font=("Segoe UI", 9))
        tax.pack(anchor=tk.W, padx=(15, 0))

        ttk.Label(frame, text="Max Buy Price (1% profit):").pack(anchor=tk.W, pady=(3, 0))
        max_buy = ttk.Label(frame, text="--", font=("Segoe UI", 10, "bold"), foreground="green")
        max_buy.pack(anchor=tk.W, padx=(15, 0))

        use_btn = ttk.Button(frame, text="Use as Alert Price", command=use_callback, state=tk.DISABLED)
        use_btn.pack(anchor=tk.W, pady=(5, 0))

        return {
            "frame": frame,
            "header": header,
            "station": station,
            "distance": distance,
            "standings": standings,
            "tax": tax,
            "max_buy": max_buy,
            "use_btn": use_btn,
        }

    def _calculate_max_buy(self):
        """Fetch regional buy orders and calculate max profitable buy price."""
        # Safe no-op when the calc section wasn't built (e.g. personal watchlist)
        if not self._calc_ui_ready():
            return

        selected = getattr(self, "selected_item", None)
        if not selected:
            self.calc_status_label.configure(text="Select an item first")
            return

        type_id = selected["type_id"]
        self.calc_status_label.configure(text="Fetching buy orders...")
        self.calc_btn.configure(state=tk.DISABLED)
        self.best_buy_label.configure(text="...")
        if self.nearest_station_mode and self.closest_block is not None:
            self.closest_block["max_buy"].configure(text="...")
            self.closest_block["use_btn"].configure(state=tk.DISABLED)
            if self.max_rep_block is not None:
                self.max_rep_block["frame"].pack_forget()
                self.max_rep_block["use_btn"].configure(state=tk.DISABLED)
        else:
            self.max_buy_label.configure(text="...")
            self.use_price_btn.configure(state=tk.DISABLED)

        def fetch_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            payload = None
            error_msg = None

            try:
                client = self.get_client() if self.get_client else None
                if not client:
                    error_msg = "No client available"
                else:
                    payload, error_msg = loop.run_until_complete(
                        self._async_resolve_best_buy(client, type_id)
                    )
            except Exception as e:
                error_msg = str(e)
                print(f"Max buy calc error: {e}")
            finally:
                loop.close()

            if error_msg:
                submit(lambda: self._update_calc_error(error_msg))
            elif self.nearest_station_mode:
                submit(lambda: self._update_calc_display_nearest(payload))
            else:
                submit(lambda: self._update_calc_display(payload["best_buy"]))

        threading.Thread(target=fetch_thread, daemon=True).start()

    async def _async_resolve_best_buy(self, client, type_id: int):
        """Fetch buy orders and (in nearest mode) resolve station + jumps.

        Returns (payload, error_msg). Exactly one of payload/error_msg is
        non-None.
          - non-nearest: payload = {"best_buy": float}
          - nearest:     payload = {"candidates": [list of resolved top-price
                                    buyers, each {price, location_id, system_id,
                                    jumps, station_info}]}
        """
        import aiohttp
        from gui.gui_jump_cache import JumpCache
        from gui.gui_station_lookup import StationLookup, PLAYER_STRUCTURE_ID_THRESHOLD

        client.reset_for_new_loop()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        ) as session:
            client.session = session

            # Paginate buy orders for this type in the configured region.
            url = f"https://esi.evetech.net/latest/markets/{self.region_id}/orders/"
            all_orders = []
            page = 1
            while True:
                async with session.get(url, params={
                    "type_id": type_id,
                    "order_type": "buy",
                    "page": page,
                }) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    if not data:
                        break
                    all_orders.extend(data)
                    total_pages = int(resp.headers.get("X-Pages", 1))
                    if page >= total_pages:
                        break
                    page += 1

            if not all_orders:
                return (None, "No buy orders found")

            if not self.nearest_station_mode:
                return ({"best_buy": max(o["price"] for o in all_orders)}, None)

            # --- Nearest-station mode -------------------------------------
            # NPC sales tax doesn't apply the same way at player structures
            # (structure owner sets a market tax outside the rep system).
            candidates = [
                o for o in all_orders
                if o.get("location_id", 0) < PLAYER_STRUCTURE_ID_THRESHOLD
            ]
            if not candidates:
                return (None, "Only player-structure buy orders -- skipping")

            origin = self.get_origin_system() if self.get_origin_system else None
            if origin is None:
                return (None, "Origin system not configured")

            # Resolve jumps for each unique system once (cached across calls).
            unique_systems = {o["system_id"] for o in candidates if o.get("system_id")}
            jc = JumpCache.singleton()
            jumps_map: dict[int, int] = {}
            for sys_id in unique_systems:
                j = await jc.fetch(session, origin, sys_id)
                if j is not None:
                    jumps_map[sys_id] = j

            in_range = [
                o for o in candidates
                if jumps_map.get(o.get("system_id"), 999) <= self.max_jumps
            ]
            if not in_range:
                return (None,
                        f"No buy orders within {self.max_jumps} jumps of origin")

            # Surface every buyer at the top regional price so the display can
            # show "closest" vs "max-rep" picks side-by-side. Tax math is done
            # main-thread-side after station_info is resolved here.
            top_price = max(o["price"] for o in in_range)
            tied = [o for o in in_range if o["price"] == top_price]

            sl = StationLookup.singleton()
            resolved = []
            for o in tied:
                info = await sl.fetch(session, o["location_id"])
                resolved.append({
                    "price": o["price"],
                    "location_id": o["location_id"],
                    "system_id": o.get("system_id"),
                    "jumps": jumps_map.get(o.get("system_id")),
                    "station_info": info,
                })

            return ({"candidates": resolved}, None)

    def _update_calc_error(self, msg: str):
        """Display calculation error."""
        if not self._calc_ui_ready():
            return
        self.calc_status_label.configure(text=f"Error: {msg[:40]}")
        self.calc_btn.configure(state=tk.NORMAL)
        self.best_buy_label.configure(text="--")
        if self.nearest_station_mode and self.closest_block is not None:
            self.closest_block["max_buy"].configure(text="--")
            self.closest_block["use_btn"].configure(state=tk.DISABLED)
            if self.max_rep_block is not None:
                self.max_rep_block["frame"].pack_forget()
                self.max_rep_block["use_btn"].configure(state=tk.DISABLED)
        elif self.max_buy_label is not None:
            self.max_buy_label.configure(text="--")

    def _update_calc_display(self, best_buy: float):
        """Calculate and display max buy price from best regional buy order."""
        if not self._calc_ui_ready():
            return

        self.best_buy_price = best_buy
        self.best_buy_label.configure(text=f"{best_buy:,.2f} ISK")

        # Get current skills (with standings/manual overrides) for fee calc
        skills = None
        if getattr(self, "get_skills", None):
            skills = self.get_skills()

        broker_rate = get_broker_fee_rate(skills) / 100.0
        tax_rate = get_sales_tax_rate(skills) / 100.0
        target_margin = 0.01  # 1% profit target

        # Instant-buy workflow: we buy from existing sell orders (no buy-side
        # broker fee), then sell our own sell order (broker + sales tax).
        # net_revenue = best_buy * (1 - broker_rate - tax_rate)   # what we pocket
        # For 1% profit: net_revenue / max_buy >= 1.01
        # => max_buy = net_revenue / (1 + target_margin)
        net_revenue = best_buy * (1.0 - broker_rate - tax_rate)
        max_buy = net_revenue / (1.0 + target_margin)

        self.calculated_max_buy = max_buy
        self.max_buy_label.configure(text=f"{max_buy:,.2f} ISK")

        fee_info = f"Broker: {broker_rate*100:.2f}% | Tax: {tax_rate*100:.2f}%"
        if skills:
            fee_info += " (from skills)"
        else:
            fee_info += " (default - no skills)"
        self.calc_status_label.configure(text=fee_info)

        self.calc_btn.configure(state=tk.NORMAL)
        self.use_price_btn.configure(state=tk.NORMAL)

    def _update_calc_display_nearest(self, payload: dict):
        """Render the dual-pick nearest-mode result: 'Closest' and 'Max-rep'
        picks from the buyers tied at the top regional price. Collapses to a
        single block when both picks land on the same station.
        """
        if not self._calc_ui_ready():
            return

        candidates = payload.get("candidates") or []
        if not candidates:
            self._update_calc_error("No candidates returned")
            return

        top_price = candidates[0]["price"]
        self.best_buy_price = top_price
        self.best_buy_label.configure(text=f"{top_price:,.2f} ISK")

        standings_obj = self.get_esi_standings() if self.get_esi_standings else None
        base = self.get_skills() if self.get_skills else DEFAULT_SKILLS
        if base is None:
            base = DEFAULT_SKILLS

        target_margin = 0.01
        for c in candidates:
            info = c.get("station_info") or {}
            corp_id = info.get("corp_id")
            faction_id = info.get("faction_id")
            corp_std = 0.0
            faction_std = 0.0
            if standings_obj:
                if corp_id:
                    corp_std = standings_obj.get_corp_standing(corp_id, slot="seller")
                if faction_id:
                    faction_std = standings_obj.get_faction_standing(faction_id, slot="seller")
            adjusted = TradingSkills(
                broker_relations=base.broker_relations,
                accounting=base.accounting,
                advanced_broker_relations=base.advanced_broker_relations,
                station_standing=corp_std,
                faction_standing=faction_std,
                manual_broker_fee=base.manual_broker_fee,
                manual_sales_tax=base.manual_sales_tax,
            )
            broker_rate = get_broker_fee_rate(adjusted) / 100.0
            tax_rate = get_sales_tax_rate(adjusted) / 100.0
            net_revenue = c["price"] * (1.0 - broker_rate - tax_rate)
            c["corp_std"] = corp_std
            c["faction_std"] = faction_std
            c["broker_rate"] = broker_rate
            c["tax_rate"] = tax_rate
            c["max_buy"] = net_revenue / (1.0 + target_margin)

        # Closest: fewest jumps. Max-rep: lowest tax (monotonic in rep benefit).
        # Each tiebreaks against the other so the picks differ only when they
        # truly diverge.
        closest = min(
            candidates,
            key=lambda c: (c["jumps"] if c["jumps"] is not None else 999, c["tax_rate"]),
        )
        max_rep = min(
            candidates,
            key=lambda c: (c["tax_rate"], c["jumps"] if c["jumps"] is not None else 999),
        )

        distinct = max_rep["location_id"] != closest["location_id"]

        self._render_pick_block(
            self.closest_block, closest,
            header_text=(f"Closest pick — {closest['jumps']} jump(s)" if distinct else "")
        )
        self.closest_max_buy = closest["max_buy"]
        self.closest_block["use_btn"].configure(state=tk.NORMAL)

        if distinct:
            self._render_pick_block(
                self.max_rep_block, max_rep,
                header_text=f"Max-rep pick — {max_rep['jumps']} jump(s)"
            )
            self.max_rep_max_buy = max_rep["max_buy"]
            self.max_rep_block["use_btn"].configure(state=tk.NORMAL)
            self.max_rep_block["frame"].pack(fill=tk.X, pady=(8, 0))
        else:
            self.max_rep_max_buy = None
            self.max_rep_block["use_btn"].configure(state=tk.DISABLED)
            self.max_rep_block["frame"].pack_forget()

        self.calc_status_label.configure(
            text=f"Broker: {closest['broker_rate']*100:.2f}%  ·  "
                 f"Tax varies by station rep ({len(candidates)} tied @ top price)"
        )
        self.calc_btn.configure(state=tk.NORMAL)

    def _render_pick_block(self, block: dict, c: dict, header_text: str):
        """Fill a pick block's widgets with one candidate's resolved data."""
        block["header"].configure(text=header_text)
        info = c.get("station_info") or {}
        station_name = info.get("name") or f"Station {c['location_id']}"
        block["station"].configure(text=station_name)
        if c.get("jumps") is not None:
            block["distance"].configure(text=f"{c['jumps']} jump(s)")
        else:
            block["distance"].configure(text="?")
        block["standings"].configure(
            text=f"Corp {c['corp_std']:.2f}  ·  Faction {c['faction_std']:.2f}"
        )
        block["tax"].configure(text=f"{c['tax_rate']*100:.2f}%")
        block["max_buy"].configure(text=f"{c['max_buy']:,.2f} ISK")

    def _use_calculated_price(self):
        """Copy calculated max buy price into the Alert if price UNDER field."""
        if self.calculated_max_buy is not None and hasattr(self, "price_under_var"):
            self.price_under_var.set(str(int(self.calculated_max_buy)))

    def _use_closest_price(self):
        """Copy the closest-pick max-buy into the Alert if price UNDER field."""
        if self.closest_max_buy is not None and hasattr(self, "price_under_var"):
            self.price_under_var.set(str(int(self.closest_max_buy)))

    def _use_max_rep_price(self):
        """Copy the max-rep-pick max-buy into the Alert if price UNDER field."""
        if self.max_rep_max_buy is not None and hasattr(self, "price_under_var"):
            self.price_under_var.set(str(int(self.max_rep_max_buy)))
