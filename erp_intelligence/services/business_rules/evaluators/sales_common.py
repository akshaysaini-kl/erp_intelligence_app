"""Helpers shared by the Sales and Fulfilment evaluators.

The Sales Order, Pick List, Delivery Note and Sales Invoice evaluators implement the
rules of SALES_ORDER_TO_DELIVERY_GUIDE.md. This module holds what more than one of
them needs, so each rule stays one small function. Nothing here names a site value
(tax templates, accounts, warehouses, state codes): those are rule parameters.

Status vocabulary, as in the Purchase Order evaluators:

* FAIL: a value that was supplied violates the rule.
* NEEDS_INPUT: a value is missing or ambiguous, or an earlier dependency is unresolved.
* NEEDS_CONFIRMATION: acceptable only if the user explicitly confirms.
* WARNING: allowed, but the user must be told.

A result that says a document is consistent with the documented rules never says it
is GST compliant. ERPNext does not validate the GST tax type, the place of supply or
missing addresses on these documents (guide, section 8), so no evaluator can claim it.
"""
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from erp_intelligence.services.business_rules.context import EvaluationContext
from erp_intelligence.services.business_rules.errors import RuleDataError
from erp_intelligence.services.business_rules.evaluators.common import (
    Findings,
    is_blank,
    same_number,
    to_decimal,
)

GUIDE = "SALES_ORDER_TO_DELIVERY_GUIDE.md"
MAX_CANDIDATES = 10
GST_NOT_VERIFIED = (
    "This checks consistency with the documented rules only. It is not GST compliance "
    "verification: ERPNext does not validate the tax type, place of supply or addresses."
)


class SalesFindings(Findings):
    """``Findings`` that also says whether a failure was recorded, without private access."""

    @property
    def has_failures(self) -> bool:
        return bool(self._failures)


def evidence(tag: str, section: str, note: str) -> dict[str, str]:
    """Evidence note. ``tag`` follows the guide: V verified, C source code, H history, U unverified."""
    return {"source": GUIDE, "section": section, "tag": tag, "note": note}


def truthy(value: Any) -> bool:
    number = to_decimal(value)
    return bool(number) if number is not None else bool(value)


def label(table: str, index: int) -> str:
    return f"{table}[{index}]"


def not_applicable(context: EvaluationContext, reason: str) -> dict[str, Any]:
    """A PASS that says the rule does not apply to this payload. It is not evidence of a check."""
    return context.passed(f"Not applicable: {reason}", {"applicable": False, "reason": reason})


def is_return(context: EvaluationContext) -> bool:
    return truthy(context.payload.get("is_return"))


def payload_rows(
    context: EvaluationContext, table: str, noun: str
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    rows = context.payload.get(table)
    if not isinstance(rows, list) or not rows or not all(isinstance(row, Mapping) for row in rows):
        return None, context.needs_input(f"The {noun} needs at least one {table} row.", [table])
    return [dict(row) for row in rows], None


def state_code(place_of_supply: Any) -> str | None:
    """The two-digit state code of a place of supply such as ``29-Karnataka``."""
    if not isinstance(place_of_supply, str):
        return None
    code = place_of_supply.strip()[:2]
    return code if len(code) == 2 and code.isdigit() else None


def split_serials(value: Any) -> list[str]:
    """Serial numbers of a ``serial_no`` text (one per line), in order, without blanks."""
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.replace(",", "\n").splitlines() if part.strip()]


def row_serials(context: EvaluationContext, rows: list[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Serial numbers of document rows, keyed by the row's ``name``.

    A row keeps its serial numbers in the ``serial_no`` text, or, when that text is empty,
    in the Serial and Batch Bundle it points to (the standard Pick List screen stores them
    that way). Both are read, so a Pick List made on the screen is compared as correctly as one
    made through the assistant. Each row needs ``name``, ``serial_no`` and
    ``serial_and_batch_bundle``.
    """
    bundles = sorted(
        {
            row["serial_and_batch_bundle"]
            for row in rows
            if not split_serials(row.get("serial_no")) and not is_blank(row.get("serial_and_batch_bundle"))
        }
    )
    entries: dict[str, list[str]] = {}
    if bundles:
        for entry in context.data.fetch_child_records(
            "Serial and Batch Entry", "Serial and Batch Bundle", bundles, ["parent", "serial_no"]
        ):
            if not is_blank(entry.get("serial_no")):
                entries.setdefault(entry["parent"], []).append(entry["serial_no"])
    result: dict[str, list[str]] = {}
    for row in rows:
        listed = split_serials(row.get("serial_no"))
        result[row["name"]] = listed or entries.get(row.get("serial_and_batch_bundle"), [])
    return result


def same_value(actual: Any, expected: Any) -> bool:
    if to_decimal(actual) is not None and to_decimal(expected) is not None:
        return same_number(actual, expected)
    return str(actual if actual is not None else "") == str(expected if expected is not None else "")


# ---------------------------------------------------------------------- customer
def resolve_customer(context: EvaluationContext) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Resolve payload.customer. Only an exact record name resolves.

    Returns ``(state, name, candidates)``; state is ``resolved``, ``missing``,
    ``not_found`` or ``ambiguous``. A near match is never picked automatically.
    """
    value = context.payload.get("customer")
    if not isinstance(value, str) or is_blank(value):
        return "missing", None, []
    exact = context.data.fetch_records("Customer", ["name"], [("name", "=", value)])
    if len(exact) == 1:
        return "resolved", exact[0]["name"], []
    candidates = context.data.fetch_records(
        "Customer",
        ["name", "customer_name"],
        [("customer_name", "like", f"%{value}%")],
        page_length=MAX_CANDIDATES,
        require_complete=False,
    )
    if not candidates:
        return "not_found", None, []
    return "ambiguous", None, candidates


def customer_record(
    context: EvaluationContext, fields: list[str], depends_on: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(record, None)`` or ``(None, result)`` when the customer is not resolved."""
    state, name, _candidates = resolve_customer(context)
    if state != "resolved":
        return None, context.needs_input(
            f"The customer is not resolved ({state}), so this rule cannot be evaluated.",
            ["customer"],
            {"depends_on": depends_on, "resolution": state},
        )
    rows = context.data.fetch_records("Customer", ["name", *fields], [("name", "=", name)])
    if len(rows) != 1:
        raise RuleDataError(f"Customer {name} could not be read back", code="incomplete")
    return rows[0], None


# ------------------------------------------------------------------ sales order
def sales_order_header(
    context: EvaluationContext, name: str, fields: list[str]
) -> dict[str, Any] | None:
    rows = context.data.fetch_records("Sales Order", ["name", *fields], [("name", "=", name)])
    return rows[0] if rows else None


def sales_order_rows(
    context: EvaluationContext, name: str, fields: list[str]
) -> dict[str, dict[str, Any]]:
    """The item rows of one Sales Order keyed by the row id (``name``)."""
    rows = context.data.fetch_child_records(
        "Sales Order Item", "Sales Order", [name], ["parent", "name", *fields]
    )
    return {row["name"]: row for row in rows}


# --------------------------------------------------------------------- amounts
def line_rate(line: Mapping[str, Any]) -> Decimal | None:
    """The net rate of a payload line: price list rate less discount, else ``rate``."""
    price = to_decimal(line.get("price_list_rate"))
    if price is not None:
        discount = to_decimal(line.get("discount_percentage")) or Decimal(0)
        return price * (1 - discount / 100)
    return to_decimal(line.get("rate"))


def line_total(lines: list[Mapping[str, Any]]) -> Decimal | None:
    total = Decimal(0)
    for line in lines:
        qty, rate = to_decimal(line.get("qty")), line_rate(line)
        if qty is None or rate is None:
            return None
        total += qty * rate
    return total


def inclusive_totals(total: Decimal, taxes: list[Mapping[str, Any]]) -> dict[str, Decimal] | None:
    """Expected totals of a tax-inclusive document, or None when a row is not "On Net Total" and inclusive.

    Prices include tax, so ``grand_total`` equals the item total and the tax is the part
    of it that belongs to the tax rows (guide, section 4 step 8).
    """
    rate_sum = Decimal(0)
    for row in taxes:
        rate = to_decimal(row.get("rate"))
        if (
            row.get("charge_type") != "On Net Total"
            or rate is None
            or not truthy(row.get("included_in_print_rate"))
        ):
            return None
        sign = -1 if row.get("add_deduct_tax") == "Deduct" else 1
        rate_sum += sign * rate
    net = total / (1 + rate_sum / 100) if rate_sum else total
    return {
        "total": total,
        "net_total": net,
        "total_taxes_and_charges": total - net,
        "grand_total": total,
        "rounded_total": total.quantize(Decimal(1), rounding=ROUND_HALF_UP),
    }


def normalized_tax_row(row: Mapping[str, Any], fields: list[str]) -> tuple[str, ...]:
    values = []
    for name in fields:
        value = row.get(name)
        if name == "rate":
            number = to_decimal(value)
            values.append(format(number.normalize(), "f") if number is not None else "")
        else:
            values.append("" if value is None else str(value).strip())
    return tuple(values)


def check_tax_row_shape(
    findings: Findings, rows: list[Any], required_included: Any, table: str = "taxes"
) -> None:
    """Every tax row needs a description and an explicit ``included_in_print_rate``."""
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            findings.fail(f"{label(table, index)} is not an object.")
            continue
        if is_blank(row.get("description")):
            findings.fail(f"{label(table, index)} has no description (the server rejects it).")
        included = row.get("included_in_print_rate")
        if is_blank(included):
            findings.fail(
                f"{label(table, index)} must send included_in_print_rate explicitly. "
                f"Prices are tax-inclusive: it must be {required_included}."
            )
        elif not same_number(included, required_included):
            findings.fail(
                f"{label(table, index)} included_in_print_rate must be {required_included}, "
                f"got {included}."
            )


def compare_rows(
    findings: Findings,
    sent: list[Mapping[str, Any]],
    read: list[Mapping[str, Any]],
    fields: list[str],
    table: str,
) -> None:
    """Compare sent rows with the rows read back, by position, on the given fields."""
    if len(read) != len(sent):
        findings.fail(f"{len(read)} {table} row(s) were read back but {len(sent)} were sent.")
    for index, (row_sent, row_read) in enumerate(zip(sent, read)):
        for field in fields:
            if field in row_sent and not same_value(row_read.get(field), row_sent[field]):
                findings.fail(
                    f"{label(table, index)}.{field} is {row_read.get(field)!r} but "
                    f"{row_sent[field]!r} was sent.",
                    row=index + 1,
                )
