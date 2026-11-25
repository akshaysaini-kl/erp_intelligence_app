"""Read-only service behind the ``fulfilment_gap`` tool.

It answers one question in a single call: for a set of Sales Orders, which items are
still undelivered, and how much of each is not covered by stock or open Purchase Orders?

The service never reads the database itself. Every read goes through
``IntelligenceService.execute_request``, the same entry point the ``intelligence_request``
tool uses, so DocType validation, field policy, parent-record permission checks (for the
Sales Order Item rows) and the site's limits apply exactly as they do for the model's own
calls. The arithmetic lives in ``fulfilment_gap_math`` (pure, no Frappe).

It returns a complete result or raises. A partial read is never turned into a figure:
if any page, batch or aggregation is incomplete, the call fails.
"""
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import frappe
from frappe import _

from erp_intelligence.services import fulfilment_gap_math as gap_math
from erp_intelligence.services.config_service import ConfigService
from erp_intelligence.services.intelligence_service import IntelligenceService
from erp_intelligence.utils.response_utils import build_response

ORDER_DOCTYPE = "Sales Order"
LINE_DOCTYPE = "Sales Order Item"
OPEN_STATUSES = ["To Deliver", "To Deliver and Bill"]
LINE_FIELDS = ["parent", "item_code", "qty", "delivered_qty", "uom", "conversion_factor"]

MAX_ORDERS = 100
DEFAULT_ORDER_COUNT = 50
MAX_LINES = 5_000
MAX_PAGE_LENGTH = 250
DEFAULT_PAGE_LENGTH = 100
RESULT_TYPE = "fulfilment_gap"


class FulfilmentGapService:
    """Compute pending Sales Order demand against stock and open purchase quantity."""

    @classmethod
    def analyse(cls, request: Mapping[str, Any], user: str | None = None) -> dict[str, Any]:
        """Run the analysis for an already validated request. Raises on any incomplete read."""
        limits = ConfigService.get_limit_snapshot()
        warnings: list[str] = []

        order_names, selection = cls._select_orders(request, limits, user)
        lines, unreadable = cls._read_lines(order_names, limits, user)
        demand = gap_math.consolidate_pending(lines)
        if unreadable:
            warnings.append(
                f"{unreadable} of the requested orders are not readable by the current user "
                "or do not exist. They are not part of the result."
            )

        stock, on_order = cls._read_bin(sorted(demand.items), request.get("warehouse"), limits, user)
        gap = gap_math.compute_gap(
            demand.items,
            stock,
            on_order,
            subtract_on_order=request.get("subtract_on_order", True),
        )

        offset = request.get("offset", 0)
        page_length = request.get("page_length", DEFAULT_PAGE_LENGTH)
        page = gap.needing[offset : offset + page_length]
        cls._attach_item_names(page, limits, user, warnings)

        if gap.without_bin_record:
            warnings.append(
                f"{gap.without_bin_record} pending items have no Bin record and are counted "
                "with stock 0 (flag has_bin_record is false)."
            )
        if demand.lines_without_item:
            warnings.append(
                f"{demand.lines_without_item} order lines have no item_code and were skipped."
            )
        if any(item.get("uom_converted") for item in page):
            warnings.append(
                "Some quantities were converted to the stock unit with conversion_factor "
                "(flag uom_converted)."
            )

        scope: dict[str, Any] = {
            "selection": selection,
            "warehouse": request.get("warehouse"),
            "subtract_on_order": request.get("subtract_on_order", True),
        }
        if offset == 0:
            scope["order_names"] = order_names

        payload: dict[str, Any] = {
            "complete": True,
            "formula": gap.formula,
            "scope": scope,
            "counts": {
                "orders_considered": len(order_names) - unreadable,
                "orders_unreadable": unreadable,
                "order_lines_read": demand.lines_read,
                "lines_with_pending": demand.lines_with_pending,
                "distinct_items_pending": gap.pending_item_count,
                "items_needing_procurement": len(gap.needing),
                "items_fully_covered": gap.covered_count,
                "items_without_bin_record": gap.without_bin_record,
            },
            "offset": offset,
            "page_length": page_length,
            "next_offset": offset + len(page) if offset + len(page) < len(gap.needing) else None,
            "items": page,
            "warnings": warnings,
        }
        return build_response(
            RESULT_TYPE,
            doctype=ORDER_DOCTYPE,
            returned_count=len(page),
            has_more=payload["next_offset"] is not None,
            payload=payload,
        )

    # ------------------------------------------------------------------ orders
    @classmethod
    def _select_orders(
        cls,
        request: Mapping[str, Any],
        limits: Mapping[str, int],
        user: str | None,
    ) -> tuple[list[str], str]:
        names = request.get("order_names")
        if names is not None:
            return list(names), "named orders"

        count = request["order_count"]
        filters: list[dict[str, Any]] = [
            {"field": "docstatus", "operator": "=", "value": 1},
            {"field": "status", "operator": "in", "value": OPEN_STATUSES},
            {"field": "per_delivered", "operator": "<", "value": 100},
        ]
        if request.get("customer"):
            filters.append({"field": "customer", "operator": "=", "value": request["customer"]})
        if request.get("from_date"):
            filters.append({"field": "transaction_date", "operator": ">=", "value": request["from_date"]})
        if request.get("to_date"):
            filters.append({"field": "transaction_date", "operator": "<=", "value": request["to_date"]})

        found: list[str] = []
        offset = 0
        while len(found) < count:
            result = cls._execute(
                {
                    "doctype": ORDER_DOCTYPE,
                    "fields": ["name"],
                    "filters": filters,
                    "sort": [
                        {"field": "creation", "direction": "desc"},
                        {"field": "name", "direction": "desc"},
                    ],
                    "page_length": min(count - len(found), limits["page_length"]),
                    "offset": offset,
                },
                user,
            )
            rows = result.get("records") or []
            found.extend(row["name"] for row in rows)
            offset += len(rows)
            if not result.get("has_more") or not rows:
                break
        selection = f"the latest {count} open Sales Orders (submitted, status To Deliver or To Deliver and Bill, per_delivered below 100)"
        return found[:count], selection

    # ------------------------------------------------------------------- lines
    @classmethod
    def _read_lines(
        cls,
        order_names: list[str],
        limits: Mapping[str, int],
        user: str | None,
    ) -> tuple[list[Mapping[str, Any]], int]:
        """Read every Sales Order Item row of the orders. Returns the rows and the number
        of orders the user cannot read."""
        lines: list[Mapping[str, Any]] = []
        unreadable = 0
        for batch in gap_math.chunked(order_names, limits["parent_names"]):
            offset = 0
            while True:
                result = cls._execute(
                    {
                        "doctype": LINE_DOCTYPE,
                        "fields": LINE_FIELDS,
                        "page_length": limits["page_length"],
                        "offset": offset,
                        "parent_context": {"parent_doctype": ORDER_DOCTYPE, "parent_names": batch},
                    },
                    user,
                )
                rows = result.get("records") or []
                if offset == 0:
                    context = result.get("parent_context") or {}
                    unreadable += len(batch) - int(context.get("accessible_count", len(batch)))
                lines.extend(rows)
                if len(lines) > MAX_LINES:
                    frappe.throw(
                        _(
                            "The selected orders have more than {0} lines. Use fewer orders "
                            "(order_count) or split the request."
                        ).format(MAX_LINES),
                        frappe.ValidationError,
                    )
                offset += len(rows)
                if not result.get("has_more"):
                    break
                if not rows:
                    frappe.throw(_("A paged read returned no rows but reported more"), frappe.ValidationError)
        return lines, unreadable

    # --------------------------------------------------------------------- bin
    @classmethod
    def _read_bin(
        cls,
        item_codes: list[str],
        warehouse: str | None,
        limits: Mapping[str, int],
        user: str | None,
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        """Sum Bin ``actual_qty`` and ``ordered_qty`` per item. Items with no Bin row are absent."""
        stock: dict[str, Decimal] = {}
        on_order: dict[str, Decimal] = {}
        if not item_codes:
            return stock, on_order

        size = gap_math.bin_chunk_size(
            complexity_limit=limits["aggregate_complexity"],
            filter_values_limit=limits["filter_values"],
            group_limit=limits["returned_groups"],
            has_warehouse_filter=bool(warehouse),
        )
        if size < 1:
            frappe.throw(
                _("The site's aggregation limits are too small to read stock for this request"),
                frappe.ValidationError,
            )

        for chunk in gap_math.chunked(item_codes, size):
            filters: list[dict[str, Any]] = [{"field": "item_code", "operator": "in", "value": chunk}]
            if warehouse:
                filters.append({"field": "warehouse", "operator": "=", "value": warehouse})
            result = cls._execute(
                {
                    "doctype": "Bin",
                    "aggregations": [
                        {"operation": "sum", "field": "actual_qty"},
                        {"operation": "sum", "field": "ordered_qty"},
                    ],
                    "group_by": ["item_code"],
                    "filters": filters,
                },
                user,
            )
            if result.get("truncated") is not False:
                frappe.throw(_("Stock aggregation was incomplete"), frappe.ValidationError)
            columns = {
                entry["field"]: entry["result_field"]
                for entry in result.get("aggregations", [])
                if entry.get("operation") == "sum"
            }
            if set(columns) != {"actual_qty", "ordered_qty"}:
                frappe.throw(_("Stock aggregation returned unexpected columns"), frappe.ValidationError)
            for row in result.get("results", []):
                code = row.get("item_code")
                if not isinstance(code, str):
                    frappe.throw(_("Stock aggregation returned a row without item_code"), frappe.ValidationError)
                stock[code] = stock.get(code, gap_math.ZERO) + gap_math.to_decimal(
                    row.get(columns["actual_qty"]), f"stock of {code}"
                )
                on_order[code] = on_order.get(code, gap_math.ZERO) + gap_math.to_decimal(
                    row.get(columns["ordered_qty"]), f"on-order quantity of {code}"
                )
        return stock, on_order

    # ------------------------------------------------------------------- names
    @classmethod
    def _attach_item_names(
        cls,
        page: list[dict[str, Any]],
        limits: Mapping[str, int],
        user: str | None,
        warnings: list[str],
    ) -> None:
        """Add ``item_name`` to the returned page. A failure leaves it null and says so."""
        for item in page:
            item["item_name"] = None
        codes = [item["item_code"] for item in page]
        if not codes:
            return
        try:
            names: dict[str, Any] = {}
            for chunk in gap_math.chunked(codes, min(limits["filter_values"], limits["page_length"])):
                result = cls._execute(
                    {
                        "doctype": "Item",
                        "fields": ["name", "item_name"],
                        "filters": [{"field": "name", "operator": "in", "value": chunk}],
                        "page_length": len(chunk),
                    },
                    user,
                )
                for row in result.get("records") or []:
                    names[row["name"]] = row.get("item_name")
            for item in page:
                item["item_name"] = names.get(item["item_code"])
        except frappe.PermissionError:
            warnings.append("Item names could not be read for the current user (item_name is null).")

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _execute(request: dict[str, Any], user: str | None) -> Mapping[str, Any]:
        """Run one read through IntelligenceService and return its ``execution_result``."""
        response = IntelligenceService.execute_request(request, user=user)
        if not isinstance(response, Mapping) or response.get("status") != "executed":
            frappe.throw(
                _("An internal read for {0} could not be planned: {1}").format(
                    request.get("doctype"),
                    str((response or {}).get("unresolved_references") or (response or {}).get("status"))[:300],
                ),
                frappe.ValidationError,
            )
        result = response.get("execution_result")
        if not isinstance(result, Mapping):
            frappe.throw(_("An internal read returned no result"), frappe.ValidationError)
        return result
