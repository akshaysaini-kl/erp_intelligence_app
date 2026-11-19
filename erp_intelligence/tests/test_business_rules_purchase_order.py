"""Tests for the Purchase Order pilot rule set.

These run the real pilot rule set and the real evaluators through the real engine. Only
the ERP data source is replaced: ``FakeData`` answers the same calls as
``RuleDataAccess`` from in-memory fixtures, so the data is controlled and no Purchase
Order or master record is ever created. The pilot rule set is seeded inside the test
transaction and rolled back.

``test_business_rules_integration`` covers the real ERP Intelligence path.

Run with:
    bench --site <site> run-tests --module erp_intelligence.tests.test_business_rules_purchase_order
"""

import contextlib
import copy
import json
import unittest
from decimal import Decimal

import frappe
from frappe.utils import add_days, add_to_date, nowdate, now_datetime

from erp_intelligence.services.business_rules import constants as c
from erp_intelligence.services.business_rules.engine import BusinessRulesEngine
from erp_intelligence.services.business_rules.errors import RuleDataError
from erp_intelligence.services.business_rules.pilots.purchase_order_create import (
    RULES,
    seed_purchase_order_create_rule_set,
)

TODAY = nowdate()
IN_SEVEN_DAYS = add_days(TODAY, 7)
YESTERDAY = add_days(TODAY, -1)
IN_STATE = "Input GST In-state - DPL"
OUT_STATE = "Input GST Out-state - DPL"
RCM_IN_STATE = "Input GST RCM In-state - DPL"


def setUpModule():
    if not getattr(frappe.local, "site", None):
        raise unittest.SkipTest("requires an initialized Frappe site")
    if not frappe.db.exists("DocType", "Business Rule Set"):
        raise unittest.SkipTest("Business Rule DocTypes are not installed on this site")


def tearDownModule():
    frappe.set_user("Administrator")
    frappe.db.rollback()


# ------------------------------------------------------------------------ fixtures
def _row(**values):
    return values


def _supplier(name, supplier_name=None, **over):
    return _row(
        **{
            "name": name, "supplier_name": supplier_name or name, "disabled": 0, "on_hold": 0,
            "hold_type": "", "release_date": None, "prevent_pos": 0, "default_price_list": "PL-A",
            "default_currency": "INR", "gst_category": "Registered Regular",
            "gstin": "07AAAAA0000A1Z5", **over,
        }
    )


def _item(name, item_name=None, **over):
    return _row(
        **{
            "name": name, "item_name": item_name or name, "disabled": 0, "end_of_life": None,
            "is_purchase_item": 1, "stock_uom": "Pcs", "min_order_qty": 0, **over,
        }
    )


def _price(name, item, **over):
    return _row(
        **{
            "name": name, "item_code": item, "price_list": "PL-A", "supplier": "SUP-A",
            "price_list_rate": 100, "currency": "INR", "valid_from": "2020-01-01",
            "valid_upto": None, "uom": None, "workflow_state": "Approved", "custom_disable": 0, **over,
        }
    )


def _tax_row(parent, account, rate, **over):
    return _row(
        **{
            "parent": parent, "account_head": account, "rate": rate, "charge_type": "On Net Total",
            "category": "Total", "add_deduct_tax": "Add", "description": account.split()[-1],
            "idx": 1, "tax_amount": 0, "included_in_print_rate": 0, **over,
        }
    )


def make_world():
    linked = ["SUP-A", "SUP-B", "SUP-MH", "SUP-UNREG", "SUP-USD", "SUP-A2", "SUP-OFFPL", "SUP-NOPL"]
    return {
        "tables": {
            "Supplier": [
                _supplier("SUP-A", "Alpha Traders"),
                _supplier("SUP-A2", "Alpha Textiles"),
                _supplier("SUP-B", "Beta Imports", gstin="19BBBBB0000B1Z5", default_price_list="PL-B"),
                _supplier("SUP-MH", "Maha Goods", gstin="27CCCCC0000C1Z5"),
                _supplier("SUP-UNREG", "Unreg Co", gst_category="Unregistered", gstin=None),
                _supplier("SUP-USD", "Dollar Co", default_price_list="PL-USD", default_currency="USD"),
                _supplier("SUP-HOLD", "Hold Co", prevent_pos=1),
                _supplier("SUP-ONHOLD", "On Hold Co", on_hold=1, hold_type="All"),
                _supplier("SUP-OFF", "Off Co", disabled=1),
                _supplier("SUP-OFFPL", "Off List Co", default_price_list="PL-OFF"),
                _supplier("SUP-NOPL", "No List Co", default_price_list=None),
                _supplier("SUP-ODD", "Odd Co", gst_category="Input Service Distributor"),
            ],
            "Price List": [
                _row(name="PL-A", enabled=1, buying=1, currency="INR"),
                _row(name="PL-B", enabled=1, buying=1, currency="INR"),
                _row(name="PL-USD", enabled=1, buying=1, currency="USD"),
                _row(name="PL-OFF", enabled=0, buying=1, currency="INR"),
                _row(name="PL-SELL", enabled=1, buying=0, currency="INR"),
            ],
            "Company": [_row(name="TEST-CO", default_currency="INR")],
            "Item": [
                _item("ITEM-1", "Blue Widget", min_order_qty=10),
                _item("ITEM-2", "Red Gadget"),
                _item("ITEM-OFF", "Old Thing", disabled=1),
                _item("ITEM-NOBUY", "Not For Sale", is_purchase_item=0),
                _item("ITEM-OLD", "Retired", end_of_life=YESTERDAY),
                _item("ITEM-ORPHAN", "Unlinked"),
            ],
            "UOM": [
                _row(name="Pcs", must_be_whole_number=1),
                _row(name="Box", must_be_whole_number=1),
                _row(name="Kg", must_be_whole_number=0),
            ],
            "Item Price": [_price("IP-1", "ITEM-1")],
            "Currency Exchange": [
                _row(from_currency="USD", to_currency="INR", exchange_rate=83.5, date="2026-01-29"),
                _row(from_currency="USD", to_currency="INR", exchange_rate=80.0, date="2025-01-01"),
            ],
            "Purchase Taxes and Charges Template": [
                _row(name=IN_STATE, disabled=0),
                _row(name=OUT_STATE, disabled=0),
                _row(name=RCM_IN_STATE, disabled=0),
            ],
            "Purchase Order": [],
        },
        "children": {
            ("Item Supplier", "Item"): [
                _row(parent=item, supplier=sup)
                for item in ("ITEM-1", "ITEM-2", "ITEM-OFF", "ITEM-NOBUY", "ITEM-OLD")
                for sup in linked
            ],
            ("UOM Conversion Detail", "Item"): [
                _row(parent="ITEM-1", uom="Pcs", conversion_factor=1),
                _row(parent="ITEM-1", uom="Box", conversion_factor=12),
            ],
            ("Purchase Taxes and Charges", "Purchase Taxes and Charges Template"): [
                _tax_row(IN_STATE, "Input Tax CGST - DPL", 9, idx=1, description="CGST"),
                _tax_row(IN_STATE, "Input Tax SGST - DPL", 9, idx=2, description="SGST"),
                _tax_row(OUT_STATE, "Input Tax IGST - DPL", 18, description="IGST"),
                _tax_row(RCM_IN_STATE, "Input Tax CGST RCM - DPL", 9, idx=1, description="CGST"),
            ],
            ("Purchase Order Item", "Purchase Order"): [],
            ("Purchase Taxes and Charges", "Purchase Order"): [],
        },
    }


def base_payload(**over):
    payload = {
        "supplier": "SUP-A",
        "company": "TEST-CO",
        "transaction_date": TODAY,
        "schedule_date": IN_SEVEN_DAYS,
        "currency": "INR",
        "conversion_rate": 1,
        "buying_price_list": "PL-A",
        "custom_type": "B2B",
        "place_of_supply": "07-Delhi",
        "taxes_and_charges": IN_STATE,
        "items": [
            {
                "item_code": "ITEM-1", "qty": 10, "uom": "Pcs", "price_list_rate": 100,
                "warehouse": "WH-1", "schedule_date": IN_SEVEN_DAYS,
            }
        ],
        "taxes": [
            {
                "category": "Total", "add_deduct_tax": "Add", "charge_type": "On Net Total",
                "account_head": "Input Tax CGST - DPL", "description": "CGST", "rate": 9,
                "included_in_print_rate": 0,
            },
            {
                "category": "Total", "add_deduct_tax": "Add", "charge_type": "On Net Total",
                "account_head": "Input Tax SGST - DPL", "description": "SGST", "rate": 9,
                "included_in_print_rate": 0,
            },
        ],
    }
    payload.update(over)
    return payload


def igst_payload(**over):
    """A valid out-of-state payload for SUP-B."""
    payload = base_payload(
        supplier="SUP-B", buying_price_list="PL-B", taxes_and_charges=OUT_STATE,
        taxes=[
            {
                "category": "Total", "add_deduct_tax": "Add", "charge_type": "On Net Total",
                "account_head": "Input Tax IGST - DPL", "description": "IGST", "rate": 18,
                "included_in_print_rate": 0,
            }
        ],
    )
    payload.update(over)
    return payload


def add_created_po(world, payload, **over):
    """Add a Purchase Order, as it would be read back after creation, matching ``payload``."""
    name = "PO-NEW"
    net = sum(row["qty"] * row["price_list_rate"] for row in payload["items"])
    tax = sum(net * row["rate"] / 100 for row in payload["taxes"])
    header = _row(
        name=name, supplier=payload["supplier"], company=payload["company"], currency=payload["currency"],
        conversion_rate=payload["conversion_rate"], buying_price_list=payload["buying_price_list"],
        custom_type=payload["custom_type"], docstatus=0, workflow_state="Draft", status="Draft",
        price_list_currency=payload["currency"], total=net, total_taxes_and_charges=tax,
        grand_total=net + tax, rounded_total=round(net + tax), supplier_gstin="07AAAAA0000A1Z5",
        creation=str(now_datetime()), **over,
    )
    world["tables"]["Purchase Order"].append(header)
    for index, line in enumerate(payload["items"], start=1):
        world["children"][("Purchase Order Item", "Purchase Order")].append(
            _row(
                parent=name, idx=index, item_code=line["item_code"], qty=line["qty"], uom=line["uom"],
                warehouse=line.get("warehouse"), schedule_date=line["schedule_date"],
                rate=line["price_list_rate"], price_list_rate=line["price_list_rate"],
            )
        )
    for index, row in enumerate(payload["taxes"], start=1):
        world["children"][("Purchase Taxes and Charges", "Purchase Order")].append(
            _row(
                parent=name, idx=index, account_head=row["account_head"], rate=row["rate"],
                tax_amount=net * row["rate"] / 100, included_in_print_rate=0,
            )
        )
    return name


class FakeData:
    """In-memory stand-in for RuleDataAccess. Same methods, controlled data."""

    def __init__(self, world, *, fail_for=()):
        self.tables = world["tables"]
        self.children = world["children"]
        self.fail_for = set(fail_for)
        self.request_count = 0
        self.calls = []

    # -- helpers
    @staticmethod
    def _matches(row, filters):
        for field, operator, value in filters:
            actual = row[field]
            if operator == "=":
                ok = actual == value
            elif operator == "in":
                ok = actual in value
            elif operator == "like":
                ok = actual is not None and value.strip("%").lower() in str(actual).lower()
            elif operator == ">=":
                ok = actual is not None and str(actual) >= str(value)
            else:
                raise AssertionError(f"FakeData does not support operator {operator}")
            if not ok:
                return False
        return True

    def _select(self, rows, fields, filters, sort):
        self.request_count += 1
        selected = [row for row in rows if self._matches(row, filters)]
        for field, direction in reversed(list(sort)):
            selected.sort(key=lambda row: str(row[field]), reverse=direction == "desc")
        return [{field: copy.deepcopy(row[field]) for field in fields} for row in selected]

    def _guard(self, doctype):
        self.calls.append(doctype)
        if doctype in self.fail_for:
            raise RuleDataError(f"{doctype} is not readable", code="permission_denied")

    # -- RuleDataAccess interface
    def fetch_records(self, doctype, fields, filters=(), *, sort=(), page_length=None, require_complete=True):
        self._guard(doctype)
        rows = self._select(self.tables.get(doctype, []), fields, filters, sort)
        return rows[:page_length] if page_length else rows

    def fetch_by_names(self, doctype, names, fields, filters=()):
        return self.fetch_records(doctype, fields, [("name", "in", list(names)), *filters])

    def fetch_child_records(self, child_doctype, parent_doctype, parent_names, fields, filters=()):
        self._guard(child_doctype)
        rows = self.children.get((child_doctype, parent_doctype), [])
        return self._select(rows, fields, [("parent", "in", list(parent_names)), *filters], ())

    def count_records(self, doctype, filters=()):
        self._guard(doctype)
        self.request_count += 1
        return sum(1 for row in self.tables.get(doctype, []) if self._matches(row, filters))


# --------------------------------------------------------------------------- base
class PurchaseOrderCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        # Deterministic pilot configuration, inside the test transaction.
        seed_purchase_order_create_rule_set(force=True)

    @classmethod
    def tearDownClass(cls):
        frappe.db.rollback()

    def setUp(self):
        self.world = make_world()

    def run_stage(self, stage, payload=None, *, confirmations=None, options=None,
                  execution_result=None, fail_for=()):
        return BusinessRulesEngine.evaluate(
            "Purchase Order", "Create", base_payload() if payload is None else payload, stage=stage,
            confirmations=confirmations, execution_options=options, execution_result=execution_result,
            data_access=FakeData(self.world, fail_for=fail_for),
        )

    @staticmethod
    def result_of(report, code):
        return next(item for item in report["results"] if item["rule_code"] == code)

    def check(self, code, stage=c.PRE_EXECUTION, payload=None, **kwargs):
        report = self.run_stage(stage, payload, **kwargs)
        return self.result_of(report, code), report

    def assertStatus(self, code, expected, payload=None, stage=c.PRE_EXECUTION, contains=None, **kwargs):
        result, report = self.check(code, stage, payload, **kwargs)
        self.assertEqual(result["status"], expected, f"{code}: {result['message']}")
        if contains:
            self.assertIn(contains.lower(), result["message"].lower())
        return result

    @contextlib.contextmanager
    def params(self, rule_code, override):
        """Temporarily change an assignment's parameter override (inside the test transaction)."""
        row = frappe.get_all(
            "Business Rule Assignment",
            filters={"parent": "Purchase Order - Create", "business_rule": rule_code},
            fields=["name", "parameters_override"],
        )[0]
        current = json.loads(row["parameters_override"]) if row["parameters_override"] else {}
        frappe.db.set_value(
            "Business Rule Assignment", row["name"], "parameters_override", json.dumps({**current, **override})
        )
        try:
            yield
        finally:
            frappe.db.set_value(
                "Business Rule Assignment", row["name"], "parameters_override", row["parameters_override"]
            )

    def edit_item(self, **changes):
        payload = base_payload()
        payload["items"][0].update(changes)
        return payload


# --------------------------------------------------------------------- the rule set
class TestPilotConfiguration(PurchaseOrderCase):
    def test_rule_set_exists_with_every_pilot_rule_in_order(self):
        doc = frappe.get_doc("Business Rule Set", "Purchase Order - Create")
        self.assertEqual((doc.target_doctype, doc.operation, doc.enabled), ("Purchase Order", "Create", 1))
        self.assertEqual(len(doc.rules), 15)
        self.assertEqual([row.business_rule for row in doc.rules], [rule["rule_code"] for rule in RULES])
        self.assertEqual([row.sequence for row in doc.rules], sorted(row.sequence for row in doc.rules))

    def test_each_rule_is_atomic_with_its_own_evaluator(self):
        keys = [rule["evaluator_key"] for rule in RULES]
        self.assertEqual(len(keys), len(set(keys)), "one evaluator per rule")
        codes = [rule["rule_code"] for rule in RULES]
        self.assertEqual(len(codes), len(set(codes)))

    def test_rules_are_spread_over_the_three_stages(self):
        stages = {rule["rule_code"]: rule["execution_stage"] for rule in RULES}
        self.assertEqual(set(stages.values()), {c.PRE_EXECUTION, c.PRE_COMMIT, c.POST_EXECUTION})
        self.assertEqual(stages["PO_POST_CREATION_READBACK"], c.POST_EXECUTION)
        self.assertEqual(stages["PO_DUPLICATE_GUARD"], c.PRE_COMMIT)

    def test_site_specific_values_are_not_in_the_evaluators_or_engine(self):
        import pathlib

        import erp_intelligence.services.business_rules as package

        root = pathlib.Path(package.__file__).parent
        forbidden = ("DPL", "Deltacma", "Stores -", "Input GST", "07-Delhi", "Care N Class", "Edgistify")
        offenders = []
        for path in root.rglob("*.py"):
            if "pilots" in path.parts:
                continue  # the pilot definitions carry the site configuration on purpose
            text = path.read_text()
            offenders += [f"{path.name}: {token}" for token in forbidden if token in text]
        self.assertEqual(offenders, [])


# ------------------------------------------------------------------------- happy
class TestHappyPath(PurchaseOrderCase):
    def test_pre_execution_passes_with_an_approved_price(self):
        report = self.run_stage(c.PRE_EXECUTION)
        self.assertEqual(
            [(item["rule_code"], item["status"]) for item in report["results"]],
            [(rule["rule_code"], c.PASS) for rule in RULES if rule["execution_stage"] == c.PRE_EXECUTION],
        )
        self.assertEqual(report["decision"], c.DECISION_PROCEED)
        self.assertTrue(report["can_proceed"])
        self.assertTrue(report["summary"]["all_passed"])
        self.assertIs(report["execution_performed"], False)

    def test_pre_commit_passes(self):
        report = self.run_stage(c.PRE_COMMIT)
        self.assertEqual({item["status"] for item in report["results"]}, {c.PASS})
        self.assertEqual(report["returned_count"], 3)
        self.assertEqual(report["decision"], c.DECISION_PROCEED)

    def test_post_execution_passes_when_the_read_back_matches(self):
        name = add_created_po(self.world, base_payload())
        report = self.run_stage(c.POST_EXECUTION, execution_result={"name": name})
        self.assertEqual(report["decision"], c.DECISION_PROCEED, report["results"])
        self.assertEqual(report["results"][0]["details"]["verification"], "READ_BACK_COMPARED")

    def test_out_of_state_supplier_uses_the_inter_state_template(self):
        report = self.run_stage(c.PRE_COMMIT, igst_payload())
        self.assertEqual(report["decision"], c.DECISION_PROCEED, report["results"])


# --------------------------------------------------------------------- 1-4 supplier
class TestSupplierRules(PurchaseOrderCase):
    def test_missing_supplier_needs_input_and_dependent_rules_cannot_pass(self):
        payload = base_payload()
        del payload["supplier"]
        report = self.run_stage(c.PRE_EXECUTION, payload)
        self.assertEqual(self.result_of(report, "PO_SUPPLIER_RESOLVES_ONE")["status"], c.NEEDS_INPUT)
        for code in ("PO_SUPPLIER_ENABLED", "PO_SUPPLIER_NOT_ON_HOLD", "PO_SUPPLIER_PRICE_LIST_AVAILABLE",
                     "PO_ITEMS_LINKED_TO_SUPPLIER", "PO_ITEM_APPROVED_PRICE"):
            result = self.result_of(report, code)
            self.assertEqual(result["status"], c.NEEDS_INPUT, code)
            self.assertIn("supplier", result["required_inputs"])
        self.assertFalse(report["can_proceed"])

    def test_unknown_supplier_fails(self):
        self.assertStatus("PO_SUPPLIER_RESOLVES_ONE", c.FAIL, base_payload(supplier="Nonexistent"))

    def test_partial_name_is_ambiguous_and_never_picked(self):
        result = self.assertStatus("PO_SUPPLIER_RESOLVES_ONE", c.NEEDS_INPUT, base_payload(supplier="Alpha"))
        self.assertEqual({row["name"] for row in result["details"]["candidates"]}, {"SUP-A", "SUP-A2"})
        # Even a single near match is offered, not accepted.
        result = self.assertStatus("PO_SUPPLIER_RESOLVES_ONE", c.NEEDS_INPUT, base_payload(supplier="Beta"))
        self.assertEqual([row["name"] for row in result["details"]["candidates"]], ["SUP-B"])

    def test_disabled_supplier_fails(self):
        self.assertStatus("PO_SUPPLIER_ENABLED", c.FAIL, base_payload(supplier="SUP-OFF"))

    def test_hold_flags_fail_and_carry_the_uncertainty_note(self):
        for supplier in ("SUP-HOLD", "SUP-ONHOLD"):
            result = self.assertStatus("PO_SUPPLIER_NOT_ON_HOLD", c.FAIL, base_payload(supplier=supplier))
            self.assertEqual(result["details"]["evidence"]["tag"], "U")
        passed = self.assertStatus("PO_SUPPLIER_NOT_ON_HOLD", c.PASS)
        self.assertEqual(passed["details"]["evidence"]["tag"], "U")

    def test_price_list_missing_needs_input(self):
        payload = base_payload(supplier="SUP-NOPL")
        del payload["buying_price_list"]
        result = self.assertStatus("PO_SUPPLIER_PRICE_LIST_AVAILABLE", c.NEEDS_INPUT, payload)
        self.assertEqual(result["required_inputs"], ["buying_price_list"])

    def test_price_list_other_than_the_suppliers_needs_confirmation(self):
        payload = base_payload(buying_price_list="PL-B")
        result = self.assertStatus("PO_SUPPLIER_PRICE_LIST_AVAILABLE", c.NEEDS_CONFIRMATION, payload)
        self.assertEqual(result["details"]["confirmation_keys"], ["buying_price_list_override"])
        self.assertStatus(
            "PO_SUPPLIER_PRICE_LIST_AVAILABLE", c.WARNING, payload,
            confirmations={"buying_price_list_override": True},
        )

    def test_supplier_without_a_price_list_needs_confirmation_even_when_one_is_sent(self):
        self.assertStatus(
            "PO_SUPPLIER_PRICE_LIST_AVAILABLE", c.NEEDS_CONFIRMATION, base_payload(supplier="SUP-NOPL")
        )

    def test_disabled_or_selling_price_list_fails(self):
        self.assertStatus(
            "PO_SUPPLIER_PRICE_LIST_AVAILABLE", c.FAIL, base_payload(supplier="SUP-OFFPL", buying_price_list="PL-OFF")
        )
        self.assertStatus(
            "PO_SUPPLIER_PRICE_LIST_AVAILABLE", c.FAIL, base_payload(buying_price_list="PL-SELL"),
            confirmations={"buying_price_list_override": True},
        )


# ----------------------------------------------------------------------- 5-8 items
class TestItemRules(PurchaseOrderCase):
    def test_items_missing_needs_input(self):
        payload = base_payload()
        payload["items"] = []
        for code in ("PO_ITEMS_RESOLVE_ONE", "PO_ITEMS_QUANTITY_UOM", "PO_ITEM_APPROVED_PRICE"):
            result = self.assertStatus(code, c.NEEDS_INPUT, payload)
            self.assertEqual(result["required_inputs"], ["items"])

    def test_unknown_item_fails_and_near_match_needs_input(self):
        self.assertStatus("PO_ITEMS_RESOLVE_ONE", c.FAIL, self.edit_item(item_code="NOPE-123"))
        result = self.assertStatus("PO_ITEMS_RESOLVE_ONE", c.NEEDS_INPUT, self.edit_item(item_code="Widget"))
        self.assertEqual(result["details"]["issues"]["missing_inputs"][0]["candidates"][0]["name"], "ITEM-1")

    def test_unresolved_item_stops_dependent_item_rules_from_passing(self):
        payload = self.edit_item(item_code="NOPE-123")
        for code in ("PO_ITEMS_ENABLED_PURCHASABLE", "PO_ITEMS_LINKED_TO_SUPPLIER", "PO_ITEMS_QUANTITY_UOM",
                     "PO_ITEM_APPROVED_PRICE"):
            self.assertStatus(code, c.NEEDS_INPUT, payload)

    def test_disabled_expired_or_non_purchase_items_fail(self):
        for code in ("ITEM-OFF", "ITEM-NOBUY", "ITEM-OLD"):
            with self.subTest(item=code):
                self.assertStatus("PO_ITEMS_ENABLED_PURCHASABLE", c.FAIL, self.edit_item(item_code=code))

    def test_item_not_linked_to_the_supplier_fails(self):
        result = self.assertStatus("PO_ITEMS_LINKED_TO_SUPPLIER", c.FAIL, self.edit_item(item_code="ITEM-ORPHAN"))
        self.assertIn("Do not edit the Item", result["message"])
        self.assertEqual(result["details"]["evidence"]["tag"], "V")

    def test_quantity_must_be_positive_and_present(self):
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.FAIL, self.edit_item(qty=0))
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.FAIL, self.edit_item(qty=-3))
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.FAIL, self.edit_item(qty="abc"))
        payload = base_payload()
        del payload["items"][0]["qty"]
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.NEEDS_INPUT, payload)

    def test_fractional_quantity_fails_on_a_whole_number_uom(self):
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.FAIL, self.edit_item(qty=10.5), contains="whole number")

    def test_quantity_below_minimum_order_qty_fails(self):
        result = self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.FAIL, self.edit_item(qty=5), contains="minimum order")
        self.assertEqual(result["details"]["issues"]["failures"][0]["min_order_qty"], "10")

    def test_minimum_order_qty_applies_to_the_total_of_merged_lines(self):
        payload = base_payload()
        second = dict(payload["items"][0])
        payload["items"][0]["qty"] = 6
        second["qty"] = 6
        payload["items"].append(second)
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.PASS, payload)
        payload["items"][1]["qty"] = 2
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.FAIL, payload, contains="minimum order")

    def test_uom_must_be_defined_for_the_item(self):
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.FAIL, self.edit_item(uom="Kg"), contains="not defined")
        payload = base_payload()
        del payload["items"][0]["uom"]
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.NEEDS_INPUT, payload)

    def test_alternate_uom_needs_the_items_conversion_factor(self):
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.NEEDS_INPUT, self.edit_item(uom="Box", qty=1))
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.PASS, self.edit_item(uom="Box", qty=1, conversion_factor=12))
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.FAIL, self.edit_item(uom="Box", qty=1, conversion_factor=1))
        # 1 Box = 12 Pcs satisfies the minimum of 10; the check is on stock quantity.
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.PASS, self.edit_item(uom="Box", qty=1, conversion_factor=12))

    def test_stock_uom_with_a_wrong_conversion_factor_fails(self):
        self.assertStatus("PO_ITEMS_QUANTITY_UOM", c.FAIL, self.edit_item(conversion_factor=3))


# ------------------------------------------------------------------------- 9 price
class TestItemPriceRule(PurchaseOrderCase):
    CODE = "PO_ITEM_APPROVED_PRICE"

    def test_matching_approved_price_passes_and_is_resolved(self):
        result = self.assertStatus(self.CODE, c.PASS)
        self.assertEqual(result["details"]["resolved_prices"]["ITEM-1"]["item_price"], "IP-1")

    def test_user_price_that_differs_from_the_approved_price_warns_with_the_percentage(self):
        result = self.assertStatus(self.CODE, c.WARNING, self.edit_item(price_list_rate=110), contains="differs")
        self.assertEqual(Decimal(result["details"]["issues"]["warnings"][0]["difference_percent"]), 10)
        self.assertIn("10.00%", result["message"])

    def test_difference_within_the_configured_tolerance_passes(self):
        with self.params(self.CODE, {"price_difference_warning_percent": 15}):
            self.assertStatus(self.CODE, c.PASS, self.edit_item(price_list_rate=110))

    def test_approved_price_but_no_price_in_the_payload_needs_input(self):
        payload = base_payload()
        del payload["items"][0]["price_list_rate"]
        result = self.assertStatus(self.CODE, c.NEEDS_INPUT, payload)
        self.assertEqual(result["required_inputs"], ["items[0].price_list_rate"])
        self.assertIn("100", result["message"])

    def test_no_approved_price_and_no_user_price_needs_input_never_zero(self):
        payload = self.edit_item(item_code="ITEM-2")
        del payload["items"][0]["price_list_rate"]
        self.assertStatus(self.CODE, c.NEEDS_INPUT, payload, contains="no approved")

    def test_no_approved_price_with_a_user_price_warns(self):
        self.assertStatus(self.CODE, c.WARNING, self.edit_item(item_code="ITEM-2"), contains="only from the user")

    def test_zero_or_negative_price_fails(self):
        for price in (0, -5, "free"):
            with self.subTest(price=price):
                self.assertStatus(self.CODE, c.FAIL, self.edit_item(price_list_rate=price))

    def test_pending_disabled_expired_future_and_other_uom_prices_are_ignored(self):
        ineligible = [
            _price("IP-P", "ITEM-2", workflow_state="Pending"),
            _price("IP-D", "ITEM-2", custom_disable=1),
            _price("IP-E", "ITEM-2", valid_upto=YESTERDAY),
            _price("IP-F", "ITEM-2", valid_from=add_days(TODAY, 3)),
            _price("IP-U", "ITEM-2", uom="Box"),
            _price("IP-OTHER-SUPPLIER", "ITEM-2", supplier="SUP-B"),
            _price("IP-OTHER-LIST", "ITEM-2", price_list="PL-B"),
        ]
        self.world["tables"]["Item Price"].extend(ineligible)
        result = self.assertStatus(self.CODE, c.WARNING, self.edit_item(item_code="ITEM-2"), contains="no approved")
        self.assertEqual(result["details"]["resolved_prices"], {})

    def test_price_valid_today_with_an_open_end_is_eligible(self):
        self.world["tables"]["Item Price"].append(
            _price("IP-T", "ITEM-2", valid_from=TODAY, valid_upto=TODAY, price_list_rate=50)
        )
        self.assertStatus(self.CODE, c.PASS, self.edit_item(item_code="ITEM-2", price_list_rate=50))

    def test_newest_eligible_price_wins_and_equal_start_dates_are_ambiguous(self):
        self.world["tables"]["Item Price"].append(
            _price("IP-NEW", "ITEM-1", valid_from="2025-01-01", price_list_rate=120)
        )
        self.assertStatus(self.CODE, c.PASS, self.edit_item(price_list_rate=120))
        self.world["tables"]["Item Price"].append(
            _price("IP-TWIN", "ITEM-1", valid_from="2025-01-01", price_list_rate=130)
        )
        self.assertStatus(self.CODE, c.NEEDS_INPUT, self.edit_item(price_list_rate=120), contains="several approved")

    def test_price_currency_must_match_the_po_currency(self):
        self.world["tables"]["Item Price"][0]["currency"] = "USD"
        self.assertStatus(self.CODE, c.FAIL, contains="currency")

    def test_free_item_needs_confirmation(self):
        payload = self.edit_item(custom_free_item=1)
        del payload["items"][0]["price_list_rate"]
        result = self.assertStatus(self.CODE, c.NEEDS_CONFIRMATION, payload)
        self.assertEqual(result["details"]["confirmation_keys"], ["free_item:ITEM-1"])
        self.assertStatus(self.CODE, c.WARNING, payload, confirmations=["free_item:ITEM-1"])

    def test_price_needs_currency_and_price_list_in_the_payload(self):
        payload = base_payload()
        del payload["currency"]
        result = self.assertStatus(self.CODE, c.NEEDS_INPUT, payload)
        self.assertIn("currency", result["required_inputs"])

    def test_unconfigured_approval_parameters_are_an_error_not_a_pass(self):
        with self.params(self.CODE, {"approval_field": None}):
            result = self.assertStatus(self.CODE, c.ERROR)
        self.assertEqual(result["details"]["error_type"], c.ERROR_TYPE_CONFIGURATION)

    def test_evidence_note_is_preserved(self):
        evidence = self.assertStatus(self.CODE, c.PASS)["details"]["evidence"]
        self.assertEqual((evidence["tag"], evidence["source"]), ("V", "PURCHASE_ORDER_CREATION_GUIDE.md"))


# ------------------------------------------------------------------ 10-11 currency/date
class TestCurrencyRule(PurchaseOrderCase):
    CODE = "PO_CURRENCY_EXCHANGE_RATE"

    def usd(self, **over):
        return base_payload(
            supplier="SUP-USD", buying_price_list="PL-USD", currency="USD", conversion_rate=83.5, **over
        )

    def test_company_currency_with_rate_one_passes(self):
        self.assertStatus(self.CODE, c.PASS)

    def test_company_currency_with_another_rate_fails(self):
        self.assertStatus(self.CODE, c.FAIL, base_payload(conversion_rate=2), contains="must be 1")

    def test_currency_must_match_the_price_list_currency(self):
        self.assertStatus(self.CODE, c.FAIL, base_payload(currency="USD", conversion_rate=83), contains="price list currency")

    def test_foreign_currency_needs_a_confirmed_rate_and_shows_the_latest_record(self):
        result = self.assertStatus(self.CODE, c.NEEDS_CONFIRMATION, self.usd())
        self.assertEqual(result["details"]["confirmation_keys"], ["exchange_rate_confirmed"])
        self.assertEqual(result["details"]["latest_exchange_rate"], {"exchange_rate": 83.5, "date": "2026-01-29"})
        self.assertEqual(result["details"]["evidence"]["tag"], "V")
        self.assertStatus(self.CODE, c.WARNING, self.usd(), confirmations={"exchange_rate_confirmed": True})

    def test_foreign_currency_without_a_rate_needs_input(self):
        payload = self.usd()
        del payload["conversion_rate"]
        self.assertStatus(self.CODE, c.NEEDS_INPUT, payload)

    def test_missing_currency_needs_input_and_names_the_expected_one(self):
        payload = base_payload()
        del payload["currency"]
        result = self.assertStatus(self.CODE, c.NEEDS_INPUT, payload)
        self.assertEqual(result["details"]["expected_currency"], "INR")

    def test_supplier_default_currency_mismatch_needs_confirmation(self):
        self.world["tables"]["Supplier"][0]["default_currency"] = "EUR"
        result = self.assertStatus(self.CODE, c.NEEDS_CONFIRMATION)
        self.assertEqual(result["details"]["confirmation_keys"], ["supplier_currency_mismatch"])

    def test_non_positive_rate_fails(self):
        self.assertStatus(self.CODE, c.FAIL, base_payload(conversion_rate=0))


class TestRequiredByDateRule(PurchaseOrderCase):
    CODE = "PO_REQUIRED_BY_DATE_VALID"

    def test_valid_dates_pass(self):
        self.assertStatus(self.CODE, c.PASS)
        self.assertStatus(self.CODE, c.PASS, base_payload(schedule_date=TODAY, items=[
            dict(base_payload()["items"][0], schedule_date=TODAY)]))

    def test_missing_dates_need_input_and_are_never_invented(self):
        payload = base_payload()
        del payload["schedule_date"]
        result = self.assertStatus(self.CODE, c.NEEDS_INPUT, payload)
        self.assertEqual(result["required_inputs"], ["schedule_date"])
        payload = base_payload()
        del payload["items"][0]["schedule_date"]
        result = self.assertStatus(self.CODE, c.NEEDS_INPUT, payload)
        self.assertEqual(result["required_inputs"], ["items[0].schedule_date"])

    def test_date_before_the_transaction_date_fails(self):
        payload = base_payload(schedule_date=YESTERDAY)
        payload["items"][0]["schedule_date"] = YESTERDAY
        self.assertStatus(self.CODE, c.FAIL, payload, contains="before the transaction date")

    def test_invalid_date_format_fails(self):
        for value in ("26/11/2026", "2026-13-01", "next week", "2026-11-1"):
            with self.subTest(value=value):
                self.assertStatus(self.CODE, c.FAIL, base_payload(schedule_date=value))

    def test_back_dated_transaction_warns(self):
        self.assertStatus(self.CODE, c.WARNING, base_payload(transaction_date=YESTERDAY), contains="back-dated")

    def test_missing_transaction_date_is_assumed_and_flagged(self):
        payload = base_payload()
        del payload["transaction_date"]
        result = self.assertStatus(self.CODE, c.WARNING, payload, contains="assumed")
        self.assertTrue(result["details"]["assumptions"])

    def test_header_date_different_from_the_earliest_line_warns(self):
        self.assertStatus(self.CODE, c.WARNING, base_payload(schedule_date=add_days(TODAY, 9)), contains="resets")


# --------------------------------------------------------------------------- 12 tax
class TestTaxRule(PurchaseOrderCase):
    CODE = "PO_TAX_TREATMENT_RESOLVED"

    def test_same_state_template_and_rows_pass(self):
        result = self.assertStatus(self.CODE, c.PASS, stage=c.PRE_COMMIT)
        self.assertEqual(result["details"]["expected_template"], IN_STATE)

    def test_wrong_template_for_the_state_comparison_fails(self):
        payload = igst_payload(taxes_and_charges=IN_STATE)
        self.assertStatus(self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT, contains="does not match the state")

    def test_rows_that_do_not_match_the_template_fail(self):
        payload = base_payload()
        payload["taxes"][0]["rate"] = 10
        self.assertStatus(self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT, contains="do not match template")
        payload = base_payload()
        payload["taxes"].pop()
        self.assertStatus(self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT)

    def test_cgst_sgst_for_an_out_of_state_supplier_fails(self):
        payload = base_payload(supplier="SUP-B", buying_price_list="PL-B")
        self.assertStatus(self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT)

    def test_tax_inclusive_flag_must_be_sent_and_be_zero(self):
        payload = base_payload()
        del payload["taxes"][0]["included_in_print_rate"]
        self.assertStatus(self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT, contains="included_in_print_rate")
        payload = base_payload()
        payload["taxes"][0]["included_in_print_rate"] = 1
        self.assertStatus(self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT, contains="must be 0")

    def test_tax_row_needs_a_description(self):
        payload = base_payload()
        payload["taxes"][1]["description"] = ""
        self.assertStatus(self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT, contains="description")

    def test_missing_template_fails(self):
        payload = base_payload()
        del payload["taxes_and_charges"]
        payload["taxes"] = []
        self.assertStatus(self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT, contains="tax template is required")

    def test_state_27_needs_the_users_decision_before_any_treatment_is_accepted(self):
        payload = base_payload(supplier="SUP-MH")
        result = self.assertStatus(self.CODE, c.NEEDS_CONFIRMATION, payload, stage=c.PRE_COMMIT)
        self.assertEqual(result["details"]["confirmation_keys"], ["tax_treatment_confirmed"])
        self.assertStatus(
            self.CODE, c.WARNING, payload, stage=c.PRE_COMMIT, confirmations={"tax_treatment_confirmed": True}
        )
        no_tax = base_payload(supplier="SUP-MH")
        del no_tax["taxes_and_charges"]
        no_tax["taxes"] = []
        self.assertStatus(
            self.CODE, c.WARNING, no_tax, stage=c.PRE_COMMIT, confirmations={"tax_treatment_confirmed": True}
        )

    def test_confirmed_state_27_still_validates_the_rows_that_were_sent(self):
        payload = base_payload(supplier="SUP-MH")
        payload["taxes"][0]["rate"] = 5
        self.assertStatus(
            self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT, confirmations={"tax_treatment_confirmed": True}
        )

    def test_unregistered_supplier_takes_no_tax(self):
        clean = base_payload(supplier="SUP-UNREG")
        del clean["taxes_and_charges"]
        clean["taxes"] = []
        self.assertStatus(self.CODE, c.PASS, clean, stage=c.PRE_COMMIT)
        self.assertStatus(self.CODE, c.FAIL, base_payload(supplier="SUP-UNREG"), stage=c.PRE_COMMIT)

    def test_other_gst_category_needs_the_user(self):
        result = self.assertStatus(self.CODE, c.NEEDS_INPUT, base_payload(supplier="SUP-ODD"), stage=c.PRE_COMMIT)
        self.assertEqual(result["required_inputs"], ["tax_treatment"])

    def test_missing_place_of_supply_needs_input(self):
        payload = base_payload()
        del payload["place_of_supply"]
        self.assertStatus(self.CODE, c.NEEDS_INPUT, payload, stage=c.PRE_COMMIT)

    def test_registered_supplier_without_a_gstin_fails(self):
        self.world["tables"]["Supplier"][0]["gstin"] = None
        self.assertStatus(self.CODE, c.FAIL, stage=c.PRE_COMMIT, contains="GSTIN")

    def test_reverse_charge_template_needs_confirmation(self):
        payload = base_payload(taxes_and_charges=RCM_IN_STATE)
        payload["taxes"] = [
            dict(payload["taxes"][0], account_head="Input Tax CGST RCM - DPL"),
        ]
        result = self.assertStatus(self.CODE, c.NEEDS_CONFIRMATION, payload, stage=c.PRE_COMMIT)
        self.assertIn("reverse_charge_confirmed", result["details"]["confirmation_keys"])

    def test_unconfigured_templates_are_an_error_not_a_pass(self):
        with self.params(self.CODE, {"intra_state_template": None}):
            result = self.assertStatus(self.CODE, c.ERROR, stage=c.PRE_COMMIT)
        self.assertEqual(result["details"]["error_type"], c.ERROR_TYPE_CONFIGURATION)

    def test_disabled_or_unknown_template_fails(self):
        self.world["tables"]["Purchase Taxes and Charges Template"][0]["disabled"] = 1
        self.assertStatus(self.CODE, c.FAIL, stage=c.PRE_COMMIT, contains="disabled")


# ------------------------------------------------------------ 13 duplicate / 14 forbidden
class TestDuplicateGuard(PurchaseOrderCase):
    CODE = "PO_DUPLICATE_GUARD"

    def existing(self, **over):
        return _row(
            **{
                "name": "PO-OLD-1", "supplier": "SUP-A", "workflow_state": "Draft",
                "creation": str(add_to_date(now_datetime(), hours=-2)), "grand_total": 1180, **over,
            }
        )

    def test_no_recent_order_passes_and_states_its_visibility_limit(self):
        result = self.assertStatus(self.CODE, c.PASS, stage=c.PRE_COMMIT)
        self.assertIn("current user may read", result["details"]["evidence"]["note"])

    def test_recent_draft_or_pending_order_needs_confirmation_and_is_listed(self):
        for state in ("Draft", "Pending Approval"):
            with self.subTest(state=state):
                self.world["tables"]["Purchase Order"] = [self.existing(workflow_state=state)]
                result = self.assertStatus(self.CODE, c.NEEDS_CONFIRMATION, stage=c.PRE_COMMIT)
                self.assertEqual(result["details"]["confirmation_keys"], ["duplicate_po_review"])
                self.assertEqual(result["details"]["existing_orders"][0]["name"], "PO-OLD-1")

    def test_confirmation_lets_the_user_continue_with_a_warning(self):
        self.world["tables"]["Purchase Order"] = [self.existing()]
        self.assertStatus(
            self.CODE, c.WARNING, stage=c.PRE_COMMIT, confirmations={"duplicate_po_review": True}
        )

    def test_other_suppliers_old_orders_and_other_states_are_ignored(self):
        self.world["tables"]["Purchase Order"] = [
            self.existing(supplier="SUP-B"),
            self.existing(creation=str(add_to_date(now_datetime(), days=-3))),
            self.existing(workflow_state="Approved"),
        ]
        self.assertStatus(self.CODE, c.PASS, stage=c.PRE_COMMIT)

    def test_window_is_a_parameter(self):
        self.world["tables"]["Purchase Order"] = [
            self.existing(creation=str(add_to_date(now_datetime(), days=-3)))
        ]
        with self.params(self.CODE, {"window_hours": 96}):
            self.assertStatus(self.CODE, c.NEEDS_CONFIRMATION, stage=c.PRE_COMMIT)

    def test_missing_supplier_needs_input(self):
        payload = base_payload()
        del payload["supplier"]
        self.assertStatus(self.CODE, c.NEEDS_INPUT, payload, stage=c.PRE_COMMIT)


class TestForbiddenFields(PurchaseOrderCase):
    CODE = "PO_FORBIDDEN_PAYLOAD_FIELDS"

    def test_clean_payload_passes(self):
        self.assertStatus(self.CODE, c.PASS, stage=c.PRE_COMMIT, options={"submit": False})

    def test_forbidden_header_fields_fail(self):
        for field, value in (
            ("docstatus", 1), ("workflow_state", "Approved"), ("status", "Draft"), ("naming_series", "X"),
            ("grand_total", 5), ("total_taxes_and_charges", 1), ("base_net_total", 2), ("igst_amount", 3),
            ("custom_list_price", 1), ("custom_shipment_id", "A"), ("contact_person", "Bob"),
            ("set_warehouse", "W"),
        ):
            with self.subTest(field=field):
                result = self.assertStatus(self.CODE, c.FAIL, base_payload(**{field: value}), stage=c.PRE_COMMIT)
                self.assertIn(field, result["details"]["issues"]["failures"][0]["fields"])

    def test_forbidden_item_fields_fail(self):
        for field in ("rate", "item_name", "stock_uom", "stock_qty", "gst_hsn_code", "sales_order",
                      "delivered_by_supplier", "custom_ip_name", "net_amount"):
            with self.subTest(field=field):
                self.assertStatus(self.CODE, c.FAIL, self.edit_item(**{field: 1}), stage=c.PRE_COMMIT)

    def test_price_list_rate_and_uom_are_allowed(self):
        self.assertStatus(self.CODE, c.PASS, self.edit_item(price_list_rate=5, uom="Pcs"), stage=c.PRE_COMMIT)

    def test_execution_options_that_bypass_the_workflow_fail(self):
        for options in ({"submit": True}, {"validate_only": True}, {"submit": 1}):
            with self.subTest(options=options):
                self.assertStatus(self.CODE, c.FAIL, stage=c.PRE_COMMIT, options=options)

    def test_every_offender_is_reported_not_just_the_first(self):
        payload = base_payload(docstatus=1, status="X")
        payload["items"][0]["rate"] = 1
        result = self.assertStatus(self.CODE, c.FAIL, payload, stage=c.PRE_COMMIT, options={"submit": True})
        self.assertEqual(len(result["details"]["issues"]["failures"]), 3)


# ----------------------------------------------------------------------- 15 read-back
class TestReadBack(PurchaseOrderCase):
    CODE = "PO_POST_CREATION_READBACK"

    def run_readback(self, payload=None, name="PO-NEW", **kwargs):
        return self.check(
            self.CODE, c.POST_EXECUTION, payload or base_payload(),
            execution_result={"name": name}, **kwargs,
        )[0]

    def test_matching_read_back_passes(self):
        add_created_po(self.world, base_payload())
        result = self.run_readback()
        self.assertEqual(result["status"], c.PASS, result["message"])
        self.assertEqual(result["details"]["expected_totals"]["grand_total"], "1180")

    def test_discount_is_applied_to_the_expected_rate(self):
        payload = base_payload()
        payload["items"][0]["discount_percentage"] = 5
        name = add_created_po(self.world, payload)
        self.world["children"][("Purchase Order Item", "Purchase Order")][0]["rate"] = 95
        self.world["tables"]["Purchase Order"][0].update(total=950, total_taxes_and_charges=171, grand_total=1121, rounded_total=1121)
        result = self.run_readback(payload, name)
        self.assertEqual(result["status"], c.PASS, result["message"])

    def test_no_document_name_needs_input(self):
        result = self.check(self.CODE, c.POST_EXECUTION, execution_result={})[0]
        self.assertEqual(result["status"], c.NEEDS_INPUT)
        self.assertEqual(result["required_inputs"], ["execution_result.name"])

    def test_unreadable_document_is_unverified_never_correct(self):
        result = self.run_readback(name="PO-HIDDEN")
        self.assertEqual(result["status"], c.NEEDS_CONFIRMATION)
        self.assertEqual(result["details"]["verification"], "NOT_VERIFIED")
        self.assertIn("do not create it again", result["message"].lower())
        acknowledged = self.run_readback(name="PO-HIDDEN", confirmations=["readback_unverified_acknowledged"])
        self.assertEqual(acknowledged["status"], c.WARNING)
        self.assertEqual(acknowledged["details"]["verification"], "NOT_VERIFIED")

    def test_not_a_draft_fails(self):
        add_created_po(self.world, base_payload())
        self.world["tables"]["Purchase Order"][0].update(docstatus=1, workflow_state="Approved", status="To Bill")
        result = self.run_readback()
        self.assertEqual(result["status"], c.FAIL)
        self.assertEqual(len(result["details"]["issues"]["failures"]), 3)

    def test_header_differences_fail(self):
        for field, value in (("currency", "USD"), ("supplier", "SUP-B"), ("buying_price_list", "PL-B"),
                             ("conversion_rate", 2), ("custom_type", "B2C")):
            with self.subTest(field=field):
                world = make_world()
                self.world = world
                add_created_po(world, base_payload())
                world["tables"]["Purchase Order"][0][field] = value
                self.assertEqual(self.run_readback()["status"], c.FAIL)

    def test_line_differences_fail(self):
        for field, value in (("qty", 11), ("item_code", "ITEM-2"), ("uom", "Box"), ("warehouse", "OTHER"),
                             ("schedule_date", add_days(TODAY, 1)), ("rate", 99)):
            with self.subTest(field=field):
                self.world = make_world()
                add_created_po(self.world, base_payload())
                self.world["children"][("Purchase Order Item", "Purchase Order")][0][field] = value
                self.assertEqual(self.run_readback()["status"], c.FAIL)

    def test_zero_rate_fails(self):
        add_created_po(self.world, base_payload())
        self.world["children"][("Purchase Order Item", "Purchase Order")][0]["rate"] = 0
        self.assertEqual(self.run_readback()["status"], c.FAIL)

    def test_missing_lines_or_tax_rows_fail(self):
        add_created_po(self.world, base_payload())
        self.world["children"][("Purchase Order Item", "Purchase Order")].clear()
        self.assertEqual(self.run_readback()["status"], c.FAIL)
        self.world = make_world()
        add_created_po(self.world, base_payload())
        self.world["children"][("Purchase Taxes and Charges", "Purchase Order")].pop()
        self.assertEqual(self.run_readback()["status"], c.FAIL)

    def test_tax_inclusive_row_fails(self):
        add_created_po(self.world, base_payload())
        self.world["children"][("Purchase Taxes and Charges", "Purchase Order")][0]["included_in_print_rate"] = 1
        self.assertEqual(self.run_readback()["status"], c.FAIL)

    def test_total_differences_fail(self):
        for field, value in (("total", 900), ("total_taxes_and_charges", 1), ("grand_total", 1000),
                             ("rounded_total", 1185)):
            with self.subTest(field=field):
                self.world = make_world()
                add_created_po(self.world, base_payload())
                self.world["tables"]["Purchase Order"][0][field] = value
                result = self.run_readback()
                self.assertEqual(result["status"], c.FAIL, field)

    def test_rounded_total_is_not_checked_when_the_site_does_not_round(self):
        add_created_po(self.world, base_payload())
        self.world["tables"]["Purchase Order"][0]["rounded_total"] = 0
        self.assertEqual(self.run_readback()["status"], c.PASS)

    def test_unverifiable_tax_type_warns_instead_of_claiming_the_totals_are_right(self):
        payload = base_payload()
        payload["taxes"][0]["charge_type"] = "On Previous Row Total"
        add_created_po(self.world, payload)
        result = self.run_readback(payload)
        self.assertEqual(result["status"], c.WARNING)
        self.assertIn("unverified", result["message"])

    def test_empty_supplier_gstin_on_a_registered_supplier_warns(self):
        add_created_po(self.world, base_payload())
        self.world["tables"]["Purchase Order"][0]["supplier_gstin"] = None
        result = self.run_readback()
        self.assertEqual(result["status"], c.WARNING)
        self.assertIn("supplier_gstin", result["message"])

    def test_uncertainty_notes_are_preserved(self):
        add_created_po(self.world, base_payload())
        self.assertEqual(self.run_readback()["details"]["evidence"]["tag"], "V")


# ------------------------------------------------------------------- engine-level
class TestPilotThroughTheEngine(PurchaseOrderCase):
    def test_empty_payload_is_never_a_pass(self):
        for stage in (c.PRE_EXECUTION, c.PRE_COMMIT):
            report = self.run_stage(stage, {})
            self.assertFalse(report["can_proceed"], stage)
            self.assertEqual(report["decision"], c.DECISION_NEEDS_INPUT, stage)
            self.assertFalse(report["summary"]["all_passed"])
        pre = self.run_stage(c.PRE_EXECUTION, {})
        self.assertEqual(self.result_of(pre, "PO_SUPPLIER_RESOLVES_ONE")["status"], c.NEEDS_INPUT)

    def test_data_errors_become_error_results_and_stop_the_operation(self):
        report = self.run_stage(c.PRE_EXECUTION, fail_for=["Supplier"])
        errors = [item for item in report["results"] if item["status"] == c.ERROR]
        self.assertTrue(errors)
        self.assertTrue(all(item["details"]["error_type"] == c.ERROR_TYPE_DATA_ACCESS for item in errors))
        self.assertEqual(report["decision"], c.DECISION_ERROR)
        self.assertFalse(report["can_proceed"])

    def test_a_failing_rule_blocks_even_when_every_other_rule_passes(self):
        report = self.run_stage(c.PRE_EXECUTION, base_payload(supplier="SUP-OFF"))
        self.assertEqual(report["decision"], c.DECISION_BLOCKED)
        self.assertFalse(report["can_proceed"])
        self.assertIn("PO_SUPPLIER_ENABLED", report["blocking_failures"])
        passing = [item["rule_code"] for item in report["results"] if item["status"] == c.PASS]
        self.assertIn("PO_ITEMS_RESOLVE_ONE", passing, "other rules still ran and passed")

    def test_foreign_currency_order_waits_for_confirmation_then_proceeds_with_warnings(self):
        payload = base_payload(supplier="SUP-USD", buying_price_list="PL-USD", currency="USD", conversion_rate=83.5)
        self.world["tables"]["Item Price"].append(
            _price("IP-USD", "ITEM-1", supplier="SUP-USD", price_list="PL-USD", currency="USD")
        )
        pending = self.run_stage(c.PRE_EXECUTION, payload)
        self.assertEqual(pending["decision"], c.DECISION_NEEDS_CONFIRMATION, pending["results"])
        self.assertFalse(pending["can_proceed"])
        self.assertEqual(
            [item["confirmation_keys"] for item in pending["pending_confirmations"]],
            [["exchange_rate_confirmed"]],
        )
        done = self.run_stage(c.PRE_EXECUTION, payload, confirmations=["exchange_rate_confirmed"])
        self.assertEqual(done["decision"], c.DECISION_PROCEED_WITH_WARNINGS)
        self.assertTrue(done["can_proceed"])
        self.assertEqual(done["warnings"], ["PO_CURRENCY_EXCHANGE_RATE"])

    def test_report_is_json_serializable_and_never_executes(self):
        for stage in c.EXECUTION_STAGES:
            report = self.run_stage(stage, execution_result={"name": "PO-X"})
            json.dumps(report)
            self.assertIs(report["execution_performed"], False)

    def test_the_payload_is_not_modified(self):
        payload = base_payload()
        snapshot = copy.deepcopy(payload)
        self.run_stage(c.PRE_EXECUTION, payload)
        self.run_stage(c.PRE_COMMIT, payload)
        self.assertEqual(payload, snapshot)
