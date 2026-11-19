"""Business Rule: one reusable, atomic business responsibility."""

import re

import frappe
from frappe import _
from frappe.model.document import Document

from erp_intelligence.services.business_rules.errors import RuleConfigurationError
from erp_intelligence.services.business_rules.parameters import parse_json_object
from erp_intelligence.services.business_rules.registry import EVALUATOR_KEY_PATTERN

RULE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class BusinessRule(Document):
    def validate(self):
        self.rule_code = (self.rule_code or "").strip()
        self.evaluator_key = (self.evaluator_key or "").strip()

        if not RULE_CODE_PATTERN.fullmatch(self.rule_code):
            frappe.throw(
                _("Rule Code must use upper-case letters, digits and underscores, and start with a letter"),
                frappe.ValidationError,
            )
        if not EVALUATOR_KEY_PATTERN.fullmatch(self.evaluator_key):
            frappe.throw(
                _("Evaluator Key must be lower-case dotted identifiers, for example purchase_order.supplier_enabled"),
                frappe.ValidationError,
            )
        try:
            parse_json_object(self.default_parameters, _("Default Parameters"))
        except RuleConfigurationError as exc:
            frappe.throw(str(exc), frappe.ValidationError)
