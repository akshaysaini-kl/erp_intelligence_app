"""MCP tool that exposes the Business Rules Engine to FAC.

The tool is a thin, read-only adapter. It validates the request, checks that the
current user may read the target DocType, calls ``BusinessRulesEngine.evaluate`` once
and returns the engine's report. It does not evaluate rules, read rule configuration
or read ERP data itself, and it never writes a document.

Every response, including every refusal, is a report in the engine's shape with
``execution_performed`` set to false. The tool does not raise for its own failures,
because an exception would reach the caller without that field. It fails closed: a
disabled tool, an invalid request, a permission failure, an unexpected exception and
an engine report that breaks its own contract all return ``decision`` ERROR and
``can_proceed`` false. Nothing is converted into a proceeding decision.

Authentication and authorization stay with FAC and Frappe. FAC authenticates the
caller and sets the session user. The tool only compares an optional ``user``
assertion with that session user and asks Frappe whether that user may read the
DocType. There is no second authentication mechanism.

The tool is off by default (see ``_is_enabled``).
"""

import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

import frappe
from frappe_assistant_core.core.base_tool import BaseTool

from erp_intelligence.services.business_rules import binding as binding_module
from erp_intelligence.services.business_rules import constants as c
from erp_intelligence.services.business_rules.engine import BusinessRulesEngine
from erp_intelligence.services.business_rules.errors import describe_exception
from erp_intelligence.utils.response_utils import to_json_safe

TOOL_NAME = "evaluate_business_rules"

_DECISIONS = c.PROCEEDING_DECISIONS | frozenset(
    {
        c.DECISION_NEEDS_CONFIRMATION,
        c.DECISION_NEEDS_INPUT,
        c.DECISION_BLOCKED,
        c.DECISION_ERROR,
    }
)
_NON_PASSING_BLOCKING_STATUSES = frozenset(
    {c.FAIL, c.ERROR, c.NEEDS_INPUT, c.NEEDS_CONFIRMATION}
)

_SAFE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_FINGERPRINT = re.compile(r"v[0-9]{1,3}\.[0-9a-f]{32}")

# Limits shared by the schema, the validation and the description, so they cannot drift.
MAX_NAME_LENGTH = 140
MAX_CONFIRMATIONS = 50
MAX_BINDING_LENGTH = 200
MAX_OBJECT_BYTES = 256 * 1024
OPERATIONS = ("Create", "Update", "Submit", "Cancel", "Delete")

_DESCRIPTION = f"""Evaluate a proposed ERP operation against the Business Rule Set configured for a DocType and operation, and return a structured report that says whether the caller may proceed. Rules are configured in the ERP and are generic: the tool works for any DocType and operation that has a rule set.

WHEN TO CALL
Call this tool BEFORE carrying out an ERP operation that business rules may govern, using the same doctype, operation and data you intend to use. Call it again at a later stage when the rule set has rules for that stage: PRE_EXECUTION checks the inputs before the payload is built; PRE_COMMIT checks the final payload just before the operation is committed; POST_EXECUTION checks the outcome after the operation was performed. One stage is evaluated per call, and a result for one stage says nothing about the others. The rules of other stages are listed under skipped (reason other_stage). If the rule set has no enabled rule for the stage you ask for, the tool returns ERROR with error_code NO_RULES_FOR_STAGE, never PROCEED.

INPUTS
- doctype (required): exact DocType name, at most {MAX_NAME_LENGTH} characters. The current user must be able to read it.
- operation (required): one of {", ".join(OPERATIONS)}. Together with doctype it selects exactly one enabled rule set.
- payload (required): JSON object with the proposed document data exactly as you intend to use it, at most {MAX_OBJECT_BYTES // 1024} KB. It is evaluated as given. The tool never fills in, corrects or completes it, so never add values the user did not provide.
- stage (optional, default {c.PRE_EXECUTION}): one of {", ".join(c.EXECUTION_STAGES)}.
- confirmations (optional): up to {MAX_CONFIRMATIONS} unique confirmation keys, each at most {MAX_NAME_LENGTH} characters, that the user has explicitly approved. Use only keys listed in pending_confirmations of an earlier report for this same request. They are applied only together with confirmation_binding.
- confirmation_binding (optional, but required whenever confirmations is not empty): the confirmation_binding.fingerprint taken from the report that asked for those confirmations, at most {MAX_BINDING_LENGTH} characters.
- execution_options (optional): JSON object with the flags you intend to use when performing the operation. Rules may read it, and it is part of the confirmation binding.
- execution_result (optional): JSON object with the outcome of the operation. Used by POST_EXECUTION rules, and part of the confirmation binding.
- user (optional): may only repeat the authenticated user. Omit it normally.
No other arguments are accepted.

READING THE REPORT
Always read decision first, then inspect every entry in results: each has rule_code, status ({", ".join(c.RESULT_STATUSES)}), severity, is_blocking, message and details. Use these messages when you explain the outcome to the user, and tell the user about every entry in warnings and non_blocking_issues. execution_performed is always false. decision is one of these, and can_proceed is true only for PROCEED and PROCEED_WITH_WARNINGS:
- PROCEED: every evaluated rule passed. You may continue with the next step.
- PROCEED_WITH_WARNINGS: nothing blocks, but warnings or non-blocking issues exist. Tell the user about each one before continuing.
- BLOCKED: a blocking rule failed (blocking_failures). Stop. Explain the failed rules to the user. Do not retry the same request.
- NEEDS_INPUT: required information is missing or ambiguous (pending_inputs lists required_inputs per rule). Stop and ask the user for it. Never invent or assume a value. Then evaluate again with the updated payload.
- NEEDS_CONFIRMATION: a rule can be satisfied only if the user explicitly approves (pending_confirmations lists the rule, the confirmation_keys and a message). Show the message and ask the user. Only after the user explicitly approves, evaluate again with the same payload, stage and options, confirmations set to the approved keys, and confirmation_binding set to confirmation_binding.fingerprint from the report that asked. Never approve on the user's behalf.
- ERROR: the outcome is unknown. Either no usable rule set exists or the request was refused (error_code is one of RULE_SET_NOT_FOUND, RULE_SET_DISABLED, RULE_SET_AMBIGUOUS, RULE_SET_EMPTY, NO_RULES_FOR_STAGE, INVALID_REQUEST, PERMISSION_DENIED, EVALUATION_FAILED, TOOL_DISABLED), or a rule could not be evaluated (evaluation_errors lists rule_code and error_type: unregistered_evaluator, invalid_configuration, data_access, contract_violation or evaluator_exception). A rule that could not be evaluated stops the run whether or not it is a blocking rule. Stop. Do not treat ERROR as a pass, do not repeat the same request unchanged, and report it to the user. A configuration problem is for an administrator to fix. RULE_SET_NOT_FOUND means no rule set is configured for that DocType and operation. NO_RULES_FOR_STAGE means a rule set exists but none of its enabled rules applies to the requested stage, so no business rules were evaluated for that stage: this is not a pass and not permission to proceed. The message and skipped show which stages do have rules; evaluate one of those stages, or tell the user that this stage is not covered by business rules.
decision precedence is ERROR, BLOCKED, NEEDS_INPUT, NEEDS_CONFIRMATION, so lower items can still be pending when a higher one is reported. A report with an empty results list evaluated nothing and always has an error_code; it is never approval. For POST_EXECUTION, outcome_verification.status is VERIFIED, FAILED or UNVERIFIED; when it is UNVERIFIED, do not assume the operation succeeded or failed, tell the user, and check the document directly.

CONFIRMATIONS AND BINDING
Every report that evaluated rules has confirmation_binding.fingerprint, a keyed fingerprint of the whole request: the authenticated user, doctype, operation, stage, the rule set and the rules evaluated (name, version and modified time), payload, execution_options and execution_result. Confirmations are applied only if the confirmation_binding you send equals the fingerprint of the request you send them with. Otherwise ALL of them are discarded: confirmation_binding.confirmations_rejected gives the keys and the reason (binding_missing, binding_invalid, binding_stale or binding_unavailable), the decision stays NEEDS_CONFIRMATION, and the report carries a new fingerprint. If that happens, ask the user again and use the new fingerprint from the new report. If the binding is unavailable on the site, confirmations are never applied. Never reuse confirmations or a binding after changing the payload, stage, operation, DocType, execution_options or execution_result, or after the user or the rule configuration changed, and never carry them to another request. Always evaluate again with the latest payload and context at the appropriate stage; in particular evaluate again at PRE_COMMIT with the final payload.

LIMITATIONS
- This tool is read-only. It evaluates rules. It does not create, update, submit, cancel or delete any ERP document, and it does not authorize or execute the operation. Carry out the operation yourself with the normal ERP tools, and only when the report allows it.
- PROCEED does not guarantee that the ERP operation will succeed. ERPNext permissions, validations, mandatory fields and native workflows still apply and may reject the operation.
- Evidence is read as the current user. A record the user cannot read produces an ERROR result, never a pass.
- A decision applies only to the exact payload, stage and ERP data at the time of evaluation. The confirmation binding does not cover the ERP data read during evaluation (for example statuses or prices), so data can change between calls.
- The binding shows that confirmations were returned for the same request. It cannot show that the user actually agreed; only ask for and pass confirmations the user has explicitly given."""


class EvaluateBusinessRules(BaseTool):
    """Evaluate Business Rules for a DocType and operation. Read-only, fail closed."""

    OPERATIONS = OPERATIONS

    _ALLOWED_ARGUMENTS = frozenset(
        {
            "doctype",
            "operation",
            "payload",
            "stage",
            "confirmations",
            "confirmation_binding",
            "execution_options",
            "execution_result",
            "user",
        }
    )
    _MAX_NAME_LENGTH = MAX_NAME_LENGTH
    _MAX_BINDING_LENGTH = MAX_BINDING_LENGTH
    _MAX_CONFIRMATIONS = MAX_CONFIRMATIONS
    _MAX_OBJECT_BYTES = MAX_OBJECT_BYTES

    def __init__(self) -> None:
        super().__init__()
        self.name = TOOL_NAME
        self.description = _DESCRIPTION
        self.category = "ERP Intelligence"
        self.source_app = "erp_intelligence"
        self.requires_permission = None
        # Off unless the site turns it on. See _is_enabled.
        self.default_config = {"enabled": False}
        self.inputSchema = {
            "type": "object",
            "properties": {
                "doctype": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": self._MAX_NAME_LENGTH,
                    "description": "Exact name of the DocType the operation applies to. The current user must be able to read it.",
                },
                "operation": {
                    "type": "string",
                    "enum": list(self.OPERATIONS),
                    "description": (
                        "The operation the caller intends to perform. With doctype it selects "
                        "the enabled Business Rule Set."
                    ),
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "The proposed document data exactly as the caller intends to "
                        "use it, as a JSON object of at most 256 KB. It is evaluated as "
                        "given and never modified."
                    ),
                },
                "stage": {
                    "type": "string",
                    "enum": list(c.EXECUTION_STAGES),
                    "default": c.PRE_EXECUTION,
                    "description": (
                        "Which stage's rules to run. Defaults to PRE_EXECUTION. "
                        "One stage is evaluated per call."
                    ),
                },
                "confirmations": {
                    "type": "array",
                    "maxItems": self._MAX_CONFIRMATIONS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": self._MAX_NAME_LENGTH,
                    },
                    "description": (
                        "Confirmation keys the user has explicitly approved, taken from "
                        "pending_confirmations of an earlier report for the same request. "
                        "They are applied only together with a matching confirmation_binding; "
                        "otherwise all of them are discarded."
                    ),
                },
                "confirmation_binding": {
                    "type": "string",
                    "maxLength": self._MAX_BINDING_LENGTH,
                    "description": (
                        "The confirmation_binding.fingerprint from the report that asked for "
                        "the confirmations. Required whenever confirmations is not empty. "
                        "Confirmations are applied only if it matches this exact request."
                    ),
                },
                "execution_options": {
                    "type": "object",
                    "description": (
                        "The flags the caller intends to use when performing the operation. "
                        "Part of the confirmation binding."
                    ),
                },
                "execution_result": {
                    "type": "object",
                    "description": (
                        "The outcome of the operation. Used by POST_EXECUTION rules. "
                        "Part of the confirmation binding."
                    ),
                },
                "user": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional assertion of the authenticated Frappe user. "
                        "It must match the current session user."
                    ),
                },
            },
            "required": ["doctype", "operation", "payload"],
            "additionalProperties": False,
        }

    # ---------------------------------------------------------------- execution
    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return the engine's report, or a fail-closed report. Never raises."""
        try:
            if not self._is_enabled():
                return self._refuse(
                    arguments,
                    c.ERROR_TOOL_DISABLED,
                    "This tool is not enabled on this site. No rules were evaluated.",
                )
            return self._evaluate(arguments)
        except Exception as exc:  # noqa: BLE001 - every failure must fail closed
            # No message and no traceback text: they can contain payload values.
            frappe.logger(c.LOGGER_NAME).error(
                "evaluate_business_rules failed: %s", describe_exception(exc)
            )
            return self._refuse(
                arguments,
                c.ERROR_EVALUATION_FAILED,
                "The evaluation could not be completed and no decision was reached. "
                "Do not proceed.",
            )

    def _evaluate(self, arguments: Any) -> dict[str, Any]:
        request, problem = self._parse(arguments)
        if problem:
            return self._refuse(arguments, c.ERROR_INVALID_REQUEST, problem)

        asserted = request["user"]
        if asserted is not None and asserted != frappe.session.user:
            return self._refuse(
                arguments,
                c.ERROR_PERMISSION_DENIED,
                "The user can only be asserted as the authenticated session user.",
            )
        if not self._can_read(request["doctype"]):
            # One message for "no such DocType" and "no permission", so the response
            # does not reveal which DocTypes exist.
            return self._refuse(
                arguments,
                c.ERROR_PERMISSION_DENIED,
                "The current user may not evaluate rules for this DocType, "
                "or the DocType does not exist.",
            )

        report = BusinessRulesEngine.evaluate(
            request["doctype"],
            request["operation"],
            request["payload"],
            stage=request["stage"],
            confirmations=request["confirmations"],
            confirmation_binding=request["confirmation_binding"],
            require_confirmation_binding=True,
            execution_options=request["execution_options"],
            execution_result=request["execution_result"],
            user=asserted,
        )
        broken = self._contract_problem(report, request["stage"], request)
        if broken:
            frappe.logger(c.LOGGER_NAME).error(
                "evaluate_business_rules: engine report rejected: %s", broken
            )
            return self._refuse(
                arguments,
                c.ERROR_EVALUATION_FAILED,
                "The evaluation returned an inconsistent report and no decision was "
                "reached. Do not proceed.",
            )
        return report

    # ------------------------------------------------------------------ gating
    def _is_enabled(self) -> bool:
        """True only when the site explicitly enables the tool.

        Uses FAC's own tool configuration hierarchy (BaseTool.get_config), so the
        switch is ``"assistant_tools": {"evaluate_business_rules": {"enabled": true}}``
        in the site configuration. The default is off, and only the JSON value true
        enables it. Any problem reading the configuration leaves the tool off.
        """
        try:
            self.clear_config_cache()
            return self.get_config().get("enabled") is True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _can_read(doctype: str) -> bool:
        try:
            return bool(frappe.has_permission(doctype, "read"))
        except Exception:  # noqa: BLE001 - unknown DocType or any failure means no
            return False

    # ------------------------------------------------------------------- audit
    # FAC's BaseTool.log_execution writes every call to "Assistant Audit Log" and
    # passes the arguments and the output through the two methods below. FAC's own
    # versions only redact top-level keys of the arguments, so a nested payload would
    # be stored in full, and they cut long lists, so a report would lose most of its
    # results. They are overridden here (FAC is not modified) to store a compact
    # projection that holds no document values. If a projection cannot be built,
    # nothing from the original is stored.
    def _sanitize_arguments(self, arguments: Any) -> dict[str, Any]:
        try:
            return self._audit_input(arguments)
        except Exception:  # noqa: BLE001
            return {"redacted": True}

    def _sanitize_data(self, data: Any) -> Any:
        try:
            return self._audit_output(data)
        except Exception:  # noqa: BLE001
            return {"redacted": True}

    @classmethod
    def _audit_input(cls, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            return {"redacted": True}
        doctype, operation, stage = (
            cls._audit_text(arguments.get(key)) for key in ("doctype", "operation", "stage")
        )
        payload = arguments.get("payload")
        projection: dict[str, Any] = {
            "doctype": doctype,
            "operation": operation,
            "stage": stage if "stage" in arguments else c.PRE_EXECUTION,
            "payload": cls._audit_payload(payload),
        }
        binding = arguments.get("confirmation_binding")
        if binding is not None:
            projection["confirmation_binding"] = (
                binding if isinstance(binding, str) and _FINGERPRINT.fullmatch(binding) else "<malformed>"
            )
        confirmations = arguments.get("confirmations")
        if isinstance(confirmations, list):
            projection["confirmations"] = [cls._audit_key(key) for key in confirmations[:cls._MAX_CONFIRMATIONS]]
        return projection

    @classmethod
    def _audit_payload(cls, payload: Any) -> dict[str, Any]:
        """Only how big the payload is and how many top-level keys it has."""
        key_count = len(payload) if isinstance(payload, Mapping) else None
        try:
            size = len(json.dumps(to_json_safe(dict(payload)), separators=(",", ":")).encode())
        except (TypeError, ValueError, RecursionError):
            size = None
        return {"size_bytes": size, "key_count": key_count}

    @staticmethod
    def _audit_text(value: Any) -> str | None:
        """A short caller-supplied label (DocType, operation, stage), clipped."""
        return value[:140] if isinstance(value, str) else None

    @staticmethod
    def _audit_key(key: Any) -> str:
        """A confirmation key, if it is a plain identifier.

        Keys are chosen by rule evaluators and may carry a document value after a
        colon (for example ``name:<item code>``). Only the identifier before the
        colon is kept.
        """
        if not isinstance(key, str):
            return "<invalid>"
        head, separator, _ = key.partition(":")
        if not _SAFE_KEY.fullmatch(head):
            return "<redacted>"
        return f"{head}:*" if separator else head

    @classmethod
    def _audit_output(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, Mapping):
            return {"redacted": True}
        if data.get("result_type") != c.REPORT_RESULT_TYPE:
            # FAC's own failure wrapper or anything unexpected: keep no text.
            error_type = data.get("error_type")
            return {
                "success": data.get("success") if isinstance(data.get("success"), bool) else None,
                "error_type": error_type if isinstance(error_type, str) and len(error_type) <= 60 else None,
            }
        rule_set = data.get("rule_set")
        summary = data.get("summary")
        results = data.get("results")
        return {
            "decision": cls._audit_text(data.get("decision")),
            "can_proceed": data.get("can_proceed") if isinstance(data.get("can_proceed"), bool) else None,
            "error_code": cls._audit_text(data.get("error_code")),
            "rule_set": (
                {
                    "name": cls._audit_text(rule_set.get("name")),
                    "version": cls._audit_text(str(rule_set.get("version")) if rule_set.get("version") is not None else None),
                }
                if isinstance(rule_set, Mapping)
                else None
            ),
            "summary": cls._audit_summary(summary),
            "results": [
                {
                    "rule_code": cls._audit_text(item.get("rule_code")),
                    "status": cls._audit_text(item.get("status")),
                    "is_blocking": item.get("is_blocking") if isinstance(item.get("is_blocking"), bool) else None,
                    "severity": cls._audit_text(item.get("severity")),
                }
                for item in (results if isinstance(results, list) else [])
                if isinstance(item, Mapping)
            ],
        }

    @staticmethod
    def _audit_summary(summary: Any) -> dict[str, Any] | None:
        if not isinstance(summary, Mapping):
            return None
        counts = summary.get("status_counts")
        return {
            "rules_evaluated": summary.get("rules_evaluated") if isinstance(summary.get("rules_evaluated"), int) else None,
            "rules_skipped": summary.get("rules_skipped") if isinstance(summary.get("rules_skipped"), int) else None,
            "evaluation_errors": summary.get("evaluation_errors") if isinstance(summary.get("evaluation_errors"), int) else None,
            "status_counts": (
                {str(k): v for k, v in counts.items() if k in c.RESULT_STATUSES and isinstance(v, int)}
                if isinstance(counts, Mapping)
                else None
            ),
        }

    # -------------------------------------------------------------- validation
    @classmethod
    def _parse(cls, arguments: Any) -> tuple[dict[str, Any], str | None]:
        """Return the validated request, or a message describing what is wrong."""
        if not isinstance(arguments, Mapping):
            return {}, "Tool arguments must be a mapping."
        unknown = set(arguments) - cls._ALLOWED_ARGUMENTS
        if unknown:
            names = ", ".join(sorted(str(key)[:40] for key in unknown)[:10])
            return {}, f"Unsupported arguments: {names}."

        doctype = arguments.get("doctype")
        if not cls._is_name(doctype):
            return {}, "doctype must be a non-empty string of printable characters, at most 140 long."
        operation = arguments.get("operation")
        if operation not in cls.OPERATIONS:
            return {}, f"operation must be one of: {', '.join(cls.OPERATIONS)}."
        stage = arguments.get("stage", c.PRE_EXECUTION)
        if stage not in c.EXECUTION_STAGES:
            return {}, f"stage must be one of: {', '.join(c.EXECUTION_STAGES)}."

        objects: dict[str, Any] = {}
        for key, required in (("payload", True), ("execution_options", False), ("execution_result", False)):
            value = arguments.get(key)
            if value is None and key not in arguments and not required:
                continue
            if not isinstance(value, Mapping):
                return {}, f"{key} must be an object."
            problem = cls._object_problem(key, value)
            if problem:
                return {}, problem
            objects[key] = value

        confirmations = arguments.get("confirmations")
        if "confirmations" in arguments and not cls._valid_confirmations(confirmations):
            return {}, (
                f"confirmations must be a list of at most {cls._MAX_CONFIRMATIONS} "
                "unique non-empty strings."
            )
        binding = arguments.get("confirmation_binding")
        if "confirmation_binding" in arguments and (
            not isinstance(binding, str) or len(binding) > cls._MAX_BINDING_LENGTH
        ):
            return {}, (
                f"confirmation_binding must be a string of at most {cls._MAX_BINDING_LENGTH} characters."
            )
        user = arguments.get("user")
        if "user" in arguments and (not isinstance(user, str) or not user.strip()):
            return {}, "user must be a non-empty string."

        return {
            "doctype": doctype.strip(),
            "operation": operation,
            "stage": stage,
            "payload": objects["payload"],
            "execution_options": objects.get("execution_options"),
            "execution_result": objects.get("execution_result"),
            "confirmations": confirmations if "confirmations" in arguments else None,
            "confirmation_binding": binding if "confirmation_binding" in arguments else None,
            "user": user,
        }, None

    @classmethod
    def _is_name(cls, value: Any) -> bool:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value) <= cls._MAX_NAME_LENGTH
            and "`" not in value
            and all(character.isprintable() for character in value)
        )

    @classmethod
    def _valid_confirmations(cls, value: Any) -> bool:
        return (
            isinstance(value, list)
            and len(value) <= cls._MAX_CONFIRMATIONS
            and all(cls._is_name(key) for key in value)
            and len(set(value)) == len(value)
        )

    @classmethod
    def _object_problem(cls, key: str, value: Mapping[str, Any]) -> str | None:
        try:
            size = len(json.dumps(to_json_safe(dict(value)), separators=(",", ":")).encode())
        except (TypeError, ValueError, RecursionError) as exc:
            return f"{key} must contain only JSON-safe values: {type(exc).__name__}."
        if size > cls._MAX_OBJECT_BYTES:
            return f"{key} is larger than {cls._MAX_OBJECT_BYTES // 1024} KB."
        return None

    # ----------------------------------------------------------------- report
    @staticmethod
    def _contract_problem(report: Any, stage: str, request: Mapping[str, Any] | None = None) -> str | None:
        """Describe how the engine's report breaks its contract, or return None.

        This does not re-evaluate anything. It only refuses to pass on a report that
        contradicts itself, so a bug cannot turn into a proceeding decision.
        """
        if not isinstance(report, Mapping):
            return "report is not a mapping"
        if report.get("result_type") != c.REPORT_RESULT_TYPE:
            return "unexpected result_type"
        if report.get("execution_performed") is not False:
            return "execution_performed is not false"
        if report.get("stage") != stage:
            return "stage does not match the request"
        decision = report.get("decision")
        can_proceed = report.get("can_proceed")
        if decision not in _DECISIONS or not isinstance(can_proceed, bool):
            return "decision or can_proceed is invalid"
        if can_proceed is not (decision in c.PROCEEDING_DECISIONS):
            return "can_proceed contradicts decision"
        if report.get("error_code") is not None and decision != c.DECISION_ERROR:
            return "error_code without an ERROR decision"
        results = report.get("results")
        if not isinstance(results, list):
            return "results is not a list"
        if can_proceed and not results:
            return "a proceeding report evaluated no rules"
        errored = []
        for result in results:
            if not isinstance(result, Mapping) or result.get("status") not in c.RESULT_STATUSES:
                return "a result has an invalid status"
            if result["status"] == c.ERROR:
                errored.append(result.get("rule_code"))
            elif (
                can_proceed
                and result.get("is_blocking") is not False
                and result["status"] in _NON_PASSING_BLOCKING_STATUSES
            ):
                return "a blocking rule did not pass but can_proceed is true"
        if can_proceed and any(
            report.get(key) for key in ("blocking_failures", "blocking_errors", "pending_inputs", "pending_confirmations")
        ):
            return "blocking items are listed but can_proceed is true"
        problem = EvaluateBusinessRules._error_policy_problem(report, errored, decision, stage)
        return problem or EvaluateBusinessRules._binding_problem(report, request or {})

    @staticmethod
    def _binding_problem(report: Mapping[str, Any], request: Mapping[str, Any]) -> str | None:
        """Check that the engine honored confirmations only for the exact request.

        The tool cannot recompute the fingerprint, but it can check that the engine
        applied confirmations only when the caller's binding equals the fingerprint
        the engine reports for this request, and that every supplied confirmation is
        accounted for as applied or rejected.
        """
        section = report.get("confirmation_binding")
        if section is None:
            # Only a report that evaluated nothing may omit the section.
            return None if report.get("error_code") is not None else "confirmation_binding is missing"
        expected_keys = {
            "version", "available", "fingerprint", "covers", "required",
            "confirmations_applied", "confirmations_rejected",
        }
        if not isinstance(section, Mapping) or set(section) != expected_keys:
            return "confirmation_binding is malformed"
        fingerprint = section["fingerprint"]
        if fingerprint is not None and not (
            isinstance(fingerprint, str) and binding_module.FINGERPRINT_PATTERN.fullmatch(fingerprint)
        ):
            return "confirmation_binding fingerprint is malformed"
        if section["available"] is not (fingerprint is not None):
            return "confirmation_binding availability contradicts the fingerprint"
        if section["required"] is not True:
            return "binding was not required for the tool"
        if section["covers"] != list(binding_module.COVERS):
            return "confirmation_binding covers an unexpected context"

        applied = section["confirmations_applied"]
        rejected = section["confirmations_rejected"]
        supplied = request.get("confirmations") or []
        if not isinstance(applied, list) or not all(isinstance(key, str) for key in applied):
            return "confirmations_applied is invalid"
        if not set(applied) <= set(supplied):
            return "a confirmation was applied that the caller did not supply"
        if applied:
            supplied_binding = request.get("confirmation_binding")
            if (
                fingerprint is None
                or not isinstance(supplied_binding, str)
                or not hmac.compare_digest(supplied_binding.encode("utf-8"), fingerprint.encode("utf-8"))
            ):
                return "confirmations were applied without a matching binding"
            if rejected is not None:
                return "confirmations are both applied and rejected"
        if rejected is not None:
            if (
                not isinstance(rejected, Mapping)
                or set(rejected) != {"keys", "reason"}
                or rejected["reason"] not in (
                    binding_module.REASON_MISSING, binding_module.REASON_INVALID,
                    binding_module.REASON_STALE, binding_module.REASON_UNAVAILABLE,
                )
                or sorted(rejected["keys"]) != sorted(set(supplied))
            ):
                return "confirmations_rejected is invalid"
        elif supplied and set(applied) != set(supplied):
            return "a supplied confirmation is neither applied nor rejected"
        return None

    @staticmethod
    def _error_policy_problem(report: Mapping[str, Any], errored: list, decision: str, stage: str) -> str | None:
        """Enforce the strict evaluator-error policy on a report.

        Any ERROR result, from any rule, whatever its blocking flag or severity,
        must give decision ERROR with evaluation_errors listing it, and a
        POST_EXECUTION report must say the outcome is unverified.
        """
        entries = report.get("evaluation_errors")
        if not isinstance(entries, list):
            return "evaluation_errors is not a list"
        for entry in entries:
            if (
                not isinstance(entry, Mapping)
                or set(entry) != {"rule_code", "error_type", "rule_is_blocking"}
                or entry["error_type"] not in c.ERROR_TYPES
                or not isinstance(entry["rule_is_blocking"], bool)
            ):
                return "an evaluation_errors entry is invalid"
        listed = [entry["rule_code"] for entry in entries]
        if sorted(map(str, listed)) != sorted(map(str, errored)):
            return "evaluation_errors does not match the ERROR results"
        if errored and decision != c.DECISION_ERROR:
            return "an ERROR result did not produce decision ERROR"
        if decision == c.DECISION_ERROR and not errored and report.get("error_code") is None:
            return "decision ERROR without an error_code or an ERROR result"
        if not isinstance(report.get("blocking_errors"), list) or sorted(
            map(str, report["blocking_errors"])
        ) != sorted(map(str, errored)):
            return "blocking_errors does not match the ERROR results"

        verification = report.get("outcome_verification")
        if stage != c.POST_EXECUTION:
            return None if verification is None else "outcome_verification set outside POST_EXECUTION"
        if not isinstance(verification, Mapping) or verification.get("status") not in c.VERIFICATION_STATUSES:
            return "outcome_verification is missing or invalid for POST_EXECUTION"
        status = verification["status"]
        if status == c.VERIFICATION_VERIFIED and (decision not in c.PROCEEDING_DECISIONS or errored):
            return "outcome reported as verified despite the decision"
        if status != c.VERIFICATION_VERIFIED and decision in c.PROCEEDING_DECISIONS:
            return "a proceeding POST_EXECUTION decision must be verified"
        if decision in (c.DECISION_ERROR, c.DECISION_NEEDS_INPUT, c.DECISION_NEEDS_CONFIRMATION) and (
            status != c.VERIFICATION_UNVERIFIED
        ):
            return "an unverifiable POST_EXECUTION outcome must be UNVERIFIED"
        return None

    @staticmethod
    def _refuse(arguments: Any, error_code: str, message: str) -> dict[str, Any]:
        """A fail-closed report in the engine's shape. Never raises."""
        source = arguments if isinstance(arguments, Mapping) else {}
        stage = source.get("stage", c.PRE_EXECUTION)
        return BusinessRulesEngine.error_report(
            source.get("doctype"), source.get("operation"), stage, error_code, message
        )
