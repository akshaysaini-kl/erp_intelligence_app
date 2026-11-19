"""Purchase Order evaluators for the pilot rule set.

Each evaluator implements one atomic responsibility from
PURCHASE_ORDER_CREATION_GUIDE.md. They are written against the standard
EvaluationContext and read ERP data only through ``context.data``.

Site-specific values (tax templates, workflow state names, custom field names,
state codes) are never written here. They are rule parameters, supplied by the
Business Rule's default parameters and the Rule Set assignment's override.

Status vocabulary used by these evaluators:

* FAIL: a value that was supplied violates the rule.
* NEEDS_INPUT: a value is missing, ambiguous, or an earlier dependency (for example
  an unresolved supplier) means the rule cannot be evaluated yet.
* NEEDS_CONFIRMATION: the rule can be satisfied only if the user explicitly confirms.
* WARNING: allowed, but the user must be told.
"""

import datetime
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from frappe.utils import now_datetime

from erp_intelligence.services.business_rules.context import EvaluationContext
from erp_intelligence.services.business_rules.errors import RuleConfigurationError, RuleDataError
from erp_intelligence.services.business_rules.evaluators.common import (
    Findings,
    is_blank,
    parse_iso_date,
    same_number,
    to_decimal,
)
from erp_intelligence.services.business_rules.registry import EvaluatorRegistry

_GUIDE = "PURCHASE_ORDER_CREATION_GUIDE.md"
_MAX_CANDIDATES = 10
_PRICE_CHUNK = 20


def _evidence(tag: str, section: str, note: str) -> dict[str, str]:
    """Evidence note. ``tag`` follows the guide: V verified, C source code, H history, U unverified."""
    return {"source": _GUIDE, "section": section, "tag": tag, "note": note}


def _truthy(value: Any) -> bool:
    number = to_decimal(value)
    return bool(number) if number is not None else bool(value)


# ----------------------------------------------------------------------- lookups
def _resolve_supplier(context: EvaluationContext) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Resolve payload.supplier. Only an exact record name resolves.

    Returns ``(state, name, candidates)`` where state is ``resolved``, ``missing``,
    ``not_found`` or ``ambiguous``. A near match is never picked automatically.
    """
    value = context.payload.get("supplier")
    if not isinstance(value, str) or is_blank(value):
        return "missing", None, []
    exact = context.data.fetch_records("Supplier", ["name"], [("name", "=", value)])
    if len(exact) == 1:
        return "resolved", exact[0]["name"], []
    candidates = context.data.fetch_records(
        "Supplier",
        ["name", "supplier_name"],
        [("supplier_name", "like", f"%{value}%")],
        page_length=_MAX_CANDIDATES,
        require_complete=False,
    )
    if not candidates:
        return "not_found", None, []
    return "ambiguous", None, candidates


def _supplier_record(
    context: EvaluationContext,
    fields: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(record, None)`` or ``(None, result)`` when the supplier is not resolved."""
    state, name, _candidates = _resolve_supplier(context)
    if state != "resolved":
        return None, context.needs_input(
            f"The supplier is not resolved ({state}), so this rule cannot be evaluated.",
            ["supplier"],
            {"depends_on": "purchase_order.supplier_resolves_exactly_one", "resolution": state},
        )
    rows = context.data.fetch_records("Supplier", ["name", *fields], [("name", "=", name)])
    if len(rows) != 1:
        raise RuleDataError(f"Supplier {name} could not be read back", code="incomplete")
    return rows[0], None


def _payload_items(context: EvaluationContext) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    items = context.payload.get("items")
    if not isinstance(items, list) or not items or not all(isinstance(row, Mapping) for row in items):
        return None, context.needs_input(
            "The Purchase Order needs at least one item row.", ["items"]
        )
    return [dict(row) for row in items], None


def _item_codes(items: list[dict[str, Any]]) -> list[str]:
    return [row["item_code"] for row in items if isinstance(row.get("item_code"), str) and row["item_code"]]


def _items_by_code(
    context: EvaluationContext,
    items: list[dict[str, Any]],
    fields: list[str],
) -> dict[str, dict[str, Any]]:
    rows = context.data.fetch_by_names("Item", _item_codes(items), ["name", *fields])
    return {row["name"]: row for row in rows}


def _item_dependency(findings: Findings, index: int, line: Mapping[str, Any], found: Mapping[str, Any]) -> bool:
    """Record a NEEDS_INPUT finding if the line's item is unresolved. Return True if it is."""
    code = line.get("item_code")
    if is_blank(code):
        findings.need(f"items[{index}].item_code", f"Row {index + 1} has no item_code.")
        return True
    if code not in found:
        findings.need(
            f"items[{index}].item_code",
            f"Item '{code}' is not resolved, so this rule cannot be evaluated for row {index + 1}.",
            depends_on="purchase_order.items_resolve_exactly_one",
        )
        return True
    return False


def _line_label(index: int) -> str:
    return f"items[{index}]"


# --------------------------------------------------------------------- 1. supplier
def supplier_resolves_exactly_one(context: EvaluationContext) -> dict[str, Any]:
    state, name, candidates = _resolve_supplier(context)
    supplied = context.payload.get("supplier")
    if state == "resolved":
        return context.passed("Supplier resolves to exactly one record.", {"supplier": name})
    if state == "missing":
        return context.needs_input("The supplier is required.", ["supplier"])
    if state == "not_found":
        return context.failed(f"No Supplier matches '{supplied}'.", {"supplier": supplied})
    return context.needs_input(
        f"'{supplied}' is not an exact Supplier ID. Choose one candidate and use its ID.",
        ["supplier"],
        {
            "supplier": supplied,
            "candidates": [
                {"name": row["name"], "supplier_name": row.get("supplier_name")}
                for row in candidates
            ],
        },
    )


def supplier_enabled(context: EvaluationContext) -> dict[str, Any]:
    record, dependency = _supplier_record(context, ["disabled"])
    if dependency:
        return dependency
    if _truthy(record["disabled"]):
        return context.failed(
            f"Supplier {record['name']} is disabled.", {"supplier": record["name"]}
        )
    return context.passed(f"Supplier {record['name']} is enabled.", {"supplier": record["name"]})


def supplier_not_on_hold(context: EvaluationContext) -> dict[str, Any]:
    record, dependency = _supplier_record(
        context, ["on_hold", "hold_type", "release_date", "prevent_pos"]
    )
    if dependency:
        return dependency
    details = {
        "supplier": record["name"],
        "on_hold": _truthy(record["on_hold"]),
        "hold_type": record["hold_type"],
        "release_date": record["release_date"],
        "prevent_pos": _truthy(record["prevent_pos"]),
        "evidence": _evidence(
            "U",
            "7 step 2 / 13",
            "ERPNext enforcement of Supplier on_hold and prevent_pos when a Purchase Order is "
            "created was not verified. This rule stops conservatively on either flag.",
        ),
    }
    if details["prevent_pos"] or details["on_hold"]:
        return context.failed(
            f"Supplier {record['name']} is on hold or blocked for purchase orders.", details
        )
    return context.passed(f"Supplier {record['name']} has no hold or PO block.", details)


def supplier_price_list_available(context: EvaluationContext) -> dict[str, Any]:
    record, dependency = _supplier_record(context, ["default_price_list"])
    if dependency:
        return dependency
    expected = record["default_price_list"] or None
    chosen = context.payload.get("buying_price_list")
    details = {
        "supplier": record["name"],
        "supplier_default_price_list": expected,
        "buying_price_list": chosen,
    }
    findings = Findings()
    if is_blank(chosen):
        hint = (
            f"The supplier's price list is '{expected}'."
            if expected
            else "The supplier has no default price list; ask which one to use."
        )
        findings.need("buying_price_list", f"buying_price_list is required. {hint}")
        return findings.result(context, "", details=details)

    key = "buying_price_list_override"
    if chosen != expected:
        reason = (
            "The supplier has no default price list."
            if not expected
            else f"It differs from the supplier's price list '{expected}'."
        )
        if context.is_confirmed(key):
            findings.warn(f"Price list '{chosen}' was confirmed by the user. {reason}")
        else:
            findings.confirm(key, f"Confirm using price list '{chosen}'. {reason}")

    rows = context.data.fetch_records(
        "Price List", ["name", "enabled", "buying", "currency"], [("name", "=", chosen)]
    )
    if not rows:
        findings.fail(f"Price List '{chosen}' does not exist or is not readable.")
    else:
        details["price_list_currency"] = rows[0]["currency"]
        if not _truthy(rows[0]["enabled"]):
            findings.fail(f"Price List '{chosen}' is disabled.")
        if not _truthy(rows[0]["buying"]):
            findings.fail(f"Price List '{chosen}' is not a buying price list.")
    return findings.result(context, f"Price List '{chosen}' is available for this supplier.", details=details)


# ----------------------------------------------------------------------- 2. items
def items_resolve_exactly_one(context: EvaluationContext) -> dict[str, Any]:
    items, dependency = _payload_items(context)
    if dependency:
        return dependency
    found = _items_by_code(context, items, ["item_name"])
    findings = Findings()
    for index, line in enumerate(items):
        code = line.get("item_code")
        if is_blank(code) or not isinstance(code, str):
            findings.need(f"{_line_label(index)}.item_code", f"Row {index + 1} has no item_code.")
        elif code not in found:
            candidates = context.data.fetch_records(
                "Item",
                ["name", "item_name"],
                [("item_name", "like", f"%{code}%")],
                page_length=_MAX_CANDIDATES,
                require_complete=False,
            )
            if candidates:
                findings.need(
                    f"{_line_label(index)}.item_code",
                    f"'{code}' is not an exact Item code. Choose one candidate.",
                    candidates=[
                        {"name": row["name"], "item_name": row.get("item_name")} for row in candidates
                    ],
                )
            else:
                findings.fail(f"No Item matches '{code}'.", row=index + 1, item_code=code)
    return findings.result(
        context,
        "Every item resolves to exactly one Item.",
        details={"resolved_items": sorted(found)},
    )


def items_enabled_and_purchasable(context: EvaluationContext) -> dict[str, Any]:
    items, dependency = _payload_items(context)
    if dependency:
        return dependency
    found = _items_by_code(context, items, ["disabled", "end_of_life", "is_purchase_item"])
    today = parse_iso_date(context.today)
    findings = Findings()
    for index, line in enumerate(items):
        if _item_dependency(findings, index, line, found):
            continue
        item = found[line["item_code"]]
        code = item["name"]
        if _truthy(item["disabled"]):
            findings.fail(f"Item {code} is disabled.", item_code=code)
        end_of_life = parse_iso_date(item["end_of_life"])
        if end_of_life is not None and end_of_life <= today:
            findings.fail(
                f"Item {code} reached its end of life on {end_of_life.isoformat()}.", item_code=code
            )
        if not _truthy(item["is_purchase_item"]):
            findings.fail(f"Item {code} is not marked as a purchase item.", item_code=code)
    return findings.result(
        context,
        "Every item is enabled and eligible for purchasing.",
        details={
            "evidence": _evidence(
                "C",
                "7 step 5",
                "ERPNext does not enforce is_purchase_item on a Purchase Order, so this rule does.",
            )
        },
    )


def items_linked_to_supplier(context: EvaluationContext) -> dict[str, Any]:
    items, dependency = _payload_items(context)
    if dependency:
        return dependency
    state, supplier, _candidates = _resolve_supplier(context)
    findings = Findings()
    if state != "resolved":
        findings.need(
            "supplier",
            f"The supplier is not resolved ({state}), so item links cannot be checked.",
            depends_on="purchase_order.supplier_resolves_exactly_one",
        )
    found = _items_by_code(context, items, [])
    linked: set[str] = set()
    if state == "resolved" and found:
        rows = context.data.fetch_child_records(
            "Item Supplier", "Item", list(found), ["parent", "supplier"],
            [("supplier", "=", supplier)],
        )
        linked = {row["parent"] for row in rows}
    for index, line in enumerate(items):
        if _item_dependency(findings, index, line, found):
            continue
        if state == "resolved" and line["item_code"] not in linked:
            findings.fail(
                f"Item {line['item_code']} is not linked to supplier {supplier}. "
                "Do not edit the Item to add the link.",
                item_code=line["item_code"],
                supplier=supplier,
            )
    return findings.result(
        context,
        "Every item is linked to the selected supplier.",
        details={
            "supplier": supplier,
            "evidence": _evidence(
                "V",
                "6",
                "The server accepts an item that is not linked to the supplier, so this rule "
                "checks the Item's supplier table. It is read through ERP Intelligence child-table "
                "retrieval (parent_context).",
            ),
        },
    )


def items_quantity_and_uom(context: EvaluationContext) -> dict[str, Any]:
    items, dependency = _payload_items(context)
    if dependency:
        return dependency
    found = _items_by_code(context, items, ["stock_uom", "min_order_qty"])
    conversions: dict[tuple[str, str], Decimal] = {}
    if found:
        for row in context.data.fetch_child_records(
            "UOM Conversion Detail", "Item", list(found), ["parent", "uom", "conversion_factor"]
        ):
            factor = to_decimal(row["conversion_factor"])
            if factor is not None:
                conversions[(row["parent"], row["uom"])] = factor
    used_uoms = sorted({row["uom"] for row in items if isinstance(row.get("uom"), str) and row["uom"]})
    uom_master = {
        row["name"]: row
        for row in context.data.fetch_by_names("UOM", used_uoms, ["name", "must_be_whole_number"])
    }

    findings = Findings()
    stock_qty: dict[str, Decimal] = {}
    for index, line in enumerate(items):
        label = _line_label(index)
        if _item_dependency(findings, index, line, found):
            continue
        item = found[line["item_code"]]
        code = item["name"]

        qty = to_decimal(line.get("qty"))
        if is_blank(line.get("qty")):
            findings.need(f"{label}.qty", f"Row {index + 1} ({code}) has no qty.")
            continue
        if qty is None or qty <= 0:
            findings.fail(f"Row {index + 1} ({code}) qty must be a positive number.", item_code=code)
            continue

        uom = line.get("uom")
        if is_blank(uom):
            findings.need(f"{label}.uom", f"Row {index + 1} ({code}) has no uom.")
            continue

        factor = Decimal(1)
        supplied_factor = line.get("conversion_factor")
        if uom == item["stock_uom"]:
            if not is_blank(supplied_factor) and not same_number(supplied_factor, 1):
                findings.fail(
                    f"Row {index + 1} ({code}) uses the stock UOM, so conversion_factor must be 1.",
                    item_code=code,
                )
        elif (code, uom) in conversions:
            factor = conversions[(code, uom)]
            if is_blank(supplied_factor):
                findings.need(
                    f"{label}.conversion_factor",
                    f"Row {index + 1} ({code}) uses UOM {uom}; send its conversion_factor {factor}.",
                )
            elif not same_number(supplied_factor, factor):
                findings.fail(
                    f"Row {index + 1} ({code}) conversion_factor {supplied_factor} does not match "
                    f"the Item's {factor} for UOM {uom}.",
                    item_code=code,
                )
        else:
            findings.fail(
                f"UOM {uom} is not defined for Item {code} (stock UOM {item['stock_uom']}). "
                "The server would store a conversion factor of 1.",
                item_code=code,
                uom=uom,
            )
            continue

        master = uom_master.get(uom)
        if master is None:
            findings.fail(f"UOM {uom} does not exist or is not readable.", uom=uom)
        elif _truthy(master["must_be_whole_number"]) and qty != qty.to_integral_value():
            findings.fail(
                f"Row {index + 1} ({code}) qty {qty} must be a whole number for UOM {uom}.",
                item_code=code,
            )
        stock_qty[code] = stock_qty.get(code, Decimal(0)) + qty * factor

    for code, total in stock_qty.items():
        minimum = to_decimal(found[code]["min_order_qty"]) or Decimal(0)
        if minimum > 0 and total < minimum:
            findings.fail(
                f"Item {code}: ordered stock qty {total} is below the minimum order qty {minimum}. "
                "Ask the user to raise the quantity.",
                item_code=code,
                ordered_stock_qty=total,
                min_order_qty=minimum,
            )
    return findings.result(
        context,
        "Quantities satisfy the UOM and minimum order quantity constraints.",
        details={"stock_qty_by_item": stock_qty},
    )


def _eligible_prices(
    rows: list[dict[str, Any]],
    today: datetime.date,
    uom: Any,
) -> list[dict[str, Any]]:
    """Filter in code what the ERP Intelligence filter grammar cannot express (OR on empty)."""
    eligible = []
    for row in rows:
        valid_from = parse_iso_date(row["valid_from"])
        valid_upto = parse_iso_date(row["valid_upto"])
        if valid_from is not None and valid_from > today:
            continue
        if valid_upto is not None and valid_upto < today:
            continue
        if not is_blank(row["uom"]) and row["uom"] != uom:
            continue
        eligible.append(row)
    return eligible


def item_approved_price_resolved(context: EvaluationContext) -> dict[str, Any]:
    items, dependency = _payload_items(context)
    if dependency:
        return dependency
    approval_field = context.required_param("approval_field")
    approved_value = context.required_param("approved_value")
    disabled_field = context.param("disabled_field", None)
    warn_percent = to_decimal(context.param("price_difference_warning_percent", 0)) or Decimal(0)

    findings = Findings()
    state, supplier, _candidates = _resolve_supplier(context)
    price_list = context.payload.get("buying_price_list")
    currency = context.payload.get("currency")
    if state != "resolved":
        findings.need(
            "supplier",
            f"The supplier is not resolved ({state}), so prices cannot be looked up.",
            depends_on="purchase_order.supplier_resolves_exactly_one",
        )
    if is_blank(price_list):
        findings.need("buying_price_list", "buying_price_list is required to look up prices.")
    if is_blank(currency):
        findings.need("currency", "currency is required to compare with the Item Price currency.")
    found = _items_by_code(context, items, [])

    prices: list[dict[str, Any]] = []
    if state == "resolved" and not is_blank(price_list) and found:
        fields = [
            "name", "item_code", "price_list_rate", "currency", "valid_from", "valid_upto",
            "uom", approval_field,
        ]
        codes = list(found)
        for start in range(0, len(codes), _PRICE_CHUNK):
            filters = [
                ("price_list", "=", price_list),
                ("supplier", "=", supplier),
                ("item_code", "in", codes[start : start + _PRICE_CHUNK]),
                (approval_field, "=", approved_value),
            ]
            if disabled_field:
                filters.append((disabled_field, "=", 0))
            prices.extend(context.data.fetch_records("Item Price", fields, filters))

    today = parse_iso_date(context.today)
    resolved: dict[str, Any] = {}
    for index, line in enumerate(items):
        label = _line_label(index)
        if _item_dependency(findings, index, line, found):
            continue
        code = line["item_code"]
        raw_price = line.get("price_list_rate")
        price = to_decimal(raw_price)

        if _truthy(line.get("custom_free_item")):
            key = f"free_item:{code}"
            if context.is_confirmed(key):
                findings.warn(
                    f"Row {index + 1} ({code}) is a free item, confirmed by the user. "
                    "Downstream handling is unverified.",
                    item_code=code,
                )
            else:
                findings.confirm(
                    key,
                    f"Row {index + 1} ({code}) is marked as a free item. Confirm this is intended.",
                    item_code=code,
                )
            continue

        if state != "resolved" or is_blank(price_list) or is_blank(currency):
            continue

        eligible = _eligible_prices([row for row in prices if row["item_code"] == code], today, line.get("uom"))
        approved = None
        if eligible:
            newest = max(parse_iso_date(row["valid_from"]) or datetime.date.min for row in eligible)
            leading = [
                row for row in eligible
                if (parse_iso_date(row["valid_from"]) or datetime.date.min) == newest
            ]
            if len({to_decimal(row["price_list_rate"]) for row in leading}) > 1:
                findings.need(
                    f"{label}.price_list_rate",
                    f"Item {code} has several approved prices with the same start date. "
                    "Choose the price to use.",
                    item_code=code,
                    prices=[row["name"] for row in leading],
                )
                continue
            approved = leading[0]

        if approved is not None and approved["currency"] != currency:
            findings.fail(
                f"Item {code}: approved price currency {approved['currency']} differs from the "
                f"Purchase Order currency {currency}.",
                item_code=code,
            )
            continue

        approved_rate = to_decimal(approved["price_list_rate"]) if approved else None
        if approved is not None:
            resolved[code] = {"item_price": approved["name"], "price_list_rate": approved_rate}

        if is_blank(raw_price):
            hint = (
                f"The approved Item Price is {approved_rate}; send it as price_list_rate."
                if approved is not None
                else "No approved Item Price exists. Ask the user for the price."
            )
            findings.need(f"{label}.price_list_rate", f"Row {index + 1} ({code}) has no price. {hint}")
            continue
        if price is None or price <= 0:
            findings.fail(
                f"Row {index + 1} ({code}) price must be greater than zero.", item_code=code
            )
            continue
        if approved is None:
            findings.warn(
                f"Item {code} has no approved Item Price. The price {price} comes only from the "
                "user's instruction.",
                item_code=code,
            )
        elif approved_rate and price != approved_rate:
            difference = abs(price - approved_rate) / approved_rate * 100
            if difference > warn_percent:
                findings.warn(
                    f"Item {code}: the price {price} differs from the approved Item Price "
                    f"{approved_rate} by {difference.quantize(Decimal('0.01'))}%.",
                    item_code=code,
                    difference_percent=difference,
                )

    return findings.result(
        context,
        "An eligible approved Item Price resolves for every item.",
        details={
            "resolved_prices": resolved,
            "evidence": _evidence(
                "V",
                "7 step 8",
                "Only Approved, enabled Item Prices count. A Pending price is ignored and the line "
                "silently gets rate 0. Validity dates and UOM are evaluated here because the "
                "ERP Intelligence filter grammar has no OR condition. The local site had no "
                "Item Price rows when the guide was verified.",
            ),
        },
    )


# ------------------------------------------------------------- currency and dates
def currency_and_exchange_rate(context: EvaluationContext) -> dict[str, Any]:
    findings = Findings()
    payload = context.payload
    company, price_list = payload.get("company"), payload.get("buying_price_list")
    currency, rate = payload.get("currency"), payload.get("conversion_rate")
    for name, value in (
        ("company", company),
        ("buying_price_list", price_list),
        ("currency", currency),
        ("conversion_rate", rate),
    ):
        if is_blank(value):
            findings.need(name, f"{name} is required.")
    details: dict[str, Any] = {"currency": currency, "conversion_rate": rate}

    list_currency = company_currency = None
    if not is_blank(price_list):
        rows = context.data.fetch_records(
            "Price List", ["name", "currency"], [("name", "=", price_list)]
        )
        if rows:
            list_currency = rows[0]["currency"]
            details["price_list_currency"] = list_currency
        else:
            findings.fail(f"Price List '{price_list}' does not exist or is not readable.")
    if not is_blank(company):
        rows = context.data.fetch_records(
            "Company", ["name", "default_currency"], [("name", "=", company)]
        )
        if rows:
            company_currency = rows[0]["default_currency"]
            details["company_currency"] = company_currency
        else:
            findings.fail(f"Company '{company}' does not exist or is not readable.")

    if list_currency and not is_blank(currency):
        if currency != list_currency:
            findings.fail(
                f"Currency {currency} differs from the price list currency {list_currency}.",
                expected_currency=list_currency,
            )
    elif list_currency:
        details["expected_currency"] = list_currency

    supplier_record, _dependency = _supplier_record(context, ["default_currency"])
    supplier_currency = supplier_record["default_currency"] if supplier_record else None
    if supplier_currency and not is_blank(currency) and supplier_currency != currency:
        key = "supplier_currency_mismatch"
        message = (
            f"The supplier's default currency {supplier_currency} differs from the PO currency "
            f"{currency}. Confirm which to use."
        )
        if context.is_confirmed(key):
            findings.warn(message + " Confirmed by the user.")
        else:
            findings.confirm(key, message, supplier_default_currency=supplier_currency)

    if company_currency and not is_blank(currency) and not is_blank(rate):
        number = to_decimal(rate)
        if number is None or number <= 0:
            findings.fail("conversion_rate must be a positive number.")
        elif currency == company_currency:
            if number != 1:
                findings.fail(
                    f"The PO currency is the company currency {company_currency}, so "
                    "conversion_rate must be 1."
                )
        else:
            suggestion = context.data.fetch_records(
                "Currency Exchange",
                ["from_currency", "to_currency", "exchange_rate", "date"],
                [("from_currency", "=", currency), ("to_currency", "=", company_currency)],
                sort=[("date", "desc")],
                page_length=1,
                require_complete=False,
            )
            if suggestion:
                details["latest_exchange_rate"] = {
                    "exchange_rate": suggestion[0]["exchange_rate"],
                    "date": suggestion[0]["date"],
                }
            key = "exchange_rate_confirmed"
            if context.is_confirmed(key):
                findings.warn(
                    f"Exchange rate {number} {currency}->{company_currency} was confirmed by the user."
                )
            else:
                findings.confirm(
                    key,
                    f"Foreign currency {currency}: the system will not fetch a rate. Confirm "
                    f"conversion_rate {number}. The latest Currency Exchange record, if any, is in "
                    "details and may be stale.",
                )
    details["evidence"] = _evidence(
        "V",
        "7 step 6",
        "create_document defaults currency to the company currency with rate 1 and raises no "
        "error for a foreign-currency supplier. The Currency Exchange lookup call itself was not "
        "verified on the site ([U]).",
    )
    return findings.result(context, "Currency and exchange rate are consistent.", details=details)


def required_by_date_valid(context: EvaluationContext) -> dict[str, Any]:
    findings = Findings()
    payload = context.payload
    today = parse_iso_date(context.today)
    assumptions: list[str] = []

    transaction = today
    raw_transaction = payload.get("transaction_date")
    if is_blank(raw_transaction):
        assumption = f"transaction_date was not supplied; today ({today.isoformat()}) was assumed."
        assumptions.append(assumption)
        findings.warn(assumption + " Send it explicitly.")
    else:
        transaction = parse_iso_date(raw_transaction)
        if transaction is None:
            findings.fail(f"transaction_date '{raw_transaction}' is not a valid YYYY-MM-DD date.")
            transaction = today
        elif transaction < today:
            findings.warn(
                f"transaction_date {transaction.isoformat()} is back-dated. Use today unless the "
                "user gave a date."
            )

    def check(label: str, value: Any) -> datetime.date | None:
        if is_blank(value):
            findings.need(label, f"{label} (required-by date) is required. Do not invent one.")
            return None
        parsed = parse_iso_date(value)
        if parsed is None or not isinstance(value, str) or len(value.strip()) != 10:
            findings.fail(f"{label} '{value}' is not a valid YYYY-MM-DD date.")
            return None
        if parsed < transaction:
            findings.fail(
                f"{label} {parsed.isoformat()} is before the transaction date {transaction.isoformat()}."
            )
        return parsed

    parent_date = check("schedule_date", payload.get("schedule_date"))
    line_dates: list[datetime.date] = []
    items = payload.get("items")
    if isinstance(items, list):
        for index, line in enumerate(items):
            value = line.get("schedule_date") if isinstance(line, Mapping) else None
            parsed = check(f"{_line_label(index)}.schedule_date", value)
            if parsed is not None:
                line_dates.append(parsed)
    else:
        findings.need("items", "The Purchase Order needs at least one item row.")

    if parent_date and line_dates and parent_date != min(line_dates):
        findings.warn(
            f"schedule_date {parent_date.isoformat()} differs from the earliest line date "
            f"{min(line_dates).isoformat()}. The system resets the header to the earliest line date."
        )
    return findings.result(
        context,
        "The required-by date is present and valid.",
        details={"transaction_date": transaction, "assumptions": assumptions},
    )


# --------------------------------------------------------------------------- tax
def _normalized_tax_row(row: Mapping[str, Any], fields: list[str]) -> tuple[str, ...]:
    values = []
    for name in fields:
        value = row.get(name)
        if name == "rate":
            number = to_decimal(value)
            values.append(format(number.normalize(), "f") if number is not None else "")
        else:
            values.append("" if value is None else str(value).strip())
    return tuple(values)


def tax_treatment_resolved(context: EvaluationContext) -> dict[str, Any]:
    taxed = list(context.required_param("taxed_gst_categories"))
    untaxed = list(context.param("untaxed_gst_categories", []))
    compare_fields = list(context.required_param("row_compare_fields"))
    required_included = context.param("required_included_in_print_rate", 0)
    confirm_codes = [str(code) for code in context.param("confirmation_state_codes", [])]
    reverse_templates = list(context.param("reverse_charge_templates", []))

    record, dependency = _supplier_record(context, ["gst_category", "gstin"])
    if dependency:
        return dependency

    payload = context.payload
    template = payload.get("taxes_and_charges")
    rows = payload.get("taxes")
    rows = rows if isinstance(rows, list) else []
    category = record["gst_category"]
    details: dict[str, Any] = {
        "supplier": record["name"],
        "gst_category": category,
        "taxes_and_charges": template,
        "evidence": _evidence(
            "V",
            "7 step 9",
            "The server creates no tax rows and does not check that the tax type matches the "
            "states. included_in_print_rate defaults to 1 when omitted. History: GSTIN state 27 "
            "has mixed treatment, so the user decides. Reverse-charge templates are [U].",
        ),
    }
    findings = Findings()

    if category in untaxed:
        if not is_blank(template) or rows:
            findings.fail(
                f"Supplier category '{category}' takes no tax rows and no tax template, but the "
                "payload has them."
            )
        return findings.result(
            context, f"No tax is expected for supplier category '{category}'.", details=details
        )
    if category not in taxed:
        findings.need(
            "tax_treatment",
            f"Supplier GST category '{category}' has no configured tax treatment. Ask the user.",
        )
        return findings.result(context, "", details=details)

    gstin = record["gstin"] or ""
    supplier_state = gstin[:2]
    if len(gstin) < 2 or not supplier_state.isdigit():
        findings.fail(f"Supplier {record['name']} has no usable GSTIN, so its state is unknown.")
        return findings.result(context, "", details=details)
    place_of_supply = payload.get("place_of_supply")
    if is_blank(place_of_supply):
        findings.need("place_of_supply", "place_of_supply is required to compare states.")
        return findings.result(context, "", details=details)
    delivery_state = str(place_of_supply).strip()[:2]
    details.update(supplier_state_code=supplier_state, place_of_supply_state_code=delivery_state)

    # Two cases leave the choice of template to the user. The state comparison then
    # cannot decide it, so the template is not forced; its rows are still validated.
    treatment_open = supplier_state in confirm_codes
    reverse_charge = not is_blank(template) and template in reverse_templates
    if treatment_open:
        key = "tax_treatment_confirmed"
        message = (
            f"Supplier GSTIN state {supplier_state} has no single historical treatment. "
            "Ask the user which tax treatment to use."
        )
        if context.is_confirmed(key):
            findings.warn(message + " Confirmed by the user.")
        else:
            findings.confirm(key, message)
    if reverse_charge:
        key = "reverse_charge_confirmed"
        if context.is_confirmed(key):
            findings.warn(f"Reverse-charge template '{template}' was confirmed by the user.")
        else:
            findings.confirm(
                key, f"Template '{template}' is a reverse-charge template. Use it only if the user asked."
            )
    if not treatment_open and not reverse_charge:
        same_state = supplier_state == delivery_state
        expected = context.required_param("intra_state_template" if same_state else "inter_state_template")
        details["expected_template"] = expected
        if is_blank(template):
            findings.fail(f"A tax template is required. Expected '{expected}'.")
        elif template != expected:
            findings.fail(
                f"Tax template '{template}' does not match the state comparison "
                f"(supplier {supplier_state}, delivery {delivery_state}). Expected '{expected}'."
            )

    if is_blank(template):
        # A missing template was already reported above unless the user confirmed an
        # open treatment, in which case sending no tax at all is an allowed choice.
        if rows:
            findings.fail("Tax rows were sent without a tax template.")
        return findings.result(context, "Tax treatment is resolved.", details=details)

    template_rows = context.data.fetch_records(
        "Purchase Taxes and Charges Template", ["name", "disabled"], [("name", "=", template)]
    )
    if not template_rows:
        findings.fail(f"Tax template '{template}' does not exist or is not readable.")
        return findings.result(context, "", details=details)
    if _truthy(template_rows[0]["disabled"]):
        findings.fail(f"Tax template '{template}' is disabled.")
    source_rows = context.data.fetch_child_records(
        "Purchase Taxes and Charges", "Purchase Taxes and Charges Template", [template],
        ["parent", *compare_fields, "description"],
    )
    expected_rows = sorted(_normalized_tax_row(row, compare_fields) for row in source_rows)
    actual_rows = sorted(
        _normalized_tax_row(row, compare_fields) for row in rows if isinstance(row, Mapping)
    )
    details.update(template_rows=expected_rows, payload_rows=actual_rows)
    if not source_rows:
        findings.fail(f"Tax template '{template}' has no tax rows.")
    elif actual_rows != expected_rows:
        findings.fail(
            f"The payload tax rows do not match template '{template}' on {', '.join(compare_fields)}."
        )
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            findings.fail(f"taxes[{index}] is not an object.")
            continue
        if is_blank(row.get("description")):
            findings.fail(f"taxes[{index}] has no description (the server rejects it).")
        included = row.get("included_in_print_rate")
        if is_blank(included):
            findings.fail(
                f"taxes[{index}] must send included_in_print_rate explicitly; "
                "omitting it makes the price tax-inclusive."
            )
        elif not same_number(included, required_included):
            findings.fail(
                f"taxes[{index}] included_in_print_rate must be {required_included}, got {included}."
            )
    return findings.result(context, "Tax treatment is resolved and valid.", details=details)


# --------------------------------------------------------------- duplicate guard
def duplicate_guard(context: EvaluationContext) -> dict[str, Any]:
    statuses = list(context.required_param("statuses"))
    status_field = context.required_param("status_field")
    window_hours = to_decimal(context.param("window_hours", 24))
    if window_hours is None or window_hours <= 0:
        raise RuleConfigurationError("window_hours must be a positive number")

    supplier = context.payload.get("supplier")
    if is_blank(supplier):
        return context.needs_input("The supplier is required for the duplicate check.", ["supplier"])

    cutoff = now_datetime() - datetime.timedelta(hours=float(window_hours))
    filters = [
        ("supplier", "=", supplier),
        (status_field, "in", statuses),
        ("creation", ">=", cutoff.strftime("%Y-%m-%d %H:%M:%S")),
    ]
    count = context.data.count_records("Purchase Order", filters)
    details = {
        "supplier": supplier,
        "statuses": statuses,
        "window_hours": window_hours,
        "matching_count": count,
        "evidence": _evidence(
            "V",
            "7 step 11 / 6 rule 5",
            "Counts only Purchase Orders the current user may read. The B2B/B2C channel filter can "
            "hide orders from this user, so zero is not proof that none exist.",
        ),
    }
    if count == 0:
        return context.passed("No recent draft or pending Purchase Order exists for this supplier.", details)

    existing = context.data.fetch_records(
        "Purchase Order",
        ["name", status_field, "creation", "grand_total"],
        filters,
        sort=[("creation", "desc")],
        page_length=_MAX_CANDIDATES,
        require_complete=False,
    )
    details["existing_orders"] = existing
    key = "duplicate_po_review"
    if context.is_confirmed(key):
        return context.warning(
            f"{count} similar Purchase Order(s) exist; the user chose to continue.", details
        )
    return context.needs_confirmation(
        f"{count} Purchase Order(s) for this supplier are in {', '.join(statuses)} and were created "
        f"in the last {window_hours} hours. Show them to the user and ask whether to continue.",
        [key],
        details,
    )


# ------------------------------------------------------------ forbidden payload
def forbidden_payload_fields(context: EvaluationContext) -> dict[str, Any]:
    header_fields = set(context.required_param("forbidden_fields"))
    header_prefixes = tuple(context.param("forbidden_prefixes", []))
    item_fields = set(context.required_param("forbidden_item_fields"))
    item_prefixes = tuple(context.param("forbidden_item_prefixes", []))
    option_flags = list(context.param("forbidden_truthy_options", []))

    def offenders(row: Mapping[str, Any], names: set[str], prefixes: tuple[str, ...]) -> list[str]:
        return sorted(
            key for key in row
            if key in names or (prefixes and isinstance(key, str) and key.startswith(prefixes))
        )

    findings = Findings()
    header = offenders(context.payload, header_fields, header_prefixes)
    if header:
        findings.fail(f"Forbidden header field(s) in the payload: {', '.join(header)}.", fields=header)
    items = context.payload.get("items")
    for index, line in enumerate(items if isinstance(items, list) else []):
        if isinstance(line, Mapping):
            bad = offenders(line, item_fields, item_prefixes)
            if bad:
                findings.fail(
                    f"Forbidden field(s) in {_line_label(index)}: {', '.join(bad)}.",
                    row=index + 1,
                    fields=bad,
                )
    for flag in option_flags:
        if _truthy(context.execution_options.get(flag)):
            findings.fail(
                f"The execution option '{flag}' must not be used: it bypasses the approval "
                "workflow or is not a reliable gate.",
                option=flag,
            )
    return findings.result(
        context,
        "The payload contains no forbidden fields or execution options.",
        details={
            "evidence": _evidence(
                "V",
                "0 / 3.1 / 5.3",
                "submit, docstatus and workflow_state skip the approval workflow and create an "
                "Approved order. validate_only is not a reliable gate.",
            )
        },
    )


# ---------------------------------------------------------------- read-back
def _same(actual: Any, expected: Any) -> bool:
    if to_decimal(actual) is not None and to_decimal(expected) is not None:
        return same_number(actual, expected)
    return str(actual if actual is not None else "") == str(expected if expected is not None else "")


def post_creation_readback(context: EvaluationContext) -> dict[str, Any]:
    name = context.execution_result.get("name")
    if is_blank(name) or not isinstance(name, str):
        return context.needs_input(
            "The created document name is required to read it back.", ["execution_result.name"]
        )
    compare_fields = list(context.param("compare_fields", []))
    initial_state = dict(context.param("expected_initial_state", {}))
    tolerance = to_decimal(context.param("amount_tolerance", "0.01")) or Decimal("0.01")
    taxed = list(context.param("taxed_gst_categories", []))

    header_fields = list(dict.fromkeys([
        "name", *initial_state, *compare_fields, "price_list_currency", "total",
        "total_taxes_and_charges", "grand_total", "rounded_total", "supplier_gstin",
    ]))
    rows = context.data.fetch_records("Purchase Order", header_fields, [("name", "=", name)])
    details: dict[str, Any] = {
        "document": name,
        "evidence": _evidence(
            "V",
            "7 step 13",
            "Read-back must come from the system. A user whose channel role hides the order cannot "
            "read it back although it exists; that is reported as unverified, never as correct. "
            "The Item Price reference check (custom_ip_name) is not implemented.",
        ),
    }
    if not rows:
        key = "readback_unverified_acknowledged"
        details["verification"] = "NOT_VERIFIED"
        message = (
            f"{name} could not be read back by the current user (permission filter, or it does not "
            "exist). Do not create it again. Report the name and say it is unverified."
        )
        if context.is_confirmed(key):
            return context.warning(message + " Acknowledged by the user.", details)
        return context.needs_confirmation(message, [key], details)
    header = rows[0]

    findings = Findings()
    for field, expected in initial_state.items():
        if not _same(header.get(field), expected):
            findings.fail(f"{field} is {header.get(field)!r}, expected {expected!r}.", field=field)
    for field in compare_fields:
        if field in context.payload and not _same(header.get(field), context.payload[field]):
            findings.fail(
                f"{field} is {header.get(field)!r} but {context.payload[field]!r} was sent.",
                field=field,
            )

    items = context.payload.get("items")
    items = items if isinstance(items, list) else []
    item_fields = ["parent", "idx", "item_code", "qty", "uom", "warehouse", "schedule_date", "rate", "price_list_rate"]
    read_items = sorted(
        context.data.fetch_child_records("Purchase Order Item", "Purchase Order", [name], item_fields),
        key=lambda row: row["idx"],
    )
    net = Decimal(0)
    if len(read_items) != len(items):
        findings.fail(f"{len(read_items)} item row(s) were read back but {len(items)} were sent.")
    for index, (sent, read) in enumerate(zip(items, read_items)):
        label = _line_label(index)
        for field in ("item_code", "qty", "uom", "warehouse", "schedule_date"):
            if field in sent and not _same(read.get(field), sent[field]):
                findings.fail(
                    f"{label}.{field} is {read.get(field)!r} but {sent[field]!r} was sent.", row=index + 1
                )
        discount = to_decimal(sent.get("discount_percentage")) or Decimal(0)
        sent_price = to_decimal(sent.get("price_list_rate"))
        qty = to_decimal(sent.get("qty"))
        read_rate = to_decimal(read.get("rate"))
        if sent_price is not None and qty is not None:
            expected_rate = sent_price * (1 - discount / 100)
            net += qty * expected_rate
            if read_rate is None or abs(read_rate - expected_rate) > tolerance:
                findings.fail(
                    f"{label}.rate is {read.get('rate')!r}, expected {expected_rate.quantize(Decimal('0.0001'))}.",
                    row=index + 1,
                )
        if (read_rate is None or read_rate <= 0) and not _truthy(sent.get("custom_free_item")):
            findings.fail(f"{label}.rate is zero or empty.", row=index + 1)

    sent_taxes = [row for row in (context.payload.get("taxes") or []) if isinstance(row, Mapping)]
    read_taxes = sorted(
        context.data.fetch_child_records(
            "Purchase Taxes and Charges", "Purchase Order", [name],
            ["parent", "idx", "account_head", "rate", "tax_amount", "included_in_print_rate"],
        ),
        key=lambda row: row["idx"],
    )
    expected_tax = Decimal(0)
    tax_amounts_known = True
    if len(read_taxes) != len(sent_taxes):
        findings.fail(f"{len(read_taxes)} tax row(s) were read back but {len(sent_taxes)} were sent.")
    for index, (sent, read) in enumerate(zip(sent_taxes, read_taxes)):
        label = f"taxes[{index}]"
        if not _same(read.get("account_head"), sent.get("account_head")) or not _same(
            read.get("rate"), sent.get("rate")
        ):
            findings.fail(f"{label} account_head or rate differs from what was sent.", row=index + 1)
        if _truthy(read.get("included_in_print_rate")):
            findings.fail(f"{label} is tax-inclusive (included_in_print_rate is 1).", row=index + 1)
        if sent.get("charge_type") == "On Net Total" and to_decimal(sent.get("rate")) is not None:
            expected_tax += net * to_decimal(sent["rate"]) / 100
        else:
            tax_amounts_known = False

    if tax_amounts_known:
        grand = net + expected_tax
        expectations = {
            "total": net,
            "total_taxes_and_charges": expected_tax,
            "grand_total": grand,
        }
        for field, expected in expectations.items():
            actual = to_decimal(header.get(field))
            if actual is None or abs(actual - expected) > tolerance:
                findings.fail(
                    f"{field} is {header.get(field)!r}, expected {expected.quantize(Decimal('0.01'))}.",
                    field=field,
                )
        rounded = to_decimal(header.get("rounded_total"))
        if rounded:
            expected_rounded = grand.quantize(Decimal(1), rounding=ROUND_HALF_UP)
            if abs(rounded - expected_rounded) > tolerance:
                findings.fail(
                    f"rounded_total is {header.get('rounded_total')!r}, expected {expected_rounded}.",
                    field="rounded_total",
                )
        details["expected_totals"] = {**expectations, "rounded_total": grand.quantize(Decimal(1), rounding=ROUND_HALF_UP)}
    else:
        findings.warn(
            "A tax row is not 'On Net Total', so expected totals were not computed and the "
            "totals are unverified."
        )

    supplier = context.payload.get("supplier")
    if taxed and not is_blank(supplier):
        supplier_rows = context.data.fetch_records(
            "Supplier", ["name", "gst_category"], [("name", "=", supplier)]
        )
        if (
            supplier_rows
            and supplier_rows[0]["gst_category"] in taxed
            and is_blank(header.get("supplier_gstin"))
        ):
            findings.warn(
                "supplier_gstin is empty on the created order although the supplier is registered. "
                "The supplier may have no usable address."
            )
    details["verification"] = "READ_BACK_COMPARED"
    return findings.result(context, f"{name} was read back and matches what was sent.", details=details)


EVALUATORS: dict[str, Any] = {
    "purchase_order.supplier_resolves_exactly_one": supplier_resolves_exactly_one,
    "purchase_order.supplier_enabled": supplier_enabled,
    "purchase_order.supplier_not_on_hold": supplier_not_on_hold,
    "purchase_order.supplier_price_list_available": supplier_price_list_available,
    "purchase_order.items_resolve_exactly_one": items_resolve_exactly_one,
    "purchase_order.items_enabled_and_purchasable": items_enabled_and_purchasable,
    "purchase_order.items_linked_to_supplier": items_linked_to_supplier,
    "purchase_order.items_quantity_and_uom": items_quantity_and_uom,
    "purchase_order.item_approved_price_resolved": item_approved_price_resolved,
    "purchase_order.currency_and_exchange_rate": currency_and_exchange_rate,
    "purchase_order.required_by_date_valid": required_by_date_valid,
    "purchase_order.tax_treatment_resolved": tax_treatment_resolved,
    "purchase_order.duplicate_guard": duplicate_guard,
    "purchase_order.forbidden_payload_fields": forbidden_payload_fields,
    "purchase_order.post_creation_readback": post_creation_readback,
}


def register_all(registry: EvaluatorRegistry) -> None:
    """Register every Purchase Order evaluator explicitly."""
    for key, evaluator in EVALUATORS.items():
        registry.register(key, evaluator, description=(evaluator.__doc__ or "").strip())
