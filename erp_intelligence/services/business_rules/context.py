"""Rule definition and the context handed to an evaluator."""

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from erp_intelligence.services.business_rules import constants as c
from erp_intelligence.services.business_rules.data_access import RuleDataAccess
from erp_intelligence.services.business_rules.errors import RuleConfigurationError
from erp_intelligence.services.business_rules.results import build_result

_MISSING = object()


@dataclass(frozen=True)
class RuleDefinition:
    """One resolved assignment: the rule, its effective severity and parameters."""

    rule_code: str
    rule_name: str
    description: str
    rule_category: str
    evaluator_key: str
    execution_stage: str
    severity: str
    is_blocking: bool
    parameters: Mapping[str, Any]
    sequence: int


def normalize_confirmations(confirmations: Any) -> frozenset[str]:
    """Return the set of confirmed keys.

    Accepts a mapping of key to truthy/falsy, or an iterable of keys. A key mapped
    to a falsy value is not confirmed.
    """
    if confirmations is None:
        return frozenset()
    if isinstance(confirmations, Mapping):
        return frozenset(str(key) for key, value in confirmations.items() if value is True)
    if isinstance(confirmations, (str, bytes)) or not isinstance(confirmations, Iterable):
        raise TypeError("confirmations must be a mapping or an iterable of keys")
    return frozenset(str(key) for key in confirmations)


class EvaluationContext:
    """Everything an evaluator may use. Evaluators receive it as their only argument.

    The payload, options and execution result are deep copies, so an evaluator
    cannot change what later rules see. ``data`` is the only way to read ERP data.
    """

    def __init__(
        self,
        *,
        rule: RuleDefinition,
        target_doctype: str,
        operation: str,
        stage: str,
        payload: Mapping[str, Any],
        data: RuleDataAccess,
        confirmations: frozenset[str],
        execution_options: Mapping[str, Any],
        execution_result: Mapping[str, Any],
        today: str,
    ) -> None:
        self.rule = rule
        self.target_doctype = target_doctype
        self.operation = operation
        self.stage = stage
        self.payload: dict[str, Any] = copy.deepcopy(dict(payload))
        self.execution_options: dict[str, Any] = copy.deepcopy(dict(execution_options))
        self.execution_result: dict[str, Any] = copy.deepcopy(dict(execution_result))
        self.data = data
        self.confirmations = confirmations
        self.today = today

    # ------------------------------------------------------------- parameters
    def param(self, name: str, default: Any = _MISSING) -> Any:
        """Return an effective parameter. Raises ``RuleConfigurationError`` if absent."""
        if name in self.rule.parameters:
            return copy.deepcopy(self.rule.parameters[name])
        if default is _MISSING:
            raise RuleConfigurationError(
                f"Rule {self.rule.rule_code} has no parameter {name!r}"
            )
        return default

    def required_param(self, name: str) -> Any:
        """Return a parameter that must have a value. ``None`` or empty is a configuration error."""
        value = self.param(name)
        if value is None or value == "" or value == []:
            raise RuleConfigurationError(
                f"Rule {self.rule.rule_code} parameter {name!r} is not configured"
            )
        return value

    def is_confirmed(self, key: str) -> bool:
        return key in self.confirmations

    # ---------------------------------------------------------------- results
    def result(
        self,
        status: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        required_inputs: list[str] | None = None,
        requires_confirmation: bool | None = None,
    ) -> dict[str, Any]:
        """Build a standard result for this rule."""
        return build_result(
            rule_code=self.rule.rule_code,
            status=status,
            severity=self.rule.severity,
            is_blocking=self.rule.is_blocking,
            message=message,
            details=details,
            required_inputs=required_inputs,
            requires_confirmation=requires_confirmation,
        )

    def passed(self, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.result(c.PASS, message, details=details)

    def failed(self, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.result(c.FAIL, message, details=details)

    def warning(self, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.result(c.WARNING, message, details=details)

    def needs_input(
        self,
        message: str,
        required_inputs: list[str],
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.result(
            c.NEEDS_INPUT, message, details=details, required_inputs=required_inputs
        )

    def needs_confirmation(
        self,
        message: str,
        confirmation_keys: list[str],
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = {**(details or {}), "confirmation_keys": list(confirmation_keys)}
        return self.result(c.NEEDS_CONFIRMATION, message, details=merged)
