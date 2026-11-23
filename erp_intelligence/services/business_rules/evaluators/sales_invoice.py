"""Sales Invoice and credit note evaluators.

Each evaluator implements one atomic responsibility from SALES_ORDER_TO_DELIVERY_GUIDE.md
(section 8, "Stage 4: create the Sales Invoice", and its "Credit note (return)" part).

How the business bills: invoices are raised against Sales Order lines (``sales_order`` and
``so_detail``), never against a Delivery Note (0 of 2,757 historical invoice lines link to
one). Linking an invoice to a Delivery Note works, but only when the user asks, so it is a
stop-and-ask. Prices are tax-inclusive.

Credit notes are Sales Invoices with ``is_return`` set. The framework allows one enabled
Rule Set per DocType and operation, so ``Sales Invoice / Create`` carries the invoice rules
and the credit-note rules together. An evaluator that does not apply to the payload at
hand returns a PASS that says so (``details.applicable`` false); it checked nothing.

GST. ERPNext accepted a wrong tax type, a contradicting place of supply, missing addresses
and no tax rows, and the invoice submitted (guide, section 8). No evaluator here claims GST
compliance. ``gst_compliance_not_verified`` says so explicitly on every invoice, and the
e-invoice (IRN) and e-way bill are manual steps these rules never perform or verify.
"""
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from erp_intelligence.services.business_rules.context import EvaluationContext
from erp_intelligence.services.business_rules.evaluators import sales_shared as shared
from erp_intelligence.services.business_rules.evaluators import sales_shared_checks as checks
from erp_intelligence.services.business_rules.evaluators.common import (
    Findings,
    is_blank,
    same_number,
    to_decimal,
)
from erp_intelligence.services.business_rules.evaluators.sales_common import (
    GST_NOT_VERIFIED,
    SalesFindings,
    check_tax_row_shape,
    evidence,
    inclusive_totals,
    is_return,
    label,
    line_rate,
    line_total,
    not_applicable,
    payload_rows,
    sales_order_header,
    sales_order_rows,
    same_value,
    truthy,
)
from erp_intelligence.services.business_rules.registry import EvaluatorRegistry

_ITEMS = "items"
_ORDER_RULE = "sales_invoice.sales_order_resolves_exactly_one"
_SO_FIELDS = ["item_code", "qty", "uom", "rate", "price_list_rate", "amount", "billed_amt", "custom_fulfilled_by"]


# ------------------------------------------------------------------ link lookups
def _items(context: EvaluationContext) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    return payload_rows(context, _ITEMS, "Sales Invoice")


def _single_order(context: EvaluationContext) -> tuple[str, str | None, set[str]]:
    rows = context.payload.get(_ITEMS)
    values: set[str] = set()
    blank = False
    for row in rows if isinstance(rows, list) else []:
        value = row.get("sales_order") if isinstance(row, Mapping) else None
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
        else:
            blank = True
    if not values or blank:
        return "missing", None, values
    if len(values) > 1:
        return "multiple", None, values
    return "resolved", next(iter(values)), values


def _need_order(context: EvaluationContext) -> tuple[str | None, dict[str, Any] | None]:
    state, name, _all = _single_order(context)
    if state != "resolved":
        return None, context.needs_input(
            f"The Sales Order is not resolved ({state}), so this rule cannot be evaluated.",
            [f"{_ITEMS}[].sales_order"],
            {"depends_on": _ORDER_RULE, "resolution": state},
        )
    return name, None


def _order_rows(context: EvaluationContext, name: str) -> dict[str, dict[str, Any]]:
    return sales_order_rows(context, name, _SO_FIELDS)


def _line_amount(line: Mapping[str, Any]) -> Decimal | None:
    qty, rate = to_decimal(line.get("qty")), line_rate(line)
    return None if qty is None or rate is None else qty * rate


# ------------------------------------------------------------- 1. Sales Order
def sales_order_resolves_exactly_one(context: EvaluationContext) -> dict[str, Any]:
    """Every row names the same, existing Sales Order. An invoice is raised against one order."""
    rows, dependency = _items(context)
    if dependency:
        return dependency
    state, name, names = _single_order(context)
    details = {
        "sales_orders": sorted(names),
        "evidence": evidence(
            "H",
            "1 / 8",
            "Invoices are raised against Sales Order lines (2,756 of 2,757 lines carry the order) and "
            "no invoice spans two orders (0 of 653).",
        ),
    }
    if state == "missing":
        return context.needs_input(
            "Every row needs the sales_order it bills. An invoice is raised against a Sales Order.",
            [f"{_ITEMS}[].sales_order"],
            details,
        )
    if state == "multiple":
        return context.failed(
            f"The rows point to {len(names)} Sales Orders ({', '.join(sorted(names))}). An invoice is "
            "raised against one Sales Order.",
            details,
        )
    if sales_order_header(context, name, []) is None:
        return context.failed(f"Sales Order '{name}' does not exist or is not readable.", details)
    return context.passed(f"Sales Order {name} is the single order billed.", {**details, "sales_order": name})


def sales_order_billable(context: EvaluationContext) -> dict[str, Any]:
    """The Sales Order must be approved, open for billing, B2B and not an internal order."""
    approved = context.required_param("approved_workflow_state")
    statuses = list(context.required_param("billable_statuses"))
    channels = list(context.required_param("allowed_channels"))
    name, dependency = _need_order(context)
    if dependency:
        return dependency
    header = sales_order_header(
        context, name, ["docstatus", "workflow_state", "status", "custom_type", "is_internal_customer"]
    )
    if header is None:
        return context.failed(f"Sales Order '{name}' does not exist or is not readable.")
    findings = Findings()
    if header["docstatus"] != 1 or header["workflow_state"] != approved:
        findings.fail(f"Sales Order {name} is not approved (docstatus {header['docstatus']}, '{header['workflow_state']}').")
    if header["status"] not in statuses:
        findings.fail(f"Sales Order {name} status is '{header['status']}'. Needed: {', '.join(statuses)}.")
    if header["custom_type"] not in channels:
        findings.fail(f"Sales Order {name} is channel '{header['custom_type']}'. Allowed: {', '.join(channels)}.")
    if truthy(header["is_internal_customer"]):
        findings.fail(f"Sales Order {name} is for an internal customer, which this process does not handle.")
    return findings.result(
        context, f"Sales Order {name} is approved and open for billing.", details={"sales_order": name}
    )


def customer_matches_sales_order(context: EvaluationContext) -> dict[str, Any]:
    """The invoice's customer and company must be the Sales Order's."""
    name, dependency = _need_order(context)
    if dependency:
        return dependency
    header = sales_order_header(context, name, ["customer", "company"])
    if header is None:
        return context.failed(f"Sales Order '{name}' does not exist or is not readable.")
    findings = Findings()
    for field in ("customer", "company"):
        value = context.payload.get(field)
        if is_blank(value):
            findings.need(field, f"{field} is required and must equal the Sales Order's '{header[field]}'.")
        elif not same_value(value, header[field]):
            findings.fail(f"{field} '{value}' differs from Sales Order {name} ('{header[field]}').")
    return findings.result(
        context, f"The invoice is for the customer of Sales Order {name}.", details={"sales_order": name}
    )


def selling_price_list_valid(context: EvaluationContext) -> dict[str, Any]:
    """The price list must be a usable selling list whose currency is the invoice currency."""
    chosen, currency = context.payload.get("selling_price_list"), context.payload.get("currency")
    findings = Findings()
    if is_blank(chosen):
        findings.need("selling_price_list", "selling_price_list is required. Copy it from the Sales Order.")
        return findings.result(context, "", details={})
    rows = context.data.fetch_records(
        "Price List", ["name", "enabled", "selling", "currency"], [("name", "=", chosen)]
    )
    details: dict[str, Any] = {"selling_price_list": chosen}
    if not rows:
        findings.fail(f"Price List '{chosen}' does not exist or is not readable.")
        return findings.result(context, "", details=details)
    details["price_list_currency"] = rows[0]["currency"]
    if not truthy(rows[0]["enabled"]):
        findings.fail(f"Price List '{chosen}' is disabled.")
    if not truthy(rows[0]["selling"]):
        findings.fail(f"Price List '{chosen}' is not a selling price list.")
    if is_blank(currency):
        findings.need("currency", "currency is required.")
    elif currency != rows[0]["currency"]:
        findings.fail(f"Currency {currency} differs from the price list currency {rows[0]['currency']}.")
    return findings.result(context, f"Price List '{chosen}' is a valid selling list for this invoice.", details=details)


# ------------------------------------------------------------------ 2. row links
def row_linkage_to_sales_order(context: EvaluationContext) -> dict[str, Any]:
    """Each row is billed against a Sales Order line. Linking to a Delivery Note needs the user's request."""
    rows, dependency = _items(context)
    if dependency:
        return dependency
    order, dependency = _need_order(context)
    if dependency:
        return dependency
    order_rows = _order_rows(context, order)
    findings = Findings()
    delivery_rows = []
    for index, line in enumerate(rows):
        tag, detail = label(_ITEMS, index), line.get("so_detail")
        if is_blank(detail):
            findings.need(f"{tag}.so_detail", f"Row {index + 1} needs so_detail, the Sales Order line's row id.")
        else:
            source = order_rows.get(detail)
            if source is None:
                findings.fail(f"Row {index + 1}: so_detail '{detail}' is not a line of Sales Order {order}.", row=index + 1)
            elif line.get("item_code") != source["item_code"]:
                findings.fail(
                    f"Row {index + 1}: item {line.get('item_code')} differs from the Sales Order line's {source['item_code']}.",
                    row=index + 1,
                )
        if not is_blank(line.get("delivery_note")) or not is_blank(line.get("dn_detail")):
            delivery_rows.append(index + 1)
    if delivery_rows:
        key = "delivery_note_linked_invoice_requested"
        message = (
            f"Row(s) {', '.join(map(str, delivery_rows))} link the invoice to a Delivery Note. This "
            "business bills against the Sales Order. Link a Delivery Note only if the user asked for it."
        )
        if context.is_confirmed(key):
            findings.warn(message + " The user asked for it.", rows=delivery_rows)
        else:
            findings.confirm(key, message, rows=delivery_rows)
    return findings.result(
        context,
        "Every row is billed against a Sales Order line.",
        details={
            "sales_order": order,
            "evidence": evidence(
                "H",
                "1 / 8 step 3",
                "No historical invoice line links to a Delivery Note; 372 lines have an order but no "
                "so_detail, which is why this rule asks for so_detail. Billing against a Delivery Note "
                "was verified to work.",
            ),
        },
    )


def unbilled_quantity_available(context: EvaluationContext) -> dict[str, Any]:
    """An invoice may bill only what the Sales Order line has not been billed for yet."""
    if is_return(context):
        return not_applicable(context, "credit notes are checked by the return rules")
    tolerance = to_decimal(context.param("amount_tolerance", "0.02")) or Decimal("0.02")
    rows, dependency = _items(context)
    if dependency:
        return dependency
    order, dependency = _need_order(context)
    if dependency:
        return dependency
    order_rows = _order_rows(context, order)
    billing: dict[str, Decimal] = {}
    findings = Findings()
    for index, line in enumerate(rows):
        detail, amount = line.get("so_detail"), _line_amount(line)
        if is_blank(detail) or detail not in order_rows:
            findings.need(
                f"{label(_ITEMS, index)}.so_detail",
                f"Row {index + 1} has no resolved Sales Order line.",
                depends_on="sales_invoice.row_linkage_to_sales_order",
            )
            continue
        if amount is None or amount <= 0:
            findings.need(f"{label(_ITEMS, index)}.price_list_rate", f"Row {index + 1} needs a positive qty and price.")
            continue
        billing[detail] = billing.get(detail, Decimal(0)) + amount
    remaining_by_line: dict[str, Any] = {}
    for detail, amount in billing.items():
        source = order_rows[detail]
        unbilled = (to_decimal(source["amount"]) or Decimal(0)) - (to_decimal(source["billed_amt"]) or Decimal(0))
        remaining_by_line[detail] = {"item_code": source["item_code"], "order_amount": source["amount"], "billed_amount": source["billed_amt"], "unbilled": unbilled, "billing": amount}
        if unbilled <= tolerance:
            findings.fail(
                f"Item {source['item_code']} on Sales Order {order} is already fully billed. A second "
                "invoice would save without error. Do not create it.",
                item_code=source["item_code"],
            )
        elif amount > unbilled + tolerance:
            findings.fail(
                f"Item {source['item_code']}: billing {amount} but only {unbilled} is unbilled on Sales Order {order}.",
                item_code=source["item_code"],
            )
    return findings.result(
        context,
        "Every line bills no more than its unbilled amount.",
        details={
            "unbilled_by_line": remaining_by_line,
            "evidence": evidence(
                "V",
                "8 step 1",
                "The system inserted a second invoice for a fully billed line without error. billed_amt "
                "counts submitted invoices only; Draft invoices are checked by the duplicate guard.",
            ),
        },
    )


def duplicate_billing_guard(context: EvaluationContext) -> dict[str, Any]:
    """A Draft invoice that already bills the same Sales Order line needs the user's review."""
    if is_return(context):
        return not_applicable(context, "credit notes are checked by the return duplicate guard")
    tolerance = to_decimal(context.param("amount_tolerance", "0.02")) or Decimal("0.02")
    rows, dependency = _items(context)
    if dependency:
        return dependency
    order, dependency = _need_order(context)
    if dependency:
        return dependency
    customer, po_no = context.payload.get("customer"), context.payload.get("po_no")
    if is_blank(customer) or is_blank(po_no):
        return context.needs_input(
            "The customer and the customer PO number are required for the duplicate check.",
            [name for name, value in (("customer", customer), ("po_no", po_no)) if is_blank(value)],
        )
    mine = {row["so_detail"] for row in rows if not is_blank(row.get("so_detail"))}
    drafts = context.data.fetch_records(
        "Sales Invoice", ["name", "docstatus"],
        [("customer", "=", customer), ("po_no", "=", po_no), ("docstatus", "=", 0), ("is_return", "=", 0)],
    )
    overlaps = []
    if drafts:
        for row in context.data.fetch_child_records(
            "Sales Invoice Item", "Sales Invoice", [draft["name"] for draft in drafts], ["parent", "so_detail", "amount"]
        ):
            if row["so_detail"] in mine:
                overlaps.append(row)
    details = {
        "draft_invoices": [draft["name"] for draft in drafts],
        "evidence": evidence(
            "V",
            "8 step 2",
            "Draft invoices are found by customer and customer PO number. Counts only invoices this user may read.",
        ),
    }
    if not overlaps:
        return context.passed("No Draft invoice already bills these Sales Order lines.", details)
    order_rows = _order_rows(context, order)
    findings = SalesFindings()
    drafted: dict[str, Decimal] = {}
    for row in overlaps:
        drafted[row["so_detail"]] = drafted.get(row["so_detail"], Decimal(0)) + (to_decimal(row["amount"]) or Decimal(0))
    this_amount: dict[str, Decimal] = {}
    for line in rows:
        if not is_blank(line.get("so_detail")):
            this_amount[line["so_detail"]] = this_amount.get(line["so_detail"], Decimal(0)) + (_line_amount(line) or Decimal(0))
    for detail, amount in drafted.items():
        source = order_rows.get(detail)
        if source is None:
            continue
        unbilled = (to_decimal(source["amount"]) or Decimal(0)) - (to_decimal(source["billed_amt"]) or Decimal(0))
        if amount + this_amount.get(detail, Decimal(0)) > unbilled + tolerance:
            findings.fail(
                f"Item {source['item_code']}: Draft invoice(s) already bill {amount} and this one bills "
                f"{this_amount.get(detail)}, together more than the {unbilled} unbilled.",
                item_code=source["item_code"],
            )
    if not findings.has_failures:
        key = "duplicate_invoice_review"
        message = (
            f"Draft invoice(s) {', '.join(sorted({draft['name'] for draft in drafts}))} already bill "
            "some of these lines. Show them to the user and ask whether to continue."
        )
        if context.is_confirmed(key):
            findings.warn(message + " The user chose to continue.", draft_invoices=[d["name"] for d in drafts])
        else:
            findings.confirm(key, message, draft_invoices=[d["name"] for d in drafts])
    return findings.result(context, "No duplicate billing was found.", details=details)


def prices_match_sales_order(context: EvaluationContext) -> dict[str, Any]:
    """The invoice bills the Sales Order's price. A different price is a stop-and-ask."""
    rows, dependency = _items(context)
    if dependency:
        return dependency
    order, dependency = _need_order(context)
    if dependency:
        return dependency
    order_rows = _order_rows(context, order)
    findings = SalesFindings()
    differing = []
    for index, line in enumerate(rows):
        detail, tag = line.get("so_detail"), label(_ITEMS, index)
        if is_blank(detail) or detail not in order_rows:
            findings.need(f"{tag}.so_detail", f"Row {index + 1} has no resolved Sales Order line.", depends_on="sales_invoice.row_linkage_to_sales_order")
            continue
        source = order_rows[detail]
        price = line.get("price_list_rate")
        if is_blank(price):
            findings.need(f"{tag}.price_list_rate", f"Row {index + 1} needs price_list_rate (the Sales Order line's is {source['price_list_rate']}).")
            continue
        if to_decimal(price) is None or to_decimal(price) <= 0:
            findings.fail(f"Row {index + 1} price must be greater than zero.", row=index + 1)
        elif not same_value(price, source["price_list_rate"]) or (
            not is_blank(line.get("rate")) and not same_value(line["rate"], source["rate"])
        ):
            differing.append({"row": index + 1, "item_code": line.get("item_code"), "invoice_price": price, "order_price": source["price_list_rate"]})
    if differing and not findings.has_failures:
        key = "invoice_price_differs_from_order"
        message = f"{len(differing)} row(s) bill a price different from the Sales Order. Ask the user."
        if context.is_confirmed(key):
            findings.warn(message + " Confirmed by the user.", differences=differing)
        else:
            findings.confirm(key, message, differences=differing)
    return findings.result(
        context,
        "Prices equal the Sales Order lines.",
        details={"differences": differing, "evidence": evidence("U", "8 step 3", "The guide copies the header and line facts from the Sales Order; billing another price is not described, so it is a stop-and-ask.")},
    )


def forbidden_payload_fields(context: EvaluationContext) -> dict[str, Any]:
    return shared.forbidden_payload_fields(context)


# ------------------------------------------------------------- 3. GST notice
def gst_compliance_not_verified(context: EvaluationContext) -> dict[str, Any]:
    """Always a warning: this rule set cannot establish GST compliance, and IRN and e-way bill are manual."""
    details: dict[str, Any] = {
        "notice": GST_NOT_VERIFIED,
        "manual_steps": ["e-invoice (IRN) generation", "e-way bill generation"],
        "evidence": evidence(
            "V",
            "8 step 5 / 13",
            "ERPNext accepted a wrong tax type, a place of supply that contradicts the GSTIN, missing "
            "addresses and no tax rows, and the invoice submitted. IRN and e-way bill generation are "
            "switched off for automatic creation and are done by staff in the ERPNext screen.",
        ),
    }
    name = context.payload.get("name")
    if isinstance(name, str) and name.strip():
        rows = context.data.fetch_records(
            "Sales Invoice", ["name", "irn", "einvoice_status", "ewaybill", "e_waybill_status"], [("name", "=", name)]
        )
        if rows:
            details["irn_generated"] = not is_blank(rows[0]["irn"])
            details["einvoice_status"] = rows[0]["einvoice_status"]
            details["ewaybill_generated"] = not is_blank(rows[0]["ewaybill"])
    return context.warning(
        "GST compliance is NOT verified. The system does not validate the tax type, the place of supply "
        "or missing addresses, and the IRN and e-way bill have not been generated by this process. Do not "
        "tell the user the invoice is GST compliant.",
        details,
    )


# ------------------------------------------------------------------ read-back
def post_creation_readback(context: EvaluationContext) -> dict[str, Any]:
    """Read the created invoice Draft back: state, series, links, tax-inclusive totals, GST fields."""
    if is_return(context):
        return not_applicable(context, "credit notes are verified by sales_invoice.return_post_creation_verification")
    name = context.execution_result.get("name")
    if is_blank(name) or not isinstance(name, str):
        return context.needs_input("The created document name is required to read it back.", ["execution_result.name"])
    initial = dict(context.param("expected_initial_state", {}))
    compare_fields = list(context.param("compare_fields", []))
    series_prefix = context.param("expected_series_prefix", None)
    tolerance = to_decimal(context.param("amount_tolerance", "0.02")) or Decimal("0.02")
    taxed = list(context.param("taxed_gst_categories", []))
    header_fields = list(
        dict.fromkeys(
            [
                "name", *initial, *compare_fields, "naming_series", "total", "net_total",
                "total_taxes_and_charges", "grand_total", "rounded_total", "gst_category",
                "billing_address_gstin", "update_stock",
            ]
        )
    )
    rows = context.data.fetch_records("Sales Invoice", header_fields, [("name", "=", name)])
    details: dict[str, Any] = {
        "document": name,
        "notice": GST_NOT_VERIFIED,
        "evidence": evidence("V", "8 step 4", "The three placeholder totals the assistant tool demands are recalculated by the server; the real totals are read here."),
    }
    if not rows:
        key = "readback_unverified_acknowledged"
        details["verification"] = "NOT_VERIFIED"
        message = f"{name} could not be read back by the current user. Do not create it again. Report the name and say it is unverified."
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
            findings.fail(f"{field} is {header.get(field)!r} but {context.payload[field]!r} was sent.", field=field)
    if series_prefix and not str(header.get("naming_series") or "").startswith(str(series_prefix)):
        findings.fail(f"naming_series is {header.get('naming_series')!r}, expected one starting with {series_prefix!r}.")
    if truthy(header.get("update_stock")):
        findings.fail("update_stock is set. An invoice here never moves stock; the Delivery Note does.")
    sent = [row for row in (context.payload.get(_ITEMS) or []) if isinstance(row, Mapping)]
    read = sorted(
        context.data.fetch_child_records(
            "Sales Invoice Item", "Sales Invoice", [name],
            ["parent", "idx", "item_code", "qty", "uom", "rate", "price_list_rate", "amount",
             "sales_order", "so_detail", "delivery_note", "dn_detail", "income_account"],
        ),
        key=lambda row: row["idx"],
    )
    if len(read) != len(sent):
        findings.fail(f"{len(read)} item row(s) were read back but {len(sent)} were sent.")
    for index, (row_sent, row_read) in enumerate(zip(sent, read)):
        for field in ("item_code", "qty", "uom", "price_list_rate", "sales_order", "so_detail", "delivery_note", "dn_detail", "income_account"):
            if field in row_sent and not same_value(row_read.get(field), row_sent[field]):
                findings.fail(f"{label(_ITEMS, index)}.{field} is {row_read.get(field)!r} but {row_sent[field]!r} was sent.", row=index + 1)
        for field in ("sales_order", "so_detail"):
            if is_blank(row_read.get(field)):
                findings.fail(f"{label(_ITEMS, index)}.{field} was not kept, so the Sales Order's billed amount would not update.", row=index + 1)
        rate = to_decimal(row_read.get("rate"))
        if rate is None or rate <= 0:
            findings.fail(f"{label(_ITEMS, index)}.rate is zero or empty.", row=index + 1)
    sent_taxes = [row for row in (context.payload.get("taxes") or []) if isinstance(row, Mapping)]
    read_taxes = sorted(
        context.data.fetch_child_records(
            "Sales Taxes and Charges", "Sales Invoice", [name],
            ["parent", "idx", "account_head", "rate", "tax_amount", "included_in_print_rate"],
        ),
        key=lambda row: row["idx"],
    )
    if len(read_taxes) != len(sent_taxes):
        findings.fail(f"{len(read_taxes)} tax row(s) were read back but {len(sent_taxes)} were sent.")
    for index, (row_sent, row_read) in enumerate(zip(sent_taxes, read_taxes)):
        if not same_value(row_read.get("account_head"), row_sent.get("account_head")) or not same_value(row_read.get("rate"), row_sent.get("rate")):
            findings.fail(f"{label('taxes', index)} account_head or rate differs from what was sent.", row=index + 1)
        if not truthy(row_read.get("included_in_print_rate")):
            findings.fail(f"{label('taxes', index)} is not tax-inclusive.", row=index + 1)
    total = line_total(sent)
    expected = inclusive_totals(total, sent_taxes) if total is not None else None
    if expected is not None:
        for field in ("total", "total_taxes_and_charges", "grand_total"):
            actual = to_decimal(header.get(field))
            if actual is None or abs(actual - expected[field]) > tolerance:
                findings.fail(f"{field} is {header.get(field)!r}, expected {expected[field].quantize(Decimal('0.01'))} (tax-inclusive).", field=field)
        if same_number(header.get("grand_total"), 1) and not same_number(expected["grand_total"], 1):
            findings.fail("grand_total is still the placeholder 1. The server did not recalculate it.")
        details["expected_totals"] = expected
    else:
        findings.warn("Expected totals were not computed; the totals are unverified.")
    customer = context.payload.get("customer")
    if taxed and not is_blank(customer):
        record = context.data.fetch_records("Customer", ["name", "gst_category"], [("name", "=", customer)])
        if record and record[0]["gst_category"] in taxed and (is_blank(header.get("gst_category")) or is_blank(header.get("billing_address_gstin"))):
            findings.warn("gst_category or billing_address_gstin is empty although the customer is registered. The billing address may be missing.")
    details["verification"] = "READ_BACK_COMPARED"
    return findings.result(context, f"{name} was read back and matches what was sent.", details=details)


# =========================================================================
# Credit note / Sales Invoice return
# =========================================================================
def _original(context: EvaluationContext) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(original_invoice_header, None)`` or ``(None, result)`` when it cannot be resolved."""
    original = context.payload.get("return_against")
    if is_blank(original) or not isinstance(original, str):
        return None, context.needs_input("return_against, the invoice being credited, is required.", ["return_against"])
    rows = context.data.fetch_records(
        "Sales Invoice", ["name", "docstatus", "is_return", "customer", "company", "status", "currency"], [("name", "=", original)]
    )
    if not rows:
        return None, context.failed(f"Invoice '{original}' does not exist or is not readable.")
    return rows[0], None


def return_source_eligible(context: EvaluationContext) -> dict[str, Any]:
    """The invoice being credited must be a submitted, non-return invoice of the same customer."""
    if not is_return(context):
        return not_applicable(context, "the payload is not a credit note")
    original, dependency = _original(context)
    if dependency:
        return dependency
    findings = Findings()
    if original["docstatus"] != 1:
        findings.fail(f"Invoice {original['name']} is not submitted (docstatus {original['docstatus']}).")
    if truthy(original["is_return"]):
        findings.fail(f"Invoice {original['name']} is itself a credit note.")
    for field in ("customer", "company"):
        value = context.payload.get(field)
        if is_blank(value):
            findings.need(field, f"{field} is required and must equal the invoice's '{original[field]}'.")
        elif not same_value(value, original[field]):
            findings.fail(f"{field} '{value}' differs from invoice {original['name']} ('{original[field]}').")
    return findings.result(
        context,
        f"Invoice {original['name']} can be credited.",
        details={
            "return_against": original["name"],
            "evidence": evidence("V", "8 'Credit note'", "A credit note is a Sales Invoice with is_return, return_against and negative quantities."),
        },
    )


def _original_rows(context: EvaluationContext, name: str) -> dict[str, dict[str, Any]]:
    rows = context.data.fetch_child_records(
        "Sales Invoice Item", "Sales Invoice", [name],
        ["parent", "name", "idx", "item_code", "qty", "rate", "price_list_rate", "sales_order", "so_detail"],
    )
    return {row["so_detail"]: row for row in rows if not is_blank(row.get("so_detail"))}


def _live_returns(context: EvaluationContext, original: str) -> list[dict[str, Any]]:
    return context.data.fetch_records(
        "Sales Invoice", ["name", "docstatus", "grand_total"],
        [("return_against", "=", original), ("is_return", "=", 1), ("docstatus", "in", [0, 1])],
    )


def return_quantity_valid(context: EvaluationContext) -> dict[str, Any]:
    """Return quantities are negative and never exceed what the invoice still has uncredited."""
    if not is_return(context):
        return not_applicable(context, "the payload is not a credit note")
    rows, dependency = _items(context)
    if dependency:
        return dependency
    original, dependency = _original(context)
    if dependency:
        return dependency
    source = _original_rows(context, original["name"])
    already: dict[str, Decimal] = {}
    existing = _live_returns(context, original["name"])
    if existing:
        for row in context.data.fetch_child_records(
            "Sales Invoice Item", "Sales Invoice", [item["name"] for item in existing], ["parent", "so_detail", "qty"]
        ):
            already[row["so_detail"]] = already.get(row["so_detail"], Decimal(0)) + abs(to_decimal(row["qty"]) or Decimal(0))
    findings = Findings()
    returning: dict[str, Decimal] = {}
    for index, line in enumerate(rows):
        tag, detail, qty = label(_ITEMS, index), line.get("so_detail"), to_decimal(line.get("qty"))
        if qty is None:
            findings.need(f"{tag}.qty", f"Row {index + 1} needs a numeric qty.")
        elif qty >= 0:
            findings.fail(f"Row {index + 1}: a credit note quantity must be negative, got {qty}.", row=index + 1)
        elif is_blank(detail) or detail not in source:
            findings.need(f"{tag}.so_detail", f"Row {index + 1} has no line of invoice {original['name']} to credit.", depends_on="sales_invoice.return_item_linkage")
        else:
            returning[detail] = returning.get(detail, Decimal(0)) + abs(qty)
    for detail, qty in returning.items():
        credited = already.get(detail, Decimal(0))
        remaining = (to_decimal(source[detail]["qty"]) or Decimal(0)) - credited
        if qty > remaining:
            findings.fail(
                f"Item {source[detail]['item_code']}: returning {qty} but only {remaining} of "
                f"{source[detail]['qty']} is left to credit ({credited} already on other credit notes).",
                item_code=source[detail]["item_code"],
            )
    return findings.result(
        context,
        "Return quantities are negative and within what is left to credit.",
        details={"return_against": original["name"], "evidence": evidence("V", "8 'Credit note'", "A hand-built return with qty -2 against an invoice of qty 2 was created with negative totals.")},
    )


def return_item_linkage(context: EvaluationContext) -> dict[str, Any]:
    """Each return row keeps the original row's Sales Order link and the same item."""
    if not is_return(context):
        return not_applicable(context, "the payload is not a credit note")
    rows, dependency = _items(context)
    if dependency:
        return dependency
    original, dependency = _original(context)
    if dependency:
        return dependency
    source = _original_rows(context, original["name"])
    findings = Findings()
    for index, line in enumerate(rows):
        tag, detail = label(_ITEMS, index), line.get("so_detail")
        if is_blank(detail):
            findings.need(f"{tag}.so_detail", f"Row {index + 1} needs so_detail, copied from the invoice row it credits.")
            continue
        base = source.get(detail)
        if base is None:
            findings.fail(f"Row {index + 1}: so_detail '{detail}' is not on invoice {original['name']}.", row=index + 1)
            continue
        if line.get("item_code") != base["item_code"]:
            findings.fail(f"Row {index + 1}: item {line.get('item_code')} differs from the invoice row's {base['item_code']}.", row=index + 1)
        if not is_blank(line.get("sales_order")) and line["sales_order"] != base["sales_order"]:
            findings.fail(f"Row {index + 1}: sales_order {line['sales_order']} differs from the invoice row's {base['sales_order']}.", row=index + 1)
        if not is_blank(line.get("delivery_note")) or not is_blank(line.get("dn_detail")):
            findings.fail(f"Row {index + 1}: a credit note is not linked to a Delivery Note.", row=index + 1)
    return findings.result(
        context,
        "Every return row is linked to the invoice row it credits.",
        details={"return_against": original["name"]},
    )


def return_amounts_consistent(context: EvaluationContext) -> dict[str, Any]:
    """Return prices and tax rows must be those of the original invoice, still tax-inclusive."""
    if not is_return(context):
        return not_applicable(context, "the payload is not a credit note")
    rows, dependency = _items(context)
    if dependency:
        return dependency
    original, dependency = _original(context)
    if dependency:
        return dependency
    source = _original_rows(context, original["name"])
    findings = Findings()
    for index, line in enumerate(rows):
        detail, tag = line.get("so_detail"), label(_ITEMS, index)
        base = source.get(detail) if not is_blank(detail) else None
        if base is None:
            findings.need(f"{tag}.so_detail", f"Row {index + 1} has no invoice row to compare with.", depends_on="sales_invoice.return_item_linkage")
            continue
        for field in ("price_list_rate", "rate"):
            sent = line.get(field)
            if is_blank(sent):
                if field == "price_list_rate":
                    findings.need(f"{tag}.{field}", f"Row {index + 1} needs {field} (the invoice row's is {base[field]}).")
            elif not same_value(sent, base[field]):
                findings.fail(f"Row {index + 1}: {field} {sent} differs from the invoice row's {base[field]}.", row=index + 1)
    sent_taxes = [row for row in (context.payload.get("taxes") or []) if isinstance(row, Mapping)]
    base_taxes = context.data.fetch_child_records(
        "Sales Taxes and Charges", "Sales Invoice", [original["name"]],
        ["parent", "account_head", "rate", "charge_type", "included_in_print_rate"],
    )
    signature = lambda row: (str(row.get("account_head")), str(to_decimal(row.get("rate"))), str(row.get("charge_type")))  # noqa: E731
    if sorted(signature(row) for row in sent_taxes) != sorted(signature(row) for row in base_taxes):
        findings.fail("The credit note's tax rows differ from the original invoice's (account, rate, charge type).")
    check_tax_row_shape(findings, sent_taxes, context.param("required_included_in_print_rate", 1))
    return findings.result(
        context,
        "Return prices and tax rows match the original invoice and stay tax-inclusive.",
        details={"return_against": original["name"], "evidence": evidence("V", "8 'Credit note'", "The return had totals -200 and tax -30.51 for an invoice of 200 with tax 30.51.")},
    )


def return_duplicate_guard(context: EvaluationContext) -> dict[str, Any]:
    """Another live credit note against the same invoice needs the user's review."""
    if not is_return(context):
        return not_applicable(context, "the payload is not a credit note")
    original, dependency = _original(context)
    if dependency:
        return dependency
    existing = _live_returns(context, original["name"])
    details = {
        "return_against": original["name"],
        "existing_returns": [item["name"] for item in existing],
        "evidence": evidence("V", "8 'Credit note'", "Counts only credit notes this user may read."),
    }
    if not existing:
        return context.passed("No other live credit note exists against this invoice.", details)
    key = "duplicate_return_review"
    message = (
        f"Credit note(s) {', '.join(item['name'] for item in existing)} already exist against invoice "
        f"{original['name']}. Show them to the user and ask whether this is a further partial return."
    )
    if context.is_confirmed(key):
        return context.warning(message + " The user chose to continue.", details)
    return context.needs_confirmation(message, [key], details)


def return_stock_and_outstanding_notice(context: EvaluationContext) -> dict[str, Any]:
    """A credit note does not return goods to stock and does not by itself reduce the invoice's outstanding."""
    if not is_return(context):
        return not_applicable(context, "the payload is not a credit note")
    details = {
        "unresolved_requirements": [
            "A Delivery Note return (series DRET) is what brings goods back into stock. It was not verified.",
            "No return warehouse is defined by this process.",
        ],
        "evidence": evidence(
            "V",
            "8 'Credit note'",
            "update_stock stays 0, so a credit note moves no stock. Submitting it did not reduce the "
            "original invoice's outstanding; ERPNext only asked whether to.",
        ),
    }
    if truthy(context.payload.get("update_stock")):
        return context.failed(
            "update_stock must not be set on a credit note. Returning goods is a Delivery Note return, "
            "which is not verified. Stop and ask the user.",
            details,
        )
    return context.warning(
        "This credit note will not bring goods back into stock and will not by itself reduce the original "
        "invoice's outstanding amount. Tell the user. A Delivery Note return for the goods is not covered "
        "by this process.",
        details,
    )


def return_post_creation_verification(context: EvaluationContext) -> dict[str, Any]:
    """Read the created credit note back: return markers, CN series, negative lines and totals."""
    if not is_return(context):
        return not_applicable(context, "the payload is not a credit note")
    name = context.execution_result.get("name")
    if is_blank(name) or not isinstance(name, str):
        return context.needs_input("The created document name is required to read it back.", ["execution_result.name"])
    prefix = context.param("expected_series_prefix", None)
    tolerance = to_decimal(context.param("amount_tolerance", "0.02")) or Decimal("0.02")
    header_rows = context.data.fetch_records(
        "Sales Invoice",
        ["name", "docstatus", "is_return", "return_against", "naming_series", "total", "total_taxes_and_charges", "grand_total", "update_stock"],
        [("name", "=", name)],
    )
    details: dict[str, Any] = {
        "document": name,
        "notice": GST_NOT_VERIFIED,
        "evidence": evidence("V", "8 'Credit note'", "The credit note gets series CN-.#### from a hook even when no series is sent."),
    }
    if not header_rows:
        key = "readback_unverified_acknowledged"
        details["verification"] = "NOT_VERIFIED"
        message = f"{name} could not be read back by the current user. Do not create it again. Report the name and say it is unverified."
        if context.is_confirmed(key):
            return context.warning(message + " Acknowledged by the user.", details)
        return context.needs_confirmation(message, [key], details)
    header = header_rows[0]
    findings = Findings()
    if header["docstatus"] != 0:
        findings.fail(f"docstatus is {header['docstatus']!r}, expected 0 (Draft).")
    if not truthy(header["is_return"]):
        findings.fail("is_return is not set on the created document.")
    if not same_value(header["return_against"], context.payload.get("return_against")):
        findings.fail(f"return_against is {header['return_against']!r}, expected {context.payload.get('return_against')!r}.")
    if prefix and not str(header.get("naming_series") or "").startswith(str(prefix)):
        findings.fail(f"naming_series is {header.get('naming_series')!r}, expected one starting with {prefix!r}.")
    if truthy(header.get("update_stock")):
        findings.fail("update_stock is set on a credit note.")
    sent = [row for row in (context.payload.get(_ITEMS) or []) if isinstance(row, Mapping)]
    read = sorted(
        context.data.fetch_child_records(
            "Sales Invoice Item", "Sales Invoice", [name], ["parent", "idx", "item_code", "qty", "rate", "sales_order", "so_detail"]
        ),
        key=lambda row: row["idx"],
    )
    if len(read) != len(sent):
        findings.fail(f"{len(read)} item row(s) were read back but {len(sent)} were sent.")
    for index, (row_sent, row_read) in enumerate(zip(sent, read)):
        quantity = to_decimal(row_read.get("qty"))
        if quantity is None or quantity >= 0:
            findings.fail(f"{label(_ITEMS, index)}.qty is {row_read.get('qty')!r}; a credit note line must be negative.", row=index + 1)
        for field in ("item_code", "qty", "so_detail", "sales_order"):
            if field in row_sent and not same_value(row_read.get(field), row_sent[field]):
                findings.fail(f"{label(_ITEMS, index)}.{field} is {row_read.get(field)!r} but {row_sent[field]!r} was sent.", row=index + 1)
    sent_taxes = [row for row in (context.payload.get("taxes") or []) if isinstance(row, Mapping)]
    total = line_total(sent)
    expected = inclusive_totals(total, sent_taxes) if total is not None else None
    if expected is not None:
        for field in ("total", "total_taxes_and_charges", "grand_total"):
            actual = to_decimal(header.get(field))
            if actual is None or abs(actual - expected[field]) > tolerance:
                findings.fail(f"{field} is {header.get(field)!r}, expected {expected[field].quantize(Decimal('0.01'))} (negative, tax-inclusive).", field=field)
        details["expected_totals"] = expected
    else:
        findings.warn("Expected totals were not computed; the totals are unverified.")
    details["verification"] = "READ_BACK_COMPARED"
    return findings.result(context, f"{name} was read back and is a valid Draft credit note.", details=details)


# =========================================================================
# Sales Invoice / Submit
# =========================================================================
def _invoice(context: EvaluationContext) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    name = context.payload.get("name")
    if is_blank(name) or not isinstance(name, str):
        return None, context.needs_input("The invoice name is required.", ["name"])
    rows = context.data.fetch_records(
        "Sales Invoice",
        ["name", "docstatus", "status", "customer", "is_return", "debit_to", "grand_total", "rounded_total",
         "total_taxes_and_charges", "outstanding_amount", "update_stock"],
        [("name", "=", name)],
    )
    if not rows:
        return None, context.needs_input(f"Invoice '{name}' could not be read, so it cannot be checked.", ["name"])
    return rows[0], None


def draft_ready_to_submit(context: EvaluationContext) -> dict[str, Any]:
    """Before submitting: a Draft with order links, real totals and, for a registered customer, tax rows."""
    taxed = list(context.param("taxed_gst_categories", []))
    invoice, dependency = _invoice(context)
    if dependency:
        return dependency
    findings = Findings()
    if invoice["docstatus"] != 0:
        findings.fail(f"Invoice {invoice['name']} is not a Draft (docstatus {invoice['docstatus']}).")
    grand = to_decimal(invoice["grand_total"])
    if grand is None or grand == 0 or same_number(grand, 1):
        findings.fail(f"grand_total is {invoice['grand_total']!r}. It looks like an unrecalculated placeholder or an empty invoice.")
    elif truthy(invoice["is_return"]) != (grand < 0):
        findings.fail(f"grand_total {grand} has the wrong sign for {'a credit note' if truthy(invoice['is_return']) else 'an invoice'}.")
    if truthy(invoice["update_stock"]):
        findings.fail("update_stock is set. An invoice here never moves stock.")
    rows = context.data.fetch_child_records(
        "Sales Invoice Item", "Sales Invoice", [invoice["name"]], ["parent", "idx", "sales_order", "so_detail"]
    )
    if not rows:
        findings.fail("The invoice has no items.")
    for row in rows:
        for field in ("sales_order", "so_detail"):
            if is_blank(row.get(field)):
                findings.fail(f"Row {row['idx']} has no {field}, so the Sales Order's billed amount would not update.", row=row["idx"])
    record = context.data.fetch_records("Customer", ["name", "gst_category"], [("name", "=", invoice["customer"])])
    tax_rows = context.data.fetch_child_records(
        "Sales Taxes and Charges", "Sales Invoice", [invoice["name"]], ["parent", "account_head"]
    )
    if record and record[0]["gst_category"] in taxed and not tax_rows:
        findings.fail("The customer is registered but the invoice has no tax rows. The system would submit it anyway.")
    return findings.result(
        context,
        "The Draft invoice is ready to submit.",
        details={
            "document": invoice["name"],
            "notice": "Submitting posts to the ledger and cannot be undone by the assistant. " + GST_NOT_VERIFIED,
            "evidence": evidence("V", "8 step 5", "The system submitted an invoice with a wrong tax type, no addresses and no tax rows."),
        },
    )


def ledger_posting_verified(context: EvaluationContext) -> dict[str, Any]:
    """After submit: the general ledger balances and the receivable carries the invoice total."""
    tolerance = to_decimal(context.param("amount_tolerance", "0.02")) or Decimal("0.02")
    invoice, dependency = _invoice(context)
    if dependency:
        return dependency
    findings = Findings()
    if invoice["docstatus"] != 1:
        findings.fail(f"Invoice {invoice['name']} is not submitted (docstatus {invoice['docstatus']}), so nothing was posted.")
        return findings.result(context, "", details={"document": invoice["name"]})
    entries = context.data.fetch_records(
        "GL Entry", ["account", "debit", "credit", "is_cancelled"],
        [("voucher_type", "=", "Sales Invoice"), ("voucher_no", "=", invoice["name"]), ("is_cancelled", "=", 0)],
    )
    if not entries:
        findings.fail(f"No general ledger entries were found for {invoice['name']}.")
        return findings.result(context, "", details={"document": invoice["name"]})
    debit = sum((to_decimal(row["debit"]) or Decimal(0)) for row in entries)
    credit = sum((to_decimal(row["credit"]) or Decimal(0)) for row in entries)
    if abs(debit - credit) > tolerance:
        findings.fail(f"The ledger does not balance: debits {debit}, credits {credit}.")
    receivable = [row for row in entries if row["account"] == invoice["debit_to"]]
    net_receivable = sum((to_decimal(row["debit"]) or Decimal(0)) - (to_decimal(row["credit"]) or Decimal(0)) for row in receivable)
    expected_candidates = [value for value in (to_decimal(invoice["rounded_total"]), to_decimal(invoice["grand_total"])) if value]
    if not receivable:
        findings.fail(f"No ledger entry on the receivable account {invoice['debit_to']}.")
    elif not any(abs(net_receivable - value) <= tolerance for value in expected_candidates):
        findings.fail(
            f"The receivable {invoice['debit_to']} carries {net_receivable}, but the invoice total is "
            f"{invoice['rounded_total']} (rounded) / {invoice['grand_total']}."
        )
    return findings.result(
        context,
        "The ledger balances and the receivable carries the invoice total.",
        details={
            "document": invoice["name"],
            "debit": debit,
            "credit": credit,
            "net_receivable": net_receivable,
            "ledger_entries": len(entries),
            "notice": GST_NOT_VERIFIED,
            "evidence": evidence("V", "8 step 5", "Debtors debit 200, Sales credit 169.49 and the tax account credit 30.51 for an invoice of 200."),
        },
    )


def sales_order_billing_updated(context: EvaluationContext) -> dict[str, Any]:
    """After submit: the Sales Order lines show at least the billed amount."""
    tolerance = to_decimal(context.param("amount_tolerance", "0.02")) or Decimal("0.02")
    invoice, dependency = _invoice(context)
    if dependency:
        return dependency
    if invoice["docstatus"] != 1:
        return context.failed(f"Invoice {invoice['name']} is not submitted, so no order was updated.")
    rows = context.data.fetch_child_records(
        "Sales Invoice Item", "Sales Invoice", [invoice["name"]], ["parent", "idx", "sales_order", "so_detail", "amount"]
    )
    findings = Findings()
    billed: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        if is_blank(row.get("sales_order")) or is_blank(row.get("so_detail")):
            findings.fail(f"Row {row['idx']} has no Sales Order link, so the order cannot have been updated.", row=row["idx"])
            continue
        key = (row["sales_order"], row["so_detail"])
        billed[key] = billed.get(key, Decimal(0)) + abs(to_decimal(row["amount"]) or Decimal(0))
    for order in sorted({order for order, _detail in billed}):
        order_rows = sales_order_rows(context, order, ["billed_amt", "item_code"])
        for (name, detail), amount in billed.items():
            if name == order and detail in order_rows and not truthy(invoice["is_return"]):
                if (to_decimal(order_rows[detail]["billed_amt"]) or Decimal(0)) + tolerance < amount:
                    findings.fail(
                        f"Sales Order {order} line {order_rows[detail]['item_code']} shows billed_amt "
                        f"{order_rows[detail]['billed_amt']}, below the {amount} on this invoice.",
                        sales_order=order,
                    )
    return findings.result(
        context,
        "The Sales Order lines show the billed amount.",
        details={"document": invoice["name"], "evidence": evidence("V", "8 step 5", "per_billed rose to 100 when the invoice was submitted.")},
    )


EVALUATORS: dict[str, Any] = {
    "sales_invoice.customer_resolves_exactly_one": checks.customer_resolves_exactly_one,
    "sales_invoice.customer_enabled": checks.customer_enabled,
    "sales_invoice.channel_allowed": checks.channel_allowed,
    "sales_invoice.customer_not_internal": checks.customer_not_internal,
    "sales_invoice.sales_order_resolves_exactly_one": sales_order_resolves_exactly_one,
    "sales_invoice.sales_order_billable": sales_order_billable,
    "sales_invoice.customer_matches_sales_order": customer_matches_sales_order,
    "sales_invoice.items_resolve_exactly_one": checks.items_resolve_exactly_one,
    "sales_invoice.brand_lines_stop_and_ask": shared.brand_lines_stop_and_ask,
    "sales_invoice.row_linkage_to_sales_order": row_linkage_to_sales_order,
    "sales_invoice.unbilled_quantity_available": unbilled_quantity_available,
    "sales_invoice.duplicate_billing_guard": duplicate_billing_guard,
    "sales_invoice.selling_price_list_valid": selling_price_list_valid,
    "sales_invoice.prices_match_sales_order": prices_match_sales_order,
    "sales_invoice.customer_addresses_resolved": shared.customer_addresses_resolved,
    "sales_invoice.gst_information_resolved": shared.gst_information_resolved,
    "sales_invoice.header_matches_sales_order": checks.header_matches_sales_order,
    "sales_invoice.tax_treatment_resolved": shared.tax_treatment_resolved,
    "sales_invoice.tax_inclusive_pricing": shared.tax_inclusive_pricing,
    "sales_invoice.gst_compliance_not_verified": gst_compliance_not_verified,
    "sales_invoice.forbidden_payload_fields": forbidden_payload_fields,
    "sales_invoice.draft_only_creation": checks.draft_only_creation,
    "sales_invoice.post_creation_readback": post_creation_readback,
    "sales_invoice.return_source_eligible": return_source_eligible,
    "sales_invoice.return_quantity_valid": return_quantity_valid,
    "sales_invoice.return_item_linkage": return_item_linkage,
    "sales_invoice.return_amounts_consistent": return_amounts_consistent,
    "sales_invoice.return_duplicate_guard": return_duplicate_guard,
    "sales_invoice.return_stock_and_outstanding_notice": return_stock_and_outstanding_notice,
    "sales_invoice.return_post_creation_verification": return_post_creation_verification,
    "sales_invoice.draft_ready_to_submit": draft_ready_to_submit,
    "sales_invoice.ledger_posting_verified": ledger_posting_verified,
    "sales_invoice.sales_order_billing_updated": sales_order_billing_updated,
}


def register_all(registry: EvaluatorRegistry) -> None:
    """Register every Sales Invoice and credit note evaluator explicitly."""
    for key, evaluator in EVALUATORS.items():
        registry.register(key, evaluator, description=(evaluator.__doc__ or "").strip())
