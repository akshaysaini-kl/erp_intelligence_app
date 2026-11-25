"""MCP tool that finds what is still undelivered on Sales Orders and not covered by stock.

One call replaces the model's own multi-step work: select the orders, read every order
line, add up the pending quantity per item, read stock and open purchase quantity from
Bin, and return only the items that still need procurement. The arithmetic is done in
code, not by the model.

The tool only reads. It goes through ``IntelligenceService`` for every read, so it sees
exactly what the current user may see through ``intelligence_request``. It never writes.

It is off by default, with the same two layers as ``evaluate_business_rules`` (see
``_is_enabled`` and ``utils.fac_tool_configuration``).
"""
import datetime
import re
from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _
from frappe_assistant_core.core.base_tool import BaseTool

from erp_intelligence.services import fulfilment_gap_math as gap_math
from erp_intelligence.services.fulfilment_gap_service import (
    DEFAULT_ORDER_COUNT,
    DEFAULT_PAGE_LENGTH,
    MAX_ORDERS,
    MAX_PAGE_LENGTH,
    FulfilmentGapService,
)

TOOL_NAME = "fulfilment_gap"
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_NAME_LENGTH = 140

_DESCRIPTION = f"""Find which items on open Sales Orders are still undelivered and not covered by stock or open Purchase Orders, in ONE call. Read-only. The tool does all the arithmetic: never add up order lines or compare stock yourself when this tool is available.

WHEN TO CALL
Use it for questions like "which items from the last 50 sales orders are not fulfilled", "what do we need to buy for pending orders" or "net requirement for these orders". Call it first, before reading suppliers, prices or drafting purchase orders. It replaces reading Sales Order Item rows and Bin quantities.

INPUTS
- order_count: the latest N open Sales Orders (submitted, status To Deliver or To Deliver and Bill, per_delivered below 100), 1 to {MAX_ORDERS}. Default {DEFAULT_ORDER_COUNT} when order_names is not given.
- order_names: instead of order_count, an explicit list of Sales Order names (at most {MAX_ORDERS}). Give one of the two, not both.
- customer, from_date, to_date (YYYY-MM-DD): optional, narrow the order_count selection.
- warehouse: optional. Count stock and open purchase quantity of this warehouse only. Default is all warehouses.
- subtract_on_order: default true. Set false to ignore open Purchase Order quantity.
- page_length (default {DEFAULT_PAGE_LENGTH}, at most {MAX_PAGE_LENGTH}) and offset: page through items. Use next_offset from the previous response while it is not null.

HOW THE RESULT IS COMPUTED
pending = (qty - delivered_qty) * conversion_factor, added up per item over all selected order lines. stock = sum of Bin actual_qty. on_order = sum of Bin ordered_qty (open Purchase Orders not yet received). net_qty = max(0, pending - stock - on_order). reserved_qty is not subtracted. The formula is repeated in the response as formula. Items with net_qty 0 are not listed; they are only counted in items_fully_covered. An item is covered only when stock plus on_order is at least its pending quantity, so partly covered items are listed with the remaining net_qty.

READING THE RESULT
- counts shows the orders, lines and items behind the answer. Quote it when you report. items_needing_procurement is the full number of items; items holds one page of them.
- Each item has item_code, item_name, pending_qty, stock_qty, on_order_qty, net_qty and source_orders. Use net_qty as the quantity to buy. Do not recalculate it.
- has_bin_record false means the item has no stock record and was counted with stock 0. Say so to the user instead of calling it out of stock.
- uom_converted true means the quantities are in the stock unit, not the selling unit.
- warnings lists anything the user should know, for example orders that could not be read. Tell the user.
- complete is always true when a result is returned. If the tool cannot read everything it returns an error, never a partial figure. Do not retry the same request unchanged after an error; tell the user the reason.

WHAT IT DOES NOT DO
It does not find suppliers or prices, create or change any document, or check business rules. Use the other tools for those, starting from the items and net_qty this tool returns."""


class FulfilmentGap(BaseTool):
    """Net requirement of open Sales Orders against stock. Read-only."""

    _ARGUMENTS = frozenset(
        {
            "order_count",
            "order_names",
            "customer",
            "from_date",
            "to_date",
            "warehouse",
            "subtract_on_order",
            "page_length",
            "offset",
            "user",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self.name = TOOL_NAME
        self.description = _DESCRIPTION
        self.category = "ERP Intelligence"
        self.source_app = "erp_intelligence"
        self.requires_permission = None
        # Off unless the site turns it on. See _is_enabled.
        self.default_config = {"enabled": False}
        self.inputSchema = {
            "type": "object",
            "properties": {
                "order_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_ORDERS,
                    "description": "Use the latest N open Sales Orders.",
                },
                "order_names": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_ORDERS,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": _MAX_NAME_LENGTH},
                    "description": "Explicit Sales Order names. Use instead of order_count.",
                },
                "customer": {"type": "string", "minLength": 1, "maxLength": _MAX_NAME_LENGTH},
                "from_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                "to_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                "warehouse": {"type": "string", "minLength": 1, "maxLength": _MAX_NAME_LENGTH},
                "subtract_on_order": {"type": "boolean", "default": True},
                "page_length": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_PAGE_LENGTH,
                    "default": DEFAULT_PAGE_LENGTH,
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "user": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional assertion of the authenticated Frappe user. "
                        "It must match the current session user."
                    ),
                },
            },
            "additionalProperties": False,
        }

    # ---------------------------------------------------------------- execution
    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return the analysis, or raise. Never returns a partial figure."""
        if not self._is_enabled():
            frappe.throw(_("This tool is not enabled on this site."), frappe.PermissionError)
        request = self._parse(arguments)
        asserted = request.pop("user", None)
        if asserted is not None and asserted != frappe.session.user:
            frappe.throw(
                _("The user can only be asserted as the authenticated session user."),
                frappe.PermissionError,
            )
        if not frappe.has_permission("Sales Order", "read"):
            frappe.throw(_("You do not have permission to read Sales Orders."), frappe.PermissionError)
        try:
            return FulfilmentGapService.analyse(request, user=asserted)
        except gap_math.InvalidDataError as exc:
            frappe.throw(
                _("The data cannot be added up: {0}").format(str(exc)),
                frappe.ValidationError,
            )

    # ------------------------------------------------------------------ gating
    def _is_enabled(self) -> bool:
        """True only when the site explicitly enables the tool.

        Uses FAC's tool configuration hierarchy, so the switch is
        ``"assistant_tools": {"fulfilment_gap": {"enabled": true}}`` in the site
        configuration. Default off. Only the JSON value true enables it. Any problem
        reading the configuration leaves the tool off.
        """
        try:
            self.clear_config_cache()
            return self.get_config().get("enabled") is True
        except Exception:  # noqa: BLE001
            return False

    # ----------------------------------------------------------------- parsing
    @classmethod
    def _parse(cls, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            cls._invalid("Tool arguments must be an object")
        unknown = set(arguments) - cls._ARGUMENTS
        if unknown:
            cls._invalid("Unsupported arguments: {0}".format(", ".join(sorted(str(k) for k in unknown))))

        request: dict[str, Any] = {}
        has_count = arguments.get("order_count") is not None
        has_names = arguments.get("order_names") is not None
        if has_count and has_names:
            cls._invalid("Give either order_count or order_names, not both")
        if has_names:
            names = arguments["order_names"]
            if (
                not isinstance(names, list)
                or not 1 <= len(names) <= MAX_ORDERS
                or not all(isinstance(n, str) and 0 < len(n) <= _MAX_NAME_LENGTH for n in names)
            ):
                cls._invalid(f"order_names must be a list of 1 to {MAX_ORDERS} Sales Order names")
            if len(set(names)) != len(names):
                cls._invalid("order_names must not contain duplicates")
            request["order_names"] = list(names)
            for key in ("customer", "from_date", "to_date"):
                if arguments.get(key) is not None:
                    cls._invalid(f"{key} can only be used with order_count")
        else:
            count = arguments.get("order_count", DEFAULT_ORDER_COUNT)
            request["order_count"] = cls._integer(count, "order_count", 1, MAX_ORDERS)
            if arguments.get("customer") is not None:
                request["customer"] = cls._text(arguments["customer"], "customer")
            for key in ("from_date", "to_date"):
                if arguments.get(key) is not None:
                    request[key] = cls._date(arguments[key], key)
            if (
                "from_date" in request
                and "to_date" in request
                and request["from_date"] > request["to_date"]
            ):
                cls._invalid("from_date must not be after to_date")

        if arguments.get("warehouse") is not None:
            request["warehouse"] = cls._text(arguments["warehouse"], "warehouse")
        subtract = arguments.get("subtract_on_order", True)
        if not isinstance(subtract, bool):
            cls._invalid("subtract_on_order must be true or false")
        request["subtract_on_order"] = subtract
        request["page_length"] = cls._integer(
            arguments.get("page_length", DEFAULT_PAGE_LENGTH), "page_length", 1, MAX_PAGE_LENGTH
        )
        request["offset"] = cls._integer(arguments.get("offset", 0), "offset", 0, 1_000_000)
        if arguments.get("user") is not None:
            request["user"] = cls._text(arguments["user"], "user")
        return request

    @staticmethod
    def _invalid(message: str) -> None:
        frappe.throw(_(message), frappe.ValidationError)

    @classmethod
    def _integer(cls, value: Any, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            cls._invalid(f"{name} must be an integer from {minimum} to {maximum}")
        return value

    @classmethod
    def _text(cls, value: Any, name: str) -> str:
        if not isinstance(value, str) or not 0 < len(value) <= _MAX_NAME_LENGTH:
            cls._invalid(f"{name} must be a non-empty text of at most {_MAX_NAME_LENGTH} characters")
        return value

    @classmethod
    def _date(cls, value: Any, name: str) -> str:
        if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
            cls._invalid(f"{name} must be a date in YYYY-MM-DD format")
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            cls._invalid(f"{name} is not a valid date")
        return value

