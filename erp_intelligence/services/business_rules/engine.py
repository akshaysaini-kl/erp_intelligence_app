"""Generic Business Rules Engine.

The engine resolves the enabled Business Rule Set for a target DocType and
operation, runs the evaluators of the assigned rules for one execution stage, and
returns a structured report with a decision for the caller.

It is read-only. It never creates, changes, submits or deletes an ERP document and
never changes a workflow state. Creating the document stays with the caller's
authorized execution layer. ERPNext permissions, validations and workflows remain
authoritative: the engine only reads, as the current session user, and its decision
is advice that the caller must respect.

Configuration (rule sets, rules, assignments) is read with ``frappe.get_all`` and
``frappe.get_doc`` because rule configuration is system configuration, not ERP
business data. ERP business data is read only through ``RuleDataAccess``.
"""

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import nowdate

from erp_intelligence.services.business_rules import constants as c
from erp_intelligence.services.business_rules.context import (
    EvaluationContext,
    RuleDefinition,
    normalize_confirmations,
)
from erp_intelligence.services.business_rules.data_access import RuleDataAccess
from erp_intelligence.services.business_rules.errors import (
    EvaluatorContractError,
    RuleConfigurationError,
    RuleDataError,
)
from erp_intelligence.services.business_rules.parameters import resolve_parameters
from erp_intelligence.services.business_rules.registry import (
    EvaluatorRegistry,
    get_default_registry,
)
from erp_intelligence.services.business_rules.results import build_result, validate_result
from erp_intelligence.utils.response_utils import build_response, to_json_safe

_RULE_FIELDS = [
    "name",
    "rule_code",
    "rule_name",
    "description",
    "rule_category",
    "evaluator_key",
    "execution_stage",
    "default_severity",
    "is_blocking",
    "enabled",
    "default_parameters",
]
_MAX_ERROR_TEXT = 500


class BusinessRulesEngine:
    """Evaluate the business rules assigned to a target DocType and operation."""

    @classmethod
    def evaluate(
        cls,
        target_doctype: str,
        operation: str,
        payload: Mapping[str, Any],
        *,
        stage: str,
        confirmations: Mapping[str, Any] | list[str] | None = None,
        execution_options: Mapping[str, Any] | None = None,
        execution_result: Mapping[str, Any] | None = None,
        user: str | None = None,
        registry: EvaluatorRegistry | None = None,
        data_access: RuleDataAccess | None = None,
    ) -> dict[str, Any]:
        """Run the rules of one stage and return the execution report.

        ``payload`` is the proposed document data (for a create, the data the
        caller intends to send). ``confirmations`` lists the confirmation keys the
        user has explicitly given; a mapping counts only keys whose value is
        ``True``. ``execution_options`` carries the caller's intended execution
        flags (for example ``{"submit": False}``). ``execution_result`` carries the
        outcome of the execution for POST_EXECUTION rules (for example the created
        document name). ``user`` may only assert the current session user.
        ``registry`` and ``data_access`` are injectable for tests.

        The report's ``decision`` and ``can_proceed`` always fail closed: a missing
        or disabled rule set, an unregistered evaluator, a data error and a
        pending input or confirmation never produce a proceeding decision.
        """
        request_error = cls._validate_request(
            target_doctype, operation, payload, stage, confirmations,
            execution_options, execution_result,
        )
        if request_error:
            return cls._report_without_rules(
                target_doctype, operation, stage, c.ERROR_INVALID_REQUEST, request_error
            )

        rule_set, error = cls._resolve_rule_set(target_doctype, operation)
        if error:
            return cls._report_without_rules(
                target_doctype, operation, stage, error[0], error[1]
            )

        registry = registry or get_default_registry()
        data_access = data_access or RuleDataAccess(user=user)
        confirmed = normalize_confirmations(confirmations)
        today = nowdate()

        assignments = cls._ordered_assignments(rule_set)
        if not any(row.enabled for row in assignments):
            return cls._report_without_rules(
                target_doctype,
                operation,
                stage,
                c.ERROR_RULE_SET_EMPTY,
                f"Business Rule Set {rule_set.name} has no enabled rule assignments.",
                rule_set=rule_set,
            )

        rules = cls._load_rules([row.business_rule for row in assignments if row.enabled])

        results: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in assignments:
            code = row.business_rule
            if not row.enabled:
                skipped.append({"rule_code": code, "reason": "assignment_disabled"})
                continue
            rule_row = rules.get(code)
            if rule_row is None:
                results.append(
                    cls._engine_error(
                        code,
                        c.SEVERITY_CRITICAL,
                        True,
                        c.ERROR_TYPE_CONFIGURATION,
                        f"Business Rule {code} assigned to {rule_set.name} does not exist.",
                        row.sequence,
                    )
                )
                continue
            if not rule_row["enabled"]:
                skipped.append({"rule_code": code, "reason": "rule_disabled"})
                continue
            if rule_row["execution_stage"] != stage:
                skipped.append(
                    {
                        "rule_code": code,
                        "reason": "other_stage",
                        "execution_stage": rule_row["execution_stage"],
                    }
                )
                continue

            results.append(
                cls._run_rule(
                    row,
                    rule_row,
                    rule_set=rule_set,
                    target_doctype=target_doctype,
                    operation=operation,
                    stage=stage,
                    payload=payload,
                    data_access=data_access,
                    confirmed=confirmed,
                    execution_options=execution_options or {},
                    execution_result=execution_result or {},
                    today=today,
                    registry=registry,
                )
            )

        return cls._build_report(
            target_doctype, operation, stage, rule_set, results, skipped, data_access
        )

    # ------------------------------------------------------------------ request
    @staticmethod
    def _validate_request(
        target_doctype: Any,
        operation: Any,
        payload: Any,
        stage: Any,
        confirmations: Any,
        execution_options: Any,
        execution_result: Any,
    ) -> str | None:
        if not isinstance(target_doctype, str) or not target_doctype.strip():
            return "target_doctype must be a non-empty string."
        if not isinstance(operation, str) or not operation.strip():
            return "operation must be a non-empty string."
        if stage not in c.EXECUTION_STAGES:
            return f"stage must be one of: {', '.join(c.EXECUTION_STAGES)}."
        for name, value in (
            ("payload", payload),
            ("execution_options", execution_options),
            ("execution_result", execution_result),
        ):
            if value is None and name != "payload":
                continue
            if not isinstance(value, Mapping):
                return f"{name} must be a mapping."
            try:
                to_json_safe(dict(value))
            except (TypeError, ValueError) as exc:
                return f"{name} must contain only JSON-safe values: {exc}"
        try:
            normalize_confirmations(confirmations)
        except TypeError as exc:
            return str(exc)
        return None

    # ----------------------------------------------------------- configuration
    @staticmethod
    def _resolve_rule_set(
        target_doctype: str,
        operation: str,
    ) -> tuple[Any, tuple[str, str] | None]:
        candidates = frappe.get_all(
            "Business Rule Set",
            filters={"target_doctype": target_doctype.strip(), "operation": operation.strip()},
            fields=["name", "enabled"],
            order_by="name asc",
        )
        if not candidates:
            return None, (
                c.ERROR_RULE_SET_NOT_FOUND,
                f"No Business Rule Set exists for {target_doctype} / {operation}.",
            )
        enabled = [row for row in candidates if row.enabled]
        if not enabled:
            return None, (
                c.ERROR_RULE_SET_DISABLED,
                f"Every Business Rule Set for {target_doctype} / {operation} is disabled.",
            )
        if len(enabled) > 1:
            return None, (
                c.ERROR_RULE_SET_AMBIGUOUS,
                f"More than one enabled Business Rule Set exists for {target_doctype} / "
                f"{operation}: {', '.join(row.name for row in enabled)}.",
            )
        return frappe.get_doc("Business Rule Set", enabled[0].name), None

    @staticmethod
    def _ordered_assignments(rule_set: Any) -> list[Any]:
        """Assignments by ascending sequence. Equal sequences keep their row order."""
        return sorted(rule_set.rules, key=lambda row: (row.sequence or 0, row.idx or 0))

    @staticmethod
    def _load_rules(codes: list[str]) -> dict[str, dict[str, Any]]:
        if not codes:
            return {}
        rows = frappe.get_all(
            "Business Rule",
            filters={"name": ["in", list(dict.fromkeys(codes))]},
            fields=_RULE_FIELDS,
        )
        return {row["name"]: row for row in rows}

    # ---------------------------------------------------------------- execution
    @classmethod
    def _run_rule(
        cls,
        row: Any,
        rule_row: Mapping[str, Any],
        *,
        rule_set: Any,
        target_doctype: str,
        operation: str,
        stage: str,
        payload: Mapping[str, Any],
        data_access: RuleDataAccess,
        confirmed: frozenset[str],
        execution_options: Mapping[str, Any],
        execution_result: Mapping[str, Any],
        today: str,
        registry: EvaluatorRegistry,
    ) -> dict[str, Any]:
        code = rule_row["rule_code"] or rule_row["name"]
        is_blocking = bool(rule_row["is_blocking"])
        sequence = row.sequence or 0
        severity = row.severity_override or rule_row["default_severity"]

        if severity not in c.SEVERITIES:
            return cls._engine_error(
                code, c.SEVERITY_CRITICAL, is_blocking, c.ERROR_TYPE_CONFIGURATION,
                f"Rule {code} has an invalid severity {severity!r}.", sequence,
            )
        try:
            parameters = resolve_parameters(
                rule_row["default_parameters"], row.parameters_override, f"Rule {code}"
            )
        except RuleConfigurationError as exc:
            return cls._engine_error(
                code, severity, is_blocking, c.ERROR_TYPE_CONFIGURATION, str(exc), sequence
            )

        evaluator = registry.get(rule_row["evaluator_key"])
        if evaluator is None:
            return cls._engine_error(
                code, severity, is_blocking, c.ERROR_TYPE_UNREGISTERED_EVALUATOR,
                f"Rule {code} uses evaluator {rule_row['evaluator_key']!r}, "
                "which is not registered.",
                sequence,
                {"evaluator_key": rule_row["evaluator_key"]},
            )

        definition = RuleDefinition(
            rule_code=code,
            rule_name=rule_row["rule_name"],
            description=rule_row["description"] or "",
            rule_category=rule_row["rule_category"],
            evaluator_key=rule_row["evaluator_key"],
            execution_stage=rule_row["execution_stage"],
            severity=severity,
            is_blocking=is_blocking,
            parameters=parameters,
            sequence=sequence,
        )
        context = EvaluationContext(
            rule=definition,
            target_doctype=target_doctype,
            operation=operation,
            stage=stage,
            payload=payload,
            data=data_access,
            confirmations=confirmed,
            execution_options=execution_options,
            execution_result=execution_result,
            today=today,
        )

        try:
            raw = evaluator(context)
            result = validate_result(
                raw, rule_code=code, severity=severity, is_blocking=is_blocking
            )
        except EvaluatorContractError as exc:
            return cls._engine_error(
                code, severity, is_blocking, c.ERROR_TYPE_CONTRACT,
                f"Rule {code} returned an invalid result: {cls._clip(exc)}", sequence,
                {"evaluator_key": definition.evaluator_key},
            )
        except RuleConfigurationError as exc:
            return cls._engine_error(
                code, severity, is_blocking, c.ERROR_TYPE_CONFIGURATION,
                f"Rule {code} is misconfigured: {cls._clip(exc)}", sequence,
            )
        except RuleDataError as exc:
            return cls._engine_error(
                code, severity, is_blocking, c.ERROR_TYPE_DATA_ACCESS,
                f"Rule {code} could not read the data it needs: {cls._clip(exc)}", sequence,
                {"data_error_code": exc.code},
            )
        except Exception as exc:  # noqa: BLE001 - an evaluator bug must not stop the run
            frappe.logger(c.LOGGER_NAME).exception("Evaluator for rule %s raised", code)
            return cls._engine_error(
                code, severity, is_blocking, c.ERROR_TYPE_EXCEPTION,
                f"Rule {code} raised {type(exc).__name__}: {cls._clip(exc)}", sequence,
                {"exception_type": type(exc).__name__},
            )

        return cls._annotate(result, definition)

    @staticmethod
    def _annotate(result: dict[str, Any], rule: RuleDefinition) -> dict[str, Any]:
        """Add the report-only fields. They are not part of the evaluator contract."""
        return {
            **result,
            "rule_name": rule.rule_name,
            "rule_category": rule.rule_category,
            "execution_stage": rule.execution_stage,
            "evaluator_key": rule.evaluator_key,
            "sequence": rule.sequence,
        }

    @classmethod
    def _engine_error(
        cls,
        rule_code: str,
        severity: str,
        is_blocking: bool,
        error_type: str,
        message: str,
        sequence: int,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """An ERROR result produced by the engine: the rule could not be evaluated."""
        result = build_result(
            rule_code=rule_code,
            status=c.ERROR,
            severity=severity,
            is_blocking=is_blocking,
            message=message,
            details={"error_type": error_type, **(extra or {})},
        )
        result.update(
            {
                "rule_name": None,
                "rule_category": None,
                "execution_stage": None,
                "evaluator_key": (extra or {}).get("evaluator_key"),
                "sequence": sequence,
            }
        )
        return result

    @staticmethod
    def _clip(value: Any) -> str:
        text = str(value)
        return text if len(text) <= _MAX_ERROR_TEXT else text[:_MAX_ERROR_TEXT] + "..."

    # ------------------------------------------------------------------- report
    @classmethod
    def _build_report(
        cls,
        target_doctype: str,
        operation: str,
        stage: str,
        rule_set: Any,
        results: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        data_access: RuleDataAccess,
    ) -> dict[str, Any]:
        blocking_failures: list[str] = []
        blocking_errors: list[str] = []
        pending_inputs: list[dict[str, Any]] = []
        pending_confirmations: list[dict[str, Any]] = []
        warnings: list[str] = []
        non_blocking_issues: list[dict[str, str]] = []
        counts = dict.fromkeys(c.RESULT_STATUSES, 0)

        for result in results:
            status = result["status"]
            code = result["rule_code"]
            counts[status] += 1
            if status == c.PASS:
                continue
            if status == c.WARNING:
                warnings.append(code)
                continue
            if not result["is_blocking"]:
                non_blocking_issues.append({"rule_code": code, "status": status})
                continue
            if status == c.FAIL:
                blocking_failures.append(code)
            elif status == c.ERROR:
                blocking_errors.append(code)
            elif status == c.NEEDS_INPUT:
                pending_inputs.append(
                    {"rule_code": code, "required_inputs": result["required_inputs"]}
                )
            elif status == c.NEEDS_CONFIRMATION:
                pending_confirmations.append(
                    {
                        "rule_code": code,
                        "confirmation_keys": result["details"]["confirmation_keys"],
                        "message": result["message"],
                    }
                )

        if blocking_errors:
            decision = c.DECISION_ERROR
        elif blocking_failures:
            decision = c.DECISION_BLOCKED
        elif pending_inputs:
            decision = c.DECISION_NEEDS_INPUT
        elif pending_confirmations:
            decision = c.DECISION_NEEDS_CONFIRMATION
        elif warnings or non_blocking_issues:
            decision = c.DECISION_PROCEED_WITH_WARNINGS
        else:
            decision = c.DECISION_PROCEED

        notes = []
        if not results:
            notes.append(f"No enabled rule is assigned to stage {stage}; nothing was evaluated.")

        return build_response(
            c.REPORT_RESULT_TYPE,
            doctype=target_doctype,
            returned_count=len(results),
            payload={
                "decision": decision,
                "can_proceed": decision in c.PROCEEDING_DECISIONS,
                "error_code": None,
                "message": cls._decision_message(decision),
                "operation": operation,
                "stage": stage,
                "rule_set": {
                    "name": rule_set.name,
                    "version": rule_set.version,
                    "enabled": bool(rule_set.enabled),
                },
                "results": results,
                "skipped": skipped,
                "summary": {
                    "rules_evaluated": len(results),
                    "rules_skipped": len(skipped),
                    "status_counts": counts,
                    "all_passed": bool(results) and counts[c.PASS] == len(results),
                    "data_requests": data_access.request_count,
                },
                "blocking_failures": blocking_failures,
                "blocking_errors": blocking_errors,
                "pending_inputs": pending_inputs,
                "pending_confirmations": pending_confirmations,
                "warnings": warnings,
                "non_blocking_issues": non_blocking_issues,
                "notes": notes,
                # The engine reads only. Creating or changing documents is the
                # caller's authorized execution layer.
                "execution_performed": False,
            },
        )

    @classmethod
    def _report_without_rules(
        cls,
        target_doctype: Any,
        operation: Any,
        stage: Any,
        error_code: str,
        message: str,
        *,
        rule_set: Any = None,
    ) -> dict[str, Any]:
        """A fail-closed report for a request that could not be evaluated at all."""
        return build_response(
            c.REPORT_RESULT_TYPE,
            doctype=target_doctype if isinstance(target_doctype, str) and target_doctype else None,
            returned_count=0,
            payload={
                "decision": c.DECISION_ERROR,
                "can_proceed": False,
                "error_code": error_code,
                "message": message,
                "operation": operation if isinstance(operation, str) else None,
                "stage": stage if isinstance(stage, str) else None,
                "rule_set": (
                    {
                        "name": rule_set.name,
                        "version": rule_set.version,
                        "enabled": bool(rule_set.enabled),
                    }
                    if rule_set is not None
                    else None
                ),
                "results": [],
                "skipped": [],
                "summary": {
                    "rules_evaluated": 0,
                    "rules_skipped": 0,
                    "status_counts": dict.fromkeys(c.RESULT_STATUSES, 0),
                    "all_passed": False,
                    "data_requests": 0,
                },
                "blocking_failures": [],
                "blocking_errors": [],
                "pending_inputs": [],
                "pending_confirmations": [],
                "warnings": [],
                "non_blocking_issues": [],
                "notes": [],
                "execution_performed": False,
            },
        )

    @staticmethod
    def _decision_message(decision: str) -> str:
        return {
            c.DECISION_PROCEED: "All evaluated rules passed.",
            c.DECISION_PROCEED_WITH_WARNINGS: (
                "No blocking issue. Review the warnings and non-blocking issues before proceeding."
            ),
            c.DECISION_NEEDS_CONFIRMATION: (
                "A user confirmation is pending. Do not proceed until it is given."
            ),
            c.DECISION_NEEDS_INPUT: (
                "Required input is missing. Do not proceed until it is provided."
            ),
            c.DECISION_BLOCKED: "A blocking rule failed. Do not proceed.",
            c.DECISION_ERROR: (
                "A blocking rule could not be evaluated. Do not proceed; the outcome is unknown."
            ),
        }[decision]
