from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _
from frappe_assistant_core.core.base_tool import BaseTool

from erp_intelligence.services.intelligence_service import IntelligenceService
from erp_intelligence.utils.constants import (
    SORT_DIRECTIONS,
    SUPPORTED_AGGREGATION_OPERATIONS,
    SUPPORTED_QUERY_FILTER_OPERATORS,
)


class IntelligenceRequest(BaseTool):
    """Expose IntelligenceService.execute_request() as the agent entry point."""

    _REQUEST_ARGUMENTS = frozenset(
        {
            "doctype",
            "fields",
            "filters",
            "sort",
            "page_length",
            "offset",
            "aggregations",
            "group_by",
        }
    )
    _ALLOWED_ARGUMENTS = _REQUEST_ARGUMENTS | {"user"}
    _MAX_REFERENCE_LENGTH = 256
    _REFERENCE_FORBIDDEN_CHARACTERS = frozenset({".", "`"})

    def __init__(self) -> None:
        super().__init__()
        self.name = "intelligence_request"
        self.description = (
            "Retrieve or aggregate ERP records with the current user's native "
            "Frappe permissions. Use this tool for ordinary ERP record "
            "retrieval and aggregation questions, and prefer it over "
            "query_records and aggregate_records. Supply structured "
            "references: a DocType (canonical name or label), field names or "
            "labels, filters as {field, operator, value}, sort entries as "
            "{field, direction}, and aggregations as {operation, field} with "
            "optional group_by. Send either a record request (fields, sort, "
            "page_length, offset) or an aggregation request (aggregations, "
            "group_by), not both. The result is one of: executed records or "
            "aggregation values (result_type 'intelligence_execution'); a "
            "'query_plan' with status 'needs_clarification' listing "
            "ambiguous references and candidate choices; a 'query_plan' with "
            "status 'unresolved' listing references that could not be "
            "matched; or an error. When clarification is required, choose "
            "from the returned candidates or ask the user, then retry with "
            "the resolved canonical names. Never invent fieldnames, filter "
            "values, or aggregation results, and never retry an unchanged "
            "ambiguous or unresolved request."
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
                    "maxLength": self._MAX_REFERENCE_LENGTH,
                    "description": "Canonical DocType name or a resolvable label.",
                },
                "fields": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": self._reference_schema(),
                    "description": (
                        "Fieldnames or labels to return for a record request. "
                        "Do not combine with aggregations."
                    ),
                },
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": self._reference_schema(),
                            "operator": {
                                "type": "string",
                                "enum": list(SUPPORTED_QUERY_FILTER_OPERATORS),
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
                            "field": self._reference_schema(),
                            "direction": {
                                "type": "string",
                                "enum": list(SORT_DIRECTIONS),
                            },
                        },
                        "required": ["field", "direction"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "Sort order for a record request. "
                        "Do not combine with aggregations."
                    ),
                },
                "page_length": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Bounded number of records for a record request. "
                        "Do not combine with aggregations."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Zero-based offset for a record request. "
                        "Do not combine with aggregations."
                    ),
                },
                "aggregations": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "operation": {
                                "type": "string",
                                "enum": list(SUPPORTED_AGGREGATION_OPERATIONS),
                            },
                            "field": self._reference_schema(),
                        },
                        "required": ["operation", "field"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "Aggregations to calculate. Makes this an aggregation "
                        "request."
                    ),
                },
                "group_by": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": self._reference_schema(),
                    "description": (
                        "Fieldnames or labels to group by. Requires "
                        "aggregations."
                    ),
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
            "required": ["doctype"],
            "additionalProperties": False,
        }

    @classmethod
    def _reference_schema(cls) -> dict[str, Any]:
        return {
            "type": "string",
            "minLength": 1,
            "maxLength": cls._MAX_REFERENCE_LENGTH,
            "description": "Fieldname or resolvable label. No dotted paths.",
        }

    @classmethod
    def _validate_reference(cls, value: Any, name: str) -> None:
        """Reject dotted paths, backticks and control characters in references.

        Non-string values are left for the planner to reject.
        """
        if not isinstance(value, str):
            return
        if any(
            character in cls._REFERENCE_FORBIDDEN_CHARACTERS
            or not character.isprintable()
            for character in value
        ):
            frappe.throw(
                _("{0} contains unsupported characters").format(name),
                frappe.ValidationError,
            )

    @classmethod
    def _validate_references(cls, request: Mapping[str, Any]) -> None:
        cls._validate_reference(request.get("doctype"), "doctype")

        for key in ("fields", "group_by"):
            values = request.get(key)
            if isinstance(values, (list, tuple)):
                for value in values:
                    cls._validate_reference(value, key)

        for key in ("filters", "sort", "aggregations"):
            entries = request.get(key)
            if isinstance(entries, (list, tuple)):
                for entry in entries:
                    if isinstance(entry, Mapping):
                        cls._validate_reference(entry.get("field"), f"{key}.field")

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return IntelligenceService's response unchanged."""
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
        self._validate_references(request)
        return IntelligenceService.execute_request(
            request, user=arguments.get("user")
        )
