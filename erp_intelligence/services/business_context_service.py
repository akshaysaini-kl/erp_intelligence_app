"""Permission-aware discovery of ERPNext business configuration context."""

from collections.abc import Mapping
from typing import Any

import frappe
import frappe.defaults
from frappe import _
from frappe.utils import today

from erp_intelligence.services.config_service import ConfigService
from erp_intelligence.services.field_policy import FieldPolicy
from erp_intelligence.services.metadata_service import MetadataService
from erp_intelligence.utils.response_utils import to_json_safe


class BusinessContextService:
    """Discover current-session business defaults without reading transactions."""

    @classmethod
    def get_business_context(cls, user: str | None = None) -> dict[str, Any]:
        """Return bounded, permission-aware company, currency, and fiscal context."""
        authenticated_user = cls._get_authenticated_user(user)
        max_companies = ConfigService.get_limit("business_context_companies")
        max_fiscal_years = ConfigService.get_limit("business_context_fiscal_years")

        user_defaults, global_defaults = cls._get_default_maps(authenticated_user)
        available_companies = cls._get_available_companies(max_companies)

        user_company = cls._resolve_company_default(
            cls._extract_default_values(user_defaults, "Company", "company"),
            source="user_default",
        )
        global_company = cls._resolve_company_default(
            cls._extract_default_values(global_defaults, "Company", "company"),
            source="global_default",
        )
        effective_company_hint = frappe.defaults.get_user_default("Company")
        company_context = cls._resolve_company_context(
            user_company=user_company,
            global_company=global_company,
            effective_hint=effective_company_hint,
            available_companies=available_companies,
        )

        user_currency = cls._resolve_linked_default(
            "Currency",
            cls._extract_default_values(user_defaults, "Currency", "currency"),
            source="user_default",
        )
        global_currency = cls._resolve_linked_default(
            "Currency",
            cls._extract_default_values(global_defaults, "Currency", "currency"),
            source="global_default",
        )
        effective_currency = cls._resolve_effective_linked_default(
            "Currency",
            frappe.defaults.get_user_default("Currency"),
            user_currency,
            global_currency,
        )

        user_country = cls._resolve_linked_default(
            "Country",
            cls._extract_default_values(user_defaults, "Country", "country"),
            source="user_default",
        )
        global_country = cls._resolve_linked_default(
            "Country",
            cls._extract_default_values(global_defaults, "Country", "country"),
            source="global_default",
        )
        effective_country = cls._resolve_effective_linked_default(
            "Country",
            frappe.defaults.get_user_default("Country"),
            user_country,
            global_country,
        )

        company_currency = cls._company_currency_context(company_context)
        fiscal_year = cls._get_current_fiscal_year(
            company_context,
            max_fiscal_years,
        )

        response = {
            "result_type": "business_context",
            "user": authenticated_user,
            "as_of_date": today(),
            "available_companies": available_companies,
            "company_defaults": {
                "user": user_company,
                "global": global_company,
            },
            "company_context": company_context,
            "company_currency": company_currency,
            "currency_defaults": {
                "user": user_currency,
                "global": global_currency,
                "effective": effective_currency,
            },
            "country_defaults": {
                "user": user_country,
                "global": global_country,
                "effective": effective_country,
            },
            "current_fiscal_year": fiscal_year,
            "erpnext_global_defaults": cls._get_interpretation_defaults(global_defaults),
        }
        return to_json_safe(response)

    @staticmethod
    def _get_authenticated_user(user: str | None) -> str:
        session_user = getattr(getattr(frappe, "session", None), "user", None)
        if not isinstance(session_user, str) or not session_user:
            frappe.throw(_("An authenticated Frappe user is required"), frappe.PermissionError)

        if user is not None:
            if not isinstance(user, str):
                frappe.throw(_("User must be a string"), frappe.ValidationError)
            if user != session_user:
                frappe.throw(
                    _("Business context can only be read for the current session user"),
                    frappe.PermissionError,
                )

        return session_user

    @staticmethod
    def _get_default_maps(user: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        user_defaults = frappe.defaults.get_defaults_for(user)
        global_defaults = frappe.defaults.get_defaults_for()
        if not isinstance(user_defaults, Mapping) or not isinstance(global_defaults, Mapping):
            frappe.throw(
                _("Unable to read Frappe defaults"),
                frappe.ValidationError,
            )
        return user_defaults, global_defaults

    @staticmethod
    def _extract_default_values(defaults: Mapping[str, Any], *keys: str) -> dict[str, Any]:
        values = []
        configured = False
        invalid = False

        for key in keys:
            if key not in defaults:
                continue
            configured = True
            raw_value = defaults[key]
            raw_values = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]
            for value in raw_values:
                if not isinstance(value, str) or not value:
                    invalid = True
                    continue
                if value not in values:
                    values.append(value)

        return {
            "configured": configured,
            "invalid": invalid,
            "values": values,
        }

    @classmethod
    def _get_available_companies(cls, maximum: int) -> dict[str, Any]:
        try:
            has_read = bool(frappe.has_permission("Company", ptype="read"))
            has_select = bool(frappe.has_permission("Company", ptype="select"))
        except frappe.DoesNotExistError:
            return {
                "status": "not_available",
                "items": [],
                "returned_count": 0,
                "limit": maximum,
                "truncated": False,
                "complete": True,
            }

        if not (has_read or has_select):
            return {
                "status": "permission_denied",
                "items": [],
                "returned_count": 0,
                "limit": maximum,
                "truncated": False,
                "complete": False,
            }

        fields = ["name"]
        if has_read:
            fields.extend(["company_name", "default_currency", "country"])

        try:
            rows = frappe.get_list(
                "Company",
                fields=fields,
                order_by="name asc",
                limit_start=0,
                limit_page_length=maximum + 1,
                as_list=False,
                strict=True,
            )
        except frappe.PermissionError:
            return {
                "status": "permission_denied",
                "items": [],
                "returned_count": 0,
                "limit": maximum,
                "truncated": False,
                "complete": False,
            }

        if not isinstance(rows, list):
            frappe.throw(_("Company context returned an invalid result"), frappe.ValidationError)

        truncated = len(rows) > maximum
        items = [cls._company_descriptor(row) for row in rows[:maximum]]
        return {
            "status": "incomplete" if truncated else ("resolved" if items else "not_found"),
            "items": items,
            "returned_count": len(items),
            "limit": maximum,
            "truncated": truncated,
            "complete": not truncated,
        }

    @staticmethod
    def _company_descriptor(row: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            frappe.throw(_("Company context returned an invalid row"), frappe.ValidationError)

        descriptor = {"name": row["name"]}
        for fieldname in ("company_name", "default_currency", "country"):
            if fieldname in row:
                descriptor[fieldname] = row[fieldname]
        return descriptor

    @classmethod
    def _resolve_company_default(
        cls,
        extracted: Mapping[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        if extracted["invalid"]:
            return cls._status("invalid", source=source)
        if not extracted["configured"] or not extracted["values"]:
            return cls._status("not_found", source=source)

        candidates = []
        for company in extracted["values"]:
            descriptor = cls._get_authorized_company(company)
            if descriptor is not None:
                candidates.append(descriptor)

        candidates = cls._deduplicate_by_name(candidates)
        if len(candidates) == 1:
            return cls._status("resolved", source=source, value=candidates[0])
        if len(candidates) > 1:
            return cls._status("ambiguous", source=source, candidates=candidates)
        return cls._status("unavailable", source=source)

    @classmethod
    def _get_authorized_company(cls, company: Any) -> dict[str, Any] | None:
        if not isinstance(company, str) or not company:
            return None
        try:
            has_read = bool(frappe.has_permission("Company", ptype="read"))
            has_select = bool(frappe.has_permission("Company", ptype="select"))
        except frappe.DoesNotExistError:
            return None
        if not (has_read or has_select):
            return None

        fields = ["name"]
        if has_read:
            fields.extend(["company_name", "default_currency", "country"])

        rows = frappe.get_list(
            "Company",
            fields=fields,
            filters=[["Company", "name", "=", company]],
            limit_start=0,
            limit_page_length=1,
            as_list=False,
            strict=True,
        )
        if not isinstance(rows, list):
            frappe.throw(_("Company context returned an invalid result"), frappe.ValidationError)
        if not rows:
            return None
        return cls._company_descriptor(rows[0])

    @classmethod
    def _resolve_company_context(
        cls,
        *,
        user_company: Mapping[str, Any],
        global_company: Mapping[str, Any],
        effective_hint: Any,
        available_companies: Mapping[str, Any],
    ) -> dict[str, Any]:
        if user_company["status"] in {"resolved", "ambiguous", "invalid", "unavailable"}:
            return dict(user_company)

        if isinstance(effective_hint, str) and effective_hint:
            effective_company = cls._get_authorized_company(effective_hint)
            if effective_company is not None:
                return cls._status(
                    "resolved",
                    source="effective_frappe_default",
                    value=effective_company,
                )

        if global_company["status"] in {"resolved", "ambiguous", "invalid", "unavailable"}:
            return dict(global_company)

        companies = available_companies["items"]
        if available_companies["truncated"]:
            return cls._status("incomplete", candidates=companies)
        if len(companies) == 1:
            return cls._status(
                "resolved",
                source="only_permitted_company",
                value=companies[0],
            )
        if len(companies) > 1:
            return cls._status("ambiguous", candidates=companies)
        if available_companies["status"] == "permission_denied":
            return cls._status("permission_denied")
        if available_companies["status"] == "not_available":
            return cls._status("not_available")
        return cls._status("not_found")

    @classmethod
    def _resolve_linked_default(
        cls,
        doctype: str,
        extracted: Mapping[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        if extracted["invalid"]:
            return cls._status("invalid", source=source)
        if not extracted["configured"] or not extracted["values"]:
            return cls._status("not_found", source=source)
        try:
            has_read = bool(frappe.has_permission(doctype, ptype="read"))
            has_select = bool(frappe.has_permission(doctype, ptype="select"))
        except frappe.DoesNotExistError:
            return cls._status("not_available", source=source)
        if not (has_read or has_select):
            return cls._status("permission_denied", source=source)

        candidates = []
        for value in extracted["values"]:
            rows = frappe.get_list(
                doctype,
                fields=["name"],
                filters=[[doctype, "name", "=", value]],
                limit_start=0,
                limit_page_length=1,
                as_list=False,
                strict=True,
            )
            if not isinstance(rows, list):
                frappe.throw(
                    _("Default context returned an invalid result"),
                    frappe.ValidationError,
                )
            if rows:
                row = rows[0]
                if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
                    frappe.throw(
                        _("Default context returned an invalid row"),
                        frappe.ValidationError,
                    )
                candidates.append(row["name"])

        candidates = sorted(set(candidates))
        if len(candidates) == 1:
            return cls._status("resolved", source=source, value=candidates[0])
        if len(candidates) > 1:
            return cls._status("ambiguous", source=source, candidates=candidates)
        return cls._status("unavailable", source=source)

    @classmethod
    def _resolve_effective_linked_default(
        cls,
        doctype: str,
        effective_hint: Any,
        user_default: Mapping[str, Any],
        global_default: Mapping[str, Any],
    ) -> dict[str, Any]:
        if user_default["status"] in {"resolved", "ambiguous", "invalid"}:
            return dict(user_default)

        if isinstance(effective_hint, str) and effective_hint:
            resolved = cls._resolve_linked_default(
                doctype,
                {"configured": True, "invalid": False, "values": [effective_hint]},
                source="effective_frappe_default",
            )
            if resolved["status"] == "resolved":
                return resolved

        if global_default["status"] in {"resolved", "ambiguous", "invalid"}:
            return dict(global_default)
        if user_default["status"] == "permission_denied" or global_default["status"] == "permission_denied":
            return cls._status("permission_denied")
        return cls._status("not_found")

    @staticmethod
    def _company_currency_context(company_context: Mapping[str, Any]) -> dict[str, Any]:
        if company_context.get("status") != "resolved":
            return BusinessContextService._status("unresolved_company")

        company = company_context.get("value")
        if not isinstance(company, Mapping) or not company.get("default_currency"):
            return BusinessContextService._status("not_found", source="company")
        return BusinessContextService._status(
            "resolved",
            source="company",
            value=company["default_currency"],
        )

    @classmethod
    def _get_current_fiscal_year(
        cls,
        company_context: Mapping[str, Any],
        maximum: int,
    ) -> dict[str, Any]:
        if company_context.get("status") != "resolved":
            return cls._status("unresolved_company")

        company = company_context.get("value")
        if not isinstance(company, Mapping) or not isinstance(company.get("name"), str):
            return cls._status("unresolved_company")
        company_name = company["name"]

        try:
            fiscal_metadata = MetadataService.get_doctype_metadata("Fiscal Year")
            fiscal_policy = FieldPolicy.get_field_policy(fiscal_metadata)
        except frappe.DoesNotExistError:
            return cls._status("not_available")

        required_fields = {"year", "disabled", "year_start_date", "year_end_date"}
        queryable_fields = fiscal_policy.get("queryable_fields")
        if not isinstance(queryable_fields, list) or not required_fields.issubset(queryable_fields):
            return cls._status("permission_denied")

        as_of_date = today()
        try:
            rows = frappe.get_list(
                "Fiscal Year",
                fields=["name", "year", "year_start_date", "year_end_date"],
                filters=[
                    ["Fiscal Year", "disabled", "=", 0],
                    ["Fiscal Year", "year_start_date", "<=", as_of_date],
                    ["Fiscal Year", "year_end_date", ">=", as_of_date],
                ],
                order_by="year_start_date desc, name asc",
                limit_start=0,
                limit_page_length=maximum + 1,
                as_list=False,
                strict=True,
            )
        except frappe.PermissionError:
            return cls._status("permission_denied")

        if not isinstance(rows, list):
            frappe.throw(_("Fiscal year context returned an invalid result"), frappe.ValidationError)

        if len(rows) > maximum:
            return cls._status(
                "incomplete",
                candidates=[cls._fiscal_year_descriptor(row) for row in rows[:maximum]],
                truncated=True,
            )

        scoped_candidates = []
        for row in rows:
            descriptor = cls._fiscal_year_descriptor(row)
            scope_status = cls._fiscal_year_applies_to_company(
                descriptor["name"],
                company_name,
            )
            if scope_status is None:
                return cls._status("permission_denied")
            if scope_status:
                scoped_candidates.append(descriptor)

        if len(scoped_candidates) == 1:
            return cls._status(
                "resolved",
                source="permission_aware_fiscal_year",
                value=scoped_candidates[0],
            )
        if len(scoped_candidates) > 1:
            return cls._status("ambiguous", candidates=scoped_candidates)
        return cls._status("not_found")

    @staticmethod
    def _fiscal_year_descriptor(row: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            frappe.throw(_("Fiscal year context returned an invalid row"), frappe.ValidationError)

        descriptor = {"name": row["name"]}
        for fieldname in ("year", "year_start_date", "year_end_date"):
            if fieldname not in row:
                frappe.throw(
                    _("Fiscal year fields are no longer permitted"),
                    frappe.PermissionError,
                )
            descriptor[fieldname] = row[fieldname]
        return descriptor

    @classmethod
    def _fiscal_year_applies_to_company(
        cls,
        fiscal_year: str,
        company: str,
    ) -> bool | None:
        try:
            child_metadata = MetadataService.get_doctype_metadata("Fiscal Year Company")
            child_policy = FieldPolicy.get_field_policy(
                child_metadata,
                parent_doctype="Fiscal Year",
                parent_fieldname="companies",
            )
            if "company" not in child_policy.get("queryable_fields", []):
                return None

            base_filters = [
                ["Fiscal Year Company", "parent", "=", fiscal_year],
                ["Fiscal Year Company", "parenttype", "=", "Fiscal Year"],
                ["Fiscal Year Company", "parentfield", "=", "companies"],
            ]
            any_scope = frappe.get_list(
                "Fiscal Year Company",
                fields=["name"],
                filters=base_filters,
                limit_start=0,
                limit_page_length=1,
                parent_doctype="Fiscal Year",
                as_list=False,
                strict=True,
            )
            if not any_scope:
                return True

            company_scope = frappe.get_list(
                "Fiscal Year Company",
                fields=["name"],
                filters=[*base_filters, ["Fiscal Year Company", "company", "=", company]],
                limit_start=0,
                limit_page_length=1,
                parent_doctype="Fiscal Year",
                as_list=False,
                strict=True,
            )
            return bool(company_scope)
        except (frappe.DoesNotExistError, frappe.PermissionError):
            return None

    @classmethod
    def _get_interpretation_defaults(
        cls,
        global_defaults: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "hide_currency_symbol": cls._resolve_boolean_setting(
                global_defaults,
                "hide_currency_symbol",
                yes_no=True,
            ),
            "disable_rounded_total": cls._resolve_boolean_setting(
                global_defaults,
                "disable_rounded_total",
            ),
            "disable_in_words": cls._resolve_boolean_setting(
                global_defaults,
                "disable_in_words",
            ),
        }

    @classmethod
    def _resolve_boolean_setting(
        cls,
        defaults: Mapping[str, Any],
        key: str,
        *,
        yes_no: bool = False,
    ) -> dict[str, Any]:
        if key not in defaults:
            return cls._status("not_found", source="global_default")

        value = defaults[key]
        if isinstance(value, (list, tuple)):
            try:
                unique_values = list(dict.fromkeys(value))
            except TypeError:
                return cls._status("invalid", source="global_default")
            if len(unique_values) != 1:
                return cls._status(
                    "ambiguous",
                    source="global_default",
                    candidates=unique_values,
                )
            value = unique_values[0]

        if yes_no:
            if value == "Yes":
                normalized = True
            elif value == "No" or value == "":
                normalized = False
            else:
                return cls._status("invalid", source="global_default")
        elif isinstance(value, bool):
            normalized = value
        elif value == 0 or value == "0":
            normalized = False
        elif value == 1 or value == "1":
            normalized = True
        else:
            return cls._status("invalid", source="global_default")

        return cls._status("resolved", source="global_default", value=normalized)

    @staticmethod
    def _deduplicate_by_name(values: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        deduplicated = {}
        for value in values:
            deduplicated[value["name"]] = dict(value)
        return [deduplicated[name] for name in sorted(deduplicated)]

    @staticmethod
    def _status(
        status: str,
        *,
        source: str | None = None,
        value: Any = None,
        candidates: list[Any] | None = None,
        truncated: bool = False,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "source": source,
            "value": value,
            "candidates": candidates or [],
            "truncated": truncated,
        }
