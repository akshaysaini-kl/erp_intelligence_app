"""Keep ``evaluate_business_rules`` disabled in FAC until an administrator enables it.

FAC (Frappe Assistant Core) treats a tool with no ``FAC Tool Configuration`` row as
enabled for every user, and its own migrate hook creates a missing row with
``enabled = 1``. This module creates the row first, with ``enabled = 0``, so the tool
is hidden from ``tools/list`` and refused by ``tools/call``.

It only writes a data row through FAC's own DocType, as FAC's admin tools do. FAC code
is not changed. It never edits an existing row, so an administrator's later choice
(enable, role restrictions) survives every migrate.

It runs from ``before_migrate`` (before FAC's ``after_migrate`` sync, on every migrate)
and ``after_install``. A one-time patch is not enough: ``bench install-app`` marks all
patches as already run, so a patch would not run on a fresh install, and the next
migrate would then let FAC create an enabled row.

This is the second of two layers. The tool also refuses to run unless the site enables
it (see ``EvaluateBusinessRules._is_enabled``), because FAC fails open when its
configuration cannot be read: a missing table or a failed query leaves every tool
enabled.
"""

import frappe

from erp_intelligence.services.business_rules import constants as c

# Defined here, not imported from the tool: the tool module imports FAC, and a migrate
# hook must not fail on import when FAC is absent. A test keeps the two names equal.
TOOL_NAME = "evaluate_business_rules"
FAC_DOCTYPE = "FAC Tool Configuration"
TOOL_MODULE_PATH = "erp_intelligence.tools.evaluate_business_rules.EvaluateBusinessRules"

CREATED = "created"
EXISTS = "exists"
SKIPPED = "skipped"
FAILED = "failed"


def ensure_disabled_tool_configuration(tool_name: str = TOOL_NAME) -> str:
    """Create a disabled FAC Tool Configuration for ``tool_name`` if none exists.

    Returns ``created``, ``exists`` (left untouched), ``skipped`` (FAC is not
    installed on the site) or ``failed``. It never raises, so it cannot abort a
    migrate; the tool's own switch keeps it off in every case.
    """
    try:
        if not frappe.db.table_exists(FAC_DOCTYPE):
            return SKIPPED
        if frappe.db.exists(FAC_DOCTYPE, tool_name):
            return EXISTS

        config = frappe.new_doc(FAC_DOCTYPE)
        config.tool_name = tool_name
        config.plugin_name = "custom_tools"
        config.description = (
            "Evaluate Business Rules for a DocType and operation. Read-only. "
            "Disabled until an administrator enables it."
        )
        config.enabled = 0
        # The tool only reads. FAC's detector cannot tell, so the category is set by hand.
        config.tool_category = "read_only"
        config.auto_detected_category = "read_only"
        config.category_override = 1
        config.role_access_mode = "Allow All"
        config.source_app = "erp_intelligence"
        config.module_path = TOOL_MODULE_PATH
        config.flags.ignore_permissions = True
        config.insert()
        return CREATED
    except Exception:  # noqa: BLE001 - never abort a migrate or an install
        frappe.logger(c.LOGGER_NAME).error(
            "Could not create the disabled %s row for %s", FAC_DOCTYPE, tool_name, exc_info=True
        )
        return FAILED


def before_migrate() -> None:
    """Hook: runs before patches and before every app's ``after_migrate`` hooks."""
    ensure_disabled_tool_configuration()


def after_install() -> None:
    """Hook: runs after ``bench install-app erp_intelligence``."""
    ensure_disabled_tool_configuration()
