"""Parsing and merging of rule parameters.

Parameters are plain JSON data. They are never evaluated or executed.
"""

import copy
import json
from collections.abc import Mapping
from typing import Any

from erp_intelligence.services.business_rules.errors import RuleConfigurationError


def parse_json_object(value: Any, label: str) -> dict[str, Any]:
    """Return ``value`` as a dict. Empty values give an empty dict.

    Accepts a JSON string or a mapping. Anything else, including a JSON array or
    scalar, raises ``RuleConfigurationError``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise RuleConfigurationError(f"{label} is not valid JSON: {exc}") from None
    if not isinstance(value, Mapping):
        raise RuleConfigurationError(f"{label} must be a JSON object")
    return copy.deepcopy(dict(value))


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``override`` onto ``base``. Nested objects merge; lists and scalars replace."""
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def validate_override_keys(
    defaults: Mapping[str, Any],
    override: Mapping[str, Any],
    label: str,
) -> None:
    """Reject override keys that the rule's default parameters do not declare.

    This catches typos that would otherwise leave a parameter silently unchanged.
    Only top-level keys are checked.
    """
    unknown = sorted(set(override) - set(defaults))
    if unknown:
        raise RuleConfigurationError(
            f"{label} overrides parameters the rule does not declare: {', '.join(unknown)}"
        )


def resolve_parameters(
    default_parameters: Any,
    parameters_override: Any,
    label: str,
) -> dict[str, Any]:
    """Return the effective parameters: rule defaults with the assignment override applied."""
    defaults = parse_json_object(default_parameters, f"{label} default parameters")
    override = parse_json_object(parameters_override, f"{label} parameters override")
    validate_override_keys(defaults, override, label)
    return deep_merge(defaults, override)
