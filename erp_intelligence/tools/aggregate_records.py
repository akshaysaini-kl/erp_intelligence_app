from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _
from frappe_assistant_core.core.base_tool import BaseTool

from erp_intelligence.services.aggregate_service import AggregateService


class AggregateRecords(BaseTool):
    """Expose permission-aware database aggregations through FAC."""

    _REQUEST_ARGUMENTS = frozenset(
        {"doctype", "aggregations", "group_by", "filters"}
    )
    _ALLOWED_ARGUMENTS = _REQUEST_ARGUMENTS | {"user"}
    _FIELDNAME_PATTERN = "^[A-Za-z_][A-Za-z0-9_]*$"
    _AGGREGATION_OPERATIONS = ["count", "sum", "avg", "min", "max"]
    _FILTER_OPERATORS = [
        "=",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
        "in",
        "not in",
        "between",
        "like",
        "not like",
        "is",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.name = "aggregate_records"
        self.description = (
            "Calculate permission-aware COUNT, SUM, AVG, MIN, and MAX values "
            "with optional validated filters and grouping for a Frappe DocType."
        )
        self.category = "ERP Intelligence"
        self.source_app = "erp_intelligence"
        self.requires_permission = None
        self.inputSchema = {
            "type": "object",
            "properties": {
                "doctype": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact name of the Frappe DocType to aggregate.",
                },
                "aggregations": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "operation": {
                                "type": "string",
                                "enum": self._AGGREGATION_OPERATIONS,
                            },
                            "field": {
                                "type": "string",
                                "pattern": self._FIELDNAME_PATTERN,
                            },
                        },
                        "required": ["operation", "field"],
                        "additionalProperties": False,
                    },
                    "description": "Controlled database aggregation operations.",
                },
                "group_by": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "pattern": self._FIELDNAME_PATTERN,
                    },
                    "description": "Queryable fields used to group aggregation results.",
                },
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "pattern": self._FIELDNAME_PATTERN,
                            },
                            "operator": {
                                "type": "string",
                                "enum": self._FILTER_OPERATORS,
                            },
                            "value": {},
                        },
                        "required": ["field", "operator", "value"],
                        "additionalProperties": False,
                    },
                    "description": "Structured filters validated by field type.",
                },
                "user": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional assertion of the authenticated Frappe user. "
                        "It must match the current session user."
                    ),
                },
            },
            "required": ["doctype", "aggregations"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return AggregateService's permission-aware response directly."""
        if not isinstance(arguments, Mapping):
            frappe.throw(_("Tool arguments must be a mapping"), frappe.ValidationError)

        unknown_arguments = set(arguments) - self._ALLOWED_ARGUMENTS
        if unknown_arguments:
            frappe.throw(
                _("Unsupported arguments: {0}").format(
                    ", ".join(sorted(str(key) for key in unknown_arguments))
                ),
                frappe.ValidationError,
            )

        request = {
            key: arguments[key]
            for key in self._REQUEST_ARGUMENTS
            if key in arguments
        }
        return AggregateService.aggregate_records(request, user=arguments.get("user"))
