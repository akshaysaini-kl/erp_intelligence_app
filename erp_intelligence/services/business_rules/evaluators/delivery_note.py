"""Delivery Note evaluators.

Each evaluator implements one atomic responsibility from SALES_ORDER_TO_DELIVERY_GUIDE.md
(section 7, "Stage 3: create the Delivery Note"). In this business a Delivery Note is
always made from a submitted Pick List and keeps the exact row links to both parents
(1,186 of 1,186 Delivery Notes, guide section 1). The LLM has no tool that maps a Pick List
into a Delivery Note, so it builds the document by hand and these rules check the links.

Two rule sets use these evaluators:

* ``Delivery Note / Create`` checks the Draft payload and reads the created Draft back.
* ``Delivery Note / Submit`` checks the Draft before it is submitted (PRE_EXECUTION) and
  then verifies the effects of the submission on stock and on the source documents
  (POST_EXECUTION). Submitting is irreversible for the assistant, so it is never done as
  part of creation.

Delivery Note returns are not verified (guide, section 14) and have no rules here.
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
    to_decimal,
)
from erp_intelligence.services.business_rules.evaluators.sales_common import (
    SalesFindings,
    evidence,
    inclusive_totals,
    label,
    payload_rows,
    row_serials,
    sales_order_header,
    sales_order_rows,
    same_value,
    split_serials,
    truthy,
)
from erp_intelligence.services.business_rules.registry import EvaluatorRegistry

_ITEMS = "items"
_PICK_LIST_RULE = "delivery_note.pick_list_resolves_exactly_one"
_ORDER_RULE = "delivery_note.sales_order_resolves_exactly_one"


# ------------------------------------------------------------------- link lookups
def _single(context: EvaluationContext, field: str) -> tuple[str, str | None, set[str]]:
    """The single value of ``field`` over the item rows: ``(state, value, all_values)``."""
    rows = context.payload.get(_ITEMS)
    values: set[str] = set()
    blank = False
    for row in rows if isinstance(rows, list) else []:
        value = row.get(field) if isinstance(row, Mapping) else None
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
        else:
            blank = True
    if not values or blank:
        return "missing", None, values
    if len(values) > 1:
        return "multiple", None, values
    return "resolved", next(iter(values)), values


def _need_single(
    context: EvaluationContext, field: str, depends_on: str
) -> tuple[str | None, dict[str, Any] | None]:
    state, value, _all = _single(context, field)
    if state != "resolved":
        return None, context.needs_input(
            f"{field} is not resolved ({state}), so this rule cannot be evaluated.",
            [f"{_ITEMS}[].{field}"],
            {"depends_on": depends_on, "resolution": state},
        )
    return value, None


def _items(context: EvaluationContext) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    return payload_rows(context, _ITEMS, "Delivery Note")


def _pick_list_rows(context: EvaluationContext, name: str) -> dict[str, dict[str, Any]]:
    rows = context.data.fetch_child_records(
        "Pick List Item", "Pick List", [name],
        ["parent", "name", "idx", "item_code", "qty", "picked_qty", "warehouse", "batch_no",
         "serial_no", "serial_and_batch_bundle", "sales_order", "sales_order_item", "uom"],
    )
    return {row["name"]: row for row in rows}


def _order_rows(context: EvaluationContext, name: str) -> dict[str, dict[str, Any]]:
    return sales_order_rows(
        context, name, ["item_code", "qty", "delivered_qty", "uom", "rate", "price_list_rate", "custom_fulfilled_by"]
    )


# --------------------------------------------------------------- 1. Pick List
def pick_list_resolves_exactly_one(context: EvaluationContext) -> dict[str, Any]:
    """Every row names the same, existing Pick List. A Delivery Note comes from exactly one."""
    rows, dependency = _items(context)
    if dependency:
        return dependency
    state, value, values = _single(context, "against_pick_list")
    details = {
        "pick_lists": sorted(values),
        "evidence": evidence(
            "H",
            "1 / 7",
            "Every historical Delivery Note line (3,192 of 3,192) points to a Pick List, and no "
            "Delivery Note spans two Pick Lists (0 of 1,186).",
        ),
    }
    if state == "missing":
        return context.needs_input(
            "Every row needs against_pick_list. A Delivery Note is made from a Pick List.",
            [f"{_ITEMS}[].against_pick_list"],
            details,
        )
    if state == "multiple":
        return context.failed(
            f"The rows point to {len(values)} Pick Lists ({', '.join(sorted(values))}). A Delivery Note "
            "is made from exactly one Pick List.",
            details,
        )
    found = context.data.fetch_records("Pick List", ["name"], [("name", "=", value)])
    if not found:
        return context.failed(f"Pick List '{value}' does not exist or is not readable.", details)
    return context.passed(f"Pick List {value} is the single source of this Delivery Note.", {**details, "pick_list": value})


def pick_list_submitted_state(context: EvaluationContext) -> dict[str, Any]:
    """The Pick List must be submitted, sent for pickup and not yet fully delivered."""
    states = list(context.required_param("allowed_workflow_states"))
    statuses = list(context.required_param("allowed_statuses"))
    purposes = list(context.param("allowed_purposes", []))
    name, dependency = _need_single(context, "against_pick_list", _PICK_LIST_RULE)
    if dependency:
        return dependency
    rows = context.data.fetch_records("Pick List", ["name", "docstatus", "workflow_state", "status", "purpose"], [("name", "=", name)])
    if not rows:
        return context.failed(f"Pick List '{name}' does not exist or is not readable.")
    record = rows[0]
    findings = Findings()
    if record["docstatus"] != 1:
        findings.fail(f"Pick List {name} is not submitted (docstatus {record['docstatus']}). Send it for pickup first.")
    if record["workflow_state"] not in states:
        findings.fail(f"Pick List {name} is '{record['workflow_state']}'. Needed: {', '.join(states)}.")
    if record["status"] not in statuses:
        findings.fail(
            f"Pick List {name} status is '{record['status']}'. Needed: {', '.join(statuses)}. A "
            "Completed Pick List was already delivered."
        )
    if purposes and record["purpose"] not in purposes:
        findings.fail(f"Pick List {name} purpose is '{record['purpose']}'. Needed: {', '.join(purposes)}.")
    return findings.result(
        context,
        f"Pick List {name} is submitted and open for delivery.",
        details={
            "pick_list": name,
            "evidence": evidence(
                "V",
                "1.3 / 7",
                "A Delivery Note can be made as soon as the Pick List is submitted and sent for pickup. "
                "The later confirmed-picked state is used once in the history and nobody holds the role, "
                "so it is not required.",
            ),
        },
    )


def pick_list_fully_picked(context: EvaluationContext) -> dict[str, Any]:
    """Every Pick List row must have picked_qty equal to qty."""
    name, dependency = _need_single(context, "against_pick_list", _PICK_LIST_RULE)
    if dependency:
        return dependency
    pl_rows = _pick_list_rows(context, name)
    findings = Findings()
    if not pl_rows:
        findings.fail(f"Pick List {name} has no rows.")
    for row in pl_rows.values():
        picked, qty = to_decimal(row["picked_qty"]), to_decimal(row["qty"])
        if picked is None or qty is None or picked != qty:
            findings.fail(
                f"Pick List row {row['idx']} ({row['item_code']}) has picked_qty {row['picked_qty']} for "
                f"qty {row['qty']}. Do not deliver goods that were not picked.",
                row=row["idx"],
            )
    return findings.result(
        context,
        "Every Pick List row is fully picked.",
        details={
            "pick_list": name,
            "evidence": evidence("V", "7", "A Delivery Note built from a Pick List with picked_qty 0 fails: 'Quantity for Item X cannot be zero'."),
        },
    )


def pick_list_not_consolidated(context: EvaluationContext) -> dict[str, Any]:
    """A Pick List that belongs to a Consolidated Pick List is unverified territory."""
    field = context.required_param("consolidation_field")
    name, dependency = _need_single(context, "against_pick_list", _PICK_LIST_RULE)
    if dependency:
        return dependency
    rows = context.data.fetch_records("Pick List", ["name", field], [("name", "=", name)])
    if not rows:
        return context.failed(f"Pick List '{name}' does not exist or is not readable.")
    details = {
        "pick_list": name,
        "consolidated_pick_list": rows[0][field],
        "evidence": evidence("U", "6 'Consolidated Pick List'", "Consolidated Pick Lists were not tested."),
    }
    if is_blank(rows[0][field]):
        return context.passed("The Pick List is not part of a Consolidated Pick List.", details)
    key = "consolidated_pick_list_review"
    message = (
        f"Pick List {name} belongs to Consolidated Pick List {rows[0][field]}. This was never verified. "
        "Ask the user before delivering from it."
    )
    if context.is_confirmed(key):
        return context.warning(message + " The user chose to continue.", details)
    return context.needs_confirmation(message, [key], details)


# --------------------------------------------------------------- 2. Sales Order
def sales_order_resolves_exactly_one(context: EvaluationContext) -> dict[str, Any]:
    rows, dependency = _items(context)
    if dependency:
        return dependency
    state, value, values = _single(context, "against_sales_order")
    if state == "missing":
        return context.needs_input(
            "Every row needs against_sales_order.", [f"{_ITEMS}[].against_sales_order"]
        )
    if state == "multiple":
        return context.failed(
            f"The rows point to {len(values)} Sales Orders ({', '.join(sorted(values))}). One Delivery "
            "Note covers one Sales Order."
        )
    if sales_order_header(context, value, []) is None:
        return context.failed(f"Sales Order '{value}' does not exist or is not readable.")
    return context.passed(f"Sales Order {value} is the single source order.", {"sales_order": value})


def sales_order_eligible(context: EvaluationContext) -> dict[str, Any]:
    """The Sales Order must be approved, open for delivery, B2B and not internal."""
    approved = context.required_param("approved_workflow_state")
    deliverable = list(context.required_param("deliverable_statuses"))
    channels = list(context.required_param("allowed_channels"))
    name, dependency = _need_single(context, "against_sales_order", _ORDER_RULE)
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
    if header["status"] not in deliverable:
        findings.fail(f"Sales Order {name} status is '{header['status']}'. Needed: {', '.join(deliverable)}.")
    if header["custom_type"] not in channels:
        findings.fail(f"Sales Order {name} is channel '{header['custom_type']}'. Allowed: {', '.join(channels)}.")
    if truthy(header["is_internal_customer"]):
        findings.fail(f"Sales Order {name} is for an internal customer, which this process does not handle.")
    return findings.result(
        context, f"Sales Order {name} is approved and open for delivery.", details={"sales_order": name}
    )


def sales_order_matches_pick_list_and_customer(context: EvaluationContext) -> dict[str, Any]:
    """The Delivery Note, its Pick List and its Sales Order must all be about the same order and customer."""
    order, dependency = _need_single(context, "against_sales_order", _ORDER_RULE)
    if dependency:
        return dependency
    pick_list, dependency = _need_single(context, "against_pick_list", _PICK_LIST_RULE)
    if dependency:
        return dependency
    header = sales_order_header(context, order, ["customer", "company", "po_no"])
    if header is None:
        return context.failed(f"Sales Order '{order}' does not exist or is not readable.")
    findings = Findings()
    for field in ("customer", "company"):
        value = context.payload.get(field)
        if is_blank(value):
            findings.need(field, f"{field} is required and must equal the Sales Order's '{header[field]}'.")
        elif not same_value(value, header[field]):
            findings.fail(f"{field} '{value}' differs from Sales Order {order} ('{header[field]}').")
    pick_rows = _pick_list_rows(context, pick_list)
    foreign = sorted({row["sales_order"] for row in pick_rows.values() if row["sales_order"] != order})
    if foreign:
        findings.fail(f"Pick List {pick_list} also covers Sales Order(s) {', '.join(foreign)}, not only {order}.")
    if not pick_rows:
        findings.fail(f"Pick List {pick_list} has no rows.")
    return findings.result(
        context,
        "The Delivery Note, Pick List and Sales Order agree on order and customer.",
        details={"sales_order": order, "pick_list": pick_list},
    )


# ------------------------------------------------------------------ 3. row links
def row_linkage_exact(context: EvaluationContext) -> dict[str, Any]:
    """Each row keeps the exact link to its Sales Order line and its Pick List row.

    The links ``so_detail`` and ``pick_list_item`` are what update the Sales Order's
    delivered quantity and the Pick List's status when the Delivery Note is submitted
    (guide, section 7). A row without them delivers goods the order never hears about.
    """
    rows, dependency = _items(context)
    if dependency:
        return dependency
    order, dependency = _need_single(context, "against_sales_order", _ORDER_RULE)
    if dependency:
        return dependency
    pick_list, dependency = _need_single(context, "against_pick_list", _PICK_LIST_RULE)
    if dependency:
        return dependency
    order_rows, pick_rows = _order_rows(context, order), _pick_list_rows(context, pick_list)
    findings = Findings()
    used: dict[str, int] = {}
    for index, line in enumerate(rows):
        tag = label(_ITEMS, index)
        detail, source_row = line.get("so_detail"), line.get("pick_list_item")
        if is_blank(detail):
            findings.need(f"{tag}.so_detail", f"Row {index + 1} needs so_detail, the Sales Order line's row id.")
        if is_blank(source_row):
            findings.need(f"{tag}.pick_list_item", f"Row {index + 1} needs pick_list_item, the Pick List row's id.")
        if is_blank(detail) or is_blank(source_row):
            continue
        order_row, pick_row = order_rows.get(detail), pick_rows.get(source_row)
        if order_row is None:
            findings.fail(f"Row {index + 1}: so_detail '{detail}' is not a line of Sales Order {order}.", row=index + 1)
            continue
        if pick_row is None:
            findings.fail(f"Row {index + 1}: pick_list_item '{source_row}' is not a row of Pick List {pick_list}.", row=index + 1)
            continue
        code = line.get("item_code")
        if code != order_row["item_code"] or code != pick_row["item_code"]:
            findings.fail(
                f"Row {index + 1}: item {code} differs from the Sales Order line "
                f"({order_row['item_code']}) or the Pick List row ({pick_row['item_code']}).",
                row=index + 1,
            )
        if pick_row["sales_order_item"] != detail or pick_row["sales_order"] != order:
            findings.fail(
                f"Row {index + 1}: the Pick List row picks for {pick_row['sales_order']}/"
                f"{pick_row['sales_order_item']}, not {order}/{detail}.",
                row=index + 1,
            )
        used[source_row] = used.get(source_row, 0) + 1
    for source_row, count in used.items():
        if count > 1:
            findings.fail(f"Pick List row {source_row} is used by {count} rows. Each row is delivered once.")
    return findings.result(
        context,
        "Every row is linked to its Sales Order line and Pick List row.",
        details={
            "sales_order": order,
            "pick_list": pick_list,
            "evidence": evidence(
                "V",
                "7 step 3",
                "against_sales_order, so_detail, against_pick_list and pick_list_item together update the "
                "Sales Order (per_delivered) and the Pick List (status Completed).",
            ),
        },
    )


def quantities_match_pick_list(context: EvaluationContext) -> dict[str, Any]:
    """Each row delivers exactly what the Pick List row picked, and never more than the order still needs."""
    rows, dependency = _items(context)
    if dependency:
        return dependency
    order, dependency = _need_single(context, "against_sales_order", _ORDER_RULE)
    if dependency:
        return dependency
    pick_list, dependency = _need_single(context, "against_pick_list", _PICK_LIST_RULE)
    if dependency:
        return dependency
    order_rows, pick_rows = _order_rows(context, order), _pick_list_rows(context, pick_list)
    findings = SalesFindings()
    covered: set[str] = set()
    per_order_line: dict[str, Decimal] = {}
    for index, line in enumerate(rows):
        link, detail = line.get("pick_list_item"), line.get("so_detail")
        qty = to_decimal(line.get("qty"))
        if is_blank(link) or link not in pick_rows or is_blank(detail) or detail not in order_rows:
            findings.need(
                f"{label(_ITEMS, index)}.pick_list_item",
                f"Row {index + 1} has no resolved Pick List row or Sales Order line.",
                depends_on="delivery_note.row_linkage_exact",
            )
            continue
        if qty is None or qty <= 0:
            findings.fail(f"Row {index + 1} qty must be a positive number.", row=index + 1)
            continue
        covered.add(link)
        if qty != to_decimal(pick_rows[link]["qty"]):
            findings.fail(f"Row {index + 1}: qty {qty} differs from the Pick List's {pick_rows[link]['qty']}.", row=index + 1)
        per_order_line[detail] = per_order_line.get(detail, Decimal(0)) + qty
    for detail, qty in per_order_line.items():
        source = order_rows[detail]
        pending = (to_decimal(source["qty"]) or Decimal(0)) - (to_decimal(source["delivered_qty"]) or Decimal(0))
        if qty > pending:
            findings.fail(
                f"Item {source['item_code']}: delivering {qty} but only {pending} is still to deliver.",
                item_code=source["item_code"],
            )
    missing = sorted(row["idx"] for name, row in pick_rows.items() if name not in covered)
    if missing and not findings.has_failures:
        key = "partial_pick_list_delivery"
        message = (
            f"Pick List row(s) {', '.join(map(str, missing))} are not on this Delivery Note. Confirm a "
            "partial delivery of the Pick List."
        )
        if context.is_confirmed(key):
            findings.warn(message + " Confirmed by the user.", rows=missing)
        else:
            findings.confirm(key, message, rows=missing)
    return findings.result(
        context,
        "Quantities match the Pick List and stay within the order.",
        details={"evidence": evidence("V", "7 step 3", "Quantity must equal the Pick List quantity.")},
    )


def prices_match_sales_order(context: EvaluationContext) -> dict[str, Any]:
    """rate and price_list_rate must equal the Sales Order line. They are the same tax-inclusive price."""
    rows, dependency = _items(context)
    if dependency:
        return dependency
    order, dependency = _need_single(context, "against_sales_order", _ORDER_RULE)
    if dependency:
        return dependency
    order_rows = _order_rows(context, order)
    findings = Findings()
    for index, line in enumerate(rows):
        tag, detail = label(_ITEMS, index), line.get("so_detail")
        if is_blank(detail) or detail not in order_rows:
            findings.need(f"{tag}.so_detail", f"Row {index + 1} has no resolved Sales Order line.", depends_on="delivery_note.row_linkage_exact")
            continue
        source = order_rows[detail]
        for field in ("rate", "price_list_rate"):
            sent = line.get(field)
            if is_blank(sent):
                findings.need(f"{tag}.{field}", f"Row {index + 1} needs {field}, copied from the Sales Order line ({source[field]}).")
            elif not same_value(sent, source[field]):
                findings.fail(
                    f"Row {index + 1}: {field} {sent} differs from the Sales Order line's {source[field]}.",
                    row=index + 1,
                )
    return findings.result(
        context,
        "Prices equal the Sales Order lines.",
        details={"evidence": evidence("V", "7 step 3", "rate and price_list_rate must equal the Sales Order line (same tax-inclusive price).")},
    )


# ---------------------------------------------------- 4. warehouse, batch, stock
def warehouse_consistent_with_pick_list(context: EvaluationContext) -> dict[str, Any]:
    rows, dependency = _items(context)
    if dependency:
        return dependency
    pick_list, dependency = _need_single(context, "against_pick_list", _PICK_LIST_RULE)
    if dependency:
        return dependency
    pick_rows = _pick_list_rows(context, pick_list)
    findings = Findings()
    for index, line in enumerate(rows):
        link = line.get("pick_list_item")
        if is_blank(link) or link not in pick_rows:
            findings.need(f"{label(_ITEMS, index)}.pick_list_item", f"Row {index + 1} has no resolved Pick List row.", depends_on="delivery_note.row_linkage_exact")
            continue
        if is_blank(line.get("warehouse")):
            findings.need(f"{label(_ITEMS, index)}.warehouse", f"Row {index + 1} needs the rack warehouse of the Pick List row ({pick_rows[link]['warehouse']}).")
        elif line["warehouse"] != pick_rows[link]["warehouse"]:
            findings.fail(
                f"Row {index + 1}: warehouse {line['warehouse']} differs from the Pick List's {pick_rows[link]['warehouse']}.",
                row=index + 1,
            )
    return findings.result(
        context,
        "Every row delivers from the warehouse it was picked in.",
        details={"evidence": evidence("V", "7 step 3", "Stock leaves the rack warehouse (for example 'L3 - P15'), not the Sales Order's default warehouse.")},
    )


def batch_and_serial_match_pick_list(context: EvaluationContext) -> dict[str, Any]:
    """Batch and serial numbers must be those that were picked, and batch-tracked rows need a batch."""
    rows, dependency = _items(context)
    if dependency:
        return dependency
    pick_list, dependency = _need_single(context, "against_pick_list", _PICK_LIST_RULE)
    if dependency:
        return dependency
    pick_rows = _pick_list_rows(context, pick_list)
    picked_serials = row_serials(context, list(pick_rows.values()))
    codes = sorted({row["item_code"] for row in rows if isinstance(row.get("item_code"), str) and row["item_code"]})
    tracked = {row["name"]: row for row in context.data.fetch_by_names("Item", codes, ["name", "has_batch_no", "has_serial_no"])}
    findings = Findings()
    for index, line in enumerate(rows):
        tag, link = label(_ITEMS, index), line.get("pick_list_item")
        item = tracked.get(line.get("item_code"))
        if item is None or is_blank(link) or link not in pick_rows:
            findings.need(f"{tag}.pick_list_item", f"Row {index + 1} has no resolved item or Pick List row.", depends_on="delivery_note.row_linkage_exact")
            continue
        source = pick_rows[link]
        if truthy(item["has_batch_no"]) and is_blank(line.get("batch_no")):
            findings.need(f"{tag}.batch_no", f"Row {index + 1} is batch tracked and needs batch_no '{source['batch_no']}'.")
        elif not is_blank(line.get("batch_no")) and line["batch_no"] != source["batch_no"]:
            findings.fail(f"Row {index + 1}: batch_no {line['batch_no']} differs from the Pick List's {source['batch_no']}.", row=index + 1)
        if truthy(item["has_serial_no"]):
            if is_blank(line.get("serial_no")):
                findings.need(f"{tag}.serial_no", f"Row {index + 1} is serial tracked and needs the Pick List's serial numbers.")
            else:
                picked = picked_serials.get(link, [])
                if not picked:
                    findings.warn(
                        f"Row {index + 1}: the Pick List row lists no serial numbers, so those sent cannot be compared.",
                        row=index + 1,
                    )
                elif sorted(split_serials(line["serial_no"])) != sorted(picked):
                    findings.fail(f"Row {index + 1}: serial numbers differ from the Pick List row.", row=index + 1)
    return findings.result(
        context,
        "Batch and serial numbers are those that were picked.",
        details={"evidence": evidence("V", "7 step 3", "'The following items require a Batch Number but none was provided' for a batch-tracked row without batch_no.")},
    )


def stock_still_available(context: EvaluationContext) -> dict[str, Any]:
    return checks.stock_available(context)


def brand_lines_stop_and_ask(context: EvaluationContext) -> dict[str, Any]:
    return shared.brand_lines_stop_and_ask(context)


# ------------------------------------------------------------------ duplicates
def duplicate_delivery_guard(context: EvaluationContext) -> dict[str, Any]:
    """A Delivery Note may not deliver a Pick List row that another live Delivery Note already delivers."""
    live = list(context.required_param("live_docstatuses"))
    customer, po_no = context.payload.get("customer"), context.payload.get("po_no")
    rows, dependency = _items(context)
    if dependency:
        return dependency
    if is_blank(customer) or is_blank(po_no):
        return context.needs_input(
            "The customer and the customer PO number are required for the duplicate check.",
            [name for name, value in (("customer", customer), ("po_no", po_no)) if is_blank(value)],
        )
    mine = {row["pick_list_item"] for row in rows if not is_blank(row.get("pick_list_item"))}
    existing = context.data.fetch_records(
        "Delivery Note", ["name", "docstatus", "status"],
        [("customer", "=", customer), ("po_no", "=", po_no), ("docstatus", "in", live)],
    )
    clashes = []
    if existing:
        for row in context.data.fetch_child_records(
            "Delivery Note Item", "Delivery Note", [dn["name"] for dn in existing], ["parent", "pick_list_item"]
        ):
            if row["pick_list_item"] in mine:
                clashes.append({"delivery_note": row["parent"], "pick_list_item": row["pick_list_item"]})
    details = {
        "same_order_delivery_notes": [dn["name"] for dn in existing],
        "evidence": evidence(
            "V",
            "7 step 2",
            "Delivery Notes are found by customer and customer PO number, then their rows are "
            "compared. Counts only Delivery Notes this user may read.",
        ),
    }
    if clashes:
        return context.failed(
            f"{len({item['delivery_note'] for item in clashes})} live Delivery Note(s) already deliver "
            f"{len(clashes)} of these Pick List row(s). Show them to the user instead of creating another.",
            {**details, "clashes": clashes},
        )
    if existing:
        return context.warning(
            f"{len(existing)} other live Delivery Note(s) exist for this order. They do not overlap these "
            "rows, so this looks like a partial delivery. Tell the user.",
            details,
        )
    return context.passed("No other live Delivery Note exists for this order.", details)


# ------------------------------------------------------------ safety and read-back
def forbidden_payload_fields(context: EvaluationContext) -> dict[str, Any]:
    return shared.forbidden_payload_fields(context)


def post_creation_readback(context: EvaluationContext) -> dict[str, Any]:
    """Read the created Draft back: state, row links, prices, tax-inclusive totals."""
    name = context.execution_result.get("name")
    if is_blank(name) or not isinstance(name, str):
        return context.needs_input("The created document name is required to read it back.", ["execution_result.name"])
    initial = dict(context.param("expected_initial_state", {}))
    compare_fields = list(context.param("compare_fields", []))
    tolerance = to_decimal(context.param("amount_tolerance", "0.02")) or Decimal("0.02")
    header_fields = list(dict.fromkeys(["name", *initial, *compare_fields, "total", "total_taxes_and_charges", "grand_total"]))
    rows = context.data.fetch_records("Delivery Note", header_fields, [("name", "=", name)])
    details: dict[str, Any] = {
        "document": name,
        "evidence": evidence("V", "7 step 4", "Read-back must come from the system; an unreadable document is unverified, never correct."),
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
    sent = [row for row in (context.payload.get(_ITEMS) or []) if isinstance(row, Mapping)]
    read = sorted(
        context.data.fetch_child_records(
            "Delivery Note Item", "Delivery Note", [name],
            ["parent", "idx", "item_code", "qty", "uom", "warehouse", "batch_no", "serial_no", "rate",
             "price_list_rate", "against_sales_order", "so_detail", "against_pick_list", "pick_list_item"],
        ),
        key=lambda row: row["idx"],
    )
    if len(read) != len(sent):
        findings.fail(f"{len(read)} item row(s) were read back but {len(sent)} were sent.")
    for index, (row_sent, row_read) in enumerate(zip(sent, read)):
        for field in ("item_code", "qty", "uom", "warehouse", "batch_no", "serial_no", "rate", "price_list_rate",
                      "against_sales_order", "so_detail", "against_pick_list", "pick_list_item"):
            if field in row_sent and not same_value(row_read.get(field), row_sent[field]):
                findings.fail(f"{label(_ITEMS, index)}.{field} is {row_read.get(field)!r} but {row_sent[field]!r} was sent.", row=index + 1)
        for field in ("so_detail", "pick_list_item", "against_sales_order", "against_pick_list"):
            if is_blank(row_read.get(field)):
                findings.fail(f"{label(_ITEMS, index)}.{field} was not kept. The source documents would not be updated.", row=index + 1)
    sent_taxes = [row for row in (context.payload.get("taxes") or []) if isinstance(row, Mapping)]
    read_taxes = context.data.fetch_child_records(
        "Sales Taxes and Charges", "Delivery Note", [name], ["parent", "account_head", "rate", "included_in_print_rate"]
    )
    if sent_taxes and len(read_taxes) != len(sent_taxes):
        findings.fail(f"{len(read_taxes)} tax row(s) were read back but {len(sent_taxes)} were sent.")
    for row in read_taxes:
        if not truthy(row.get("included_in_print_rate")):
            findings.fail(f"Tax row {row['account_head']} is not tax-inclusive.")
    total = Decimal(0)
    for line in sent:
        qty, price = to_decimal(line.get("qty")), to_decimal(line.get("price_list_rate"))
        if qty is None or price is None:
            total = None
            break
        total += qty * price
    expected = inclusive_totals(total, sent_taxes) if total is not None else None
    if expected is not None:
        for field in ("total", "total_taxes_and_charges", "grand_total"):
            actual = to_decimal(header.get(field))
            if actual is None or abs(actual - expected[field]) > tolerance:
                findings.fail(f"{field} is {header.get(field)!r}, expected {expected[field].quantize(Decimal('0.01'))} (tax-inclusive).", field=field)
        details["expected_totals"] = expected
    else:
        findings.warn("Expected totals were not computed; the totals are unverified.")
    details["verification"] = "READ_BACK_COMPARED"
    return findings.result(context, f"{name} was read back and matches what was sent.", details=details)


# =========================================================================
# Delivery Note / Submit
# =========================================================================
def _document(context: EvaluationContext) -> tuple[str | None, dict[str, Any] | None]:
    name = context.payload.get("name")
    if is_blank(name) or not isinstance(name, str):
        return None, context.needs_input("The Delivery Note name is required.", ["name"])
    return name, None


def _dn_header_and_rows(context: EvaluationContext, name: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    header = context.data.fetch_records("Delivery Note", ["name", "docstatus", "status", "customer"], [("name", "=", name)])
    rows = context.data.fetch_child_records(
        "Delivery Note Item", "Delivery Note", [name],
        ["parent", "idx", "item_code", "qty", "warehouse", "so_detail", "against_sales_order", "against_pick_list", "pick_list_item"],
    )
    return (header[0] if header else None), sorted(rows, key=lambda row: row["idx"])


def draft_ready_to_submit(context: EvaluationContext) -> dict[str, Any]:
    """Before submitting: the document is a Draft with intact links, and stock is still there."""
    name, dependency = _document(context)
    if dependency:
        return dependency
    header, rows = _dn_header_and_rows(context, name)
    if header is None:
        return context.failed(f"Delivery Note '{name}' does not exist or is not readable.")
    findings = Findings()
    if header["docstatus"] != 0:
        findings.fail(f"Delivery Note {name} is not a Draft (docstatus {header['docstatus']}).")
    if not rows:
        findings.fail(f"Delivery Note {name} has no items.")
    for row in rows:
        for field in ("so_detail", "against_sales_order", "against_pick_list", "pick_list_item"):
            if is_blank(row.get(field)):
                findings.fail(f"Row {row['idx']} has no {field}. Submitting would not update the source documents.", row=row["idx"])
    on_hand = {}
    items = sorted({row["item_code"] for row in rows})
    if items:
        for bin_row in context.data.fetch_records("Bin", ["item_code", "warehouse", "actual_qty"], [("item_code", "in", items)]):
            on_hand[(bin_row["item_code"], bin_row["warehouse"])] = to_decimal(bin_row["actual_qty"]) or Decimal(0)
    needed: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        key = (row["item_code"], row["warehouse"])
        needed[key] = needed.get(key, Decimal(0)) + (to_decimal(row["qty"]) or Decimal(0))
    for (code, warehouse), qty in needed.items():
        if on_hand.get((code, warehouse), Decimal(0)) < qty:
            findings.fail(f"Item {code}: {qty} to deliver from {warehouse} but only {on_hand.get((code, warehouse), 0)} is on hand.", item_code=code)
    return findings.result(
        context,
        "The Draft is ready to submit.",
        details={
            "document": name,
            "notice": "Submitting posts a stock ledger entry out of the rack warehouse and cannot be undone by the assistant. Submit only on the user's explicit instruction.",
            "evidence": evidence("V", "7 step 5", "Submitting reduces stock, sets the Sales Order to To Bill and the Pick List to Completed."),
        },
    )


def stock_movement_verified(context: EvaluationContext) -> dict[str, Any]:
    """After submit: a stock ledger entry takes each delivered quantity out of its warehouse."""
    name, dependency = _document(context)
    if dependency:
        return dependency
    header, rows = _dn_header_and_rows(context, name)
    if header is None:
        return context.needs_input(f"Delivery Note '{name}' could not be read, so the stock movement is unverified.", ["name"])
    findings = Findings()
    if header["docstatus"] != 1:
        findings.fail(f"Delivery Note {name} is not submitted (docstatus {header['docstatus']}), so no stock moved.")
        return findings.result(context, "", details={"document": name})
    entries = context.data.fetch_records(
        "Stock Ledger Entry", ["voucher_no", "item_code", "warehouse", "actual_qty", "is_cancelled"],
        [("voucher_type", "=", "Delivery Note"), ("voucher_no", "=", name), ("is_cancelled", "=", 0)],
    )
    moved: dict[tuple[str, str], Decimal] = {}
    for entry in entries:
        key = (entry["item_code"], entry["warehouse"])
        moved[key] = moved.get(key, Decimal(0)) + (to_decimal(entry["actual_qty"]) or Decimal(0))
    expected: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        key = (row["item_code"], row["warehouse"])
        expected[key] = expected.get(key, Decimal(0)) - (to_decimal(row["qty"]) or Decimal(0))
    for key, qty in expected.items():
        if moved.get(key, Decimal(0)) != qty:
            findings.fail(
                f"Stock ledger moved {moved.get(key, Decimal(0))} of {key[0]} in {key[1]}, expected {qty}.",
                item_code=key[0],
                warehouse=key[1],
            )
    for key in moved:
        if key not in expected:
            findings.fail(f"Stock moved for {key[0]} in {key[1]}, which is not on the Delivery Note.")
    return findings.result(
        context,
        "The stock ledger shows every delivered quantity leaving its warehouse.",
        details={
            "document": name,
            "ledger_entries": len(entries),
            "evidence": evidence("V", "7 step 5", "A stock ledger entry of -2 left the rack warehouse in the test."),
        },
    )


def sales_order_progress_updated(context: EvaluationContext) -> dict[str, Any]:
    """After submit: the Sales Order lines show at least the delivered quantity."""
    name, dependency = _document(context)
    if dependency:
        return dependency
    header, rows = _dn_header_and_rows(context, name)
    if header is None or header["docstatus"] != 1:
        return context.needs_input(f"Delivery Note '{name}' is not readable or not submitted, so the Sales Order update is unverified.", ["name"])
    findings = Findings()
    orders = sorted({row["against_sales_order"] for row in rows if not is_blank(row.get("against_sales_order"))})
    if not orders:
        findings.fail("The Delivery Note rows have no against_sales_order, so the order cannot have been updated.")
    delivered: dict[str, Decimal] = {}
    for row in rows:
        if not is_blank(row.get("so_detail")):
            delivered[row["so_detail"]] = delivered.get(row["so_detail"], Decimal(0)) + (to_decimal(row["qty"]) or Decimal(0))
    for order in orders:
        order_rows = sales_order_rows(context, order, ["delivered_qty", "item_code"])
        for detail, qty in delivered.items():
            if detail in order_rows and (to_decimal(order_rows[detail]["delivered_qty"]) or Decimal(0)) < qty:
                findings.fail(
                    f"Sales Order {order} line {order_rows[detail]['item_code']} shows delivered_qty "
                    f"{order_rows[detail]['delivered_qty']}, below the {qty} on this Delivery Note.",
                    sales_order=order,
                )
    return findings.result(
        context,
        "The Sales Order lines show the delivered quantity.",
        details={"document": name, "evidence": evidence("V", "7 step 5", "Submitting set the Sales Order to To Bill and per_delivered 100.")},
    )


def pick_list_completed(context: EvaluationContext) -> dict[str, Any]:
    """After submit: the Pick List is Completed."""
    completed = context.required_param("completed_status")
    name, dependency = _document(context)
    if dependency:
        return dependency
    header, rows = _dn_header_and_rows(context, name)
    if header is None or header["docstatus"] != 1:
        return context.needs_input(f"Delivery Note '{name}' is not readable or not submitted, so the Pick List update is unverified.", ["name"])
    lists = sorted({row["against_pick_list"] for row in rows if not is_blank(row.get("against_pick_list"))})
    findings = Findings()
    if not lists:
        findings.fail("The Delivery Note rows have no against_pick_list, so no Pick List can have been updated.")
    for pick_list in lists:
        found = context.data.fetch_records("Pick List", ["name", "status"], [("name", "=", pick_list)])
        if not found:
            findings.need("against_pick_list", f"Pick List {pick_list} could not be read.")
        elif found[0]["status"] != completed:
            findings.fail(f"Pick List {pick_list} is '{found[0]['status']}', expected '{completed}'.")
    return findings.result(
        context,
        f"The Pick List is {completed}.",
        details={"document": name, "evidence": evidence("V", "7 step 5", "Submitting set the Pick List status to Completed.")},
    )


EVALUATORS: dict[str, Any] = {
    "delivery_note.customer_resolves_exactly_one": checks.customer_resolves_exactly_one,
    "delivery_note.customer_enabled": checks.customer_enabled,
    "delivery_note.channel_allowed": checks.channel_allowed,
    "delivery_note.customer_not_internal": checks.customer_not_internal,
    "delivery_note.pick_list_resolves_exactly_one": pick_list_resolves_exactly_one,
    "delivery_note.pick_list_submitted_state": pick_list_submitted_state,
    "delivery_note.pick_list_fully_picked": pick_list_fully_picked,
    "delivery_note.pick_list_not_consolidated": pick_list_not_consolidated,
    "delivery_note.sales_order_resolves_exactly_one": sales_order_resolves_exactly_one,
    "delivery_note.sales_order_eligible": sales_order_eligible,
    "delivery_note.sales_order_matches_pick_list_and_customer": sales_order_matches_pick_list_and_customer,
    "delivery_note.row_linkage_exact": row_linkage_exact,
    "delivery_note.quantities_match_pick_list": quantities_match_pick_list,
    "delivery_note.prices_match_sales_order": prices_match_sales_order,
    "delivery_note.warehouse_consistent_with_pick_list": warehouse_consistent_with_pick_list,
    "delivery_note.batch_and_serial_match_pick_list": batch_and_serial_match_pick_list,
    "delivery_note.stock_still_available": stock_still_available,
    "delivery_note.brand_lines_stop_and_ask": brand_lines_stop_and_ask,
    "delivery_note.customer_addresses_resolved": shared.customer_addresses_resolved,
    "delivery_note.gst_information_resolved": shared.gst_information_resolved,
    "delivery_note.header_matches_sales_order": checks.header_matches_sales_order,
    "delivery_note.tax_treatment_resolved": shared.tax_treatment_resolved,
    "delivery_note.tax_inclusive_pricing": shared.tax_inclusive_pricing,
    "delivery_note.duplicate_delivery_guard": duplicate_delivery_guard,
    "delivery_note.forbidden_payload_fields": forbidden_payload_fields,
    "delivery_note.draft_only_creation": checks.draft_only_creation,
    "delivery_note.post_creation_readback": post_creation_readback,
    "delivery_note.draft_ready_to_submit": draft_ready_to_submit,
    "delivery_note.stock_movement_verified": stock_movement_verified,
    "delivery_note.sales_order_progress_updated": sales_order_progress_updated,
    "delivery_note.pick_list_completed": pick_list_completed,
}


def register_all(registry: EvaluatorRegistry) -> None:
    """Register every Delivery Note evaluator explicitly."""
    for key, evaluator in EVALUATORS.items():
        registry.register(key, evaluator, description=(evaluator.__doc__ or "").strip())
