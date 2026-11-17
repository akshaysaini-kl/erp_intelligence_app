"""Shared immutable constants for ERP Intelligence query contracts."""

from types import MappingProxyType
from typing import NamedTuple

FIELDNAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

SUPPORTED_QUERY_FILTER_OPERATORS = (
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
)

SORT_DIRECTIONS = ("asc", "desc")

SUPPORTED_AGGREGATION_OPERATIONS = (
    "count",
    "sum",
    "avg",
    "min",
    "max",
)

QUERYABLE_SCALAR_FIELD_TYPES = frozenset(
    {
        "Autocomplete",
        "Check",
        "Currency",
        "Data",
        "Date",
        "Datetime",
        "Duration",
        "Dynamic Link",
        "Float",
        "Int",
        "Link",
        "Long Int",
        "Percent",
        "Phone",
        "Rating",
        "Select",
        "Small Text",
        "Text",
        "Time",
    }
)

NUMERIC_AGGREGATION_FIELD_TYPES = frozenset(
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

SUPPORTED_AGGREGATION_FIELD_TYPES = QUERYABLE_SCALAR_FIELD_TYPES

AGGREGATION_FIELD_TYPES_BY_OPERATION = MappingProxyType(
    {
        "count": SUPPORTED_AGGREGATION_FIELD_TYPES,
        "sum": NUMERIC_AGGREGATION_FIELD_TYPES,
        "avg": NUMERIC_AGGREGATION_FIELD_TYPES,
        "min": SUPPORTED_AGGREGATION_FIELD_TYPES,
        "max": SUPPORTED_AGGREGATION_FIELD_TYPES,
    }
)

LAYOUT_FIELD_TYPES = frozenset(
    {
        "Button",
        "Column Break",
        "Fold",
        "Heading",
        "HTML",
        "Image",
        "Section Break",
        "Tab Break",
    }
)

TABLE_FIELD_TYPES = frozenset({"Table", "Table MultiSelect"})
NON_VALUE_FIELD_TYPES = LAYOUT_FIELD_TYPES | TABLE_FIELD_TYPES

SENSITIVE_FIELD_TYPES = frozenset({"Password"})

RESPONSE_METADATA_KEYS = frozenset(
    {"result_type", "doctype", "returned_count", "has_more", "truncated"}
)


class LimitDefinition(NamedTuple):
    """Immutable definition of one tunable limit.

    ``default`` applies when the site does not configure ``config_key``.
    ``ceiling`` is the application safety maximum. Site configuration may lower
    or tune a limit within ``minimum``..``ceiling`` and can never exceed it.
    """

    config_key: str
    default: int
    minimum: int
    ceiling: int


# Names are shared by every service that enforces a limit, so a given limit has
# one effective value per request. Resolve through ConfigService.get_limit().
LIMIT_DEFINITIONS = MappingProxyType(
    {
        # Record queries
        "page_length": LimitDefinition("erp_intelligence_max_page_length", 100, 1, 500),
        "offset": LimitDefinition("erp_intelligence_max_offset", 100_000, 0, 1_000_000),
        "fields": LimitDefinition("erp_intelligence_max_fields", 50, 1, 100),
        "filters": LimitDefinition("erp_intelligence_max_filters", 50, 1, 100),
        "sort_fields": LimitDefinition("erp_intelligence_max_sort_fields", 3, 1, 5),
        "filter_values": LimitDefinition("erp_intelligence_max_filter_values", 100, 1, 500),
        "parent_names": LimitDefinition("erp_intelligence_max_parent_names", 50, 1, 200),
        "string_length": LimitDefinition(
            "erp_intelligence_max_filter_string_length",
            1_000,
            1,
            4_000,
        ),
        # Aggregations
        "aggregations": LimitDefinition("erp_intelligence_max_aggregations", 10, 1, 20),
        "group_by_fields": LimitDefinition("erp_intelligence_max_group_by_fields", 3, 1, 5),
        "returned_groups": LimitDefinition("erp_intelligence_max_returned_groups", 100, 1, 500),
        "aggregate_complexity": LimitDefinition(
            "erp_intelligence_max_aggregate_complexity",
            100,
            1,
            250,
        ),
        # Business context
        "business_context_companies": LimitDefinition(
            "erp_intelligence_business_context_max_companies",
            25,
            1,
            100,
        ),
        "business_context_fiscal_years": LimitDefinition(
            "erp_intelligence_business_context_max_fiscal_year_candidates",
            10,
            1,
            50,
        ),
    }
)

# Limits enforced while planning and executing one query or aggregation request.
# Business-context limits are excluded because they belong to a separate call.
QUERY_LIMIT_NAMES = (
    "page_length",
    "offset",
    "fields",
    "filters",
    "sort_fields",
    "filter_values",
    "parent_names",
    "string_length",
    "aggregations",
    "group_by_fields",
    "returned_groups",
    "aggregate_complexity",
)
