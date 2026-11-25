"""Pure arithmetic for the ``fulfilment_gap`` tool.

This module imports nothing from Frappe, so it can be tested without a site and cannot
read or write data. It turns Sales Order Item rows, Bin quantities and a formula into the
list of items that still need procurement.

Formula (the default, stated in every response):

    net = max(0, pending - stock - on_order)

* ``pending`` is ``(qty - delivered_qty) * conversion_factor`` summed over every Sales
  Order Item row with a positive balance, in the item's stock unit.
* ``stock`` is the sum of Bin ``actual_qty`` over the scoped warehouses.
* ``on_order`` is the sum of Bin ``ordered_qty`` (quantity on open Purchase Orders that
  has not been received). It is left out when ``subtract_on_order`` is false.
* ``reserved_qty`` is never subtracted. Reservations already include these orders.

An item is covered only when ``stock + on_order`` is at least ``pending``. Partly covered
items stay on the list with the difference. An item with no Bin row has stock 0 and is
flagged ``has_bin_record: false``.

All sums use ``Decimal`` so that ten thousand rows add up exactly.
"""
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal(0)
ONE = Decimal(1)
FORMULA = "net = max(0, pending - stock - on_order); pending = (qty - delivered_qty) * conversion_factor"
FORMULA_WITHOUT_ON_ORDER = "net = max(0, pending - stock); pending = (qty - delivered_qty) * conversion_factor"


class InvalidDataError(ValueError):
    """A row holds a value that cannot be used as a quantity."""


@dataclass
class ItemDemand:
    """Pending demand of one item over all scoped Sales Order lines."""

    item_code: str
    pending: Decimal = ZERO
    orders: dict[str, Decimal] = field(default_factory=dict)
    uom_converted: bool = False
    lines: int = 0


@dataclass
class DemandSummary:
    """Result of reading Sales Order Item rows."""

    items: dict[str, ItemDemand]
    lines_read: int = 0
    lines_with_pending: int = 0
    lines_without_item: int = 0


def to_decimal(value: Any, label: str, *, default: Decimal | None = ZERO) -> Decimal:
    """Convert a database number to ``Decimal``. ``None`` becomes ``default``."""
    if value is None:
        if default is None:
            raise InvalidDataError(f"{label} is missing")
        return default
    if isinstance(value, bool):
        raise InvalidDataError(f"{label} is not a number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidDataError(f"{label} is not a number") from exc
    if not number.is_finite():
        raise InvalidDataError(f"{label} is not finite")
    return number


def to_json_number(value: Decimal) -> int | float:
    """Return an int for whole numbers and a float (6 places) otherwise."""
    if value == value.to_integral_value():
        return int(value)
    return float(round(value, 6))


def consolidate_pending(lines: Iterable[Mapping[str, Any]]) -> DemandSummary:
    """Sum the pending quantity of every line per ``item_code``.

    A line is pending when ``qty - delivered_qty`` is greater than 0. Lines with nothing
    pending, and over-delivered lines, are counted as read but add nothing. The quantity
    is converted to the stock unit with ``conversion_factor`` (missing means 1).
    """
    summary = DemandSummary(items={})
    for row in lines:
        summary.lines_read += 1
        item_code = row.get("item_code")
        if not isinstance(item_code, str) or not item_code:
            summary.lines_without_item += 1
            continue
        qty = to_decimal(row.get("qty"), f"qty of {item_code}")
        delivered = to_decimal(row.get("delivered_qty"), f"delivered_qty of {item_code}")
        factor = to_decimal(row.get("conversion_factor"), f"conversion_factor of {item_code}", default=ONE)
        if factor <= ZERO:
            raise InvalidDataError(f"conversion_factor of {item_code} must be positive")
        balance = qty - delivered
        if balance <= ZERO:
            continue
        pending = balance * factor
        entry = summary.items.get(item_code)
        if entry is None:
            entry = summary.items[item_code] = ItemDemand(item_code)
        entry.pending += pending
        entry.lines += 1
        if factor != ONE:
            entry.uom_converted = True
        order = row.get("parent")
        if isinstance(order, str) and order:
            entry.orders[order] = entry.orders.get(order, ZERO) + pending
        summary.lines_with_pending += 1
    return summary


@dataclass
class GapReport:
    """Every pending item, with its net requirement, before paging."""

    needing: list[dict[str, Any]]
    covered_count: int
    pending_item_count: int
    without_bin_record: int
    formula: str


def compute_gap(
    demand: Mapping[str, ItemDemand],
    stock: Mapping[str, Decimal],
    on_order: Mapping[str, Decimal],
    *,
    subtract_on_order: bool = True,
) -> GapReport:
    """Apply the formula to every pending item, one by one.

    ``stock`` and ``on_order`` hold only items that have a Bin row. Items not in them
    have stock 0 and are flagged. The list is ordered by ``net_qty`` (largest first),
    then ``item_code``, so that paging is stable.
    """
    needing: list[dict[str, Any]] = []
    covered = 0
    without_bin = 0
    for item_code in sorted(demand):
        entry = demand[item_code]
        has_bin = item_code in stock or item_code in on_order
        if not has_bin:
            without_bin += 1
        item_stock = stock.get(item_code, ZERO)
        item_on_order = on_order.get(item_code, ZERO) if subtract_on_order else ZERO
        net = entry.pending - item_stock - item_on_order
        if net <= ZERO:
            covered += 1
            continue
        record: dict[str, Any] = {
            "item_code": item_code,
            "pending_qty": to_json_number(entry.pending),
            "stock_qty": to_json_number(item_stock),
            "on_order_qty": to_json_number(on_order.get(item_code, ZERO)),
            "net_qty": to_json_number(net),
            "source_orders": sorted(entry.orders),
            "has_bin_record": has_bin,
        }
        if entry.uom_converted:
            record["uom_converted"] = True
        needing.append(record)
    needing.sort(key=lambda r: (-Decimal(str(r["net_qty"])), r["item_code"]))
    return GapReport(
        needing=needing,
        covered_count=covered,
        pending_item_count=len(demand),
        without_bin_record=without_bin,
        formula=FORMULA if subtract_on_order else FORMULA_WITHOUT_ON_ORDER,
    )


def chunked(values: list[Any], size: int) -> list[list[Any]]:
    """Split ``values`` into lists of at most ``size`` items, keeping order."""
    if size < 1:
        raise ValueError("size must be at least 1")
    return [values[start : start + size] for start in range(0, len(values), size)]


def bin_chunk_size(
    *,
    complexity_limit: int,
    filter_values_limit: int,
    group_limit: int,
    has_warehouse_filter: bool,
) -> int:
    """How many item codes one Bin aggregation call may carry.

    The app scores an aggregation as ``aggregations * 4 + group_by fields * 8 +
    filters * 3 + filter values``. This call has 2 aggregations (``actual_qty`` and
    ``ordered_qty``), 1 group-by field (``item_code``) and one filter, or two with a
    warehouse. The result is also bounded by the filter-value and returned-group limits.
    Returns 0 when the limits leave no room for even one item.
    """
    filters = 2 if has_warehouse_filter else 1
    fixed = 2 * 4 + 1 * 8 + filters * 3 + (1 if has_warehouse_filter else 0)
    room = complexity_limit - fixed
    return max(0, min(room, filter_values_limit, group_limit))
