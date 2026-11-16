"""Plan permission-aware ERP queries from structured interpretations."""

from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _

from erp_intelligence.services.aggregate_service import AggregateService
from erp_intelligence.services.config_service import ConfigService
from erp_intelligence.services.context_service import ContextService
from erp_intelligence.services.metadata_service import MetadataService
from erp_intelligence.services.query_validator import QueryValidator
from erp_intelligence.utils.constants import (
    AGGREGATION_FIELD_TYPES_BY_OPERATION,
    SUPPORTED_AGGREGATION_OPERATIONS,
)
from erp_intelligence.utils.response_utils import build_response


class QueryPlanner:
    """Resolve structured metadata references and return validated plans."""

    _ALLOWED_REQUEST_KEYS = frozenset(
        {
            "doctype",
            "fields",
            "filters",
            "sort",
            "page_length",
            "offset",
            "aggregations",
            "group_by",
            "user",
        }
    )
    _FILTER_KEYS = frozenset({"field", "operator", "value"})
    _SORT_KEYS = frozenset({"field", "direction"})
    _AGGREGATION_KEYS = frozenset({"operation", "field"})
    _RECORD_ONLY_KEYS = frozenset({"fields", "sort", "page_length", "offset"})
    _LIMIT_NAMES = (
        "fields",
        "filters",
        "sort_fields",
        "aggregations",
        "group_by_fields",
        "returned_groups",
        "aggregate_complexity",
    )

    @classmethod
    def plan_query(
        cls,
        request: Mapping[str, Any],
        user: str | None = None,
        limit_snapshot: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Resolve a structured interpretation into a non-executing query plan.

        ``limit_snapshot`` is the request-scoped snapshot supplied by
        IntelligenceService and reused for execution. A direct call takes its
        own snapshot, so planning and QueryValidator use the same limits.
        """
        cls._validate_request_mapping(request)
        authenticated_user = cls._get_authenticated_user(user, request.get("user"))
        plan_type = cls._get_plan_type(request)
        if limit_snapshot is None:
            limit_snapshot = ConfigService.get_limit_snapshot()
        limits = cls._get_limits(limit_snapshot)
        cls._validate_outer_structure(request, plan_type, limits)

        doctype_reference = request["doctype"]
        doctype_resolution = ContextService.resolve_doctype(
            doctype_reference,
            user=authenticated_user,
        )
        if doctype_resolution.get("match_status") != "resolved":
            issue = cls._resolution_issue(
                "/doctype",
                doctype_reference,
                doctype_resolution,
            )
            return cls._non_executable_response(
                plan_type,
                issues=[issue],
                resolutions=[],
            )

        doctype_value = doctype_resolution.get("resolved_value")
        if not isinstance(doctype_value, Mapping) or not isinstance(
            doctype_value.get("name"), str
        ):
            frappe.throw(
                _("Unable to establish canonical DocType context"),
                frappe.PermissionError,
            )
        doctype = doctype_value["name"]
        resolutions = [
            cls._resolution_record(
                "/doctype",
                doctype_reference,
                doctype,
                doctype_resolution,
            )
        ]

        cls._validate_execution_target(doctype)
        if plan_type == "records":
            return cls._plan_records(
                request,
                doctype,
                authenticated_user,
                resolutions,
                limit_snapshot,
            )
        return cls._plan_aggregation(
            request,
            doctype,
            authenticated_user,
            resolutions,
            limits,
            limit_snapshot,
        )

    @classmethod
    def _plan_records(
        cls,
        request: Mapping[str, Any],
        doctype: str,
        user: str,
        resolutions: list[dict[str, Any]],
        limit_snapshot: Mapping[str, int],
    ) -> dict[str, Any]:
        issues = []
        cache: dict[str, dict[str, Any]] = {}

        fields = cls._resolve_field_list(
            doctype,
            request["fields"],
            "/fields",
            user,
            cache,
            issues,
            resolutions,
        )
        filters = cls._resolve_filters(
            doctype,
            request.get("filters", []),
            user,
            cache,
            issues,
            resolutions,
        )
        sort = cls._resolve_sort(
            doctype,
            request.get("sort", []),
            user,
            cache,
            issues,
            resolutions,
        )

        if issues:
            return cls._non_executable_response(
                "records",
                issues=issues,
                resolutions=resolutions,
                doctype=doctype,
            )

        validation_request: dict[str, Any] = {
            "doctype": doctype,
            "fields": fields,
            "filters": filters,
            "sort": sort,
        }
        if "page_length" in request:
            validation_request["page_length"] = request["page_length"]
        if "offset" in request:
            validation_request["offset"] = request["offset"]

        validated = QueryValidator.validate_query(
            validation_request,
            user=user,
            limit_snapshot=limit_snapshot,
        )
        plan = {
            "doctype": validated["doctype"],
            "fields": validated["fields"],
            "filters": validated["filters"],
            "sort": validated["sort"],
            "page_length": validated["page_length"],
            "offset": validated["offset"],
        }
        return cls._ready_response("records", doctype, plan, resolutions)

    @classmethod
    def _plan_aggregation(
        cls,
        request: Mapping[str, Any],
        doctype: str,
        user: str,
        resolutions: list[dict[str, Any]],
        limits: Mapping[str, int],
        limit_snapshot: Mapping[str, int],
    ) -> dict[str, Any]:
        issues = []
        cache: dict[str, dict[str, Any]] = {}

        aggregations = []
        for index, aggregation in enumerate(request["aggregations"]):
            operation = aggregation["operation"].lower()
            fieldname = cls._resolve_field_reference(
                doctype,
                aggregation["field"],
                f"/aggregations/{index}/field",
                user,
                cache,
                issues,
                resolutions,
            )
            if fieldname is not None:
                aggregations.append({"operation": operation, "field": fieldname})

        group_by = cls._resolve_field_list(
            doctype,
            request.get("group_by", []),
            "/group_by",
            user,
            cache,
            issues,
            resolutions,
        )
        filters = cls._resolve_filters(
            doctype,
            request.get("filters", []),
            user,
            cache,
            issues,
            resolutions,
        )

        if issues:
            return cls._non_executable_response(
                "aggregation",
                issues=issues,
                resolutions=resolutions,
                doctype=doctype,
            )

        cls._validate_aggregation_duplicates(aggregations, group_by)
        referenced_fields = cls._unique_fields(
            [*group_by, *(item["field"] for item in aggregations)]
        )
        validated = QueryValidator.validate_query(
            {
                "doctype": doctype,
                "fields": referenced_fields,
                "filters": filters,
                "sort": [],
                "page_length": 1,
                "offset": 0,
            },
            user=user,
            limit_snapshot=limit_snapshot,
        )

        cls._validate_aggregation_field_types(doctype, aggregations)
        complexity = AggregateService.enforce_complexity_limit(
            aggregations,
            group_by,
            validated["filters"],
            limits["aggregate_complexity"],
        )

        plan = {
            "doctype": validated["doctype"],
            "aggregations": aggregations,
            "group_by": group_by,
            "filters": validated["filters"],
        }
        return cls._ready_response(
            "aggregation",
            doctype,
            plan,
            resolutions,
            planning_metadata={
                "complexity": complexity,
                "complexity_limit": limits["aggregate_complexity"],
                "group_limit": limits["returned_groups"],
            },
        )

    @classmethod
    def _resolve_field_list(
        cls,
        doctype: str,
        references: list[str],
        location_prefix: str,
        user: str,
        cache: dict[str, dict[str, Any]],
        issues: list[dict[str, Any]],
        resolutions: list[dict[str, Any]],
    ) -> list[str]:
        resolved = []
        for index, reference in enumerate(references):
            fieldname = cls._resolve_field_reference(
                doctype,
                reference,
                f"{location_prefix}/{index}",
                user,
                cache,
                issues,
                resolutions,
            )
            if fieldname is not None:
                resolved.append(fieldname)
        return resolved

    @classmethod
    def _resolve_filters(
        cls,
        doctype: str,
        filters: list[Mapping[str, Any]],
        user: str,
        cache: dict[str, dict[str, Any]],
        issues: list[dict[str, Any]],
        resolutions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        resolved = []
        for index, item in enumerate(filters):
            fieldname = cls._resolve_field_reference(
                doctype,
                item["field"],
                f"/filters/{index}/field",
                user,
                cache,
                issues,
                resolutions,
            )
            if fieldname is not None:
                resolved.append(
                    {
                        "field": fieldname,
                        "operator": item["operator"],
                        "value": item["value"],
                    }
                )
        return resolved

    @classmethod
    def _resolve_sort(
        cls,
        doctype: str,
        sort: list[Mapping[str, Any]],
        user: str,
        cache: dict[str, dict[str, Any]],
        issues: list[dict[str, Any]],
        resolutions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        resolved = []
        for index, item in enumerate(sort):
            fieldname = cls._resolve_field_reference(
                doctype,
                item["field"],
                f"/sort/{index}/field",
                user,
                cache,
                issues,
                resolutions,
            )
            if fieldname is not None:
                resolved.append({"field": fieldname, "direction": item["direction"]})
        return resolved

    @classmethod
    def _resolve_field_reference(
        cls,
        doctype: str,
        reference: str,
        location: str,
        user: str,
        cache: dict[str, dict[str, Any]],
        issues: list[dict[str, Any]],
        resolutions: list[dict[str, Any]],
    ) -> str | None:
        if reference not in cache:
            cache[reference] = ContextService.resolve_field(
                doctype,
                reference,
                user=user,
            )
        result = cache[reference]
        if result.get("match_status") != "resolved":
            issues.append(cls._resolution_issue(location, reference, result))
            return None

        resolved_value = result.get("resolved_value")
        if not isinstance(resolved_value, Mapping) or not isinstance(
            resolved_value.get("fieldname"), str
        ):
            frappe.throw(
                _("Unable to establish canonical field context"),
                frappe.PermissionError,
            )

        fieldname = resolved_value["fieldname"]
        resolutions.append(
            cls._resolution_record(location, reference, fieldname, result)
        )
        return fieldname

    @classmethod
    def _validate_request_mapping(cls, request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping):
            frappe.throw(_("Query planning request must be a mapping"), frappe.ValidationError)

        unknown_keys = set(request) - cls._ALLOWED_REQUEST_KEYS
        if unknown_keys:
            frappe.throw(
                _("Unsupported query planning keys: {0}").format(
                    ", ".join(sorted(str(key) for key in unknown_keys))
                ),
                frappe.ValidationError,
            )
        if "doctype" not in request:
            frappe.throw(_("Query planning request requires a doctype"), frappe.ValidationError)

    @classmethod
    def _get_plan_type(cls, request: Mapping[str, Any]) -> str:
        aggregate_intent = "aggregations" in request or "group_by" in request
        if aggregate_intent:
            if "aggregations" not in request:
                frappe.throw(
                    _("group_by requires aggregation operations"),
                    frappe.ValidationError,
                )
            conflicting = cls._RECORD_ONLY_KEYS & set(request)
            if conflicting:
                frappe.throw(
                    _("Aggregation requests cannot contain record-only keys: {0}").format(
                        ", ".join(sorted(conflicting))
                    ),
                    frappe.ValidationError,
                )
            return "aggregation"

        if "fields" not in request:
            frappe.throw(_("Record query planning requires fields"), frappe.ValidationError)
        return "records"

    @classmethod
    def _validate_outer_structure(
        cls,
        request: Mapping[str, Any],
        plan_type: str,
        limits: Mapping[str, int],
    ) -> None:
        filters = cls._require_list(request.get("filters", []), "filters")
        if len(filters) > limits["filters"]:
            cls._raise_limit("filters", limits["filters"])
        for item in filters:
            cls._validate_mapping_keys(item, cls._FILTER_KEYS, "filter")
            cls._require_reference(item["field"], "filter field")

        if plan_type == "records":
            fields = cls._require_list(request["fields"], "fields", require_non_empty=True)
            if len(fields) > limits["fields"]:
                cls._raise_limit("fields", limits["fields"])
            for field in fields:
                cls._require_reference(field, "field")

            sort = cls._require_list(request.get("sort", []), "sort")
            if len(sort) > limits["sort_fields"]:
                cls._raise_limit("sort", limits["sort_fields"])
            for item in sort:
                cls._validate_mapping_keys(item, cls._SORT_KEYS, "sort entry")
                cls._require_reference(item["field"], "sort field")
            return

        aggregations = cls._require_list(
            request["aggregations"],
            "aggregations",
            require_non_empty=True,
        )
        if len(aggregations) > limits["aggregations"]:
            cls._raise_limit("aggregations", limits["aggregations"])
        for item in aggregations:
            cls._validate_mapping_keys(item, cls._AGGREGATION_KEYS, "aggregation")
            operation = item["operation"]
            if not isinstance(operation, str) or operation.lower() not in SUPPORTED_AGGREGATION_OPERATIONS:
                frappe.throw(
                    _("Unsupported aggregation operation"),
                    frappe.ValidationError,
                )
            cls._require_reference(item["field"], "aggregation field")

        group_by = cls._require_list(request.get("group_by", []), "group_by")
        if len(group_by) > limits["group_by_fields"]:
            cls._raise_limit("group_by", limits["group_by_fields"])
        for field in group_by:
            cls._require_reference(field, "group_by field")

    @staticmethod
    def _validate_mapping_keys(value: Any, expected: frozenset[str], name: str) -> None:
        if not isinstance(value, Mapping) or set(value) != expected:
            frappe.throw(
                _("Each {0} must contain exactly: {1}").format(
                    name,
                    ", ".join(sorted(expected)),
                ),
                frappe.ValidationError,
            )

    @staticmethod
    def _require_list(
        value: Any,
        name: str,
        *,
        require_non_empty: bool = False,
    ) -> list[Any]:
        if not isinstance(value, list):
            frappe.throw(_("{0} must be a list").format(name), frappe.ValidationError)
        if require_non_empty and not value:
            frappe.throw(_("{0} must not be empty").format(name), frappe.ValidationError)
        return value

    @staticmethod
    def _require_reference(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            frappe.throw(
                _("{0} must be a non-empty string").format(name),
                frappe.ValidationError,
            )
        return value

    @staticmethod
    def _get_authenticated_user(
        method_user: str | None,
        request_user: Any,
    ) -> str:
        session_user = getattr(getattr(frappe, "session", None), "user", None)
        if not isinstance(session_user, str) or not session_user:
            frappe.throw(_("An authenticated Frappe user is required"), frappe.PermissionError)

        for supplied_user in (method_user, request_user):
            if supplied_user is not None and not isinstance(supplied_user, str):
                frappe.throw(_("User must be a string"), frappe.ValidationError)
            if supplied_user is not None and supplied_user != session_user:
                frappe.throw(
                    _("Query plans can only be created for the current session user"),
                    frappe.PermissionError,
                )

        if method_user is not None and request_user is not None and method_user != request_user:
            frappe.throw(_("Conflicting user assertions"), frappe.PermissionError)
        return session_user

    @classmethod
    def _get_limits(cls, limit_snapshot: Mapping[str, int]) -> dict[str, int]:
        # Same names and snapshot as QueryValidator and AggregateService, so
        # planning and execution enforce identical limits.
        return ConfigService.resolve_limits(*cls._LIMIT_NAMES, snapshot=limit_snapshot)

    @staticmethod
    def _validate_execution_target(doctype: str) -> None:
        meta = frappe.get_meta(doctype)
        if bool(getattr(meta, "istable", False)):
            frappe.throw(
                _("Direct child DocType plans are not supported"),
                frappe.ValidationError,
            )
        if bool(getattr(meta, "issingle", False)):
            frappe.throw(
                _("Single DocType plans are not supported"),
                frappe.ValidationError,
            )
        if bool(getattr(meta, "is_virtual", False)):
            frappe.throw(
                _("Virtual DocType plans are not supported"),
                frappe.ValidationError,
            )

    @classmethod
    def _validate_aggregation_duplicates(
        cls,
        aggregations: list[Mapping[str, str]],
        group_by: list[str],
    ) -> None:
        signatures = [(item["operation"], item["field"]) for item in aggregations]
        if len(signatures) != len(set(signatures)):
            frappe.throw(_("Duplicate aggregation operation"), frappe.ValidationError)
        if len(group_by) != len(set(group_by)):
            frappe.throw(_("Duplicate group_by field"), frappe.ValidationError)

        generated_names = {f"aggregation_{index}" for index in range(1, len(aggregations) + 1)}
        if generated_names & set(group_by):
            frappe.throw(
                _("A group_by field conflicts with an aggregation result field"),
                frappe.ValidationError,
            )

    @staticmethod
    def _validate_aggregation_field_types(
        doctype: str,
        aggregations: list[Mapping[str, str]],
    ) -> None:
        metadata = MetadataService.get_doctype_metadata(doctype)
        field_types = {
            field["fieldname"]: field["fieldtype"]
            for field in metadata["fields"]
            if isinstance(field.get("fieldname"), str)
        }
        for aggregation in aggregations:
            operation = aggregation["operation"]
            fieldtype = field_types.get(aggregation["field"])
            if fieldtype not in AGGREGATION_FIELD_TYPES_BY_OPERATION[operation]:
                frappe.throw(
                    _("Aggregation operation is not valid for the resolved field type"),
                    frappe.ValidationError,
                )

    @staticmethod
    def _unique_fields(fields: list[str]) -> list[str]:
        return list(dict.fromkeys(fields))

    @staticmethod
    def _resolution_issue(
        location: str,
        reference: Any,
        resolution: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "location": location,
            "reference": reference,
            "match_status": resolution.get("match_status", "not_found"),
            "resolution_type": resolution.get("resolution_type"),
            "confidence": resolution.get("confidence", "none"),
            "candidates": resolution.get("candidates", []),
        }

    @staticmethod
    def _resolution_record(
        location: str,
        reference: Any,
        resolved: str,
        resolution: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "location": location,
            "reference": reference,
            "resolved": resolved,
            "resolution_type": resolution.get("resolution_type"),
            "confidence": resolution.get("confidence"),
        }

    @classmethod
    def _non_executable_response(
        cls,
        plan_type: str,
        *,
        issues: list[dict[str, Any]],
        resolutions: list[dict[str, Any]],
        doctype: str | None = None,
    ) -> dict[str, Any]:
        clarifications = [
            issue
            for issue in issues
            if "ambiguous" in issue["match_status"]
            or "incomplete" in issue["match_status"]
        ]
        unresolved = [
            issue
            for issue in issues
            if "ambiguous" not in issue["match_status"]
            and "incomplete" not in issue["match_status"]
        ]
        status = "needs_clarification" if clarifications else "unresolved"
        return build_response(
            "query_plan",
            doctype=doctype,
            payload={
                "status": status,
                "plan_type": plan_type,
                "plan": None,
                "unresolved_references": unresolved,
                "clarification_requirements": clarifications,
                "resolutions": resolutions,
                "planning_metadata": {},
            },
        )

    @staticmethod
    def _ready_response(
        plan_type: str,
        doctype: str,
        plan: Mapping[str, Any],
        resolutions: list[dict[str, Any]],
        *,
        planning_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "ready",
            "plan_type": plan_type,
            "plan": dict(plan),
            "unresolved_references": [],
            "clarification_requirements": [],
            "resolutions": resolutions,
            "planning_metadata": dict(planning_metadata or {}),
        }
        return build_response("query_plan", doctype=doctype, payload=payload)

    @staticmethod
    def _raise_limit(name: str, maximum: int) -> None:
        frappe.throw(
            _("{0} cannot contain more than {1} entries").format(name, maximum),
            frappe.ValidationError,
        )
