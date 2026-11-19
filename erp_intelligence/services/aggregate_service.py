"""Permission-aware database aggregation service."""

import re
from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _

from erp_intelligence.services.config_service import ConfigService
from erp_intelligence.services.metadata_service import MetadataService
from erp_intelligence.services.query_validator import QueryValidator
from erp_intelligence.utils.response_utils import build_collection_response


class AggregateService:
    """Execute validated database aggregations for the current Frappe user."""

    # Limits are resolved by ConfigService; these names are shared with QueryPlanner.
    _LIMIT_NAMES = (
        "aggregations",
        "group_by_fields",
        "returned_groups",
        "aggregate_complexity",
    )

    _ALLOWED_REQUEST_KEYS = frozenset(
        {
            "doctype",
            "aggregations",
            "group_by",
            "filters",
            "parent_doctype",
            "parent_fieldname",
        }
    )
    _ALLOWED_AGGREGATION_KEYS = frozenset({"operation", "field"})
    _SUPPORTED_OPERATIONS = frozenset({"count", "sum", "avg", "min", "max"})
    _NUMERIC_FIELDTYPES = frozenset(
        {
            "Check",
            "Currency",
            "Duration",
            "Float",
            "Int",
            "Long Int",
            "Percent",
            "Rating",
        }
    )
    _FIELDNAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    @classmethod
    def aggregate_records(
        cls,
        request: Mapping[str, Any],
        user: str | None = None,
        limit_snapshot: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Validate and execute one bounded, permission-aware aggregation.

        ``limit_snapshot`` is the request-scoped snapshot supplied by
        IntelligenceService. A direct call takes its own snapshot here, so the
        aggregation limits and the QueryValidator limits used below agree.
        """
        cls._assert_authenticated_user(user)
        cls._validate_request_shape(request)
        if limit_snapshot is None:
            limit_snapshot = ConfigService.get_limit_snapshot()
        limits = cls._get_limits(limit_snapshot)

        aggregations = cls._normalize_aggregations(
            request.get("aggregations"),
            limits["aggregations"],
        )
        group_by = cls._normalize_group_by(
            request.get("group_by", []),
            limits["group_by_fields"],
        )
        cls._validate_result_field_names(aggregations, group_by)

        validation_request = cls._build_validation_request(
            request,
            aggregations,
            group_by,
        )
        plan = QueryValidator.validate_query(
            validation_request,
            user=user,
            limit_snapshot=limit_snapshot,
        )

        meta = frappe.get_meta(plan["doctype"])
        cls._validate_execution_target(meta, plan)
        cls._validate_aggregation_types(meta, aggregations)
        cls.enforce_complexity_limit(
            aggregations,
            group_by,
            plan["filters"],
            limits["aggregate_complexity"],
        )

        query_options: dict[str, Any] = {
            "fields": cls._build_query_fields(aggregations, group_by),
            "filters": cls._build_filters(plan["doctype"], plan["filters"]),
            "order_by": cls._build_order_by(group_by),
            "limit_start": 0,
            "limit_page_length": limits["returned_groups"] + 1 if group_by else 2,
            "as_list": False,
            "strict": True,
        }
        if group_by:
            query_options["group_by"] = cls._build_group_by(group_by)

        # get_list applies Frappe's current-session record permission conditions
        # before the database performs grouping and aggregate calculations.
        rows = frappe.get_list(plan["doctype"], **query_options)
        results = cls._normalize_results(
            rows,
            aggregations,
            group_by,
            limits["returned_groups"],
        )

        # The query fetched one row beyond the group limit and _normalize_results
        # raises if that row exists, so any response built here contains every
        # group. truncated=False is therefore an established fact, not a default.
        try:
            return build_collection_response(
                "aggregation",
                results,
                item_key="results",
                doctype=plan["doctype"],
                truncated=False,
                extra={
                    "group_by": group_by,
                    "aggregations": aggregations,
                    "group_limit": limits["returned_groups"] if group_by else 1,
                },
            )
        except (TypeError, ValueError):
            frappe.throw(
                _("Aggregation returned a value that cannot be represented as JSON"),
                frappe.ValidationError,
            )

    @staticmethod
    def _assert_authenticated_user(user: str | None) -> str:
        session_user = getattr(getattr(frappe, "session", None), "user", None)
        if not isinstance(session_user, str) or not session_user:
            frappe.throw(_("An authenticated Frappe user is required"), frappe.PermissionError)

        if user is not None:
            if not isinstance(user, str):
                frappe.throw(_("User must be a string"), frappe.ValidationError)
            if user != session_user:
                frappe.throw(
                    _("Aggregations can only be evaluated for the current session user"),
                    frappe.PermissionError,
                )

        return session_user

    @classmethod
    def _validate_request_shape(cls, request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping):
            frappe.throw(_("Aggregation request must be a mapping"), frappe.ValidationError)

        unknown_keys = set(request) - cls._ALLOWED_REQUEST_KEYS
        if unknown_keys:
            frappe.throw(
                _("Unsupported aggregation request keys: {0}").format(
                    ", ".join(sorted(str(key) for key in unknown_keys))
                ),
                frappe.ValidationError,
            )

        if "doctype" not in request:
            frappe.throw(_("Aggregation request requires a doctype"), frappe.ValidationError)
        if "aggregations" not in request:
            frappe.throw(_("Aggregation request requires aggregations"), frappe.ValidationError)

    @classmethod
    def _normalize_aggregations(
        cls,
        aggregations: Any,
        maximum: int,
    ) -> list[dict[str, str]]:
        if not isinstance(aggregations, list) or not aggregations:
            frappe.throw(
                _("aggregations must be a non-empty list"),
                frappe.ValidationError,
            )
        if len(aggregations) > maximum:
            frappe.throw(
                _("aggregations cannot contain more than {0} entries").format(maximum),
                frappe.ValidationError,
            )

        normalized = []
        seen = set()
        for index, aggregation in enumerate(aggregations, start=1):
            if not isinstance(aggregation, Mapping):
                frappe.throw(
                    _("Each aggregation must be a mapping"),
                    frappe.ValidationError,
                )
            if set(aggregation) != cls._ALLOWED_AGGREGATION_KEYS:
                frappe.throw(
                    _("Each aggregation must contain only operation and field"),
                    frappe.ValidationError,
                )

            operation = aggregation.get("operation")
            if not isinstance(operation, str):
                frappe.throw(
                    _("Aggregation operation must be a string"),
                    frappe.ValidationError,
                )
            operation = operation.lower()
            if operation not in cls._SUPPORTED_OPERATIONS:
                frappe.throw(
                    _("Unsupported aggregation operation: {0}").format(operation),
                    frappe.ValidationError,
                )

            fieldname = cls._validate_field_identifier(aggregation.get("field"))
            signature = (operation, fieldname)
            if signature in seen:
                frappe.throw(
                    _("Duplicate aggregation operation for field: {0}").format(fieldname),
                    frappe.ValidationError,
                )

            seen.add(signature)
            normalized_aggregation = {
                "operation": operation,
                "field": fieldname,
                "result_field": f"aggregation_{index}",
            }
            if operation == "count":
                # `name` is the non-null primary key, so COUNT(name) is the number of
                # permitted rows. COUNT(other_field) counts only non-null values.
                normalized_aggregation["count_basis"] = (
                    "rows" if fieldname == "name" else "non_null_values"
                )
            normalized.append(normalized_aggregation)

        return normalized

    @classmethod
    def _normalize_group_by(cls, group_by: Any, maximum: int) -> list[str]:
        if group_by is None:
            group_by = []
        if not isinstance(group_by, list):
            frappe.throw(_("group_by must be a list"), frappe.ValidationError)
        if len(group_by) > maximum:
            frappe.throw(
                _("group_by cannot contain more than {0} fields").format(maximum),
                frappe.ValidationError,
            )

        normalized = []
        seen = set()
        for fieldname in group_by:
            fieldname = cls._validate_field_identifier(fieldname)
            if fieldname in seen:
                frappe.throw(
                    _("Duplicate group_by field: {0}").format(fieldname),
                    frappe.ValidationError,
                )
            seen.add(fieldname)
            normalized.append(fieldname)

        return normalized

    @staticmethod
    def _validate_result_field_names(
        aggregations: list[Mapping[str, str]],
        group_by: list[str],
    ) -> None:
        group_fields = set(group_by)
        if any(aggregation["result_field"] in group_fields for aggregation in aggregations):
            frappe.throw(
                _("A group_by field conflicts with an aggregation result field"),
                frappe.ValidationError,
            )

    @staticmethod
    def _build_validation_request(
        request: Mapping[str, Any],
        aggregations: list[Mapping[str, str]],
        group_by: list[str],
    ) -> dict[str, Any]:
        referenced_fields = []
        seen = set()
        for fieldname in [
            *group_by,
            *(aggregation["field"] for aggregation in aggregations),
        ]:
            if fieldname not in seen:
                seen.add(fieldname)
                referenced_fields.append(fieldname)

        return {
            "doctype": request.get("doctype"),
            "fields": referenced_fields,
            "filters": request.get("filters", []),
            "sort": [],
            "page_length": 1,
            "offset": 0,
            "parent_doctype": request.get("parent_doctype"),
            "parent_fieldname": request.get("parent_fieldname"),
        }

    @staticmethod
    def _validate_execution_target(meta: Any, plan: Mapping[str, Any]) -> None:
        if getattr(meta, "name", None) != plan["doctype"]:
            frappe.throw(
                _("DocType metadata changed during aggregation validation"),
                frappe.ValidationError,
            )

        if bool(getattr(meta, "istable", False)):
            frappe.throw(
                _(
                    "Direct child DocType aggregation cannot enforce parent record permissions safely"
                ),
                frappe.PermissionError,
            )

        if plan.get("parent_doctype") is not None or plan.get("parent_fieldname") is not None:
            frappe.throw(
                _("Parent context is not valid for this aggregation"),
                frappe.ValidationError,
            )

        if bool(getattr(meta, "issingle", False)):
            frappe.throw(
                _("Single DocTypes are not supported by aggregation"),
                frappe.ValidationError,
            )

        if bool(getattr(meta, "is_virtual", False)):
            frappe.throw(
                _("Virtual DocTypes are not supported by aggregation"),
                frappe.ValidationError,
            )

    @classmethod
    def _validate_aggregation_types(
        cls,
        meta: Any,
        aggregations: list[Mapping[str, str]],
    ) -> None:
        for aggregation in aggregations:
            field = MetadataService.get_live_field(meta, aggregation["field"])
            if field is None:
                frappe.throw(
                    _("Aggregation field is no longer available"),
                    frappe.ValidationError,
                )

            if (
                aggregation["operation"] in {"sum", "avg"}
                and getattr(field, "fieldtype", None) not in cls._NUMERIC_FIELDTYPES
            ):
                frappe.throw(
                    _("{0} requires a numeric field").format(
                        aggregation["operation"].upper()
                    ),
                    frappe.ValidationError,
                )

    # Public aliases for reuse by other aggregation executors (child-table
    # aggregation). They delegate to the private implementations above and below
    # without changing behavior, so existing callers and tests are unaffected.
    @classmethod
    def normalize_aggregations(
        cls,
        aggregations: Any,
        maximum: int,
    ) -> list[dict[str, str]]:
        """Validate aggregation entries and assign ``result_field`` and ``count_basis``."""
        return cls._normalize_aggregations(aggregations, maximum)

    @classmethod
    def validate_aggregation_types(
        cls,
        meta: Any,
        aggregations: list[Mapping[str, str]],
    ) -> None:
        """Require live fields, and numeric fields for sum and avg."""
        cls._validate_aggregation_types(meta, aggregations)

    @classmethod
    def build_select_fields(
        cls,
        aggregations: list[Mapping[str, str]],
        group_by: list[str],
    ) -> list[str]:
        """Return the ``get_list`` field expressions for the group-by and aggregates."""
        return cls._build_query_fields(aggregations, group_by)

    @staticmethod
    def calculate_complexity(
        aggregations: list[Mapping[str, Any]],
        group_by: list[str],
        filters: list[Mapping[str, Any]],
    ) -> int:
        """Return the aggregate complexity score used by planning and execution.

        This is the single definition of the formula:
        aggregations * 4 + group_by fields * 8 + filters * 3 + filter values.
        A list filter value counts once per item and any other value counts once.
        """
        filter_value_count = sum(
            len(item["value"]) if isinstance(item["value"], list) else 1
            for item in filters
        )
        return (
            len(aggregations) * 4
            + len(group_by) * 8
            + len(filters) * 3
            + filter_value_count
        )

    @classmethod
    def enforce_complexity_limit(
        cls,
        aggregations: list[Mapping[str, Any]],
        group_by: list[str],
        filters: list[Mapping[str, Any]],
        maximum: int,
    ) -> int:
        """Return the complexity score, or raise when it exceeds ``maximum``.

        ``maximum`` is the effective ``aggregate_complexity`` limit resolved by
        ConfigService.
        """
        complexity = cls.calculate_complexity(aggregations, group_by, filters)
        if complexity > maximum:
            frappe.throw(
                _("Aggregation query complexity exceeds the configured maximum"),
                frappe.ValidationError,
            )
        return complexity

    @staticmethod
    def _build_query_fields(
        aggregations: list[Mapping[str, str]],
        group_by: list[str],
    ) -> list[str]:
        fields = list(group_by)
        fields.extend(
            "{operation}(`{field}`) AS `{result_field}`".format(
                operation=aggregation["operation"].upper(),
                field=aggregation["field"],
                result_field=aggregation["result_field"],
            )
            for aggregation in aggregations
        )
        return fields

    @staticmethod
    def _build_filters(
        doctype: str,
        filters: list[Mapping[str, Any]],
    ) -> list[list[Any]]:
        return [
            [doctype, item["field"], item["operator"], item["value"]]
            for item in filters
        ]

    @staticmethod
    def _build_group_by(group_by: list[str]) -> str:
        return ", ".join(f"`{fieldname}`" for fieldname in group_by)

    @staticmethod
    def _build_order_by(group_by: list[str]) -> str:
        return ", ".join(f"`{fieldname}` asc" for fieldname in group_by)

    @staticmethod
    def _normalize_results(
        rows: Any,
        aggregations: list[Mapping[str, str]],
        group_by: list[str],
        maximum_groups: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            frappe.throw(
                _("Aggregation returned an invalid result"),
                frappe.ValidationError,
            )

        if group_by and len(rows) > maximum_groups:
            # Partial grouped results are never returned. The caller must narrow
            # the request so that every group fits within the limit.
            frappe.throw(
                _(
                    "Aggregation produced more than {0} groups. Narrow the filters "
                    "or reduce group_by; partial grouped results are not returned."
                ).format(maximum_groups),
                frappe.ValidationError,
            )
        if not group_by and len(rows) > 1:
            frappe.throw(
                _("Ungrouped aggregation returned an invalid result"),
                frappe.ValidationError,
            )

        expected_fields = [
            *group_by,
            *(aggregation["result_field"] for aggregation in aggregations),
        ]
        results = []
        for row in rows:
            if not isinstance(row, Mapping):
                frappe.throw(
                    _("Aggregation returned an invalid row"),
                    frappe.ValidationError,
                )
            if any(fieldname not in row for fieldname in expected_fields):
                frappe.throw(
                    _("Aggregation fields are no longer permitted"),
                    frappe.PermissionError,
                )
            results.append({fieldname: row[fieldname] for fieldname in expected_fields})

        return results

    @classmethod
    def _get_limits(cls, limit_snapshot: Mapping[str, int] | None) -> dict[str, int]:
        return ConfigService.resolve_limits(*cls._LIMIT_NAMES, snapshot=limit_snapshot)

    @classmethod
    def _validate_field_identifier(cls, fieldname: Any) -> str:
        if not isinstance(fieldname, str) or not cls._FIELDNAME_PATTERN.fullmatch(fieldname):
            frappe.throw(_("Invalid aggregation field reference"), frappe.ValidationError)
        return fieldname
