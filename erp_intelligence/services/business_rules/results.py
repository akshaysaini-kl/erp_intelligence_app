"""The standard evaluator result contract."""

from collections.abc import Mapping
from typing import Any

from erp_intelligence.services.business_rules.constants import (
    NEEDS_CONFIRMATION,
    NEEDS_INPUT,
    RESULT_FIELDS,
    RESULT_STATUSES,
    SEVERITIES,
)
from erp_intelligence.services.business_rules.errors import EvaluatorContractError
from erp_intelligence.utils.response_utils import to_json_safe


def build_result(
    *,
    rule_code: str,
    status: str,
    severity: str,
    is_blocking: bool,
    message: str,
    details: Mapping[str, Any] | None = None,
    required_inputs: list[str] | None = None,
    requires_confirmation: bool | None = None,
) -> dict[str, Any]:
    """Build one standard result.

    ``requires_confirmation`` defaults to True exactly when the status is
    NEEDS_CONFIRMATION.
    """
    if requires_confirmation is None:
        requires_confirmation = status == NEEDS_CONFIRMATION
    return {
        "rule_code": rule_code,
        "status": status,
        "severity": severity,
        "is_blocking": bool(is_blocking),
        "message": message,
        "details": dict(details) if details else {},
        "required_inputs": list(required_inputs) if required_inputs else [],
        "requires_confirmation": requires_confirmation,
    }


def validate_result(
    raw: Any,
    *,
    rule_code: str,
    severity: str,
    is_blocking: bool,
) -> dict[str, Any]:
    """Check an evaluator's return value against the contract and return a clean copy.

    Raises ``EvaluatorContractError`` when the value is not a valid result, or when
    it contradicts the rule it was produced for. Details are converted to JSON-safe
    values with the shared ERP Intelligence conversion.
    """
    if not isinstance(raw, Mapping):
        raise EvaluatorContractError("Evaluator did not return a mapping")

    keys = set(raw)
    missing = [name for name in RESULT_FIELDS if name not in keys]
    unexpected = sorted(str(key) for key in keys - set(RESULT_FIELDS))
    if missing:
        raise EvaluatorContractError(f"Result is missing fields: {', '.join(missing)}")
    if unexpected:
        raise EvaluatorContractError(f"Result has unexpected fields: {', '.join(unexpected)}")

    if raw["rule_code"] != rule_code:
        raise EvaluatorContractError(
            f"Result rule_code {raw['rule_code']!r} does not match rule {rule_code!r}"
        )
    status = raw["status"]
    if status not in RESULT_STATUSES:
        raise EvaluatorContractError(f"Result status {status!r} is not supported")
    if raw["severity"] not in SEVERITIES or raw["severity"] != severity:
        raise EvaluatorContractError("Result severity does not match the effective rule severity")
    if not isinstance(raw["is_blocking"], bool) or raw["is_blocking"] != is_blocking:
        raise EvaluatorContractError("Result is_blocking does not match the rule")
    message = raw["message"]
    if not isinstance(message, str) or not message.strip():
        raise EvaluatorContractError("Result message must be a non-empty string")
    if not isinstance(raw["details"], Mapping):
        raise EvaluatorContractError("Result details must be a mapping")

    required_inputs = raw["required_inputs"]
    if not isinstance(required_inputs, (list, tuple)) or not all(
        isinstance(item, str) and item for item in required_inputs
    ):
        raise EvaluatorContractError("Result required_inputs must be a list of non-empty strings")
    requires_confirmation = raw["requires_confirmation"]
    if not isinstance(requires_confirmation, bool):
        raise EvaluatorContractError("Result requires_confirmation must be a boolean")

    # Status and flag consistency. A pending input or confirmation can never be
    # presented as a pass.
    if status == NEEDS_INPUT and not required_inputs:
        raise EvaluatorContractError("A NEEDS_INPUT result must list required_inputs")
    if status != NEEDS_INPUT and required_inputs:
        raise EvaluatorContractError("Only a NEEDS_INPUT result may list required_inputs")
    if (status == NEEDS_CONFIRMATION) != requires_confirmation:
        raise EvaluatorContractError(
            "requires_confirmation must be true exactly for NEEDS_CONFIRMATION results"
        )
    if status == NEEDS_CONFIRMATION:
        keys_pending = raw["details"].get("confirmation_keys")
        if (
            not isinstance(keys_pending, (list, tuple))
            or not keys_pending
            or not all(isinstance(item, str) and item for item in keys_pending)
        ):
            raise EvaluatorContractError(
                "A NEEDS_CONFIRMATION result must list details.confirmation_keys"
            )

    try:
        details = to_json_safe(dict(raw["details"]))
    except (TypeError, ValueError) as exc:
        raise EvaluatorContractError(f"Result details are not JSON-safe: {exc}") from None

    return {
        "rule_code": rule_code,
        "status": status,
        "severity": severity,
        "is_blocking": is_blocking,
        "message": message,
        "details": details,
        "required_inputs": list(required_inputs),
        "requires_confirmation": requires_confirmation,
    }
