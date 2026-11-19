"""Helpers shared by evaluators. Generic: nothing here refers to a specific DocType."""

import datetime
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from erp_intelligence.services.business_rules import constants as c
from erp_intelligence.services.business_rules.context import EvaluationContext

_MAX_MESSAGES_IN_SUMMARY = 3


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def to_decimal(value: Any) -> Decimal | None:
    """Return ``value`` as a finite Decimal, or None when it is not a plain number.

    Booleans are not numbers. Floats are converted through their string form so
    ``0.1`` stays ``0.1``.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float, str)):
        try:
            number = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
    else:
        return None
    return number if number.is_finite() else None


def same_number(left: Any, right: Any, tolerance: Decimal = Decimal("0.0001")) -> bool:
    first, second = to_decimal(left), to_decimal(right)
    if first is None or second is None:
        return False
    return abs(first - second) <= tolerance


def parse_iso_date(value: Any) -> datetime.date | None:
    """Return a date for a ``YYYY-MM-DD`` string (a longer datetime string uses its date part)."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 10:
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return None


class Findings:
    """Collect the problems one rule found, then turn them into one result.

    The result status is the most serious finding: FAIL, then NEEDS_INPUT, then
    NEEDS_CONFIRMATION, then WARNING, then PASS. Every finding stays in
    ``details["issues"]`` whichever status wins, so nothing is hidden.
    """

    def __init__(self) -> None:
        self._failures: list[dict[str, Any]] = []
        self._inputs: list[dict[str, Any]] = []
        self._confirmations: list[dict[str, Any]] = []
        self._warnings: list[dict[str, Any]] = []

    def fail(self, message: str, **details: Any) -> None:
        self._failures.append({"message": message, **details})

    def need(self, input_name: str, message: str, **details: Any) -> None:
        self._inputs.append({"input": input_name, "message": message, **details})

    def confirm(self, key: str, message: str, **details: Any) -> None:
        self._confirmations.append({"confirmation_key": key, "message": message, **details})

    def warn(self, message: str, **details: Any) -> None:
        self._warnings.append({"message": message, **details})

    @property
    def clean(self) -> bool:
        return not (self._failures or self._inputs or self._confirmations or self._warnings)

    def result(
        self,
        context: EvaluationContext,
        pass_message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        base = dict(details or {})
        issues = {
            "failures": self._failures,
            "missing_inputs": self._inputs,
            "pending_confirmations": self._confirmations,
            "warnings": self._warnings,
        }
        if any(issues.values()):
            base["issues"] = {name: items for name, items in issues.items() if items}

        if self._failures:
            return context.failed(self._summary(self._failures), base)
        if self._inputs:
            inputs = list(dict.fromkeys(item["input"] for item in self._inputs))
            return context.needs_input(self._summary(self._inputs), inputs, base)
        if self._confirmations:
            keys = list(dict.fromkeys(item["confirmation_key"] for item in self._confirmations))
            return context.needs_confirmation(self._summary(self._confirmations), keys, base)
        if self._warnings:
            return context.warning(self._summary(self._warnings), base)
        return context.result(c.PASS, pass_message, details=base)

    @staticmethod
    def _summary(items: list[dict[str, Any]]) -> str:
        messages = [item["message"] for item in items[:_MAX_MESSAGES_IN_SUMMARY]]
        extra = len(items) - len(messages)
        text = "; ".join(messages)
        return f"{text} (+{extra} more)" if extra > 0 else text
