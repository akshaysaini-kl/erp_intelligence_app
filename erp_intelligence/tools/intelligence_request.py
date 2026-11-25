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
            "parent_context",
            "parent_scope",
            "ranking",
        }
    )
    _ALLOWED_ARGUMENTS = _REQUEST_ARGUMENTS | {"user"}
    _MAX_REFERENCE_LENGTH = 256
    _MAX_PARENT_NAME_LENGTH = 140
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
            "group_by), not both. To read child-table rows, such as the items "
            "of specific Sales Orders, set doctype to the child DocType (for "
            "example 'Sales Order Item') and supply parent_context with the "
            "parent DocType and the exact parent document names; record and "
            "aggregation requests accept parent_context, and rows are used only "
            "for parents the user may read. To total child rows over named "
            "parents, such as the quantity per item over specific Sales Orders, "
            "send an aggregation request with the child DocType, "
            "aggregations [{operation: 'sum', field: 'qty'}], group_by "
            "['item_code'] and parent_context; the result has one row per group "
            "and parent_context.accessible_count says how many parents were "
            "readable. To rank parent documents by an aggregate of "
            "their child rows, such as the Purchase Order with the most item rows, "
            "set doctype to the child DocType (for example 'Purchase Order Item'), "
            "supply parent_scope with the parent DocType and optional parent_filters "
            "(for example docstatus in [0, 1] to exclude cancelled documents) or "
            "parent_names, aggregations [{operation: 'count', field: 'name'}] (count "
            "name counts actual rows), group_by ['parent'], and ranking {top_n, "
            "include_ties, by}. Ranking is descending only. Do not send fields, sort, "
            "page_length, offset, filters or parent_context with parent_scope. The "
            "scope is limited to records the user may read: say so in the answer. "
            "No child rows are returned. Read the result's status before using it: "
            "'executed' holds the ranking in execution_result.results, with "
            "complete true. status 'failed' (result_type 'intelligence_execution_failure') "
            "means no ranking was computed because a limit was reached: it carries "
            "error_code, limit and guidance. Never present a failed result as an "
            "answer, never guess a ranking from it, and do not repeat the same request. "
            "Narrow parent_filters or parent_names as the guidance says, or, for "
            "child_aggregation_tie_cap_exceeded, retry with include_ties false. "
            "The result is one of: executed records or "
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
                "parent_context": {
                    "type": "object",
                    "properties": {
                        "parent_doctype": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": self._MAX_REFERENCE_LENGTH,
                            "description": "DocType that owns the child table, e.g. Sales Order.",
                        },
                        "parent_names": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": self._MAX_PARENT_NAME_LENGTH,
                            },
                            "description": (
                                "Exact names of the parent documents whose child "
                                "rows are wanted."
                            ),
                        },
                    },
                    "required": ["parent_doctype", "parent_names"],
                    "additionalProperties": False,
                    "description": (
                        "Required when doctype is a child table. Child rows are "
                        "returned only for the named parents the user can read. "
                        "Record requests only."
                    ),
                },
                "parent_scope": {
                    "type": "object",
                    "properties": {
                        "parent_doctype": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": self._MAX_REFERENCE_LENGTH,
                            "description": "DocType that owns the child table, e.g. Purchase Order.",
                        },
                        "parent_filters": {
                            "type": "array",
                            "items": self._parent_filter_schema(),
                            "description": (
                                "Filters on the PARENT DocType, for example "
                                "docstatus in [0, 1]. They only narrow the scope."
                            ),
                        },
                        "parent_names": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": self._MAX_PARENT_NAME_LENGTH,
                            },
                            "description": (
                                "Optional exact parent names to compare. They only "
                                "narrow the scope and never grant access."
                            ),
                        },
                    },
                    "required": ["parent_doctype"],
                    "additionalProperties": False,
                    "description": (
                        "Required for child-table aggregation, and only valid with "
                        "aggregations, group_by ['parent'] and ranking."
                    ),
                },
                "ranking": {
                    "type": "object",
                    "properties": {
                        "by": {
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
                            "description": (
                                "The aggregation to rank by. Required when there is "
                                "more than one aggregation."
                            ),
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["desc"],
                            "description": "Only descending ranking is supported.",
                        },
                        "top_n": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "How many parents to return, subject to the configured limit.",
                        },
                        "include_ties": {
                            "type": "boolean",
                            "description": (
                                "true (default) returns every parent tied at the "
                                "boundary. false returns exactly top_n rows and "
                                "reports whether a tie was cut."
                            ),
                        },
                    },
                    "required": ["top_n"],
                    "additionalProperties": False,
                    "description": "Required with parent_scope.",
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
    def _parent_filter_schema(cls) -> dict[str, Any]:
        """One parent filter. Same shape as an ordinary filter."""
        return {
            "type": "object",
            "properties": {
                "field": cls._reference_schema(),
                "operator": {
                    "type": "string",
                    "enum": list(SUPPORTED_QUERY_FILTER_OPERATORS),
                },
                "value": {},
            },
            "required": ["field", "operator", "value"],
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

        parent_context = request.get("parent_context")
        if isinstance(parent_context, Mapping):
            # Parent document names may legitimately contain dots, so only the
            # DocType reference is checked here; names are validated by QueryValidator.
            cls._validate_reference(
                parent_context.get("parent_doctype"),
                "parent_context.parent_doctype",
            )

        scope = request.get("parent_scope")
        if isinstance(scope, Mapping):
            # Parent names may contain dots, so only DocType and field references are checked.
            cls._validate_reference(scope.get("parent_doctype"), "parent_scope.parent_doctype")
            filters = scope.get("parent_filters")
            if isinstance(filters, (list, tuple)):
                for entry in filters:
                    if isinstance(entry, Mapping):
                        cls._validate_reference(entry.get("field"), "parent_scope.parent_filters.field")
        ranking = request.get("ranking")
        if isinstance(ranking, Mapping) and isinstance(ranking.get("by"), Mapping):
            cls._validate_reference(ranking["by"].get("field"), "ranking.by.field")

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
