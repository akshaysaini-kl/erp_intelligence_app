"""Permission-aware record query service."""

from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _

from erp_intelligence.services.query_validator import QueryValidator
from erp_intelligence.utils.response_utils import build_paginated_response


class QueryService:
    """Execute validated record queries for the authenticated Frappe user."""

    @classmethod
    def query_records(
        cls,
        request: Mapping[str, Any],
        user: str | None = None,
        limit_snapshot: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Validate and execute one bounded, permission-aware record query.

        ``limit_snapshot`` is the request-scoped snapshot supplied by
        IntelligenceService. Direct callers omit it and limits are resolved here.
        """
        plan = QueryValidator.validate_query(
            request,
            user=user,
            limit_snapshot=limit_snapshot,
        )
        cls._assert_authenticated_user(user)

        doctype = plan["doctype"]
        meta = frappe.get_meta(doctype)
        cls._validate_execution_target(meta, plan)

        requested_fields = list(plan["fields"])
        page_length = plan["page_length"]
        fetch_length = page_length + 1
        is_child = bool(getattr(meta, "istable", False))

        query_options: dict[str, Any] = {
            "fields": requested_fields,
            "filters": cls._build_filters(doctype, plan["filters"]),
            "limit_start": plan["offset"],
            "limit_page_length": fetch_length,
            "as_list": False,
            "strict": True,
        }
        if plan["sort"]:
            query_options["order_by"] = cls._build_order_by(plan["sort"])

        parent_context = None
        accessible_parents: list[str] = []
        if is_child:
            # Frappe applies none of the parent's row-level rules to child rows,
            # so first ask which of the named parents this user may read.
            accessible_parents = cls._get_accessible_parent_names(plan)
            parent_context = {
                "parent_doctype": plan["parent_doctype"],
                "parent_fieldname": plan["parent_fieldname"],
                "requested_count": len(plan["parent_names"]),
                "accessible_count": len(accessible_parents),
            }
            query_options["parent_doctype"] = plan["parent_doctype"]
            query_options["filters"] = [
                *query_options["filters"],
                *cls._build_parent_filters(doctype, plan, accessible_parents),
            ]
            query_options["order_by"] = cls._build_child_order_by(plan["sort"])

        # Permission enforcement remains enabled because get_list is called for
        # the current session without ignore_permissions or a user override.
        if is_child and not accessible_parents:
            # No readable parent: no child rows may be read, so no child query runs.
            rows = []
        else:
            rows = frappe.get_list(doctype, **query_options)
        records, has_more = cls._normalize_result(
            rows,
            requested_fields,
            page_length,
            fetch_length,
        )

        # has_more is established by the lookahead row fetched above, so it is an
        # explicit completion signal for this page. No total count is reported
        # because none was computed.
        try:
            return build_paginated_response(
                "records",
                records,
                page_length=page_length,
                offset=plan["offset"],
                has_more=has_more,
                item_key="records",
                doctype=doctype,
                extra=cls._response_extra(requested_fields, parent_context),
            )
        except (TypeError, ValueError):
            frappe.throw(
                _("Record query returned a value that cannot be represented as JSON"),
                frappe.ValidationError,
            )

    @staticmethod
    def _assert_authenticated_user(user: str | None) -> str:
        session_user = getattr(getattr(frappe, "session", None), "user", None)
        if not isinstance(session_user, str) or not session_user:
            frappe.throw(_("An authenticated Frappe user is required"), frappe.PermissionError)

        if user is not None and user != session_user:
            frappe.throw(
                _("Records can only be queried for the current session user"),
                frappe.PermissionError,
            )

        return session_user

    @staticmethod
    def _validate_execution_target(meta: Any, plan: Mapping[str, Any]) -> None:
        if getattr(meta, "name", None) != plan["doctype"]:
            frappe.throw(
                _("DocType metadata changed during query validation"),
                frappe.ValidationError,
            )

        has_parent_context = (
            plan.get("parent_doctype") is not None
            or plan.get("parent_fieldname") is not None
            or plan.get("parent_names") is not None
        )
        if bool(getattr(meta, "istable", False)):
            # A child DocType is only queryable with a complete, validated parent
            # context. Without it the rows' parent permissions cannot be enforced.
            if not (
                plan.get("parent_doctype")
                and plan.get("parent_fieldname")
                and plan.get("parent_names")
            ):
                frappe.throw(
                    _(
                        "Direct child DocType queries cannot enforce parent record permissions safely"
                    ),
                    frappe.PermissionError,
                )
        elif has_parent_context:
            frappe.throw(
                _("Parent context is not valid for this record query"),
                frappe.ValidationError,
            )

        if bool(getattr(meta, "issingle", False)):
            frappe.throw(
                _("Single DocTypes are not supported by record queries"),
                frappe.ValidationError,
            )

        if bool(getattr(meta, "is_virtual", False)):
            frappe.throw(
                _("Virtual DocTypes are not supported by record queries"),
                frappe.ValidationError,
            )

    @staticmethod
    def _get_accessible_parent_names(plan: Mapping[str, Any]) -> list[str]:
        """Return the requested parent names the current user may read, in request order.

        Uses get_list on the parent DocType, so the parent's own permission
        rules apply (role permissions, user permissions, owner-only rules,
        shares and permission query conditions). Names that do not exist and
        names the user cannot read are indistinguishable by design.
        """
        parent_doctype = plan["parent_doctype"]
        requested = list(plan["parent_names"])
        rows = frappe.get_list(
            parent_doctype,
            fields=["name"],
            filters=[[parent_doctype, "name", "in", requested]],
            limit_page_length=len(requested),
            as_list=False,
            strict=True,
        )
        if not isinstance(rows, list) or len(rows) > len(requested):
            frappe.throw(
                _("Parent access check returned an invalid result"),
                frappe.ValidationError,
            )

        accessible = set()
        for row in rows:
            name = row.get("name") if isinstance(row, Mapping) else None
            if not isinstance(name, str) or name not in requested:
                frappe.throw(
                    _("Parent access check returned an invalid result"),
                    frappe.ValidationError,
                )
            accessible.add(name)

        return [name for name in requested if name in accessible]

    @staticmethod
    def _build_parent_filters(
        doctype: str,
        plan: Mapping[str, Any],
        accessible_parents: list[str],
    ) -> list[list[Any]]:
        """Restrict child rows to readable parents of the stated table only."""
        return [
            [doctype, "parent", "in", list(accessible_parents)],
            [doctype, "parenttype", "=", plan["parent_doctype"]],
            [doctype, "parentfield", "=", plan["parent_fieldname"]],
        ]

    @staticmethod
    def _build_child_order_by(sort: list[Mapping[str, str]]) -> str:
        """User sort first, then parent and row order so offset paging is stable."""
        parts = [f"`{item['field']}` {item['direction']}" for item in sort]
        used = {item["field"] for item in sort}
        for fieldname in ("parent", "idx"):
            if fieldname not in used:
                parts.append(f"`{fieldname}` asc")
        return ", ".join(parts)

    @staticmethod
    def _response_extra(
        requested_fields: list[str],
        parent_context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {"fields": requested_fields}
        if parent_context is not None:
            extra["parent_context"] = dict(parent_context)
        return extra

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
    def _build_order_by(sort: list[Mapping[str, str]]) -> str:
        return ", ".join(
            f"`{item['field']}` {item['direction']}"
            for item in sort
        )

    @staticmethod
    def _normalize_result(
        rows: Any,
        requested_fields: list[str],
        page_length: int,
        fetch_length: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(rows, list) or len(rows) > fetch_length:
            frappe.throw(
                _("Record query returned an invalid result"),
                frappe.ValidationError,
            )

        has_more = len(rows) > page_length
        records = []
        for row in rows[:page_length]:
            if not isinstance(row, Mapping):
                frappe.throw(
                    _("Record query returned an invalid row"),
                    frappe.ValidationError,
                )

            if any(fieldname not in row for fieldname in requested_fields):
                frappe.throw(
                    _("Requested record fields are no longer permitted"),
                    frappe.PermissionError,
                )

            records.append(
                {fieldname: row[fieldname] for fieldname in requested_fields}
            )

        return records, has_more
