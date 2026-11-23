"""Evaluators that the Sales Order, Delivery Note and Sales Invoice rule sets share.

The logic is written once here and registered under one key per DocType
(``sales_order.tax_treatment_resolved``, ``delivery_note.tax_treatment_resolved``,
``sales_invoice.tax_treatment_resolved``). Each DocType keeps its own rule and its own
parameters. Nothing here names a site value: tax templates, state codes, categories and
field names are rule parameters.

None of these evaluators claims GST compliance. ERPNext does not validate the GST tax
type, the place of supply or missing addresses on these documents (guide, section 8), so
a PASS only means the payload agrees with the documented rules.
"""
import re
from collections.abc import Mapping
from typing import Any

from erp_intelligence.services.business_rules.context import EvaluationContext
from erp_intelligence.services.business_rules.evaluators.common import (
    Findings,
    is_blank,
    same_number,
)
from erp_intelligence.services.business_rules.evaluators.sales_common import (
    GST_NOT_VERIFIED,
    MAX_CANDIDATES,
    customer_record,
    evidence,
    label,
    normalized_tax_row,
    sales_order_rows,
    state_code,
    truthy,
)

_PLACE_OF_SUPPLY = re.compile(r"^\d{2}-\S.*$")
TAX_TEMPLATE_DOCTYPE = "Sales Taxes and Charges Template"
TAX_ROW_DOCTYPE = "Sales Taxes and Charges"


# ------------------------------------------------------------------------ addresses
def customer_addresses_resolved(context: EvaluationContext) -> dict[str, Any]:
    """Billing and shipping addresses must be sent, and must belong to the customer.

    The server picks an address by itself when none is sent, and the one it picked was a
    Shipping address used as the billing address (guide, section 2.2). A registered
    customer therefore needs both addresses sent explicitly. A marketplace customer
    without addresses needs none.
    """
    taxed = list(context.required_param("taxed_gst_categories"))
    untaxed = list(context.param("untaxed_gst_categories", []))
    billing_type = context.param("billing_address_type", None)
    shipping_type = context.param("shipping_address_type", None)
    record, dependency = customer_record(context, ["gst_category"], "the customer")
    if dependency:
        return dependency
    customer, category = record["name"], record["gst_category"]
    findings = Findings()
    details: dict[str, Any] = {
        "customer": customer,
        "gst_category": category,
        "evidence": evidence(
            "V",
            "4 step 4 / 2.2",
            "With no address sent the server chose a Shipping address as the billing address "
            "for a registered customer. Address ownership is not checked by the server.",
        ),
    }
    if category not in taxed and category not in untaxed:
        findings.need(
            "tax_treatment",
            f"Customer GST category '{category}' has no documented address or tax treatment. "
            "Ask the user.",
        )
        return findings.result(context, "", details=details)
    required = category in taxed
    fields = (
        ("customer_address", billing_type, "billing"),
        ("shipping_address_name", shipping_type, "shipping"),
    )
    candidates: list[dict[str, Any]] | None = None
    for field, expected_type, role in fields:
        value = context.payload.get(field)
        if is_blank(value):
            if required:
                if candidates is None:
                    candidates = context.data.fetch_records(
                        "Address",
                        ["name", "address_type"],
                        [("address_title", "like", f"%{customer}%")],
                        page_length=MAX_CANDIDATES,
                        require_complete=False,
                    )
                    details["candidate_addresses"] = [
                        {"name": row["name"], "address_type": row.get("address_type")}
                        for row in candidates
                    ]
                hint = (
                    "Ask the user which site receives the goods."
                    if candidates
                    else "The customer has no address on record. Ask the user."
                )
                findings.need(field, f"The {role} address ({field}) is required. {hint}")
            continue
        rows = context.data.fetch_records(
            "Address", ["name", "address_type", "disabled"], [("name", "=", value)]
        )
        if not rows:
            findings.fail(f"Address '{value}' ({field}) does not exist or is not readable.")
            continue
        if truthy(rows[0]["disabled"]):
            findings.fail(f"Address '{value}' ({field}) is disabled.")
        links = context.data.fetch_child_records(
            "Dynamic Link", "Address", [value], ["parent", "link_doctype", "link_name"]
        )
        if not any(
            row["link_doctype"] == "Customer" and row["link_name"] == customer for row in links
        ):
            findings.fail(f"Address '{value}' ({field}) is not linked to customer {customer}.")
        if expected_type and rows[0]["address_type"] != expected_type:
            findings.warn(
                f"Address '{value}' ({field}) is of type {rows[0]['address_type']}, not "
                f"{expected_type}.",
                field=field,
            )
    return findings.result(
        context,
        "Addresses are resolved." if required else "No address is required for this customer.",
        details=details,
    )


# ------------------------------------------------------------------------------ GST
def gst_information_resolved(context: EvaluationContext) -> dict[str, Any]:
    """The place of supply must agree with the customer's GSTIN state.

    This is a consistency check against the documented rule. It is not GST compliance:
    a place of supply that contradicts the GSTIN saved and its invoice submitted
    (guide, section 4 step 4 and section 8).
    """
    taxed = list(context.required_param("taxed_gst_categories"))
    untaxed = list(context.param("untaxed_gst_categories", []))
    unregistered_place = context.param("unregistered_place_of_supply", None)
    record, dependency = customer_record(context, ["gst_category", "gstin"], "the customer")
    if dependency:
        return dependency
    category, gstin = record["gst_category"], record["gstin"] or ""
    place = context.payload.get("place_of_supply")
    details: dict[str, Any] = {
        "customer": record["name"],
        "gst_category": category,
        "place_of_supply": place,
        "notice": GST_NOT_VERIFIED,
        "evidence": evidence(
            "V",
            "4 step 4 / 8",
            "ERPNext accepted a place of supply that contradicts the customer's GSTIN, a wrong "
            "tax type and missing addresses, and the invoice submitted. This rule checks the "
            "documented consistency only.",
        ),
    }
    findings = Findings()
    if category not in taxed and category not in untaxed:
        findings.need(
            "tax_treatment",
            f"Customer GST category '{category}' has no documented treatment. Ask the user.",
        )
        return findings.result(context, "", details=details)
    if is_blank(place):
        findings.need("place_of_supply", "place_of_supply is required. The server does not fill it in.")
        return findings.result(context, "", details=details)
    if not isinstance(place, str) or not _PLACE_OF_SUPPLY.match(place.strip()):
        findings.fail(
            f"place_of_supply '{place}' must look like 'NN-State name', for example '29-Karnataka'."
        )
        return findings.result(context, "", details=details)
    place_state = state_code(place)
    details["place_of_supply_state_code"] = place_state
    if category in taxed:
        gstin_state = gstin[:2]
        if len(gstin) < 2 or not gstin_state.isdigit():
            findings.fail(f"Customer {record['name']} has no usable GSTIN, so its state is unknown.")
        else:
            details["gstin_state_code"] = gstin_state
            if gstin_state != place_state:
                findings.fail(
                    f"place_of_supply state {place_state} differs from the customer GSTIN state "
                    f"{gstin_state}. The system would not stop this."
                )
    elif not is_blank(unregistered_place) and place.strip() != unregistered_place:
        findings.warn(
            f"An unregistered customer is normally supplied with place_of_supply "
            f"'{unregistered_place}'. '{place}' was sent."
        )
    return findings.result(
        context, "The place of supply agrees with the customer's GSTIN state.", details=details
    )


# ------------------------------------------------------------------------------ tax
def tax_treatment_resolved(context: EvaluationContext) -> dict[str, Any]:
    """The tax template must follow the state comparison, and the rows must match the template.

    Prices are tax-inclusive, so the place-of-supply state is compared with the company
    state. A template name alone leaves the tax table empty on a Delivery Note and a
    Sales Invoice, so the rows must be in the payload (guide, section 2.2).
    """
    supported = list(context.required_param("supported_gst_categories"))
    company_state = str(context.required_param("company_state_code"))
    compare_fields = list(context.required_param("row_compare_fields"))
    reverse_templates = list(context.param("reverse_charge_templates", []))
    record, dependency = customer_record(context, ["gst_category"], "the customer")
    if dependency:
        return dependency
    payload = context.payload
    template = payload.get("taxes_and_charges")
    rows = payload.get("taxes")
    rows = rows if isinstance(rows, list) else []
    details: dict[str, Any] = {
        "customer": record["name"],
        "gst_category": record["gst_category"],
        "taxes_and_charges": template,
        "notice": GST_NOT_VERIFIED,
        "evidence": evidence(
            "V",
            "4 step 4 / 7 step 3",
            "The server does not check the tax type against the states and creates no tax rows "
            "on a Delivery Note or Sales Invoice from a template name.",
        ),
    }
    findings = Findings()
    if record["gst_category"] not in supported:
        findings.need(
            "tax_treatment",
            f"Customer GST category '{record['gst_category']}' has no documented tax treatment. "
            "Ask the user.",
        )
        return findings.result(context, "", details=details)
    place_state = state_code(payload.get("place_of_supply"))
    if place_state is None:
        findings.need("place_of_supply", "place_of_supply is required to choose the tax template.")
        return findings.result(context, "", details=details)
    details.update(place_of_supply_state_code=place_state, company_state_code=company_state)
    confirm_codes = [str(code) for code in context.param("confirmation_state_codes", [])]
    if place_state in confirm_codes:
        state_key = f"state_code_confirmed:{place_state}"
        state_message = (
            f"State code {place_state} is one the guide lists as unclear tax treatment. Ask the user "
            "to confirm the tax treatment before continuing."
        )
        if context.is_confirmed(state_key):
            findings.warn(state_message + " Confirmed by the user.")
        else:
            findings.confirm(state_key, state_message)
    if not is_blank(template) and template in reverse_templates:
        key = "reverse_charge_confirmed"
        if context.is_confirmed(key):
            findings.warn(f"Reverse-charge template '{template}' was confirmed by the user.")
        else:
            findings.confirm(
                key,
                f"Template '{template}' is a reverse-charge template. Use it only if the user asked.",
            )
    else:
        expected = context.required_param(
            "intra_state_template" if place_state == company_state else "inter_state_template"
        )
        details["expected_template"] = expected
        if is_blank(template):
            findings.fail(f"A tax template is required. Expected '{expected}'.")
        elif template != expected:
            findings.fail(
                f"Tax template '{template}' does not match the state comparison (place of supply "
                f"{place_state}, company {company_state}). Expected '{expected}'."
            )
    if is_blank(template):
        return findings.result(context, "", details=details)
    template_rows = context.data.fetch_records(
        TAX_TEMPLATE_DOCTYPE, ["name", "disabled"], [("name", "=", template)]
    )
    if not template_rows:
        findings.fail(f"Tax template '{template}' does not exist or is not readable.")
        return findings.result(context, "", details=details)
    if truthy(template_rows[0]["disabled"]):
        findings.fail(f"Tax template '{template}' is disabled.")
    source_rows = context.data.fetch_child_records(
        TAX_ROW_DOCTYPE, TAX_TEMPLATE_DOCTYPE, [template], ["parent", *compare_fields, "description"]
    )
    expected_rows = sorted(normalized_tax_row(row, compare_fields) for row in source_rows)
    actual_rows = sorted(
        normalized_tax_row(row, compare_fields) for row in rows if isinstance(row, Mapping)
    )
    details.update(template_rows=expected_rows, payload_rows=actual_rows)
    if not source_rows:
        findings.fail(f"Tax template '{template}' has no tax rows.")
    elif not rows:
        findings.need(
            "taxes", f"Send the tax rows of template '{template}'. A template name alone is not enough."
        )
    elif actual_rows != expected_rows:
        findings.fail(
            f"The payload tax rows do not match template '{template}' on {', '.join(compare_fields)}."
        )
    return findings.result(context, "Tax treatment follows the documented rule.", details=details)


def tax_inclusive_pricing(context: EvaluationContext) -> dict[str, Any]:
    """Every tax row must say the price already includes the tax (``included_in_print_rate`` 1)."""
    required = context.param("required_included_in_print_rate", 1)
    rows = context.payload.get("taxes")
    if not isinstance(rows, list) or not rows:
        return context.needs_input("The tax rows are required to check tax-inclusive pricing.", ["taxes"])
    findings = Findings()
    deviating = []
    structural_failure = False
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            findings.fail(f"{label('taxes', index)} is not an object.")
            structural_failure = True
            continue
        if is_blank(row.get("description")):
            findings.fail(f"{label('taxes', index)} has no description (the server rejects it).")
            structural_failure = True
        included = row.get("included_in_print_rate")
        if is_blank(included):
            findings.fail(
                f"{label('taxes', index)} must send included_in_print_rate explicitly. "
                f"Prices are tax-inclusive, so it must be {required}."
            )
            structural_failure = True
        elif not same_number(included, required):
            deviating.append(index)
    if deviating and not structural_failure:
        key = "tax_exclusive_pricing_confirmed"
        message = (
            f"Tax row(s) {', '.join(str(i) for i in deviating)} send included_in_print_rate other "
            f"than {required}. Prices here are tax-inclusive. Confirm tax-exclusive pricing."
        )
        if context.is_confirmed(key):
            findings.warn(message + " Confirmed by the user.")
        else:
            findings.confirm(key, message, rows=deviating)
    return findings.result(
        context,
        "Pricing is tax-inclusive on every tax row.",
        details={
            "evidence": evidence(
                "H",
                "0 rule 9",
                "2,098 of 2,104 submitted Sales Orders carry tax-inclusive rows. Totals are "
                "tax-inclusive: grand total equals the item total.",
            )
        },
    )


# --------------------------------------------------------------------- payload safety
def forbidden_payload_fields(context: EvaluationContext) -> dict[str, Any]:
    """Reject server-owned fields and execution options that bypass the approval workflow.

    ``item_tables`` names the child tables to scan (``items``, or ``locations`` on a Pick
    List). ``allowed_fields`` exempts fields that a forbidden prefix would otherwise catch:
    the three Sales Invoice total fields FAC demands are an integration workaround, not a
    business decision, and the server recalculates them.
    """
    header_fields = set(context.required_param("forbidden_fields"))
    header_prefixes = tuple(context.param("forbidden_prefixes", []))
    item_fields = set(context.param("forbidden_item_fields", []))
    item_prefixes = tuple(context.param("forbidden_item_prefixes", []))
    item_tables = list(context.param("item_tables", ["items"]))
    allowed = set(context.param("allowed_fields", []))
    truthy_fields = list(context.param("forbidden_truthy_fields", []))
    option_flags = list(context.param("forbidden_truthy_options", []))

    def offenders(row: Mapping[str, Any], names: set[str], prefixes: tuple[str, ...]) -> list[str]:
        return sorted(
            key
            for key in row
            if key not in allowed
            and (key in names or (prefixes and isinstance(key, str) and key.startswith(prefixes)))
        )

    findings = Findings()
    bad_header = offenders(context.payload, header_fields, header_prefixes)
    if bad_header:
        findings.fail(
            f"Forbidden header field(s) in the payload: {', '.join(bad_header)}.", fields=bad_header
        )
    for table in item_tables:
        rows = context.payload.get(table)
        for index, line in enumerate(rows if isinstance(rows, list) else []):
            if isinstance(line, Mapping):
                bad = offenders(line, item_fields, item_prefixes)
                if bad:
                    findings.fail(
                        f"Forbidden field(s) in {label(table, index)}: {', '.join(bad)}.",
                        row=index + 1,
                        fields=bad,
                    )
    for field in truthy_fields:
        if truthy(context.payload.get(field)):
            findings.fail(
                f"The field '{field}' must not be set: the documented process does not use it.",
                field=field,
            )
    for flag in option_flags:
        if truthy(context.execution_options.get(flag)):
            findings.fail(
                f"The execution option '{flag}' must not be used: it bypasses the approval "
                "workflow or is not a reliable gate.",
                option=flag,
            )
    return findings.result(
        context,
        "The payload contains no forbidden fields or execution options.",
        details={
            "evidence": evidence(
                "V",
                "0 rule 2 / 2.2 / 4 step 7",
                "submit, docstatus and submit_document skip the approval workflow on Sales Orders "
                "and Pick Lists. validate_only is not a reliable gate. This rule cannot stop a "
                "caller that does not evaluate first: that gap is in the execution layer.",
            )
        },
    )


# ------------------------------------------------------------------------ Brand lines
def brand_lines_stop_and_ask(context: EvaluationContext) -> dict[str, Any]:
    """A Brand-fulfilled line needs the user's decision before it is ordered, picked, delivered or billed.

    The system does not stop it: on a B2B order a Brand line has ``delivered_by_supplier``
    0, and the pick, delivery and invoice paths accept it (guide, section 3.2). ``source``
    says where the fulfilment value comes from: ``item`` reads the Item master (a new
    Sales Order); ``sales_order`` reads the Sales Order line the row points to, because
    the Item master changed after most historical orders.
    """
    source = context.required_param("source")
    table = context.param("item_table", "items")
    field = context.required_param("fulfilment_field")
    brand = context.required_param("brand_value")
    rows = context.payload.get(table)
    if not isinstance(rows, list) or not rows or not all(isinstance(row, Mapping) for row in rows):
        return context.needs_input(f"{table} is required to check fulfilment.", [table])
    findings = Findings()
    fulfilment: dict[int, tuple[str, Any]] = {}
    if source == "item":
        codes = [row["item_code"] for row in rows if isinstance(row.get("item_code"), str)]
        found = {
            row["name"]: row
            for row in context.data.fetch_by_names("Item", codes, ["name", field])
        }
        for index, row in enumerate(rows):
            code = row.get("item_code")
            if code in found:
                fulfilment[index] = (code, found[code][field])
            else:
                findings.need(
                    f"{label(table, index)}.item_code",
                    f"Item '{code}' is not resolved, so its fulfilment cannot be read.",
                )
    else:
        order_field = context.required_param("order_field")
        row_field = context.required_param("row_field")
        cache: dict[str, dict[str, dict[str, Any]]] = {}
        for index, row in enumerate(rows):
            order, line = row.get(order_field), row.get(row_field)
            if is_blank(order) or is_blank(line):
                findings.need(
                    f"{label(table, index)}.{row_field}",
                    f"Row {index + 1} has no {order_field}/{row_field}, so its fulfilment cannot be read.",
                )
                continue
            if order not in cache:
                cache[order] = sales_order_rows(context, order, ["item_code", field])
            source_row = cache[order].get(line)
            if source_row is None:
                findings.need(
                    f"{label(table, index)}.{row_field}",
                    f"Row {index + 1} points to a Sales Order line that was not found.",
                )
                continue
            fulfilment[index] = (source_row["item_code"], source_row[field])
    for index, (code, value) in fulfilment.items():
        if value == brand:
            key = f"brand_line:{code}"
            message = (
                f"Row {index + 1} ({code}) is {brand} fulfilled. The system does not stop it. "
                "Ask the user how this line is handled."
            )
            if context.is_confirmed(key):
                findings.warn(message + " The user decided to keep it.", item_code=code)
            else:
                findings.confirm(key, message, item_code=code, row=index + 1)
    return findings.result(
        context,
        "No Brand-fulfilled line is present.",
        details={
            "fulfilment_by_row": {str(i): v[1] for i, v in fulfilment.items()},
            "evidence": evidence(
                "V",
                "3.2",
                "custom_fulfilled_by is overwritten from the Item master on save. Brand lines on a "
                "B2B order are accepted by the pick, delivery and invoice paths.",
            ),
        },
    )
