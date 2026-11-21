"""Create the pilot Business Rule Set for Purchase Order / Create if it is missing.

``seed_purchase_order_create_rule_set`` is a patch that runs once per site and is
skipped when the app is installed after the patch was written. Sites that missed it
get the rules from this patch on their next migrate. It is safe to run again: it only
creates what is missing and never overwrites rules edited in the desk.
"""
from erp_intelligence.services.business_rules.pilots.purchase_order_create import (
    seed_purchase_order_create_rule_set,
)


def execute():
    seed_purchase_order_create_rule_set()
