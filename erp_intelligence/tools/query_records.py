from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _
from frappe_assistant_core.core.base_tool import BaseTool

from erp_intelligence.services.query_service import QueryService


class QueryRecords(BaseTool):
    """Expose permission-aware record queries through FAC."""

    _REQUEST_ARGUMENTS = frozenset(
        {"doctype", "fields", "filters", "sort", "page_length", "offset"}
    )
    _ALLOWED_ARGUMENTS = _REQUEST_ARGUMENTS | {"user"}
    _FIELDNAME_PATTERN = "^[A-Za-z_][A-Za-z0-9_]*$"
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
        self.name = "query_records"
        self.description = (
            "Query selected fields from a Frappe DocType using strict filters, "
            "bounded pagination, and the current user's native Frappe permissions."
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
                    "description": "Exact name of the Frappe DocType to query.",
                },
                "fields": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "pattern": self._FIELDNAME_PATTERN,
                    },
                    "description": "Readable, queryable fieldnames to return.",
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
                "sort": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "pattern": self._FIELDNAME_PATTERN,
                            },
                            "direction": {
                                "type": "string",
                                "enum": ["asc", "desc"],
                            },
                        },
                        "required": ["field", "direction"],
                        "additionalProperties": False,
                    },
                    "description": "Validated field sorting in ascending or descending order.",
                },
                "page_length": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Bounded number of records requested for this page.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Zero-based offset into the permitted result set.",
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
            "required": ["doctype", "fields"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return QueryService's permission-aware response directly."""
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
        return QueryService.query_records(request, user=arguments.get("user"))
