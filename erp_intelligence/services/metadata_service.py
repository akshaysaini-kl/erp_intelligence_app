"""Normalize live Frappe DocType metadata without accessing ERP records."""

from typing import Any

import frappe
from frappe import _


class MetadataService:
    """Provide schema metadata without evaluating user permissions."""

    _TABLE_FIELD_TYPES = frozenset({"Table", "Table MultiSelect"})

    # Frappe standard columns exposed as fields. ``meta.fields`` never contains
    # them, so they are described here. Each entry is
    # (fieldname, label, fieldtype, options, requires_submittable).
    _STANDARD_FIELDS = (
        ("name", "ID", "Data", None, False),
        ("owner", "Created By", "Link", "User", False),
        ("creation", "Created On", "Datetime", None, False),
        ("modified", "Last Updated On", "Datetime", None, False),
        ("modified_by", "Last Updated By", "Link", "User", False),
        ("docstatus", "Document Status", "Int", None, True),
    )

    # Row columns of child DocTypes. Same tuple layout as _STANDARD_FIELDS.
    _CHILD_STANDARD_FIELDS = (
        ("name", "ID", "Data", None, False),
        ("parent", "Parent", "Data", None, False),
        ("parentfield", "Parent Field", "Data", None, False),
        ("idx", "Row No", "Int", None, False),
    )

    @classmethod
    def get_doctype_metadata(cls, doctype: str) -> dict[str, Any]:
        """Return normalized live metadata for a Frappe DocType.

        Standard fields, Custom Fields, and effective property overrides are
        supplied by ``frappe.get_meta``. Frappe standard columns (``name``,
        ``owner``, ``creation``, ``modified``, ``modified_by`` and, for
        submittable DocTypes, ``docstatus``) are appended with
        ``standard: True``. The result describes schema only; callers must
        enforce user and document permissions separately.
        """
        normalized_doctype = cls._validate_doctype(doctype)
        meta = frappe.get_meta(normalized_doctype)

        fields = [cls._normalize_field(meta.name, field) for field in meta.fields]
        fields.extend(
            cls._normalize_standard_field(meta.name, definition)
            for definition in cls.get_standard_field_definitions(meta)
        )

        return {
            "doctype": meta.name,
            "name": meta.name,
            "label": getattr(meta, "label", None) or meta.name,
            "istable": bool(getattr(meta, "istable", False)),
            "is_submittable": bool(getattr(meta, "is_submittable", False)),
            "fields": fields,
        }

    @classmethod
    def get_standard_field_definitions(cls, meta: Any) -> list[dict[str, Any]]:
        """Return the standard field definitions that apply to a live DocType.

        Single and virtual DocTypes have no queryable standard columns. Child
        DocTypes expose only their row columns (``name``, ``parent``,
        ``parentfield``, ``idx``). ``docstatus`` applies only to submittable
        DocTypes. A real DocField with the same fieldname always takes
        precedence over a standard definition.
        """
        if bool(getattr(meta, "issingle", False)) or bool(getattr(meta, "is_virtual", False)):
            return []

        is_child = bool(getattr(meta, "istable", False))
        standard_fields = cls._CHILD_STANDARD_FIELDS if is_child else cls._STANDARD_FIELDS

        is_submittable = bool(getattr(meta, "is_submittable", False))
        definitions = []
        for fieldname, label, fieldtype, options, requires_submittable in standard_fields:
            if requires_submittable and not is_submittable:
                continue
            if meta.get_field(fieldname) is not None:
                continue
            definitions.append(
                {
                    "fieldname": fieldname,
                    "label": label,
                    "fieldtype": fieldtype,
                    "options": options,
                    "permlevel": 0,
                }
            )
        return definitions

    @classmethod
    def get_live_field(cls, meta: Any, fieldname: str) -> Any | None:
        """Return the live DocField, or a standard field object, for a fieldname."""
        field = meta.get_field(fieldname)
        if field is not None:
            return field

        for definition in cls.get_standard_field_definitions(meta):
            if definition["fieldname"] == fieldname:
                return frappe._dict(definition)
        return None

    @classmethod
    def get_child_table_fieldnames(cls, parent_doctype: str, child_doctype: str) -> list[str]:
        """Return the Table fieldnames of ``parent_doctype`` whose rows are ``child_doctype``.

        The result comes only from live metadata. It is empty when the parent has
        no such table, when ``child_doctype`` is not a child DocType, or when
        ``parent_doctype`` is itself a child DocType. Raises
        ``frappe.DoesNotExistError`` if either DocType does not exist.
        """
        parent_meta = frappe.get_meta(cls._validate_doctype(parent_doctype))
        child_meta = frappe.get_meta(cls._validate_doctype(child_doctype))

        if bool(getattr(parent_meta, "istable", False)) or not bool(
            getattr(child_meta, "istable", False)
        ):
            return []

        return [
            field.fieldname
            for field in parent_meta.fields
            if getattr(field, "fieldtype", None) in cls._TABLE_FIELD_TYPES
            and getattr(field, "options", None) == child_meta.name
            and getattr(field, "fieldname", None)
        ]

    @staticmethod
    def _validate_doctype(doctype: str) -> str:
        if not isinstance(doctype, str):
            frappe.throw(_("DocType must be a string"), frappe.ValidationError)

        normalized_doctype = doctype.strip()
        if not normalized_doctype:
            frappe.throw(_("DocType is required"), frappe.ValidationError)

        return normalized_doctype

    @classmethod
    def _normalize_field(cls, doctype: str, field: Any) -> dict[str, Any]:
        fieldtype = getattr(field, "fieldtype", None)
        fieldname = getattr(field, "fieldname", None)
        options = getattr(field, "options", None)

        return {
            "doctype": doctype,
            "name": fieldname,
            "label": getattr(field, "label", None),
            "fieldname": fieldname,
            "fieldtype": fieldtype,
            "options": options,
            "mandatory": bool(getattr(field, "reqd", False)),
            "read_only": bool(getattr(field, "read_only", False)),
            "hidden": bool(getattr(field, "hidden", False)),
            "link_target": options if fieldtype == "Link" else None,
            "child_doctype": options if fieldtype in cls._TABLE_FIELD_TYPES else None,
            "standard": False,
        }

    @staticmethod
    def _normalize_standard_field(
        doctype: str,
        definition: dict[str, Any],
    ) -> dict[str, Any]:
        fieldtype = definition["fieldtype"]
        options = definition["options"]

        return {
            "doctype": doctype,
            "name": definition["fieldname"],
            "label": definition["label"],
            "fieldname": definition["fieldname"],
            "fieldtype": fieldtype,
            "options": options,
            "mandatory": False,
            "read_only": True,
            "hidden": False,
            "link_target": options if fieldtype == "Link" else None,
            "child_doctype": None,
            "standard": True,
        }
