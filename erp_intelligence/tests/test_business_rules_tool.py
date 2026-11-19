"""Tests for the ``evaluate_business_rules`` MCP tool.

The tool is a thin adapter over the Business Rules Engine. These tests cover its
registration and schema, real engine invocation, permission handling, fail-closed
behaviour, malformed input, the absence of any ERP write, and the two layers that keep
it disabled by default (the tool's own switch and a disabled FAC Tool Configuration row).

Most tests use synthetic rules on a harmless target DocType (ToDo) with a stub evaluator
registry, so they do not depend on any business data. One test group runs the seeded
pilot rule set through the tool and compares it with a direct engine call. Every record
is created inside the test transaction and rolled back. No FAC code is modified and
nothing is committed.

Run with:
    bench --site <site> run-tests --module erp_intelligence.tests.test_business_rules_tool
"""

import ast
import contextlib
import copy
import inspect
import json
import pathlib
import re
import unittest
from unittest import mock

import frappe

import erp_intelligence.tools.evaluate_business_rules as tool_module
from erp_intelligence.services.business_rules import binding as binding_module
from erp_intelligence.services.business_rules import constants as c
from erp_intelligence.services.business_rules.engine import BusinessRulesEngine
from erp_intelligence.services.business_rules.pilots.purchase_order_create import (
    seed_purchase_order_create_rule_set,
)
from erp_intelligence.services.business_rules.registry import get_default_registry
from erp_intelligence.tests.test_business_rules_engine import _make_rule, _make_set, _registry
from erp_intelligence.tools.evaluate_business_rules import TOOL_NAME, EvaluateBusinessRules
from erp_intelligence.utils import fac_tool_configuration as fac_config

TARGET = "ToDo"
ENABLED_CONF = {"assistant_tools": {TOOL_NAME: {"enabled": True}}}
ENGINE_PATH = "erp_intelligence.services.business_rules.engine"
WRITE_SQL = ("insert", "update", "delete", "replace", "alter", "create", "drop", "truncate")


def setUpModule():
    if not getattr(frappe.local, "site", None):
        raise unittest.SkipTest("requires an initialized Frappe site")
    if not frappe.db.exists("DocType", "Business Rule Set"):
        raise unittest.SkipTest("Business Rule DocTypes are not installed on this site")


def tearDownModule():
    frappe.set_user("Administrator")
    frappe.db.rollback()


def _fac_installed() -> bool:
    return bool(frappe.db.table_exists(fac_config.FAC_DOCTYPE))


def _user_where(read=(), no_read=()):
    """A non-admin System User who can read every DocType in ``read`` and none in ``no_read``."""
    users = frappe.db.get_all(
        "User",
        filters={"enabled": 1, "user_type": "System User", "name": ("not in", ["Administrator", "Guest"])},
        pluck="name",
    )
    for user in sorted(users):
        if "System Manager" in frappe.get_roles(user):
            continue
        if not all(frappe.has_permission(d, "read", user=user) for d in read):
            continue
        if any(
            frappe.has_permission(d, "read", user=user) or frappe.has_permission(d, "select", user=user)
            for d in no_read
        ):
            continue
        return user
    return None


class ToolCase(unittest.TestCase):
    """Fixture: tool enabled by site config, stub registry, quiet logger, rollback."""

    def setUp(self):
        frappe.set_user("Administrator")
        self.registry = _registry()
        self.registry.register(
            "test.echo",
            lambda ctx: ctx.passed(
                "echo",
                {"options": ctx.execution_options, "result": ctx.execution_result, "payload": ctx.payload},
            ),
        )
        self.tool = EvaluateBusinessRules()
        self.logger = mock.MagicMock()
        for patcher in (
            mock.patch.object(frappe, "logger", return_value=self.logger),
            mock.patch.dict(frappe.local.conf, ENABLED_CONF),
            mock.patch(f"{ENGINE_PATH}.get_default_registry", return_value=self.registry),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(frappe.set_user, "Administrator")
        self.addCleanup(frappe.db.rollback)

    # -------------------------------------------------------------- helpers
    def arguments(self, **overrides):
        base = {"doctype": TARGET, "operation": "Create", "payload": {"field": "value"}}
        base.update(overrides)
        return base

    def call(self, **overrides):
        return self.tool.execute(self.arguments(**overrides))

    def direct(self, **kwargs):
        """The same evaluation, called on the engine directly with the test registry."""
        payload = kwargs.pop("payload", {"field": "value"})
        stage = kwargs.pop("stage", c.PRE_EXECUTION)
        kwargs.setdefault("require_confirmation_binding", True)  # the tool always asks for binding
        return BusinessRulesEngine.evaluate(
            TARGET, "Create", payload, stage=stage, registry=self.registry, **kwargs
        )

    def standard_set(self):
        _make_rule("TEST_A", "test.pass")
        _make_rule("TEST_B", "test.warning")
        _make_rule("TEST_C", "test.pass", stage=c.PRE_COMMIT)
        _make_rule("TEST_D", "test.pass", enabled=0)
        _make_rule("TEST_E", "test.pass")
        _make_set(
            [("TEST_A", 30), ("TEST_B", 10), ("TEST_C", 20), ("TEST_D", 40), ("TEST_E", 50, {"enabled": 0})]
        )

    def single_rule(self, key, **rule_kwargs):
        _make_rule("TEST_RULE", key, **rule_kwargs)
        _make_set([("TEST_RULE", 10)])

    def assertFailsClosed(self, report, error_code=None):
        self.assertEqual(report["decision"], c.DECISION_ERROR)
        self.assertIs(report["can_proceed"], False)
        self.assertIs(report["execution_performed"], False)
        self.assertEqual(report["result_type"], c.REPORT_RESULT_TYPE)
        if error_code:
            self.assertEqual(report["error_code"], error_code)

    @staticmethod
    def codes(report):
        return [item["rule_code"] for item in report["results"]]


# ------------------------------------------------------------ registration and schema
class TestRegistrationAndSchema(unittest.TestCase):
    def setUp(self):
        self.tool = EvaluateBusinessRules()
        self.schema = self.tool.inputSchema

    def test_registered_once_through_the_existing_hook(self):
        import erp_intelligence.hooks as hooks

        path = "erp_intelligence.tools.evaluate_business_rules.EvaluateBusinessRules"
        self.assertEqual(hooks.assistant_tools.count(path), 1)
        self.assertEqual(len(hooks.assistant_tools), len(set(hooks.assistant_tools)))
        for original in (
            "describe_doctype.DescribeDocType",
            "query_records.QueryRecords",
            "aggregate_records.AggregateRecords",
            "intelligence_request.IntelligenceRequest",
        ):
            self.assertIn(f"erp_intelligence.tools.{original}", hooks.assistant_tools)

    def test_follows_the_existing_tool_conventions(self):
        from frappe_assistant_core.core.base_tool import BaseTool

        self.assertIsInstance(self.tool, BaseTool)
        self.assertEqual(self.tool.name, "evaluate_business_rules")
        self.assertEqual(self.tool.name, TOOL_NAME)
        self.assertEqual(self.tool.name, fac_config.TOOL_NAME)
        self.assertEqual(self.tool.category, "ERP Intelligence")
        self.assertEqual(self.tool.source_app, "erp_intelligence")
        self.assertIsNone(self.tool.requires_permission)
        self.assertEqual(set(self.tool.to_mcp_format()), {"name", "description", "inputSchema"})

    def test_fac_discovers_the_tool_from_the_hook(self):
        from frappe_assistant_core.core.tool_registry import get_tool_registry

        external = get_tool_registry()._get_external_tools()
        if not external:
            self.skipTest("FAC custom_tools plugin is not enabled on this site")
        self.assertIn(TOOL_NAME, external)
        self.assertIsInstance(external[TOOL_NAME].instance, EvaluateBusinessRules)

    def test_schema_is_strict_and_complete(self):
        schema = self.schema
        self.assertEqual(schema["type"], "object")
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["required"], ["doctype", "operation", "payload"])
        self.assertEqual(
            set(schema["properties"]),
            {"doctype", "operation", "payload", "stage", "confirmations", "confirmation_binding",
             "execution_options", "execution_result", "user"},
        )
        self.assertEqual(set(schema["properties"]), set(EvaluateBusinessRules._ALLOWED_ARGUMENTS))
        props = schema["properties"]
        self.assertEqual(props["stage"]["enum"], list(c.EXECUTION_STAGES))
        self.assertEqual(props["stage"]["default"], c.PRE_EXECUTION)
        self.assertEqual(props["payload"]["type"], "object")
        confirmations = props["confirmations"]
        self.assertEqual((confirmations["type"], confirmations["uniqueItems"]), ("array", True))
        self.assertEqual(confirmations["items"]["type"], "string")
        self.assertEqual(props["execution_options"]["type"], "object")
        self.assertEqual(props["execution_result"]["type"], "object")
        json.dumps(schema)

    def test_operation_enum_matches_the_rule_set_doctype(self):
        options = frappe.get_meta("Business Rule Set").get_field("operation").options.split("\n")
        self.assertEqual(self.schema["properties"]["operation"]["enum"], options)

    def test_fac_argument_validation_accepts_and_rejects(self):
        self.tool.validate_arguments({"doctype": TARGET, "operation": "Create", "payload": {}})
        with self.assertRaises(frappe.ValidationError):
            self.tool.validate_arguments({"doctype": TARGET, "operation": "Create"})
        with self.assertRaises(frappe.ValidationError):
            self.tool.validate_arguments({"doctype": TARGET, "operation": "Create", "payload": "x"})

    def test_tool_source_has_no_site_or_process_specific_content(self):
        source = pathlib.Path(tool_module.__file__).read_text().lower()
        for forbidden in ("purchase_order", "purchase order", "kindlife", "po_", "supplier"):
            self.assertNotIn(forbidden, source)

    def test_default_config_is_off(self):
        self.assertEqual(self.tool.default_config, {"enabled": False})


class TestDescriptionAndSchemaConsistency(unittest.TestCase):
    """What the LLM reads must match what the tool does."""

    def setUp(self):
        self.tool = EvaluateBusinessRules()
        self.description = self.tool.description
        self.schema = self.tool.inputSchema
        self.properties = self.schema["properties"]

    def input_lines(self):
        section = self.description.split("INPUTS\n", 1)[1].split("\n\nREADING THE REPORT", 1)[0]
        lines = {}
        for line in section.splitlines():
            match = re.match(r"- (\w+) \((required|optional)", line)
            if match:
                lines[match.group(1)] = (match.group(2), line)
        return lines

    def test_every_schema_input_is_documented_and_nothing_else(self):
        documented = self.input_lines()
        self.assertEqual(set(documented), set(self.properties))
        self.assertEqual(
            {name for name, (kind, _) in documented.items() if kind == "required"}, set(self.schema["required"])
        )
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertIn("No other arguments are accepted", self.description)

    def test_every_input_has_a_schema_description(self):
        for name, definition in self.properties.items():
            with self.subTest(name):
                self.assertTrue(definition.get("description", "").strip())

    def test_enums_and_defaults_match_the_implementation(self):
        stage = self.properties["stage"]
        self.assertEqual(stage["enum"], list(c.EXECUTION_STAGES))
        self.assertEqual(stage["default"], c.PRE_EXECUTION)
        line = self.input_lines()["stage"][1]
        self.assertIn(f"default {c.PRE_EXECUTION}", line)
        for value in c.EXECUTION_STAGES:
            self.assertIn(value, line)
        operations = self.properties["operation"]["enum"]
        self.assertEqual(operations, list(EvaluateBusinessRules.OPERATIONS))
        for value in operations:
            self.assertIn(value, self.input_lines()["operation"][1])

    def test_documented_limits_are_the_enforced_limits(self):
        lines = self.input_lines()
        self.assertEqual(self.properties["doctype"]["maxLength"], tool_module.MAX_NAME_LENGTH)
        self.assertIn(f"at most {tool_module.MAX_NAME_LENGTH} characters", lines["doctype"][1])
        self.assertEqual(self.properties["confirmations"]["maxItems"], tool_module.MAX_CONFIRMATIONS)
        self.assertIn(f"up to {tool_module.MAX_CONFIRMATIONS} unique", lines["confirmations"][1])
        self.assertEqual(self.properties["confirmations"]["items"]["maxLength"], tool_module.MAX_NAME_LENGTH)
        self.assertEqual(self.properties["confirmation_binding"]["maxLength"], tool_module.MAX_BINDING_LENGTH)
        self.assertIn(f"at most {tool_module.MAX_BINDING_LENGTH} characters", lines["confirmation_binding"][1])
        self.assertIn(f"at most {tool_module.MAX_OBJECT_BYTES // 1024} KB", lines["payload"][1])
        self.assertIn("256 KB", self.properties["payload"]["description"])
        # The values themselves are part of the contract. Change them on purpose, with the docs.
        self.assertEqual(
            (tool_module.MAX_NAME_LENGTH, tool_module.MAX_CONFIRMATIONS, tool_module.MAX_BINDING_LENGTH,
             tool_module.MAX_OBJECT_BYTES),
            (140, 50, 200, 256 * 1024),
        )

    def test_confirmation_binding_is_documented_as_a_dependency_of_confirmations(self):
        schema = self.properties["confirmation_binding"]
        self.assertEqual(schema["type"], "string")
        self.assertNotIn("confirmation_binding", self.schema["required"])
        line = self.input_lines()["confirmation_binding"][1]
        self.assertIn("required whenever confirmations is not empty", line)
        self.assertIn("confirmation_binding.fingerprint", line)
        self.assertIn("matching confirmation_binding", self.properties["confirmations"]["description"])

    def test_every_decision_value_is_documented_with_an_action(self):
        decisions = {
            c.DECISION_PROCEED, c.DECISION_PROCEED_WITH_WARNINGS, c.DECISION_BLOCKED,
            c.DECISION_NEEDS_INPUT, c.DECISION_NEEDS_CONFIRMATION, c.DECISION_ERROR,
        }
        self.assertEqual(decisions, set(tool_module._DECISIONS))
        listed = set(re.findall(r"^- ([A-Z_]+): ", self.description, flags=re.M))
        self.assertEqual(listed, decisions)
        for decision in decisions:
            self.assertEqual(len(re.findall(rf"^- {decision}: ", self.description, flags=re.M)), 1)

    def test_decision_guidance_is_complete(self):
        text = self.description
        for phrase in (
            "can_proceed is true only for PROCEED and PROCEED_WITH_WARNINGS",
            "Stop and ask the user for it",
            "Never invent or assume a value",
            "ask the user",
            "Only after the user explicitly approves",
            "confirmation_binding set to confirmation_binding.fingerprint",
            "Never approve on the user's behalf",
            "Stop. Explain the failed rules",
            "Stop. Do not treat ERROR as a pass",
            "inspect every entry in results",
            "Never reuse confirmations or a binding after changing the payload",
            "evaluate again at PRE_COMMIT with the final payload",
            "Tell the user about each one before continuing",
        ):
            with self.subTest(phrase):
                self.assertIn(phrase, text)

    def test_limitations_are_stated(self):
        text = self.description
        for phrase in (
            "This tool is read-only",
            "does not create, update, submit, cancel or delete any ERP document",
            "does not authorize or execute the operation",
            "PROCEED does not guarantee that the ERP operation will succeed",
            "ERPNext permissions, validations, mandatory fields and native workflows still apply",
            "execution_performed is always false",
            "does not cover the ERP data read during evaluation",
            "cannot show that the user actually agreed",
        ):
            with self.subTest(phrase):
                self.assertIn(phrase, text)

    def test_every_status_error_code_and_reason_named_exists_in_the_engine(self):
        for status in c.RESULT_STATUSES:
            self.assertIn(status, self.description)
        codes = {
            value for name, value in vars(c).items()
            if name.startswith("ERROR_") and not name.startswith("ERROR_TYPE") and isinstance(value, str)
        }
        self.assertEqual(len(codes), 9)
        for code in codes:
            self.assertIn(code, self.description)
        for error_type in c.ERROR_TYPES:
            self.assertIn(error_type, self.description)
        for reason in ("binding_missing", "binding_invalid", "binding_stale", "binding_unavailable"):
            self.assertIn(reason, self.description)
        for status in c.VERIFICATION_STATUSES:
            self.assertIn(status, self.description)

    def test_documented_report_fields_exist_in_a_real_report(self):
        for name in ("decision", "can_proceed", "results", "skipped", "warnings", "non_blocking_issues",
                     "blocking_failures", "pending_inputs", "pending_confirmations", "evaluation_errors",
                     "outcome_verification", "confirmation_binding", "execution_performed", "error_code"):
            self.assertIn(name, self.description)
        frappe.set_user("Administrator")
        registry = _registry()
        try:
            _make_rule("TEST_RULE", "test.needs_confirmation")
            _make_set([("TEST_RULE", 10)])
            with mock.patch(f"{ENGINE_PATH}.get_default_registry", return_value=registry):
                report = BusinessRulesEngine.evaluate(TARGET, "Create", {}, stage=c.PRE_EXECUTION)
        finally:
            frappe.db.rollback()
        for name in ("decision", "can_proceed", "results", "skipped", "warnings", "non_blocking_issues",
                     "blocking_failures", "pending_inputs", "pending_confirmations", "evaluation_errors",
                     "outcome_verification", "confirmation_binding", "execution_performed", "error_code"):
            self.assertIn(name, report)
        self.assertEqual(set(report["pending_confirmations"][0]), {"rule_code", "confirmation_keys", "message"})
        self.assertIn("confirmation_keys", self.description)
        self.assertIn("fingerprint", report["confirmation_binding"])
        self.assertIn("confirmations_rejected", report["confirmation_binding"])
        for key in ("confirmations_rejected", "fingerprint"):
            self.assertIn(key, self.description)

    def test_the_description_describes_binding_accurately(self):
        text = self.description
        for covered in ("authenticated user", "doctype", "operation", "stage", "payload",
                        "execution_options", "execution_result"):
            self.assertIn(covered, text)
        self.assertIn("ALL of them are discarded", text)
        self.assertIn("never applied", text)
        self.assertIn("keyed fingerprint", text)
        # The binding really does cover what the description says it covers.
        self.assertEqual(
            set(binding_module.COVERS),
            {"version", "user", "doctype", "operation", "stage", "rule_set", "rules", "payload",
             "execution_options", "execution_result"},
        )
        # It must not promise more than the implementation gives.
        for overpromise in ("guarantee success", "guarantees", "authorizes the operation", "proves the user"):
            self.assertNotIn(overpromise, text)

    def test_the_description_is_generic(self):
        lowered = self.description.lower()
        for forbidden in ("purchase order", "supplier", "kindlife", "po_", "item price", "gst", "sales order",
                          "invoice", "exchange_rate", "price list"):
            self.assertNotIn(forbidden, lowered)
        self.assertGreater(len(self.description), 3000)

    def test_tool_defaults_still_off(self):
        self.assertEqual(self.tool.default_config, {"enabled": False})


# ------------------------------------------------------------------- engine invocation
class TestEngineInvocation(ToolCase):
    def test_calls_the_engine_once_with_the_request(self):
        self.standard_set()
        with mock.patch.object(BusinessRulesEngine, "evaluate", wraps=BusinessRulesEngine.evaluate) as spy:
            self.call(
                stage=c.PRE_COMMIT,
                confirmations=["k1"],
                execution_options={"submit": False},
                execution_result={"name": "X"},
            )
        spy.assert_called_once()
        args, kwargs = spy.call_args
        self.assertEqual(args, (TARGET, "Create", {"field": "value"}))
        self.assertEqual(
            kwargs,
            {
                "stage": c.PRE_COMMIT,
                "confirmations": ["k1"],
                "confirmation_binding": None,
                "require_confirmation_binding": True,
                "execution_options": {"submit": False},
                "execution_result": {"name": "X"},
                "user": None,
            },
        )

    def test_the_caller_cannot_inject_a_registry_or_data_access(self):
        for key in ("registry", "data_access", "evaluator", "evaluator_key", "code", "expression"):
            report = self.call(**{key: "x"})
            self.assertFailsClosed(report, c.ERROR_INVALID_REQUEST)

    def test_report_is_the_engines_report_unchanged(self):
        self.standard_set()
        self.assertEqual(self.call(), self.direct())

    def test_rules_run_in_configured_sequence_for_the_requested_stage(self):
        self.standard_set()
        report = self.call()
        self.assertEqual(self.codes(report), ["TEST_B", "TEST_A"])
        self.assertEqual([item["sequence"] for item in report["results"]], [10, 30])
        skipped = {item["rule_code"]: item["reason"] for item in report["skipped"]}
        self.assertEqual(
            skipped,
            {"TEST_C": "other_stage", "TEST_D": "rule_disabled", "TEST_E": "assignment_disabled"},
        )
        self.assertEqual(report["stage"], c.PRE_EXECUTION)
        self.assertEqual(report["decision"], c.DECISION_PROCEED_WITH_WARNINGS)
        self.assertEqual(report["rule_set"]["name"], "TEST BRE Set")
        self.assertIs(report["execution_performed"], False)

    def test_stage_defaults_to_pre_execution_and_can_be_chosen(self):
        self.standard_set()
        self.assertEqual(self.call()["stage"], c.PRE_EXECUTION)
        pre_commit = self.call(stage=c.PRE_COMMIT)
        self.assertEqual(pre_commit["stage"], c.PRE_COMMIT)
        self.assertEqual(self.codes(pre_commit), ["TEST_C"])
        self.assertEqual(pre_commit, self.direct(stage=c.PRE_COMMIT))

    def test_result_contract_fields_are_preserved(self):
        self.standard_set()
        for item in self.call()["results"]:
            for field in c.RESULT_FIELDS:
                self.assertIn(field, item)

    def test_options_result_and_payload_reach_the_evaluator(self):
        self.single_rule("test.echo", stage=c.POST_EXECUTION)
        report = self.call(
            stage=c.POST_EXECUTION,
            payload={"a": 1},
            execution_options={"submit": False},
            execution_result={"name": "DOC-1"},
        )
        details = report["results"][0]["details"]
        self.assertEqual(details["options"], {"submit": False})
        self.assertEqual(details["result"], {"name": "DOC-1"})
        self.assertEqual(details["payload"], {"a": 1})

    def test_confirmations_clear_a_pending_confirmation_with_the_matching_binding(self):
        self.single_rule("test.needs_confirmation")
        pending = self.call()
        self.assertEqual(pending["decision"], c.DECISION_NEEDS_CONFIRMATION)
        self.assertFalse(pending["can_proceed"])
        self.assertEqual(pending["pending_confirmations"][0]["confirmation_keys"], ["k1"])
        fingerprint = pending["confirmation_binding"]["fingerprint"]
        confirmed = self.call(confirmations=["k1"], confirmation_binding=fingerprint)
        self.assertEqual(confirmed["decision"], c.DECISION_PROCEED)
        self.assertTrue(confirmed["can_proceed"])
        self.assertEqual(confirmed["confirmation_binding"]["confirmations_applied"], ["k1"])
        self.assertIsNone(confirmed["confirmation_binding"]["confirmations_rejected"])

    def test_confirmations_without_a_binding_are_discarded(self):
        self.single_rule("test.needs_confirmation")
        report = self.call(confirmations=["k1"])
        self.assertEqual(report["decision"], c.DECISION_NEEDS_CONFIRMATION)
        self.assertFalse(report["can_proceed"])
        self.assertEqual(
            report["confirmation_binding"]["confirmations_rejected"],
            {"keys": ["k1"], "reason": "binding_missing"},
        )
        self.assertEqual(report["confirmation_binding"]["confirmations_applied"], [])
        self.assertTrue(any("not applied" in note for note in report["notes"]))

    def test_confirmations_are_bound_to_the_payload(self):
        """A confirmation given for one payload is not honored for another."""
        self.single_rule("test.needs_confirmation")
        first = self.call(payload={"amount": 1})
        fingerprint = first["confirmation_binding"]["fingerprint"]
        self.assertTrue(self.call(payload={"amount": 1}, confirmations=["k1"], confirmation_binding=fingerprint)["can_proceed"])
        for changed in ({"amount": 999999}, {"amount": 1, "extra": 1}, {}):
            with self.subTest(changed):
                report = self.call(payload=changed, confirmations=["k1"], confirmation_binding=fingerprint)
                self.assertFalse(report["can_proceed"])
                self.assertEqual(report["decision"], c.DECISION_NEEDS_CONFIRMATION)
                self.assertEqual(report["confirmation_binding"]["confirmations_rejected"]["reason"], "binding_stale")
                self.assertNotEqual(report["confirmation_binding"]["fingerprint"], fingerprint)

    def test_a_malformed_or_non_string_binding(self):
        self.single_rule("test.needs_confirmation")
        report = self.call(confirmations=["k1"], confirmation_binding="not-a-fingerprint")
        self.assertEqual(report["confirmation_binding"]["confirmations_rejected"]["reason"], "binding_invalid")
        self.assertFalse(report["can_proceed"])
        for bad in (5, ["v1." + "a" * 32], {"x": 1}, None, "x" * 201):
            with self.subTest(repr(bad)[:20]):
                if bad is None:
                    continue
                self.assertFailsClosed(self.call(confirmation_binding=bad), c.ERROR_INVALID_REQUEST)

    def test_confirmations_are_never_honored_when_binding_is_unavailable(self):
        self.single_rule("test.needs_confirmation")
        fingerprint = self.call()["confirmation_binding"]["fingerprint"]
        for key in ("", None, "   "):
            with self.subTest(repr(key)), mock.patch.dict(frappe.local.conf, {"encryption_key": key}):
                if key is None:
                    frappe.local.conf.pop("encryption_key", None)
                report = self.call(confirmations=["k1"], confirmation_binding=fingerprint)
                self.assertFalse(report["can_proceed"])
                self.assertFalse(report["confirmation_binding"]["available"])
                self.assertIsNone(report["confirmation_binding"]["fingerprint"])
                self.assertEqual(
                    report["confirmation_binding"]["confirmations_rejected"]["reason"], "binding_unavailable"
                )

    def test_pilot_rule_set_runs_through_the_real_engine_and_evaluators(self):
        seed_purchase_order_create_rule_set(force=True)
        with mock.patch(f"{ENGINE_PATH}.get_default_registry", get_default_registry):
            args = {"doctype": "Purchase Order", "operation": "Create", "payload": {}}
            report = self.tool.execute(args)
            direct = BusinessRulesEngine.evaluate(
                "Purchase Order", "Create", {}, stage=c.PRE_EXECUTION, require_confirmation_binding=True
            )
        self.assertEqual(report, direct)
        self.assertGreater(len(report["results"]), 5)
        self.assertEqual(report["rule_set"]["name"], "Purchase Order - Create")
        self.assertFalse(report["can_proceed"])
        self.assertIs(report["execution_performed"], False)


# ----------------------------------------------------------------------- permissions
class TestPermissions(ToolCase):
    def test_user_assertion_must_match_the_session_user(self):
        self.standard_set()
        with mock.patch.object(BusinessRulesEngine, "evaluate") as engine:
            report = self.call(user="Guest")
        engine.assert_not_called()
        self.assertFailsClosed(report, c.ERROR_PERMISSION_DENIED)
        self.assertEqual(self.call(user="Administrator")["decision"], c.DECISION_PROCEED_WITH_WARNINGS)

    def test_a_user_who_cannot_read_the_doctype_is_refused_before_the_engine(self):
        self.standard_set()
        with mock.patch.object(frappe, "has_permission", return_value=False) as check, mock.patch.object(
            BusinessRulesEngine, "evaluate"
        ) as engine:
            report = self.call()
        check.assert_called_once_with(TARGET, "read")
        engine.assert_not_called()
        self.assertFailsClosed(report, c.ERROR_PERMISSION_DENIED)

    def test_a_permission_check_that_raises_is_a_refusal(self):
        with mock.patch.object(frappe, "has_permission", side_effect=RuntimeError("db down")):
            self.assertFailsClosed(self.call(), c.ERROR_PERMISSION_DENIED)

    def test_unknown_and_forbidden_doctypes_are_indistinguishable(self):
        # Administrator passes Frappe's permission check before the DocType is looked up,
        # so this needs an ordinary user.
        user = _user_where()
        if not user:
            self.skipTest("no non-admin System User on this site")
        frappe.set_user(user)
        unknown = self.call(doctype="No Such DocType For Sure")
        with mock.patch.object(frappe, "has_permission", return_value=False):
            forbidden = self.call()
        self.assertFailsClosed(unknown, c.ERROR_PERMISSION_DENIED)
        self.assertEqual(unknown["message"], forbidden["message"])
        self.assertEqual(unknown["error_code"], forbidden["error_code"])

    def test_a_real_user_without_read_access_is_refused(self):
        user = _user_where(no_read=["Purchase Order"])
        if not user:
            self.skipTest("no non-admin user without Purchase Order access on this site")
        seed_purchase_order_create_rule_set(force=True)
        frappe.set_user(user)
        report = self.tool.execute({"doctype": "Purchase Order", "operation": "Create", "payload": {}})
        self.assertFailsClosed(report, c.ERROR_PERMISSION_DENIED)
        self.assertEqual(report["results"], [])

    def test_evidence_the_user_cannot_read_is_an_error_not_a_pass(self):
        user = _user_where(read=["Purchase Order"], no_read=["Supplier"])
        if not user:
            self.skipTest("no non-admin user who reads Purchase Order but not Supplier on this site")
        seed_purchase_order_create_rule_set(force=True)
        frappe.set_user(user)
        report = self.tool.execute(
            {"doctype": "Purchase Order", "operation": "Create", "payload": {"supplier": "Anyone"}}
        )
        by_code = {item["rule_code"]: item for item in report["results"]}
        self.assertEqual(by_code["PO_SUPPLIER_RESOLVES_ONE"]["status"], c.ERROR)
        self.assertEqual(report["decision"], c.DECISION_ERROR)
        self.assertFalse(report["can_proceed"])

    def test_the_tool_never_changes_the_session_user(self):
        self.standard_set()
        with mock.patch.object(frappe, "set_user") as set_user:
            self.call()
            self.call(user="Guest")
            self.call(doctype="x" * 500)
        set_user.assert_not_called()
        self.assertEqual(frappe.session.user, "Administrator")

    def test_the_tool_has_no_authentication_or_permission_bypass(self):
        tree = ast.parse(pathlib.Path(tool_module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"set_user", "ignore_permissions", "flags", "login_manager"})
            if isinstance(node, ast.keyword):
                self.assertNotEqual(node.arg, "ignore_permissions")


# ------------------------------------------------------------ fail-closed behaviour
class TestFailClosed(ToolCase):
    def test_missing_rule_set(self):
        report = self.call(operation="Delete")
        self.assertFailsClosed(report, c.ERROR_RULE_SET_NOT_FOUND)
        self.assertEqual(report["results"], [])

    def test_disabled_rule_set(self):
        _make_rule("TEST_RULE")
        _make_set([("TEST_RULE", 10)], enabled=0)
        self.assertFailsClosed(self.call(), c.ERROR_RULE_SET_DISABLED)

    def test_ambiguous_rule_sets(self):
        _make_rule("TEST_RULE")
        _make_set([("TEST_RULE", 10)], name="TEST BRE One")
        _make_set([("TEST_RULE", 10)], name="TEST BRE Two", enabled=0)
        frappe.db.set_value("Business Rule Set", "TEST BRE Two", "enabled", 1)
        self.assertFailsClosed(self.call(), c.ERROR_RULE_SET_AMBIGUOUS)

    def test_rule_set_without_enabled_assignments(self):
        _make_rule("TEST_RULE")
        doc = _make_set([("TEST_RULE", 10)])
        frappe.db.set_value("Business Rule Assignment", doc.rules[0].name, "enabled", 0)
        self.assertFailsClosed(self.call(), c.ERROR_RULE_SET_EMPTY)

    def test_evaluator_failures_are_never_a_proceeding_decision(self):
        for key in ("test.raises", "test.data_error", "test.error_result", "test.bad_contract"):
            with self.subTest(key):
                frappe.db.rollback()
                self.single_rule(key)
                report = self.call()
                self.assertEqual(report["decision"], c.DECISION_ERROR)
                self.assertFalse(report["can_proceed"])
                self.assertEqual(report["blocking_errors"], ["TEST_RULE"])
                self.assertEqual(report["results"][0]["status"], c.ERROR)
                self.assertIs(report["execution_performed"], False)

    def test_an_unregistered_evaluator_is_an_error(self):
        self.single_rule("test.not_registered")
        report = self.call()
        self.assertEqual(report["decision"], c.DECISION_ERROR)
        self.assertEqual(report["results"][0]["details"]["error_type"], c.ERROR_TYPE_UNREGISTERED_EVALUATOR)

    def test_an_unexpected_engine_exception_fails_closed_without_leaking_it(self):
        with mock.patch.object(
            BusinessRulesEngine, "evaluate", side_effect=RuntimeError("secret internal detail")
        ):
            report = self.call()
        self.assertFailsClosed(report, c.ERROR_EVALUATION_FAILED)
        self.assertNotIn("secret internal detail", json.dumps(report))
        self.logger.error.assert_called()

    def test_a_frappe_exception_from_the_engine_fails_closed(self):
        for error in (frappe.PermissionError("no"), frappe.ValidationError("bad"), frappe.DoesNotExistError("x")):
            with self.subTest(type(error).__name__), mock.patch.object(
                BusinessRulesEngine, "evaluate", side_effect=error
            ):
                self.assertFailsClosed(self.call(), c.ERROR_EVALUATION_FAILED)

    def test_an_inconsistent_engine_report_is_rejected(self):
        self.standard_set()
        good = self.direct()
        blocking_fail = {"rule_code": "X", "status": c.FAIL, "is_blocking": True}
        cases = {
            "not a mapping": "PROCEED",
            "none": None,
            "wrong result_type": {**good, "result_type": "other"},
            "execution_performed true": {**good, "execution_performed": True},
            "execution_performed missing": {k: v for k, v in good.items() if k != "execution_performed"},
            "execution_performed truthy": {**good, "execution_performed": 1},
            "wrong stage": {**good, "stage": c.PRE_COMMIT},
            "unknown decision": {**good, "decision": "MAYBE"},
            "can_proceed contradicts decision": {**good, "decision": c.DECISION_BLOCKED, "can_proceed": True},
            "can_proceed not bool": {**good, "can_proceed": 1},
            "error_code without ERROR": {**good, "error_code": "RULE_SET_NOT_FOUND"},
            "results not a list": {**good, "results": "x"},
            "bad result status": {**good, "results": [{"status": "MAYBE", "is_blocking": False}]},
            "blocking fail but proceed": {**good, "results": [*good["results"], blocking_fail]},
            "blocking list but proceed": {**good, "blocking_failures": ["X"]},
        }
        for label, report in cases.items():
            with self.subTest(label), mock.patch.object(BusinessRulesEngine, "evaluate", return_value=report):
                self.assertFailsClosed(self.call(), c.ERROR_EVALUATION_FAILED)

    def test_a_non_blocking_evaluator_error_stops_the_tool_too(self):
        for key in ("test.raises", "test.data_error", "test.config_error", "test.bad_contract",
                    "test.not_registered", "test.error_result"):
            with self.subTest(key):
                frappe.db.rollback()
                _make_rule("TEST_RULE", key, blocking=0, severity=c.SEVERITY_INFO)
                _make_set([("TEST_RULE", 10)])
                report = self.call()
                self.assertEqual(report["decision"], c.DECISION_ERROR)
                self.assertIs(report["can_proceed"], False)
                self.assertEqual(report["evaluation_errors"][0]["rule_code"], "TEST_RULE")
                self.assertIs(report["evaluation_errors"][0]["rule_is_blocking"], False)
                self.assertEqual(report["non_blocking_issues"], [])

    def test_the_consistency_check_enforces_the_error_policy(self):
        self.standard_set()
        good = self.direct()
        error_result = {"rule_code": "X", "status": c.ERROR, "is_blocking": False}
        entry = {"rule_code": "X", "error_type": c.ERROR_TYPE_EXCEPTION, "rule_is_blocking": False}
        cases = {
            "non-blocking ERROR but can_proceed": {
                **good, "results": [*good["results"], error_result], "evaluation_errors": [entry],
                "blocking_errors": ["X"],
            },
            "ERROR result not listed": {**good, "results": [*good["results"], error_result]},
            "ERROR listed but no ERROR result": {**good, "evaluation_errors": [entry]},
            "evaluation_errors missing": {k: v for k, v in good.items() if k != "evaluation_errors"},
            "evaluation_errors not a list": {**good, "evaluation_errors": "x"},
            "unknown error_type": {
                **good, "decision": c.DECISION_ERROR, "can_proceed": False,
                "results": [error_result], "evaluation_errors": [{**entry, "error_type": "weird"}],
                "blocking_errors": ["X"],
            },
            "entry with extra keys": {
                **good, "decision": c.DECISION_ERROR, "can_proceed": False,
                "results": [error_result], "evaluation_errors": [{**entry, "message": "x"}],
                "blocking_errors": ["X"],
            },
            "ERROR result with a non-ERROR decision": {
                **good, "decision": c.DECISION_BLOCKED, "can_proceed": False,
                "results": [error_result], "evaluation_errors": [entry], "blocking_errors": ["X"],
            },
            "blocking_errors mismatch": {
                **good, "decision": c.DECISION_ERROR, "can_proceed": False,
                "results": [error_result], "evaluation_errors": [entry], "blocking_errors": [],
            },
            "ERROR decision with no explanation": {**good, "decision": c.DECISION_ERROR, "can_proceed": False},
            "proceeding with no results": {**good, "results": [], "evaluation_errors": []},
            "verification set on a PRE stage": {
                **good, "outcome_verification": {"status": "VERIFIED", "message": "x"},
            },
        }
        for label, report in cases.items():
            with self.subTest(label), mock.patch.object(BusinessRulesEngine, "evaluate", return_value=report):
                self.assertFailsClosed(self.call(), c.ERROR_EVALUATION_FAILED)

    def test_post_execution_verification_is_enforced(self):
        _make_rule("TEST_RULE", "test.pass", stage=c.POST_EXECUTION)
        _make_set([("TEST_RULE", 10)])
        good = self.direct(stage=c.POST_EXECUTION)
        self.assertEqual(good["outcome_verification"]["status"], c.VERIFICATION_VERIFIED)
        self.assertEqual(self.call(stage=c.POST_EXECUTION), good)
        error_result = {"rule_code": "X", "status": c.ERROR, "is_blocking": False}
        errored = {
            **good, "decision": c.DECISION_ERROR, "can_proceed": False, "results": [error_result],
            "evaluation_errors": [{"rule_code": "X", "error_type": c.ERROR_TYPE_DATA_ACCESS, "rule_is_blocking": False}],
            "blocking_errors": ["X"],
            "outcome_verification": {"status": c.VERIFICATION_UNVERIFIED, "message": "unverified"},
        }
        cases = {
            "verification missing": {k: v for k, v in good.items() if k != "outcome_verification"},
            "verification None": {**good, "outcome_verification": None},
            "unknown status": {**good, "outcome_verification": {"status": "MAYBE", "message": "x"}},
            "error reported as verified": {**errored, "outcome_verification": {"status": "VERIFIED", "message": "x"}},
            "error reported as failed": {**errored, "outcome_verification": {"status": "FAILED", "message": "x"}},
            "proceeding but unverified": {**good, "outcome_verification": {"status": "UNVERIFIED", "message": "x"}},
        }
        for label, report in cases.items():
            with self.subTest(label), mock.patch.object(BusinessRulesEngine, "evaluate", return_value=report):
                self.assertFailsClosed(self.call(stage=c.POST_EXECUTION), c.ERROR_EVALUATION_FAILED)
        with mock.patch.object(BusinessRulesEngine, "evaluate", return_value=errored):
            self.assertEqual(self.call(stage=c.POST_EXECUTION), errored)

    def test_refusals_at_post_execution_are_unverified(self):
        report = self.call(stage=c.POST_EXECUTION, user="Guest")
        self.assertFailsClosed(report, c.ERROR_PERMISSION_DENIED)
        self.assertEqual(report["outcome_verification"]["status"], c.VERIFICATION_UNVERIFIED)
        self.assertIsNone(self.call(user="Guest")["outcome_verification"])

    def test_a_consistent_report_passes_through_untouched(self):
        self.standard_set()
        good = self.direct()
        with mock.patch.object(BusinessRulesEngine, "evaluate", return_value=copy.deepcopy(good)):
            self.assertEqual(self.call(), good)

    def test_the_tool_is_off_unless_the_site_enables_it(self):
        self.standard_set()
        disabled_values = [
            {},
            {"assistant_tools": {}},
            {"assistant_tools": {TOOL_NAME: {}}},
            {"assistant_tools": {TOOL_NAME: {"enabled": False}}},
            {"assistant_tools": {TOOL_NAME: {"enabled": "true"}}},
            {"assistant_tools": {TOOL_NAME: {"enabled": 1}}},
            {"assistant_tools": {TOOL_NAME: {"enabled": "1"}}},
            {"assistant_tools": {TOOL_NAME: "enabled"}},
            {"assistant_tools": "evaluate_business_rules"},
        ]
        for conf in disabled_values:
            with self.subTest(conf), mock.patch.dict(frappe.local.conf), mock.patch.object(
                BusinessRulesEngine, "evaluate"
            ) as engine:
                frappe.local.conf.pop("assistant_tools", None)
                frappe.local.conf.update(conf)
                report = self.call()
                engine.assert_not_called()
            self.assertFailsClosed(report, c.ERROR_TOOL_DISABLED)
            self.assertEqual(report["results"], [])

    def test_a_json_true_enables_the_tool(self):
        self.standard_set()
        self.assertEqual(self.call()["decision"], c.DECISION_PROCEED_WITH_WARNINGS)

    def test_a_configuration_that_cannot_be_read_leaves_the_tool_off(self):
        with mock.patch.object(EvaluateBusinessRules, "get_config", side_effect=RuntimeError("boom")):
            self.assertFailsClosed(self.call(), c.ERROR_TOOL_DISABLED)

    def test_the_switch_is_checked_before_anything_else(self):
        with mock.patch.object(EvaluateBusinessRules, "_is_enabled", return_value=False), mock.patch.object(
            frappe, "has_permission"
        ) as permission, mock.patch.object(BusinessRulesEngine, "evaluate") as engine:
            self.assertFailsClosed(self.tool.execute({"garbage": 1}), c.ERROR_TOOL_DISABLED)
        permission.assert_not_called()
        engine.assert_not_called()

    def test_execute_never_raises(self):
        for arguments in (None, 5, "x", [], [("doctype", "x")], object(), {"doctype": object()}):
            with self.subTest(repr(arguments)):
                self.assertFailsClosed(self.tool.execute(arguments))


class TestEmptyStageThroughTheTool(ToolCase):
    def rules_except(self, empty_stage):
        for index, (stage, code) in enumerate(
            ((c.PRE_EXECUTION, "TEST_PRE"), (c.PRE_COMMIT, "TEST_COMMIT"), (c.POST_EXECUTION, "TEST_POST")), start=1
        ):
            if stage != empty_stage:
                _make_rule(code, "test.pass", stage=stage)
                self._assignments.append((code, index * 10))

    def test_an_empty_stage_fails_closed_through_the_tool(self):
        for stage in c.EXECUTION_STAGES:
            with self.subTest(stage):
                frappe.db.rollback()
                self._assignments = []
                self.rules_except(stage)
                _make_set(self._assignments)
                report = self.call(stage=stage, execution_result={"name": "X"})
                self.assertFailsClosed(report, c.ERROR_NO_RULES_FOR_STAGE)
                self.assertEqual(report["results"], [])
                self.assertEqual(report["stage"], stage)
                self.assertIn("No business rules were evaluated", report["message"])
                self.assertEqual(
                    report, self.direct(stage=stage, execution_result={"name": "X"})
                )
                verification = report["outcome_verification"]
                self.assertEqual(
                    None if verification is None else verification["status"],
                    c.VERIFICATION_UNVERIFIED if stage == c.POST_EXECUTION else None,
                )

    def test_the_default_stage_is_checked_too(self):
        _make_rule("TEST_COMMIT", "test.pass", stage=c.PRE_COMMIT)
        _make_set([("TEST_COMMIT", 10)])
        report = self.call()  # stage omitted: PRE_EXECUTION
        self.assertFailsClosed(report, c.ERROR_NO_RULES_FOR_STAGE)
        self.assertEqual(report["stage"], c.PRE_EXECUTION)
        self.assertEqual(self.call(stage=c.PRE_COMMIT)["decision"], c.DECISION_PROCEED)

    def test_stages_with_rules_are_unchanged_through_the_tool(self):
        self.standard_set()  # rules at PRE_EXECUTION and PRE_COMMIT
        for stage in (c.PRE_EXECUTION, c.PRE_COMMIT):
            with self.subTest(stage):
                report = self.call(stage=stage)
                self.assertIsNone(report["error_code"])
                self.assertTrue(report["can_proceed"])
                self.assertEqual(report, self.direct(stage=stage))
        self.assertFailsClosed(self.call(stage=c.POST_EXECUTION), c.ERROR_NO_RULES_FOR_STAGE)

    def test_the_audit_projection_keeps_the_error_code(self):
        _make_rule("TEST_COMMIT", "test.pass", stage=c.PRE_COMMIT)
        _make_set([("TEST_COMMIT", 10)])
        projection = self.tool._sanitize_data(self.call())
        self.assertEqual(projection["decision"], c.DECISION_ERROR)
        self.assertEqual(projection["error_code"], c.ERROR_NO_RULES_FOR_STAGE)
        self.assertEqual(projection["results"], [])

    def test_the_description_tells_the_model_what_an_empty_stage_means(self):
        text = self.tool.description
        self.assertIn(c.ERROR_NO_RULES_FOR_STAGE, text)
        self.assertIn("no business rules were evaluated for that stage", text)
        self.assertIn("not a pass and not permission to proceed", text)
        self.assertIn("returns ERROR with error_code NO_RULES_FOR_STAGE, never PROCEED", text)
        self.assertIn("A report with an empty results list evaluated nothing and always has an error_code", text)
        self.assertNotIn("If results is empty, no rule applies", text)
        self.assertIn("RULE_SET_NOT_FOUND means no rule set is configured", text)


# --------------------------------------------------------------------- malformed input
class TestMalformedInput(ToolCase):
    def test_invalid_arguments_are_refused_before_the_engine(self):
        nested = current = {}
        for _ in range(5000):
            current["x"] = {}
            current = current["x"]
        cyclic = {}
        cyclic["self"] = cyclic
        cases = {
            "no arguments": None,
            "list arguments": [],
            "unknown key": {"bogus": 1},
            "doctype missing": {"operation": "Create", "payload": {}},
            "doctype none": {"doctype": None, "operation": "Create", "payload": {}},
            "doctype int": {"doctype": 5, "operation": "Create", "payload": {}},
            "doctype empty": {"doctype": "", "operation": "Create", "payload": {}},
            "doctype blank": {"doctype": "   ", "operation": "Create", "payload": {}},
            "doctype too long": {"doctype": "D" * 141, "operation": "Create", "payload": {}},
            "doctype backtick": {"doctype": "To`Do", "operation": "Create", "payload": {}},
            "doctype control char": {"doctype": "To\x00Do", "operation": "Create", "payload": {}},
            "operation missing": {"doctype": TARGET, "payload": {}},
            "operation lower case": {"doctype": TARGET, "operation": "create", "payload": {}},
            "operation unknown": {"doctype": TARGET, "operation": "Frobnicate", "payload": {}},
            "operation int": {"doctype": TARGET, "operation": 1, "payload": {}},
            "stage unknown": {"doctype": TARGET, "operation": "Create", "payload": {}, "stage": "LATER"},
            "stage none": {"doctype": TARGET, "operation": "Create", "payload": {}, "stage": None},
            "stage int": {"doctype": TARGET, "operation": "Create", "payload": {}, "stage": 1},
            "payload missing": {"doctype": TARGET, "operation": "Create"},
            "payload none": {"doctype": TARGET, "operation": "Create", "payload": None},
            "payload list": {"doctype": TARGET, "operation": "Create", "payload": []},
            "payload string": {"doctype": TARGET, "operation": "Create", "payload": "{}"},
            "payload object value": {"doctype": TARGET, "operation": "Create", "payload": {"x": object()}},
            "payload set value": {"doctype": TARGET, "operation": "Create", "payload": {"x": {1}}},
            "payload non-string key": {"doctype": TARGET, "operation": "Create", "payload": {1: "x"}},
            "payload nan": {"doctype": TARGET, "operation": "Create", "payload": {"x": float("nan")}},
            "payload cyclic": {"doctype": TARGET, "operation": "Create", "payload": cyclic},
            "payload too deep": {"doctype": TARGET, "operation": "Create", "payload": nested},
            "payload too large": {"doctype": TARGET, "operation": "Create", "payload": {"x": "a" * (300 * 1024)}},
            "confirmations string": {"doctype": TARGET, "operation": "Create", "payload": {}, "confirmations": "k1"},
            "confirmations mapping": {"doctype": TARGET, "operation": "Create", "payload": {}, "confirmations": {"k1": True}},
            "confirmations none": {"doctype": TARGET, "operation": "Create", "payload": {}, "confirmations": None},
            "confirmations int item": {"doctype": TARGET, "operation": "Create", "payload": {}, "confirmations": [1]},
            "confirmations empty item": {"doctype": TARGET, "operation": "Create", "payload": {}, "confirmations": [""]},
            "confirmations duplicate": {"doctype": TARGET, "operation": "Create", "payload": {}, "confirmations": ["a", "a"]},
            "confirmations too many": {
                "doctype": TARGET, "operation": "Create", "payload": {},
                "confirmations": [f"k{i}" for i in range(51)],
            },
            "options list": {"doctype": TARGET, "operation": "Create", "payload": {}, "execution_options": []},
            "options none": {"doctype": TARGET, "operation": "Create", "payload": {}, "execution_options": None},
            "result string": {"doctype": TARGET, "operation": "Create", "payload": {}, "execution_result": "x"},
            "user empty": {"doctype": TARGET, "operation": "Create", "payload": {}, "user": ""},
            "user int": {"doctype": TARGET, "operation": "Create", "payload": {}, "user": 5},
        }
        with mock.patch.object(BusinessRulesEngine, "evaluate") as engine:
            for label, arguments in cases.items():
                with self.subTest(label):
                    report = self.tool.execute(arguments)
                    self.assertFailsClosed(report, c.ERROR_INVALID_REQUEST)
                    self.assertEqual(report["results"], [])
                    json.dumps(report)
        engine.assert_not_called()

    def test_hostile_values_are_not_echoed_unbounded(self):
        report = self.tool.execute({"doctype": "D" * 100000, "operation": "O" * 100000, "payload": {}})
        self.assertFailsClosed(report, c.ERROR_INVALID_REQUEST)
        self.assertLess(len(json.dumps(report)), 5000)

    def test_valid_edge_values_are_accepted(self):
        self.standard_set()
        report = self.call(
            doctype=f"  {TARGET}  ",
            confirmations=[],
            execution_options={},
            execution_result={},
            user="Administrator",
        )
        self.assertEqual(report["decision"], c.DECISION_PROCEED_WITH_WARNINGS)
        self.assertEqual(report["doctype"], TARGET)


# ---------------------------------------------------------------------------- audit
SENTINEL = "ZQX-SECRET-VALUE-91827"
FINGERPRINT = "v1." + "ab12" * 8


def sensitive_payload():
    return {
        "supplier": f"{SENTINEL}-supplier",
        "items": [{"item_code": f"{SENTINEL}-item", "rate": 98765.4321, "api_key": f"{SENTINEL}-key"}],
        "taxes": {"nested": {"deeper": [{"authorization": f"{SENTINEL}-auth", "token": f"{SENTINEL}-tok"}]}},
        "password": f"{SENTINEL}-pw",
        "authorized_by": f"{SENTINEL}-by",
    }


def audit_rows():
    return frappe.get_all(
        "Assistant Audit Log",
        filters={"tool_name": TOOL_NAME},
        fields=["name", "user", "status", "action", "target_doctype", "target_name", "input_data",
                "output_data", "error_message", "execution_time"],
        order_by="creation asc",
    )


class TestAuditProjection(ToolCase):
    """FAC stores what BaseTool._sanitize_arguments and _sanitize_data return."""

    def echo_rules(self, count=1):
        for index in range(1, count + 1):
            _make_rule(f"TEST_R{index}", "test.echo")
        _make_set([(f"TEST_R{index}", index * 10) for index in range(1, count + 1)])

    def test_fac_sanitizers_alone_would_store_the_payload(self):
        """Why the override exists: FAC's own sanitizers keep nested payload values."""
        from frappe_assistant_core.core.base_tool import BaseTool

        stored = json.dumps(BaseTool._sanitize_arguments(self.tool, {"payload": sensitive_payload()}))
        self.assertIn(SENTINEL, stored)

    def test_the_tool_overrides_both_sanitizers(self):
        from frappe_assistant_core.core.base_tool import BaseTool

        self.assertIsNot(EvaluateBusinessRules._sanitize_arguments, BaseTool._sanitize_arguments)
        self.assertIsNot(EvaluateBusinessRules._sanitize_data, BaseTool._sanitize_data)

    def test_sanitized_arguments_are_the_exact_projection(self):
        payload = sensitive_payload()
        arguments = {
            "doctype": TARGET,
            "operation": "Create",
            "payload": payload,
            "stage": c.PRE_COMMIT,
            "confirmations": ["exchange_rate_confirmed", f"free_item:{SENTINEL}-item", "not a safe key!", "x" * 80],
            "confirmation_binding": FINGERPRINT,
            "execution_options": {"submit": True, "secret": SENTINEL},
            "execution_result": {"name": f"{SENTINEL}-name"},
            "user": "someone@example.com",
        }
        projection = self.tool._sanitize_arguments(arguments)
        expected_size = len(json.dumps(payload, separators=(",", ":")).encode())
        self.assertEqual(
            projection,
            {
                "doctype": TARGET,
                "operation": "Create",
                "stage": c.PRE_COMMIT,
                "payload": {"size_bytes": expected_size, "key_count": 5},
                "confirmation_binding": FINGERPRINT,
                "confirmations": ["exchange_rate_confirmed", "free_item:*", "<redacted>", "<redacted>"],
            },
        )
        text = json.dumps(projection)
        for secret in (SENTINEL, "98765", "someone@example.com", "submit"):
            self.assertNotIn(secret, text)

    def test_stage_defaults_in_the_projection_and_optional_parts_are_omitted(self):
        projection = self.tool._sanitize_arguments({"doctype": TARGET, "operation": "Create", "payload": {}})
        self.assertEqual(
            projection,
            {"doctype": TARGET, "operation": "Create", "stage": c.PRE_EXECUTION,
             "payload": {"size_bytes": 2, "key_count": 0}},
        )

    def test_a_malformed_binding_is_not_stored(self):
        for binding in ("v1." + "g" * 32, "x" * 500, f"{SENTINEL}", 5, ["v1." + "a" * 32]):
            with self.subTest(repr(binding)):
                projection = self.tool._sanitize_arguments({"payload": {}, "confirmation_binding": binding})
                self.assertEqual(projection["confirmation_binding"], "<malformed>")
                self.assertNotIn(SENTINEL, json.dumps(projection))

    def test_malformed_arguments_never_raise_and_never_leak(self):
        cyclic = {"secret": SENTINEL}
        cyclic["self"] = cyclic
        for arguments in (None, 5, "x", [SENTINEL], {"payload": cyclic}, {"payload": [SENTINEL]},
                          {"payload": SENTINEL}, {"doctype": 5, "operation": object(), "payload": {"x": object()}},
                          {"confirmations": [SENTINEL, 5, None]}):
            with self.subTest(repr(arguments)[:40]):
                projection = self.tool._sanitize_arguments(arguments)
                self.assertNotIn(SENTINEL, json.dumps(projection))

    def test_a_failing_projection_stores_nothing_from_the_original(self):
        with mock.patch.object(EvaluateBusinessRules, "_audit_input", side_effect=RuntimeError(SENTINEL)):
            self.assertEqual(self.tool._sanitize_arguments({"payload": {"a": SENTINEL}}), {"redacted": True})
        with mock.patch.object(EvaluateBusinessRules, "_audit_output", side_effect=RuntimeError(SENTINEL)):
            self.assertEqual(self.tool._sanitize_data({"decision": "PROCEED"}), {"redacted": True})

    def test_sanitized_output_is_the_exact_projection(self):
        self.echo_rules(2)
        report = self.call(payload=sensitive_payload())
        # The full report does carry document values (the echo evaluator puts the payload in details).
        self.assertIn(SENTINEL, json.dumps(report))
        projection = self.tool._sanitize_data(report)
        self.assertEqual(
            set(projection), {"decision", "can_proceed", "error_code", "rule_set", "summary", "results"}
        )
        self.assertEqual(projection["decision"], c.DECISION_PROCEED)
        self.assertIs(projection["can_proceed"], True)
        self.assertIsNone(projection["error_code"])
        self.assertEqual(projection["rule_set"], {"name": "TEST BRE Set", "version": "1"})
        self.assertEqual(
            projection["summary"],
            {
                "rules_evaluated": 2,
                "rules_skipped": 0,
                "evaluation_errors": 0,
                "status_counts": report["summary"]["status_counts"],
            },
        )
        self.assertEqual(
            projection["results"],
            [
                {"rule_code": item["rule_code"], "status": item["status"],
                 "is_blocking": item["is_blocking"], "severity": item["severity"]}
                for item in report["results"]
            ],
        )
        for item in projection["results"]:
            self.assertEqual(set(item), {"rule_code", "status", "is_blocking", "severity"})
        self.assertNotIn(SENTINEL, json.dumps(projection))

    def test_long_reports_are_not_truncated_in_the_audit(self):
        self.echo_rules(12)
        projection = self.tool._sanitize_data(self.call())
        self.assertEqual(len(projection["results"]), 12)
        self.assertEqual(projection["summary"]["rules_evaluated"], 12)

    def test_fail_closed_reports_keep_their_error_code(self):
        projection = self.tool._sanitize_data(self.call(operation="Delete"))
        self.assertEqual(projection["decision"], c.DECISION_ERROR)
        self.assertEqual(projection["error_code"], c.ERROR_RULE_SET_NOT_FOUND)
        self.assertEqual(projection["results"], [])
        refused = self.tool._sanitize_data(self.call(user="Guest"))
        self.assertEqual(refused["error_code"], c.ERROR_PERMISSION_DENIED)
        self.assertIsNone(refused["rule_set"])

    def test_error_results_keep_only_the_compact_fields(self):
        self.single_rule("test.data_error", blocking=0, severity=c.SEVERITY_INFO)
        report = self.call(payload=sensitive_payload())
        projection = self.tool._sanitize_data(report)
        self.assertEqual(projection["decision"], c.DECISION_ERROR)
        self.assertEqual(projection["summary"]["evaluation_errors"], 1)
        self.assertEqual(
            projection["results"],
            [{"rule_code": "TEST_RULE", "status": c.ERROR, "is_blocking": False, "severity": c.SEVERITY_INFO}],
        )

    def test_unexpected_output_is_reduced_to_a_type(self):
        wrapper = {"success": False, "error": f"boom {SENTINEL}", "error_type": "ValidationError", "execution_time": 1}
        self.assertEqual(self.tool._sanitize_data(wrapper), {"success": False, "error_type": "ValidationError"})
        for data in (None, "text " + SENTINEL, [SENTINEL], 5, {"anything": SENTINEL}):
            with self.subTest(repr(data)[:30]):
                self.assertNotIn(SENTINEL, json.dumps(self.tool._sanitize_data(data)))

    # ----------------------------------------------- static: FAC still uses the hooks we override
    def test_fac_audit_path_still_calls_the_overridden_methods(self):
        from frappe_assistant_core.core.base_tool import BaseTool

        tree = ast.parse(inspect.getsource(BaseTool.log_execution).replace("\n    ", "\n").lstrip())
        called_on_self = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        }
        self.assertIn("_sanitize_arguments", called_on_self)
        self.assertIn("_sanitize_data", called_on_self)

        # The sanitized values are what is passed on to the audit writer.
        sinks = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "log_tool_execution":
                for keyword in node.keywords:
                    sinks[keyword.arg] = ast.dump(keyword.value)
        self.assertIn("_sanitize_arguments", sinks["arguments"])
        self.assertIn("sanitized_output", sinks["output_data"])
        self.assertIn("_sanitize_data", inspect.getsource(BaseTool.log_execution))

        # The safe-execute wrapper that both MCP entry points use still calls log_execution.
        execute_tree = ast.parse(inspect.getsource(BaseTool._safe_execute).replace("\n    ", "\n").lstrip())
        self.assertIn(
            "log_execution",
            {n.func.attr for n in ast.walk(execute_tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)},
        )

    def test_fac_audit_writer_still_takes_the_arguments_we_rely_on(self):
        from frappe_assistant_core.utils.audit_trail import log_tool_execution

        parameters = set(inspect.signature(log_tool_execution).parameters)
        self.assertTrue({"tool_name", "arguments", "output_data", "error_message"} <= parameters)

    # ------------------------------------------------- end to end through the real audit path
    def test_the_audit_record_holds_no_payload_values(self):
        if not frappe.db.exists("DocType", "Assistant Audit Log"):
            self.skipTest("FAC Assistant Audit Log is not installed on this site")
        self.echo_rules(3)
        before = len(audit_rows())
        arguments = self.arguments(
            payload=sensitive_payload(),
            stage=c.PRE_EXECUTION,
            confirmations=["exchange_rate_confirmed", f"free_item:{SENTINEL}-item"],
            execution_options={"password": f"{SENTINEL}-opt"},
            execution_result={"token": f"{SENTINEL}-res"},
        )
        outcome = self.tool._safe_execute(arguments)  # FAC's wrapper: validate, execute, audit
        self.assertTrue(outcome["success"])
        self.assertIn(SENTINEL, json.dumps(outcome["result"]))  # the caller still gets the full report

        rows = audit_rows()
        self.assertEqual(len(rows), before + 1)
        row = rows[-1]
        self.assertEqual(row.status, "Success")
        self.assertEqual(row.user, "Administrator")
        self.assertEqual(row.target_doctype, TARGET)
        self.assertEqual(json.dumps(row, default=str).count(SENTINEL), 0, "a payload value reached the audit row")
        for fragment in ("98765", "password", "api_key", "token", "authorization", "authorized_by"):
            self.assertNotIn(fragment, json.dumps(row, default=str).lower().replace("confirmation_binding", ""))

        stored_input = json.loads(row.input_data)
        self.assertEqual(
            set(stored_input), {"doctype", "operation", "stage", "payload", "confirmations"},
        )
        self.assertEqual(set(stored_input["payload"]), {"size_bytes", "key_count"})
        self.assertEqual(stored_input["confirmations"], ["exchange_rate_confirmed", "free_item:*"])

        stored_output = json.loads(row.output_data)
        self.assertEqual(stored_output["decision"], c.DECISION_PROCEED)
        self.assertEqual(len(stored_output["results"]), 3)
        self.assertEqual(
            set(stored_output), {"decision", "can_proceed", "error_code", "rule_set", "summary", "results"}
        )

    def test_refusals_are_audited_without_values(self):
        if not frappe.db.exists("DocType", "Assistant Audit Log"):
            self.skipTest("FAC Assistant Audit Log is not installed on this site")
        before = len(audit_rows())
        for arguments in (
            self.arguments(payload=sensitive_payload(), operation="Delete"),       # no rule set
            self.arguments(payload=sensitive_payload(), user="Guest"),             # wrong user
            self.arguments(payload={"bad": {SENTINEL: object()}}),                 # invalid payload
        ):
            outcome = self.tool._safe_execute(arguments)
            self.assertTrue(outcome["success"])
        rows = audit_rows()[before:]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertNotIn(SENTINEL, json.dumps(row, default=str))
        self.assertEqual(
            [json.loads(row.output_data)["error_code"] for row in rows],
            [c.ERROR_RULE_SET_NOT_FOUND, c.ERROR_PERMISSION_DENIED, c.ERROR_INVALID_REQUEST],
        )

    def test_logs_never_contain_exception_messages(self):
        registry = _registry()
        registry.register("test.leaky", lambda ctx: (_ for _ in ()).throw(RuntimeError(f"bad value {SENTINEL}")))
        with mock.patch(f"{ENGINE_PATH}.get_default_registry", return_value=registry):
            self.single_rule("test.leaky")
            self.call(payload=sensitive_payload())
        with mock.patch.object(BusinessRulesEngine, "evaluate", side_effect=ValueError(f"bad {SENTINEL}")):
            self.call(payload=sensitive_payload())
        logged = json.dumps([call.args for call in self.logger.method_calls], default=str)
        logged += json.dumps([call.kwargs for call in self.logger.method_calls], default=str)
        self.assertNotIn(SENTINEL, logged)
        self.assertIn("RuntimeError", logged)
        self.assertIn("ValueError", logged)
        self.assertFalse([call for call in self.logger.method_calls if call.kwargs.get("exc_info")])


# ---------------------------------------------------------------------- no mutation
class TestNoMutation(ToolCase):
    COUNTED = ("ToDo", "Business Rule Set", "Business Rule", "Business Rule Assignment", "Purchase Order")
    DOCUMENT_WRITES = (
        "insert", "save", "submit", "cancel", "delete", "db_insert", "db_update", "db_set", "_submit", "_cancel",
    )

    def counts(self):
        return {doctype: frappe.db.count(doctype) for doctype in self.COUNTED}

    def run_guarded(self, call):
        """Run ``call`` while any Document write, commit or write SQL fails the test."""
        from frappe.model.document import Document

        written = []
        original_sql = frappe.db.sql

        def watch_sql(*args, **kwargs):
            query = args[0] if args else kwargs.get("query")
            text = str(query).lstrip().lower()
            if text.startswith(WRITE_SQL):
                written.append(text[:80])
            return original_sql(*args, **kwargs)

        with contextlib.ExitStack() as stack:
            for name in self.DOCUMENT_WRITES:
                stack.enter_context(
                    mock.patch.object(Document, name, side_effect=AssertionError(f"Document.{name} was called"))
                )
            stack.enter_context(
                mock.patch.object(frappe, "delete_doc", side_effect=AssertionError("delete_doc was called"))
            )
            stack.enter_context(mock.patch.object(frappe.db, "sql", side_effect=watch_sql))
            stack.enter_context(
                mock.patch.object(frappe.db, "commit", side_effect=AssertionError("commit was called"))
            )
            result = call()
        self.assertEqual(written, [], "the tool issued write SQL")
        return result

    def test_no_document_is_created_or_changed(self):
        self.standard_set()
        before = self.counts()
        modified = frappe.db.get_value("Business Rule Set", "TEST BRE Set", "modified")
        for stage in c.EXECUTION_STAGES:
            report = self.run_guarded(lambda stage=stage: self.call(stage=stage, execution_result={"name": "X"}))
            self.assertIs(report["execution_performed"], False)
        self.assertEqual(self.counts(), before)
        self.assertEqual(frappe.db.get_value("Business Rule Set", "TEST BRE Set", "modified"), modified)

    def test_the_pilot_rule_set_run_creates_nothing(self):
        seed_purchase_order_create_rule_set(force=True)
        before = self.counts()
        arguments = {"doctype": "Purchase Order", "operation": "Create", "payload": {"supplier": "Nobody"}}
        for stage in c.EXECUTION_STAGES:
            report = self.run_guarded(lambda stage=stage: self.tool.execute({**arguments, "stage": stage}))
            self.assertIs(report["execution_performed"], False)
            self.assertIs(report["can_proceed"], False)
        self.assertEqual(self.counts(), before)

    def test_refusals_and_failures_write_nothing(self):
        before = self.counts()
        self.run_guarded(lambda: self.call(doctype="No Such DocType"))
        self.run_guarded(lambda: self.tool.execute({"bogus": 1}))
        self.run_guarded(lambda: self.call(operation="Delete"))
        with mock.patch.object(BusinessRulesEngine, "evaluate", side_effect=RuntimeError("x")):
            self.run_guarded(lambda: self.call())
        self.assertEqual(self.counts(), before)

    def test_the_callers_inputs_are_not_modified(self):
        self.standard_set()
        arguments = self.arguments(
            payload={"items": [{"qty": 1}], "nested": {"a": [1, 2]}},
            confirmations=["k1"],
            execution_options={"submit": False},
            execution_result={"name": "X"},
        )
        snapshot = copy.deepcopy(arguments)
        self.tool.execute(arguments)
        self.assertEqual(arguments, snapshot)

    def test_every_response_says_no_execution_was_performed(self):
        self.standard_set()
        reports = [
            self.call(),
            self.call(stage=c.PRE_COMMIT),
            self.call(operation="Delete"),
            self.call(doctype="No Such DocType"),
            self.call(user="Guest"),
            self.tool.execute({"bogus": 1}),
            self.tool.execute(None),
        ]
        with mock.patch.dict(frappe.local.conf, {"assistant_tools": {}}):
            reports.append(self.call())
        for report in reports:
            self.assertIs(report["execution_performed"], False)
            self.assertIn("execution_performed", report)


class TestToolSourceSafety(unittest.TestCase):
    """Static checks on the tool module, in the style of the engine's safety tests."""

    BANNED_CALLS = {"eval", "exec", "compile", "__import__"}
    BANNED_MODULES = {"importlib", "subprocess", "os", "pickle", "marshal"}
    WRITE_METHODS = {
        "insert", "submit", "save", "delete", "db_set", "set_value", "delete_doc", "rename_doc",
        "cancel", "db_insert", "db_update", "run_workflow", "apply_workflow", "commit", "new_doc",
    }
    DIRECT_READS = {"get_list", "get_all", "get_doc", "get_value", "get_cached_doc", "sql", "db"}

    def setUp(self):
        self.tree = ast.parse(pathlib.Path(tool_module.__file__).read_text())

    def test_no_dynamic_code(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, self.BANNED_CALLS)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], self.BANNED_MODULES)
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], self.BANNED_MODULES)

    def test_no_writes_and_no_direct_data_access(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, self.WRITE_METHODS, f"line {node.lineno}")
                self.assertNotIn(node.attr, self.DIRECT_READS, f"line {node.lineno}")

    def test_the_only_engine_entry_points_are_evaluate_and_error_report(self):
        used = {
            node.attr
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "BusinessRulesEngine"
        }
        self.assertEqual(used, {"evaluate", "error_report"})


# ------------------------------------------------- FAC configuration: disabled by default
class TestFacToolConfiguration(ToolCase):
    """The disabled FAC row, and FAC's real behaviour with and without it. No FAC code is changed."""

    def setUp(self):
        if not _fac_installed():
            self.skipTest("FAC Tool Configuration is not installed on this site")
        super().setUp()
        from frappe_assistant_core.core.tool_registry import get_tool_registry

        self.fac = get_tool_registry()
        self.addCleanup(self.fac.clear_cache)
        self.remove_row()

    def remove_row(self):
        if frappe.db.exists(fac_config.FAC_DOCTYPE, TOOL_NAME):
            frappe.delete_doc(fac_config.FAC_DOCTYPE, TOOL_NAME, force=True, ignore_permissions=True)
        self.fac.clear_cache()

    def listed(self):
        self.fac.clear_cache()
        return TOOL_NAME in {item["name"] for item in self.fac.get_available_tools(user="Administrator")}

    def require_custom_tools(self):
        if "custom_tools" not in self.fac_enabled_plugins():
            self.skipTest("FAC custom_tools plugin is not enabled on this site")

    @staticmethod
    def fac_enabled_plugins():
        from frappe_assistant_core.utils.plugin_manager import get_plugin_manager

        return get_plugin_manager().get_enabled_plugins()

    # ------------------------------------------------------------- the row
    def test_creates_a_disabled_row(self):
        self.assertEqual(fac_config.ensure_disabled_tool_configuration(), fac_config.CREATED)
        row = frappe.get_doc(fac_config.FAC_DOCTYPE, TOOL_NAME)
        self.assertEqual(row.enabled, 0)
        self.assertEqual(row.plugin_name, "custom_tools")
        self.assertEqual(row.source_app, "erp_intelligence")
        self.assertEqual(row.tool_category, "read_only")
        self.assertEqual(row.role_access_mode, "Allow All")
        module, cls = row.module_path.rsplit(".", 1)
        self.assertIs(getattr(__import__(module, fromlist=[cls]), cls), EvaluateBusinessRules)

    def test_never_modifies_an_existing_row(self):
        fac_config.ensure_disabled_tool_configuration()
        frappe.db.set_value(fac_config.FAC_DOCTYPE, TOOL_NAME, "enabled", 1)
        self.assertEqual(fac_config.ensure_disabled_tool_configuration(), fac_config.EXISTS)
        self.assertEqual(frappe.db.get_value(fac_config.FAC_DOCTYPE, TOOL_NAME, "enabled"), 1)

    def test_skips_when_fac_is_not_installed(self):
        with mock.patch.object(frappe.db, "table_exists", return_value=False):
            self.assertEqual(fac_config.ensure_disabled_tool_configuration(), fac_config.SKIPPED)
        self.assertFalse(frappe.db.exists(fac_config.FAC_DOCTYPE, TOOL_NAME))

    def test_a_failure_never_aborts_a_migrate(self):
        with mock.patch.object(frappe.db, "exists", side_effect=RuntimeError("db down")):
            self.assertEqual(fac_config.ensure_disabled_tool_configuration(), fac_config.FAILED)
            fac_config.before_migrate()
            fac_config.after_install()

    def test_hooks_run_the_helper_before_fac_syncs(self):
        import erp_intelligence.hooks as hooks

        self.assertEqual(hooks.before_migrate, "erp_intelligence.utils.fac_tool_configuration.before_migrate")
        self.assertEqual(hooks.after_install, "erp_intelligence.utils.fac_tool_configuration.after_install")
        with mock.patch.object(fac_config, "ensure_disabled_tool_configuration") as ensure:
            frappe.get_attr(hooks.before_migrate)()
            frappe.get_attr(hooks.after_install)()
        self.assertEqual(ensure.call_count, 2)
        self.assertIn(
            "frappe_assistant_core.utils.migration_hooks.after_migrate", frappe.get_hooks("after_migrate")
        )

    # ------------------------------------------------- FAC behaviour (verified, not assumed)
    def test_fac_lists_a_tool_that_has_no_row(self):
        """Why the row and the switch are both needed: with no row FAC enables the tool."""
        self.require_custom_tools()
        self.assertTrue(self.listed())

    def test_fac_hides_and_refuses_the_tool_when_the_row_is_disabled(self):
        self.require_custom_tools()
        fac_config.ensure_disabled_tool_configuration()
        self.assertFalse(self.listed())
        with mock.patch.object(EvaluateBusinessRules, "log_execution"):
            with self.assertRaises(PermissionError):
                self.fac.execute_tool(TOOL_NAME, self.arguments())

    def test_fac_sync_leaves_the_disabled_row_alone_and_creates_enabled_rows_for_others(self):
        from frappe_assistant_core.utils.migration_hooks import _sync_tool_configurations

        self.require_custom_tools()
        fac_config.ensure_disabled_tool_configuration()
        frappe.db.delete(fac_config.FAC_DOCTYPE, {"tool_name": "intelligence_request"})
        with mock.patch.object(frappe.db, "commit"):
            _sync_tool_configurations()
        self.assertEqual(frappe.db.get_value(fac_config.FAC_DOCTYPE, TOOL_NAME, "enabled"), 0)
        self.assertEqual(frappe.db.get_value(fac_config.FAC_DOCTYPE, "intelligence_request", "enabled"), 1)

    def test_fac_sync_would_create_an_enabled_row_without_the_helper(self):
        from frappe_assistant_core.utils.migration_hooks import _sync_tool_configurations

        self.require_custom_tools()
        with mock.patch.object(frappe.db, "commit"):
            _sync_tool_configurations()
        self.assertEqual(frappe.db.get_value(fac_config.FAC_DOCTYPE, TOOL_NAME, "enabled"), 1)

    def test_the_switch_still_blocks_the_tool_if_fac_enables_it(self):
        self.require_custom_tools()
        self.standard_set()
        fac_config.ensure_disabled_tool_configuration()
        frappe.db.set_value(fac_config.FAC_DOCTYPE, TOOL_NAME, "enabled", 1)
        self.fac.clear_cache()
        self.assertTrue(self.listed())
        with mock.patch.dict(frappe.local.conf, {"assistant_tools": {}}), mock.patch.object(
            EvaluateBusinessRules, "log_execution"
        ):
            report = self.fac.execute_tool(TOOL_NAME, self.arguments())
        self.assertFailsClosed(report, c.ERROR_TOOL_DISABLED)

    def test_end_to_end_through_fac_when_both_layers_are_open(self):
        self.require_custom_tools()
        self.standard_set()
        fac_config.ensure_disabled_tool_configuration()
        frappe.db.set_value(fac_config.FAC_DOCTYPE, TOOL_NAME, "enabled", 1)
        self.fac.clear_cache()
        with mock.patch.object(EvaluateBusinessRules, "log_execution"):
            report = self.fac.execute_tool(TOOL_NAME, self.arguments())
        self.assertEqual(report, self.direct())
        self.assertIs(report["execution_performed"], False)


if __name__ == "__main__":
    unittest.main()
