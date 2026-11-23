"""More evaluators that several Sales and Fulfilment rule sets share.

``sales_shared`` holds the address, GST, tax and payload-safety evaluators. This module
holds the customer, channel, item, draft-only, stock and order-header evaluators. Each
function is one atomic responsibility, registered by each DocType module under its own
key (``sales_order.customer_enabled``, ``delivery_note.customer_enabled`` ...).

Nothing here names a site value; they are parameters.
"""
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from erp_intelligence.services.business_rules.context import EvaluationContext
from erp_intelligence.services.business_rules.evaluators.common import (
    Findings,
    is_blank,
    parse_iso_date,
    same_number,
    to_decimal,
)
from erp_intelligence.services.business_rules.evaluators.sales_common import (
    MAX_CANDIDATES,
    customer_record,
    evidence,
    label,
    payload_rows,
    resolve_customer,
    same_value,
    truthy,
)

DEPENDS_ON_CUSTOMER = "the customer"


# ---------------------------------------------------------------------- customer
def customer_resolves_exactly_one(context: EvaluationContext) -> dict[str, Any]:
    state, name, candidates = resolve_customer(context)
    supplied = context.payload.get("customer")
    if state == "resolved":
        return context.passed("Customer resolves to exactly one record.", {"customer": name})
    if state == "missing":
        return context.needs_input("The customer is required.", ["customer"])
    if state == "not_found":
        return context.failed(f"No Customer matches '{supplied}'.", {"customer": supplied})
    return context.needs_input(
        f"'{supplied}' is not an exact Customer ID. Choose one candidate and use its ID.",
        ["customer"],
        {
            "customer": supplied,
            "candidates": [
                {"name": row["name"], "customer_name": row.get("customer_name")} for row in candidates
            ],
        },
    )


def customer_enabled(context: EvaluationContext) -> dict[str, Any]:
    record, dependency = customer_record(context, ["disabled"], DEPENDS_ON_CUSTOMER)
    if dependency:
        return dependency
    if truthy(record["disabled"]):
        return context.failed(f"Customer {record['name']} is disabled.", {"customer": record["name"]})
    return context.passed(f"Customer {record['name']} is enabled.", {"customer": record["name"]})


def channel_allowed(context: EvaluationContext) -> dict[str, Any]:
    """Only the listed channels may be handled. B2C (the CS-Cart channel) is refused.

    The guide forbids creating B2C orders by hand: that channel is created by the
    integration and its manual creation was never verified (guide, section 0 rule 4).
    ``refused_customers`` lists customers that belong to a refused channel.
    """
    allowed = list(context.required_param("allowed_channels"))
    refused_customers = list(context.param("refused_customers", []))
    channel = context.payload.get("custom_type")
    customer = context.payload.get("customer")
    details = {
        "custom_type": channel,
        "allowed_channels": allowed,
        "evidence": evidence(
            "U",
            "0 rule 4",
            "B2C and CS-Cart orders could not be exercised on the site (no CS-Cart customer or "
            "price list), so the guide refuses them.",
        ),
    }
    if is_blank(channel):
        return context.needs_input("custom_type is required. Send it explicitly.", ["custom_type"], details)
    if channel not in allowed:
        return context.failed(
            f"Channel '{channel}' is not handled by this process. Allowed: {', '.join(allowed)}. "
            "Stop and tell the user.",
            details,
        )
    if isinstance(customer, str) and customer in refused_customers:
        return context.failed(
            f"Customer '{customer}' belongs to a channel this process does not handle. Stop and "
            "tell the user.",
            details,
        )
    return context.passed(f"Channel {channel} is handled by this process.", details)


def customer_not_internal(context: EvaluationContext) -> dict[str, Any]:
    record, dependency = customer_record(context, ["is_internal_customer"], DEPENDS_ON_CUSTOMER)
    if dependency:
        return dependency
    details = {
        "customer": record["name"],
        "evidence": evidence(
            "U",
            "0 rule 4",
            "Internal-customer orders (stock transfers with transit warehouses) were not verified.",
        ),
    }
    if truthy(record["is_internal_customer"]):
        return context.failed(
            f"Customer {record['name']} is an internal customer. Internal orders are not handled "
            "by this process. Stop and tell the user.",
            details,
        )
    return context.passed(f"Customer {record['name']} is not an internal customer.", details)


def selling_price_list_for_customer(context: EvaluationContext) -> dict[str, Any]:
    """The selling price list must be the customer's own list, and must be a usable selling list."""
    record, dependency = customer_record(context, ["default_price_list"], DEPENDS_ON_CUSTOMER)
    if dependency:
        return dependency
    expected = record["default_price_list"] or None
    chosen = context.payload.get("selling_price_list")
    details = {
        "customer": record["name"],
        "customer_default_price_list": expected,
        "selling_price_list": chosen,
        "evidence": evidence(
            "H",
            "3.1 / 4 step 6",
            "Every historical Sales Order uses the customer's default price list.",
        ),
    }
    findings = Findings()
    if is_blank(chosen):
        hint = (
            f"The customer's price list is '{expected}'."
            if expected
            else "The customer has no default price list; ask which one to use."
        )
        findings.need("selling_price_list", f"selling_price_list is required. {hint}")
        return findings.result(context, "", details=details)
    key = "selling_price_list_override"
    if chosen != expected:
        reason = (
            "The customer has no default price list."
            if not expected
            else f"It differs from the customer's price list '{expected}'."
        )
        if context.is_confirmed(key):
            findings.warn(f"Price list '{chosen}' was confirmed by the user. {reason}")
        else:
            findings.confirm(key, f"Confirm using price list '{chosen}'. {reason}")
    rows = context.data.fetch_records(
        "Price List", ["name", "enabled", "selling", "currency"], [("name", "=", chosen)]
    )
    if not rows:
        findings.fail(f"Price List '{chosen}' does not exist or is not readable.")
    else:
        details["price_list_currency"] = rows[0]["currency"]
        if not truthy(rows[0]["enabled"]):
            findings.fail(f"Price List '{chosen}' is disabled.")
        if not truthy(rows[0]["selling"]):
            findings.fail(f"Price List '{chosen}' is not a selling price list.")
    return findings.result(
        context, f"Price List '{chosen}' is available for this customer.", details=details
    )


def currency_and_exchange_rate(context: EvaluationContext) -> dict[str, Any]:
    """Currency must match the selling price list. A foreign currency is not a verified process."""
    payload = context.payload
    findings = Findings()
    for name in ("company", "selling_price_list", "currency", "conversion_rate", "price_list_currency", "plc_conversion_rate"):
        if is_blank(payload.get(name)):
            findings.need(name, f"{name} is required (the assistant tool refuses a create without it).")
    details: dict[str, Any] = {
        "currency": payload.get("currency"),
        "conversion_rate": payload.get("conversion_rate"),
        "evidence": evidence(
            "U",
            "3 / 14",
            "Every verified Sales Order, Delivery Note and invoice is in the company currency. "
            "Foreign-currency sales were not exercised.",
        ),
    }
    list_currency = company_currency = None
    price_list, company = payload.get("selling_price_list"), payload.get("company")
    if not is_blank(price_list):
        rows = context.data.fetch_records("Price List", ["name", "currency"], [("name", "=", price_list)])
        if rows:
            list_currency = rows[0]["currency"]
            details["price_list_currency_on_record"] = list_currency
        else:
            findings.fail(f"Price List '{price_list}' does not exist or is not readable.")
    if not is_blank(company):
        rows = context.data.fetch_records("Company", ["name", "default_currency"], [("name", "=", company)])
        if rows:
            company_currency = rows[0]["default_currency"]
            details["company_currency"] = company_currency
        else:
            findings.fail(f"Company '{company}' does not exist or is not readable.")
    currency = payload.get("currency")
    if list_currency and not is_blank(currency) and currency != list_currency:
        findings.fail(
            f"Currency {currency} differs from the selling price list currency {list_currency}.",
            expected_currency=list_currency,
        )
    if not is_blank(payload.get("price_list_currency")) and list_currency:
        if payload["price_list_currency"] != list_currency:
            findings.fail(
                f"price_list_currency {payload['price_list_currency']} differs from the price list "
                f"currency {list_currency}."
            )
    if company_currency and not is_blank(currency) and not is_blank(payload.get("conversion_rate")):
        rate = to_decimal(payload["conversion_rate"])
        plc_rate = to_decimal(payload.get("plc_conversion_rate"))
        if rate is None or rate <= 0:
            findings.fail("conversion_rate must be a positive number.")
        elif currency == company_currency:
            if rate != 1:
                findings.fail(
                    f"The currency is the company currency {company_currency}, so conversion_rate must be 1."
                )
            if plc_rate is not None and plc_rate != 1 and payload.get("price_list_currency") == company_currency:
                findings.fail("plc_conversion_rate must be 1 for a price list in the company currency.")
        else:
            key = "foreign_currency_sales_confirmed"
            message = (
                f"Currency {currency} is not the company currency {company_currency}. Foreign-currency "
                "sales are not part of the verified process and the system will not fetch a rate. "
                f"Confirm conversion_rate {rate}."
            )
            if context.is_confirmed(key):
                findings.warn(message + " Confirmed by the user.")
            else:
                findings.confirm(key, message)
    return findings.result(context, "Currency and exchange rate are consistent.", details=details)


# --------------------------------------------------------------------------- items
def items_resolve_exactly_one(context: EvaluationContext) -> dict[str, Any]:
    table = context.param("item_table", "items")
    rows, dependency = payload_rows(context, table, "document")
    if dependency:
        return dependency
    codes = [row["item_code"] for row in rows if isinstance(row.get("item_code"), str) and row["item_code"]]
    found = {row["name"]: row for row in context.data.fetch_by_names("Item", codes, ["name", "item_name"])}
    findings = Findings()
    for index, line in enumerate(rows):
        code = line.get("item_code")
        if is_blank(code) or not isinstance(code, str):
            findings.need(f"{label(table, index)}.item_code", f"Row {index + 1} has no item_code.")
        elif code not in found:
            candidates = context.data.fetch_records(
                "Item",
                ["name", "item_name"],
                [("item_name", "like", f"%{code}%")],
                page_length=MAX_CANDIDATES,
                require_complete=False,
            )
            if candidates:
                findings.need(
                    f"{label(table, index)}.item_code",
                    f"'{code}' is not an exact Item code. Choose one candidate.",
                    candidates=[
                        {"name": row["name"], "item_name": row.get("item_name")} for row in candidates
                    ],
                )
            else:
                findings.fail(f"No Item matches '{code}'.", row=index + 1, item_code=code)
    return findings.result(
        context, "Every item resolves to exactly one Item.", details={"resolved_items": sorted(found)}
    )


def items_enabled_and_saleable(context: EvaluationContext) -> dict[str, Any]:
    """Disabled and expired items fail. Whether the system enforces ``is_sales_item`` is unverified."""
    rows, dependency = payload_rows(context, "items", "Sales Order")
    if dependency:
        return dependency
    codes = [row["item_code"] for row in rows if isinstance(row.get("item_code"), str) and row["item_code"]]
    found = {
        row["name"]: row
        for row in context.data.fetch_by_names(
            "Item", codes, ["name", "disabled", "end_of_life", "is_sales_item"]
        )
    }
    today = parse_iso_date(context.today)
    findings = Findings()
    for index, line in enumerate(rows):
        code = line.get("item_code")
        if code not in found:
            findings.need(
                f"{label('items', index)}.item_code",
                f"Item '{code}' is not resolved, so this rule cannot be evaluated for row {index + 1}.",
                depends_on="items_resolve_exactly_one",
            )
            continue
        item = found[code]
        if truthy(item["disabled"]):
            findings.fail(f"Item {code} is disabled.", item_code=code)
        end_of_life = parse_iso_date(item["end_of_life"])
        if end_of_life is not None and end_of_life <= today:
            findings.fail(
                f"Item {code} reached its end of life on {end_of_life.isoformat()}.", item_code=code
            )
        if not truthy(item["is_sales_item"]):
            findings.warn(
                f"Item {code} is not marked as a sales item. Whether the system enforces this was "
                "not verified. Tell the user.",
                item_code=code,
            )
    return findings.result(
        context,
        "Every item is enabled and not past its end of life.",
        details={
            "evidence": evidence(
                "U",
                "4 step 5",
                "Disabled items are rejected by the system. Enforcement of is_sales_item on a Sales "
                "Order was not tested, so a non-sales item is reported as a warning, not a failure.",
            )
        },
    )


def items_quantity_and_uom(context: EvaluationContext) -> dict[str, Any]:
    """Quantity must be positive, whole where the UOM requires, in a defined UOM, with the right factor."""
    table = context.param("item_table", "items")
    rows, dependency = payload_rows(context, table, "document")
    if dependency:
        return dependency
    codes = [row["item_code"] for row in rows if isinstance(row.get("item_code"), str) and row["item_code"]]
    found = {
        row["name"]: row for row in context.data.fetch_by_names("Item", codes, ["name", "stock_uom"])
    }
    conversions: dict[tuple[str, str], Decimal] = {}
    if found:
        for row in context.data.fetch_child_records(
            "UOM Conversion Detail", "Item", list(found), ["parent", "uom", "conversion_factor"]
        ):
            factor = to_decimal(row["conversion_factor"])
            if factor is not None:
                conversions[(row["parent"], row["uom"])] = factor
    used = sorted({row["uom"] for row in rows if isinstance(row.get("uom"), str) and row["uom"]})
    uom_master = {
        row["name"]: row
        for row in context.data.fetch_by_names("UOM", used, ["name", "must_be_whole_number"])
    }
    findings = Findings()
    for index, line in enumerate(rows):
        tag = label(table, index)
        code = line.get("item_code")
        if code not in found:
            findings.need(
                f"{tag}.item_code",
                f"Item '{code}' is not resolved, so this rule cannot be evaluated for row {index + 1}.",
                depends_on="items_resolve_exactly_one",
            )
            continue
        if is_blank(line.get("qty")):
            findings.need(f"{tag}.qty", f"Row {index + 1} ({code}) has no qty.")
            continue
        qty = to_decimal(line.get("qty"))
        if qty is None or qty <= 0:
            findings.fail(f"Row {index + 1} ({code}) qty must be a positive number.", item_code=code)
            continue
        uom = line.get("uom")
        if is_blank(uom):
            findings.need(f"{tag}.uom", f"Row {index + 1} ({code}) has no uom.")
            continue
        stock_uom = found[code]["stock_uom"]
        supplied_factor = line.get("conversion_factor")
        if uom == stock_uom:
            if not is_blank(supplied_factor) and not same_number(supplied_factor, 1):
                findings.fail(
                    f"Row {index + 1} ({code}) uses the stock UOM, so conversion_factor must be 1.",
                    item_code=code,
                )
        elif (code, uom) in conversions:
            factor = conversions[(code, uom)]
            if is_blank(supplied_factor):
                findings.need(
                    f"{tag}.conversion_factor",
                    f"Row {index + 1} ({code}) uses UOM {uom}; send its conversion_factor {factor}.",
                )
            elif not same_number(supplied_factor, factor):
                findings.fail(
                    f"Row {index + 1} ({code}) conversion_factor {supplied_factor} does not match the "
                    f"Item's {factor} for UOM {uom}.",
                    item_code=code,
                )
        else:
            findings.fail(
                f"UOM {uom} is not defined for Item {code} (stock UOM {stock_uom}). The server would "
                "store a conversion factor of 1.",
                item_code=code,
                uom=uom,
            )
            continue
        master = uom_master.get(uom)
        if master is None:
            findings.fail(f"UOM {uom} does not exist or is not readable.", uom=uom)
        elif truthy(master["must_be_whole_number"]) and qty != qty.to_integral_value():
            findings.fail(
                f"Row {index + 1} ({code}) qty {qty} must be a whole number for UOM {uom}.",
                item_code=code,
            )
    return findings.result(
        context,
        "Quantities satisfy the UOM constraints.",
        details={
            "evidence": evidence(
                "V",
                "4 step 5",
                "A fraction on a whole-number UOM is rejected by the system. A UOM the item does not "
                "define is stored with factor 1 and a wrong stock quantity.",
            )
        },
    )


# --------------------------------------------------------------------- draft only
def draft_only_creation(context: EvaluationContext) -> dict[str, Any]:
    """A document may only be created as a Draft. Submit and approval bypass fields are refused.

    On a Sales Order and a Pick List this protects the approval workflow: ``submit``,
    ``docstatus`` and ``submit_document`` skip it (guide, section 0 rule 2). On a Delivery
    Note and a Sales Invoice there is no workflow, but submitting posts stock and ledger
    entries, so it needs its own explicit instruction (rule 3).

    Limits, stated plainly: this rule judges the payload and options that the caller shows
    it. It cannot stop a caller that evaluates one payload and executes another, or that
    does not evaluate at all. That gap is in the execution layer (FAC), not here.
    """
    fields = list(context.required_param("forbidden_payload_fields"))
    options = list(context.required_param("forbidden_truthy_options"))
    draft_states = dict(context.param("allowed_values", {"docstatus": [0]}))
    findings = Findings()
    for field in fields:
        if field in context.payload and not is_blank(context.payload[field]):
            allowed = draft_states.get(field)
            if allowed is not None and any(same_value(context.payload[field], value) for value in allowed):
                continue
            findings.fail(
                f"The field '{field}' is set to {context.payload[field]!r}. Only a Draft may be "
                "created; moving the document forward is a separate, explicit step.",
                field=field,
            )
    for flag in options:
        if truthy(context.execution_options.get(flag)):
            findings.fail(
                f"The execution option '{flag}' must not be used. It submits the document in the "
                "same call and skips the explicit user instruction (or the approval workflow).",
                option=flag,
            )
    return findings.result(
        context,
        "The request creates a Draft only.",
        details={
            "evidence": evidence(
                "V",
                "0 rules 2 and 3 / 2.2",
                "A Sales Manager without the Sales Approver role took a Sales Order to Approved with "
                "submit, docstatus or submit_document. This rule reads the payload and options only.",
            )
        },
    )


# ---------------------------------------------------------------------------- stock
def stock_available(context: EvaluationContext) -> dict[str, Any]:
    """Each row's quantity must be on hand in its warehouse, summed per item and warehouse.

    Reads ``Bin.actual_qty``. The system also checks stock when a Pick List is sent for
    pickup or a Delivery Note is submitted (guide, sections 6 and 7), so this is an early
    warning of a refusal, not the only protection.
    """
    table = context.required_param("item_table")
    qty_field = context.param("qty_field", "qty")
    rows, dependency = payload_rows(context, table, "document")
    if dependency:
        return dependency
    required: dict[tuple[str, str], Decimal] = {}
    findings = Findings()
    for index, line in enumerate(rows):
        code, warehouse = line.get("item_code"), line.get("warehouse")
        qty = to_decimal(line.get(qty_field))
        if is_blank(code) or is_blank(warehouse):
            findings.need(
                f"{label(table, index)}.warehouse",
                f"Row {index + 1} needs an item_code and a warehouse to check stock.",
            )
            continue
        if qty is None or qty <= 0:
            findings.need(f"{label(table, index)}.{qty_field}", f"Row {index + 1} ({code}) has no valid quantity.")
            continue
        required[(code, warehouse)] = required.get((code, warehouse), Decimal(0)) + qty
    on_hand: dict[tuple[str, str], Decimal] = {}
    items = sorted({code for code, _warehouse in required})
    if items:
        for row in context.data.fetch_records(
            "Bin", ["item_code", "warehouse", "actual_qty"], [("item_code", "in", items)]
        ):
            on_hand[(row["item_code"], row["warehouse"])] = to_decimal(row["actual_qty"]) or Decimal(0)
    shortages = []
    for (code, warehouse), qty in required.items():
        available = on_hand.get((code, warehouse), Decimal(0))
        if available < qty:
            shortages.append({"item_code": code, "warehouse": warehouse, "required": qty, "available": available})
            findings.fail(
                f"Item {code}: {qty} required in {warehouse} but only {available} is on hand. Tell the "
                "user the shortage. Do not pick from another item or reduce the quantity yourself.",
                item_code=code,
                warehouse=warehouse,
                required=qty,
                available=available,
            )
    return findings.result(
        context,
        "Every row has enough stock on hand in its warehouse.",
        details={
            "shortages": shortages,
            "evidence": evidence(
                "V",
                "6 step 3 / 11",
                "The system refuses a short Pick List at Submit & Send: 'the Available qty A is less "
                "than the Required Qty R in the warehouse W'. Reserved stock in other open Pick "
                "Lists is not subtracted here.",
            ),
        },
    )


# -------------------------------------------------------------- header consistency
def header_matches_sales_order(context: EvaluationContext) -> dict[str, Any]:
    """Header facts must be copied from the Sales Order the document is built from.

    A difference is a stop-and-ask, not a failure: the guide says to copy these values
    from the Sales Order, and a document that disagrees with its order needs the user's say.
    ``order_field`` is the payload key that names the Sales Order on the first row's link.
    """
    fields = list(context.required_param("compare_fields"))
    order = _single_sales_order(context)
    if order is None:
        return context.needs_input(
            "The Sales Order is not resolved, so the header cannot be compared with it.",
            ["items.sales_order"],
        )
    header = context.data.fetch_records("Sales Order", ["name", *fields], [("name", "=", order)])
    if not header:
        return context.needs_input(f"Sales Order {order} is not readable.", ["items.sales_order"])
    findings = Findings()
    differing = []
    for field in fields:
        if field in context.payload and not same_value(context.payload[field], header[0].get(field)):
            differing.append(field)
    if differing:
        key = "header_differs_from_sales_order"
        message = (
            f"{', '.join(differing)} differ(s) from Sales Order {order}. Copy these values from the "
            "order unless the user wants them different."
        )
        if context.is_confirmed(key):
            findings.warn(message + " Confirmed by the user.", fields=differing)
        else:
            findings.confirm(key, message, fields=differing, sales_order=order)
    missing = [field for field in fields if is_blank(context.payload.get(field)) and not is_blank(header[0].get(field))]
    if missing:
        findings.need(missing[0], f"{', '.join(missing)} are set on Sales Order {order} but missing here.")
    return findings.result(
        context,
        f"The header agrees with Sales Order {order}.",
        details={"sales_order": order, "compared_fields": fields},
    )


def _single_sales_order(context: EvaluationContext) -> str | None:
    """The one Sales Order named by the payload's rows, or None when absent or several."""
    link_fields = list(context.param("order_link_fields", ["sales_order", "against_sales_order"]))
    tables = list(context.param("item_tables", ["items"]))
    names: set[str] = set()
    for table in tables:
        rows = context.payload.get(table)
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, Mapping):
                for field in link_fields:
                    value = row.get(field)
                    if isinstance(value, str) and value.strip():
                        names.add(value.strip())
    return next(iter(names)) if len(names) == 1 else None
