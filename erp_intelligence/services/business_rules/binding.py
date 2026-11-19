"""Confirmation binding: tie a user's confirmations to the exact request they saw.

A confirmation key such as ``exchange_rate_confirmed`` says nothing about the value
that was confirmed. Without binding, a confirmation given for one payload is honored
for any other payload. Binding closes that: every report carries a fingerprint of the
whole evaluation context, and confirmations are honored only when the caller returns
the fingerprint of the request they are used with.

The fingerprint is ``v1.`` plus the first 128 bits of
``HMAC-SHA256(derived_key, canonical_json(context))``.

* The context covers the binding version, the session user, DocType, operation,
  stage, the rule set (name, version, modified), the modified timestamp of every
  rule that is evaluated, the payload, ``execution_options`` and
  ``execution_result``. It does not cover ERP evidence read during the evaluation
  (supplier status, prices and so on). That data changes between calls, so the
  caller must evaluate again at PRE_COMMIT.
* The HMAC is keyed, so the fingerprint reveals nothing about the payload and a
  reader of a log cannot test guesses against it. Nothing is stored: the engine
  recomputes the fingerprint on every call.
* The key is derived from the site's ``encryption_key`` with a fixed context
  string, so the encryption key itself is never used as a MAC key. The key is read
  from ``frappe.conf`` and never created: ``get_encryption_key()`` can write a
  missing key to the site configuration, which a read-only engine must not do. When
  there is no usable key, binding is unavailable and no confirmation is honored.

Binding proves that a confirmation was returned for the same request. It cannot
prove that the user agreed: the caller sits between the user and this code.

Canonical form (version 1)
--------------------------
UTF-8 JSON with no whitespace. Object keys are sorted by code point at every depth.
Array order is kept. Numbers (int, float, ``Decimal``) are written as the shortest
exact decimal with no exponent and no trailing zeros, so ``1``, ``1.0`` and
``Decimal("1.00")`` are the same number. Strings are never equated with numbers, and
booleans are never equated with numbers. ``None`` and a missing key are different. A
``date``, ``datetime``, ``time`` or ``timedelta`` is written as the string the engine
uses elsewhere (``to_json_safe``), so a ``date`` and its ISO string are the same. No
Unicode normalization or trimming is applied: strings that differ are different.
"""

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import frappe

from erp_intelligence.utils.response_utils import to_json_safe

BINDING_VERSION = 1
KEY_CONTEXT = b"erp_intelligence.business_rules.confirmation_binding.v1"
FINGERPRINT_PREFIX = f"v{BINDING_VERSION}."
FINGERPRINT_HEX_LENGTH = 32
FINGERPRINT_PATTERN = re.compile(r"v[0-9]{1,3}\.[0-9a-f]{32}")

# What the fingerprint covers. Reported to the caller so it is never a guess.
COVERS = (
    "version",
    "user",
    "doctype",
    "operation",
    "stage",
    "rule_set",
    "rules",
    "payload",
    "execution_options",
    "execution_result",
)

# Reasons a supplied confirmation is not honored.
REASON_MISSING = "binding_missing"
REASON_INVALID = "binding_invalid"
REASON_STALE = "binding_stale"
REASON_UNAVAILABLE = "binding_unavailable"

_MAX_EXPONENT = 1000
_MAX_DEPTH = 200


class BindingUnavailable(Exception):
    """The fingerprint cannot be computed (no key, or a value that cannot be canonicalized)."""


# ----------------------------------------------------------------- canonical form
def canonicalize(value: Any) -> str:
    """Return the canonical JSON text of ``value``. Raises ``BindingUnavailable``."""
    try:
        return _canonical(value, 0)
    except BindingUnavailable:
        raise
    except (TypeError, ValueError, RecursionError):
        raise BindingUnavailable("value cannot be canonicalized") from None


def _canonical(value: Any, depth: int) -> str:
    if depth > _MAX_DEPTH:
        raise BindingUnavailable("value is nested too deeply")
    if value is None:
        return "null"
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime, date, time, timedelta)):
        return json.dumps(to_json_safe(value), ensure_ascii=False)
    if isinstance(value, Mapping):
        items = []
        for key in value:
            if not isinstance(key, str):
                raise BindingUnavailable("object keys must be strings")
            items.append(key)
        items.sort()
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _canonical(value[key], depth + 1) for key in items
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(item, depth + 1) for item in value) + "]"
    raise BindingUnavailable(f"unsupported value type {type(value).__name__}")


def _number(value: int | float | Decimal) -> str:
    """The shortest exact decimal text of a number. ``1``, ``1.0`` and ``Decimal("1.00")`` agree.

    The text is built from the digits and exponent of the value, so it is exact for
    any number of digits. ``Decimal.normalize()`` is not used: it rounds to the
    active context precision (28 digits by default), which would make two different
    values canonicalize identically. ``Decimal(int)``, ``Decimal(repr(float))``,
    ``as_tuple()``, ``adjusted()`` and comparison with zero are all exact and do not
    read the context.
    """
    number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    if not number.is_finite():
        raise BindingUnavailable("non-finite numbers cannot be bound")
    if number == 0:
        return "0"
    if abs(number.adjusted()) > _MAX_EXPONENT:
        raise BindingUnavailable("number is out of range")

    sign, digit_tuple, exponent = number.as_tuple()
    digits = list(digit_tuple)
    while digits[-1] == 0:  # trailing zeros carry no value: 1.00 is 1, 100 is 1E+2
        digits.pop()
        exponent += 1
    text = "".join(map(str, digits))
    if exponent >= 0:
        text += "0" * exponent
    elif -exponent >= len(text):
        text = "0." + "0" * (-exponent - len(text)) + text
    else:
        text = text[:exponent] + "." + text[exponent:]
    return "-" + text if sign else text


def to_canonical_bytes(value: Any) -> bytes:
    """The canonical text as UTF-8."""
    try:
        return canonicalize(value).encode("utf-8")
    except UnicodeEncodeError:
        raise BindingUnavailable("value is not valid UTF-8 text") from None


# ------------------------------------------------------------------------- key
def derive_key() -> bytes:
    """Return the binding key, derived from the site's existing ``encryption_key``.

    Raises ``BindingUnavailable`` when the site has no usable key. The key is read
    from ``frappe.conf`` only: it is never generated or written.
    """
    try:
        secret = frappe.conf.get("encryption_key")
    except Exception:  # noqa: BLE001 - no site context
        secret = None
    if not isinstance(secret, str) or not secret.strip():
        raise BindingUnavailable("the site has no encryption_key")
    return hmac.new(secret.encode("utf-8"), KEY_CONTEXT, hashlib.sha256).digest()


# ------------------------------------------------------------------ fingerprint
def build_context(
    *,
    user: str,
    doctype: str,
    operation: str,
    stage: str,
    rule_set: Mapping[str, Any],
    rules: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
    execution_options: Mapping[str, Any] | None,
    execution_result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The evaluation context that a fingerprint covers."""
    return {
        "version": BINDING_VERSION,
        "user": user,
        "doctype": doctype,
        "operation": operation,
        "stage": stage,
        "rule_set": {
            "name": rule_set.get("name"),
            "version": rule_set.get("version"),
            "modified": rule_set.get("modified"),
        },
        "rules": [{"rule_code": rule.get("rule_code"), "modified": rule.get("modified")} for rule in rules],
        "payload": payload,
        "execution_options": execution_options or {},
        "execution_result": execution_result or {},
    }


def fingerprint(context: Mapping[str, Any]) -> str:
    """Return the fingerprint of ``context``. Raises ``BindingUnavailable``."""
    key = derive_key()
    digest = hmac.new(key, to_canonical_bytes(context), hashlib.sha256).hexdigest()
    return FINGERPRINT_PREFIX + digest[:FINGERPRINT_HEX_LENGTH]


def check(supplied: Any, expected: str | None) -> str | None:
    """Compare a supplied binding with the fingerprint of the current request.

    Returns None when they match, otherwise the reason it is not honored.
    """
    if expected is None:
        return REASON_UNAVAILABLE
    if supplied is None or (isinstance(supplied, str) and not supplied.strip()):
        return REASON_MISSING
    if not isinstance(supplied, str) or not FINGERPRINT_PATTERN.fullmatch(supplied):
        return REASON_INVALID
    if not hmac.compare_digest(supplied.encode("ascii"), expected.encode("ascii")):
        return REASON_STALE
    return None
