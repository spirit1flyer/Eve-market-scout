"""Wallet data models and staged parsing, shared by disk and ESI responses."""

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Optional


@dataclass
class Transaction:
    transaction_id: int
    date: datetime
    type_id: int
    type_name: str
    quantity: int
    unit_price: float
    is_buy: bool
    location_id: int
    journal_ref_id: int

    @property
    def total(self) -> float:
        return self.quantity * self.unit_price


@dataclass
class JournalEntry:
    entry_id: int
    date: datetime
    ref_type: str
    amount: float
    balance: float
    description: str
    context_id: Optional[int] = None
    context_type: Optional[str] = None


@dataclass
class MarketOrder:
    order_id: int
    type_id: int
    type_name: str
    is_buy_order: bool
    price: float
    volume_total: int
    volume_remain: int
    issued: datetime
    duration: int
    location_id: int
    state: str = "active"
    escrow: float = 0

    @property
    def volume_filled(self) -> int:
        return self.volume_total - self.volume_remain

    @property
    def is_complete(self) -> bool:
        return self.volume_remain == 0


def _date(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("wallet dates must include timezone")
    return parsed


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid numeric wallet field")
    return value


def _integer(value):
    if type(value) is not int:
        raise ValueError("invalid integer wallet field")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("invalid boolean wallet field")
    return value


def parse_wallet_resource(resource, payload):
    """Parse the entire payload before accepting it or advancing freshness."""
    if resource == "balance":
        return float(_number(payload))
    if not isinstance(payload, list):
        raise ValueError("wallet list response required")
    result = []
    seen = set()
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("wallet row must be an object")
        if resource == "transactions":
            item = Transaction(
                _integer(row["transaction_id"]), _date(row["date"]),
                _integer(row["type_id"]), "", _integer(row["quantity"]),
                _number(row["unit_price"]), _boolean(row["is_buy"]),
                _integer(row["location_id"]), _integer(row["journal_ref_id"]))
            identity = item.transaction_id
        elif resource == "journal":
            item = JournalEntry(
                _integer(row["id"]), _date(row["date"]), row.get("ref_type", ""),
                _number(row.get("amount", 0)), _number(row.get("balance", 0)),
                row.get("description", ""), row.get("context_id"),
                row.get("context_id_type"))
            if not isinstance(item.ref_type, str) or not isinstance(item.description, str):
                raise ValueError("invalid journal text field")
            if item.context_id is not None:
                _integer(item.context_id)
            identity = item.entry_id
        elif resource in ("orders", "order_history"):
            item = MarketOrder(
                order_id=_integer(row["order_id"]), type_id=_integer(row["type_id"]),
                type_name="", is_buy_order=_boolean(row.get("is_buy_order", False)),
                price=_number(row["price"]), volume_total=_integer(row["volume_total"]),
                volume_remain=_integer(row["volume_remain"]), issued=_date(row["issued"]),
                duration=_integer(row["duration"]), location_id=_integer(row["location_id"]),
                state=row.get("state", "expired") if resource == "order_history" else "active",
                escrow=_number(row.get("escrow", 0)))
            identity = item.order_id
        else:
            raise ValueError("unsupported wallet resource")
        if identity in seen:
            raise ValueError("duplicate wallet row identity")
        seen.add(identity)
        result.append(item)
    if resource in ("transactions", "journal"):
        result.sort(key=lambda item: item.date, reverse=True)
    return result
