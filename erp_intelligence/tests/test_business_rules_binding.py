"""Tests for confirmation binding.

Covers the canonical form (with golden vectors that pin the format), the HMAC key
derivation, the engine's handling of supplied confirmations, and the assistant tool's
checks on the engine's binding section.

The golden values were computed with a separate HMAC-SHA256 calculation over
hand-written canonical text, not with the code under test. If one of them changes,
the binding format changed, and every outstanding confirmation is invalidated:
bump ``BINDING_VERSION`` deliberately instead of editing the vector.

Records are created inside the test transaction and rolled back.

Run with:
    bench --site <site> run-tests --module erp_intelligence.tests.test_business_rules_binding
"""

import ast
import datetime
import decimal
import hashlib
import hmac
import json
import random
import pathlib
import unittest
from collections import OrderedDict
from decimal import Decimal
from fractions import Fraction
from unittest import mock

import frappe

import erp_intelligence.services.business_rules.binding as binding
from erp_intelligence.services.business_rules import constants as c
from erp_intelligence.services.business_rules.binding import BindingUnavailable, canonicalize
from erp_intelligence.services.business_rules.engine import BusinessRulesEngine
from erp_intelligence.tests.test_business_rules_engine import _make_rule, _make_set, _registry
from erp_intelligence.tests.test_business_rules_tool import ENABLED_CONF, ENGINE_PATH, TARGET, ToolCase

SECRET = "golden-test-encryption-key-not-a-real-key"
GOLDEN_KEY_HEX = "23b9d9a14a01082ef138173728c8c6fc30a0f91b3ddde98be7550b10bce8ba36"
GOLDEN_FINGERPRINT = "v1.391f2ffc559974d4f27e6922d668fc51"
GOLDEN_CANONICAL = (
    '{"doctype":"Sample DocType","execution_options":{"submit":false},"execution_result":{},'
    '"operation":"Create","payload":{"amount":83.25,"items":[{"qty":2,"uom":"Nos"},{"qty":1,"uom":"Kg"}],'
    '"note":"caf\u00e9","remarks":null,"required_by":"2026-01-05"},'
    '"rule_set":{"modified":"2026-03-01T10:15:30","name":"Sample Set","version":"3"},'
    '"rules":[{"modified":"2026-02-01T08:00:00","rule_code":"RULE_A"},'
    '{"modified":"2026-02-02T09:30:00.250000","rule_code":"RULE_B"}],'
    '"stage":"PRE_EXECUTION","user":"user@example.com","version":1}'
)
SENTINEL = "ZQX-BOUND-PAYLOAD-4455"


def setUpModule():
    if not getattr(frappe.local, "site", None):
        raise unittest.SkipTest("requires an initialized Frappe site")


def tearDownModule():
    frappe.set_user("Administrator")
    frappe.db.rollback()


def golden_context():
    """The golden context, built with scrambled key order and non-normalized values."""
    payload = OrderedDict(
        [
            ("required_by", datetime.date(2026, 1, 5)),
            ("remarks", None),
            ("note", "caf\u00e9"),
            ("items", [OrderedDict([("uom", "Nos"), ("qty", 2)]), OrderedDict([("uom", "Kg"), ("qty", 1.0)])]),
            ("amount", Decimal("83.2500")),
        ]
    )
    return binding.build_context(
        user="user@example.com",
        doctype="Sample DocType",
        operation="Create",
        stage="PRE_EXECUTION",
        rule_set={"name": "Sample Set", "version": "3", "modified": datetime.datetime(2026, 3, 1, 10, 15, 30)},
        rules=[
            {"rule_code": "RULE_A", "modified": datetime.datetime(2026, 2, 1, 8, 0, 0)},
            {"rule_code": "RULE_B", "modified": datetime.datetime(2026, 2, 2, 9, 30, 0, 250000)},
        ],
        payload=payload,
        execution_options=OrderedDict([("submit", False)]),
        execution_result=None,
    )


# --------------------------------------------------------------------- canonical form
class TestCanonicalization(unittest.TestCase):
    def test_object_keys_are_sorted_recursively(self):
        value = OrderedDict([("b", 2), ("a", OrderedDict([("d", [3, 1, 2]), ("c", None)]))])
        self.assertEqual(canonicalize(value), '{"a":{"c":null,"d":[3,1,2]},"b":2}')
        self.assertEqual(canonicalize({"b": 1, "a": 2}), canonicalize({"a": 2, "b": 1}))
        deep = {"z": {"y": {"x": 1, "w": 2}}}
        self.assertEqual(canonicalize(deep), '{"z":{"y":{"w":2,"x":1}}}')

    def test_keys_sort_by_code_point(self):
        self.assertEqual(canonicalize({"b": 1, "B": 2, "a": 3, "\u00e9": 4}), '{"B":2,"a":3,"b":1,"\u00e9":4}')

    def test_list_order_is_preserved_and_significant(self):
        self.assertEqual(canonicalize([3, 1, 2]), "[3,1,2]")
        self.assertNotEqual(canonicalize([1, 2]), canonicalize([2, 1]))
        self.assertNotEqual(canonicalize({"items": [{"a": 1}, {"a": 2}]}), canonicalize({"items": [{"a": 2}, {"a": 1}]}))
        self.assertEqual(canonicalize((1, 2)), canonicalize([1, 2]))

    def test_equivalent_numeric_representations_agree(self):
        for equivalent in (1, 1.0, Decimal("1"), Decimal("1.00"), Decimal("1E+0"), Decimal("0.1E+1")):
            with self.subTest(repr(equivalent)):
                self.assertEqual(canonicalize(equivalent), "1")
        self.assertEqual(canonicalize(83.25), canonicalize(Decimal("83.2500")))
        self.assertEqual(canonicalize(100), canonicalize(Decimal("1E+2")))
        self.assertEqual(canonicalize(100), canonicalize(100.0))

    def test_number_text_is_exact(self):
        cases = {
            0: "0", 0.0: "0", -0.0: "0", Decimal("-0.00"): "0",
            0.1: "0.1", -2.5: "-2.5", 1e21: "1000000000000000000000", 1.5e-7: "0.00000015",
            10**30: "1000000000000000000000000000000", Decimal("83.2500"): "83.25",
            Decimal("1E+2"): "100", 7: "7", -7: "-7",
        }
        for value, expected in cases.items():
            with self.subTest(repr(value)):
                self.assertEqual(canonicalize(value), expected)

    def test_numbers_that_differ_stay_different(self):
        self.assertNotEqual(canonicalize(0.1 + 0.2), canonicalize(0.3))
        self.assertNotEqual(canonicalize(83.25), canonicalize(83.2500001))
        self.assertNotEqual(canonicalize(1), canonicalize(-1))

    # ---------------------------------------------- exactness beyond the Decimal context precision
    def test_numbers_with_more_than_28_significant_digits_are_exact(self):
        """Decimal.normalize() rounds to the context precision (28). The canonical text must not."""
        cases = {
            Decimal("123456789012345678901234567890.123456789"): "123456789012345678901234567890.123456789",
            Decimal("-123456789012345678901234567890.123456789"): "-123456789012345678901234567890.123456789",
            Decimal("1.0000000000000000000000000000001"): "1.0000000000000000000000000000001",
            Decimal("0.1234567890123456789012345678901234567890"): "0.123456789012345678901234567890123456789",
            12345678901234567890123456789012345: "12345678901234567890123456789012345",
            -(10**40 + 1): "-10000000000000000000000000000000000000001",
            Decimal("9" * 60): "9" * 60,
            Decimal("9" * 60 + "." + "9" * 60): "9" * 60 + "." + "9" * 60,
        }
        for value, expected in cases.items():
            with self.subTest(str(value)[:50]):
                self.assertEqual(canonicalize(value), expected)

    def test_values_that_differ_only_beyond_the_28th_digit_stay_different(self):
        pairs = [
            (Decimal("1.0000000000000000000000000000001"), Decimal("1.0000000000000000000000000000002")),
            (Decimal("1.0000000000000000000000000000001"), Decimal(1)),
            (Decimal("1234567890123456789012345678.9"), Decimal("1234567890123456789012345678.8")),
            (Decimal("123456789012345678901234567890.123456789"), Decimal("123456789012345678901234567890.123456788")),
            (12345678901234567890123456789012345, 12345678901234567890123456789012346),
            (10**30, 10**30 + 1),
            (Decimal("0.0000000000000000000000000000001"), Decimal("0.0000000000000000000000000000002")),
        ]
        for first, second in pairs:
            with self.subTest(f"{str(first)[:30]} vs {str(second)[:30]}"):
                self.assertNotEqual(canonicalize(first), canonicalize(second))

    def test_the_canonical_text_does_not_depend_on_the_decimal_context(self):
        values = [Decimal("123456789012345678901234567890.123456789"), 12345678901234567890123456789012345,
                  Decimal("1.0000000000000000000000000000001"), Decimal("1E-30"), Decimal("1E+60"), 83.25,
                  Decimal("100.50")]
        reference = [canonicalize(value) for value in values]
        for precision in (1, 5, 28, 100, 1000):
            with self.subTest(precision), decimal.localcontext(decimal.Context(prec=precision)):
                self.assertEqual([canonicalize(value) for value in values], reference)
        with decimal.localcontext(decimal.Context(prec=3, Emax=5, Emin=-5, traps=[])):
            self.assertEqual([canonicalize(value) for value in values], reference)

    def test_long_equivalent_representations_still_agree(self):
        base = "123456789012345678901234567890.123"
        for equivalent in (Decimal(base), Decimal(base + "0000000"), Decimal("00" + base)):
            self.assertEqual(canonicalize(equivalent), base)
        self.assertEqual(canonicalize(10**40), "1" + "0" * 40)
        self.assertEqual(canonicalize(Decimal("1E+40")), "1" + "0" * 40)
        self.assertEqual(canonicalize(Decimal("1" + "0" * 40 + ".000")), "1" + "0" * 40)
        self.assertEqual(canonicalize(Decimal("1" + "0" * 40)), canonicalize(10**40))
        self.assertEqual(canonicalize(Decimal("0.5000000000000000000000000000000")), "0.5")
        self.assertEqual(canonicalize(Decimal("-0.5000000000000000000000000000000")), "-0.5")

    def test_very_small_decimals(self):
        self.assertEqual(canonicalize(Decimal("1E-30")), "0." + "0" * 29 + "1")
        self.assertEqual(canonicalize(1e-30), "0." + "0" * 29 + "1")
        self.assertEqual(canonicalize(Decimal("-2.5E-40")), "-0." + "0" * 39 + "25")
        self.assertEqual(canonicalize(Decimal("1E-999")), "0." + "0" * 998 + "1")
        self.assertEqual(canonicalize(Decimal("1E-1000")), "0." + "0" * 999 + "1")  # the supported limit
        self.assertNotEqual(canonicalize(Decimal("1E-30")), canonicalize(Decimal("2E-30")))
        self.assertNotEqual(canonicalize(Decimal("1E-30")), canonicalize(0))
        self.assertEqual(canonicalize(Decimal("0E-50")), "0")

    def test_very_large_supported_decimals(self):
        self.assertEqual(canonicalize(Decimal("1E+999")), "1" + "0" * 999)
        self.assertEqual(canonicalize(Decimal("1E+1000")), "1" + "0" * 1000)  # the supported limit
        self.assertEqual(canonicalize(Decimal("1.5E+1000")), "15" + "0" * 999)
        self.assertEqual(canonicalize(10**1000), "1" + "0" * 1000)
        self.assertEqual(canonicalize(Decimal("-9.87654321E+500")), "-987654321" + "0" * 492)
        self.assertNotEqual(canonicalize(Decimal("1E+999")), canonicalize(Decimal("1E+998")))
        self.assertNotEqual(canonicalize(Decimal("1E+999")), canonicalize(Decimal("1.000001E+999")))

    def test_out_of_range_and_non_finite_numbers_are_still_rejected(self):
        for value in (Decimal("1E+1001"), Decimal("1E-1001"), 10**1001, Decimal("-1E+5000"), float("inf"),
                      float("-inf"), float("nan"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN"),
                      Decimal("sNaN")):
            with self.subTest(repr(value)[:30]), self.assertRaises(BindingUnavailable):
                canonicalize(value)

    def test_exactness_does_not_make_unlike_types_equal(self):
        long_number = Decimal("123456789012345678901234567890.123456789")
        self.assertNotEqual(canonicalize(long_number), canonicalize(str(long_number)))
        self.assertEqual(canonicalize(str(long_number)), '"123456789012345678901234567890.123456789"')
        self.assertNotEqual(canonicalize(1), canonicalize(True))
        self.assertNotEqual(canonicalize(Decimal("1.00")), canonicalize("1"))
        self.assertNotEqual(canonicalize(0), canonicalize(False))

    def test_the_canonical_text_always_has_exactly_the_value_of_the_number(self):
        """Independent check: parse the text back with exact fractions and compare."""
        rng = random.Random(20260619)
        values = [Decimal("123456789012345678901234567890.123456789"), 12345678901234567890123456789012345,
                  Decimal("1E-1000"), Decimal("1E+1000"), Decimal("-7.000000000000000000000000000000001E+35"),
                  Decimal("0.000000000000000000000000000000000000001"), 3, -3, 0.5, 1e-7, 123456.789e3]
        for _ in range(300):
            digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 70)))
            exponent = rng.randint(-80, 80)
            sign = rng.choice(["", "-"])
            values.append(Decimal(f"{sign}{digits}E{exponent}"))
        for value in values:
            text = canonicalize(value)
            exact = Fraction(Decimal(repr(value)) if isinstance(value, float) else value)
            self.assertEqual(Fraction(text), exact, text)
            self.assertNotIn("E", text.upper())  # no exponent
            if "." in text:
                self.assertFalse(text.endswith("0") or text.endswith("."), text)  # shortest form
                self.assertFalse(text.startswith(("00", "-00")), text)
            self.assertNotEqual(text, "-0")

    def test_numbers_and_strings_are_distinct(self):
        self.assertEqual(canonicalize("1"), '"1"')
        self.assertNotEqual(canonicalize("1"), canonicalize(1))
        self.assertNotEqual(canonicalize({"qty": "1"}), canonicalize({"qty": 1}))
        self.assertNotEqual(canonicalize(Decimal("1.00")), canonicalize("1.00"))

    def test_booleans_are_distinct_from_numbers(self):
        self.assertEqual((canonicalize(True), canonicalize(False)), ("true", "false"))
        self.assertNotEqual(canonicalize(True), canonicalize(1))
        self.assertNotEqual(canonicalize(False), canonicalize(0))
        self.assertNotEqual(canonicalize([True]), canonicalize([1.0]))
        self.assertNotEqual(canonicalize(True), canonicalize("true"))

    def test_a_missing_field_is_not_an_explicit_none(self):
        self.assertEqual(canonicalize({"a": None}), '{"a":null}')
        self.assertNotEqual(canonicalize({"a": None}), canonicalize({}))
        self.assertNotEqual(canonicalize({"a": None, "b": 1}), canonicalize({"b": 1}))
        self.assertNotEqual(canonicalize({"a": ""}), canonicalize({"a": None}))
        self.assertNotEqual(canonicalize({"a": []}), canonicalize({"a": None}))

    def test_dates_and_times_serialize_as_iso_strings(self):
        self.assertEqual(canonicalize(datetime.date(2026, 1, 5)), '"2026-01-05"')
        self.assertEqual(canonicalize(datetime.datetime(2026, 1, 5, 10, 30, 0)), '"2026-01-05T10:30:00"')
        self.assertEqual(
            canonicalize(datetime.datetime(2026, 1, 5, 10, 30, 0, 250000)), '"2026-01-05T10:30:00.250000"'
        )
        self.assertEqual(canonicalize(datetime.time(10, 30)), '"10:30:00"')
        self.assertEqual(canonicalize(datetime.timedelta(seconds=90)), '"90"')
        aware = datetime.datetime(2026, 1, 5, 10, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(canonicalize(aware), '"2026-01-05T10:30:00+00:00"')

    def test_a_date_and_its_iso_string_agree_but_other_formats_do_not(self):
        self.assertEqual(canonicalize(datetime.date(2026, 1, 5)), canonicalize("2026-01-05"))
        self.assertNotEqual(canonicalize(datetime.datetime(2026, 1, 5, 10, 30)), canonicalize("2026-01-05 10:30:00"))
        self.assertNotEqual(canonicalize(datetime.date(2026, 1, 5)), canonicalize("05-01-2026"))

    def test_encoding_is_compact_json(self):
        text = canonicalize({"a": [1, {"b": "x y"}], "c": {}})
        self.assertEqual(text, '{"a":[1,{"b":"x y"}],"c":{}}')
        self.assertNotIn(", ", text)
        self.assertNotIn(": ", text)
        self.assertNotIn("\n", text)
        self.assertEqual(canonicalize({}), "{}")
        self.assertEqual(canonicalize([]), "[]")

    def test_encoding_is_utf_8(self):
        self.assertEqual(binding.to_canonical_bytes("caf\u00e9"), b'"caf\xc3\xa9"')
        self.assertEqual(binding.to_canonical_bytes("\u20b9"), '"\u20b9"'.encode("utf-8"))
        self.assertEqual(binding.to_canonical_bytes("\U0001f4e6"), b'"\xf0\x9f\x93\xa6"')
        self.assertEqual(binding.to_canonical_bytes({"\u00e9": 1}), b'{"\xc3\xa9":1}')
        with self.assertRaises(BindingUnavailable):
            binding.to_canonical_bytes("lone surrogate \ud800")

    def test_text_is_not_normalized(self):
        self.assertNotEqual(canonicalize("\u00e9"), canonicalize("e\u0301"))
        self.assertNotEqual(canonicalize("a"), canonicalize("a "))
        self.assertNotEqual(canonicalize("a"), canonicalize("A"))

    def test_values_that_cannot_be_canonicalized_make_binding_unavailable(self):
        cyclic = {}
        cyclic["self"] = cyclic
        nested = current = {}
        for _ in range(500):
            current["x"] = {}
            current = current["x"]
        for value in (object(), {1, 2}, b"bytes", {1: "non-string key"}, float("nan"), float("inf"),
                      Decimal("NaN"), Decimal("1E+5000"), cyclic, nested):
            with self.subTest(repr(value)[:30]), self.assertRaises(BindingUnavailable):
                canonicalize(value)


# ------------------------------------------------------------------------ golden vectors
class TestGoldenVectors(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(frappe.local.conf, {"encryption_key": SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_derived_key(self):
        self.assertEqual(binding.derive_key().hex(), GOLDEN_KEY_HEX)
        self.assertEqual(binding.KEY_CONTEXT, b"erp_intelligence.business_rules.confirmation_binding.v1")

    def test_canonical_text_of_the_golden_context(self):
        self.assertEqual(canonicalize(golden_context()), GOLDEN_CANONICAL)

    def test_fingerprint_of_the_golden_context(self):
        self.assertEqual(binding.fingerprint(golden_context()), GOLDEN_FINGERPRINT)

    def test_the_golden_fingerprint_matches_an_independent_calculation(self):
        key = hmac.new(SECRET.encode(), b"erp_intelligence.business_rules.confirmation_binding.v1", hashlib.sha256).digest()
        digest = hmac.new(key, GOLDEN_CANONICAL.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(GOLDEN_FINGERPRINT, "v1." + digest[:32])

    def test_fingerprint_format(self):
        self.assertTrue(binding.FINGERPRINT_PATTERN.fullmatch(GOLDEN_FINGERPRINT))
        self.assertEqual(len(GOLDEN_FINGERPRINT), 3 + 32)
        self.assertEqual(binding.BINDING_VERSION, 1)

    def test_equivalent_contexts_have_the_golden_fingerprint(self):
        context = golden_context()
        context["payload"] = {
            "amount": 83.25,
            "items": [{"qty": 2.0, "uom": "Nos"}, {"qty": Decimal("1.0"), "uom": "Kg"}],
            "note": "caf\u00e9",
            "remarks": None,
            "required_by": "2026-01-05",
        }
        context["execution_result"] = {}
        self.assertEqual(binding.fingerprint(context), GOLDEN_FINGERPRINT)

    def test_any_change_to_the_golden_context_changes_the_fingerprint(self):
        def mutate(change):
            context = golden_context()
            change(context)
            return binding.fingerprint(context)

        changes = {
            "user": lambda x: x.update(user="other@example.com"),
            "doctype": lambda x: x.update(doctype="Other"),
            "operation": lambda x: x.update(operation="Update"),
            "stage": lambda x: x.update(stage="PRE_COMMIT"),
            "version": lambda x: x.update(version=2),
            "rule set name": lambda x: x["rule_set"].update(name="Other Set"),
            "rule set version": lambda x: x["rule_set"].update(version="4"),
            "rule set modified": lambda x: x["rule_set"].update(modified=datetime.datetime(2026, 3, 1, 10, 15, 31)),
            "rule modified": lambda x: x["rules"][0].update(modified=datetime.datetime(2026, 2, 1, 8, 0, 1)),
            "rule code": lambda x: x["rules"][1].update(rule_code="RULE_C"),
            "rule removed": lambda x: x["rules"].pop(),
            "rule order": lambda x: x["rules"].reverse(),
            "amount": lambda x: x["payload"].update(amount=Decimal("83.26")),
            "nested qty": lambda x: x["payload"]["items"][0].update(qty=3),
            "nested uom": lambda x: x["payload"]["items"][1].update(uom="Nos"),
            "row order": lambda x: x["payload"]["items"].reverse(),
            "extra field": lambda x: x["payload"].update(extra=1),
            "removed field": lambda x: x["payload"].pop("remarks"),
            "none to empty": lambda x: x["payload"].update(remarks=""),
            "date": lambda x: x["payload"].update(required_by=datetime.date(2026, 1, 6)),
            "options": lambda x: x["execution_options"].update(submit=True),
            "result": lambda x: x.update(execution_result={"name": "DOC-1"}),
        }
        seen = {GOLDEN_FINGERPRINT}
        for label, change in changes.items():
            with self.subTest(label):
                value = mutate(change)
                self.assertNotIn(value, seen)
                seen.add(value)

    def test_different_exact_numbers_give_different_canonical_text_and_fingerprints(self):
        values = [
            Decimal("123456789012345678901234567890.123456789"),
            Decimal("123456789012345678901234567890.123456788"),
            Decimal("123456789012345678901234567900"),  # what the old rounding produced
            Decimal("1.0000000000000000000000000000001"),
            Decimal("1.0000000000000000000000000000002"),
            Decimal(1),
            12345678901234567890123456789012345,
            12345678901234567890123456789012346,
            Decimal("1E-30"),
            Decimal("2E-30"),
            Decimal("1E+999"),
            Decimal("1E+998"),
            0,
        ]
        texts = [canonicalize(value) for value in values]
        self.assertEqual(len(set(texts)), len(values))

        def fingerprint_of(value):
            context = golden_context()
            context["payload"] = {"amount": value}
            return binding.fingerprint(context)

        fingerprints = [fingerprint_of(value) for value in values]
        self.assertEqual(len(set(fingerprints)), len(values))
        # The same exact value in another representation still has the same fingerprint.
        self.assertEqual(
            fingerprint_of(Decimal("123456789012345678901234567890.1234567890000")),
            fingerprint_of(Decimal("123456789012345678901234567890.123456789")),
        )
        self.assertEqual(fingerprint_of(10**40), fingerprint_of(Decimal("1E+40")))

    def test_the_fingerprint_does_not_depend_on_the_decimal_context(self):
        context = golden_context()
        context["payload"] = {"amount": Decimal("123456789012345678901234567890.123456789")}
        reference = binding.fingerprint(context)
        for precision in (1, 28, 200):
            with decimal.localcontext(decimal.Context(prec=precision)):
                self.assertEqual(binding.fingerprint(context), reference)

    def test_context_keys_are_exactly_what_the_report_says_it_covers(self):
        self.assertEqual(set(golden_context()), set(binding.COVERS))
        self.assertEqual(len(binding.COVERS), len(set(binding.COVERS)))


# ------------------------------------------------------------------------------ key
class TestKeyDerivation(unittest.TestCase):
    def test_the_key_is_derived_not_the_encryption_key_itself(self):
        with mock.patch.dict(frappe.local.conf, {"encryption_key": SECRET}):
            key = binding.derive_key()
        self.assertEqual(len(key), 32)
        self.assertNotEqual(key, SECRET.encode())
        self.assertEqual(
            key, hmac.new(SECRET.encode(), binding.KEY_CONTEXT, hashlib.sha256).digest()
        )

    def test_different_secrets_give_different_keys_and_fingerprints(self):
        with mock.patch.dict(frappe.local.conf, {"encryption_key": SECRET}):
            first = binding.fingerprint(golden_context())
        with mock.patch.dict(frappe.local.conf, {"encryption_key": SECRET + "x"}):
            second = binding.fingerprint(golden_context())
        self.assertNotEqual(first, second)

    def test_the_fingerprint_is_keyed(self):
        """A plain hash of the context must not equal the fingerprint, so logs cannot confirm guesses."""
        plain = hashlib.sha256(GOLDEN_CANONICAL.encode()).hexdigest()
        self.assertNotEqual(GOLDEN_FINGERPRINT, "v1." + plain[:32])
        self.assertNotIn(plain[:32], GOLDEN_FINGERPRINT)

    def test_no_usable_key_makes_binding_unavailable(self):
        for value in (None, "", "   ", 5, b"bytes", ["x"]):
            with self.subTest(repr(value)), mock.patch.dict(frappe.local.conf):
                frappe.local.conf.pop("encryption_key", None)
                if value is not None:
                    frappe.local.conf["encryption_key"] = value
                with self.assertRaises(BindingUnavailable):
                    binding.derive_key()
                with self.assertRaises(BindingUnavailable):
                    binding.fingerprint(golden_context())

    def test_the_key_is_never_created_or_written(self):
        """get_encryption_key() can write a missing key to site_config.json. It must not be used."""
        with mock.patch.dict(frappe.local.conf), mock.patch(
            "frappe.utils.password.get_encryption_key", side_effect=AssertionError("get_encryption_key called")
        ), mock.patch("frappe.installer.update_site_config", side_effect=AssertionError("site config written")):
            frappe.local.conf.pop("encryption_key", None)
            with self.assertRaises(BindingUnavailable):
                binding.derive_key()
            frappe.local.conf["encryption_key"] = SECRET
            self.assertEqual(binding.derive_key().hex(), GOLDEN_KEY_HEX)

    def test_the_module_cannot_create_a_key_or_write_anything(self):
        source = pathlib.Path(binding.__file__).read_text()
        names = {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)}
        for forbidden in ("get_encryption_key", "update_site_config", "generate_encryption_key", "db",
                          "insert", "save", "set_value", "commit", "get_doc", "get_all"):
            self.assertNotIn(forbidden, names)


# -------------------------------------------------------------------------- comparison
class TestCheck(unittest.TestCase):
    FINGERPRINT = "v1." + "0a" * 16

    def test_reasons(self):
        check = binding.check
        self.assertIsNone(check(self.FINGERPRINT, self.FINGERPRINT))
        self.assertEqual(check(self.FINGERPRINT, None), binding.REASON_UNAVAILABLE)
        self.assertEqual(check(None, None), binding.REASON_UNAVAILABLE)
        for missing in (None, "", "   "):
            self.assertEqual(check(missing, self.FINGERPRINT), binding.REASON_MISSING, repr(missing))
        for invalid in ("garbage", "v1.", "v1." + "g" * 32, "V1." + "0a" * 16, "v1." + "0a" * 15,
                        "v1." + "0A" * 16, 5, ["x"], {"a": 1}, b"v1."):
            self.assertEqual(check(invalid, self.FINGERPRINT), binding.REASON_INVALID, repr(invalid))
        self.assertEqual(check("v1." + "0b" * 16, self.FINGERPRINT), binding.REASON_STALE)
        self.assertEqual(check("v2." + "0a" * 16, self.FINGERPRINT), binding.REASON_STALE)

    def test_comparison_is_constant_time(self):
        with mock.patch.object(binding.hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            binding.check(self.FINGERPRINT, self.FINGERPRINT)
            binding.check("v1." + "0b" * 16, self.FINGERPRINT)
        self.assertEqual(compare.call_count, 2)
        for call in compare.call_args_list:
            self.assertTrue(all(isinstance(arg, bytes) for arg in call.args))

    def test_the_module_never_compares_fingerprints_with_equality(self):
        tree = ast.parse(pathlib.Path(binding.__file__).read_text())
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "check")
        for node in ast.walk(function):
            if isinstance(node, ast.Compare):
                for operator in node.ops:
                    self.assertNotIsInstance(operator, (ast.Eq, ast.NotEq))


# ------------------------------------------------------------------------------ engine
class StubData:
    """Stands in for RuleDataAccess and returns whatever evidence the test chooses."""

    def __init__(self, rows):
        self.rows = rows
        self.request_count = 0

    def fetch_records(self, doctype, fields, filters=(), **kwargs):
        self.request_count += 1
        return list(self.rows)


class BindingCase(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.registry = _registry()
        self.registry.register("test.evidence", lambda ctx: ctx.passed("ok", {"rows": ctx.data.fetch_records("ToDo", ["name"])}))
        for patcher in (
            mock.patch.object(frappe, "logger", return_value=mock.MagicMock()),
            mock.patch.dict(frappe.local.conf, {"encryption_key": SECRET}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(frappe.set_user, "Administrator")
        self.addCleanup(frappe.db.rollback)

    def evaluate(self, payload=None, *, doctype=TARGET, operation="Create", stage=c.PRE_EXECUTION, **kwargs):
        kwargs.setdefault("registry", self.registry)
        return BusinessRulesEngine.evaluate(
            doctype, operation, {"amount": 1} if payload is None else payload, stage=stage, **kwargs
        )

    def fingerprint(self, *args, **kwargs):
        return self.evaluate(*args, **kwargs)["confirmation_binding"]["fingerprint"]

    def confirm_set(self):
        _make_rule("TEST_RULE", "test.needs_confirmation")
        return _make_set([("TEST_RULE", 10)])


class TestEngineFingerprint(BindingCase):
    def test_every_report_carries_the_binding_section(self):
        self.confirm_set()
        report = self.evaluate()
        section = report["confirmation_binding"]
        self.assertEqual(
            set(section),
            {"version", "available", "fingerprint", "covers", "required",
             "confirmations_applied", "confirmations_rejected"},
        )
        self.assertEqual(section["version"], 1)
        self.assertIs(section["available"], True)
        self.assertTrue(binding.FINGERPRINT_PATTERN.fullmatch(section["fingerprint"]))
        self.assertEqual(section["covers"], list(binding.COVERS))
        self.assertIs(section["required"], False)
        self.assertEqual(section["confirmations_applied"], [])
        self.assertIsNone(section["confirmations_rejected"])

    def test_reports_that_evaluated_nothing_have_no_section(self):
        self.assertIsNone(self.evaluate(operation="Delete")["confirmation_binding"])

    def test_the_fingerprint_is_deterministic(self):
        self.confirm_set()
        self.assertEqual(self.fingerprint(), self.fingerprint())
        self.assertEqual(self.fingerprint({"a": 1, "b": 2}), self.fingerprint({"b": 2, "a": 1}))

    def test_the_report_never_contains_payload_values(self):
        self.confirm_set()
        payload = {"supplier": SENTINEL, "nested": {"token": SENTINEL, "rows": [{"rate": 98765.4321}]}}
        section = self.evaluate(payload)["confirmation_binding"]
        text = json.dumps(section)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("98765", text)
        self.assertNotIn("supplier", text)

    def test_the_context_covers_exactly_the_documented_elements(self):
        self.confirm_set()
        rule_set = frappe.get_doc("Business Rule Set", "TEST BRE Set")
        rule_modified = frappe.db.get_value("Business Rule", "TEST_RULE", "modified")
        with mock.patch.object(binding, "fingerprint", wraps=binding.fingerprint) as spy:
            self.evaluate({"amount": 5}, execution_options={"submit": False}, execution_result={"name": "X"})
        context = spy.call_args.args[0]
        self.assertEqual(
            context,
            {
                "version": 1,
                "user": "Administrator",
                "doctype": TARGET,
                "operation": "Create",
                "stage": c.PRE_EXECUTION,
                "rule_set": {"name": "TEST BRE Set", "version": "1", "modified": rule_set.modified},
                "rules": [{"rule_code": "TEST_RULE", "modified": rule_modified}],
                "payload": {"amount": 5},
                "execution_options": {"submit": False},
                "execution_result": {"name": "X"},
            },
        )

    def test_every_context_element_changes_the_fingerprint(self):
        _make_rule("TEST_RULE", "test.needs_confirmation")
        _make_rule("TEST_OTHER", "test.pass", stage=c.PRE_COMMIT)
        _make_set([("TEST_RULE", 10), ("TEST_OTHER", 20)])  # a rule at PRE_EXECUTION and at PRE_COMMIT
        _make_set([("TEST_RULE", 10)], name="TEST BRE Update", operation="Update")
        _make_set([("TEST_RULE", 10)], name="TEST BRE Note", target="Note")
        base = self.fingerprint({"amount": 1, "rows": [1, 2]})
        seen = {base}

        def check(label, value):
            with self.subTest(label):
                self.assertNotIn(value, seen)
                seen.add(value)

        check("payload value", self.fingerprint({"amount": 2, "rows": [1, 2]}))
        check("payload nested", self.fingerprint({"amount": 1, "rows": [1, 3]}))
        check("payload added key", self.fingerprint({"amount": 1, "rows": [1, 2], "x": 1}))
        check("payload list order", self.fingerprint({"amount": 1, "rows": [2, 1]}))
        check("execution_options", self.fingerprint({"amount": 1, "rows": [1, 2]}, execution_options={"submit": True}))
        check("execution_result", self.fingerprint({"amount": 1, "rows": [1, 2]}, execution_result={"name": "X"}))
        check("stage", self.fingerprint({"amount": 1, "rows": [1, 2]}, stage=c.PRE_COMMIT))
        check("operation", self.fingerprint({"amount": 1, "rows": [1, 2]}, operation="Update"))
        check("doctype", self.fingerprint({"amount": 1, "rows": [1, 2]}, doctype="Note"))
        frappe.set_user("Guest")
        check("user", self.fingerprint({"amount": 1, "rows": [1, 2]}))
        frappe.set_user("Administrator")
        frappe.db.set_value("Business Rule Set", "TEST BRE Set", "version", "2", update_modified=False)
        check("rule set version", self.fingerprint({"amount": 1, "rows": [1, 2]}))
        frappe.db.set_value("Business Rule Set", "TEST BRE Set", "modified", "2030-01-01 00:00:00", update_modified=False)
        check("rule set modified", self.fingerprint({"amount": 1, "rows": [1, 2]}))
        frappe.db.set_value("Business Rule", "TEST_RULE", "modified", "2030-01-01 00:00:00", update_modified=False)
        check("evaluated rule modified", self.fingerprint({"amount": 1, "rows": [1, 2]}))

    def test_equivalent_payloads_have_the_same_fingerprint(self):
        self.confirm_set()
        first = self.fingerprint({"b": [1, {"y": 2, "x": Decimal("1.50")}], "a": 1, "d": datetime.date(2026, 1, 5)})
        second = self.fingerprint({"a": 1.0, "b": [1.0, {"x": 1.5, "y": 2}], "d": "2026-01-05"})
        self.assertEqual(first, second)

    def test_payload_values_that_differ_beyond_28_digits_have_different_fingerprints(self):
        self.confirm_set()
        first = self.fingerprint({"rate": Decimal("1.0000000000000000000000000000001")})
        second = self.fingerprint({"rate": Decimal("1.0000000000000000000000000000002")})
        whole = self.fingerprint({"rate": 1})
        self.assertEqual(len({first, second, whole}), 3)
        self.assertEqual(
            self.fingerprint({"rate": Decimal("1.00000000000000000000000000000010")}), first
        )

    def test_non_equivalent_payloads_differ(self):
        self.confirm_set()
        base = self.fingerprint({"qty": 1})
        for other in ({"qty": "1"}, {"qty": True}, {"qty": None}, {}, {"qty": 1, "uom": None}):
            with self.subTest(other):
                self.assertNotEqual(base, self.fingerprint(other))

    def test_only_evaluated_rules_are_covered(self):
        self.confirm_set()
        _make_rule("TEST_SKIPPED", "test.pass", stage=c.PRE_COMMIT)
        _make_rule("TEST_DISABLED", "test.pass", enabled=0)
        _make_set([("TEST_RULE", 10), ("TEST_SKIPPED", 20), ("TEST_DISABLED", 30)], name="TEST BRE Set Two",
                  target="Note")
        frappe.db.set_value("Business Rule Set", "TEST BRE Set", "enabled", 0, update_modified=False)
        before = self.fingerprint(doctype="Note")
        for code in ("TEST_SKIPPED", "TEST_DISABLED"):
            frappe.db.set_value("Business Rule", code, "modified", "2031-05-05 05:05:05", update_modified=False)
        self.assertEqual(self.fingerprint(doctype="Note"), before)
        frappe.db.set_value("Business Rule", "TEST_RULE", "modified", "2031-05-05 05:05:05", update_modified=False)
        self.assertNotEqual(self.fingerprint(doctype="Note"), before)

    def test_erp_evidence_is_not_part_of_the_fingerprint(self):
        _make_rule("TEST_EVIDENCE", "test.evidence")
        _make_set([("TEST_EVIDENCE", 10)])
        first = self.evaluate(data_access=StubData([{"name": "A", "status": "Active"}]))
        second = self.evaluate(data_access=StubData([{"name": "A", "status": "On Hold"}, {"name": "B"}]))
        self.assertNotEqual(first["results"][0]["details"], second["results"][0]["details"])
        self.assertEqual(
            first["confirmation_binding"]["fingerprint"], second["confirmation_binding"]["fingerprint"]
        )

    def test_a_missing_key_makes_the_fingerprint_unavailable_without_writing_anything(self):
        self.confirm_set()
        with mock.patch.dict(frappe.local.conf), mock.patch(
            "frappe.installer.update_site_config", side_effect=AssertionError("site config written")
        ), mock.patch("frappe.utils.password.get_encryption_key", side_effect=AssertionError("key created")):
            frappe.local.conf.pop("encryption_key", None)
            report = self.evaluate()
        section = report["confirmation_binding"]
        self.assertIs(section["available"], False)
        self.assertIsNone(section["fingerprint"])
        self.assertEqual(report["decision"], c.DECISION_NEEDS_CONFIRMATION)


class TestEngineConfirmations(BindingCase):
    def test_a_matching_binding_applies_the_confirmations(self):
        self.confirm_set()
        pending = self.evaluate()
        self.assertEqual(pending["decision"], c.DECISION_NEEDS_CONFIRMATION)
        done = self.evaluate(
            confirmations=["k1"], confirmation_binding=pending["confirmation_binding"]["fingerprint"],
            require_confirmation_binding=True,
        )
        self.assertEqual(done["decision"], c.DECISION_PROCEED)
        self.assertTrue(done["can_proceed"])
        self.assertEqual(done["confirmation_binding"]["confirmations_applied"], ["k1"])
        self.assertIsNone(done["confirmation_binding"]["confirmations_rejected"])
        self.assertEqual(done["confirmation_binding"]["fingerprint"], pending["confirmation_binding"]["fingerprint"])

    def assertRejected(self, report, reason, keys=("k1",)):
        self.assertEqual(report["decision"], c.DECISION_NEEDS_CONFIRMATION)
        self.assertFalse(report["can_proceed"])
        section = report["confirmation_binding"]
        self.assertEqual(section["confirmations_applied"], [])
        self.assertEqual(section["confirmations_rejected"], {"keys": sorted(keys), "reason": reason})
        self.assertTrue(any(reason in note for note in report["notes"]), report["notes"])
        self.assertEqual(report["pending_confirmations"][0]["confirmation_keys"], ["k1"])

    def test_a_missing_binding_discards_the_confirmations(self):
        self.confirm_set()
        for missing in (None, "", "   "):
            with self.subTest(repr(missing)):
                report = self.evaluate(
                    confirmations=["k1"], confirmation_binding=missing, require_confirmation_binding=True
                )
                self.assertRejected(report, binding.REASON_MISSING)

    def test_an_invalid_binding_discards_the_confirmations(self):
        self.confirm_set()
        for invalid in ("garbage", "v1." + "z" * 32, 12345, ["v1." + "a" * 32]):
            with self.subTest(repr(invalid)):
                report = self.evaluate(
                    confirmations=["k1"], confirmation_binding=invalid, require_confirmation_binding=True
                )
                self.assertRejected(report, binding.REASON_INVALID)

    def test_a_stale_binding_discards_the_confirmations(self):
        self.confirm_set()
        fingerprint = self.fingerprint({"amount": 1})
        report = self.evaluate(
            {"amount": 2}, confirmations=["k1"], confirmation_binding=fingerprint, require_confirmation_binding=True
        )
        self.assertRejected(report, binding.REASON_STALE)
        self.assertNotEqual(report["confirmation_binding"]["fingerprint"], fingerprint)

    def test_a_confirmation_for_one_long_value_is_not_honored_for_a_slightly_different_one(self):
        self.confirm_set()
        confirmed = {"rate": Decimal("83.250000000000000000000000000001")}
        fingerprint = self.fingerprint(confirmed)
        self.assertTrue(
            self.evaluate(confirmed, confirmations=["k1"], confirmation_binding=fingerprint,
                          require_confirmation_binding=True)["can_proceed"]
        )
        other = {"rate": Decimal("83.250000000000000000000000000002")}
        report = self.evaluate(other, confirmations=["k1"], confirmation_binding=fingerprint,
                               require_confirmation_binding=True)
        self.assertRejected(report, binding.REASON_STALE)

    def test_the_fresh_fingerprint_in_a_rejection_can_be_used_after_the_user_confirms_again(self):
        self.confirm_set()
        old = self.fingerprint({"amount": 1})
        rejected = self.evaluate(
            {"amount": 2}, confirmations=["k1"], confirmation_binding=old, require_confirmation_binding=True
        )
        fresh = rejected["confirmation_binding"]["fingerprint"]
        accepted = self.evaluate(
            {"amount": 2}, confirmations=["k1"], confirmation_binding=fresh, require_confirmation_binding=True
        )
        self.assertTrue(accepted["can_proceed"])

    def test_a_binding_from_another_stage_user_or_rule_set_state_is_stale(self):
        _make_rule("TEST_RULE", "test.needs_confirmation")
        _make_rule("TEST_COMMIT", "test.needs_confirmation", stage=c.PRE_COMMIT)
        _make_set([("TEST_RULE", 10), ("TEST_COMMIT", 20)])
        _make_set([("TEST_RULE", 10)], name="TEST BRE Other", target="Note")
        fingerprint = self.fingerprint()

        def attempt(**kwargs):
            return self.evaluate(
                confirmations=["k1"], confirmation_binding=fingerprint, require_confirmation_binding=True, **kwargs
            )

        self.assertEqual(attempt(stage=c.PRE_COMMIT)["confirmation_binding"]["confirmations_rejected"]["reason"],
                         binding.REASON_STALE)
        self.assertEqual(attempt(doctype="Note")["confirmation_binding"]["confirmations_rejected"]["reason"],
                         binding.REASON_STALE)
        frappe.set_user("Guest")
        self.assertEqual(attempt()["confirmation_binding"]["confirmations_rejected"]["reason"], binding.REASON_STALE)
        frappe.set_user("Administrator")
        frappe.db.set_value("Business Rule Set", "TEST BRE Set", "modified", "2030-01-01 00:00:00", update_modified=False)
        self.assertEqual(attempt()["confirmation_binding"]["confirmations_rejected"]["reason"], binding.REASON_STALE)

    def test_confirmations_are_never_honored_when_binding_is_unavailable(self):
        self.confirm_set()
        fingerprint = self.fingerprint()
        with mock.patch.dict(frappe.local.conf):
            frappe.local.conf.pop("encryption_key", None)
            for supplied in (fingerprint, None, "garbage"):
                with self.subTest(repr(supplied)):
                    report = self.evaluate(
                        confirmations=["k1"], confirmation_binding=supplied, require_confirmation_binding=True
                    )
                    self.assertRejected(report, binding.REASON_UNAVAILABLE)
                    self.assertIs(report["confirmation_binding"]["available"], False)

    def test_every_supplied_key_is_reported_as_rejected(self):
        self.confirm_set()
        report = self.evaluate(confirmations=["k2", "k1", "k3"], require_confirmation_binding=True)
        self.assertEqual(report["confirmation_binding"]["confirmations_rejected"]["keys"], ["k1", "k2", "k3"])

    def test_no_confirmations_means_nothing_to_reject(self):
        self.confirm_set()
        report = self.evaluate(require_confirmation_binding=True)
        self.assertIsNone(report["confirmation_binding"]["confirmations_rejected"])
        self.assertIs(report["confirmation_binding"]["required"], True)
        self.assertEqual(self.evaluate(confirmations=[], require_confirmation_binding=True)["decision"],
                         c.DECISION_NEEDS_CONFIRMATION)

    def test_python_callers_keep_the_legacy_behaviour_without_a_binding(self):
        self.confirm_set()
        report = self.evaluate(confirmations=["k1"])
        self.assertEqual(report["decision"], c.DECISION_PROCEED)
        self.assertEqual(report["confirmation_binding"]["confirmations_applied"], ["k1"])
        self.assertIs(report["confirmation_binding"]["required"], False)
        # Mapping form still works, and only True values count.
        self.assertEqual(self.evaluate(confirmations={"k1": True})["decision"], c.DECISION_PROCEED)
        self.assertEqual(self.evaluate(confirmations={"k1": "yes"})["decision"], c.DECISION_NEEDS_CONFIRMATION)

    def test_python_callers_that_supply_a_binding_must_match(self):
        self.confirm_set()
        good = self.fingerprint({"amount": 1})
        self.assertEqual(
            self.evaluate({"amount": 1}, confirmations=["k1"], confirmation_binding=good)["decision"],
            c.DECISION_PROCEED,
        )
        report = self.evaluate({"amount": 2}, confirmations=["k1"], confirmation_binding=good)
        self.assertRejected(report, binding.REASON_STALE)

    def test_the_comparison_uses_compare_digest(self):
        self.confirm_set()
        fingerprint = self.fingerprint()
        with mock.patch.object(binding.hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            self.evaluate(confirmations=["k1"], confirmation_binding=fingerprint, require_confirmation_binding=True)
        compare.assert_called_once()

    def test_only_rules_that_asked_for_a_key_use_it(self):
        _make_rule("TEST_A", "test.needs_confirmation")
        _make_rule("TEST_B", "test.pass")
        _make_set([("TEST_A", 10), ("TEST_B", 20)])
        fingerprint = self.fingerprint()
        report = self.evaluate(
            confirmations=["k1", "unrequested"], confirmation_binding=fingerprint, require_confirmation_binding=True
        )
        self.assertTrue(report["can_proceed"])
        self.assertEqual(report["confirmation_binding"]["confirmations_applied"], ["k1", "unrequested"])


# ------------------------------------------------------------------------------- tool
class TestToolBindingChecks(ToolCase):
    """The tool double-checks that the engine honored confirmations only for the exact request."""

    def setUp(self):
        super().setUp()
        _make_rule("TEST_RULE", "test.needs_confirmation")
        _make_set([("TEST_RULE", 10)])
        self.fingerprint = self.call()["confirmation_binding"]["fingerprint"]

    def applied(self):
        return self.call(confirmations=["k1"], confirmation_binding=self.fingerprint)

    def test_the_tool_always_requires_binding(self):
        with mock.patch.object(BusinessRulesEngine, "evaluate", wraps=BusinessRulesEngine.evaluate) as spy:
            self.call()
            self.call(confirmations=["k1"], confirmation_binding=self.fingerprint)
        for call in spy.call_args_list:
            self.assertIs(call.kwargs["require_confirmation_binding"], True)
        self.assertEqual(spy.call_args.kwargs["confirmation_binding"], self.fingerprint)

    def test_an_honest_flow_passes_the_checks(self):
        report = self.applied()
        self.assertEqual(report["decision"], c.DECISION_PROCEED)
        self.assertEqual(report, self.direct(confirmations=["k1"], confirmation_binding=self.fingerprint))

    def test_the_tool_refuses_a_report_that_breaks_the_binding_rules(self):
        good = self.applied()
        section = good["confirmation_binding"]
        other = "v1." + "f" * 32

        def altered(**changes):
            return {**good, "confirmation_binding": {**section, **changes}}

        cases = {
            "section missing": {k: v for k, v in good.items() if k != "confirmation_binding"},
            "section None on a normal report": {**good, "confirmation_binding": None},
            "section not a mapping": {**good, "confirmation_binding": "x"},
            "extra key": altered(extra=1),
            "missing key": {**good, "confirmation_binding": {k: v for k, v in section.items() if k != "covers"}},
            "malformed fingerprint": altered(fingerprint="nope"),
            "available contradicts": altered(available=False),
            "binding not required": altered(required=False),
            "covers changed": altered(covers=["payload"]),
            "applied key never supplied": altered(confirmations_applied=["k1", "k9"]),
            "applied without unavailable key": altered(available=False, fingerprint=None),
            "applied and rejected": altered(confirmations_rejected={"keys": ["k1"], "reason": "binding_stale"}),
            "applied is not a list": altered(confirmations_applied="k1"),
            "fingerprint differs from the supplied binding": altered(fingerprint=other),
        }
        for label, report in cases.items():
            with self.subTest(label), mock.patch.object(BusinessRulesEngine, "evaluate", return_value=report):
                self.assertFailsClosed(self.applied(), c.ERROR_EVALUATION_FAILED)

    def test_the_tool_refuses_confirmations_applied_without_a_binding(self):
        good = self.applied()
        with mock.patch.object(BusinessRulesEngine, "evaluate", return_value=good):
            self.assertFailsClosed(self.call(confirmations=["k1"]), c.ERROR_EVALUATION_FAILED)
            self.assertFailsClosed(
                self.call(confirmations=["k1"], confirmation_binding="v1." + "0" * 32), c.ERROR_EVALUATION_FAILED
            )

    def test_the_tool_refuses_rejections_that_do_not_account_for_the_confirmations(self):
        rejected = self.call(confirmations=["k1"])
        section = rejected["confirmation_binding"]
        self.assertEqual(section["confirmations_rejected"]["reason"], binding.REASON_MISSING)
        cases = {
            "unknown reason": {**section, "confirmations_rejected": {"keys": ["k1"], "reason": "because"}},
            "wrong keys": {**section, "confirmations_rejected": {"keys": ["k2"], "reason": "binding_stale"}},
            "extra field": {**section, "confirmations_rejected": {"keys": ["k1"], "reason": "binding_stale", "x": 1}},
            "neither applied nor rejected": {**section, "confirmations_rejected": None},
        }
        for label, altered in cases.items():
            with self.subTest(label), mock.patch.object(
                BusinessRulesEngine, "evaluate", return_value={**rejected, "confirmation_binding": altered}
            ):
                self.assertFailsClosed(self.call(confirmations=["k1"]), c.ERROR_EVALUATION_FAILED)
        with mock.patch.object(BusinessRulesEngine, "evaluate", return_value=rejected):
            self.assertEqual(self.call(confirmations=["k1"]), rejected)

    def test_the_audit_record_keeps_the_binding_and_the_safe_part_of_the_keys(self):
        if not frappe.db.exists("DocType", "Assistant Audit Log"):
            self.skipTest("FAC Assistant Audit Log is not installed on this site")
        before = frappe.db.count("Assistant Audit Log", {"tool_name": "evaluate_business_rules"})
        outcome = self.tool._safe_execute(
            self.arguments(confirmations=["k1", f"free_item:{SENTINEL}"], confirmation_binding=self.fingerprint)
        )
        self.assertTrue(outcome["success"])
        rows = frappe.get_all(
            "Assistant Audit Log", filters={"tool_name": "evaluate_business_rules"},
            fields=["input_data", "output_data"], order_by="creation asc",
        )
        self.assertEqual(len(rows), before + 1)
        stored = json.loads(rows[-1].input_data)
        self.assertEqual(stored["confirmation_binding"], self.fingerprint)
        self.assertEqual(stored["confirmations"], ["k1", "free_item:*"])
        self.assertNotIn(SENTINEL, json.dumps(rows[-1], default=str))


if __name__ == "__main__":
    unittest.main()
