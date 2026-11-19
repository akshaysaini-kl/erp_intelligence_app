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
        # Child-table aggregation. The defaults are PROVISIONAL: they have not been
        # benchmarked yet and must be reviewed against measured results. The ceilings
        # are safety bounds and do not move when a default is tuned. The number of
        # rows a ranking may return reuses "returned_groups".
        "child_agg_parent_scan": LimitDefinition(
            "erp_intelligence_max_child_agg_parent_scan",
            5_000,
            1,
            50_000,
        ),
        "child_agg_parent_batch": LimitDefinition(
            "erp_intelligence_child_agg_parent_batch",
            500,
            1,
            1_000,
        ),
        "child_agg_top_n": LimitDefinition(
            "erp_intelligence_max_child_agg_top_n",
            20,
            1,
            100,
        ),
        "child_agg_max_seconds": LimitDefinition(
            "erp_intelligence_max_child_agg_seconds",
            15,
            1,
            60,
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
# Child-aggregation limits are excluded too (see CHILD_AGGREGATION_LIMIT_NAMES): every
# request resolves and validates every name in this tuple, so a malformed setting for a
# feature that most requests never use must not be able to break unrelated requests.
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

# Limits that only child-table aggregation (a request carrying ``parent_scope``) uses.
# Their definitions stay in LIMIT_DEFINITIONS, so they keep their defaults, minimums and
# ceilings and are validated exactly like every other limit. They are resolved and
# validated only when a child aggregation runs, through
# ``ConfigService.get_child_aggregation_limit_snapshot``, which merges them into the
# request snapshot.
CHILD_AGGREGATION_LIMIT_NAMES = (
    "child_agg_parent_scan",
    "child_agg_parent_batch",
    "child_agg_top_n",
    "child_agg_max_seconds",
)
