from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _
from frappe_assistant_core.core.base_tool import BaseTool

from erp_intelligence.services.field_policy import FieldPolicy
from erp_intelligence.services.metadata_service import MetadataService


class DescribeDocType(BaseTool):
    """Describe only the DocType metadata authorized for the current user."""

    _ALLOWED_ARGUMENTS = frozenset({"doctype", "user"})
    _TABLE_FIELDTYPES = frozenset({"Table", "Table MultiSelect"})

    def __init__(self) -> None:
        super().__init__()
        self.name = "describe_doctype"
        self.description = (
            "Describe the readable and queryable metadata for a Frappe DocType "
            "using the current user's native Frappe permissions."
        )
        self.category = "ERP Intelligence"
        self.source_app = "erp_intelligence"
        self.requires_permission = None
        self.inputSchema = {
            "type": "object",
            "properties": {
                "doctype": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact name of the Frappe DocType to describe.",
                },
                "user": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Optional assertion of the authenticated Frappe user. "
                        "It must match the current session user."
                    ),
                },
            },
            "required": ["doctype"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return a permission-filtered description of one DocType."""
        doctype, user = self._validate_arguments(arguments)
        metadata = MetadataService.get_doctype_metadata(doctype)
        policy = FieldPolicy.get_field_policy(metadata, user=user)

        fields = self._authorized_fields(metadata, policy)
        queryable_fields = self._authorized_queryable_fields(fields, policy)
        child_tables = self._authorized_child_tables(metadata, user)

        if not fields and not child_tables:
            frappe.throw(
                _("No authorized metadata is available for this DocType"),
                frappe.PermissionError,
            )

        return {
            "doctype": metadata["doctype"],
            "label": metadata["label"],
            "fields": fields,
            "queryable_fields": queryable_fields,
            "child_tables": child_tables,
        }

    @classmethod
    def _validate_arguments(cls, arguments: Any) -> tuple[str, str | None]:
        if not isinstance(arguments, Mapping):
            frappe.throw(_("Tool arguments must be a mapping"), frappe.ValidationError)

        unknown_arguments = set(arguments) - cls._ALLOWED_ARGUMENTS
        if unknown_arguments:
            frappe.throw(
                _("Unsupported arguments: {0}").format(
                    ", ".join(sorted(str(key) for key in unknown_arguments))
                ),
                frappe.ValidationError,
            )

        doctype = arguments.get("doctype")
        if not isinstance(doctype, str) or not doctype.strip():
            frappe.throw(_("doctype must be a non-empty string"), frappe.ValidationError)

        user = arguments.get("user")
        if user is not None and (not isinstance(user, str) or not user):
            frappe.throw(_("user must be a non-empty string"), frappe.ValidationError)

        return doctype.strip(), user

    @classmethod
    def _authorized_fields(
        cls,
        metadata: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        readable_fields = policy.get("readable_fields")
        if not isinstance(readable_fields, list):
            frappe.throw(_("Unable to establish readable fields"), frappe.PermissionError)

        readable_set = set(readable_fields)
        return [
            cls._public_field(field)
            for field in metadata["fields"]
            if field.get("fieldname") in readable_set
        ]

    @staticmethod
    def _authorized_queryable_fields(
        fields: list[Mapping[str, Any]],
        policy: Mapping[str, Any],
    ) -> list[str]:
        queryable_fields = policy.get("queryable_fields")
        if not isinstance(queryable_fields, list):
            frappe.throw(_("Unable to establish queryable fields"), frappe.PermissionError)

        authorized_fieldnames = {field["fieldname"] for field in fields}
        return [
            fieldname
            for fieldname in queryable_fields
            if fieldname in authorized_fieldnames
        ]

    @classmethod
    def _authorized_child_tables(
        cls,
        metadata: Mapping[str, Any],
        user: str | None,
    ) -> list[dict[str, Any]]:
        relationships = []
        for table_field in metadata["fields"]:
            if table_field.get("fieldtype") not in cls._TABLE_FIELDTYPES:
                continue

            fieldname = table_field.get("fieldname")
            child_doctype = table_field.get("child_doctype")
            if not isinstance(fieldname, str) or not isinstance(child_doctype, str):
                continue
            if not fieldname or not child_doctype:
                continue

            child_metadata = MetadataService.get_doctype_metadata(child_doctype)
            child_policy = FieldPolicy.get_field_policy(
                child_metadata,
                user=user,
                parent_doctype=metadata["doctype"],
                parent_fieldname=fieldname,
            )
            child_fields = cls._authorized_fields(child_metadata, child_policy)
            if not child_fields:
                continue

            relationships.append(
                {
                    "fieldname": fieldname,
                    "label": table_field.get("label"),
                    "fieldtype": table_field.get("fieldtype"),
                    "options": table_field.get("options"),
                    "child_doctype": child_metadata["doctype"],
                    "fields": child_fields,
                    "queryable_fields": cls._authorized_queryable_fields(
                        child_fields,
                        child_policy,
                    ),
                }
            )

        return relationships

    @staticmethod
    def _public_field(field: Mapping[str, Any]) -> dict[str, Any]:
        public_field = {
            "fieldname": field.get("fieldname"),
            "label": field.get("label"),
            "fieldtype": field.get("fieldtype"),
            "options": field.get("options"),
            "standard": bool(field.get("standard", False)),
        }

        if field.get("link_target"):
            public_field["link_target"] = field["link_target"]
        if field.get("child_doctype"):
            public_field["child_doctype"] = field["child_doctype"]

        return public_field
