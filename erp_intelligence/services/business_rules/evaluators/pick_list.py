"""Pick List evaluators.

Each evaluator implements one atomic responsibility from SALES_ORDER_TO_DELIVERY_GUIDE.md
(section 6, "Stage 2: create the Pick List"). The LLM has no tool that maps a Sales Order
into a Pick List, so it builds ``locations`` by hand. These rules check that hand-built
payload against the order and against stock.

Scope. The system enforces several checks itself when a Pick List is sent for pickup
(picked quantity, batch numbers, one Sales Order, stock). They are evaluated here as
well, on the Draft payload, so the caller learns of a refusal before it happens. They
do not replace the system's checks.

Unresolved validation requirements (guide, section 14) are reported as evidence with
tag ``U`` and never turned into silent policy: whether over-picking is stopped when stock
exists, and whether one serial number can sit on two Draft Pick Lists.
"""
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from erp_intelligence.services.business_rules.context import EvaluationContext
from erp_intelligence.services.business_rules.errors import RuleDataError
from erp_intelligence.services.business_rules.evaluators import sales_shared as shared
from erp_intelligence.services.business_rules.evaluators import sales_shared_checks as checks
from erp_intelligence.services.business_rules.evaluators.common import (
    Findings,
    is_blank,
    parse_iso_date,
    to_decimal,
)
from erp_intelligence.services.business_rules.evaluators.sales_common import (
    SalesFindings,
    evidence,
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

_ORDER_RULE = "pick_list.sales_order_resolves_exactly_one"
_LOCATIONS = "locations"


# ------------------------------------------------------------------ order lookup
def _order(context: EvaluationContext) -> tuple[str, str | None, set[str]]:
    """The Sales Order the payload's locations point to: ``(state, name, all_names)``.

    state is ``missing``, ``multiple`` or ``resolved``. A row without a ``sales_order``
    makes the whole set ``missing``.
    """
    rows = context.payload.get(_LOCATIONS)
    names: set[str] = set()
    blank = False
    for row in rows if isinstance(rows, list) else []:
        value = row.get("sales_order") if isinstance(row, Mapping) else None
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
        else:
            blank = True
    if not names or blank:
        return "missing", None, names
    if len(names) > 1:
        return "multiple", None, names
    return "resolved", next(iter(names)), names


def _need_order(context: EvaluationContext) -> tuple[str | None, dict[str, Any] | None]:
    state, name, _names = _order(context)
    if state != "resolved":
        return None, context.needs_input(
            f"The Sales Order is not resolved ({state}), so this rule cannot be evaluated.",
            [f"{_LOCATIONS}[].sales_order"],
            {"depends_on": _ORDER_RULE, "resolution": state},
        )
    return name, None


def _locations(context: EvaluationContext) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    return payload_rows(context, _LOCATIONS, "Pick List")


# ------------------------------------------------------------------ 1. Sales Order
def sales_order_resolves_exactly_one(context: EvaluationContext) -> dict[str, Any]:
    """Every location names the same, existing Sales Order. One Pick List covers one order."""
    rows, dependency = _locations(context)
    if dependency:
        return dependency
    state, name, names = _order(context)
    details = {
        "sales_orders": sorted(names),
        "evidence": evidence(
            "V",
            "6 step 2",
            "A Pick List with locations from two Sales Orders is refused at Submit & Send: "
            "'This Pick List is linked to 2 Sales Orders. Please verify before submitting.'",
        ),
    }
    if state == "missing":
        return context.needs_input(
            "Every location needs the sales_order it picks for.", [f"{_LOCATIONS}[].sales_order"], details
        )
    if state == "multiple":
        return context.failed(
            f"The locations point to {len(names)} Sales Orders ({', '.join(sorted(names))}). One Pick "
            "List covers exactly one Sales Order. Create one Pick List per order.",
            details,
        )
    if sales_order_header(context, name, []) is None:
        return context.failed(f"Sales Order '{name}' does not exist or is not readable.", details)
    return context.passed(f"Sales Order {name} is the single source of this Pick List.", {**details, "sales_order": name})


def sales_order_approved(context: EvaluationContext) -> dict[str, Any]:
    """The Sales Order must be approved, submitted and still have goods to deliver."""
    approved = context.required_param("approved_workflow_state")
    pickable = list(context.required_param("pickable_statuses"))
    name, dependency = _need_order(context)
    if dependency:
        return dependency
    header = sales_order_header(context, name, ["docstatus", "workflow_state", "status"])
    if header is None:
        return context.failed(f"Sales Order '{name}' does not exist or is not readable.")
    findings = Findings()
    if header["docstatus"] != 1:
        findings.fail(f"Sales Order {name} is not submitted (docstatus {header['docstatus']}).")
    if header["workflow_state"] != approved:
        findings.fail(
            f"Sales Order {name} workflow state is '{header['workflow_state']}', not '{approved}'."
        )
    if header["status"] not in pickable:
        findings.fail(
            f"Sales Order {name} status is '{header['status']}'. A Pick List needs one of: "
            f"{', '.join(pickable)}."
        )
    return findings.result(
        context,
        f"Sales Order {name} is approved and open for delivery.",
        details={
            "sales_order": name,
            "workflow_state": header["workflow_state"],
            "status": header["status"],
            "evidence": evidence("V", "5 step 4", "An approved order reads docstatus 1, Approved, To Deliver and Bill."),
        },
    )


def sales_order_channel_allowed(context: EvaluationContext) -> dict[str, Any]:
    """The Sales Order must be of an allowed channel and not an internal-customer order."""
    allowed = list(context.required_param("allowed_channels"))
    name, dependency = _need_order(context)
    if dependency:
        return dependency
    header = sales_order_header(context, name, ["custom_type", "is_internal_customer"])
    if header is None:
        return context.failed(f"Sales Order '{name}' does not exist or is not readable.")
    findings = Findings()
    if header["custom_type"] not in allowed:
        findings.fail(
            f"Sales Order {name} is channel '{header['custom_type']}'. Allowed: {', '.join(allowed)}. "
            "Stop and tell the user."
        )
    if truthy(header["is_internal_customer"]):
        findings.fail(f"Sales Order {name} is for an internal customer, which this process does not handle.")
    return findings.result(
        context,
        "The Sales Order is a B2B, non-internal order.",
        details={
            "sales_order": name,
            "evidence": evidence("U", "0 rule 4", "B2C and internal-customer fulfilment was not verified."),
        },
    )


def header_consistent_with_sales_order(context: EvaluationContext) -> dict[str, Any]:
    """customer, company, channel and purpose must agree with the Sales Order."""
    purposes = list(context.param("allowed_purposes", []))
    name, dependency = _need_order(context)
    if dependency:
        return dependency
    header = sales_order_header(context, name, ["customer", "company", "custom_type"])
    if header is None:
        return context.failed(f"Sales Order '{name}' does not exist or is not readable.")
    findings = Findings()
    for field in ("customer", "company", "custom_type"):
        value = context.payload.get(field)
        if is_blank(value):
            findings.need(field, f"{field} is required and must equal the Sales Order's '{header[field]}'.")
        elif not same_value(value, header[field]):
            findings.fail(f"{field} '{value}' differs from Sales Order {name} ('{header[field]}').")
    purpose = context.payload.get("purpose")
    if purposes and purpose not in purposes:
        findings.fail(f"purpose '{purpose}' must be one of: {', '.join(purposes)}.")
    return findings.result(context, f"The Pick List header agrees with Sales Order {name}.", details={"sales_order": name})


# --------------------------------------------------------------- 2. order rows
def _order_item_rows(context: EvaluationContext, name: str) -> dict[str, dict[str, Any]]:
    return sales_order_rows(
        context, name, ["item_code", "qty", "delivered_qty", "uom", "custom_fulfilled_by", "warehouse"]
    )


def location_rows_linked(context: EvaluationContext) -> dict[str, Any]:
    """Each location points to a Sales Order line for the same item and unit."""
    rows, dependency = _locations(context)
    if dependency:
        return dependency
    name, dependency = _need_order(context)
    if dependency:
        return dependency
    order_rows = _order_item_rows(context, name)
    findings = Findings()
    for index, line in enumerate(rows):
        tag = label(_LOCATIONS, index)
        link = line.get("sales_order_item")
        if is_blank(link):
            findings.need(f"{tag}.sales_order_item", f"Row {index + 1} needs the Sales Order line it picks for (its row id).")
            continue
        source = order_rows.get(link)
        if source is None:
            findings.fail(f"Row {index + 1}: '{link}' is not a line of Sales Order {name}.", row=index + 1)
            continue
        if line.get("item_code") != source["item_code"]:
            findings.fail(
                f"Row {index + 1}: item {line.get('item_code')} differs from the Sales Order line's {source['item_code']}.",
                row=index + 1,
            )
        if not is_blank(line.get("uom")) and line["uom"] != source["uom"]:
            findings.fail(
                f"Row {index + 1}: uom {line['uom']} differs from the Sales Order line's {source['uom']}.",
                row=index + 1,
            )
    return findings.result(
        context,
        "Every location is linked to a matching Sales Order line.",
        details={
            "sales_order": name,
            "evidence": evidence(
                "V", "6 step 4", "sales_order_item is the Sales Order line's row id, read with get_document."
            ),
        },
    )


def quantity_within_pending(context: EvaluationContext) -> dict[str, Any]:
    """Picked quantity per Sales Order line may not exceed what is still to deliver."""
    rows, dependency = _locations(context)
    if dependency:
        return dependency
    name, dependency = _need_order(context)
    if dependency:
        return dependency
    order_rows = _order_item_rows(context, name)
    totals: dict[str, Decimal] = {}
    findings = Findings()
    for index, line in enumerate(rows):
        link, qty = line.get("sales_order_item"), to_decimal(line.get("qty"))
        if is_blank(link) or link not in order_rows:
            findings.need(
                f"{label(_LOCATIONS, index)}.sales_order_item",
                f"Row {index + 1} has no resolved Sales Order line.",
                depends_on="pick_list.location_rows_linked",
            )
            continue
        if qty is None or qty <= 0:
            findings.fail(f"Row {index + 1} qty must be a positive number.", row=index + 1)
            continue
        totals[link] = totals.get(link, Decimal(0)) + qty
    pending: dict[str, Any] = {}
    for link, qty in totals.items():
        source = order_rows[link]
        remaining = (to_decimal(source["qty"]) or Decimal(0)) - (to_decimal(source["delivered_qty"]) or Decimal(0))
        pending[link] = {"item_code": source["item_code"], "ordered": source["qty"], "delivered": source["delivered_qty"], "pending": remaining, "picking": qty}
        if qty > remaining:
            findings.fail(
                f"Item {source['item_code']}: picking {qty} but only {remaining} is still to deliver on "
                f"Sales Order {name}.",
                item_code=source["item_code"],
            )
    return findings.result(
        context,
        "No line is picked beyond its pending quantity.",
        details={
            "pending_by_line": pending,
            "evidence": evidence(
                "U",
                "6 step 1",
                "Whether the system stops over-picking when stock exists was not tested. The guide "
                "says never to pick more than the pending quantity, so this rule does.",
            ),
        },
    )


def brand_lines_stop_and_ask(context: EvaluationContext) -> dict[str, Any]:
    return shared.brand_lines_stop_and_ask(context)


# ----------------------------------------------------- 3. duplicate and excess
def _live_pick_lists(
    context: EvaluationContext, extra_filters: list[tuple[str, str, Any]]
) -> list[dict[str, Any]]:
    statuses = list(context.required_param("live_statuses"))
    filters = [("docstatus", "in", list(context.required_param("live_docstatuses"))), ("status", "in", statuses), *extra_filters]
    return context.data.fetch_records("Pick List", ["name", "status", "workflow_state", "customer", "docstatus"], filters)


def no_duplicate_or_excess_picking(context: EvaluationContext) -> dict[str, Any]:
    """Another live Pick List for the same Sales Order line needs the user's review."""
    rows, dependency = _locations(context)
    if dependency:
        return dependency
    name, dependency = _need_order(context)
    if dependency:
        return dependency
    customer = context.payload.get("customer")
    if is_blank(customer):
        return context.needs_input("customer is required for the duplicate check.", ["customer"])
    order_rows = _order_item_rows(context, name)
    live = _live_pick_lists(context, [("customer", "=", customer)])
    others: dict[str, list[dict[str, Any]]] = {}
    if live:
        for row in context.data.fetch_child_records(
            "Pick List Item", "Pick List", [pl["name"] for pl in live],
            ["parent", "sales_order", "sales_order_item", "qty"], [("sales_order", "=", name)],
        ):
            others.setdefault(row["sales_order_item"], []).append(row)
    this_qty: dict[str, Decimal] = {}
    for line in rows:
        link = line.get("sales_order_item")
        if not is_blank(link):
            this_qty[link] = this_qty.get(link, Decimal(0)) + (to_decimal(line.get("qty")) or Decimal(0))
    findings = SalesFindings()
    review = []
    for link, existing in others.items():
        if link not in this_qty:
            continue
        source = order_rows.get(link)
        taken = sum((to_decimal(row["qty"]) or Decimal(0)) for row in existing)
        review.append({"sales_order_item": link, "pick_lists": sorted({row["parent"] for row in existing}), "qty_on_live_pick_lists": taken})
        if source is not None:
            remaining = (to_decimal(source["qty"]) or Decimal(0)) - (to_decimal(source["delivered_qty"]) or Decimal(0))
            if taken + this_qty[link] > remaining:
                findings.fail(
                    f"Item {source['item_code']}: {taken} is already on live Pick List(s) "
                    f"{', '.join(sorted({row['parent'] for row in existing}))} and {this_qty[link]} more "
                    f"would exceed the {remaining} still to deliver.",
                    item_code=source["item_code"],
                )
    if review and not findings.has_failures:
        key = "duplicate_pick_list_review"
        message = (
            f"A live Pick List already covers {len(review)} line(s) of Sales Order {name}. Show it to "
            "the user and ask whether to continue."
        )
        if context.is_confirmed(key):
            findings.warn(message + " The user chose to continue.", existing=review)
        else:
            findings.confirm(key, message, existing=review)
    return findings.result(
        context,
        "No live Pick List already covers these lines.",
        details={
            "existing": review,
            "evidence": evidence(
                "V",
                "6 step 2",
                "Child rows cannot be filtered directly, so live Pick Lists of the customer are read "
                "and then their rows. Counts only Pick Lists this user may read.",
            ),
        },
    )


def serials_not_on_other_pick_lists(context: EvaluationContext) -> dict[str, Any]:
    """A serial number may not sit on two live Pick Lists."""
    rows, dependency = _locations(context)
    if dependency:
        return dependency
    mine: dict[str, str] = {}
    for line in rows:
        for serial in split_serials(line.get("serial_no")):
            mine[serial] = line.get("item_code") or ""
    details = {
        "serials_checked": len(mine),
        "evidence": evidence(
            "U",
            "6 step 3",
            "The guide says to exclude serials already on another live Pick List. Whether the system "
            "stops two Draft Pick Lists holding the same serial was not tested.",
        ),
    }
    if not mine:
        return context.passed("The payload names no serial numbers, so none can be on another Pick List.", details)
    live = _live_pick_lists(context, [])
    clashes = []
    if live:
        other_rows = context.data.fetch_child_records(
            "Pick List Item", "Pick List", [pl["name"] for pl in live],
            ["parent", "name", "item_code", "serial_no", "serial_and_batch_bundle"],
        )
        listed = row_serials(context, other_rows)
        for row in other_rows:
            for serial in listed[row["name"]]:
                if serial in mine:
                    clashes.append({"serial_no": serial, "pick_list": row["parent"]})
    if clashes:
        return context.failed(
            f"{len(clashes)} serial number(s) are already on a live Pick List "
            f"({', '.join(sorted({item['pick_list'] for item in clashes}))}). Choose other serials.",
            {**details, "clashes": clashes[:20]},
        )
    return context.passed("No serial number is on another live Pick List.", details)


# ------------------------------------------------------------ 4. warehouse, stock
def warehouse_pickable(context: EvaluationContext) -> dict[str, Any]:
    """Locations must be in enabled, pickable rack warehouses, never QC, rejected or group warehouses."""
    pickable_flag = context.required_param("pickable_flag_field")
    qc_flag = context.required_param("qc_flag_field")
    rejected_flag = context.param("rejected_flag_field", None)
    rows, dependency = _locations(context)
    if dependency:
        return dependency
    names = sorted({row["warehouse"] for row in rows if isinstance(row.get("warehouse"), str) and row["warehouse"]})
    fields = ["name", "disabled", "is_group", pickable_flag, qc_flag] + ([rejected_flag] if rejected_flag else [])
    found = {row["name"]: row for row in context.data.fetch_by_names("Warehouse", names, fields)}
    findings = Findings()
    for index, line in enumerate(rows):
        warehouse = line.get("warehouse")
        if is_blank(warehouse):
            findings.need(f"{label(_LOCATIONS, index)}.warehouse", f"Row {index + 1} needs a rack warehouse.")
            continue
        record = found.get(warehouse)
        if record is None:
            findings.fail(f"Warehouse '{warehouse}' (row {index + 1}) does not exist or is not readable.")
            continue
        if truthy(record["disabled"]):
            findings.fail(f"Warehouse '{warehouse}' is disabled.")
        if truthy(record["is_group"]):
            findings.fail(f"Warehouse '{warehouse}' is a group, not a rack.")
        if not truthy(record[pickable_flag]):
            findings.fail(f"Warehouse '{warehouse}' is not flagged for picking ({pickable_flag}).")
        if truthy(record[qc_flag]):
            findings.fail(f"Warehouse '{warehouse}' is a QC warehouse. Never pick from it.")
        if rejected_flag and truthy(record[rejected_flag]):
            findings.fail(f"Warehouse '{warehouse}' is a rejected-stock warehouse. Never pick from it.")
    return findings.result(
        context,
        "Every location is in an enabled, pickable warehouse.",
        details={
            "evidence": evidence(
                "C",
                "6 step 3",
                "The system's own picker uses only warehouses flagged for picking and excludes QC and "
                "rejected warehouses. Whether the system rejects a hand-built location in another "
                "warehouse was not tested.",
            )
        },
    )


def stock_available(context: EvaluationContext) -> dict[str, Any]:
    return checks.stock_available(context)


# ------------------------------------------------------------ 5. batch and serial
def batch_and_serial_valid(context: EvaluationContext) -> dict[str, Any]:
    """Batch-tracked items need a valid batch; serial-tracked items need exactly qty active serials."""
    rows, dependency = _locations(context)
    if dependency:
        return dependency
    codes = sorted({row["item_code"] for row in rows if isinstance(row.get("item_code"), str) and row["item_code"]})
    items = {
        row["name"]: row
        for row in context.data.fetch_by_names("Item", codes, ["name", "has_batch_no", "has_serial_no"])
    }
    batch_names = sorted({row["batch_no"] for row in rows if isinstance(row.get("batch_no"), str) and row["batch_no"]})
    batches = {
        row["name"]: row
        for row in context.data.fetch_by_names("Batch", batch_names, ["name", "item", "disabled", "expiry_date"])
    }
    serial_names = sorted({serial for row in rows for serial in split_serials(row.get("serial_no"))})
    limit = int(to_decimal(context.param("max_serials_checked", 500)) or 500)
    if len(serial_names) > limit:
        raise RuleDataError(
            f"{len(serial_names)} serial numbers exceed the {limit} this rule reads in one run. "
            "Split the Pick List or raise max_serials_checked.",
            code="incomplete",
        )
    serials = {
        row["name"]: row
        for row in context.data.fetch_by_names("Serial No", serial_names, ["name", "item_code", "batch_no", "warehouse", "status"])
    } if serial_names else {}
    today = parse_iso_date(context.today)
    findings = Findings()
    for index, line in enumerate(rows):
        tag, code = label(_LOCATIONS, index), line.get("item_code")
        item = items.get(code)
        if item is None:
            findings.need(
                f"{tag}.item_code",
                f"Item '{code}' is not resolved, so batch and serials cannot be checked for row {index + 1}.",
            )
            continue
        batch = line.get("batch_no")
        if truthy(item["has_batch_no"]) and is_blank(batch):
            findings.need(f"{tag}.batch_no", f"Row {index + 1} ({code}) is batch tracked and needs a batch_no.")
        if not is_blank(batch):
            record = batches.get(batch)
            if record is None:
                findings.fail(f"Batch '{batch}' (row {index + 1}) does not exist or is not readable.")
            else:
                if record["item"] != code:
                    findings.fail(f"Batch '{batch}' belongs to item {record['item']}, not {code}.")
                if truthy(record["disabled"]):
                    findings.fail(f"Batch '{batch}' is disabled.")
                expiry = parse_iso_date(record["expiry_date"])
                if expiry is not None and expiry < today:
                    findings.fail(f"Batch '{batch}' expired on {expiry.isoformat()}.")
        listed = split_serials(line.get("serial_no"))
        if truthy(item["has_serial_no"]):
            qty = to_decimal(line.get("qty"))
            if not listed:
                findings.need(f"{tag}.serial_no", f"Row {index + 1} ({code}) is serial tracked and needs its serial numbers.")
                continue
            if qty is not None and Decimal(len(listed)) != qty:
                findings.fail(f"Row {index + 1} ({code}): {len(listed)} serial number(s) for qty {qty}.", row=index + 1)
            if len(set(listed)) != len(listed):
                findings.fail(f"Row {index + 1} ({code}) lists a serial number twice.", row=index + 1)
        for serial in listed:
            record = serials.get(serial)
            if record is None:
                findings.fail(f"Serial No '{serial}' (row {index + 1}) does not exist or is not readable.")
                continue
            if record["item_code"] != code:
                findings.fail(f"Serial No '{serial}' belongs to item {record['item_code']}, not {code}.")
            if record["status"] != "Active":
                findings.fail(f"Serial No '{serial}' is {record['status']}, not Active.")
            if not is_blank(line.get("warehouse")) and record["warehouse"] != line["warehouse"]:
                findings.fail(f"Serial No '{serial}' is in {record['warehouse']}, not {line['warehouse']}.")
            if not is_blank(batch) and record["batch_no"] != batch:
                findings.fail(f"Serial No '{serial}' is in batch {record['batch_no']}, not {batch}.")
    return findings.result(
        context,
        "Batch and serial numbers are valid.",
        details={
            "evidence": evidence(
                "V",
                "6 steps 3 and 6",
                "The system refuses a Pick List location without a batch at Submit & Send ('The "
                "following items are missing Batch Numbers'). Serial checks mirror the guide's "
                "instruction to use active serials of that warehouse and batch.",
            )
        },
    )


def picked_quantity_state(context: EvaluationContext) -> dict[str, Any]:
    """``picked_qty`` is 0 until the user confirms picking, then equals ``qty``. Never more."""
    rows, dependency = _locations(context)
    if dependency:
        return dependency
    findings = SalesFindings()
    unpicked = []
    claimed = []
    for index, line in enumerate(rows):
        picked, qty = to_decimal(line.get("picked_qty") or 0), to_decimal(line.get("qty"))
        tag = label(_LOCATIONS, index)
        if picked is None or qty is None:
            findings.need(f"{tag}.qty", f"Row {index + 1} needs numeric qty and picked_qty.")
            continue
        if picked < 0 or picked > qty:
            findings.fail(f"Row {index + 1}: picked_qty {picked} must be between 0 and qty {qty}.", row=index + 1)
        elif picked == 0 or picked < qty:
            unpicked.append(index + 1)
        else:
            claimed.append(index + 1)
    if unpicked and not findings.has_failures:
        findings.warn(
            f"Row(s) {', '.join(map(str, unpicked))} are not fully picked. The Pick List cannot be sent "
            "for pickup, and a Delivery Note cannot be made from it, until picked_qty equals qty.",
            rows=unpicked,
        )
    if claimed and not findings.has_failures:
        key = "goods_physically_picked"
        message = (
            f"Row(s) {', '.join(map(str, claimed))} claim picked_qty equal to qty. Set this only if the "
            "user confirms the goods were physically picked."
        )
        if context.is_confirmed(key):
            findings.warn(message + " Confirmed by the user.", rows=claimed)
        else:
            findings.confirm(key, message, rows=claimed)
    return findings.result(
        context,
        "picked_qty is consistent.",
        details={
            "evidence": evidence(
                "V",
                "6 steps 4 and 6",
                "'Row 1 picked quantity is less than the required quantity, additional 2.0 Pcs "
                "required.' A Delivery Note built from a Pick List with picked_qty 0 fails.",
            )
        },
    )


# --------------------------------------------------------------------- safety
def consolidated_pick_list_not_used(context: EvaluationContext) -> dict[str, Any]:
    """Consolidated Pick Lists were not verified. Do not create or change one."""
    fields = list(context.required_param("forbidden_fields"))
    used = [field for field in fields if not is_blank(context.payload.get(field))]
    details = {
        "evidence": evidence(
            "U",
            "6 'Consolidated Pick List'",
            "The Consolidated Pick List writes directly to the database and commits. It was not "
            "tested, so the guide says not to create or change one.",
        )
    }
    if used:
        return context.failed(
            f"The payload sets {', '.join(used)}. Consolidated Pick Lists are unverified and must be "
            "avoided. Stop and ask the user.",
            {**details, "fields": used},
        )
    return context.passed("The payload does not use a Consolidated Pick List.", details)


def forbidden_payload_fields(context: EvaluationContext) -> dict[str, Any]:
    return shared.forbidden_payload_fields(context)


# ------------------------------------------------------------------- read-back
def post_creation_readback(context: EvaluationContext) -> dict[str, Any]:
    """Read the created Pick List back: Draft state, the same locations and the same order links."""
    name = context.execution_result.get("name")
    if is_blank(name) or not isinstance(name, str):
        return context.needs_input("The created document name is required to read it back.", ["execution_result.name"])
    initial = dict(context.param("expected_initial_state", {}))
    compare_fields = list(context.param("compare_fields", []))
    header_fields = list(dict.fromkeys(["name", *initial, *compare_fields]))
    rows = context.data.fetch_records("Pick List", header_fields, [("name", "=", name)])
    details: dict[str, Any] = {
        "document": name,
        "evidence": evidence(
            "V", "6 step 5", "Read-back must come from the system; an unreadable document is unverified, never correct."
        ),
    }
    if not rows:
        key = "readback_unverified_acknowledged"
        details["verification"] = "NOT_VERIFIED"
        message = (
            f"{name} could not be read back by the current user. Do not create it again. Report the "
            "name and say it is unverified."
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
            findings.fail(f"{field} is {header.get(field)!r} but {context.payload[field]!r} was sent.", field=field)
    sent = [row for row in (context.payload.get(_LOCATIONS) or []) if isinstance(row, Mapping)]
    read = sorted(
        context.data.fetch_child_records(
            "Pick List Item", "Pick List", [name],
            ["parent", "name", "idx", "item_code", "qty", "uom", "warehouse", "batch_no", "serial_no",
             "picked_qty", "sales_order", "sales_order_item"],
        ),
        key=lambda row: row["idx"],
    )
    if len(read) != len(sent):
        findings.fail(f"{len(read)} location(s) were read back but {len(sent)} were sent.")
    for index, (row_sent, row_read) in enumerate(zip(sent, read)):
        for field in ("item_code", "qty", "uom", "warehouse", "batch_no", "serial_no", "picked_qty", "sales_order", "sales_order_item"):
            if field in row_sent and not same_value(row_read.get(field), row_sent[field]):
                findings.fail(
                    f"{label(_LOCATIONS, index)}.{field} is {row_read.get(field)!r} but {row_sent[field]!r} was sent.",
                    row=index + 1,
                )
    details["location_row_names"] = [{"idx": row["idx"], "item_code": row["item_code"], "name": row["name"]} for row in read]
    details["verification"] = "READ_BACK_COMPARED"
    return findings.result(context, f"{name} was read back and matches what was sent.", details=details)


EVALUATORS: dict[str, Any] = {
    "pick_list.sales_order_resolves_exactly_one": sales_order_resolves_exactly_one,
    "pick_list.sales_order_approved": sales_order_approved,
    "pick_list.sales_order_channel_allowed": sales_order_channel_allowed,
    "pick_list.header_consistent_with_sales_order": header_consistent_with_sales_order,
    "pick_list.location_rows_linked": location_rows_linked,
    "pick_list.brand_lines_stop_and_ask": brand_lines_stop_and_ask,
    "pick_list.quantity_within_pending": quantity_within_pending,
    "pick_list.no_duplicate_or_excess_picking": no_duplicate_or_excess_picking,
    "pick_list.serials_not_on_other_pick_lists": serials_not_on_other_pick_lists,
    "pick_list.warehouse_pickable": warehouse_pickable,
    "pick_list.stock_available": stock_available,
    "pick_list.batch_and_serial_valid": batch_and_serial_valid,
    "pick_list.picked_quantity_state": picked_quantity_state,
    "pick_list.consolidated_pick_list_not_used": consolidated_pick_list_not_used,
    "pick_list.forbidden_payload_fields": forbidden_payload_fields,
    "pick_list.approval_workflow_not_bypassed": checks.draft_only_creation,
    "pick_list.post_creation_readback": post_creation_readback,
}


def register_all(registry: EvaluatorRegistry) -> None:
    """Register every Pick List evaluator explicitly."""
    for key, evaluator in EVALUATORS.items():
        registry.register(key, evaluator, description=(evaluator.__doc__ or "").strip())
