"""Create the Business Rules and Rule Sets for the Sales and Fulfilment process.

Creates what is missing for Sales Order, Pick List, Delivery Note and Sales Invoice
(including credit notes). Safe to run again: existing rules and rule sets, including any
edited in the desk, are left untouched. Purchase Order rules are not read or changed.

On a site where the app was installed after this patch was written, run it by hand:
``bench --site <site> execute erp_intelligence.patches.seed_sales_fulfilment_rule_sets.execute``
and commit with ``bench --site <site> execute frappe.db.commit``.
"""
from erp_intelligence.services.business_rules.pilots.sales_fulfilment import (
    seed_sales_fulfilment_rule_sets,
)


def execute():
    seed_sales_fulfilment_rule_sets()
