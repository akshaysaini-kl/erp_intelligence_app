"""Tests for child-table queries through intelligence_request (explicit parent context).

These tests are read-only. They need an initialized Frappe site that contains Sales
Orders, and they discover their users and documents from that data instead of
creating any:

* tools are invoked through ``execute()`` as real, non-admin session users;
  ``_safe_execute`` is never used because it writes audit rows;
* ``frappe.log_error`` is replaced for every test, and asserted unused, so no
  Error Log row can be created;
* configuration overrides are in-memory only (``mock.patch.dict``).

Run with:  bench --site <site> run-tests --module erp_intelligence.tests.test_child_table_queries
"""

import ast
import json
import pathlib
import unittest
from contextlib import contextmanager
from types import MappingProxyType
from unittest import mock

import frappe
from frappe.model import get_permitted_fields

import erp_intelligence
from erp_intelligence.services.aggregate_service import AggregateService
from erp_intelligence.services.config_service import ConfigService
from erp_intelligence.services.context_service import ContextService
from erp_intelligence.services.field_policy import FieldPolicy
from erp_intelligence.services.intelligence_service import IntelligenceService
from erp_intelligence.services.metadata_service import MetadataService
from erp_intelligence.services.query_planner import QueryPlanner
from erp_intelligence.services.query_service import QueryService
from erp_intelligence.services.query_validator import QueryValidator
from erp_intelligence.tools.aggregate_records import AggregateRecords
from erp_intelligence.tools.describe_doctype import DescribeDocType
from erp_intelligence.tools.intelligence_request import IntelligenceRequest
from erp_intelligence.tools.query_records import QueryRecords
from erp_intelligence.utils.constants import QUERYABLE_SCALAR_FIELD_TYPES, QUERY_LIMIT_NAMES

PARENT = "Sales Order"
CHILD = "Sales Order Item"
ITEM_FIELDS = ["parent", "item_code", "item_name", "qty", "delivered_qty"]
FX: dict = {}


# --------------------------------------------------------------------------- fixtures
def setUpModule():
    if not getattr(frappe.local, "site", None):
        raise unittest.SkipTest("requires an initialized Frappe site")

    users = sorted(
        frappe.db.get_all(
            "User",
            filters={
                "enabled": 1,
                "user_type": "System User",
                "name": ("not in", ["Administrator", "Guest"]),
            },
            pluck="name",
        )
    )
    sees = zero = none = None
    for user in users:
        if "System Manager" in frappe.get_roles(user):
            continue
        frappe.set_user(user)
        if frappe.has_permission(PARENT, "read", user=user):
            count = frappe.get_list(PARENT, fields=["count(name) as c"])[0]["c"]
            if count > 0 and sees is None:
                sees = user
            elif count == 0 and zero is None:
                zero = user
        elif none is None and not frappe.has_permission(PARENT, "select", user=user):
            none = user

    parents, paged_parent = [], None
    if sees:
        frappe.set_user(sees)
        candidates = [
            row["name"]
            for row in frappe.get_list(PARENT, fields=["name"], order_by="creation desc", limit_page_length=40)
        ]
        if candidates:
            rows = frappe.get_list(
                CHILD,
                parent_doctype=PARENT,
                fields=["parent"],
                filters=[[CHILD, "parent", "in", candidates]],
                limit_page_length=1000,
            )
            counts = {}
            for row in rows:
                counts[row["parent"]] = counts.get(row["parent"], 0) + 1
            parents = [name for name in candidates if counts.get(name)][:3]
            paged_parent = next((name for name in parents if counts[name] >= 2), None)

    frappe.set_user("Administrator")
    FX.update(sees=sees, zero=zero, none=none, parents=parents, paged_parent=paged_parent)


def tearDownModule():
    frappe.set_user("Administrator")
    frappe.db.rollback()


# --------------------------------------------------------------------------- base class
class LiveCase(unittest.TestCase):
    """Common helpers: user switching, spies, and a hard guard on Error Log writes."""

    def setUp(self):
        patcher = mock.patch.object(frappe, "log_error")
        self.log_error = patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []  # every frappe.get_list call: (doctype, kwargs)
        self.executor_calls = {"query_records": 0, "aggregate_records": 0}
        self._install_spies()

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.local.message_log = []
        self.assertFalse(self.log_error.called, "frappe.log_error must never be called")

    def _install_spies(self):
        real_get_list = frappe.get_list

        def get_list(doctype, *args, **kwargs):
            self.calls.append((doctype, dict(kwargs)))
            return real_get_list(doctype, *args, **kwargs)

        p = mock.patch.object(frappe, "get_list", get_list)
        p.start()
        self.addCleanup(p.stop)

        for cls, name, key in (
            (QueryService, "query_records", "query_records"),
            (AggregateService, "aggregate_records", "aggregate_records"),
        ):
            original = cls.__dict__[name].__func__

            def wrapped(klass, *a, _o=original, _k=key, **kw):
                self.executor_calls[_k] += 1
                return _o(klass, *a, **kw)

            q = mock.patch.object(cls, name, classmethod(wrapped))
            q.start()
            self.addCleanup(q.stop)

    # ---- helpers
    def need(self, *keys):
        for key in keys:
            if not FX.get(key):
                self.skipTest(f"fixture unavailable on this site: {key}")

    def as_user(self, user):
        frappe.set_user(user)

    def call(self, request, user=None):
        if user:
            self.as_user(user)
        return IntelligenceRequest().execute(request)

    def child_request(self, names=None, **extra):
        request = {
            "doctype": CHILD,
            "parent_context": {"parent_doctype": PARENT, "parent_names": list(names or FX["parents"][:2])},
            "fields": list(ITEM_FIELDS),
        }
        request.update(extra)
        return request

    def native_children(self, names, fields, user, limit=500, order_by="`parent` asc, `idx` asc", extra_filters=None):
        """Independent oracle: native get_list under the same session, bypassing our code."""
        frappe.set_user(user)
        return frappe.get_list(
            CHILD,
            parent_doctype=PARENT,
            fields=fields,
            filters=[
                [CHILD, "parent", "in", names],
                [CHILD, "parenttype", "=", PARENT],
                [CHILD, "parentfield", "=", "items"],
                *(extra_filters or []),
            ],
            order_by=order_by,
            limit_page_length=limit,
        )

    def child_calls(self):
        return [c for c in self.calls if c[0] == CHILD]

    def assertNothingExecuted(self):
        self.assertEqual(self.executor_calls, {"query_records": 0, "aggregate_records": 0})
        self.assertEqual(self.child_calls(), [], "no child-table query may be issued")

    def assertRejected(self, request, exc=frappe.ValidationError, contains=None, user=None):
        with self.assertRaises(exc) as ctx:
            self.call(request, user or FX["sees"])
        if contains:
            self.assertIn(contains, str(ctx.exception))
        self.assertNothingExecuted()
        return ctx.exception


# --------------------------------------------------------------------------- 1. core behaviour
class TestChildQueryCore(LiveCase):
    def test_valid_sales_order_to_sales_order_item_query(self):
        self.need("sees", "parents")
        names = FX["parents"][:2]
        result = self.call(self.child_request(names, page_length=100), FX["sees"])

        self.assertEqual(result["result_type"], "intelligence_execution")
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["plan_type"], "records")
        self.assertEqual(result["doctype"], CHILD)
        records = result["execution_result"]["records"]
        self.assertTrue(records)
        self.assertEqual(result["returned_count"], len(records))
        for record in records:
            self.assertEqual(sorted(record), sorted(ITEM_FIELDS))
            self.assertIn(record["parent"], names)

        expected = self.native_children(names, ITEM_FIELDS, FX["sees"], limit=101)
        self.assertEqual(records, expected[:100], "must equal the native permission-aware query")

    def test_multiple_parent_documents(self):
        self.need("sees", "parents")
        names = FX["parents"]
        self.assertGreaterEqual(len(names), 2)
        result = self.call(self.child_request(names, page_length=100), FX["sees"])
        records = result["execution_result"]["records"]
        self.assertEqual({r["parent"] for r in records}, set(names), "rows from every requested parent")
        pc = result["execution_result"]["parent_context"]
        self.assertEqual((pc["requested_count"], pc["accessible_count"]), (len(names), len(names)))
        for name in names:
            native = self.native_children([name], ["name"], FX["sees"])
            self.assertEqual(sum(1 for r in records if r["parent"] == name), len(native))

    def test_response_uses_the_standard_structure(self):
        self.need("sees", "parents")
        result = self.call(self.child_request(), FX["sees"])
        json.dumps(result, allow_nan=False)  # JSON-safe
        self.assertEqual(
            sorted(result),
            sorted(
                [
                    "doctype", "execution_result", "has_more", "plan_type", "planning_metadata",
                    "planning_status", "resolutions", "result_type", "returned_count", "status",
                ]
            ),
        )
        self.assertEqual(result["planning_status"], "ready")
        self.assertEqual(result["planning_metadata"], {})
        ex = result["execution_result"]
        self.assertEqual(
            sorted(ex),
            sorted(
                [
                    "doctype", "fields", "has_more", "offset", "page_length", "parent_context",
                    "records", "result_type", "returned_count",
                ]
            ),
        )
        self.assertEqual(ex["result_type"], "records")
        self.assertEqual(ex["parent_context"]["parent_doctype"], PARENT)
        self.assertEqual(ex["parent_context"]["parent_fieldname"], "items")
        self.assertNotIn("parent_names", ex["parent_context"], "names are never echoed back")
        locations = [r["location"] for r in result["resolutions"]]
        self.assertIn("/parent_context/parent_doctype", locations)
        self.assertIn("/doctype", locations)

    def test_default_order_is_parent_then_row_number(self):
        self.need("sees", "parents")
        result = self.call(self.child_request(FX["parents"], fields=["parent", "idx", "item_code"], page_length=100), FX["sees"])
        keys = [(r["parent"], r["idx"]) for r in result["execution_result"]["records"]]
        self.assertEqual(keys, sorted(keys))
        order_by = self.child_calls()[-1][1]["order_by"]
        self.assertTrue(order_by.endswith("`parent` asc, `idx` asc"), order_by)

    def test_names_and_labels_resolve(self):
        self.need("sees", "parents")
        request = self.child_request(fields=["Item Code", "Parent"], page_length=5)
        request["doctype"] = "sales order item"  # case-insensitive child name
        result = self.call(request, FX["sees"])
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["execution_result"]["fields"], ["item_code", "parent"])

    def test_filters_sort_and_standard_child_columns(self):
        self.need("sees", "parents")
        names = FX["parents"]
        result = self.call(
            self.child_request(
                names,
                fields=["parent", "idx", "qty"],
                filters=[{"field": "qty", "operator": ">", "value": 0}],
                sort=[{"field": "qty", "direction": "desc"}],
                page_length=100,
            ),
            FX["sees"],
        )
        qty = [r["qty"] for r in result["execution_result"]["records"]]
        self.assertTrue(qty and all(q > 0 for q in qty))
        self.assertEqual(qty, sorted(qty, reverse=True))
        order_by = self.child_calls()[-1][1]["order_by"]
        self.assertTrue(order_by.startswith("`qty` desc") and order_by.endswith("`parent` asc, `idx` asc"), order_by)

    def test_user_filters_cannot_widen_the_parent_restriction(self):
        self.need("sees", "parents")
        outside = [n for n in FX["parents"][2:]] or [None]
        only = FX["parents"][:1]
        # A filter naming a different parent adds to, never replaces, the parent restriction.
        result = self.call(
            self.child_request(
                only,
                filters=[{"field": "parent", "operator": "in", "value": [*FX["parents"]]}],
                page_length=100,
            ),
            FX["sees"],
        )
        self.assertEqual({r["parent"] for r in result["execution_result"]["records"]}, set(only))
        del outside


# --------------------------------------------------------------------------- 2. rejection
class TestChildQueryRejections(LiveCase):
    def test_unknown_parent_doctype_is_unresolved_without_execution(self):
        self.need("sees", "parents")
        request = self.child_request()
        request["parent_context"]["parent_doctype"] = "No Such DocType"
        result = self.call(request, FX["sees"])
        self.assertEqual(result["result_type"], "query_plan")
        self.assertEqual(result["status"], "unresolved")
        self.assertIsNone(result["plan"])
        self.assertEqual([i["location"] for i in result["unresolved_references"]], ["/parent_context/parent_doctype"])
        self.assertNothingExecuted()

    def test_child_doctype_must_belong_to_the_parent(self):
        self.need("sees", "parents")
        for doctype in ("Sales Invoice Item", "Delivery Note Item", "Customer", PARENT, "No Such DocType"):
            with self.subTest(doctype=doctype):
                request = self.child_request()
                request["doctype"] = doctype
                self.assertRejected(request, contains="is not a child table of")

    def test_parent_that_does_not_own_the_child_table(self):
        self.need("sees", "parents")
        request = self.child_request()
        request["parent_context"]["parent_doctype"] = "Customer"
        try:
            result = self.call(request, FX["sees"])
        except frappe.ValidationError as exc:
            self.assertIn("is not a child table of", str(exc))
        else:  # a user who cannot read Customer gets an unresolved plan instead
            self.assertEqual(result["status"], "unresolved")
        self.assertNothingExecuted()

    def test_a_child_doctype_is_never_resolvable_without_parent_context(self):
        self.need("sees")
        result = self.call({"doctype": CHILD, "fields": ["name"]}, FX["sees"])
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual([i["location"] for i in result["unresolved_references"]], ["/doctype"])
        self.assertNothingExecuted()

    def test_empty_parent_names(self):
        self.need("sees", "parents")
        request = self.child_request()
        request["parent_context"]["parent_names"] = []
        self.assertRejected(request, contains="parent_names must be a non-empty list")

    def test_malformed_parent_context(self):
        self.need("sees", "parents")
        good = FX["parents"][0]
        cases = {
            "parent_context is a string": "Sales Order",
            "parent_context is a list": [PARENT],
            "parent_context is null": None,
            "parent_context empty object": {},
            "missing parent_names": {"parent_doctype": PARENT},
            "missing parent_doctype": {"parent_names": [good]},
            "extra key": {"parent_doctype": PARENT, "parent_names": [good], "parentfield": "items"},
            "parent_names not a list": {"parent_doctype": PARENT, "parent_names": good},
            "parent_names null": {"parent_doctype": PARENT, "parent_names": None},
            "name is not a string": {"parent_doctype": PARENT, "parent_names": [123]},
            "name is null": {"parent_doctype": PARENT, "parent_names": [None]},
            "name is empty": {"parent_doctype": PARENT, "parent_names": [""]},
            "name has padding": {"parent_doctype": PARENT, "parent_names": [f" {good}"]},
            "name has control character": {"parent_doctype": PARENT, "parent_names": [f"{good}\n"]},
            "name too long": {"parent_doctype": PARENT, "parent_names": ["X" * 141]},
            "duplicate names": {"parent_doctype": PARENT, "parent_names": [good, good]},
            "parent_doctype not a string": {"parent_doctype": 5, "parent_names": [good]},
            "parent_doctype empty": {"parent_doctype": "", "parent_names": [good]},
            "parent_doctype dotted": {"parent_doctype": "Sales Order.items", "parent_names": [good]},
            "parent_doctype backtick": {"parent_doctype": "Sales`Order", "parent_names": [good]},
        }
        for label, context in cases.items():
            with self.subTest(label):
                request = self.child_request()
                request["parent_context"] = context
                self.assertRejected(request)

    def test_parent_context_is_not_accepted_for_aggregations(self):
        self.need("sees", "parents")
        request = {
            "doctype": CHILD,
            "parent_context": {"parent_doctype": PARENT, "parent_names": FX["parents"][:1]},
            "aggregations": [{"operation": "count", "field": "name"}],
        }
        self.assertRejected(request, contains="only supported for record requests")

    def test_parent_context_on_a_normal_doctype_is_rejected(self):
        self.need("sees", "parents")
        request = {
            "doctype": PARENT,
            "parent_context": {"parent_doctype": PARENT, "parent_names": FX["parents"][:1]},
            "fields": ["name"],
        }
        self.assertRejected(request, contains="is not a child table of")

    def test_dotted_parent_document_names_are_allowed(self):
        self.need("sees", "parents")
        result = self.call(self.child_request([f"{FX['parents'][0]}.1"]), FX["sees"])
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["returned_count"], 0)

    def test_validator_rejects_a_forged_relationship(self):
        """Even if the planner were bypassed, QueryValidator re-checks live metadata."""
        self.need("sees", "parents")
        frappe.set_user(FX["sees"])
        base = {
            "doctype": CHILD,
            "fields": ["parent"],
            "parent_doctype": PARENT,
            "parent_names": FX["parents"][:1],
        }
        with self.assertRaises(frappe.ValidationError):
            QueryValidator.validate_query({**base, "parent_fieldname": "taxes"})  # wrong table
        with self.assertRaises(frappe.ValidationError):
            QueryValidator.validate_query({**base, "parent_doctype": "Customer", "parent_fieldname": "items"})
        with self.assertRaises(frappe.ValidationError):
            QueryValidator.validate_query({**base, "parent_names": None, "parent_fieldname": "items"})
        with self.assertRaises(frappe.ValidationError):  # parent_names only valid for child tables
            QueryValidator.validate_query({"doctype": PARENT, "fields": ["name"], "parent_names": ["X"]})
        self.assertNothingExecuted()

    def test_direct_query_service_still_refuses_a_child_without_context(self):
        self.need("sees")
        frappe.set_user(FX["sees"])
        with self.assertRaises(frappe.ValidationError):
            QueryService.query_records({"doctype": CHILD, "fields": ["name"]})
        self.assertEqual(self.child_calls(), [])


# --------------------------------------------------------------------------- 3. permissions
class TestChildQueryPermissions(LiveCase):
    def test_user_without_access_to_the_parent_gets_nothing(self):
        self.need("none", "parents")
        result = self.call(self.child_request(), FX["none"])
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual([i["location"] for i in result["unresolved_references"]], ["/parent_context/parent_doctype"])
        self.assertNotIn("execution_result", result)
        self.assertNothingExecuted()

    def test_row_level_restriction_hides_every_child_row(self):
        """A user the site's permission hook restricts to zero Sales Orders sees no child rows."""
        self.need("zero", "parents")
        result = self.call(self.child_request(FX["parents"]), FX["zero"])
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["returned_count"], 0)
        self.assertEqual(result["execution_result"]["records"], [])
        pc = result["execution_result"]["parent_context"]
        self.assertEqual((pc["requested_count"], pc["accessible_count"]), (len(FX["parents"]), 0))
        self.assertEqual(self.child_calls(), [], "no child query may run when no parent is readable")

    def test_only_readable_parents_contribute_rows(self):
        """Simulates a User Permission that leaves exactly one parent readable."""
        self.need("sees", "parents")
        names = FX["parents"]
        self.assertGreaterEqual(len(names), 2)
        keep = names[0]
        real_get_list = frappe.get_list

        def restricted(doctype, *args, **kwargs):
            if doctype == PARENT and kwargs.get("fields") == ["name"]:
                self.calls.append((doctype, dict(kwargs)))
                return [{"name": keep}]
            return real_get_list(doctype, *args, **kwargs)

        with mock.patch.object(frappe, "get_list", restricted):
            result = self.call(self.child_request(names, page_length=100), FX["sees"])

        records = result["execution_result"]["records"]
        self.assertTrue(records)
        self.assertEqual({r["parent"] for r in records}, {keep})
        pc = result["execution_result"]["parent_context"]
        self.assertEqual((pc["requested_count"], pc["accessible_count"]), (len(names), 1))
        flt = next(c[1]["filters"] for c in self.child_calls() if c[1].get("filters"))
        self.assertIn([CHILD, "parent", "in", [keep]], flt)

    def test_unknown_parent_names_are_excluded_and_not_revealed(self):
        self.need("sees", "parents")
        real, fake = FX["parents"][0], "SO-DOES-NOT-EXIST-0001"
        result = self.call(self.child_request([real, fake], page_length=100), FX["sees"])
        pc = result["execution_result"]["parent_context"]
        self.assertEqual((pc["requested_count"], pc["accessible_count"]), (2, 1))
        self.assertEqual({r["parent"] for r in result["execution_result"]["records"]}, {real})
        self.assertNotIn(fake, json.dumps(result))

        self.calls.clear()
        only_fake = self.call(self.child_request([fake]), FX["sees"])
        self.assertEqual(only_fake["returned_count"], 0)
        self.assertEqual(self.child_calls(), [], "no child query when nothing is accessible")

    def test_every_child_query_is_scoped_to_readable_parents_and_the_stated_table(self):
        self.need("sees", "parents")
        names = FX["parents"]
        self.call(self.child_request(names), FX["sees"])
        child_calls = self.child_calls()
        self.assertEqual(len(child_calls), 1)
        kwargs = child_calls[0][1]
        self.assertEqual(kwargs.get("parent_doctype"), PARENT)
        self.assertFalse(kwargs.get("ignore_permissions"))
        self.assertNotIn("user", kwargs)
        filters = kwargs["filters"]
        parent_filter = [f for f in filters if f[1] == "parent"]
        self.assertEqual(len(parent_filter), 1)
        self.assertEqual(parent_filter[0][2], "in")
        self.assertTrue(set(parent_filter[0][3]) <= set(names))
        self.assertIn([CHILD, "parenttype", "=", PARENT], filters)
        self.assertIn([CHILD, "parentfield", "=", "items"], filters)
        # the parent gate ran first, through get_list on the parent
        self.assertEqual(self.calls[[c[0] for c in self.calls].index(PARENT)][1]["fields"], ["name"])
        self.assertLess([c[0] for c in self.calls].index(PARENT), [c[0] for c in self.calls].index(CHILD))

    def test_session_user_cannot_be_overridden(self):
        self.need("sees", "parents")
        with self.assertRaises(frappe.PermissionError):
            self.call({**self.child_request(), "user": "Administrator"}, FX["sees"])
        self.assertNothingExecuted()
        result = self.call({**self.child_request(), "user": FX["sees"]}, FX["sees"])
        self.assertEqual(result["status"], "executed")

    def test_field_level_security_applies_to_child_fields(self):
        self.need("sees", "parents")
        meta = frappe.get_meta(CHILD)
        permitted = set(
            get_permitted_fields(CHILD, parenttype=PARENT, user=FX["sees"], permission_type="read", ignore_virtual=True)
        )
        denied = [
            f.fieldname
            for f in meta.fields
            if f.fieldtype in QUERYABLE_SCALAR_FIELD_TYPES and f.fieldname not in permitted
        ]
        if not denied:
            self.skipTest("this user can read every queryable child field")
        request = self.child_request(fields=["parent", denied[0]])
        result = None
        try:
            result = self.call(request, FX["sees"])
        except (frappe.ValidationError, frappe.PermissionError):
            pass
        if result is not None:  # surfaced as an unresolved reference, never as data
            self.assertEqual(result["status"], "unresolved")
            self.assertNotIn("execution_result", result)
        self.assertNothingExecuted()

    def test_child_fields_are_filtered_like_the_native_policy(self):
        self.need("sees")
        frappe.set_user(FX["sees"])
        metadata = MetadataService.get_doctype_metadata(CHILD)
        with_context = FieldPolicy.get_field_policy(
            metadata, parent_doctype=PARENT, parent_fieldname="items"
        )
        without = FieldPolicy.get_field_policy(metadata)
        self.assertIn("parent", with_context["queryable_fields"])
        self.assertEqual(without["readable_fields"], [], "a child without parent context is denied")
        wrong_table = FieldPolicy.get_field_policy(metadata, parent_doctype=PARENT, parent_fieldname="taxes")
        self.assertEqual(wrong_table["readable_fields"], [])


# --------------------------------------------------------------------------- 4. pagination and limits
class TestChildQueryPaginationAndLimits(LiveCase):
    def test_page_length_and_has_more(self):
        self.need("sees", "paged_parent")
        total = len(self.native_children([FX["paged_parent"]], ["name"], FX["sees"]))
        result = self.call(self.child_request([FX["paged_parent"]], page_length=1), FX["sees"])
        ex = result["execution_result"]
        self.assertEqual((result["returned_count"], ex["page_length"], ex["offset"]), (1, 1, 0))
        self.assertEqual(result["has_more"], total > 1)
        self.assertTrue(result["has_more"])
        self.assertEqual(self.child_calls()[-1][1]["limit_page_length"], 2, "page_length + 1 lookahead only")

    def test_paging_walks_every_row_exactly_once(self):
        self.need("sees", "paged_parent")
        parent = FX["paged_parent"]
        full = self.native_children([parent], ["parent", "idx", "item_code"], FX["sees"])
        seen, offset = [], 0
        for _ in range(len(full) + 2):
            result = self.call(
                self.child_request([parent], fields=["parent", "idx", "item_code"], page_length=1, offset=offset),
                FX["sees"],
            )
            seen.extend(result["execution_result"]["records"])
            if not result["has_more"]:
                break
            offset += 1
        self.assertEqual(seen, full)

    def test_offset_beyond_the_end_is_empty(self):
        self.need("sees", "paged_parent")
        result = self.call(self.child_request([FX["paged_parent"]], page_length=5, offset=100000), FX["sees"])
        self.assertEqual((result["returned_count"], result["has_more"]), (0, False))

    def test_page_length_and_offset_are_validated(self):
        self.need("sees", "parents")
        for label, extra in {
            "page_length zero": {"page_length": 0},
            "page_length negative": {"page_length": -1},
            "page_length too large": {"page_length": 100000},
            "page_length string": {"page_length": "3"},
            "page_length bool": {"page_length": True},
            "offset negative": {"offset": -1},
            "offset string": {"offset": "1"},
            "offset beyond maximum": {"offset": 10**9},
        }.items():
            with self.subTest(label):
                self.assertRejected(self.child_request(**extra))

    def test_configured_page_limit_applies(self):
        self.need("sees", "parents")
        with mock.patch.dict(frappe.local.conf, {"erp_intelligence_max_page_length": 2}):
            self.assertRejected(self.child_request(page_length=3), contains="between 1 and 2")
            result = self.call(self.child_request(FX["parents"]), FX["sees"])  # default page_length
        self.assertLessEqual(result["execution_result"]["page_length"], 2)
        self.assertLessEqual(len(result["execution_result"]["records"]), 2)

    def test_parent_names_limit(self):
        self.need("sees", "parents")
        names = FX["parents"]
        self.assertGreaterEqual(len(names), 3)
        with mock.patch.dict(frappe.local.conf, {"erp_intelligence_max_parent_names": 2}):
            self.assertRejected(self.child_request(names), contains="cannot contain more than 2")
            ok = self.call(self.child_request(names[:2]), FX["sees"])
        self.assertEqual(ok["status"], "executed")

    def test_parent_names_limit_configuration_is_validated(self):
        self.need("sees", "parents")
        for label, value in {"string": "5", "bool": True, "zero": 0, "above ceiling": 201, "null": None}.items():
            with self.subTest(label), mock.patch.dict(frappe.local.conf, {"erp_intelligence_max_parent_names": value}):
                self.assertRejected(self.child_request())

    def test_default_parent_names_limit_is_enforced(self):
        self.need("sees", "parents")
        too_many = [f"SO-LIMIT-{i}" for i in range(51)]
        self.assertRejected(self.child_request(too_many), contains="cannot contain more than 50")

    def test_snapshot_includes_the_new_limit_and_is_shared(self):
        snapshot = ConfigService.get_limit_snapshot()
        self.assertIsInstance(snapshot, MappingProxyType)
        self.assertEqual(set(snapshot), set(QUERY_LIMIT_NAMES))
        self.assertIn("parent_names", snapshot)
        self.assertEqual(snapshot["parent_names"], 50)


# --------------------------------------------------------------------------- 5. tool schema and registration
class TestToolSchemaAndRegistration(unittest.TestCase):
    def setUp(self):
        self.tool = IntelligenceRequest()
        self.schema = self.tool.inputSchema

    def test_parent_context_is_declared_strictly(self):
        prop = self.schema["properties"]["parent_context"]
        self.assertEqual(prop["type"], "object")
        self.assertIs(prop["additionalProperties"], False)
        self.assertEqual(sorted(prop["required"]), ["parent_doctype", "parent_names"])
        names = prop["properties"]["parent_names"]
        self.assertEqual((names["type"], names["minItems"], names["uniqueItems"]), ("array", 1, True))
        self.assertEqual(names["items"]["maxLength"], 140)
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertEqual(self.schema["required"], ["doctype"])
        json.dumps(self.schema)

    def test_request_key_and_description(self):
        self.assertIn("parent_context", IntelligenceRequest._REQUEST_ARGUMENTS)
        self.assertIn("parent_context", self.tool.description)
        self.assertIn("Sales Order Item", self.tool.description)

    def test_only_intelligence_request_gained_parent_context(self):
        for tool in (QueryRecords(), AggregateRecords(), DescribeDocType()):
            with self.subTest(tool.name):
                self.assertNotIn("parent_context", tool.inputSchema["properties"])
                self.assertIs(tool.inputSchema["additionalProperties"], False)

    def test_four_tools_are_still_registered_once(self):
        import erp_intelligence.hooks as hooks

        self.assertEqual(len(hooks.assistant_tools), len(set(hooks.assistant_tools)))
        names = []
        for path in hooks.assistant_tools:
            module, cls = path.rsplit(".", 1)
            names.append(getattr(__import__(module, fromlist=[cls]), cls)().name)
        self.assertEqual(sorted(names), ["aggregate_records", "describe_doctype", "intelligence_request", "query_records"])

    def test_fac_argument_validation_accepts_and_rejects(self):
        good = {"doctype": CHILD, "parent_context": {"parent_doctype": PARENT, "parent_names": ["X"]}}
        self.tool.validate_arguments(good)  # FAC's own check: required keys and top-level types
        with self.assertRaises(frappe.ValidationError):
            self.tool.validate_arguments({"doctype": CHILD, "parent_context": "Sales Order"})

    def test_tool_rejects_unknown_keys_inside_and_outside_parent_context(self):
        with self.assertRaises(frappe.ValidationError):
            self.tool.execute({"doctype": CHILD, "fields": ["name"], "bogus": 1})
        with self.assertRaises(frappe.ValidationError):
            self.tool.execute(
                {
                    "doctype": CHILD,
                    "fields": ["name"],
                    "parent_context": {"parent_doctype": PARENT, "parent_names": ["X"], "bogus": 1},
                }
            )

    def test_direct_query_records_tool_has_no_child_support(self):
        with self.assertRaises(frappe.ValidationError) as ctx:
            QueryRecords().execute(
                {
                    "doctype": CHILD,
                    "fields": ["name"],
                    "parent_context": {"parent_doctype": PARENT, "parent_names": ["X"]},
                }
            )
        self.assertIn("Unsupported arguments", str(ctx.exception))


# --------------------------------------------------------------------------- 6. metadata and relationship helpers
class TestMetadataAndRelationships(LiveCase):
    def test_child_standard_fields(self):
        fields = {f["fieldname"]: f for f in MetadataService.get_doctype_metadata(CHILD)["fields"]}
        for name, fieldtype in (("name", "Data"), ("parent", "Data"), ("parentfield", "Data"), ("idx", "Int")):
            self.assertTrue(fields[name]["standard"], name)
            self.assertEqual(fields[name]["fieldtype"], fieldtype)
        self.assertFalse(fields["item_code"]["standard"])

    def test_regular_doctype_standard_fields_are_unchanged(self):
        names = [d["fieldname"] for d in MetadataService.get_standard_field_definitions(frappe.get_meta(PARENT))]
        self.assertEqual(names, ["name", "owner", "creation", "modified", "modified_by", "docstatus"])

    def test_relationship_helper(self):
        helper = MetadataService.get_child_table_fieldnames
        self.assertEqual(helper(PARENT, CHILD), ["items"])
        self.assertEqual(helper(PARENT, "Sales Invoice Item"), [])
        self.assertEqual(helper(CHILD, PARENT), [], "a child cannot be a parent")
        self.assertEqual(helper(PARENT, "Customer"), [], "a non-child DocType never matches")
        with self.assertRaises(frappe.DoesNotExistError):
            helper(PARENT, "No Such DocType")

    def test_context_service_resolves_child_fields_only_with_parent_context(self):
        self.need("sees")
        frappe.set_user(FX["sees"])
        with_context = ContextService.resolve_field(
            CHILD, "Item Code", parent_doctype=PARENT, parent_fieldname="items"
        )
        self.assertEqual(with_context["match_status"], "resolved")
        self.assertEqual(with_context["resolved_value"]["fieldname"], "item_code")
        without = ContextService.resolve_field(CHILD, "Item Code")
        self.assertNotEqual(without["match_status"], "resolved")
        with self.assertRaises(frappe.ValidationError):
            ContextService.resolve_field(PARENT, "name", parent_doctype=PARENT, parent_fieldname="items")

    def test_describe_doctype_lists_child_rows_columns(self):
        self.need("sees")
        frappe.set_user(FX["sees"])
        result = DescribeDocType().execute({"doctype": PARENT})
        items = next(t for t in result["child_tables"] if t["fieldname"] == "items")
        names = {f["fieldname"] for f in items["fields"]}
        self.assertTrue({"name", "parent", "idx", "item_code"} <= names)
        self.assertIn("parent", items["queryable_fields"])


# --------------------------------------------------------------------------- 7. regression: normal queries
class TestNormalQueryRegression(LiveCase):
    def test_record_query_is_unchanged(self):
        self.need("sees")
        fields = ["name", "customer", "status"]
        result = self.call({"doctype": PARENT, "fields": fields, "page_length": 5}, FX["sees"])
        ex = result["execution_result"]
        self.assertEqual((result["status"], result["doctype"], result["plan_type"]), ("executed", PARENT, "records"))
        self.assertEqual(result["returned_count"], 5)
        self.assertTrue(result["has_more"])
        self.assertNotIn("parent_context", ex)
        self.assertEqual(sorted(ex), sorted(["doctype", "fields", "has_more", "offset", "page_length", "records", "result_type", "returned_count"]))
        # a normal query issues exactly one get_list and no parent gate (snapshot before the oracle runs)
        # (Frappe's own first-use permission-inventory lookups on DocType/DocPerm/DocShare
        # are not record queries and are ignored here.)
        issued = [c for c in self.calls if c[0] in (PARENT, CHILD)]
        self.assertEqual([c[0] for c in issued], [PARENT])
        self.assertNotIn("parent_doctype", issued[0][1])
        self.assertEqual(issued[0][1]["fields"], fields, "no parent gate: a normal query is one get_list")
        frappe.set_user(FX["sees"])
        native = frappe.get_list(PARENT, fields=fields, limit_page_length=5)
        self.assertEqual([r["name"] for r in ex["records"]], [r["name"] for r in native])

    def test_normal_plan_shape_is_unchanged(self):
        self.need("sees")
        frappe.set_user(FX["sees"])
        plan = QueryPlanner.plan_query({"doctype": PARENT, "fields": ["name"], "page_length": 2})["plan"]
        self.assertEqual(sorted(plan), sorted(["doctype", "fields", "filters", "sort", "page_length", "offset"]))

    def test_child_plan_shape(self):
        self.need("sees", "parents")
        frappe.set_user(FX["sees"])
        plan = QueryPlanner.plan_query(self.child_request())["plan"]
        self.assertEqual(plan["doctype"], CHILD)
        self.assertEqual((plan["parent_doctype"], plan["parent_fieldname"]), (PARENT, "items"))
        self.assertEqual(plan["parent_names"], FX["parents"][:2])

    def test_aggregation_is_unchanged(self):
        self.need("sees")
        request = {
            "doctype": PARENT,
            "aggregations": [{"operation": "count", "field": "name"}],
            "group_by": ["status"],
        }
        result = self.call(request, FX["sees"])
        self.assertEqual((result["status"], result["plan_type"], result["truncated"]), ("executed", "aggregation", False))
        got = {r["status"]: r["aggregation_1"] for r in result["execution_result"]["results"]}
        frappe.set_user(FX["sees"])
        native = frappe.get_list(PARENT, fields=["status", "count(name) as c"], group_by="status", limit_page_length=100)
        self.assertEqual(got, {r["status"]: r["c"] for r in native})
        self.assertEqual(result["execution_result"]["aggregations"][0]["count_basis"], "rows")

    def test_clarification_is_unchanged(self):
        self.need("sees")
        frappe.set_user(FX["sees"])
        probe = ContextService.resolve_field("Delivery Note", "Shipping Address")
        if probe["match_status"] != "ambiguous":
            self.skipTest("no ambiguous label available on this site")
        result = self.call({"doctype": "Delivery Note", "fields": ["name", "Shipping Address"]}, FX["sees"])
        self.assertEqual(result["status"], "needs_clarification")
        self.assertNothingExecuted()

    def test_unresolved_normal_doctype_is_unchanged(self):
        self.need("none")
        result = self.call({"doctype": PARENT, "fields": ["name"]}, FX["none"])
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual([i["location"] for i in result["unresolved_references"]], ["/doctype"])

    def test_normal_validator_does_not_read_the_new_limit(self):
        """A bad parent_names setting must not affect ordinary, non-child queries."""
        self.need("sees")
        frappe.set_user(FX["sees"])
        with mock.patch.dict(frappe.local.conf, {"erp_intelligence_max_parent_names": "bad"}):
            plan = QueryValidator.validate_query({"doctype": PARENT, "fields": ["name"], "page_length": 1})
            result = QueryService.query_records({"doctype": PARENT, "fields": ["name"], "page_length": 1})
        self.assertIsNone(plan["parent_names"])
        self.assertEqual(result["returned_count"], 1)

    def test_direct_tools_still_work(self):
        self.need("sees")
        frappe.set_user(FX["sees"])
        records = QueryRecords().execute({"doctype": PARENT, "fields": ["name"], "page_length": 2})
        self.assertEqual(records["returned_count"], 2)
        aggregate = AggregateRecords().execute(
            {"doctype": PARENT, "aggregations": [{"operation": "count", "field": "name"}]}
        )
        self.assertEqual(aggregate["truncated"], False)
        with self.assertRaises(frappe.ValidationError) as ctx:  # unchanged since before child support
            QueryRecords().execute({"doctype": CHILD, "fields": ["name"]})
        self.assertIn("require parent context", str(ctx.exception))

    def test_describe_doctype_for_a_normal_doctype_is_unchanged(self):
        self.need("sees")
        frappe.set_user(FX["sees"])
        result = DescribeDocType().execute({"doctype": PARENT})
        self.assertEqual(sorted(result), ["child_tables", "doctype", "fields", "label", "queryable_fields"])
        standard = {f["fieldname"] for f in result["fields"] if f["standard"]}
        self.assertEqual(standard, {"name", "owner", "creation", "modified", "modified_by", "docstatus"})


# --------------------------------------------------------------------------- 8. no permission bypass in source
class TestNoPermissionBypassInSource(unittest.TestCase):
    """The code paths added or changed here must not contain bypass constructs."""

    FORBIDDEN_FRAPPE = {"get_all", "set_user"}
    FORBIDDEN_FRAPPE_DB = {"sql", "get_value", "get_values", "get_all", "get_list"}

    @staticmethod
    def _is_frappe(node):
        return isinstance(node, ast.Name) and node.id == "frappe"

    @classmethod
    def _is_frappe_db(cls, node):
        return isinstance(node, ast.Attribute) and node.attr == "db" and cls._is_frappe(node.value)

    def test_no_unrestricted_reads_or_user_switching(self):
        root = pathlib.Path(erp_intelligence.__file__).parent
        files = [
            *(root / "services").glob("*.py"),
            *(root / "tools").glob("*.py"),
            root / "utils" / "response_utils.py",
        ]
        offences = []
        for path in files:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    if self._is_frappe(node.value) and node.attr in self.FORBIDDEN_FRAPPE:
                        offences.append(f"{path.name}:{node.lineno} frappe.{node.attr}")
                    if self._is_frappe_db(node.value) and node.attr in self.FORBIDDEN_FRAPPE_DB:
                        offences.append(f"{path.name}:{node.lineno} frappe.db.{node.attr}")
                if isinstance(node, ast.keyword) and node.arg == "ignore_permissions":
                    offences.append(f"{path.name}:{node.value.lineno} ignore_permissions=")
                if isinstance(node, ast.Constant) and node.value == "Administrator":
                    offences.append(f"{path.name}:{node.lineno} 'Administrator'")
        self.assertEqual(offences, [])


if __name__ == "__main__":
    unittest.main()
