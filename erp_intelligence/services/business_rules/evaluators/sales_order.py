"""Sales Order evaluators.

Each evaluator implements one atomic responsibility from SALES_ORDER_TO_DELIVERY_GUIDE.md
(section 4, "Stage 1: create the Sales Order"). They run through the standard
EvaluationContext and read ERP data only through ``context.data``.

Many evaluators are shared with the other Sales documents and live in ``sales_shared``
and ``sales_shared_checks``; this module registers them under ``sales_order.*`` keys and
adds the rules that only a Sales Order has: the customer PO reference, the delivery
date, the Item Price, the duplicate PO number and the read-back.

Site-specific values (tax templates, state codes, workflow state names, field names)
are never written here. They are rule parameters.
"""
import datetime
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from erp_intelligence.services.business_rules.context import EvaluationContext
from erp_intelligence.services.business_rules.evaluators import sales_shared as shared
from erp_intelligence.services.business_rules.evaluators import sales_shared_checks as checks
from erp_intelligence.services.business_rules.evaluators.common import (
    Findings,
    is_blank,
    parse_iso_date,
    to_decimal,
)

# The Item Price date and UOM filter is generic. It is reused from the Purchase Order
# evaluators instead of being written a second time.
from erp_intelligence.services.business_rules.evaluators.purchase_order import _eligible_prices
from erp_intelligence.services.business_rules.evaluators.sales_common import (
    evidence,
    inclusive_totals,
    label,
    payload_rows,
    resolve_customer,
    same_value,
    truthy,
)
from erp_intelligence.services.business_rules.registry import EvaluatorRegistry

_MAX_CANDIDATES = 10
_PRICE_CHUNK = 20


# ----------------------------------------------------------------- order reference
def po_reference_present(context: EvaluationContext) -> dict[str, Any]:
    """The customer's PO number and date are mandatory. Ask the user for them."""
    findings = Findings()
    po_no, po_date = context.payload.get("po_no"), context.payload.get("po_date")
    if is_blank(po_no):
        findings.need("po_no", "The customer's purchase order number (po_no) is required. Ask the user.")
    if is_blank(po_date):
        findings.need("po_date", "The date on the customer's purchase order (po_date) is required. Ask the user.")
    elif parse_iso_date(po_date) is None or not isinstance(po_date, str) or len(po_date.strip()) != 10:
        findings.fail(f"po_date '{po_date}' is not a valid YYYY-MM-DD date.")
    return findings.result(
        context,
        "The customer PO number and date are present.",
        details={
            "po_no": po_no,
            "evidence": evidence("V", "4 step 3", "po_no and po_date are mandatory: the system rejects a Sales Order without them."),
        },
    )


def delivery_date_valid(context: EvaluationContext) -> dict[str, Any]:
    """delivery_date is required on the header and every line, and not before the order date."""
    findings = Findings()
    payload = context.payload
    today = parse_iso_date(context.today)
    assumptions: list[str] = []
    transaction = today
    raw_transaction = payload.get("transaction_date")
    if is_blank(raw_transaction):
        text = f"transaction_date was not supplied; today ({today.isoformat()}) was assumed."
        assumptions.append(text)
        findings.warn(text + " Send it explicitly.")
    else:
        transaction = parse_iso_date(raw_transaction)
        if transaction is None:
            findings.fail(f"transaction_date '{raw_transaction}' is not a valid YYYY-MM-DD date.")
            transaction = today
        elif transaction < today:
            findings.warn(
                f"transaction_date {transaction.isoformat()} is back-dated. Use today unless the user gave a date."
            )

    def check(name: str, value: Any) -> None:
        if is_blank(value):
            findings.need(name, f"{name} (delivery date) is required. Do not invent one.")
            return
        parsed = parse_iso_date(value)
        if parsed is None or not isinstance(value, str) or len(value.strip()) != 10:
            findings.fail(f"{name} '{value}' is not a valid YYYY-MM-DD date.")
        elif parsed < transaction:
            findings.fail(
                f"{name} {parsed.isoformat()} is before the order date {transaction.isoformat()}. "
                "The system rejects it. The same day is accepted."
            )

    check("delivery_date", payload.get("delivery_date"))
    items = payload.get("items")
    if isinstance(items, list) and items:
        for index, line in enumerate(items):
            check(f"{label('items', index)}.delivery_date", line.get("delivery_date") if isinstance(line, Mapping) else None)
    else:
        findings.need("items", "The Sales Order needs at least one item row.")
    return findings.result(
        context,
        "The delivery date is present and valid.",
        details={
            "transaction_date": transaction,
            "assumptions": assumptions,
            "evidence": evidence(
                "V",
                "4 step 3",
                "'Please enter Delivery Date' without it; 'Expected Delivery Date should be after "
                "Sales Order Date' when earlier; the same day is accepted.",
            ),
        },
    )


# ------------------------------------------------------------------ item price
def item_price_resolved(context: EvaluationContext) -> dict[str, Any]:
    """Every line needs a price greater than zero, sent as ``price_list_rate``.

    An approved selling Item Price is looked up. The site had none when the guide was
    verified, so the common case is a price given by the user, which is reported as a
    warning. A line with no price saves with rate 0 and no error, so it never passes.
    """
    rows, dependency = payload_rows(context, "items", "Sales Order")
    if dependency:
        return dependency
    approval_field = context.required_param("approval_field")
    approved_value = context.required_param("approved_value")
    disabled_field = context.param("disabled_field", None)
    selling_field = context.param("selling_field", "selling")
    warn_percent = to_decimal(context.param("price_difference_warning_percent", 0)) or Decimal(0)
    price_list = context.payload.get("selling_price_list")
    findings = Findings()
    if is_blank(price_list):
        findings.need("selling_price_list", "selling_price_list is required to look up prices.")
    codes = [row["item_code"] for row in rows if isinstance(row.get("item_code"), str) and row["item_code"]]
    found = {row["name"] for row in context.data.fetch_by_names("Item", codes, ["name"])}
    prices: list[dict[str, Any]] = []
    if not is_blank(price_list) and found:
        fields = ["name", "item_code", "price_list_rate", "currency", "valid_from", "valid_upto", "uom", approval_field]
        ordered = sorted(found)
        for start in range(0, len(ordered), _PRICE_CHUNK):
            filters = [
                ("price_list", "=", price_list),
                (selling_field, "=", 1),
                ("item_code", "in", ordered[start : start + _PRICE_CHUNK]),
                (approval_field, "=", approved_value),
            ]
            if disabled_field:
                filters.append((disabled_field, "=", 0))
            prices.extend(context.data.fetch_records("Item Price", fields, filters))
    today = parse_iso_date(context.today)
    resolved: dict[str, Any] = {}
    for index, line in enumerate(rows):
        tag = label("items", index)
        code = line.get("item_code")
        if code not in found:
            findings.need(
                f"{tag}.item_code",
                f"Item '{code}' is not resolved, so its price cannot be checked for row {index + 1}.",
                depends_on="sales_order.items_resolve_exactly_one",
            )
            continue
        raw_price = line.get("price_list_rate")
        price = to_decimal(raw_price)
        approved = None
        if not is_blank(price_list):
            eligible = _eligible_prices([row for row in prices if row["item_code"] == code], today, line.get("uom"))
            if eligible:
                newest = max(parse_iso_date(row["valid_from"]) or datetime.date.min for row in eligible)
                leading = [
                    row for row in eligible
                    if (parse_iso_date(row["valid_from"]) or datetime.date.min) == newest
                ]
                if len({to_decimal(row["price_list_rate"]) for row in leading}) > 1:
                    findings.need(
                        f"{tag}.price_list_rate",
                        f"Item {code} has several approved prices with the same start date. Choose the price to use.",
                        item_code=code,
                        prices=[row["name"] for row in leading],
                    )
                    continue
                approved = leading[0]
        approved_rate = to_decimal(approved["price_list_rate"]) if approved else None
        if approved is not None:
            resolved[code] = {"item_price": approved["name"], "price_list_rate": approved_rate}
        if is_blank(raw_price):
            hint = (
                f"The approved Item Price is {approved_rate}; send it as price_list_rate."
                if approved is not None
                else "No approved selling Item Price exists. Ask the user for the price."
            )
            findings.need(f"{tag}.price_list_rate", f"Row {index + 1} ({code}) has no price. {hint}")
            continue
        if price is None or price <= 0:
            findings.fail(f"Row {index + 1} ({code}) price must be greater than zero.", item_code=code)
            continue
        if approved is None:
            findings.warn(
                f"Item {code} has no approved selling Item Price. The price {price} comes only from the "
                "user's instruction. Prices are tax-inclusive.",
                item_code=code,
            )
        elif approved_rate and price != approved_rate:
            difference = abs(price - approved_rate) / approved_rate * 100
            if difference > warn_percent:
                findings.warn(
                    f"Item {code}: the price {price} differs from the approved Item Price {approved_rate} "
                    f"by {difference.quantize(Decimal('0.01'))}%.",
                    item_code=code,
                    difference_percent=difference,
                )
    return findings.result(
        context,
        "Every line has a price greater than zero.",
        details={
            "resolved_prices": resolved,
            "evidence": evidence(
                "V",
                "4 step 6",
                "A line with no price saves with rate 0 and an order total of 0 and no error. The "
                "local site had no selling Item Price rows when the guide was verified.",
            ),
        },
    )


# ------------------------------------------------------------- duplicate PO number
def duplicate_po_number(context: EvaluationContext) -> dict[str, Any]:
    """The same customer cannot have two live Sales Orders with the same PO number.

    The system refuses the second one (Selling Settings: one order per customer PO), so
    this reports it early and names the existing order. Counts only orders the current
    user may read: the B2B/B2C channel filter can hide orders, so zero is not proof.
    """
    status_field = context.required_param("docstatus_field")
    live = list(context.required_param("live_docstatuses"))
    customer, po_no = context.payload.get("customer"), context.payload.get("po_no")
    if is_blank(customer) or is_blank(po_no):
        return context.needs_input(
            "The customer and the customer PO number are required for the duplicate check.",
            [name for name, value in (("customer", customer), ("po_no", po_no)) if is_blank(value)],
        )
    filters = [("customer", "=", customer), ("po_no", "=", po_no), (status_field, "in", live)]
    count = context.data.count_records("Sales Order", filters)
    details: dict[str, Any] = {
        "customer": customer,
        "po_no": po_no,
        "matching_count": count,
        "evidence": evidence(
            "V",
            "4 step 3",
            "The system refuses a second live Sales Order with the same customer PO number "
            "('Sales Order 0 already exists against Customer's Purchase Order X', which shows 0 "
            "instead of the order name). Counts only orders this user may read.",
        ),
    }
    if count == 0:
        return context.passed("No live Sales Order exists for this customer and PO number.", details)
    existing = context.data.fetch_records(
        "Sales Order",
        ["name", "workflow_state", "status", "creation"],
        filters,
        sort=[("creation", "desc")],
        page_length=_MAX_CANDIDATES,
        require_complete=False,
    )
    details["existing_orders"] = existing
    return context.failed(
        f"{count} live Sales Order(s) already exist for customer {customer} and PO number {po_no}. "
        "Show the existing order to the user instead of creating another.",
        details,
    )


# ------------------------------------------------------------------ read-back
def post_creation_readback(context: EvaluationContext) -> dict[str, Any]:
    """Read the created Sales Order back and compare it with what was sent."""
    name = context.execution_result.get("name")
    if is_blank(name) or not isinstance(name, str):
        return context.needs_input(
            "The created document name is required to read it back.", ["execution_result.name"]
        )
    compare_fields = list(context.param("compare_fields", []))
    initial = dict(context.param("expected_initial_state", {}))
    tolerance = to_decimal(context.param("amount_tolerance", "0.02")) or Decimal("0.02")
    taxed = list(context.param("taxed_gst_categories", []))
    brand_value = context.param("brand_value", None)
    header_fields = list(
        dict.fromkeys(
            [
                "name", *initial, *compare_fields, "total", "net_total", "total_taxes_and_charges",
                "grand_total", "rounded_total", "gst_category", "billing_address_gstin",
            ]
        )
    )
    rows = context.data.fetch_records("Sales Order", header_fields, [("name", "=", name)])
    details: dict[str, Any] = {
        "document": name,
        "evidence": evidence(
            "V",
            "4 step 10",
            "Read-back must come from the system. A user whose channel role hides the order cannot "
            "read it back although it exists; that is reported as unverified, never as correct.",
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
    for field, expected in initial.items():
        if not same_value(header.get(field), expected):
            findings.fail(f"{field} is {header.get(field)!r}, expected {expected!r}.", field=field)
    for field in compare_fields:
        if field in context.payload and not same_value(header.get(field), context.payload[field]):
            findings.fail(
                f"{field} is {header.get(field)!r} but {context.payload[field]!r} was sent.", field=field
            )
    sent_items = [row for row in (context.payload.get("items") or []) if isinstance(row, Mapping)]
    read_items = sorted(
        context.data.fetch_child_records(
            "Sales Order Item", "Sales Order", [name],
            ["parent", "name", "idx", "item_code", "qty", "uom", "delivery_date", "price_list_rate",
             "rate", "amount", "custom_fulfilled_by"],
        ),
        key=lambda row: row["idx"],
    )
    if len(read_items) != len(sent_items):
        findings.fail(f"{len(read_items)} item row(s) were read back but {len(sent_items)} were sent.")
    expected_lines = []
    brand_rows = []
    for index, (sent, read) in enumerate(zip(sent_items, read_items)):
        tag = label("items", index)
        for field in ("item_code", "qty", "uom", "delivery_date"):
            if field in sent and not same_value(read.get(field), sent[field]):
                findings.fail(f"{tag}.{field} is {read.get(field)!r} but {sent[field]!r} was sent.", row=index + 1)
        sent_price, qty = to_decimal(sent.get("price_list_rate")), to_decimal(sent.get("qty"))
        discount = to_decimal(sent.get("discount_percentage")) or Decimal(0)
        read_rate = to_decimal(read.get("rate"))
        if sent_price is not None:
            expected_rate = sent_price * (1 - discount / 100)
            if read_rate is None or abs(read_rate - expected_rate) > tolerance:
                findings.fail(
                    f"{tag}.rate is {read.get('rate')!r}, expected {expected_rate.quantize(Decimal('0.0001'))}.",
                    row=index + 1,
                )
            expected_lines.append({"qty": qty, "price_list_rate": sent_price, "discount_percentage": discount})
        if read_rate is None or read_rate <= 0:
            findings.fail(f"{tag}.rate is zero or empty.", row=index + 1)
        if brand_value is not None and read.get("custom_fulfilled_by") == brand_value:
            brand_rows.append({"row": index + 1, "item_code": read.get("item_code")})
    if brand_rows:
        findings.warn(
            "Some lines are Brand fulfilled (set from the Item master). Tell the user; do not pick, "
            "deliver or invoice them without asking.",
            brand_rows=brand_rows,
        )
    sent_taxes = [row for row in (context.payload.get("taxes") or []) if isinstance(row, Mapping)]
    read_taxes = sorted(
        context.data.fetch_child_records(
            "Sales Taxes and Charges", "Sales Order", [name],
            ["parent", "idx", "account_head", "rate", "tax_amount", "included_in_print_rate"],
        ),
        key=lambda row: row["idx"],
    )
    if len(read_taxes) != len(sent_taxes):
        findings.fail(f"{len(read_taxes)} tax row(s) were read back but {len(sent_taxes)} were sent.")
    for index, (sent, read) in enumerate(zip(sent_taxes, read_taxes)):
        tag = label("taxes", index)
        if not same_value(read.get("account_head"), sent.get("account_head")) or not same_value(
            read.get("rate"), sent.get("rate")
        ):
            findings.fail(f"{tag} account_head or rate differs from what was sent.", row=index + 1)
        if not truthy(read.get("included_in_print_rate")):
            findings.fail(f"{tag} is not tax-inclusive (included_in_print_rate is 0).", row=index + 1)
    total = Decimal(0)
    computable = len(expected_lines) == len(sent_items) and bool(sent_items)
    for line in expected_lines:
        if line["qty"] is None:
            computable = False
            break
        total += line["qty"] * line["price_list_rate"] * (1 - line["discount_percentage"] / 100)
    expected = inclusive_totals(total, sent_taxes) if computable else None
    if expected is not None:
        for field in ("total", "total_taxes_and_charges", "grand_total"):
            actual = to_decimal(header.get(field))
            if actual is None or abs(actual - expected[field]) > tolerance:
                findings.fail(
                    f"{field} is {header.get(field)!r}, expected {expected[field].quantize(Decimal('0.01'))} "
                    "(prices are tax-inclusive).",
                    field=field,
                )
        rounded = to_decimal(header.get("rounded_total"))
        if rounded and abs(rounded - expected["rounded_total"]) > tolerance:
            findings.fail(
                f"rounded_total is {header.get('rounded_total')!r}, expected {expected['rounded_total']}.",
                field="rounded_total",
            )
        details["expected_totals"] = expected
    else:
        findings.warn(
            "Expected totals were not computed (a tax row is not an inclusive 'On Net Total', or a "
            "line has no price). The totals are unverified."
        )
    state, customer, _candidates = resolve_customer(context)
    if taxed and state == "resolved":
        record = context.data.fetch_records("Customer", ["name", "gst_category"], [("name", "=", customer)])
        if record and record[0]["gst_category"] in taxed and (
            is_blank(header.get("gst_category")) or is_blank(header.get("billing_address_gstin"))
        ):
            findings.warn(
                "gst_category or billing_address_gstin is empty on the created order although the "
                "customer is registered. The billing address may be missing or wrong."
            )
    details["line_row_names"] = [{"idx": row["idx"], "item_code": row["item_code"], "name": row["name"]} for row in read_items]
    details["verification"] = "READ_BACK_COMPARED"
    return findings.result(context, f"{name} was read back and matches what was sent.", details=details)


EVALUATORS: dict[str, Any] = {
    "sales_order.customer_resolves_exactly_one": checks.customer_resolves_exactly_one,
    "sales_order.customer_enabled": checks.customer_enabled,
    "sales_order.channel_allowed": checks.channel_allowed,
    "sales_order.customer_not_internal": checks.customer_not_internal,
    "sales_order.selling_price_list_for_customer": checks.selling_price_list_for_customer,
    "sales_order.customer_addresses_resolved": shared.customer_addresses_resolved,
    "sales_order.gst_information_resolved": shared.gst_information_resolved,
    "sales_order.po_reference_present": po_reference_present,
    "sales_order.delivery_date_valid": delivery_date_valid,
    "sales_order.items_resolve_exactly_one": checks.items_resolve_exactly_one,
    "sales_order.items_enabled_and_saleable": checks.items_enabled_and_saleable,
    "sales_order.items_quantity_and_uom": checks.items_quantity_and_uom,
    "sales_order.brand_lines_stop_and_ask": shared.brand_lines_stop_and_ask,
    "sales_order.item_price_resolved": item_price_resolved,
    "sales_order.currency_and_exchange_rate": checks.currency_and_exchange_rate,
    "sales_order.tax_treatment_resolved": shared.tax_treatment_resolved,
    "sales_order.tax_inclusive_pricing": shared.tax_inclusive_pricing,
    "sales_order.duplicate_po_number": duplicate_po_number,
    "sales_order.forbidden_payload_fields": shared.forbidden_payload_fields,
    "sales_order.approval_workflow_not_bypassed": checks.draft_only_creation,
    "sales_order.post_creation_readback": post_creation_readback,
}


def register_all(registry: EvaluatorRegistry) -> None:
    """Register every Sales Order evaluator explicitly."""
    for key, evaluator in EVALUATORS.items():
        registry.register(key, evaluator, description=(evaluator.__doc__ or "").strip())
