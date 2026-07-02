"""ESI sync layer for scanner_inventory.InventoryManager.

Populates InventoryManager from ESIWallet data. Only acts on type_ids that
already have an InventoryEntry (flagged via "Add to Tracker"). Idempotent --
dedup is handled inside InventoryManager via transaction_id / order_id.

Designed to run alongside the existing TradeTracker sync without interfering.
"""

from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from scanning.scanner_inventory import InventoryManager
    from esi.esi_wallet import ESIWallet


def _ts(value) -> str:
    """Convert datetime (or anything) to ISO string."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _sales_tax_for(wallet, transaction) -> float:
    """Find the transaction_tax journal entry matching a sell transaction.

    ESI no longer populates context_id on transaction_tax entries (as of
    2026-05). We bridge via market_transaction, which still carries
    context_id == transaction_id, and the transaction_tax entry id is
    consistently market_transaction.entry_id + 1.
    """
    for mt in wallet.journal:
        if mt.ref_type != "market_transaction":
            continue
        if mt.context_id != transaction.transaction_id:
            continue
        target_id = mt.entry_id + 1
        for tax in wallet.journal:
            if tax.entry_id == target_id and tax.ref_type == "transaction_tax":
                print(f"[FeeDiag] sales_tax tx={transaction.transaction_id}: "
                      f"BRIDGE via market_transaction id={mt.entry_id} -> "
                      f"transaction_tax id={tax.entry_id} amount={abs(tax.amount)}")
                return abs(tax.amount)
        # market_transaction found but id+1 isn't a transaction_tax: user
        # has 0% effective sales tax for this sale.
        print(f"[FeeDiag] sales_tax tx={transaction.transaction_id}: "
              f"market_transaction id={mt.entry_id} found but no "
              f"transaction_tax at id+1 -> tax is 0")
        return 0.0
    # No market_transaction in journal (transaction outside journal window).
    print(f"[FeeDiag] sales_tax tx={transaction.transaction_id}: "
          f"no market_transaction match in journal -> 0 (likely out of window)")
    return 0.0


def _backfill_missing_fees(inventory: "InventoryManager",
                           wallet: "ESIWallet") -> dict:
    """Recover broker fees / sales tax for entries created before the
    ESI-context-id fix landed.

    Idempotent: only acts on listings where stored broker_fee == 0 and on
    sales where stored sales_tax == 0. If the journal has a match, the
    listing / sale gets updated AND the entry-level totals get adjusted
    so the Summary panel reconciles.
    """
    results = {"listing_fees_backfilled": 0, "sales_tax_backfilled": 0}
    if wallet is None:
        return results

    dirty = False
    for entry in inventory.entries.values():
        # --- Broker fees on active listings ---
        for listing in entry.active_listings:
            if listing.broker_fee > 0:
                continue
            try:
                issued = datetime.fromisoformat(
                    listing.listed_at.replace("Z", "+00:00")
                )
            except (ValueError, TypeError, AttributeError):
                continue
            fee = wallet.get_broker_fee_for_order(listing.order_id, issued=issued)
            if fee > 0:
                listing.broker_fee = fee
                entry.total_listing_fees += fee
                results["listing_fees_backfilled"] += 1
                dirty = True
                print(f"[FeeDiag] BACKFILL listing order_id={listing.order_id} "
                      f"({entry.type_name}): broker_fee 0 -> {fee}")

        # --- Sales tax on recorded sales (bridge via market_transaction) ---
        for sale in entry.sales:
            if sale.sales_tax > 0:
                continue
            tax_amt = 0.0
            for mt in wallet.journal:
                if mt.ref_type != "market_transaction":
                    continue
                if mt.context_id != sale.transaction_id:
                    continue
                for tx_tax in wallet.journal:
                    if (tx_tax.entry_id == mt.entry_id + 1
                            and tx_tax.ref_type == "transaction_tax"):
                        tax_amt = abs(tx_tax.amount)
                        break
                break
            if tax_amt > 0:
                sale.sales_tax = tax_amt
                entry.total_sales_tax += tax_amt
                # record_sale credited profit without subtracting tax; fix now.
                entry.total_realized_profit -= tax_amt
                results["sales_tax_backfilled"] += 1
                dirty = True
                print(f"[FeeDiag] BACKFILL sale tx={sale.transaction_id} "
                      f"({entry.type_name}): sales_tax 0 -> {tax_amt}")

    if dirty:
        inventory.save()
    return results


def sync_inventory_from_wallet(inventory: "InventoryManager",
                               wallet: Optional["ESIWallet"],
                               broker_fee_rate: float = 0.0) -> dict:
    """Sync ESI wallet data into scanner inventory.

    Only processes transactions/orders for type_ids that already have an
    InventoryEntry. Order of operations matters:
      1. Buys (so FIFO lots exist before sales consume them)
      2. Active sell orders (add/update + relist detection)
      3. Sales (FIFO-matched against lots from step 1)
      4. Retire orders that vanished from active list (look up final state)
      5. Capture broker fees for fulfilled orders we never saw active
         (actual fee from journal if present, else estimate from price * rate)
      6. Backfill missing fees on pre-fix listings/sales (idempotent)

    broker_fee_rate is a decimal fraction (e.g. 0.0148 for 1.48%); used only
    when journal lookup fails. Pass 0 to disable estimation.
    """
    results = {
        "buys_recorded": 0,
        "sales_recorded": 0,
        "orphan_fees_captured": 0,
        "listings_added": 0,
        "listings_updated": 0,
        "relists_recorded": 0,
        "orders_retired": 0,
        "listing_fees_backfilled": 0,
        "sales_tax_backfilled": 0,
    }

    if wallet is None:
        return results

    tracked_type_ids = set(inventory.entries.keys())
    if not tracked_type_ids:
        return results

    # 1. Buys first -- lots must exist before sales can FIFO-consume them.
    for tx in wallet.transactions:
        if not tx.is_buy:
            continue
        if tx.type_id not in tracked_type_ids:
            continue
        entry = inventory.entries[tx.type_id]
        _, was_new = inventory.record_buy(
            type_id=tx.type_id,
            type_name=entry.type_name or tx.type_name,
            transaction_id=tx.transaction_id,
            buy_price=tx.unit_price,
            quantity=tx.quantity,
            bought_at=_ts(tx.date),
        )
        if was_new:
            results["buys_recorded"] += 1

    # 2. Active sell orders -- add/update and detect relists.
    active_order_ids_per_type: dict = {}
    for order in wallet.orders:
        if order.is_buy_order:
            continue
        if order.type_id not in tracked_type_ids:
            continue

        active_order_ids_per_type.setdefault(order.type_id, set()).add(order.order_id)

        entry = inventory.entries[order.type_id]
        existing = next(
            (a for a in entry.active_listings if a.order_id == order.order_id),
            None
        )

        # Broker fee is only meaningful when first creating the listing record.
        # On updates, add_or_update_listing ignores it (early-returns before adding to fees).
        # The backfill step at the end handles existing listings whose fees
        # weren't captured.
        broker_fee = 0.0
        if existing is None:
            broker_fee = wallet.get_broker_fee_for_order(
                order.order_id, issued=order.issued
            ) or 0.0
            print(f"[FeeDiag] new listing order_id={order.order_id} "
                  f"type_id={order.type_id}: broker_fee resolved to {broker_fee}")

        _, listing, was_new = inventory.add_or_update_listing(
            type_id=order.type_id,
            type_name=entry.type_name or order.type_name,
            order_id=order.order_id,
            list_price=order.price,
            qty_listed=order.volume_total,
            qty_remaining=order.volume_remain,
            listed_at=_ts(order.issued),
            broker_fee=broker_fee,
        )
        if was_new:
            results["listings_added"] += 1
        else:
            results["listings_updated"] += 1
            # Price changed -> relist
            if abs(order.price - listing.current_price) > 0.001:
                relist_fees, relist_count = wallet.get_relist_fees_for_order(order.order_id)
                if relist_count > listing.relist_count:
                    new_fee = max(0.0, relist_fees - listing.relist_fees)
                    inventory.record_relist(
                        type_id=order.type_id,
                        order_id=order.order_id,
                        new_price=order.price,
                        fee=new_fee,
                    )
                    results["relists_recorded"] += 1

    # 3. Sales -- after buys so FIFO has lots to consume.
    for tx in wallet.transactions:
        if tx.is_buy:
            continue
        if tx.type_id not in tracked_type_ids:
            continue
        entry = inventory.entries[tx.type_id]
        sales_tax = _sales_tax_for(wallet, tx)
        _, was_new = inventory.record_sale(
            type_id=tx.type_id,
            type_name=entry.type_name or tx.type_name,
            transaction_id=tx.transaction_id,
            sell_price=tx.unit_price,
            quantity=tx.quantity,
            sales_tax=sales_tax,
            sold_at=_ts(tx.date),
        )
        if was_new:
            results["sales_recorded"] += 1

    # 4. Retire orders that disappeared from active list.
    # Reason: items expire after 90 days (returns to hangar, qty_out unchanged)
    # or get cancelled (same effect) or get fulfilled (qty_out already accounted
    # via the matching sell transaction in step 3).
    for type_id in tracked_type_ids:
        entry = inventory.entries[type_id]
        active_ids = active_order_ids_per_type.get(type_id, set())
        for listing in list(entry.active_listings):
            if listing.order_id in active_ids:
                continue
            # Look up final state from order_history; default to "fulfilled"
            # if not found (matches gui_tracking_sync's existing fallback).
            state = "fulfilled"
            for hist in wallet.order_history:
                if hist.order_id == listing.order_id:
                    state = hist.state
                    break
            inventory.retire_listing(type_id, listing.order_id, state)
            results["orders_retired"] += 1

    # 5. Catch fulfilled orders we never observed active.
    # If a listing was placed and fulfilled entirely between two syncs we
    # never got to capture its broker fee. order_history retains the order
    # past its active life. Try the real fee from the journal first; if that
    # entry has aged out of the journal window, estimate from price * rate.
    for hist in wallet.order_history:
        if hist.is_buy_order:
            continue
        if hist.type_id not in tracked_type_ids:
            continue
        if hist.state != "fulfilled":
            continue
        entry = inventory.entries[hist.type_id]
        order_id = hist.order_id
        if str(order_id) in entry.retired_listings:
            continue
        if any(a.order_id == order_id for a in entry.active_listings):
            continue

        actual = wallet.get_broker_fee_for_order(order_id, issued=hist.issued)
        if actual > 0:
            fee = actual
            print(f"[FeeDiag] orphan order {order_id}: JOURNAL fee = {fee}")
        elif broker_fee_rate > 0 and hist.price > 0 and hist.volume_total > 0:
            fee = hist.price * hist.volume_total * broker_fee_rate
            print(f"[FeeDiag] orphan order {order_id}: ESTIMATED fee = {fee} "
                  f"(price={hist.price} qty={hist.volume_total} "
                  f"rate={broker_fee_rate}) -- journal lookup returned 0")
        else:
            print(f"[FeeDiag] orphan order {order_id}: SKIPPED "
                  f"(journal=0, rate={broker_fee_rate})")
            continue

        inventory.record_orphan_listing(
            type_id=hist.type_id, order_id=order_id,
            broker_fee=fee, state="fulfilled",
        )
        results["orphan_fees_captured"] += 1

    # 6. Backfill missing fees on pre-fix data (idempotent).
    backfill = _backfill_missing_fees(inventory, wallet)
    results["listing_fees_backfilled"] = backfill["listing_fees_backfilled"]
    results["sales_tax_backfilled"] = backfill["sales_tax_backfilled"]

    return results
