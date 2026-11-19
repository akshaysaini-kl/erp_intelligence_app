"""Pilot: the "Purchase Order / Create" Business Rule Set.

Two layers keep the engine and the evaluators generic:

* ``RULES`` are reusable Business Rules. Their default parameters declare every
  tunable parameter. Site-specific parameters default to ``None`` so an
  unconfigured rule fails loudly (ERROR) instead of guessing.
* ``ASSIGNMENTS`` put the rules in the Rule Set in sequence order. The
  ``parameters_override`` of an assignment carries the site configuration
  verified in PURCHASE_ORDER_CREATION_GUIDE.md (Section 4 and the workflow and
  custom-field names). Change them on the Rule Set, not in code, for another site.

Seeding creates what is missing and leaves existing records untouched, so edits
made in the desk survive a re-run. ``force=True`` rewrites the pilot records.
"""

import json
from typing import Any

import frappe

from erp_intelligence.services.business_rules import constants as c

RULE_SET_NAME = "Purchase Order - Create"
TARGET_DOCTYPE = "Purchase Order"
OPERATION = "Create"

_STANDARD_HEADER_FORBIDDEN = [
    "name", "naming_series", "title", "docstatus", "workflow_state", "status", "owner",
    "creation", "supplier_name", "grand_total", "rounded_total", "per_received", "per_billed",
    "contact_person", "set_warehouse",
]
_STANDARD_ITEM_FORBIDDEN = [
    "name", "docstatus", "item_name", "stock_uom", "stock_qty", "gst_hsn_code", "gst_treatment",
    "rate", "delivered_by_supplier", "sales_order", "sales_order_item",
]
_TAX_ROW_COMPARE_FIELDS = ["account_head", "rate", "charge_type", "category", "add_deduct_tax"]

# Site configuration verified against erp.localhost in the guide. It is applied as
# assignment overrides, never inside an evaluator.
_SITE_OVERRIDES: dict[str, dict[str, Any]] = {
    "PO_ITEM_APPROVED_PRICE": {
        "approval_field": "workflow_state",
        "approved_value": "Approved",
        "disabled_field": "custom_disable",
    },
    "PO_TAX_TREATMENT_RESOLVED": {
        "intra_state_template": "Input GST In-state - DPL",
        "inter_state_template": "Input GST Out-state - DPL",
        "confirmation_state_codes": ["27"],
        "reverse_charge_templates": [
            "Input GST RCM In-state - DPL",
            "Input GST RCM Out-state - DPL",
        ],
    },
    "PO_DUPLICATE_GUARD": {
        "status_field": "workflow_state",
        "statuses": ["Draft", "Pending Approval"],
    },
    "PO_FORBIDDEN_PAYLOAD_FIELDS": {
        "forbidden_fields": _STANDARD_HEADER_FORBIDDEN + [
            "custom_list_price", "custom_margin", "custom_ip_name", "custom_mrpcompany_currency",
            "custom_signature", "custom_stamp", "custom_awb_number",
        ],
        "forbidden_prefixes": ["total", "base_", "net_", "igst_", "cgst_", "sgst_", "custom_shipment_"],
        "forbidden_item_fields": _STANDARD_ITEM_FORBIDDEN + [
            "custom_list_price", "custom_margin", "custom_ip_name",
        ],
    },
    "PO_POST_CREATION_READBACK": {
        "compare_fields": ["supplier", "company", "currency", "conversion_rate", "buying_price_list", "custom_type"],
        "expected_initial_state": {"docstatus": 0, "workflow_state": "Draft", "status": "Draft"},
    },
}


def _rule(
    code: str,
    name: str,
    category: str,
    stage: str,
    evaluator_key: str,
    description: str,
    *,
    severity: str = c.SEVERITY_CRITICAL,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "rule_code": code,
        "rule_name": name,
        "rule_category": category,
        "execution_stage": stage,
        "evaluator_key": evaluator_key,
        "description": description,
        "default_severity": severity,
        "is_blocking": 1,
        "enabled": 1,
        "default_parameters": parameters or {},
    }


RULES: list[dict[str, Any]] = [
    _rule(
        "PO_SUPPLIER_RESOLVES_ONE", "Supplier resolves to exactly one record", "Resolution",
        c.PRE_EXECUTION, "purchase_order.supplier_resolves_exactly_one",
        "The supplier must be an exact Supplier ID. No match fails. Several plausible matches ask the user.",
    ),
    _rule(
        "PO_SUPPLIER_ENABLED", "Supplier is enabled", "Eligibility",
        c.PRE_EXECUTION, "purchase_order.supplier_enabled",
        "The system refuses a disabled supplier.",
    ),
    _rule(
        "PO_SUPPLIER_NOT_ON_HOLD", "Supplier is not on hold or blocked for POs", "Eligibility",
        c.PRE_EXECUTION, "purchase_order.supplier_not_on_hold",
        "Stops on on_hold or prevent_pos. ERPNext enforcement of these flags is unverified.",
    ),
    _rule(
        "PO_SUPPLIER_PRICE_LIST_AVAILABLE", "Supplier price list is available", "Pricing",
        c.PRE_EXECUTION, "purchase_order.supplier_price_list_available",
        "buying_price_list must be the supplier's price list. Any other list needs user confirmation.",
    ),
    _rule(
        "PO_ITEMS_RESOLVE_ONE", "Items resolve to exactly one record", "Resolution",
        c.PRE_EXECUTION, "purchase_order.items_resolve_exactly_one",
        "Every item_code must be an exact Item. Near matches are offered, never picked.",
    ),
    _rule(
        "PO_ITEMS_ENABLED_PURCHASABLE", "Items are enabled and purchasable", "Eligibility",
        c.PRE_EXECUTION, "purchase_order.items_enabled_and_purchasable",
        "Items must be enabled, not past end of life, and marked as purchase items.",
    ),
    _rule(
        "PO_ITEMS_LINKED_TO_SUPPLIER", "Items are linked to the supplier", "Eligibility",
        c.PRE_EXECUTION, "purchase_order.items_linked_to_supplier",
        "Each Item must list the PO supplier in its supplier table. The server does not check this.",
    ),
    _rule(
        "PO_ITEMS_QUANTITY_UOM", "Quantity satisfies UOM and minimum order quantity", "Quantity",
        c.PRE_EXECUTION, "purchase_order.items_quantity_and_uom",
        "Quantity must be positive, whole where the UOM requires, in a defined UOM, and at least the minimum order quantity.",
    ),
    _rule(
        "PO_ITEM_APPROVED_PRICE", "Eligible approved Item Price is resolved", "Pricing",
        c.PRE_EXECUTION, "purchase_order.item_approved_price_resolved",
        "Resolves the approved, enabled, in-date Item Price. Never accepts an empty or zero price.",
        parameters={
            "approval_field": None,
            "approved_value": None,
            "disabled_field": None,
            "price_difference_warning_percent": 0,
        },
    ),
    _rule(
        "PO_CURRENCY_EXCHANGE_RATE", "Currency and exchange rate are satisfied", "Currency",
        c.PRE_EXECUTION, "purchase_order.currency_and_exchange_rate",
        "Currency must match the price list. A foreign currency needs a user-confirmed rate.",
    ),
    _rule(
        "PO_REQUIRED_BY_DATE_VALID", "Required-by date is present and valid", "Dates",
        c.PRE_EXECUTION, "purchase_order.required_by_date_valid",
        "schedule_date is required on the header and every line and may not precede the transaction date.",
    ),
    _rule(
        "PO_TAX_TREATMENT_RESOLVED", "Tax treatment is resolved and valid", "Tax",
        c.PRE_COMMIT, "purchase_order.tax_treatment_resolved",
        "Compares supplier and delivery state, then checks the template and tax rows of the final payload.",
        parameters={
            "taxed_gst_categories": ["Registered Regular"],
            "untaxed_gst_categories": ["Unregistered", "Overseas"],
            "intra_state_template": None,
            "inter_state_template": None,
            "row_compare_fields": _TAX_ROW_COMPARE_FIELDS,
            "required_included_in_print_rate": 0,
            "confirmation_state_codes": [],
            "reverse_charge_templates": [],
        },
    ),
    _rule(
        "PO_DUPLICATE_GUARD", "No recent duplicate Purchase Order", "Duplicate Control",
        c.PRE_COMMIT, "purchase_order.duplicate_guard",
        "Asks for confirmation if the same supplier has a recent draft or pending order.",
        severity=c.SEVERITY_WARNING,
        parameters={"status_field": None, "statuses": None, "window_hours": 24},
    ),
    _rule(
        "PO_FORBIDDEN_PAYLOAD_FIELDS", "Forbidden payload fields are rejected", "Payload Safety",
        c.PRE_COMMIT, "purchase_order.forbidden_payload_fields",
        "Rejects server-owned fields and execution options that bypass the approval workflow.",
        parameters={
            "forbidden_fields": _STANDARD_HEADER_FORBIDDEN,
            "forbidden_prefixes": ["total", "base_", "net_", "igst_", "cgst_", "sgst_"],
            "forbidden_item_fields": _STANDARD_ITEM_FORBIDDEN,
            "forbidden_item_prefixes": ["base_", "net_", "igst_", "cgst_", "sgst_"],
            "forbidden_truthy_options": ["submit", "validate_only"],
        },
    ),
    _rule(
        "PO_POST_CREATION_READBACK", "Post-creation read-back verification", "Verification",
        c.POST_EXECUTION, "purchase_order.post_creation_readback",
        "Reads the created order back and compares header, lines, taxes and totals with what was sent.",
        parameters={
            "compare_fields": ["supplier", "company", "currency", "conversion_rate", "buying_price_list"],
            "expected_initial_state": {"docstatus": 0},
            "amount_tolerance": "0.01",
            "taxed_gst_categories": ["Registered Regular"],
        },
    ),
]


def _assignments() -> list[dict[str, Any]]:
    rows = []
    for position, rule in enumerate(RULES, start=1):
        code = rule["rule_code"]
        override = _SITE_OVERRIDES.get(code)
        rows.append(
            {
                "business_rule": code,
                "sequence": position * 10,
                "enabled": 1,
                "parameters_override": json.dumps(override, indent=1) if override else None,
            }
        )
    return rows


def seed_purchase_order_create_rule_set(force: bool = False) -> dict[str, list[str]]:
    """Create the pilot rules and rule set. Returns the names created, updated and left alone.

    Run on a site where the three Business Rule DocTypes exist. It does not commit;
    the caller (a patch or ``bench execute``) owns the transaction.
    """
    outcome: dict[str, list[str]] = {"created": [], "updated": [], "unchanged": []}

    for definition in RULES:
        values = {**definition, "default_parameters": json.dumps(definition["default_parameters"], indent=1)}
        code = definition["rule_code"]
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

    set_values = {
        "rule_set_name": RULE_SET_NAME,
        "target_doctype": TARGET_DOCTYPE,
        "operation": OPERATION,
        "enabled": 1,
        "version": "1",
        "description": (
            "Pilot rules for creating a Purchase Order as a Draft, derived from "
            "PURCHASE_ORDER_CREATION_GUIDE.md. The engine only advises; creation stays with "
            "the authorized execution layer."
        ),
        "rules": _assignments(),
    }
    if frappe.db.exists("Business Rule Set", RULE_SET_NAME):
        if force:
            doc = frappe.get_doc("Business Rule Set", RULE_SET_NAME)
            doc.update(set_values)
            doc.save(ignore_permissions=True)
            outcome["updated"].append(RULE_SET_NAME)
        else:
            outcome["unchanged"].append(RULE_SET_NAME)
    else:
        frappe.get_doc({"doctype": "Business Rule Set", **set_values}).insert(ignore_permissions=True)
        outcome["created"].append(RULE_SET_NAME)
    return outcome
