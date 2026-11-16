"""Field access policy based on Frappe's native permission system."""

from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _
from frappe.model import get_permitted_fields

from erp_intelligence.services.metadata_service import MetadataService


class FieldPolicy:
    """Evaluate field access for the authenticated Frappe user."""

    _QUERYABLE_FIELDTYPES = frozenset(
        {
            "Autocomplete",
            "Check",
            "Currency",
            "Data",
            "Date",
            "Datetime",
            "Duration",
            "Dynamic Link",
            "Float",
            "Int",
            "Link",
            "Long Int",
            "Percent",
            "Phone",
            "Rating",
            "Select",
            "Small Text",
            "Text",
            "Time",
        }
    )
    _NEVER_EXPOSE_FIELDTYPES = frozenset({"Password"})
    _TABLE_FIELDTYPES = frozenset({"Table", "Table MultiSelect"})

    @classmethod
    def get_field_policy(
        cls,
        metadata: Mapping[str, Any],
        user: str | None = None,
        *,
        parent_doctype: str | None = None,
        parent_fieldname: str | None = None,
    ) -> dict[str, list[str]]:
        """Return readable and queryable fields for the current session user.

        ``user`` is an optional assertion about the authenticated principal. It
        cannot be used to evaluate another user's access. Child DocTypes require
        their parent DocType and table field because Frappe derives child access
        from that parent context.
        """
        doctype, fieldnames, metadata_is_table = cls._validate_metadata(metadata)
        authenticated_user = cls._get_authenticated_user(user)
        denied_policy = cls._deny_all(fieldnames)

        try:
            meta = frappe.get_meta(doctype)
            if meta.name != doctype or bool(meta.istable) != metadata_is_table:
                return denied_policy

            live_fields = cls._get_live_fields(meta, fieldnames)
            if live_fields is None:
                return denied_policy

            normalized_parent_doctype, normalized_parent_fieldname = cls._get_parent_context(
                meta,
                parent_doctype,
                parent_fieldname,
            )
            if meta.istable and not normalized_parent_doctype:
                return denied_policy

            permission_type = cls._get_permission_type(
                doctype,
                authenticated_user,
                normalized_parent_doctype,
            )
            if permission_type is None:
                return denied_policy

            if meta.istable and not cls._can_access_parent_table_field(
                child_doctype=doctype,
                parent_doctype=normalized_parent_doctype,
                parent_fieldname=normalized_parent_fieldname,
                permission_type=permission_type,
                user=authenticated_user,
            ):
                return denied_policy

            permitted_fields = set(
                get_permitted_fields(
                    doctype=doctype,
                    parenttype=normalized_parent_doctype,
                    user=authenticated_user,
                    permission_type=permission_type,
                    ignore_virtual=True,
                )
            )
            permitted_levels = set(
                meta.get_permlevel_access(
                    permission_type=permission_type,
                    parenttype=normalized_parent_doctype,
                    user=authenticated_user,
                )
            )
            # A successful DocType-level read/select check establishes level 0.
            # This also covers access through a level-0 document share.
            permitted_levels.add(0)

            readable_fields = [
                fieldname
                for fieldname in fieldnames
                if cls._is_readable(
                    live_fields[fieldname],
                    permitted_fields,
                    permitted_levels,
                )
            ]
            readable_set = set(readable_fields)
            queryable_fields = [
                fieldname
                for fieldname in readable_fields
                if getattr(live_fields[fieldname], "fieldtype", None)
                in cls._QUERYABLE_FIELDTYPES
            ]

            return {
                "allowed_fields": readable_fields.copy(),
                "denied_fields": [
                    fieldname for fieldname in fieldnames if fieldname not in readable_set
                ],
                "readable_fields": readable_fields,
                "queryable_fields": queryable_fields,
            }
        except Exception:
            cls._log_evaluation_failure(doctype, authenticated_user)
            return denied_policy

    @staticmethod
    def _validate_metadata(metadata: Mapping[str, Any]) -> tuple[str, list[str], bool]:
        if not isinstance(metadata, Mapping):
            frappe.throw(_("DocType metadata must be a mapping"), frappe.ValidationError)

        doctype = metadata.get("doctype")
        name = metadata.get("name")
        if not isinstance(doctype, str) or not doctype.strip():
            frappe.throw(_("DocType metadata must contain a valid doctype"), frappe.ValidationError)

        doctype = doctype.strip()
        if name != doctype:
            frappe.throw(_("DocType metadata name does not match doctype"), frappe.ValidationError)

        metadata_is_table = metadata.get("istable")
        if not isinstance(metadata_is_table, bool):
            frappe.throw(_("DocType metadata must contain a boolean istable value"), frappe.ValidationError)

        fields = metadata.get("fields")
        if not isinstance(fields, list):
            frappe.throw(_("DocType metadata fields must be a list"), frappe.ValidationError)

        fieldnames = []
        seen = set()
        for field in fields:
            if not isinstance(field, Mapping):
                frappe.throw(_("Each metadata field must be a mapping"), frappe.ValidationError)

            fieldname = field.get("fieldname")
            if not isinstance(fieldname, str) or not fieldname.strip():
                frappe.throw(_("Each metadata field must have a fieldname"), frappe.ValidationError)

            fieldname = fieldname.strip()
            if field.get("doctype") != doctype:
                frappe.throw(_("Metadata field does not match its DocType"), frappe.ValidationError)
            if fieldname in seen:
                frappe.throw(_("Duplicate metadata field: {0}").format(fieldname), frappe.ValidationError)

            seen.add(fieldname)
            fieldnames.append(fieldname)

        return doctype, fieldnames, metadata_is_table

    @staticmethod
    def _get_authenticated_user(user: str | None) -> str:
        session_user = getattr(getattr(frappe, "session", None), "user", None)
        if not isinstance(session_user, str) or not session_user:
            frappe.throw(_("An authenticated Frappe user is required"), frappe.PermissionError)

        if user is not None:
            if not isinstance(user, str):
                frappe.throw(_("User must be a string"), frappe.ValidationError)
            if user != session_user:
                frappe.throw(
                    _("Field access can only be evaluated for the current session user"),
                    frappe.PermissionError,
                )

        return session_user

    @classmethod
    def _get_parent_context(
        cls,
        meta: Any,
        parent_doctype: str | None,
        parent_fieldname: str | None,
    ) -> tuple[str | None, str | None]:
        if not meta.istable:
            if parent_doctype is not None or parent_fieldname is not None:
                frappe.throw(
                    _("Parent context is only valid for child DocTypes"),
                    frappe.ValidationError,
                )
            return None, None

        if not isinstance(parent_doctype, str) or not parent_doctype.strip():
            return None, None
        if not isinstance(parent_fieldname, str) or not parent_fieldname.strip():
            return None, None

        return parent_doctype.strip(), parent_fieldname.strip()

    @staticmethod
    def _get_live_fields(meta: Any, fieldnames: list[str]) -> dict[str, Any] | None:
        # Standard fields (name, owner, creation, ...) are not DocFields. They are
        # re-derived from live meta, never from caller-supplied metadata flags, and
        # remain subject to the DocType-level check and permitted-field evaluation
        # below. Any unknown fieldname still denies the whole policy.
        live_fields = {}
        for fieldname in fieldnames:
            field = MetadataService.get_live_field(meta, fieldname)
            if field is None:
                return None
            live_fields[fieldname] = field
        return live_fields

    @staticmethod
    def _get_permission_type(
        doctype: str,
        user: str,
        parent_doctype: str | None,
    ) -> str | None:
        if frappe.has_permission(
            doctype,
            ptype="read",
            user=user,
            parent_doctype=parent_doctype,
        ):
            return "read"

        if frappe.has_permission(
            doctype,
            ptype="select",
            user=user,
            parent_doctype=parent_doctype,
        ):
            return "select"

        return None

    @classmethod
    def _can_access_parent_table_field(
        cls,
        *,
        child_doctype: str,
        parent_doctype: str | None,
        parent_fieldname: str | None,
        permission_type: str,
        user: str,
    ) -> bool:
        if not parent_doctype or not parent_fieldname:
            return False

        parent_meta = frappe.get_meta(parent_doctype)
        if parent_meta.istable:
            return False

        parent_field = parent_meta.get_field(parent_fieldname)
        if (
            parent_field is None
            or getattr(parent_field, "fieldtype", None) not in cls._TABLE_FIELDTYPES
            or getattr(parent_field, "options", None) != child_doctype
        ):
            return False

        permlevel = int(getattr(parent_field, "permlevel", 0) or 0)
        if permlevel == 0:
            return True

        permitted_levels = set(
            parent_meta.get_permlevel_access(
                permission_type=permission_type,
                user=user,
            )
        )
        return permlevel in permitted_levels

    @classmethod
    def _is_readable(
        cls,
        field: Any,
        permitted_fields: set[str],
        permitted_levels: set[int],
    ) -> bool:
        fieldname = getattr(field, "fieldname", None)
        fieldtype = getattr(field, "fieldtype", None)
        permlevel = int(getattr(field, "permlevel", 0) or 0)

        return (
            fieldname in permitted_fields
            and permlevel in permitted_levels
            and fieldtype not in cls._NEVER_EXPOSE_FIELDTYPES
        )

    @staticmethod
    def _deny_all(fieldnames: list[str]) -> dict[str, list[str]]:
        return {
            "allowed_fields": [],
            "denied_fields": fieldnames.copy(),
            "readable_fields": [],
            "queryable_fields": [],
        }

    @staticmethod
    def _log_evaluation_failure(doctype: str, user: str) -> None:
        try:
            frappe.log_error(
                title="ERP Intelligence field policy evaluation failed",
                message=f"Unable to establish field access for {doctype} and user {user}",
            )
        except Exception:
            pass
