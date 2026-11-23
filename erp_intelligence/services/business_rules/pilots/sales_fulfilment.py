"""Business Rule Sets for the Sales and Fulfilment lifecycle.

Rule sets (target DocType / operation), all derived from SALES_ORDER_TO_DELIVERY_GUIDE.md:

* ``Sales Order - Create``       Sales Order / Create
* ``Pick List - Create``         Pick List / Create
* ``Delivery Note - Create``     Delivery Note / Create
* ``Delivery Note - Submit``     Delivery Note / Submit (before and after the submission)
* ``Sales Invoice - Create``     Sales Invoice / Create, invoices and credit notes
* ``Sales Invoice - Submit``     Sales Invoice / Submit (before and after the submission)

The framework allows one enabled Rule Set per DocType and operation, so credit notes are
rules inside ``Sales Invoice - Create`` that apply only when ``is_return`` is set. The
``Submit`` operation is used for Delivery Note and Sales Invoice because the effects the
guide asks to verify (stock movement, ledger posting) exist only after submission.

Same two layers as the Purchase Order pilot:

* ``RULES`` are reusable Business Rules. Default parameters declare every tunable
  parameter. Site values default to ``None`` so an unconfigured rule fails loudly (ERROR).
* ``ASSIGNMENTS`` put the rules in a Rule Set in sequence order. ``SITE`` holds the values
  verified on erp.localhost in the guide (tax templates, state code, workflow names,
  warehouse flag fields). They are written to the assignments, never to an evaluator.

Seeding creates what is missing and leaves existing records untouched, so edits made in
the desk survive a re-run. ``force=True`` rewrites the seeded records. It does not
commit; the caller (a patch or ``bench execute``) owns the transaction.
"""
import json
from typing import Any

import frappe

from erp_intelligence.services.business_rules import constants as c

CRITICAL, WARNING = c.SEVERITY_CRITICAL, c.SEVERITY_WARNING
PRE, COMMIT, POST = c.PRE_EXECUTION, c.PRE_COMMIT, c.POST_EXECUTION

# ----------------------------------------------------------------------- site values
SITE: dict[str, Any] = {
    "company_state_code": "07",
    "taxed_categories": ["Registered Regular"],
    "untaxed_categories": ["Unregistered"],
    "supported_categories": ["Registered Regular", "Unregistered"],
    "unregistered_place_of_supply": "07-Delhi",
    "intra_state_template": "Output GST In-state - DPL",
    "inter_state_template": "Output GST Out-state - DPL",
    "reverse_charge_templates": ["Output GST RCM In-state - DPL", "Output GST RCM Out-state - DPL"],
    "confirmation_state_codes": ["27"],
    "allowed_channels": ["B2B"],
    "refused_customers": ["Roadskye Tradecom Pvt. Ltd."],
    "approved_state": "Approved",
    "pickable_statuses": ["To Deliver and Bill", "To Deliver"],
    "billable_statuses": ["To Deliver and Bill", "To Bill", "To Deliver", "Completed"],
}
_TAX_ROW_COMPARE = ["account_head", "rate", "charge_type", "included_in_print_rate"]
_HEADER_COPY = [
    "customer", "company", "po_no", "po_date", "selling_price_list", "currency", "place_of_supply",
    "taxes_and_charges", "customer_address", "shipping_address_name", "company_address", "custom_type",
]
_COMPUTED_PREFIXES = ["total", "base_", "net_", "igst_", "cgst_", "sgst_", "cess_"]
_GST_ITEM_FIELDS = ["gst_hsn_code", "gst_treatment"]
_GST_ITEM_PREFIXES = ["base_", "net_", "igst_", "cgst_", "sgst_", "cess_"]
_STATUS_FIELDS = ["name", "status", "owner", "creation"]


def _rule(
    code: str,
    name: str,
    category: str,
    stage: str,
    key: str,
    description: str,
    *,
    severity: str = CRITICAL,
    blocking: bool = True,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "rule_code": code,
        "rule_name": name,
        "rule_category": category,
        "execution_stage": stage,
        "evaluator_key": key,
        "description": description,
        "default_severity": severity,
        "is_blocking": 1 if blocking else 0,
        "enabled": 1,
        "default_parameters": parameters or {},
    }


def _tax_params() -> dict[str, Any]:
    return {
        "supported_gst_categories": ["Registered Regular", "Unregistered"],
        "company_state_code": None,
        "intra_state_template": None,
        "inter_state_template": None,
        "row_compare_fields": _TAX_ROW_COMPARE,
        "reverse_charge_templates": [],
        "confirmation_state_codes": [],
    }


_TAX_OVERRIDE = {
    "supported_gst_categories": SITE["supported_categories"],
    "company_state_code": SITE["company_state_code"],
    "intra_state_template": SITE["intra_state_template"],
    "inter_state_template": SITE["inter_state_template"],
    "reverse_charge_templates": SITE["reverse_charge_templates"],
    "confirmation_state_codes": SITE["confirmation_state_codes"],
}
_ADDRESS_PARAMS = {
    "taxed_gst_categories": ["Registered Regular"],
    "untaxed_gst_categories": [],
    "billing_address_type": None,
    "shipping_address_type": None,
}
_ADDRESS_OVERRIDE = {
    "taxed_gst_categories": SITE["taxed_categories"],
    "untaxed_gst_categories": SITE["untaxed_categories"],
    "billing_address_type": "Billing",
    "shipping_address_type": "Shipping",
}
_GST_PARAMS = {"taxed_gst_categories": ["Registered Regular"], "untaxed_gst_categories": [], "unregistered_place_of_supply": None}
_GST_OVERRIDE = {
    "taxed_gst_categories": SITE["taxed_categories"],
    "untaxed_gst_categories": SITE["untaxed_categories"],
    "unregistered_place_of_supply": SITE["unregistered_place_of_supply"],
}
_CHANNEL_PARAMS = {"allowed_channels": None, "refused_customers": []}
_CHANNEL_OVERRIDE = {"allowed_channels": SITE["allowed_channels"], "refused_customers": SITE["refused_customers"]}
_INCLUSIVE_PARAMS = {"required_included_in_print_rate": 1}


def _forbidden(
    fields: list[str], prefixes: list[str], item_fields: list[str], item_prefixes: list[str],
    *, item_tables: list[str] | None = None, truthy_fields: list[str] | None = None,
    allowed: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "forbidden_fields": fields,
        "forbidden_prefixes": prefixes,
        "forbidden_item_fields": item_fields,
        "forbidden_item_prefixes": item_prefixes,
        "item_tables": item_tables or ["items"],
        "forbidden_truthy_fields": truthy_fields or [],
        "forbidden_truthy_options": ["validate_only"],
        "allowed_fields": allowed or [],
    }


def _draft_only(options: list[str], allowed_values: dict[str, Any]) -> dict[str, Any]:
    return {
        "forbidden_payload_fields": list(allowed_values),
        "forbidden_truthy_options": options,
        "allowed_values": allowed_values,
    }


_WORKFLOW_ALLOWED = {"docstatus": [0], "workflow_state": ["Draft"]}
_DRAFT_ALLOWED = {"docstatus": [0]}
_BYPASS_OPTIONS = ["submit", "submit_document"]

# ================================================================== Sales Order - Create
SALES_ORDER_RULES: list[dict[str, Any]] = [
    _rule("SO_CUSTOMER_RESOLVES_ONE", "Customer resolves to exactly one record", "Resolution", PRE,
          "sales_order.customer_resolves_exactly_one",
          "The customer must be an exact Customer ID. No match fails. Several plausible matches ask the user."),
    _rule("SO_CUSTOMER_ENABLED", "Customer is enabled", "Eligibility", PRE, "sales_order.customer_enabled",
          "A disabled customer cannot be sold to."),
    _rule("SO_CHANNEL_ALLOWED", "Channel is one this process handles", "Eligibility", PRE,
          "sales_order.channel_allowed",
          "Only B2B orders are created. B2C and the CS-Cart customer are refused: that channel was never verified.",
          parameters=_CHANNEL_PARAMS),
    _rule("SO_CUSTOMER_NOT_INTERNAL", "Customer is not an internal customer", "Eligibility", PRE,
          "sales_order.customer_not_internal",
          "Internal-customer orders (stock transfers) are not handled by this process."),
    _rule("SO_PRICE_LIST_FOR_CUSTOMER", "Selling price list is the customer's", "Pricing", PRE,
          "sales_order.selling_price_list_for_customer",
          "selling_price_list must be the customer's own price list, enabled and a selling list. Any other list needs user confirmation.",
          severity=WARNING),
    _rule("SO_PO_REFERENCE_PRESENT", "Customer PO number and date are present", "Dates", PRE,
          "sales_order.po_reference_present",
          "po_no and po_date are mandatory. The user supplies them."),
    _rule("SO_DELIVERY_DATE_VALID", "Delivery date is present and valid", "Dates", PRE,
          "sales_order.delivery_date_valid",
          "delivery_date is required on the header and every line and may not precede the order date."),
    _rule("SO_ITEMS_RESOLVE_ONE", "Items resolve to exactly one record", "Resolution", PRE,
          "sales_order.items_resolve_exactly_one",
          "Every item_code must be an exact Item. Near matches are offered, never picked.",
          parameters={"item_table": "items"}),
    _rule("SO_ITEMS_ENABLED_SALEABLE", "Items are enabled and not past end of life", "Eligibility", PRE,
          "sales_order.items_enabled_and_saleable",
          "Items must be enabled and not past end of life. A non-sales item is a warning: system enforcement is unverified."),
    _rule("SO_ITEMS_QUANTITY_UOM", "Quantity satisfies the UOM constraints", "Quantity", PRE,
          "sales_order.items_quantity_and_uom",
          "Quantity must be positive, whole where the UOM requires, in a UOM the item defines, with the right conversion factor.",
          parameters={"item_table": "items"}),
    _rule("SO_BRAND_LINES_STOP_AND_ASK", "Brand-fulfilled lines need a decision", "Eligibility", PRE,
          "sales_order.brand_lines_stop_and_ask",
          "A line whose Item is Brand fulfilled is reported and needs user confirmation. The system sets fulfilment from the Item master and cannot be told otherwise.",
          severity=WARNING,
          parameters={"source": "item", "item_table": "items", "fulfilment_field": None, "brand_value": None}),
    _rule("SO_ITEM_PRICE_RESOLVED", "Every line has a price greater than zero", "Pricing", PRE,
          "sales_order.item_price_resolved",
          "Every line needs price_list_rate above zero. An approved selling Item Price is looked up; a price from the user alone is a warning.",
          parameters={"approval_field": None, "approved_value": None, "disabled_field": None,
                      "selling_field": "selling", "price_difference_warning_percent": 0}),
    _rule("SO_CURRENCY_EXCHANGE_RATE", "Currency and exchange rate are consistent", "Currency", PRE,
          "sales_order.currency_and_exchange_rate",
          "Currency must match the selling price list. A foreign currency is outside the verified process and needs user confirmation."),
    _rule("SO_CUSTOMER_ADDRESSES", "Billing and shipping addresses are resolved", "Eligibility", PRE,
          "sales_order.customer_addresses_resolved",
          "A registered customer needs both addresses, chosen by the user and linked to the customer. A marketplace customer without addresses needs none.",
          parameters=_ADDRESS_PARAMS),
    _rule("SO_GST_PLACE_OF_SUPPLY", "Place of supply agrees with the customer's GSTIN state", "Tax", PRE,
          "sales_order.gst_information_resolved",
          "place_of_supply must be sent and agree with the customer's GSTIN state. This is a consistency check, not GST compliance.",
          parameters=_GST_PARAMS),
    _rule("SO_TAX_TREATMENT", "Tax treatment is resolved and valid", "Tax", COMMIT,
          "sales_order.tax_treatment_resolved",
          "Compares place of supply with the company state, then checks the template and tax rows. Not a GST compliance check.",
          parameters=_tax_params()),
    _rule("SO_TAX_INCLUSIVE_PRICING", "Prices are tax-inclusive", "Pricing", COMMIT,
          "sales_order.tax_inclusive_pricing",
          "Every tax row must be tax-inclusive (included_in_print_rate 1). Another value needs user confirmation.",
          parameters=_INCLUSIVE_PARAMS),
    _rule("SO_DUPLICATE_PO_NUMBER", "No live order exists for this customer PO number", "Duplicate Control", COMMIT,
          "sales_order.duplicate_po_number",
          "The same customer cannot have two live Sales Orders with the same PO number. The existing order is shown instead.",
          parameters={"docstatus_field": "docstatus", "live_docstatuses": [0, 1]}),
    _rule("SO_FORBIDDEN_PAYLOAD_FIELDS", "Forbidden payload fields are rejected", "Payload Safety", COMMIT,
          "sales_order.forbidden_payload_fields",
          "Rejects server-owned and computed fields. Fulfilment, margins, GST amounts and totals are set by the system.",
          parameters=_forbidden(
              ["name", "naming_series", "status", "owner", "creation", "order_type", "set_warehouse",
               "custom_destination_warehouse", "custom_alias", "gst_category", "billing_address_gstin",
               "company_gstin", "grand_total", "rounded_total", "rounding_adjustment", "contact_person",
               "payment_terms_template", "custom_items_updated_on_cs_cart", "per_delivered", "per_billed"],
              _COMPUTED_PREFIXES + ["custom_total", "custom_shipment_", "custom_awb"],
              ["name", "item_name", "stock_uom", "stock_qty", "rate", "amount", "billed_amt", "delivered_qty",
               "custom_fulfilled_by", "delivered_by_supplier", "supplier", "custom_list_price", "custom_margin",
               "custom_mrp_company_currency", "custom_b2c_linked_po", *_GST_ITEM_FIELDS],
              _GST_ITEM_PREFIXES + ["custom_b2c", "custom_effective", "custom_margin", "custom_total",
                                    "custom_shipment_", "custom_awb"])),
    _rule("SO_APPROVAL_WORKFLOW_PROTECTION", "The approval workflow is not bypassed", "Payload Safety", COMMIT,
          "sales_order.approval_workflow_not_bypassed",
          "A Sales Order is created as a Draft. docstatus, workflow_state, submit and submit_document would skip the approval workflow. This rule judges the request it is shown; it cannot stop a caller that does not evaluate.",
          parameters=_draft_only(_BYPASS_OPTIONS, _WORKFLOW_ALLOWED)),
    _rule("SO_POST_CREATION_READBACK", "Post-creation read-back verification", "Verification", POST,
          "sales_order.post_creation_readback",
          "Reads the created order back and compares header, lines, taxes and tax-inclusive totals with what was sent.",
          parameters={"compare_fields": None, "expected_initial_state": {"docstatus": 0}, "amount_tolerance": "0.02",
                      "taxed_gst_categories": ["Registered Regular"], "brand_value": None}),
]

SALES_ORDER_OVERRIDES: dict[str, dict[str, Any]] = {
    "SO_CHANNEL_ALLOWED": _CHANNEL_OVERRIDE,
    "SO_BRAND_LINES_STOP_AND_ASK": {"fulfilment_field": "custom_fulfilled_by", "brand_value": "Brand"},
    "SO_ITEM_PRICE_RESOLVED": {"approval_field": "workflow_state", "approved_value": "Approved", "disabled_field": "custom_disable"},
    "SO_CUSTOMER_ADDRESSES": _ADDRESS_OVERRIDE,
    "SO_GST_PLACE_OF_SUPPLY": _GST_OVERRIDE,
    "SO_TAX_TREATMENT": _TAX_OVERRIDE,
    "SO_POST_CREATION_READBACK": {
        "compare_fields": ["customer", "company", "currency", "conversion_rate", "selling_price_list", "custom_type",
                           "po_no", "po_date", "delivery_date", "customer_address", "shipping_address_name",
                           "company_address", "place_of_supply", "taxes_and_charges"],
        "expected_initial_state": {"docstatus": 0, "workflow_state": "Draft", "status": "Draft"},
        "taxed_gst_categories": SITE["taxed_categories"],
        "brand_value": "Brand",
    },
}

# =================================================================== Pick List - Create
_PL_ORDER = {"approved_workflow_state": None, "pickable_statuses": None}
PICK_LIST_RULES: list[dict[str, Any]] = [
    _rule("PL_SALES_ORDER_RESOLVES_ONE", "Locations point to exactly one Sales Order", "Resolution", PRE,
          "pick_list.sales_order_resolves_exactly_one",
          "Every location names the same, existing Sales Order. A Pick List with locations from two orders is refused at submit."),
    _rule("PL_SALES_ORDER_APPROVED", "Sales Order is approved and open for delivery", "Eligibility", PRE,
          "pick_list.sales_order_approved",
          "The Sales Order must be submitted, in the approved workflow state and in a status that still has goods to deliver.",
          parameters=_PL_ORDER),
    _rule("PL_SALES_ORDER_CHANNEL_ALLOWED", "Sales Order is B2B and not internal", "Eligibility", PRE,
          "pick_list.sales_order_channel_allowed",
          "B2C and internal-customer orders are not handled by this process.",
          parameters={"allowed_channels": None}),
    _rule("PL_HEADER_CONSISTENT_WITH_ORDER", "Pick List header agrees with the Sales Order", "Resolution", PRE,
          "pick_list.header_consistent_with_sales_order",
          "customer, company and custom_type must equal the Sales Order's, and the purpose must be Delivery.",
          parameters={"allowed_purposes": ["Delivery"]}),
    _rule("PL_LOCATION_ROWS_LINKED", "Locations are linked to matching order lines", "Resolution", PRE,
          "pick_list.location_rows_linked",
          "Each location carries sales_order_item, the Sales Order line's row id, for the same item and unit."),
    _rule("PL_BRAND_LINES_STOP_AND_ASK", "Brand-fulfilled lines are not picked without a decision", "Eligibility", PRE,
          "pick_list.brand_lines_stop_and_ask",
          "A location for a Brand-fulfilled Sales Order line needs user confirmation. The system would pick it from stock.",
          severity=WARNING,
          parameters={"source": "sales_order", "item_table": "locations", "order_field": "sales_order",
                      "row_field": "sales_order_item", "fulfilment_field": None, "brand_value": None}),
    _rule("PL_QUANTITY_WITHIN_PENDING", "Picked quantity is within what is still to deliver", "Quantity", PRE,
          "pick_list.quantity_within_pending",
          "Quantity per Sales Order line may not exceed ordered less delivered. System enforcement of over-picking is unverified."),
    _rule("PL_NO_DUPLICATE_OR_EXCESS_PICKING", "No live Pick List already covers these lines", "Duplicate Control", PRE,
          "pick_list.no_duplicate_or_excess_picking",
          "Another live Pick List for the same line needs the user's review; together they may not exceed the pending quantity.",
          severity=WARNING,
          parameters={"live_docstatuses": [0, 1], "live_statuses": None}),
    _rule("PL_WAREHOUSE_PICKABLE", "Locations are in pickable warehouses", "Eligibility", PRE,
          "pick_list.warehouse_pickable",
          "Each warehouse must exist, be enabled, not be a group, be flagged for picking and not be a QC or rejected warehouse.",
          parameters={"pickable_flag_field": None, "qc_flag_field": None, "rejected_flag_field": None}),
    _rule("PL_STOCK_AVAILABLE", "Stock is on hand in each warehouse", "Quantity", PRE,
          "pick_list.stock_available",
          "Each item and warehouse must hold at least the quantity picked. The system refuses a short Pick List at submit.",
          parameters={"item_table": "locations", "qty_field": "qty"}),
    _rule("PL_BATCH_AND_SERIAL_VALID", "Batch and serial numbers are valid", "Quantity", PRE,
          "pick_list.batch_and_serial_valid",
          "Batch-tracked items need an unexpired batch of that item; serial-tracked items need exactly qty distinct serials that are Active in that warehouse and batch.",
          parameters={"max_serials_checked": 500}),
    _rule("PL_SERIALS_NOT_ON_OTHER_PICK_LISTS", "Serials are not on another live Pick List", "Duplicate Control", PRE,
          "pick_list.serials_not_on_other_pick_lists",
          "A serial number may not appear on two live Pick Lists. Whether the system prevents it is unverified.",
          parameters={"live_docstatuses": [0, 1], "live_statuses": None}),
    _rule("PL_PICKED_QUANTITY_STATE", "picked_qty is 0 or confirmed by the user", "Quantity", COMMIT,
          "pick_list.picked_quantity_state",
          "picked_qty equal to qty needs the user's confirmation that the goods were picked. A lower value is a warning: the Pick List cannot be sent until it equals qty."),
    _rule("PL_CONSOLIDATED_NOT_USED", "Consolidated Pick Lists are not used", "Payload Safety", COMMIT,
          "pick_list.consolidated_pick_list_not_used",
          "Consolidated Pick Lists were never verified. The payload may not set their fields.",
          parameters={"forbidden_fields": ["custom_consolidate_pick_list", "custom_cpl_status"]}),
    _rule("PL_FORBIDDEN_PAYLOAD_FIELDS", "Forbidden payload fields are rejected", "Payload Safety", COMMIT,
          "pick_list.forbidden_payload_fields",
          "Rejects server-owned fields: the scanning percentage, transit warehouse, serial and batch bundle and shipment fields.",
          parameters=_forbidden(
              ["name", "status", "owner", "creation", "custom_scanning_percent", "custom_transit_warehouse",
               "custom_is_internal_customer", "for_qty"],
              ["custom_shipment_", "custom_awb"],
              ["name", "item_name", "serial_and_batch_bundle", "custom_shipment_id", "custom_shipment_document", "custom_awb_number"],
              [], item_tables=["locations"])),
    _rule("PL_WORKFLOW_PROTECTION", "The Pick List workflow is not bypassed", "Payload Safety", COMMIT,
          "pick_list.approval_workflow_not_bypassed",
          "A Pick List is created as a Draft. docstatus, workflow_state, submit and submit_document would skip the workflow. 'Confirmed Picked' is never assumed: nobody holds the Delivery Manager role. This rule judges the request it is shown.",
          parameters=_draft_only(_BYPASS_OPTIONS, _WORKFLOW_ALLOWED)),
    _rule("PL_POST_CREATION_READBACK", "Post-creation read-back verification", "Verification", POST,
          "pick_list.post_creation_readback",
          "Reads the created Pick List back and compares header, locations, batches, serials and Sales Order links with what was sent.",
          parameters={"compare_fields": None, "expected_initial_state": {"docstatus": 0}}),
]

PICK_LIST_OVERRIDES: dict[str, dict[str, Any]] = {
    "PL_SALES_ORDER_APPROVED": {"approved_workflow_state": SITE["approved_state"], "pickable_statuses": SITE["pickable_statuses"]},
    "PL_SALES_ORDER_CHANNEL_ALLOWED": {"allowed_channels": SITE["allowed_channels"]},
    "PL_BRAND_LINES_STOP_AND_ASK": {"fulfilment_field": "custom_fulfilled_by", "brand_value": "Brand"},
    "PL_NO_DUPLICATE_OR_EXCESS_PICKING": {"live_statuses": ["Draft", "Open", "Partly Delivered"]},
    "PL_SERIALS_NOT_ON_OTHER_PICK_LISTS": {"live_statuses": ["Draft", "Open", "Partly Delivered"]},
    "PL_WAREHOUSE_PICKABLE": {"pickable_flag_field": "custom_is_new_warehouse", "qc_flag_field": "custom_is_qc_warehouse", "rejected_flag_field": "is_rejected_warehouse"},
    "PL_POST_CREATION_READBACK": {
        "compare_fields": ["customer", "company", "custom_type", "purpose"],
        "expected_initial_state": {"docstatus": 0, "workflow_state": "Draft", "status": "Draft"},
    },
}

# ================================================================ Delivery Note - Create
DELIVERY_NOTE_RULES: list[dict[str, Any]] = [
    _rule("DN_CUSTOMER_RESOLVES_ONE", "Customer resolves to exactly one record", "Resolution", PRE,
          "delivery_note.customer_resolves_exactly_one", "The customer must be an exact Customer ID."),
    _rule("DN_CUSTOMER_ENABLED", "Customer is enabled", "Eligibility", PRE, "delivery_note.customer_enabled",
          "A disabled customer cannot be delivered to."),
    _rule("DN_CHANNEL_ALLOWED", "Channel is one this process handles", "Eligibility", PRE,
          "delivery_note.channel_allowed", "Only B2B deliveries are created.", parameters=_CHANNEL_PARAMS),
    _rule("DN_CUSTOMER_NOT_INTERNAL", "Customer is not an internal customer", "Eligibility", PRE,
          "delivery_note.customer_not_internal", "Internal-customer deliveries are not handled by this process."),
    _rule("DN_PICK_LIST_RESOLVES_ONE", "Rows come from exactly one Pick List", "Resolution", PRE,
          "delivery_note.pick_list_resolves_exactly_one",
          "Every row names the same, existing Pick List. In this business a Delivery Note is always made from one Pick List."),
    _rule("DN_PICK_LIST_SUBMITTED_STATE", "Pick List is submitted and open", "Eligibility", PRE,
          "delivery_note.pick_list_submitted_state",
          "The Pick List must be submitted, sent for pickup and not yet completed. Confirmed Picked is not required.",
          parameters={"allowed_workflow_states": None, "allowed_statuses": None, "allowed_purposes": []}),
    _rule("DN_PICK_LIST_FULLY_PICKED", "Pick List is fully picked", "Quantity", PRE,
          "delivery_note.pick_list_fully_picked", "Every Pick List row must have picked_qty equal to qty."),
    _rule("DN_PICK_LIST_NOT_CONSOLIDATED", "Pick List is not part of a consolidation", "Eligibility", PRE,
          "delivery_note.pick_list_not_consolidated",
          "A Pick List in a Consolidated Pick List is unverified territory and needs user confirmation.",
          severity=WARNING, parameters={"consolidation_field": None}),
    _rule("DN_SALES_ORDER_RESOLVES_ONE", "Rows are against exactly one Sales Order", "Resolution", PRE,
          "delivery_note.sales_order_resolves_exactly_one", "Every row names the same, existing Sales Order."),
    _rule("DN_SALES_ORDER_ELIGIBLE", "Sales Order is approved and open for delivery", "Eligibility", PRE,
          "delivery_note.sales_order_eligible",
          "The Sales Order must be approved, deliverable, B2B and not internal.",
          parameters={"approved_workflow_state": None, "deliverable_statuses": None, "allowed_channels": None}),
    _rule("DN_SALES_ORDER_MATCHES_PICK_LIST", "Sales Order, Pick List and customer agree", "Resolution", PRE,
          "delivery_note.sales_order_matches_pick_list_and_customer",
          "The Delivery Note's customer and company must be the Sales Order's, and the Pick List must cover only that order."),
    _rule("DN_ROW_LINKAGE_EXACT", "Rows keep the exact Sales Order and Pick List row links", "Resolution", PRE,
          "delivery_note.row_linkage_exact",
          "Each row needs so_detail and pick_list_item for the same item. They are what update the order and the Pick List."),
    _rule("DN_QUANTITIES_MATCH_PICK_LIST", "Quantities match the Pick List", "Quantity", PRE,
          "delivery_note.quantities_match_pick_list",
          "Each row delivers exactly the Pick List quantity and never more than the order still needs. Leaving Pick List rows out needs confirmation."),
    _rule("DN_PRICES_MATCH_SALES_ORDER", "Prices equal the Sales Order lines", "Pricing", PRE,
          "delivery_note.prices_match_sales_order", "rate and price_list_rate must equal the Sales Order line (tax-inclusive)."),
    _rule("DN_WAREHOUSE_CONSISTENT", "Rows deliver from the picked warehouse", "Eligibility", PRE,
          "delivery_note.warehouse_consistent_with_pick_list", "Each row's warehouse must be the Pick List row's rack warehouse."),
    _rule("DN_BATCH_SERIAL_MATCH", "Batch and serial numbers are those picked", "Quantity", PRE,
          "delivery_note.batch_and_serial_match_pick_list",
          "Batch and serial numbers must equal the Pick List row's. A batch-tracked row without a batch is refused by the system."),
    _rule("DN_STOCK_STILL_AVAILABLE", "Stock is still on hand", "Quantity", PRE,
          "delivery_note.stock_still_available",
          "Each item and warehouse must still hold the quantity to deliver; stock may have moved since the Pick List.",
          parameters={"item_table": "items", "qty_field": "qty"}),
    _rule("DN_BRAND_LINES_STOP_AND_ASK", "Brand-fulfilled lines are not delivered without a decision", "Eligibility", PRE,
          "delivery_note.brand_lines_stop_and_ask",
          "A row for a Brand-fulfilled Sales Order line needs user confirmation. The system would deliver it from stock.",
          severity=WARNING,
          parameters={"source": "sales_order", "item_table": "items", "order_field": "against_sales_order",
                      "row_field": "so_detail", "fulfilment_field": None, "brand_value": None}),
    _rule("DN_CUSTOMER_ADDRESSES", "Billing and shipping addresses are resolved", "Eligibility", COMMIT,
          "delivery_note.customer_addresses_resolved", "A registered customer needs both addresses linked to the customer.",
          parameters=_ADDRESS_PARAMS),
    _rule("DN_GST_PLACE_OF_SUPPLY", "Place of supply agrees with the customer's GSTIN state", "Tax", COMMIT,
          "delivery_note.gst_information_resolved",
          "place_of_supply must agree with the customer's GSTIN state. Consistency only, not GST compliance.",
          parameters=_GST_PARAMS),
    _rule("DN_HEADER_MATCHES_SALES_ORDER", "Header is copied from the Sales Order", "Resolution", COMMIT,
          "delivery_note.header_matches_sales_order",
          "Customer, PO, price list, addresses, place of supply and tax template are copied from the Sales Order. A difference needs confirmation.",
          severity=WARNING,
          parameters={"compare_fields": _HEADER_COPY, "item_tables": ["items"], "order_link_fields": ["against_sales_order"]}),
    _rule("DN_TAX_TREATMENT", "Tax treatment is resolved and valid", "Tax", COMMIT,
          "delivery_note.tax_treatment_resolved",
          "A template name alone leaves the tax table empty on a Delivery Note, so the rows must be sent and match the template. Not a GST compliance check.",
          parameters=_tax_params()),
    _rule("DN_TAX_INCLUSIVE_PRICING", "Prices are tax-inclusive", "Pricing", COMMIT,
          "delivery_note.tax_inclusive_pricing", "Every tax row must be tax-inclusive.", parameters=_INCLUSIVE_PARAMS),
    _rule("DN_DUPLICATE_DELIVERY_GUARD", "No live Delivery Note already delivers these rows", "Duplicate Control", COMMIT,
          "delivery_note.duplicate_delivery_guard",
          "Another live Delivery Note delivering the same Pick List row fails; other Delivery Notes for the order are a warning.",
          parameters={"live_docstatuses": [0, 1]}),
    _rule("DN_FORBIDDEN_PAYLOAD_FIELDS", "Forbidden payload fields are rejected", "Payload Safety", COMMIT,
          "delivery_note.forbidden_payload_fields",
          "Rejects server-owned fields, transit warehouses, e-way bill fields and any return marker: Delivery Note returns are unverified.",
          parameters=_forbidden(
              [*_STATUS_FIELDS, "set_warehouse", "set_target_warehouse", "gst_category", "billing_address_gstin",
               "company_gstin", "grand_total", "rounded_total", "rounding_adjustment", "per_billed", "ewaybill",
               "e_waybill_status"],
              _COMPUTED_PREFIXES + ["custom_shipment_", "custom_awb"],
              ["name", "item_name", "stock_uom", "stock_qty", "amount", "serial_and_batch_bundle", "target_warehouse",
               *_GST_ITEM_FIELDS],
              _GST_ITEM_PREFIXES + ["custom_shipment_", "custom_awb"],
              truthy_fields=["is_return"])),
    _rule("DN_DRAFT_ONLY", "The Delivery Note is created as a Draft", "Payload Safety", COMMIT,
          "delivery_note.draft_only_creation",
          "A Delivery Note is created as a Draft. Submitting posts stock entries and needs the user's explicit instruction. This rule judges the request it is shown.",
          parameters=_draft_only(_BYPASS_OPTIONS, _DRAFT_ALLOWED)),
    _rule("DN_POST_CREATION_READBACK", "Post-creation read-back verification", "Verification", POST,
          "delivery_note.post_creation_readback",
          "Reads the created Draft back and compares header, row links, prices and tax-inclusive totals with what was sent.",
          parameters={"compare_fields": None, "expected_initial_state": {"docstatus": 0}, "amount_tolerance": "0.02"}),
]

DELIVERY_NOTE_OVERRIDES: dict[str, dict[str, Any]] = {
    "DN_CHANNEL_ALLOWED": _CHANNEL_OVERRIDE,
    "DN_PICK_LIST_SUBMITTED_STATE": {"allowed_workflow_states": ["Sent for Pickup", "Items Picked"],
                                     "allowed_statuses": ["Open", "Partly Delivered"], "allowed_purposes": ["Delivery"]},
    "DN_PICK_LIST_NOT_CONSOLIDATED": {"consolidation_field": "custom_consolidate_pick_list"},
    "DN_SALES_ORDER_ELIGIBLE": {"approved_workflow_state": SITE["approved_state"],
                                "deliverable_statuses": SITE["pickable_statuses"], "allowed_channels": SITE["allowed_channels"]},
    "DN_BRAND_LINES_STOP_AND_ASK": {"fulfilment_field": "custom_fulfilled_by", "brand_value": "Brand"},
    "DN_CUSTOMER_ADDRESSES": _ADDRESS_OVERRIDE,
    "DN_GST_PLACE_OF_SUPPLY": _GST_OVERRIDE,
    "DN_TAX_TREATMENT": _TAX_OVERRIDE,
    "DN_POST_CREATION_READBACK": {
        "compare_fields": ["customer", "company", "custom_type", "po_no", "selling_price_list", "currency",
                           "place_of_supply", "taxes_and_charges", "customer_address", "shipping_address_name",
                           "company_address"],
        "expected_initial_state": {"docstatus": 0, "status": "Draft"},
    },
}

# ================================================================ Delivery Note - Submit
DELIVERY_NOTE_SUBMIT_RULES: list[dict[str, Any]] = [
    _rule("DN_SUBMIT_DRAFT_READY", "Draft is ready to submit", "Eligibility", PRE,
          "delivery_note.draft_ready_to_submit",
          "The document is a Draft, every row keeps its order and Pick List links, and the stock is still on hand. Submitting cannot be undone by the assistant."),
    _rule("DN_SUBMIT_STOCK_MOVEMENT_VERIFIED", "Stock movement is verified", "Verification", POST,
          "delivery_note.stock_movement_verified",
          "After submit, the stock ledger must show each delivered quantity leaving its warehouse and nothing else moving."),
    _rule("DN_SUBMIT_ORDER_PROGRESS_UPDATED", "Sales Order delivered quantity is updated", "Verification", POST,
          "delivery_note.sales_order_progress_updated",
          "After submit, the Sales Order lines must show at least the delivered quantity."),
    _rule("DN_SUBMIT_PICK_LIST_COMPLETED", "Pick List is completed", "Verification", POST,
          "delivery_note.pick_list_completed", "After submit, the Pick List status must be Completed.",
          parameters={"completed_status": None}),
]
DELIVERY_NOTE_SUBMIT_OVERRIDES: dict[str, dict[str, Any]] = {
    "DN_SUBMIT_PICK_LIST_COMPLETED": {"completed_status": "Completed"},
}

# ================================================================ Sales Invoice - Create
_SI_CUSTOMER_AND_CHANNEL = [
    _rule("SI_CUSTOMER_RESOLVES_ONE", "Customer resolves to exactly one record", "Resolution", PRE,
          "sales_invoice.customer_resolves_exactly_one", "The customer must be an exact Customer ID."),
    _rule("SI_CUSTOMER_ENABLED", "Customer is enabled", "Eligibility", PRE, "sales_invoice.customer_enabled",
          "A disabled customer cannot be invoiced."),
    _rule("SI_CHANNEL_ALLOWED", "Channel is one this process handles", "Eligibility", PRE,
          "sales_invoice.channel_allowed", "Only B2B invoices are created.", parameters=_CHANNEL_PARAMS),
    _rule("SI_CUSTOMER_NOT_INTERNAL", "Customer is not an internal customer", "Eligibility", PRE,
          "sales_invoice.customer_not_internal", "Internal-customer invoices are not handled by this process."),
]
SALES_INVOICE_RULES: list[dict[str, Any]] = [
    *_SI_CUSTOMER_AND_CHANNEL,
    _rule("SI_SALES_ORDER_RESOLVES_ONE", "Rows are billed against exactly one Sales Order", "Resolution", PRE,
          "sales_invoice.sales_order_resolves_exactly_one",
          "Every row names the same, existing Sales Order. Invoices are raised against Sales Orders in this business."),
    _rule("SI_SALES_ORDER_BILLABLE", "Sales Order is approved and open for billing", "Eligibility", PRE,
          "sales_invoice.sales_order_billable", "The Sales Order must be approved, in a billable status, B2B and not internal.",
          parameters={"approved_workflow_state": None, "billable_statuses": None, "allowed_channels": None}),
    _rule("SI_CUSTOMER_MATCHES_ORDER", "Customer is the Sales Order's customer", "Resolution", PRE,
          "sales_invoice.customer_matches_sales_order", "The invoice's customer and company must equal the Sales Order's."),
    _rule("SI_ITEMS_RESOLVE_ONE", "Items resolve to exactly one record", "Resolution", PRE,
          "sales_invoice.items_resolve_exactly_one", "Every item_code must be an exact Item.",
          parameters={"item_table": "items"}),
    _rule("SI_ROW_LINKAGE_TO_ORDER", "Rows are billed against Sales Order lines", "Resolution", PRE,
          "sales_invoice.row_linkage_to_sales_order",
          "Each row needs sales_order and so_detail for the same item. Linking to a Delivery Note needs the user's request."),
    _rule("SI_UNBILLED_QUANTITY_AVAILABLE", "Lines bill no more than is unbilled", "Quantity", PRE,
          "sales_invoice.unbilled_quantity_available",
          "Each Sales Order line may be billed only up to its amount less what is already billed. The system accepts a second invoice for a billed line.",
          parameters={"amount_tolerance": "0.02"}),
    _rule("SI_DUPLICATE_BILLING_GUARD", "No Draft invoice already bills these lines", "Duplicate Control", PRE,
          "sales_invoice.duplicate_billing_guard",
          "A Draft invoice that already bills the same Sales Order line needs the user's review, and together they may not exceed the unbilled amount.",
          severity=WARNING, parameters={"amount_tolerance": "0.02"}),
    _rule("SI_PRICE_LIST_VALID", "Selling price list is valid", "Pricing", PRE,
          "sales_invoice.selling_price_list_valid", "The price list must exist, be enabled, be a selling list and match the invoice currency."),
    _rule("SI_PRICES_MATCH_ORDER", "Prices equal the Sales Order lines", "Pricing", PRE,
          "sales_invoice.prices_match_sales_order",
          "Prices are the Sales Order's tax-inclusive prices. Billing another price needs user confirmation.", severity=WARNING),
    _rule("SI_BRAND_LINES_STOP_AND_ASK", "Brand-fulfilled lines are not billed without a decision", "Eligibility", PRE,
          "sales_invoice.brand_lines_stop_and_ask",
          "A row for a Brand-fulfilled Sales Order line needs user confirmation.", severity=WARNING,
          parameters={"source": "sales_order", "item_table": "items", "order_field": "sales_order",
                      "row_field": "so_detail", "fulfilment_field": None, "brand_value": None}),
    _rule("SI_RETURN_SOURCE_ELIGIBLE", "Credit note: source invoice is eligible", "Eligibility", PRE,
          "sales_invoice.return_source_eligible",
          "Applies to credit notes. return_against must be a submitted, non-return invoice of the same customer."),
    _rule("SI_RETURN_QUANTITY_VALID", "Credit note: return quantities are valid", "Quantity", PRE,
          "sales_invoice.return_quantity_valid",
          "Applies to credit notes. Quantities are negative and may not exceed what is left uncredited on the source invoice."),
    _rule("SI_RETURN_ITEM_LINKAGE", "Credit note: rows link to the source invoice rows", "Resolution", PRE,
          "sales_invoice.return_item_linkage",
          "Applies to credit notes. Each row keeps the source row's so_detail and item, and has no Delivery Note link."),
    _rule("SI_RETURN_DUPLICATE_GUARD", "Credit note: no other live credit note exists", "Duplicate Control", PRE,
          "sales_invoice.return_duplicate_guard",
          "Applies to credit notes. Another live credit note against the same invoice needs the user's review.", severity=WARNING),
    _rule("SI_CUSTOMER_ADDRESSES", "Billing and shipping addresses are resolved", "Eligibility", COMMIT,
          "sales_invoice.customer_addresses_resolved",
          "A registered customer needs both addresses linked to the customer. The system submits an invoice without them.",
          parameters=_ADDRESS_PARAMS),
    _rule("SI_GST_PLACE_OF_SUPPLY", "Place of supply agrees with the customer's GSTIN state", "Tax", COMMIT,
          "sales_invoice.gst_information_resolved",
          "place_of_supply must agree with the customer's GSTIN state. Consistency only, not GST compliance.",
          parameters=_GST_PARAMS),
    _rule("SI_HEADER_MATCHES_ORDER", "Header is copied from the Sales Order", "Resolution", COMMIT,
          "sales_invoice.header_matches_sales_order",
          "Customer, PO, price list, addresses, place of supply and tax template are copied from the Sales Order. A difference needs confirmation.",
          severity=WARNING,
          parameters={"compare_fields": _HEADER_COPY, "item_tables": ["items"], "order_link_fields": ["sales_order"]}),
    _rule("SI_TAX_TREATMENT", "Tax treatment is resolved and valid", "Tax", COMMIT,
          "sales_invoice.tax_treatment_resolved",
          "The rows must be sent and match the template chosen by the state comparison. The system also submits a wrong tax type, so this is not a GST compliance check.",
          parameters=_tax_params()),
    _rule("SI_TAX_INCLUSIVE_PRICING", "Prices are tax-inclusive", "Pricing", COMMIT,
          "sales_invoice.tax_inclusive_pricing", "Every tax row must be tax-inclusive.", parameters=_INCLUSIVE_PARAMS),
    _rule("SI_RETURN_AMOUNTS_CONSISTENT", "Credit note: prices and tax match the source invoice", "Tax", COMMIT,
          "sales_invoice.return_amounts_consistent",
          "Applies to credit notes. Prices and tax rows must be those of the source invoice and stay tax-inclusive.",
          parameters={"required_included_in_print_rate": 1}),
    _rule("SI_RETURN_STOCK_OUTSTANDING_NOTICE", "Credit note: stock and outstanding effects are stated", "Verification", COMMIT,
          "sales_invoice.return_stock_and_outstanding_notice",
          "Applies to credit notes. A credit note returns no goods to stock and does not by itself reduce the invoice's outstanding. update_stock fails. A Delivery Note return and a return warehouse are unresolved.",
          severity=WARNING),
    _rule("SI_GST_COMPLIANCE_NOT_VERIFIED", "GST compliance is not verified", "Tax", COMMIT,
          "sales_invoice.gst_compliance_not_verified",
          "Always a warning. The system does not validate the tax type, place of supply or missing addresses; IRN and e-way bill are manual steps. Never tell the user the invoice is GST compliant.",
          severity=WARNING, blocking=False),
    _rule("SI_FORBIDDEN_PAYLOAD_FIELDS", "Forbidden payload fields are rejected", "Payload Safety", COMMIT,
          "sales_invoice.forbidden_payload_fields",
          "Rejects server-owned fields, e-invoice and e-way bill fields, update_stock and point-of-sale. The three total placeholders the assistant tool demands are exempt: the server recalculates them.",
          parameters=_forbidden(
              [*_STATUS_FIELDS, "gst_category", "billing_address_gstin", "company_gstin", "rounded_total",
               "rounding_adjustment", "outstanding_amount", "due_date", "irn", "einvoice_status", "ewaybill",
               "e_waybill_status"],
              _COMPUTED_PREFIXES + ["custom_total", "custom_amount", "paid_", "write_off"],
              ["name", "item_name", "amount", "custom_list_price", "custom_margin", "custom_amount_with_gst",
               "custom_total_buying_price_without_discount", *_GST_ITEM_FIELDS],
              _GST_ITEM_PREFIXES + ["custom_total", "custom_amount"],
              truthy_fields=["update_stock", "is_pos"],
              allowed=["base_net_total", "base_grand_total", "grand_total"])),
    _rule("SI_DRAFT_ONLY", "The invoice is created as a Draft", "Payload Safety", COMMIT,
          "sales_invoice.draft_only_creation",
          "An invoice is created as a Draft. Submitting posts to the ledger and needs the user's explicit instruction. This rule judges the request it is shown.",
          parameters=_draft_only(_BYPASS_OPTIONS, _DRAFT_ALLOWED)),
    _rule("SI_POST_CREATION_READBACK", "Post-creation read-back verification", "Verification", POST,
          "sales_invoice.post_creation_readback",
          "Reads the created invoice back: Draft state, series, order links, and the server-calculated tax-inclusive totals. Credit notes are verified by their own rule.",
          parameters={"compare_fields": None, "expected_initial_state": {"docstatus": 0}, "expected_series_prefix": None,
                      "amount_tolerance": "0.02", "taxed_gst_categories": ["Registered Regular"]}),
    _rule("SI_RETURN_POST_VERIFICATION", "Credit note: post-creation verification", "Verification", POST,
          "sales_invoice.return_post_creation_verification",
          "Applies to credit notes. Reads the Draft back: return markers, CN series, negative lines and negative tax-inclusive totals.",
          parameters={"expected_series_prefix": None, "amount_tolerance": "0.02"}),
]
SALES_INVOICE_OVERRIDES: dict[str, dict[str, Any]] = {
    "SI_CHANNEL_ALLOWED": _CHANNEL_OVERRIDE,
    "SI_SALES_ORDER_BILLABLE": {"approved_workflow_state": SITE["approved_state"],
                                "billable_statuses": SITE["billable_statuses"], "allowed_channels": SITE["allowed_channels"]},
    "SI_BRAND_LINES_STOP_AND_ASK": {"fulfilment_field": "custom_fulfilled_by", "brand_value": "Brand"},
    "SI_CUSTOMER_ADDRESSES": _ADDRESS_OVERRIDE,
    "SI_GST_PLACE_OF_SUPPLY": _GST_OVERRIDE,
    "SI_TAX_TREATMENT": _TAX_OVERRIDE,
    "SI_POST_CREATION_READBACK": {
        "compare_fields": ["customer", "company", "currency", "selling_price_list", "custom_type", "po_no",
                           "place_of_supply", "taxes_and_charges", "customer_address", "shipping_address_name",
                           "company_address", "debit_to"],
        "expected_initial_state": {"docstatus": 0, "status": "Draft", "is_return": 0},
        "expected_series_prefix": "DCPL/",
        "taxed_gst_categories": SITE["taxed_categories"],
    },
    "SI_RETURN_POST_VERIFICATION": {"expected_series_prefix": "CN-"},
}

# ================================================================ Sales Invoice - Submit
SALES_INVOICE_SUBMIT_RULES: list[dict[str, Any]] = [
    _rule("SI_SUBMIT_DRAFT_READY", "Draft is ready to submit", "Eligibility", PRE,
          "sales_invoice.draft_ready_to_submit",
          "A Draft with order links, real totals of the right sign and, for a registered customer, tax rows. Submitting posts to the ledger and cannot be undone by the assistant.",
          parameters={"taxed_gst_categories": ["Registered Regular"]}),
    _rule("SI_SUBMIT_LEDGER_POSTED", "Ledger posting is verified", "Verification", POST,
          "sales_invoice.ledger_posting_verified",
          "After submit, the general ledger must balance and the receivable account must carry the invoice total.",
          parameters={"amount_tolerance": "0.02"}),
    _rule("SI_SUBMIT_ORDER_BILLING_UPDATED", "Sales Order billed amount is updated", "Verification", POST,
          "sales_invoice.sales_order_billing_updated",
          "After submit, the Sales Order lines must show at least the billed amount.",
          parameters={"amount_tolerance": "0.02"}),
    _rule("SI_SUBMIT_GST_NOT_VERIFIED", "GST compliance is not verified", "Tax", POST,
          "sales_invoice.gst_compliance_not_verified",
          "Always a warning after submit. ERPNext's submission is not proof of GST compliance; IRN and e-way bill are manual.",
          severity=WARNING, blocking=False),
]
SALES_INVOICE_SUBMIT_OVERRIDES: dict[str, dict[str, Any]] = {
    "SI_SUBMIT_DRAFT_READY": {"taxed_gst_categories": SITE["taxed_categories"]},
}

# ----------------------------------------------------------------------- the rule sets
RULE_SETS: list[dict[str, Any]] = [
    {
        "name": "Sales Order - Create", "doctype": "Sales Order", "operation": "Create",
        "rules": SALES_ORDER_RULES, "overrides": SALES_ORDER_OVERRIDES,
        "description": "Rules for creating a B2B Sales Order as a Draft, derived from SALES_ORDER_TO_DELIVERY_GUIDE.md section 4.",
    },
    {
        "name": "Pick List - Create", "doctype": "Pick List", "operation": "Create",
        "rules": PICK_LIST_RULES, "overrides": PICK_LIST_OVERRIDES,
        "description": "Rules for creating a Pick List as a Draft for one approved Sales Order, derived from SALES_ORDER_TO_DELIVERY_GUIDE.md section 6.",
    },
    {
        "name": "Delivery Note - Create", "doctype": "Delivery Note", "operation": "Create",
        "rules": DELIVERY_NOTE_RULES, "overrides": DELIVERY_NOTE_OVERRIDES,
        "description": "Rules for creating a Delivery Note as a Draft from a submitted Pick List, derived from SALES_ORDER_TO_DELIVERY_GUIDE.md section 7.",
    },
    {
        "name": "Delivery Note - Submit", "doctype": "Delivery Note", "operation": "Submit",
        "rules": DELIVERY_NOTE_SUBMIT_RULES, "overrides": DELIVERY_NOTE_SUBMIT_OVERRIDES,
        "description": "Checks before submitting a Delivery Note and verifies the stock movement and source documents afterwards.",
    },
    {
        "name": "Sales Invoice - Create", "doctype": "Sales Invoice", "operation": "Create",
        "rules": SALES_INVOICE_RULES, "overrides": SALES_INVOICE_OVERRIDES,
        "description": "Rules for creating a Sales Invoice or credit note as a Draft against a Sales Order, derived from SALES_ORDER_TO_DELIVERY_GUIDE.md section 8. Credit-note rules apply only when is_return is set.",
    },
    {
        "name": "Sales Invoice - Submit", "doctype": "Sales Invoice", "operation": "Submit",
        "rules": SALES_INVOICE_SUBMIT_RULES, "overrides": SALES_INVOICE_SUBMIT_OVERRIDES,
        "description": "Checks before submitting a Sales Invoice and verifies the ledger posting afterwards. Never proves GST compliance.",
    },
]

RULE_SET_NAMES = [definition["name"] for definition in RULE_SETS]


def all_rules() -> list[dict[str, Any]]:
    """Every rule of every set, in order. Rule codes are unique across the sets."""
    return [rule for definition in RULE_SETS for rule in definition["rules"]]


def _assignments(definition: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for position, rule in enumerate(definition["rules"], start=1):
        override = definition["overrides"].get(rule["rule_code"])
        rows.append(
            {
                "business_rule": rule["rule_code"],
                "sequence": position * 10,
                "enabled": 1,
                "parameters_override": json.dumps(override, indent=1) if override else None,
            }
        )
    return rows


def seed_sales_fulfilment_rule_sets(force: bool = False) -> dict[str, list[str]]:
    """Create the Sales and Fulfilment rules and rule sets. Returns names created, updated and left alone.

    Safe to run again. Existing records are left alone unless ``force`` is True.
    """
    outcome: dict[str, list[str]] = {"created": [], "updated": [], "unchanged": []}
    for rule in all_rules():
        values = {**rule, "default_parameters": json.dumps(rule["default_parameters"], indent=1)}
        code = rule["rule_code"]
        if frappe.db.exists("Business Rule", code):
            if not force:
                outcome["unchanged"].append(code)
                continue
            doc = frappe.get_doc("Business Rule", code)
            doc.update(values)
            doc.save(ignore_permissions=True)
            outcome["updated"].append(code)
        else:
            frappe.get_doc({"doctype": "Business Rule", **values}).insert(ignore_permissions=True)
            outcome["created"].append(code)
    for definition in RULE_SETS:
        name = definition["name"]
        values = {
            "rule_set_name": name,
            "target_doctype": definition["doctype"],
            "operation": definition["operation"],
            "enabled": 1,
            "version": "1",
            "description": definition["description"] + " The engine only advises; creation stays with the authorized execution layer.",
            "rules": _assignments(definition),
        }
        if frappe.db.exists("Business Rule Set", name):
            if not force:
                outcome["unchanged"].append(name)
                continue
            doc = frappe.get_doc("Business Rule Set", name)
            doc.update(values)
            doc.save(ignore_permissions=True)
            outcome["updated"].append(name)
        else:
            frappe.get_doc({"doctype": "Business Rule Set", **values}).insert(ignore_permissions=True)
            outcome["created"].append(name)
    return outcome
