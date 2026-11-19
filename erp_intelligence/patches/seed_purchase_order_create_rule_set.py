"""Create the pilot Business Rule Set for Purchase Order / Create. Safe to run again."""

from erp_intelligence.services.business_rules.pilots.purchase_order_create import (
    seed_purchase_order_create_rule_set,
)


def execute():
    seed_purchase_order_create_rule_set()
