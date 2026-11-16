"""Central planning and execution orchestration for ERP Intelligence."""

from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _

from erp_intelligence.services.aggregate_service import AggregateService
from erp_intelligence.services.config_service import ConfigService
from erp_intelligence.services.query_planner import QueryPlanner
from erp_intelligence.services.query_service import QueryService
from erp_intelligence.utils.response_utils import build_response


class IntelligenceService:
    """Dispatch only ready planner output to the matching execution service.

    Response contract. Every response carries ``result_type`` and a ``status``:

    * ``status == "executed"``: ``result_type`` is ``intelligence_execution``.
      Shared metadata (``doctype``, ``returned_count`` and ``has_more`` for
      records or ``truncated`` for aggregations) is promoted from the executor.
      The executor's full response is under ``execution_result``.
    * ``status == "needs_clarification"`` or ``"unresolved"``: the planner's
      ``query_plan`` response is returned unchanged, with ``plan`` set to None.
      No query is executed.

    ``plan_type``, ``resolutions`` and ``planning_metadata`` are present in all
    outcomes.
    """

    _NON_EXECUTABLE_STATUSES = frozenset({"needs_clarification", "unresolved"})
    _PLAN_TYPES = frozenset({"records", "aggregation"})
    _EXECUTED_STATUS = "executed"

    @classmethod
    def execute_request(
        cls,
        request: Mapping[str, Any],
        user: str | None = None,
    ) -> dict[str, Any]:
        """Plan a structured request and execute it only when fully ready.

        Effective limits are resolved once here. The same read-only snapshot is
        used by planning, validation and execution, so a site configuration
        change during the request cannot make them disagree.
        """
        limit_snapshot = ConfigService.get_limit_snapshot()
        planning_response = QueryPlanner.plan_query(
            request,
            user=user,
            limit_snapshot=limit_snapshot,
        )
        if not isinstance(planning_response, Mapping):
            frappe.throw(
                _("Query planner returned an invalid response"),
                frappe.ValidationError,
            )

        planning_status = planning_response.get("status")
        if planning_status in cls._NON_EXECUTABLE_STATUSES:
            cls._validate_non_executable_response(planning_response)
            return planning_response
        if planning_status != "ready":
            frappe.throw(
                _("Query planner did not return a recognized status"),
                frappe.ValidationError,
            )

        plan_type = planning_response.get("plan_type")
        if plan_type not in cls._PLAN_TYPES:
            frappe.throw(
                _("Query planner returned an unsupported plan type"),
                frappe.ValidationError,
            )

        plan = planning_response.get("plan")
        doctype = planning_response.get("doctype")
        resolutions = planning_response.get("resolutions")
        planning_metadata = planning_response.get("planning_metadata")
        if not isinstance(plan, Mapping):
            frappe.throw(_("Ready query plan is missing"), frappe.ValidationError)
        if not isinstance(doctype, str) or not doctype:
            frappe.throw(_("Ready query plan has no canonical DocType"), frappe.ValidationError)
        if plan.get("doctype") != doctype:
            frappe.throw(
                _("Ready query plan DocType is inconsistent"),
                frappe.ValidationError,
            )
        if not isinstance(resolutions, list):
            frappe.throw(
                _("Ready query plan has invalid resolutions"),
                frappe.ValidationError,
            )
        if not isinstance(planning_metadata, Mapping):
            frappe.throw(
                _("Query planner returned invalid planning metadata"),
                frappe.ValidationError,
            )

        if plan_type == "records":
            execution_result = QueryService.query_records(
                plan,
                user=user,
                limit_snapshot=limit_snapshot,
            )
            cls._validate_execution_result(execution_result, doctype, "records")
            shared_metadata = {
                "returned_count": cls._validate_record_result(execution_result),
                "has_more": execution_result["has_more"],
            }
        else:
            execution_result = AggregateService.aggregate_records(
                plan,
                user=user,
                limit_snapshot=limit_snapshot,
            )
            cls._validate_execution_result(execution_result, doctype, "aggregation")
            shared_metadata = {
                "returned_count": cls._validate_aggregation_result(execution_result),
                "truncated": execution_result["truncated"],
            }

        try:
            return build_response(
                "intelligence_execution",
                doctype=doctype,
                payload={
                    "status": cls._EXECUTED_STATUS,
                    "planning_status": planning_status,
                    "plan_type": plan_type,
                    "execution_result": execution_result,
                    "resolutions": resolutions,
                    "planning_metadata": dict(planning_metadata),
                },
                **shared_metadata,
            )
        except (TypeError, ValueError):
            frappe.throw(
                _("Execution response cannot be represented as JSON"),
                frappe.ValidationError,
            )

    @staticmethod
    def _validate_non_executable_response(planning_response: Mapping[str, Any]) -> None:
        # Clarification candidates and unresolved references are returned as the
        # planner produced them. Only the contract's invariants are checked here.
        if planning_response.get("plan") is not None:
            frappe.throw(
                _("A non-executable query plan must not contain an executable plan"),
                frappe.ValidationError,
            )
        for key in ("unresolved_references", "clarification_requirements", "resolutions"):
            if not isinstance(planning_response.get(key), list):
                frappe.throw(
                    _("Query planner returned an invalid {0} value").format(key),
                    frappe.ValidationError,
                )
        if not isinstance(planning_response.get("planning_metadata"), Mapping):
            frappe.throw(
                _("Query planner returned invalid planning metadata"),
                frappe.ValidationError,
            )

    @staticmethod
    def _validate_execution_result(
        execution_result: Any,
        doctype: str,
        expected_result_type: str,
    ) -> None:
        if not isinstance(execution_result, Mapping):
            frappe.throw(
                _("Execution service returned an invalid response"),
                frappe.ValidationError,
            )
        if execution_result.get("result_type") != expected_result_type:
            frappe.throw(
                _("Execution result type is inconsistent with the plan"),
                frappe.ValidationError,
            )
        if execution_result.get("doctype") != doctype:
            frappe.throw(
                _("Execution result DocType is inconsistent with the plan"),
                frappe.ValidationError,
            )

    @staticmethod
    def _validate_record_result(result: Mapping[str, Any]) -> int:
        records = result.get("records")
        returned_count = result.get("returned_count")
        page_length = result.get("page_length")
        offset = result.get("offset")
        has_more = result.get("has_more")
        if not isinstance(records, list):
            frappe.throw(_("Record execution returned invalid records"), frappe.ValidationError)
        if (
            isinstance(returned_count, bool)
            or not isinstance(returned_count, int)
            or returned_count != len(records)
        ):
            frappe.throw(
                _("Record execution returned an invalid count"),
                frappe.ValidationError,
            )
        if isinstance(page_length, bool) or not isinstance(page_length, int) or page_length < 1:
            frappe.throw(
                _("Record execution returned an invalid page length"),
                frappe.ValidationError,
            )
        if returned_count > page_length:
            frappe.throw(
                _("Record execution exceeded its page length"),
                frappe.ValidationError,
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            frappe.throw(
                _("Record execution returned an invalid offset"),
                frappe.ValidationError,
            )
        if not isinstance(has_more, bool):
            frappe.throw(
                _("Record execution returned an invalid has_more value"),
                frappe.ValidationError,
            )

        return returned_count

    @staticmethod
    def _validate_aggregation_result(result: Mapping[str, Any]) -> int:
        results = result.get("results")
        returned_count = result.get("returned_count")
        group_limit = result.get("group_limit")
        truncated = result.get("truncated")
        if not isinstance(results, list):
            frappe.throw(
                _("Aggregation execution returned an invalid response"),
                frappe.ValidationError,
            )
        if (
            isinstance(returned_count, bool)
            or not isinstance(returned_count, int)
            or returned_count != len(results)
        ):
            frappe.throw(
                _("Aggregation execution returned an invalid count"),
                frappe.ValidationError,
            )
        if isinstance(group_limit, bool) or not isinstance(group_limit, int) or group_limit < 1:
            frappe.throw(
                _("Aggregation execution returned an invalid group limit"),
                frappe.ValidationError,
            )
        if returned_count > group_limit:
            frappe.throw(
                _("Aggregation execution exceeded its group limit"),
                frappe.ValidationError,
            )
        # Completion is reported by the executor. It is never assumed here.
        if not isinstance(truncated, bool):
            frappe.throw(
                _("Aggregation execution did not establish completion"),
                frappe.ValidationError,
            )

        return returned_count
