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
    NUMERIC_AGGREGATION_FIELD_TYPES,
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
            "parent_context",
            "parent_scope",
            "ranking",
            "user",
        }
    )
    _PARENT_CONTEXT_KEYS = frozenset({"parent_doctype", "parent_names"})
    # Child-table aggregation (an additive request form, independent of parent_context).
    _PARENT_SCOPE_KEYS = frozenset({"parent_doctype", "parent_filters", "parent_names"})
    _RANKING_KEYS = frozenset({"by", "direction", "top_n", "include_ties"})
    _CHILD_AGGREGATION = "child_aggregation"
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
        cls._reject_unsupported_child_aggregation_forms(request)
        plan_type = cls._get_plan_type(request)
        if limit_snapshot is None:
            limit_snapshot = ConfigService.get_limit_snapshot()
        limits = cls._get_limits(limit_snapshot)
        cls._validate_outer_structure(request, plan_type, limits)

        if "parent_scope" in request:
            return cls._plan_child_aggregation(
                request,
                authenticated_user,
                limits,
                limit_snapshot,
            )

        if "parent_context" in request:
            if plan_type == "aggregation":
                # Grouped aggregation over the child rows of explicitly named parents,
                # for example sum(qty) per item_code over a list of Sales Orders.
                return cls._plan_child_group_aggregation(
                    request,
                    authenticated_user,
                    limits,
                    limit_snapshot,
                )
            if plan_type != "records":
                frappe.throw(
                    _("parent_context is only supported for record and aggregation requests"),
                    frappe.ValidationError,
                )
            return cls._plan_child_records(
                request,
                authenticated_user,
                limit_snapshot,
            )

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
    def _plan_child_records(
        cls,
        request: Mapping[str, Any],
        user: str,
        limit_snapshot: Mapping[str, int],
    ) -> dict[str, Any]:
        """Plan a record query over one child table of explicitly named parents."""
        parent_reference, _parent_names = cls._validate_parent_context_shape(
            request["parent_context"]
        )

        parent_resolution = ContextService.resolve_doctype(parent_reference, user=user)
        if parent_resolution.get("match_status") != "resolved":
            issue = cls._resolution_issue(
                "/parent_context/parent_doctype",
                parent_reference,
                parent_resolution,
            )
            return cls._non_executable_response("records", issues=[issue], resolutions=[])

        parent_value = parent_resolution.get("resolved_value")
        if not isinstance(parent_value, Mapping) or not isinstance(parent_value.get("name"), str):
            frappe.throw(
                _("Unable to establish canonical DocType context"),
                frappe.PermissionError,
            )
        parent_doctype = parent_value["name"]
        cls._validate_execution_target(parent_doctype)

        child_doctype, parent_fieldname, child_resolution = cls._resolve_child_doctype(
            request["doctype"],
            parent_doctype,
        )
        resolutions = [
            cls._resolution_record(
                "/parent_context/parent_doctype",
                parent_reference,
                parent_doctype,
                parent_resolution,
            ),
            cls._resolution_record(
                "/doctype",
                request["doctype"],
                child_doctype,
                child_resolution,
            ),
        ]

        parent = {"parent_doctype": parent_doctype, "parent_fieldname": parent_fieldname}
        issues: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any]] = {}
        fields = cls._resolve_field_list(
            child_doctype,
            request["fields"],
            "/fields",
            user,
            cache,
            issues,
            resolutions,
            parent,
        )
        filters = cls._resolve_filters(
            child_doctype,
            request.get("filters", []),
            user,
            cache,
            issues,
            resolutions,
            parent,
        )
        sort = cls._resolve_sort(
            child_doctype,
            request.get("sort", []),
            user,
            cache,
            issues,
            resolutions,
            parent,
        )
        if issues:
            return cls._non_executable_response(
                "records",
                issues=issues,
                resolutions=resolutions,
                doctype=child_doctype,
            )

        validation_request: dict[str, Any] = {
            "doctype": child_doctype,
            "fields": fields,
            "filters": filters,
            "sort": sort,
            "parent_doctype": parent_doctype,
            "parent_fieldname": parent_fieldname,
            "parent_names": request["parent_context"]["parent_names"],
        }
        if "page_length" in request:
            validation_request["page_length"] = request["page_length"]
        if "offset" in request:
            validation_request["offset"] = request["offset"]

        # QueryValidator re-verifies the relationship from live metadata, applies
        # FieldPolicy under the parent context, and enforces every limit.
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
            "parent_doctype": validated["parent_doctype"],
            "parent_fieldname": validated["parent_fieldname"],
            "parent_names": validated["parent_names"],
        }
        return cls._ready_response("records", child_doctype, plan, resolutions)

    @classmethod
    def _plan_child_group_aggregation(
        cls,
        request: Mapping[str, Any],
        user: str,
        limits: Mapping[str, int],
        limit_snapshot: Mapping[str, int],
    ) -> dict[str, Any]:
        """Plan an aggregation over one child table of explicitly named parents.

        The parents are named in ``parent_context``, exactly as for child record reads.
        The executor keeps only the parents the user may read. The plan type stays
        ``aggregation``: the plan carries the parent context and AggregateService runs it.
        """
        parent_reference, _parent_names = cls._validate_parent_context_shape(
            request["parent_context"]
        )
        parent_resolution = ContextService.resolve_doctype(parent_reference, user=user)
        if parent_resolution.get("match_status") != "resolved":
            issue = cls._resolution_issue(
                "/parent_context/parent_doctype",
                parent_reference,
                parent_resolution,
            )
            return cls._non_executable_response("aggregation", issues=[issue], resolutions=[])
        parent_value = parent_resolution.get("resolved_value")
        if not isinstance(parent_value, Mapping) or not isinstance(parent_value.get("name"), str):
            frappe.throw(
                _("Unable to establish canonical DocType context"),
                frappe.PermissionError,
            )
        parent_doctype = parent_value["name"]
        cls._validate_execution_target(parent_doctype)
        child_doctype, parent_fieldname, child_resolution = cls._resolve_child_doctype(
            request["doctype"],
            parent_doctype,
        )
        resolutions = [
            cls._resolution_record(
                "/parent_context/parent_doctype",
                parent_reference,
                parent_doctype,
                parent_resolution,
            ),
            cls._resolution_record(
                "/doctype",
                request["doctype"],
                child_doctype,
                child_resolution,
            ),
        ]
        parent = {"parent_doctype": parent_doctype, "parent_fieldname": parent_fieldname}
        issues: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any]] = {}
        aggregations = []
        for index, aggregation in enumerate(request["aggregations"]):
            operation = aggregation["operation"].lower()
            fieldname = cls._resolve_field_reference(
                child_doctype,
                aggregation["field"],
                f"/aggregations/{index}/field",
                user,
                cache,
                issues,
                resolutions,
                parent,
            )
            if fieldname is not None:
                aggregations.append({"operation": operation, "field": fieldname})
        group_by = cls._resolve_field_list(
            child_doctype,
            request.get("group_by", []),
            "/group_by",
            user,
            cache,
            issues,
            resolutions,
            parent,
        )
        filters = cls._resolve_filters(
            child_doctype,
            request.get("filters", []),
            user,
            cache,
            issues,
            resolutions,
            parent,
        )
        if issues:
            return cls._non_executable_response(
                "aggregation",
                issues=issues,
                resolutions=resolutions,
                doctype=child_doctype,
            )
        cls._validate_aggregation_duplicates(aggregations, group_by)
        referenced_fields = cls._unique_fields(
            [*group_by, *(item["field"] for item in aggregations)]
        )
        # QueryValidator re-verifies the relationship from live metadata, applies
        # FieldPolicy under the parent context, and enforces every limit.
        validated = QueryValidator.validate_query(
            {
                "doctype": child_doctype,
                "fields": referenced_fields,
                "filters": filters,
                "sort": [],
                "page_length": 1,
                "offset": 0,
                "parent_doctype": parent_doctype,
                "parent_fieldname": parent_fieldname,
                "parent_names": request["parent_context"]["parent_names"],
            },
            user=user,
            limit_snapshot=limit_snapshot,
        )
        cls._validate_aggregation_field_types(child_doctype, aggregations)
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
            "parent_doctype": validated["parent_doctype"],
            "parent_fieldname": validated["parent_fieldname"],
            "parent_names": validated["parent_names"],
        }
        return cls._ready_response(
            "aggregation",
            child_doctype,
            plan,
            resolutions,
            planning_metadata={
                "complexity": complexity,
                "complexity_limit": limits["aggregate_complexity"],
                "group_limit": limits["returned_groups"],
            },
        )

    @classmethod
    def _validate_parent_context_shape(cls, value: Any) -> tuple[str, list[Any]]:
        if not isinstance(value, Mapping) or set(value) != cls._PARENT_CONTEXT_KEYS:
            frappe.throw(
                _("parent_context must contain exactly: {0}").format(
                    ", ".join(sorted(cls._PARENT_CONTEXT_KEYS))
                ),
                frappe.ValidationError,
            )

        parent_reference = cls._require_reference(value["parent_doctype"], "parent_doctype")
        parent_names = value["parent_names"]
        if not isinstance(parent_names, list) or not parent_names:
            frappe.throw(
                _("parent_names must be a non-empty list"),
                frappe.ValidationError,
            )
        return parent_reference, parent_names

    @classmethod
    def _resolve_child_doctype(
        cls,
        reference: Any,
        parent_doctype: str,
    ) -> tuple[str, str, dict[str, str]]:
        """Match the requested child DocType against the parent's own Table fields."""
        if not isinstance(reference, str) or not reference.strip():
            frappe.throw(_("doctype must be a non-empty string"), frappe.ValidationError)
        reference = reference.strip()

        def normalize(value: str) -> str:
            return " ".join(value.split()).casefold()

        children = {
            field["child_doctype"]
            for field in MetadataService.get_doctype_metadata(parent_doctype)["fields"]
            if isinstance(field.get("child_doctype"), str) and field["child_doctype"]
        }
        matches = [name for name in children if name == reference]
        resolution = {"resolution_type": "exact_child_table_name", "confidence": "exact"}
        if not matches:
            matches = [name for name in children if normalize(name) == normalize(reference)]
            resolution = {
                "resolution_type": "case_insensitive_child_table_name",
                "confidence": "case_insensitive",
            }
        if len(matches) != 1:
            # Unknown, not a child table at all, or a child of a different parent.
            frappe.throw(
                _("{0} is not a child table of {1}").format(reference, parent_doctype),
                frappe.ValidationError,
            )

        child_doctype = matches[0]
        fieldnames = MetadataService.get_child_table_fieldnames(parent_doctype, child_doctype)
        if not fieldnames:
            frappe.throw(
                _("{0} is not a child table of {1}").format(reference, parent_doctype),
                frappe.ValidationError,
            )
        if len(fieldnames) > 1:
            frappe.throw(
                _(
                    "{0} is used by more than one table field of {1}; "
                    "such requests are not supported"
                ).format(child_doctype, parent_doctype),
                frappe.ValidationError,
            )
        return child_doctype, fieldnames[0], resolution

    # ------------------------------------------------------------------ child-table aggregation
    @classmethod
    def _reject_unsupported_child_aggregation_forms(cls, request: Mapping[str, Any]) -> None:
        """Reject, before anything else is planned, every form that is not child aggregation.

        Does nothing for a request without ``parent_scope`` or ``ranking``, so every
        existing request is planned exactly as before. ``ranking`` only exists for
        child aggregation, and ``parent_scope`` stands alone (never with ``parent_context``).
        """
        has_scope = "parent_scope" in request
        if "ranking" in request and not has_scope:
            frappe.throw(
                _("ranking is only supported together with parent_scope"),
                frappe.ValidationError,
            )
        if not has_scope:
            return

        if "parent_context" in request:
            frappe.throw(
                _("parent_scope cannot be combined with parent_context"),
                frappe.ValidationError,
            )
        if "aggregations" not in request:
            frappe.throw(
                _("parent_scope is only supported for aggregation requests"),
                frappe.ValidationError,
            )
        conflicting = cls._RECORD_ONLY_KEYS & set(request)
        if conflicting:
            frappe.throw(
                _(
                    "Child aggregation requests cannot contain record retrieval, sorting or "
                    "pagination keys: {0}"
                ).format(", ".join(sorted(conflicting))),
                frappe.ValidationError,
            )
        if request.get("filters") not in (None, []):
            frappe.throw(
                _(
                    "Child-row filters are not supported for child aggregation. Use "
                    "parent_scope.parent_filters to scope the parent records."
                ),
                frappe.ValidationError,
            )
        if "group_by" not in request:
            frappe.throw(
                _("Child aggregation requires group_by to be the parent field"),
                frappe.ValidationError,
            )
        if "ranking" not in request:
            frappe.throw(_("parent_scope requires ranking"), frappe.ValidationError)

    @classmethod
    def _plan_child_aggregation(
        cls,
        request: Mapping[str, Any],
        user: str,
        limits: Mapping[str, int],
        limit_snapshot: Mapping[str, int],
    ) -> dict[str, Any]:
        """Plan a ranking of parent documents by an aggregate of their child rows.

        Produces the request ``ChildAggregateService`` executes. Nothing is read from
        the database here except metadata and permissions. Every permission decision
        is made by ``ContextService``, ``QueryValidator`` and ``FieldPolicy`` under the
        parent context, so a Table field or a child field the user cannot read never
        reaches a plan. ``parent_names`` and ``parent_filters`` can only narrow the
        scope: they are carried as data and executed as filters on a permission-checked
        parent query. They never grant access.
        """
        parent_reference, scope = cls._validate_parent_scope_shape(request["parent_scope"], limits)

        # The child-aggregation limits are not in the base snapshot. They are added only
        # now, so a malformed setting for them can only ever fail a child aggregation.
        child_snapshot = ConfigService.get_child_aggregation_limit_snapshot(limit_snapshot)
        ranking_limits = ConfigService.resolve_limits(
            "child_agg_top_n",
            "returned_groups",
            "child_agg_parent_scan",
            "child_agg_parent_batch",
            "child_agg_max_seconds",
            snapshot=child_snapshot,
        )
        ranking = cls._validate_ranking_shape(request["ranking"], ranking_limits)

        parent_resolution = ContextService.resolve_doctype(parent_reference, user=user)
        if parent_resolution.get("match_status") != "resolved":
            issue = cls._resolution_issue(
                "/parent_scope/parent_doctype",
                parent_reference,
                parent_resolution,
            )
            return cls._non_executable_response(
                cls._CHILD_AGGREGATION, issues=[issue], resolutions=[]
            )
        parent_value = parent_resolution.get("resolved_value")
        if not isinstance(parent_value, Mapping) or not isinstance(parent_value.get("name"), str):
            frappe.throw(
                _("Unable to establish canonical DocType context"),
                frappe.PermissionError,
            )
        parent_doctype = parent_value["name"]
        cls._validate_execution_target(parent_doctype)

        # Raises unless the child is a Table of the parent, used by exactly one Table field.
        child_doctype, parent_fieldname, child_resolution = cls._resolve_child_doctype(
            request["doctype"],
            parent_doctype,
        )
        resolutions = [
            cls._resolution_record(
                "/parent_scope/parent_doctype", parent_reference, parent_doctype, parent_resolution
            ),
            cls._resolution_record("/doctype", request["doctype"], child_doctype, child_resolution),
        ]

        parent = {"parent_doctype": parent_doctype, "parent_fieldname": parent_fieldname}
        issues: list[dict[str, Any]] = []
        child_cache: dict[str, dict[str, Any]] = {}
        parent_cache: dict[str, dict[str, Any]] = {}

        aggregations = []
        for index, aggregation in enumerate(request["aggregations"]):
            fieldname = cls._resolve_field_reference(
                child_doctype,
                aggregation["field"],
                f"/aggregations/{index}/field",
                user,
                child_cache,
                issues,
                resolutions,
                parent,
            )
            if fieldname is not None:
                aggregations.append(
                    {"operation": aggregation["operation"].lower(), "field": fieldname}
                )
        group_by = cls._resolve_field_list(
            child_doctype,
            request["group_by"],
            "/group_by",
            user,
            child_cache,
            issues,
            resolutions,
            parent,
        )
        parent_filters = []
        for index, item in enumerate(scope["parent_filters"]):
            fieldname = cls._resolve_field_reference(
                parent_doctype,
                item["field"],
                f"/parent_scope/parent_filters/{index}/field",
                user,
                parent_cache,
                issues,
                resolutions,
            )
            if fieldname is not None:
                parent_filters.append(
                    {"field": fieldname, "operator": item["operator"], "value": item["value"]}
                )
        ranking_field = None
        if ranking["by"] is not None:
            ranking_field = cls._resolve_field_reference(
                child_doctype,
                ranking["by"]["field"],
                "/ranking/by/field",
                user,
                child_cache,
                issues,
                resolutions,
                parent,
            )
        if issues:
            return cls._non_executable_response(
                cls._CHILD_AGGREGATION,
                issues=issues,
                resolutions=resolutions,
                doctype=child_doctype,
            )

        if group_by != ["parent"]:
            frappe.throw(
                _("Child aggregation must group by the parent field and nothing else"),
                frappe.ValidationError,
            )
        cls._validate_aggregation_duplicates(aggregations, group_by)
        cls._validate_aggregation_field_types(child_doctype, aggregations)
        ranked = cls._select_ranking_aggregation(
            child_doctype, aggregations, ranking, ranking_field
        )

        # Neither validation is skipped, and neither is replaced: this is the same
        # QueryValidator and FieldPolicy path every other request takes, run for the
        # child under the parent context and for the parent's own filters.
        child_plan = QueryValidator.validate_query(
            {
                "doctype": child_doctype,
                "fields": cls._unique_fields(["parent", *(a["field"] for a in aggregations)]),
                "filters": [],
                "sort": [],
                "page_length": 1,
                "offset": 0,
                "parent_doctype": parent_doctype,
                "parent_fieldname": parent_fieldname,
                "parent_names": scope["parent_names"],
            },
            user=user,
            limit_snapshot=child_snapshot,
            parent_names_required=False,
        )
        parent_plan = QueryValidator.validate_query(
            {
                "doctype": parent_doctype,
                "fields": ["name"],
                "filters": parent_filters,
                "sort": [],
                "page_length": 1,
                "offset": 0,
            },
            user=user,
            limit_snapshot=child_snapshot,
        )
        complexity = AggregateService.enforce_complexity_limit(
            aggregations,
            ["parent"],
            parent_plan["filters"],
            limits["aggregate_complexity"],
        )

        plan = {
            "doctype": child_plan["doctype"],
            "parent_doctype": parent_plan["doctype"],
            "parent_fieldname": child_plan["parent_fieldname"],
            "parent_filters": parent_plan["filters"],
            "parent_names": child_plan["parent_names"],
            "aggregations": aggregations,
            "group_by": ["parent"],
            "ranking": {
                "by": dict(ranked),
                "direction": "desc",
                "top_n": ranking["top_n"],
                "include_ties": ranking["include_ties"],
            },
        }
        return cls._ready_response(
            cls._CHILD_AGGREGATION,
            child_plan["doctype"],
            plan,
            resolutions,
            planning_metadata={
                "complexity": complexity,
                "complexity_limit": limits["aggregate_complexity"],
                "top_n_limit": min(ranking_limits["child_agg_top_n"], ranking_limits["returned_groups"]),
                "result_cap": ranking_limits["returned_groups"],
                "parent_scan_limit": ranking_limits["child_agg_parent_scan"],
                "parent_batch_size": ranking_limits["child_agg_parent_batch"],
                "time_budget_seconds": ranking_limits["child_agg_max_seconds"],
            },
        )

    @classmethod
    def _validate_parent_scope_shape(
        cls,
        value: Any,
        limits: Mapping[str, int],
    ) -> tuple[str, dict[str, Any]]:
        if (
            not isinstance(value, Mapping)
            or "parent_doctype" not in value
            or set(value) - cls._PARENT_SCOPE_KEYS
        ):
            frappe.throw(
                _("parent_scope must contain parent_doctype and may contain only: {0}").format(
                    ", ".join(sorted(cls._PARENT_SCOPE_KEYS))
                ),
                frappe.ValidationError,
            )
        reference = cls._require_reference(value["parent_doctype"], "parent_doctype")

        filters = cls._require_list(value.get("parent_filters", []), "parent_filters")
        if len(filters) > limits["filters"]:
            cls._raise_limit("parent_filters", limits["filters"])
        for item in filters:
            cls._validate_mapping_keys(item, cls._FILTER_KEYS, "parent filter")
            cls._require_reference(item["field"], "parent filter field")

        names = value.get("parent_names")
        if "parent_names" in value and (not isinstance(names, list) or not names):
            frappe.throw(_("parent_names must be a non-empty list"), frappe.ValidationError)
        return reference, {"parent_filters": filters, "parent_names": names}

    @classmethod
    def _validate_ranking_shape(
        cls,
        value: Any,
        ranking_limits: Mapping[str, int],
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) - cls._RANKING_KEYS:
            frappe.throw(
                _("ranking may only contain: {0}").format(", ".join(sorted(cls._RANKING_KEYS))),
                frappe.ValidationError,
            )
        direction = value.get("direction", "desc")
        if not isinstance(direction, str) or direction.lower() != "desc":
            frappe.throw(
                _(
                    "Only descending ranking is supported: parents without child rows "
                    "produce no group, so an ascending ranking would be wrong"
                ),
                frappe.ValidationError,
            )

        # top_n must fit both the configured top-N limit and the result cap, which is
        # the number of rows a tie-inclusive ranking may return. Their effective values
        # are compared here, because the limit definitions alone cannot guarantee it.
        maximum = min(ranking_limits["child_agg_top_n"], ranking_limits["returned_groups"])
        top_n = value.get("top_n")
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= maximum:
            frappe.throw(
                _("ranking.top_n must be an integer between 1 and {0}").format(maximum),
                frappe.ValidationError,
            )
        include_ties = value.get("include_ties", True)
        if not isinstance(include_ties, bool):
            frappe.throw(_("ranking.include_ties must be a boolean"), frappe.ValidationError)

        by = value.get("by")
        if by is not None:
            cls._validate_mapping_keys(by, cls._AGGREGATION_KEYS, "ranking.by")
            operation = by["operation"]
            if not isinstance(operation, str) or operation.lower() not in SUPPORTED_AGGREGATION_OPERATIONS:
                frappe.throw(_("Unsupported aggregation operation"), frappe.ValidationError)
            cls._require_reference(by["field"], "ranking.by field")
            by = {"operation": operation.lower(), "field": by["field"]}
        return {"by": by, "direction": "desc", "top_n": top_n, "include_ties": include_ties}

    @classmethod
    def _select_ranking_aggregation(
        cls,
        child_doctype: str,
        aggregations: list[dict[str, str]],
        ranking: Mapping[str, Any],
        ranking_field: str | None,
    ) -> dict[str, str]:
        """Return the aggregation to rank by, and require that it is numeric."""
        if ranking["by"] is None:
            if len(aggregations) != 1:
                frappe.throw(
                    _("ranking.by is required when there is more than one aggregation"),
                    frappe.ValidationError,
                )
            chosen = aggregations[0]
        else:
            matches = [
                item
                for item in aggregations
                if item["operation"] == ranking["by"]["operation"] and item["field"] == ranking_field
            ]
            if len(matches) != 1:
                frappe.throw(
                    _("ranking.by must match one of the aggregations"), frappe.ValidationError
                )
            chosen = matches[0]

        if chosen["operation"] != "count":
            fieldtype = next(
                (
                    field.get("fieldtype")
                    for field in MetadataService.get_doctype_metadata(child_doctype)["fields"]
                    if field.get("fieldname") == chosen["field"]
                ),
                None,
            )
            if fieldtype not in NUMERIC_AGGREGATION_FIELD_TYPES:
                frappe.throw(
                    _("Ranking requires a numeric aggregation"), frappe.ValidationError
                )
        return chosen

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
        parent: Mapping[str, str] | None = None,
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
                parent,
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
        parent: Mapping[str, str] | None = None,
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
                parent,
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
        parent: Mapping[str, str] | None = None,
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
                parent,
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
        parent: Mapping[str, str] | None = None,
    ) -> str | None:
        if reference not in cache:
            if parent is None:
                cache[reference] = ContextService.resolve_field(
                    doctype,
                    reference,
                    user=user,
                )
            else:
                cache[reference] = ContextService.resolve_field(
                    doctype,
                    reference,
                    user=user,
                    parent_doctype=parent["parent_doctype"],
                    parent_fieldname=parent["parent_fieldname"],
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
