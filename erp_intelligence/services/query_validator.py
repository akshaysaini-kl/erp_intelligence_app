"""Validate structured, permission-aware record query requests."""

import datetime
import math
import re
from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _

from erp_intelligence.services.config_service import ConfigService
from erp_intelligence.services.field_policy import FieldPolicy
from erp_intelligence.services.metadata_service import MetadataService


class QueryValidator:
    """Build normalized query plans without executing database queries."""

    # Page length used when a request omits one. It is capped by the effective
    # page_length limit. Limits themselves are resolved by ConfigService.
    DEFAULT_PAGE_LENGTH = 20

    _LIMIT_NAMES = (
        "page_length",
        "offset",
        "fields",
        "filters",
        "sort_fields",
        "filter_values",
        "string_length",
    )

    _ALLOWED_REQUEST_KEYS = frozenset(
        {
            "doctype",
            "fields",
            "filters",
            "sort",
            "page_length",
            "offset",
            "parent_doctype",
            "parent_fieldname",
        }
    )
    _ALLOWED_FILTER_KEYS = frozenset({"field", "operator", "value"})
    _ALLOWED_SORT_KEYS = frozenset({"field", "direction"})
    _SUPPORTED_OPERATORS = frozenset(
        {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "between", "like", "not like", "is"}
    )
    _ORDERED_OPERATORS = frozenset({">", ">=", "<", "<=", "between"})
    _ORDERED_FIELDTYPES = frozenset(
        {
            "Currency",
            "Date",
            "Datetime",
            "Duration",
            "Float",
            "Int",
            "Long Int",
            "Percent",
            "Rating",
            "Time",
        }
    )
    _TEXT_FIELDTYPES = frozenset(
        {
            "Autocomplete",
            "Data",
            "Dynamic Link",
            "Link",
            "Phone",
            "Select",
            "Small Text",
            "Text",
        }
    )
    _INTEGER_FIELDTYPES = frozenset({"Int", "Long Int"})
    _NUMBER_FIELDTYPES = frozenset(
        {"Currency", "Duration", "Float", "Percent", "Rating"}
    )
    _FIELDNAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    _DATETIME_PATTERN = re.compile(
        r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?$"
    )
    _TIME_PATTERN = re.compile(r"^\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?$")
    _CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")

    @classmethod
    def validate_query(
        cls,
        request: Mapping[str, Any],
        *,
        user: str | None = None,
        limit_snapshot: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Validate a structured request and return a normalized query plan.

        ``limit_snapshot`` is the request-scoped snapshot from
        ``ConfigService.get_limit_snapshot``. When omitted, limits are resolved
        from site configuration for this call.
        """
        cls._validate_request_shape(request)

        metadata = MetadataService.get_doctype_metadata(request.get("doctype"))
        parent_doctype, parent_fieldname = cls._validate_parent_context(request, metadata)
        policy = FieldPolicy.get_field_policy(
            metadata,
            user=user,
            parent_doctype=parent_doctype,
            parent_fieldname=parent_fieldname,
        )

        field_map = {
            field["fieldname"]: field
            for field in metadata["fields"]
            if isinstance(field, Mapping) and isinstance(field.get("fieldname"), str)
        }
        queryable_fields = policy.get("queryable_fields")
        if not isinstance(queryable_fields, list):
            frappe.throw(_("Unable to establish queryable fields"), frappe.PermissionError)
        queryable_field_set = set(queryable_fields)

        limits = cls._get_limits(limit_snapshot)
        fields = cls._validate_fields(
            request.get("fields"),
            field_map,
            queryable_field_set,
            limits["fields"],
        )
        filters = cls._validate_filters(
            request.get("filters", []),
            field_map,
            queryable_field_set,
            limits,
        )
        sort = cls._validate_sort(
            request.get("sort", []),
            field_map,
            queryable_field_set,
            limits["sort_fields"],
        )
        page_length, offset = cls._validate_pagination(
            request.get("page_length"),
            request.get("offset", 0),
            limits,
        )

        return {
            "doctype": metadata["doctype"],
            "fields": fields,
            "filters": filters,
            "sort": sort,
            "page_length": page_length,
            "offset": offset,
            "parent_doctype": parent_doctype,
            "parent_fieldname": parent_fieldname,
        }

    @classmethod
    def _validate_request_shape(cls, request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping):
            frappe.throw(_("Query request must be a mapping"), frappe.ValidationError)

        unknown_keys = set(request) - cls._ALLOWED_REQUEST_KEYS
        if unknown_keys:
            frappe.throw(
                _("Unsupported query request keys: {0}").format(
                    ", ".join(sorted(str(key) for key in unknown_keys))
                ),
                frappe.ValidationError,
            )

        if "doctype" not in request:
            frappe.throw(_("Query request requires a doctype"), frappe.ValidationError)
        if "fields" not in request:
            frappe.throw(_("Query request requires fields"), frappe.ValidationError)

    @classmethod
    def _validate_parent_context(
        cls,
        request: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> tuple[str | None, str | None]:
        parent_doctype = cls._optional_string(request.get("parent_doctype"), "parent_doctype")
        parent_fieldname = cls._optional_string(
            request.get("parent_fieldname"),
            "parent_fieldname",
        )

        if bool(parent_doctype) != bool(parent_fieldname):
            frappe.throw(
                _("parent_doctype and parent_fieldname must be provided together"),
                frappe.ValidationError,
            )

        if metadata.get("istable"):
            if not parent_doctype or not parent_fieldname:
                frappe.throw(
                    _("Child DocType queries require parent context"),
                    frappe.ValidationError,
                )
            cls._validate_field_identifier(parent_fieldname)
        elif parent_doctype or parent_fieldname:
            frappe.throw(
                _("Parent context is only valid for child DocTypes"),
                frappe.ValidationError,
            )

        return parent_doctype, parent_fieldname

    @classmethod
    def _validate_fields(
        cls,
        fields: Any,
        field_map: Mapping[str, Mapping[str, Any]],
        queryable_fields: set[str],
        maximum: int,
    ) -> list[str]:
        if not isinstance(fields, list) or not fields:
            frappe.throw(_("fields must be a non-empty list"), frappe.ValidationError)
        if len(fields) > maximum:
            frappe.throw(
                _("fields cannot contain more than {0} entries").format(maximum),
                frappe.ValidationError,
            )

        normalized_fields = []
        seen = set()
        for fieldname in fields:
            fieldname = cls._validate_field_identifier(fieldname)
            if fieldname in seen:
                frappe.throw(
                    _("Duplicate requested field: {0}").format(fieldname),
                    frappe.ValidationError,
                )
            cls._require_queryable_field(fieldname, field_map, queryable_fields)
            seen.add(fieldname)
            normalized_fields.append(fieldname)

        return normalized_fields

    @classmethod
    def _validate_filters(
        cls,
        filters: Any,
        field_map: Mapping[str, Mapping[str, Any]],
        queryable_fields: set[str],
        limits: Mapping[str, int],
    ) -> list[dict[str, Any]]:
        if filters is None:
            filters = []
        if not isinstance(filters, list):
            frappe.throw(_("filters must be a list"), frappe.ValidationError)
        if len(filters) > limits["filters"]:
            frappe.throw(
                _("filters cannot contain more than {0} entries").format(limits["filters"]),
                frappe.ValidationError,
            )

        normalized_filters = []
        for filter_value in filters:
            if not isinstance(filter_value, Mapping):
                frappe.throw(_("Each filter must be a mapping"), frappe.ValidationError)
            if set(filter_value) != cls._ALLOWED_FILTER_KEYS:
                frappe.throw(
                    _("Each filter must contain only field, operator, and value"),
                    frappe.ValidationError,
                )

            fieldname = cls._validate_field_identifier(filter_value.get("field"))
            field = cls._require_queryable_field(fieldname, field_map, queryable_fields)
            operator = cls._validate_operator(filter_value.get("operator"), field)
            value = cls._validate_filter_value(
                operator,
                filter_value.get("value"),
                field,
                limits,
            )
            normalized_filters.append(
                {"field": fieldname, "operator": operator, "value": value}
            )

        return normalized_filters

    @classmethod
    def _validate_sort(
        cls,
        sort: Any,
        field_map: Mapping[str, Mapping[str, Any]],
        queryable_fields: set[str],
        maximum: int,
    ) -> list[dict[str, str]]:
        if sort is None:
            sort = []
        if not isinstance(sort, list):
            frappe.throw(_("sort must be a list"), frappe.ValidationError)
        if len(sort) > maximum:
            frappe.throw(
                _("sort cannot contain more than {0} entries").format(maximum),
                frappe.ValidationError,
            )

        normalized_sort = []
        seen = set()
        for sort_value in sort:
            if not isinstance(sort_value, Mapping):
                frappe.throw(_("Each sort entry must be a mapping"), frappe.ValidationError)
            if set(sort_value) != cls._ALLOWED_SORT_KEYS:
                frappe.throw(
                    _("Each sort entry must contain only field and direction"),
                    frappe.ValidationError,
                )

            fieldname = cls._validate_field_identifier(sort_value.get("field"))
            cls._require_queryable_field(fieldname, field_map, queryable_fields)
            if fieldname in seen:
                frappe.throw(
                    _("Duplicate sort field: {0}").format(fieldname),
                    frappe.ValidationError,
                )

            direction = sort_value.get("direction")
            if not isinstance(direction, str) or direction.lower() not in {"asc", "desc"}:
                frappe.throw(
                    _("Sort direction must be asc or desc"),
                    frappe.ValidationError,
                )

            seen.add(fieldname)
            normalized_sort.append(
                {"field": fieldname, "direction": direction.lower()}
            )

        return normalized_sort

    @classmethod
    def _validate_filter_value(
        cls,
        operator: str,
        value: Any,
        field: Mapping[str, Any],
        limits: Mapping[str, int],
    ) -> Any:
        fieldtype = field.get("fieldtype")

        if operator == "is":
            if not isinstance(value, str) or value.lower() not in {"set", "not set"}:
                frappe.throw(
                    _("The is operator requires set or not set"),
                    frappe.ValidationError,
                )
            return value.lower()

        if operator in {"in", "not in"}:
            if not isinstance(value, list) or not value:
                frappe.throw(
                    _("The {0} operator requires a non-empty list").format(operator),
                    frappe.ValidationError,
                )
            if len(value) > limits["filter_values"]:
                frappe.throw(
                    _("Filter lists cannot contain more than {0} values").format(
                        limits["filter_values"]
                    ),
                    frappe.ValidationError,
                )
            return [
                cls._normalize_scalar(item, field, limits["string_length"])
                for item in value
            ]

        if operator == "between":
            if not isinstance(value, list) or len(value) != 2:
                frappe.throw(
                    _("The between operator requires exactly two values"),
                    frappe.ValidationError,
                )
            normalized = [
                cls._normalize_scalar(item, field, limits["string_length"])
                for item in value
            ]
            if normalized[0] > normalized[1]:
                frappe.throw(
                    _("The lower between value cannot exceed the upper value"),
                    frappe.ValidationError,
                )
            return normalized

        if operator in {"like", "not like"}:
            return cls._normalize_text(value, limits["string_length"])

        return cls._normalize_scalar(value, field, limits["string_length"])

    @classmethod
    def _validate_operator(cls, operator: Any, field: Mapping[str, Any]) -> str:
        if not isinstance(operator, str):
            frappe.throw(_("Filter operator must be a string"), frappe.ValidationError)

        operator = operator.lower()
        if operator not in cls._SUPPORTED_OPERATORS:
            frappe.throw(
                _("Unsupported filter operator: {0}").format(operator),
                frappe.ValidationError,
            )

        fieldtype = field.get("fieldtype")
        if operator in cls._ORDERED_OPERATORS and fieldtype not in cls._ORDERED_FIELDTYPES:
            frappe.throw(
                _("Operator {0} is not valid for field type {1}").format(
                    operator,
                    fieldtype,
                ),
                frappe.ValidationError,
            )
        if operator in {"like", "not like"} and fieldtype not in cls._TEXT_FIELDTYPES:
            frappe.throw(
                _("Operator {0} is not valid for field type {1}").format(
                    operator,
                    fieldtype,
                ),
                frappe.ValidationError,
            )

        return operator

    @classmethod
    def _normalize_scalar(
        cls,
        value: Any,
        field: Mapping[str, Any],
        maximum_string_length: int,
    ) -> Any:
        fieldtype = field.get("fieldtype")
        if value is None or isinstance(value, (list, tuple, Mapping)):
            frappe.throw(_("Filter value must be a scalar"), frappe.ValidationError)

        if fieldtype in cls._TEXT_FIELDTYPES:
            value = cls._normalize_text(value, maximum_string_length)
            if fieldtype == "Select":
                cls._validate_select_value(value, field)
            return value

        if fieldtype in cls._INTEGER_FIELDTYPES:
            if isinstance(value, bool) or not isinstance(value, int):
                frappe.throw(
                    _("Filter value for {0} must be an integer").format(fieldtype),
                    frappe.ValidationError,
                )
            return value

        if fieldtype in cls._NUMBER_FIELDTYPES:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                frappe.throw(
                    _("Filter value for {0} must be numeric").format(fieldtype),
                    frappe.ValidationError,
                )
            try:
                is_finite = math.isfinite(value)
            except (OverflowError, TypeError):
                is_finite = False
            if not is_finite:
                frappe.throw(_("Numeric filter value must be finite"), frappe.ValidationError)
            return value

        if fieldtype == "Check":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int) and value in {0, 1}:
                return value
            frappe.throw(
                _("Filter value for Check must be true, false, 0, or 1"),
                frappe.ValidationError,
            )

        if fieldtype == "Date":
            return cls._normalize_date(value)
        if fieldtype == "Datetime":
            return cls._normalize_datetime(value)
        if fieldtype == "Time":
            return cls._normalize_time(value)

        frappe.throw(
            _("Field type {0} is not supported in filters").format(fieldtype),
            frappe.ValidationError,
        )

    @classmethod
    def _normalize_text(cls, value: Any, maximum: int) -> str:
        if not isinstance(value, str) or not value:
            frappe.throw(_("Filter value must be a non-empty string"), frappe.ValidationError)
        if len(value) > maximum:
            frappe.throw(
                _("Filter string cannot exceed {0} characters").format(maximum),
                frappe.ValidationError,
            )
        if cls._CONTROL_CHARACTER_PATTERN.search(value):
            frappe.throw(
                _("Filter string contains control characters"),
                frappe.ValidationError,
            )
        return value

    @staticmethod
    def _validate_select_value(value: str, field: Mapping[str, Any]) -> None:
        options = field.get("options")
        if not isinstance(options, str) or not options:
            return

        allowed_values = {option.strip() for option in options.splitlines() if option.strip()}
        if allowed_values and value not in allowed_values:
            frappe.throw(_("Filter value is not a valid Select option"), frappe.ValidationError)

    @classmethod
    def _normalize_date(cls, value: Any) -> str:
        if not isinstance(value, str) or not cls._DATE_PATTERN.fullmatch(value):
            frappe.throw(_("Date filter values must use YYYY-MM-DD"), frappe.ValidationError)
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            frappe.throw(_("Invalid Date filter value"), frappe.ValidationError)

    @classmethod
    def _normalize_datetime(cls, value: Any) -> str:
        if not isinstance(value, str) or not cls._DATETIME_PATTERN.fullmatch(value):
            frappe.throw(
                _("Datetime filter values must use an ISO date and time"),
                frappe.ValidationError,
            )
        try:
            normalized = datetime.datetime.fromisoformat(value)
        except ValueError:
            frappe.throw(_("Invalid Datetime filter value"), frappe.ValidationError)

        if normalized.tzinfo is not None:
            frappe.throw(
                _("Timezone-aware Datetime filter values are not supported"),
                frappe.ValidationError,
            )
        return normalized.isoformat(sep=" ")

    @classmethod
    def _normalize_time(cls, value: Any) -> str:
        if not isinstance(value, str) or not cls._TIME_PATTERN.fullmatch(value):
            frappe.throw(_("Time filter values must use HH:MM[:SS]"), frappe.ValidationError)
        try:
            normalized = datetime.time.fromisoformat(value)
        except ValueError:
            frappe.throw(_("Invalid Time filter value"), frappe.ValidationError)

        if normalized.tzinfo is not None:
            frappe.throw(
                _("Timezone-aware Time filter values are not supported"),
                frappe.ValidationError,
            )
        return normalized.isoformat()

    @classmethod
    def _validate_pagination(
        cls,
        page_length: Any,
        offset: Any,
        limits: Mapping[str, int],
    ) -> tuple[int, int]:
        if page_length is None:
            page_length = min(cls.DEFAULT_PAGE_LENGTH, limits["page_length"])
        if isinstance(page_length, bool) or not isinstance(page_length, int):
            frappe.throw(_("page_length must be an integer"), frappe.ValidationError)
        if page_length < 1 or page_length > limits["page_length"]:
            frappe.throw(
                _("page_length must be between 1 and {0}").format(limits["page_length"]),
                frappe.ValidationError,
            )

        if isinstance(offset, bool) or not isinstance(offset, int):
            frappe.throw(_("offset must be an integer"), frappe.ValidationError)
        if offset < 0 or offset > limits["offset"]:
            frappe.throw(
                _("offset must be between 0 and {0}").format(limits["offset"]),
                frappe.ValidationError,
            )

        return page_length, offset

    @classmethod
    def _get_limits(cls, limit_snapshot: Mapping[str, int] | None) -> dict[str, int]:
        return ConfigService.resolve_limits(*cls._LIMIT_NAMES, snapshot=limit_snapshot)

    @classmethod
    def _validate_field_identifier(cls, fieldname: Any) -> str:
        if not isinstance(fieldname, str) or not cls._FIELDNAME_PATTERN.fullmatch(fieldname):
            frappe.throw(_("Invalid query field reference"), frappe.ValidationError)
        return fieldname

    @staticmethod
    def _require_queryable_field(
        fieldname: str,
        field_map: Mapping[str, Mapping[str, Any]],
        queryable_fields: set[str],
    ) -> Mapping[str, Any]:
        field = field_map.get(fieldname)
        if field is None:
            frappe.throw(
                _("Invalid field for DocType: {0}").format(fieldname),
                frappe.ValidationError,
            )
        if fieldname not in queryable_fields:
            frappe.throw(
                _("Field is not permitted in queries: {0}").format(fieldname),
                frappe.PermissionError,
            )
        return field

    @staticmethod
    def _optional_string(value: Any, fieldname: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            frappe.throw(
                _("{0} must be a non-empty string").format(fieldname),
                frappe.ValidationError,
            )
        return value.strip()
