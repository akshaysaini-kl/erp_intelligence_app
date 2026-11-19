"""Integration tests: the Business Rules Engine on top of the real ERP Intelligence services.

These tests are read-only. They need an initialized Frappe site with the Business Rule
DocTypes installed and discover their data (a supplier, an item linked to it) from the
site instead of creating any. They never create, change or submit a Purchase Order.
Only the pilot rule set is seeded, inside the test transaction, and rolled back.

They prove that rules read ERP data through the existing services:

* record, child-table and aggregation reads go through ``IntelligenceService.execute_request``;
* field discovery goes through ``MetadataService``;
* reads run as the session user, so a user without access gets ERROR results, not passes.

Run with:
    bench --site <site> run-tests --module erp_intelligence.tests.test_business_rules_integration
"""

import ast
import pathlib
import unittest
from unittest import mock

import frappe
from frappe.utils import add_days, nowdate

import erp_intelligence.services.business_rules as rules_package
from erp_intelligence.services.business_rules import constants as c
from erp_intelligence.services.business_rules.data_access import RuleDataAccess
from erp_intelligence.services.business_rules.engine import BusinessRulesEngine
from erp_intelligence.services.business_rules.errors import RuleDataError
from erp_intelligence.services.business_rules.pilots.purchase_order_create import (
    seed_purchase_order_create_rule_set,
)
from erp_intelligence.services.intelligence_service import IntelligenceService
from erp_intelligence.services.metadata_service import MetadataService

FX: dict = {}
PACKAGE_ROOT = pathlib.Path(rules_package.__file__).parent


def setUpModule():
    if not getattr(frappe.local, "site", None):
        raise unittest.SkipTest("requires an initialized Frappe site")
    if not frappe.db.exists("DocType", "Business Rule Set"):
        raise unittest.SkipTest("Business Rule DocTypes are not installed on this site")

    frappe.set_user("Administrator")
    pairs = frappe.db.sql(
        """
        select s.name as supplier, i.parent as item
        from `tabItem Supplier` i
        join `tabSupplier` s on s.name = i.supplier
        join `tabItem` it on it.name = i.parent
        where s.disabled = 0 and it.disabled = 0 and it.is_purchase_item = 1
          and ifnull(s.default_price_list, '') != ''
        limit 1
        """,
        as_dict=True,
    )
    if pairs:
        supplier = frappe.db.get_value(
            "Supplier", pairs[0].supplier, ["name", "default_price_list", "gst_category", "gstin"], as_dict=True
        )
        item = frappe.db.get_value("Item", pairs[0].item, ["name", "stock_uom", "min_order_qty"], as_dict=True)
        FX.update(
            supplier=supplier,
            item=item,
            company=frappe.db.get_value("Company", {}, "name"),
            no_supplier_access=_user_without_read("Supplier"),
        )
    seed_purchase_order_create_rule_set(force=True)


def tearDownModule():
    frappe.set_user("Administrator")
    frappe.db.rollback()


def _user_without_read(doctype):
    users = frappe.db.get_all(
        "User",
        filters={"enabled": 1, "user_type": "System User", "name": ("not in", ["Administrator", "Guest"])},
        pluck="name",
    )
    for user in sorted(users):
        if "System Manager" in frappe.get_roles(user):
            continue
        if not frappe.has_permission(doctype, "read", user=user) and not frappe.has_permission(
            doctype, "select", user=user
        ):
            return user
    return None


def pre_execution_payload():
    supplier, item = FX["supplier"], FX["item"]
    due = add_days(nowdate(), 7)
    return {
        "supplier": supplier.name,
        "company": FX["company"],
        "transaction_date": nowdate(),
        "schedule_date": due,
        "currency": "INR",
        "conversion_rate": 1,
        "buying_price_list": supplier.default_price_list,
        "items": [
            {
                "item_code": item.name,
                "qty": max(1, int(item.min_order_qty or 1)),
                "uom": item.stock_uom,
                "price_list_rate": 100,
                "schedule_date": due,
            }
        ],
    }


class LiveCase(unittest.TestCase):
    def setUp(self):
        if not FX.get("supplier"):
            self.skipTest("no supplier linked to a purchasable item on this site")
        frappe.set_user("Administrator")
        self.addCleanup(frappe.set_user, "Administrator")
        # A failed read queues a user message; none of these tests should leak one.
        frappe.local.message_log = []


class TestDataAccessUsesERPIntelligence(LiveCase):
    def test_record_reads_go_through_intelligence_service(self):
        data = RuleDataAccess()
        with mock.patch.object(
            IntelligenceService, "execute_request", wraps=IntelligenceService.execute_request
        ) as spy:
            rows = data.fetch_records("Supplier", ["name", "disabled"], [("name", "=", FX["supplier"].name)])
        self.assertEqual([row["name"] for row in rows], [FX["supplier"].name])
        self.assertEqual(spy.call_count, 1)
        request = spy.call_args.args[0]
        self.assertEqual(request["doctype"], "Supplier")
        self.assertEqual(request["filters"], [{"field": "name", "operator": "=", "value": FX["supplier"].name}])

    def test_child_table_reads_use_parent_context(self):
        data = RuleDataAccess()
        with mock.patch.object(
            IntelligenceService, "execute_request", wraps=IntelligenceService.execute_request
        ) as spy:
            rows = data.fetch_child_records(
                "Item Supplier", "Item", [FX["item"].name], ["parent", "supplier"],
                [("supplier", "=", FX["supplier"].name)],
            )
        self.assertTrue(rows)
        self.assertEqual({row["parent"] for row in rows}, {FX["item"].name})
        request = spy.call_args.args[0]
        self.assertEqual(request["doctype"], "Item Supplier")
        self.assertEqual(
            request["parent_context"], {"parent_doctype": "Item", "parent_names": [FX["item"].name]}
        )

    def test_counts_use_aggregation(self):
        data = RuleDataAccess()
        with mock.patch.object(
            IntelligenceService, "execute_request", wraps=IntelligenceService.execute_request
        ) as spy:
            count = data.count_records("Supplier", [("name", "=", FX["supplier"].name)])
        self.assertEqual(count, 1)
        self.assertEqual(spy.call_args.args[0]["aggregations"], [{"operation": "count", "field": "name"}])

    def test_field_discovery_uses_metadata_service(self):
        data = RuleDataAccess()
        with mock.patch.object(
            MetadataService, "get_doctype_metadata", wraps=MetadataService.get_doctype_metadata
        ) as spy:
            fields = data.available_fields("Supplier")
            data.available_fields("Supplier")  # cached for the run
        self.assertIn("default_price_list", fields)
        self.assertEqual(spy.call_count, 1)

    def test_unknown_field_is_a_data_error_before_any_query(self):
        data = RuleDataAccess()
        with mock.patch.object(IntelligenceService, "execute_request") as spy:
            with self.assertRaises(RuleDataError) as caught:
                data.fetch_records("Supplier", ["name", "no_such_field"])
        self.assertEqual(caught.exception.code, "field_unavailable")
        spy.assert_not_called()

    def test_identical_requests_in_one_run_are_answered_once(self):
        data = RuleDataAccess()
        with mock.patch.object(
            IntelligenceService, "execute_request", wraps=IntelligenceService.execute_request
        ) as spy:
            for _ in range(3):
                data.fetch_records("Supplier", ["name"], [("name", "=", FX["supplier"].name)])
        self.assertEqual(spy.call_count, 1)

    def test_an_incomplete_page_is_never_returned_as_complete(self):
        data = RuleDataAccess()
        with mock.patch("erp_intelligence.services.business_rules.data_access.ConfigService.get_limit", return_value=1):
            with self.assertRaises(RuleDataError) as caught:
                data.fetch_records("Supplier", ["name"], page_length=1)
        self.assertEqual(caught.exception.code, "incomplete")

    def test_a_request_the_planner_cannot_resolve_is_a_data_error(self):
        data = RuleDataAccess()
        fake = mock.Mock()
        fake.execute_request.return_value = {"status": "unresolved", "result_type": "query_plan"}
        data = RuleDataAccess(intelligence_service=fake)
        with self.assertRaises(RuleDataError) as caught:
            data.fetch_records("Supplier", ["name"])
        self.assertEqual(caught.exception.code, "unresolved")

    def test_frappe_errors_are_translated_and_their_messages_discarded(self):
        fake = mock.Mock()
        data = RuleDataAccess(intelligence_service=fake)
        for error, code in ((frappe.PermissionError, "permission_denied"), (frappe.ValidationError, "request_invalid")):
            with self.subTest(error=error.__name__):
                def raising(*args, _error=error, **kwargs):
                    frappe.msgprint("internal detail")
                    raise _error("nope")

                fake.execute_request.side_effect = raising
                with self.assertRaises(RuleDataError) as caught:
                    data.fetch_records("Supplier", ["name"])
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(frappe.local.message_log, [])


class TestPilotAgainstRealData(LiveCase):
    def evaluate(self, stage, payload, **kwargs):
        return BusinessRulesEngine.evaluate("Purchase Order", "Create", payload, stage=stage, **kwargs)

    def test_pre_execution_runs_through_erp_intelligence_without_errors(self):
        with mock.patch.object(
            IntelligenceService, "execute_request", wraps=IntelligenceService.execute_request
        ) as spy:
            report = self.evaluate(c.PRE_EXECUTION, pre_execution_payload())
        self.assertGreater(spy.call_count, 5)
        self.assertEqual(report["summary"]["data_requests"], spy.call_count)
        statuses = {item["rule_code"]: item["status"] for item in report["results"]}
        self.assertEqual(report["summary"]["status_counts"][c.ERROR], 0, statuses)
        for code in (
            "PO_SUPPLIER_RESOLVES_ONE", "PO_SUPPLIER_ENABLED", "PO_ITEMS_RESOLVE_ONE",
            "PO_ITEMS_ENABLED_PURCHASABLE", "PO_ITEMS_LINKED_TO_SUPPLIER", "PO_ITEMS_QUANTITY_UOM",
            "PO_REQUIRED_BY_DATE_VALID",
        ):
            self.assertEqual(statuses[code], c.PASS, code)
        self.assertIs(report["execution_performed"], False)

    def test_item_not_linked_to_the_supplier_is_detected_from_real_data(self):
        orphan = frappe.db.sql(
            """
            select it.name from `tabItem` it
            where it.disabled = 0 and it.is_purchase_item = 1
              and not exists (select 1 from `tabItem Supplier` i where i.parent = it.name and i.supplier = %s)
            limit 1
            """,
            FX["supplier"].name,
        )
        if not orphan:
            self.skipTest("every purchasable item is linked to the supplier")
        payload = pre_execution_payload()
        payload["items"][0]["item_code"] = orphan[0][0]
        report = self.evaluate(c.PRE_EXECUTION, payload)
        result = next(item for item in report["results"] if item["rule_code"] == "PO_ITEMS_LINKED_TO_SUPPLIER")
        self.assertEqual(result["status"], c.FAIL)
        self.assertFalse(report["can_proceed"])

    def test_duplicate_guard_counts_through_aggregation(self):
        payload = pre_execution_payload()
        with mock.patch.object(
            IntelligenceService, "execute_request", wraps=IntelligenceService.execute_request
        ) as spy:
            report = self.evaluate(c.PRE_COMMIT, payload)
        result = next(item for item in report["results"] if item["rule_code"] == "PO_DUPLICATE_GUARD")
        self.assertNotEqual(result["status"], c.ERROR, result["message"])
        self.assertIn("matching_count", result["details"])
        aggregations = [call for call in spy.call_args_list if "aggregations" in call.args[0]]
        self.assertTrue(aggregations)

    def test_read_back_of_an_existing_order_flags_what_differs(self):
        rows = frappe.db.get_all("Purchase Order", fields=["name"], limit=1, order_by="creation desc")
        if not rows:
            self.skipTest("no Purchase Order exists to read back")
        before = frappe.db.count("Purchase Order")
        report = self.evaluate(
            c.POST_EXECUTION, pre_execution_payload(), execution_result={"name": rows[0]["name"]}
        )
        result = report["results"][0]
        # The sample payload is not that order, so the read-back must not pass.
        self.assertIn(result["status"], (c.FAIL, c.WARNING), result["message"])
        self.assertNotEqual(report["decision"], c.DECISION_PROCEED)
        self.assertEqual(frappe.db.count("Purchase Order"), before)

    def test_no_purchase_order_is_created_by_any_stage(self):
        before = frappe.db.count("Purchase Order")
        for stage in c.EXECUTION_STAGES:
            self.evaluate(stage, pre_execution_payload(), execution_result={"name": "PO-DOES-NOT-EXIST"})
        self.assertEqual(frappe.db.count("Purchase Order"), before)


class TestPermissions(LiveCase):
    def test_user_without_supplier_access_gets_errors_never_passes(self):
        user = FX.get("no_supplier_access")
        if not user:
            self.skipTest("no non-admin user without Supplier access on this site")
        frappe.set_user(user)
        report = BusinessRulesEngine.evaluate(
            "Purchase Order", "Create", pre_execution_payload(), stage=c.PRE_EXECUTION
        )
        by_code = {item["rule_code"]: item for item in report["results"]}
        for code in ("PO_SUPPLIER_RESOLVES_ONE", "PO_SUPPLIER_ENABLED"):
            self.assertEqual(by_code[code]["status"], c.ERROR, code)
            self.assertEqual(by_code[code]["details"]["error_type"], c.ERROR_TYPE_DATA_ACCESS)
        self.assertFalse(report["can_proceed"])
        self.assertEqual(report["decision"], c.DECISION_ERROR)

    def test_a_user_assertion_for_someone_else_is_refused(self):
        report = BusinessRulesEngine.evaluate(
            "Purchase Order", "Create", pre_execution_payload(), stage=c.PRE_EXECUTION, user="Guest"
        )
        by_code = {item["rule_code"]: item for item in report["results"]}
        self.assertEqual(by_code["PO_SUPPLIER_RESOLVES_ONE"]["status"], c.ERROR)
        self.assertFalse(report["can_proceed"])


class TestSafetyOfTheImplementation(unittest.TestCase):
    """Static checks on the engine source: no dynamic code, no writes, no site values."""

    BANNED_CALLS = {"eval", "exec", "compile", "__import__"}
    BANNED_MODULES = {"importlib", "subprocess", "os", "pickle", "marshal"}
    WRITE_METHODS = {
        "insert", "submit", "save", "delete", "db_set", "set_value", "delete_doc", "rename_doc",
        "cancel", "db_insert", "db_update", "run_workflow", "apply_workflow", "commit",
    }
    DATA_ACCESS_ATTRIBUTES = {"get_list", "get_all", "get_doc", "get_value", "get_cached_doc", "sql"}

    @classmethod
    def modules(cls):
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            if "pilots" in path.parts:
                continue
            yield path, ast.parse(path.read_text(), filename=str(path))

    def test_no_dynamic_code_execution_anywhere(self):
        for path, tree in self.modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, self.BANNED_CALLS, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], self.BANNED_MODULES, path.name)
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module.split(".")[0], self.BANNED_MODULES, path.name)

    def test_engine_and_evaluators_never_write_documents(self):
        for path, tree in self.modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in self.WRITE_METHODS:
                    self.fail(f"{path.name}:{node.lineno} uses .{node.attr}")

    def test_evaluators_and_data_access_read_only_through_erp_intelligence(self):
        for path, tree in self.modules():
            if path.name == "engine.py" or path.name == "registry.py":
                continue  # the engine reads rule configuration; it is checked below
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in self.DATA_ACCESS_ATTRIBUTES:
                    self.fail(f"{path.name}:{node.lineno} reads data directly with .{node.attr}")
                if isinstance(node, ast.Attribute) and node.attr == "db":
                    self.fail(f"{path.name}:{node.lineno} touches frappe.db")

    def test_engine_reads_only_rule_configuration_directly(self):
        tree = ast.parse((PACKAGE_ROOT / "engine.py").read_text())
        doctypes = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get_all", "get_doc")
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                doctypes.add(node.args[0].value)
        self.assertEqual(doctypes, {"Business Rule Set", "Business Rule"})

    def test_existing_tool_registration_is_unchanged(self):
        hooks = (PACKAGE_ROOT.parent.parent / "hooks.py").read_text()
        for tool in ("DescribeDocType", "QueryRecords", "AggregateRecords", "IntelligenceRequest"):
            self.assertIn(tool, hooks)
        self.assertNotIn("business_rule", hooks.lower())
