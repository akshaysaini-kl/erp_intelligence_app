"""Tests for the generic Business Rules Engine, its DocTypes and its registry.

The tests use synthetic rules and evaluators on a harmless target DocType (ToDo), so
they do not depend on Purchase Order data or on the pilot rule set. Every record is
created inside the test transaction and rolled back; nothing is committed.

Run with:
    bench --site <site> run-tests --module erp_intelligence.tests.test_business_rules_engine
"""

import json
import unittest
from unittest import mock

import frappe

from erp_intelligence.services.business_rules import constants as c
from erp_intelligence.services.business_rules.engine import BusinessRulesEngine
from erp_intelligence.services.business_rules.errors import (
    EvaluatorContractError,
    EvaluatorRegistrationError,
    RuleConfigurationError,
    RuleDataError,
)
from erp_intelligence.services.business_rules.parameters import (
    deep_merge,
    parse_json_object,
    resolve_parameters,
)
from erp_intelligence.services.business_rules.registry import EvaluatorRegistry, get_default_registry
from erp_intelligence.services.business_rules.results import build_result, validate_result

TARGET = "ToDo"
OPERATION = "Create"
SET_NAME = "TEST BRE Set"


def setUpModule():
    if not getattr(frappe.local, "site", None):
        raise unittest.SkipTest("requires an initialized Frappe site")
    if not frappe.db.exists("DocType", "Business Rule Set"):
        raise unittest.SkipTest("Business Rule DocTypes are not installed on this site")


def tearDownModule():
    frappe.set_user("Administrator")
    frappe.db.rollback()


def _registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry()
    registry.register("test.pass", lambda ctx: ctx.passed("ok", {"seen": ctx.rule.rule_code}))
    registry.register("test.fail", lambda ctx: ctx.failed("not allowed", {"reason": "x"}))
    registry.register("test.warning", lambda ctx: ctx.warning("be careful"))
    registry.register("test.needs_input", lambda ctx: ctx.needs_input("missing a", ["a"]))
    registry.register(
        "test.needs_confirmation",
        lambda ctx: ctx.passed("confirmed")
        if ctx.is_confirmed("k1")
        else ctx.needs_confirmation("please confirm", ["k1"]),
    )
    registry.register("test.error_result", lambda ctx: ctx.result(c.ERROR, "evaluator reports an error"))
    registry.register("test.raises", _raise_runtime)
    registry.register("test.data_error", _raise_data_error)
    registry.register("test.bad_contract", lambda ctx: {"status": "PASS"})
    registry.register(
        "test.confirm_without_flag",
        lambda ctx: {**ctx.passed("x"), "requires_confirmation": True},
    )
    registry.register(
        "test.params",
        lambda ctx: ctx.passed("params", {"parameters": dict(ctx.rule.parameters), "severity": ctx.rule.severity}),
    )
    return registry


def _raise_runtime(context):
    raise RuntimeError("boom")


def _raise_data_error(context):
    raise RuleDataError("cannot read", code="permission_denied")


def _make_rule(code, key="test.pass", *, stage=c.PRE_EXECUTION, blocking=1, enabled=1,
               severity=c.SEVERITY_CRITICAL, params=None):
    return frappe.get_doc(
        {
            "doctype": "Business Rule",
            "rule_code": code,
            "rule_name": code.title(),
            "rule_category": "Other",
            "evaluator_key": key,
            "execution_stage": stage,
            "default_severity": severity,
            "is_blocking": blocking,
            "enabled": enabled,
            "default_parameters": json.dumps(params) if params is not None else None,
        }
    ).insert(ignore_permissions=True)


def _make_set(assignments, *, name=SET_NAME, enabled=1, target=TARGET, operation=OPERATION):
    rows = []
    for item in assignments:
        code, sequence = item[0], item[1]
        extra = item[2] if len(item) > 2 else {}
        rows.append({"business_rule": code, "sequence": sequence, "enabled": 1, **extra})
    return frappe.get_doc(
        {
            "doctype": "Business Rule Set",
            "rule_set_name": name,
            "target_doctype": target,
            "operation": operation,
            "enabled": enabled,
            "version": "1",
            "rules": rows,
        }
    ).insert(ignore_permissions=True)


class EngineCase(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.registry = _registry()
        patcher = mock.patch.object(frappe, "logger", return_value=mock.MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(frappe.db.rollback)

    def evaluate(self, stage=c.PRE_EXECUTION, payload=None, **kwargs):
        return BusinessRulesEngine.evaluate(
            TARGET, OPERATION, {} if payload is None else payload, stage=stage,
            registry=self.registry, **kwargs,
        )

    @staticmethod
    def result_of(report, code):
        return next(item for item in report["results"] if item["rule_code"] == code)

    def single(self, key, *, blocking=1, **rule_kwargs):
        _make_rule("TEST_RULE", key, blocking=blocking, **rule_kwargs)
        _make_set([("TEST_RULE", 10)])
        return self.evaluate()


# --------------------------------------------------------------------------- DocTypes
class TestDocTypes(EngineCase):
    def test_doctype_structure_and_relationships(self):
        rule_set = frappe.get_meta("Business Rule Set")
        self.assertFalse(rule_set.istable)
        self.assertEqual(rule_set.module, "ERP Intelligence")
        self.assertEqual(rule_set.get_field("rules").fieldtype, "Table")
        self.assertEqual(rule_set.get_field("rules").options, "Business Rule Assignment")
        for fieldname in ("rule_set_name", "target_doctype", "operation", "enabled", "version", "description"):
            self.assertIsNotNone(rule_set.get_field(fieldname), fieldname)

        assignment = frappe.get_meta("Business Rule Assignment")
        self.assertTrue(assignment.istable)
        self.assertEqual(assignment.get_field("business_rule").fieldtype, "Link")
        self.assertEqual(assignment.get_field("business_rule").options, "Business Rule")
        for fieldname in ("sequence", "enabled", "parameters_override", "severity_override"):
            self.assertIsNotNone(assignment.get_field(fieldname), fieldname)

        rule = frappe.get_meta("Business Rule")
        self.assertFalse(rule.istable)
        self.assertEqual(rule.get_field("rule_code").unique, 1)
        self.assertEqual(rule.get_field("default_parameters").fieldtype, "JSON")
        for fieldname in (
            "rule_name", "description", "rule_category", "evaluator_key", "execution_stage",
            "default_severity", "is_blocking", "enabled",
        ):
            self.assertIsNotNone(rule.get_field(fieldname), fieldname)
        self.assertEqual(
            rule.get_field("execution_stage").options.split("\n"),
            [c.PRE_EXECUTION, c.PRE_COMMIT, c.POST_EXECUTION],
        )

    def test_rule_can_be_assigned_to_several_sets(self):
        _make_rule("TEST_SHARED")
        _make_set([("TEST_SHARED", 10)], name="TEST BRE A", operation="Create")
        _make_set([("TEST_SHARED", 10)], name="TEST BRE B", operation="Update")
        used_in = frappe.get_all(
            "Business Rule Assignment", filters={"business_rule": "TEST_SHARED"}, pluck="parent"
        )
        self.assertCountEqual(used_in, ["TEST BRE A", "TEST BRE B"])

    def test_rule_code_is_unique(self):
        _make_rule("TEST_UNIQUE")
        with self.assertRaises(frappe.DuplicateEntryError):
            _make_rule("TEST_UNIQUE")

    def test_rule_validation(self):
        with self.assertRaises(frappe.ValidationError):
            _make_rule("lower_case")
        with self.assertRaises(frappe.ValidationError):
            _make_rule("TEST_BAD_KEY", "Not A Key")
        doc = _make_rule("TEST_JSON")
        for bad in ("{not json", "[1, 2]", "5"):
            with self.subTest(default_parameters=bad):
                doc.default_parameters = bad
                with self.assertRaises(frappe.ValidationError):
                    doc.save()

    def test_rule_set_validation(self):
        _make_rule("TEST_A", params={"limit": 1})
        _make_set([("TEST_A", 10)])
        with self.assertRaises(frappe.ValidationError, msg="second enabled set for same target/operation"):
            _make_set([("TEST_A", 10)], name="TEST BRE Second")
        _make_set([("TEST_A", 10)], name="TEST BRE Disabled", enabled=0)  # disabled is allowed
        with self.assertRaises(frappe.ValidationError, msg="duplicate assignment"):
            _make_set([("TEST_A", 10), ("TEST_A", 20)], name="TEST BRE Dup", operation="Delete")
        with self.assertRaises(frappe.ValidationError, msg="override of an undeclared parameter"):
            _make_set(
                [("TEST_A", 10, {"parameters_override": json.dumps({"typo": 2})})],
                name="TEST BRE Typo", operation="Cancel",
            )
        with self.assertRaises(frappe.ValidationError, msg="override must be an object"):
            _make_set(
                [("TEST_A", 10, {"parameters_override": "[1]"})],
                name="TEST BRE Arr", operation="Submit",
            )


# ------------------------------------------------------------------------ rule sets
class TestRuleSetResolution(EngineCase):
    def test_resolves_by_target_doctype_and_operation(self):
        _make_rule("TEST_RULE")
        _make_set([("TEST_RULE", 10)], name="TEST BRE Create", operation="Create")
        _make_set([("TEST_RULE", 10)], name="TEST BRE Update", operation="Update")
        report = BusinessRulesEngine.evaluate(
            TARGET, "Update", {}, stage=c.PRE_EXECUTION, registry=self.registry
        )
        self.assertEqual(report["rule_set"]["name"], "TEST BRE Update")
        self.assertEqual(report["doctype"], TARGET)
        self.assertEqual(report["operation"], "Update")
        self.assertEqual(report["decision"], c.DECISION_PROCEED)

    def test_missing_rule_set_fails_closed(self):
        report = self.evaluate()
        self.assertEqual(report["error_code"], c.ERROR_RULE_SET_NOT_FOUND)
        self.assertEqual(report["decision"], c.DECISION_ERROR)
        self.assertFalse(report["can_proceed"])
        self.assertEqual(report["results"], [])

    def test_disabled_rule_set_fails_closed(self):
        _make_rule("TEST_RULE")
        _make_set([("TEST_RULE", 10)], enabled=0)
        report = self.evaluate()
        self.assertEqual(report["error_code"], c.ERROR_RULE_SET_DISABLED)
        self.assertFalse(report["can_proceed"])

    def test_two_enabled_sets_are_ambiguous(self):
        _make_rule("TEST_RULE")
        _make_set([("TEST_RULE", 10)], name="TEST BRE One")
        _make_set([("TEST_RULE", 10)], name="TEST BRE Two", enabled=0)
        frappe.db.set_value("Business Rule Set", "TEST BRE Two", "enabled", 1)  # bypass the controller
        report = self.evaluate()
        self.assertEqual(report["error_code"], c.ERROR_RULE_SET_AMBIGUOUS)
        self.assertFalse(report["can_proceed"])

    def test_rule_set_without_enabled_assignments_fails_closed(self):
        _make_rule("TEST_RULE")
        doc = _make_set([("TEST_RULE", 10)])
        frappe.db.set_value("Business Rule Assignment", doc.rules[0].name, "enabled", 0)
        report = self.evaluate()
        self.assertEqual(report["error_code"], c.ERROR_RULE_SET_EMPTY)
        self.assertFalse(report["can_proceed"])

    def test_invalid_requests_fail_closed(self):
        for kwargs in (
            {"stage": "NOT_A_STAGE"},
            {"payload": ["not", "a", "mapping"]},
            {"payload": {"x": object()}},
            {"confirmations": 5},
        ):
            with self.subTest(kwargs=kwargs):
                args = {"stage": c.PRE_EXECUTION, "payload": {}}
                args.update(kwargs)
                report = BusinessRulesEngine.evaluate(
                    TARGET, OPERATION, args.pop("payload"), registry=self.registry, **args
                )
                self.assertEqual(report["error_code"], c.ERROR_INVALID_REQUEST)
                self.assertFalse(report["can_proceed"])


# ----------------------------------------------------------------------- ordering
class TestAssignmentOrdering(EngineCase):
    def test_assignments_run_by_sequence_then_row_order(self):
        order = []
        self.registry.register(
            "test.record", lambda ctx: (order.append(ctx.rule.rule_code), ctx.passed("ok"))[1]
        )
        for code in ("TEST_C", "TEST_B", "TEST_A", "TEST_D"):
            _make_rule(code, "test.record")
        # Row order is C, B, A, D. D and C share a sequence, so row order breaks the tie.
        _make_set([("TEST_C", 30), ("TEST_B", 20), ("TEST_A", 10), ("TEST_D", 30)])
        report = self.evaluate()
        self.assertEqual(order, ["TEST_A", "TEST_B", "TEST_C", "TEST_D"])
        self.assertEqual([item["rule_code"] for item in report["results"]], order)
        self.assertEqual([item["sequence"] for item in report["results"]], [10, 20, 30, 30])


# ------------------------------------------------------------------------ registry
class TestRegistry(EngineCase):
    def test_register_and_dispatch(self):
        report = self.single("test.pass")
        self.assertEqual(self.result_of(report, "TEST_RULE")["details"]["seen"], "TEST_RULE")

    def test_duplicate_and_invalid_registration(self):
        registry = EvaluatorRegistry()
        registry.register("a.b", lambda ctx: None)
        with self.assertRaises(EvaluatorRegistrationError):
            registry.register("a.b", lambda ctx: None)
        registry.register("a.b", lambda ctx: None, replace=True)
        for bad_key in ("", "Has Space", "UPPER", "a..b", 5):
            with self.assertRaises(EvaluatorRegistrationError):
                registry.register(bad_key, lambda ctx: None)
        with self.assertRaises(EvaluatorRegistrationError):
            registry.register("not.callable", "print('x')")
        self.assertEqual(registry.keys(), ["a.b"])
        registry.unregister("a.b")
        self.assertIsNone(registry.get("a.b"))
        self.assertIsNone(registry.get(None))

    def test_unregistered_evaluator_is_an_error_not_a_pass(self):
        report = self.single("test.not_registered")
        result = self.result_of(report, "TEST_RULE")
        self.assertEqual(result["status"], c.ERROR)
        self.assertEqual(result["details"]["error_type"], c.ERROR_TYPE_UNREGISTERED_EVALUATOR)
        self.assertEqual(report["decision"], c.DECISION_ERROR)
        self.assertFalse(report["can_proceed"])
        self.assertEqual(report["blocking_errors"], ["TEST_RULE"])

    def test_evaluator_key_is_never_executed_as_code(self):
        sentinel = []
        self.registry.register("test.sentinel", lambda ctx: sentinel.append(1) or ctx.passed("ok"))
        # A key that looks like code is only a lookup string. It matches nothing.
        report = self.single("os.system", severity=c.SEVERITY_CRITICAL)
        self.assertEqual(self.result_of(report, "TEST_RULE")["details"]["error_type"],
                         c.ERROR_TYPE_UNREGISTERED_EVALUATOR)
        self.assertEqual(sentinel, [])

    def test_default_registry_contains_every_pilot_evaluator(self):
        from erp_intelligence.services.business_rules.pilots.purchase_order_create import RULES

        registry = get_default_registry()
        for rule in RULES:
            self.assertIn(rule["evaluator_key"], registry, rule["rule_code"])
        self.assertIs(get_default_registry(), registry)


# ---------------------------------------------------------------------- parameters
class TestParameters(EngineCase):
    def test_defaults_apply_without_override(self):
        _make_rule("TEST_RULE", "test.params", params={"limit": 5, "nested": {"a": 1, "b": 2}})
        _make_set([("TEST_RULE", 10)])
        result = self.result_of(self.evaluate(), "TEST_RULE")
        self.assertEqual(result["details"]["parameters"], {"limit": 5, "nested": {"a": 1, "b": 2}})

    def test_assignment_override_replaces_scalars_and_merges_objects(self):
        _make_rule("TEST_RULE", "test.params", params={"limit": 5, "nested": {"a": 1, "b": 2}, "items": [1, 2]})
        override = json.dumps({"limit": 9, "nested": {"b": 3}, "items": [7]})
        _make_set([("TEST_RULE", 10, {"parameters_override": override})])
        result = self.result_of(self.evaluate(), "TEST_RULE")
        self.assertEqual(
            result["details"]["parameters"],
            {"limit": 9, "nested": {"a": 1, "b": 3}, "items": [7]},
        )

    def test_same_rule_has_different_parameters_in_different_sets(self):
        _make_rule("TEST_RULE", "test.params", params={"limit": 5})
        _make_set([("TEST_RULE", 10, {"parameters_override": json.dumps({"limit": 1})})], name="TEST BRE A")
        _make_set([("TEST_RULE", 10)], name="TEST BRE B", operation="Update")
        first = self.result_of(self.evaluate(), "TEST_RULE")
        second = self.result_of(
            BusinessRulesEngine.evaluate(TARGET, "Update", {}, stage=c.PRE_EXECUTION, registry=self.registry),
            "TEST_RULE",
        )
        self.assertEqual(first["details"]["parameters"], {"limit": 1})
        self.assertEqual(second["details"]["parameters"], {"limit": 5})

    def test_severity_override(self):
        _make_rule("TEST_RULE", "test.params", severity=c.SEVERITY_INFO)
        _make_set([("TEST_RULE", 10, {"severity_override": c.SEVERITY_WARNING})])
        result = self.result_of(self.evaluate(), "TEST_RULE")
        self.assertEqual(result["severity"], c.SEVERITY_WARNING)
        self.assertEqual(result["details"]["severity"], c.SEVERITY_WARNING)

    def test_undeclared_override_stored_directly_becomes_an_error_result(self):
        _make_rule("TEST_RULE", "test.params", params={"limit": 5})
        doc = _make_set([("TEST_RULE", 10)])
        frappe.db.set_value(
            "Business Rule Assignment", doc.rules[0].name, "parameters_override", json.dumps({"typo": 1})
        )
        report = self.evaluate()
        result = self.result_of(report, "TEST_RULE")
        self.assertEqual(result["status"], c.ERROR)
        self.assertEqual(result["details"]["error_type"], c.ERROR_TYPE_CONFIGURATION)
        self.assertFalse(report["can_proceed"])

    def test_parameter_helpers(self):
        self.assertEqual(parse_json_object(None, "x"), {})
        self.assertEqual(parse_json_object("  ", "x"), {})
        self.assertEqual(parse_json_object({"a": 1}, "x"), {"a": 1})
        for bad in ("{bad", "[1]", "5", 7):
            with self.assertRaises(RuleConfigurationError):
                parse_json_object(bad, "x")
        self.assertEqual(deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}), {"a": {"b": 1, "c": 2}})
        with self.assertRaises(RuleConfigurationError):
            resolve_parameters('{"a": 1}', '{"z": 1}', "rule")


# ------------------------------------------------------------------------- results
class TestResultContract(EngineCase):
    def assert_contract(self, result):
        self.assertEqual(
            set(result) & set(c.RESULT_FIELDS), set(c.RESULT_FIELDS), "every contract field is present"
        )
        self.assertIn(result["status"], c.RESULT_STATUSES)
        self.assertIn(result["severity"], c.SEVERITIES)
        self.assertIsInstance(result["is_blocking"], bool)
        self.assertIsInstance(result["message"], str)
        self.assertIsInstance(result["details"], dict)
        self.assertIsInstance(result["required_inputs"], list)
        self.assertIsInstance(result["requires_confirmation"], bool)

    def test_every_status_is_reported_in_the_contract_shape(self):
        expectations = {
            "test.pass": c.PASS,
            "test.fail": c.FAIL,
            "test.warning": c.WARNING,
            "test.needs_input": c.NEEDS_INPUT,
            "test.needs_confirmation": c.NEEDS_CONFIRMATION,
            "test.error_result": c.ERROR,
        }
        for index, (key, status) in enumerate(expectations.items(), start=1):
            _make_rule(f"TEST_R{index}", key)
        _make_set([(f"TEST_R{index}", index * 10) for index in range(1, len(expectations) + 1)])
        report = self.evaluate()
        self.assertEqual(report["returned_count"], len(expectations))
        for index, (key, status) in enumerate(expectations.items(), start=1):
            result = self.result_of(report, f"TEST_R{index}")
            self.assert_contract(result)
            self.assertEqual(result["status"], status, key)
        self.assertEqual(report["summary"]["status_counts"], dict.fromkeys(c.RESULT_STATUSES, 1))

    def test_needs_input_lists_required_inputs(self):
        result = self.result_of(self.single("test.needs_input"), "TEST_RULE")
        self.assertEqual(result["required_inputs"], ["a"])
        self.assertFalse(result["requires_confirmation"])

    def test_needs_confirmation_sets_the_flag_and_lists_keys(self):
        result = self.result_of(self.single("test.needs_confirmation"), "TEST_RULE")
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["details"]["confirmation_keys"], ["k1"])

    def test_business_failure_differs_from_evaluator_error(self):
        failed = self.result_of(self.single("test.fail"), "TEST_RULE")
        self.assertEqual(failed["status"], c.FAIL)
        self.assertNotIn("error_type", failed["details"])
        frappe.db.rollback()
        errored = self.result_of(self.single("test.raises"), "TEST_RULE")
        self.assertEqual(errored["status"], c.ERROR)
        self.assertEqual(errored["details"]["error_type"], c.ERROR_TYPE_EXCEPTION)

    def test_evaluator_exception_is_contained_and_reported(self):
        report = self.single("test.raises")
        result = self.result_of(report, "TEST_RULE")
        self.assertEqual(result["status"], c.ERROR)
        self.assertIn("boom", result["message"])
        self.assertEqual(report["decision"], c.DECISION_ERROR)
        self.assertEqual(report["blocking_failures"], [])
        self.assertEqual(report["blocking_errors"], ["TEST_RULE"])

    def test_data_error_is_an_error_result_with_its_code(self):
        result = self.result_of(self.single("test.data_error"), "TEST_RULE")
        self.assertEqual(result["status"], c.ERROR)
        self.assertEqual(result["details"]["error_type"], c.ERROR_TYPE_DATA_ACCESS)
        self.assertEqual(result["details"]["data_error_code"], "permission_denied")

    def test_contract_violations_are_errors(self):
        for key in ("test.bad_contract", "test.confirm_without_flag"):
            with self.subTest(key=key):
                result = self.result_of(self.single(key), "TEST_RULE")
                self.assertEqual(result["status"], c.ERROR)
                self.assertEqual(result["details"]["error_type"], c.ERROR_TYPE_CONTRACT)
                frappe.db.rollback()

    def test_validate_result_rules(self):
        good = build_result(
            rule_code="R", status=c.PASS, severity=c.SEVERITY_INFO, is_blocking=True, message="m"
        )
        validate = lambda raw: validate_result(  # noqa: E731
            raw, rule_code="R", severity=c.SEVERITY_INFO, is_blocking=True
        )
        self.assertEqual(validate(good)["status"], c.PASS)
        for mutation in (
            {"rule_code": "OTHER"},
            {"status": "MAYBE"},
            {"severity": c.SEVERITY_CRITICAL},
            {"is_blocking": False},
            {"message": " "},
            {"details": []},
            {"required_inputs": ["a"]},  # inputs only allowed with NEEDS_INPUT
            {"requires_confirmation": True},  # only allowed with NEEDS_CONFIRMATION
            {"status": c.NEEDS_INPUT},  # NEEDS_INPUT without inputs
            {"status": c.NEEDS_CONFIRMATION, "requires_confirmation": True},  # no confirmation_keys
            {"details": {"x": object()}},
        ):
            with self.assertRaises(EvaluatorContractError, msg=str(mutation)):
                validate({**good, **mutation})
        with self.assertRaises(EvaluatorContractError):
            validate({**good, "extra": 1})
        with self.assertRaises(EvaluatorContractError):
            validate(None)
        missing = dict(good)
        del missing["details"]
        with self.assertRaises(EvaluatorContractError):
            validate(missing)


# ----------------------------------------------------------------------- decisions
class TestDecisions(EngineCase):
    def decision_for(self, *specs):
        """specs: (evaluator key, blocking). Returns the report for a set of those rules."""
        for index, (key, blocking) in enumerate(specs, start=1):
            _make_rule(f"TEST_R{index}", key, blocking=blocking)
        _make_set([(f"TEST_R{index}", index * 10) for index in range(1, len(specs) + 1)])
        return self.evaluate()

    def test_all_pass_proceeds(self):
        report = self.decision_for(("test.pass", 1), ("test.pass", 1))
        self.assertEqual(report["decision"], c.DECISION_PROCEED)
        self.assertTrue(report["can_proceed"])
        self.assertTrue(report["summary"]["all_passed"])

    def test_blocking_fail_blocks(self):
        report = self.decision_for(("test.pass", 1), ("test.fail", 1))
        self.assertEqual(report["decision"], c.DECISION_BLOCKED)
        self.assertFalse(report["can_proceed"])
        self.assertEqual(report["blocking_failures"], ["TEST_R2"])

    def test_non_blocking_fail_does_not_block_but_is_reported(self):
        report = self.decision_for(("test.pass", 1), ("test.fail", 0))
        self.assertEqual(report["decision"], c.DECISION_PROCEED_WITH_WARNINGS)
        self.assertTrue(report["can_proceed"])
        self.assertEqual(report["non_blocking_issues"], [{"rule_code": "TEST_R2", "status": c.FAIL}])
        self.assertFalse(report["summary"]["all_passed"])

    def test_warning_proceeds_with_warnings(self):
        report = self.decision_for(("test.warning", 1))
        self.assertEqual(report["decision"], c.DECISION_PROCEED_WITH_WARNINGS)
        self.assertTrue(report["can_proceed"])
        self.assertEqual(report["warnings"], ["TEST_R1"])

    def test_missing_input_is_not_a_success(self):
        report = self.decision_for(("test.pass", 1), ("test.needs_input", 1))
        self.assertEqual(report["decision"], c.DECISION_NEEDS_INPUT)
        self.assertFalse(report["can_proceed"])
        self.assertFalse(report["summary"]["all_passed"])
        self.assertEqual(report["pending_inputs"], [{"rule_code": "TEST_R2", "required_inputs": ["a"]}])

    def test_pending_confirmation_is_not_a_success(self):
        report = self.decision_for(("test.needs_confirmation", 1))
        self.assertEqual(report["decision"], c.DECISION_NEEDS_CONFIRMATION)
        self.assertFalse(report["can_proceed"])
        self.assertEqual(report["pending_confirmations"][0]["confirmation_keys"], ["k1"])

    def test_confirmation_given_by_caller_resolves_it(self):
        report = self.decision_for(("test.needs_confirmation", 1))
        self.assertFalse(report["can_proceed"])
        for confirmations in ({"k1": True}, ["k1"]):
            with self.subTest(confirmations=confirmations):
                again = self.evaluate(confirmations=confirmations)
                self.assertEqual(again["decision"], c.DECISION_PROCEED)
        for ignored in ({"k1": False}, {"k1": "yes"}, {"other": True}):
            with self.subTest(ignored=ignored):
                self.assertEqual(self.evaluate(confirmations=ignored)["decision"], c.DECISION_NEEDS_CONFIRMATION)

    def test_non_blocking_pending_items_do_not_stop_but_are_not_passes(self):
        report = self.decision_for(("test.needs_input", 0), ("test.needs_confirmation", 0))
        self.assertTrue(report["can_proceed"])
        self.assertEqual(report["decision"], c.DECISION_PROCEED_WITH_WARNINGS)
        self.assertEqual(len(report["non_blocking_issues"]), 2)
        self.assertFalse(report["summary"]["all_passed"])

    def test_blocking_error_outranks_everything(self):
        report = self.decision_for(("test.fail", 1), ("test.needs_input", 1), ("test.raises", 1))
        self.assertEqual(report["decision"], c.DECISION_ERROR)
        self.assertFalse(report["can_proceed"])
        self.assertEqual(report["blocking_failures"], ["TEST_R1"])
        self.assertEqual(report["blocking_errors"], ["TEST_R3"])

    def test_non_blocking_error_is_reported_without_blocking(self):
        report = self.decision_for(("test.pass", 1), ("test.raises", 0))
        self.assertTrue(report["can_proceed"])
        self.assertEqual(report["non_blocking_issues"], [{"rule_code": "TEST_R2", "status": c.ERROR}])

    def test_pending_input_outranks_pending_confirmation(self):
        report = self.decision_for(("test.needs_confirmation", 1), ("test.needs_input", 1))
        self.assertEqual(report["decision"], c.DECISION_NEEDS_INPUT)
        self.assertEqual(len(report["pending_confirmations"]), 1)

    def test_one_failing_rule_does_not_stop_the_others(self):
        report = self.decision_for(("test.raises", 1), ("test.pass", 1), ("test.fail", 1))
        self.assertEqual([item["status"] for item in report["results"]], [c.ERROR, c.PASS, c.FAIL])


# --------------------------------------------------------------------- stages/skips
class TestStagesAndSkips(EngineCase):
    def test_only_rules_of_the_requested_stage_run(self):
        _make_rule("TEST_PRE", stage=c.PRE_EXECUTION)
        _make_rule("TEST_COMMIT", "test.fail", stage=c.PRE_COMMIT)
        _make_rule("TEST_POST", stage=c.POST_EXECUTION)
        _make_set([("TEST_PRE", 10), ("TEST_COMMIT", 20), ("TEST_POST", 30)])

        pre = self.evaluate(c.PRE_EXECUTION)
        self.assertEqual([item["rule_code"] for item in pre["results"]], ["TEST_PRE"])
        self.assertTrue(pre["can_proceed"])
        skipped = {item["rule_code"]: item["reason"] for item in pre["skipped"]}
        self.assertEqual(skipped, {"TEST_COMMIT": "other_stage", "TEST_POST": "other_stage"})

        commit = self.evaluate(c.PRE_COMMIT)
        self.assertEqual(commit["decision"], c.DECISION_BLOCKED)
        post = self.evaluate(c.POST_EXECUTION)
        self.assertEqual([item["rule_code"] for item in post["results"]], ["TEST_POST"])

    def test_stage_with_no_rules_evaluates_nothing_and_says_so(self):
        _make_rule("TEST_PRE")
        _make_set([("TEST_PRE", 10)])
        report = self.evaluate(c.POST_EXECUTION)
        self.assertEqual(report["results"], [])
        self.assertEqual(report["summary"]["rules_evaluated"], 0)
        self.assertFalse(report["summary"]["all_passed"])
        self.assertTrue(report["notes"])

    def test_disabled_rule_and_disabled_assignment_are_skipped_and_reported(self):
        _make_rule("TEST_ON")
        _make_rule("TEST_OFF_RULE", "test.fail", enabled=0)
        _make_rule("TEST_OFF_ASSIGNMENT", "test.fail")
        doc = _make_set([("TEST_ON", 10), ("TEST_OFF_RULE", 20), ("TEST_OFF_ASSIGNMENT", 30)])
        frappe.db.set_value("Business Rule Assignment", doc.rules[2].name, "enabled", 0)
        report = self.evaluate()
        self.assertEqual([item["rule_code"] for item in report["results"]], ["TEST_ON"])
        self.assertEqual(
            {item["rule_code"]: item["reason"] for item in report["skipped"]},
            {"TEST_OFF_RULE": "rule_disabled", "TEST_OFF_ASSIGNMENT": "assignment_disabled"},
        )
        self.assertTrue(report["can_proceed"])

    def test_assignment_to_a_missing_rule_is_an_error(self):
        _make_rule("TEST_ON")
        _make_rule("TEST_GONE")
        doc = _make_set([("TEST_ON", 10), ("TEST_GONE", 20)])
        frappe.db.set_value("Business Rule Assignment", doc.rules[1].name, "business_rule", "TEST_NEVER_EXISTED")
        report = self.evaluate()
        result = self.result_of(report, "TEST_NEVER_EXISTED")
        self.assertEqual(result["status"], c.ERROR)
        self.assertFalse(report["can_proceed"])


# ----------------------------------------------------------------------- safety
class TestEngineIsReadOnly(EngineCase):
    def test_engine_does_not_change_or_create_documents(self):
        _make_rule("TEST_RULE")
        _make_set([("TEST_RULE", 10)])
        tracked = ("ToDo", "Purchase Order", "Business Rule", "Business Rule Set", "Version")
        before = {name: frappe.db.count(name) for name in tracked}
        report = self.evaluate(payload={"description": "x"})
        after = {name: frappe.db.count(name) for name in tracked}
        self.assertEqual(before, after)
        self.assertIs(report["execution_performed"], False)

    def test_evaluators_cannot_change_the_payload_seen_by_later_rules(self):
        seen = []

        def mutator(ctx):
            ctx.payload["injected"] = True
            ctx.payload["items"].append("x")
            return ctx.passed("ok")

        self.registry.register("test.mutator", mutator)
        self.registry.register("test.observer", lambda ctx: (seen.append(dict(ctx.payload)), ctx.passed("ok"))[1])
        _make_rule("TEST_A", "test.mutator")
        _make_rule("TEST_B", "test.observer")
        _make_set([("TEST_A", 10), ("TEST_B", 20)])
        original = {"items": []}
        self.evaluate(payload=original)
        self.assertEqual(seen, [{"items": []}])
        self.assertEqual(original, {"items": []})

    def test_report_is_json_serializable(self):
        _make_rule("TEST_RULE", "test.params", params={"limit": 1})
        _make_set([("TEST_RULE", 10)])
        json.dumps(self.evaluate())
