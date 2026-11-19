"""Business Rule Set: the rules that apply to one operation on one DocType."""

import frappe
from frappe import _
from frappe.model.document import Document

from erp_intelligence.services.business_rules.errors import RuleConfigurationError
from erp_intelligence.services.business_rules.parameters import (
    parse_json_object,
    validate_override_keys,
)


class BusinessRuleSet(Document):
    def validate(self):
        self._validate_single_enabled_set()
        self._validate_assignments()

    def _validate_single_enabled_set(self):
        """The engine resolves a set by DocType and operation, so only one may be enabled."""
        if not self.enabled:
            return
        clash = frappe.db.get_value(
            "Business Rule Set",
            {
                "target_doctype": self.target_doctype,
                "operation": self.operation,
                "enabled": 1,
                "name": ["!=", self.name],
            },
            "name",
        )
        if clash:
            frappe.throw(
                _("Business Rule Set {0} is already enabled for {1} / {2}. Disable it first.").format(
                    clash, self.target_doctype, self.operation
                ),
                frappe.ValidationError,
            )

    def _validate_assignments(self):
        seen = set()
        for row in self.rules:
            if row.business_rule in seen:
                frappe.throw(
                    _("Row {0}: Business Rule {1} is assigned more than once").format(
                        row.idx, row.business_rule
                    ),
                    frappe.ValidationError,
                )
            seen.add(row.business_rule)

            if (row.sequence or 0) < 0:
                frappe.throw(
                    _("Row {0}: Sequence cannot be negative").format(row.idx),
                    frappe.ValidationError,
                )

            label = _("Row {0} ({1})").format(row.idx, row.business_rule)
            try:
                override = parse_json_object(row.parameters_override, label)
                defaults = parse_json_object(
                    frappe.db.get_value("Business Rule", row.business_rule, "default_parameters"),
                    label,
                )
                validate_override_keys(defaults, override, label)
            except RuleConfigurationError as exc:
                frappe.throw(str(exc), frappe.ValidationError)
