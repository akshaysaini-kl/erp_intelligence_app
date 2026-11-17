"""Permission-aware ERP metadata terminology and relationship resolution."""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

import frappe
from frappe import _

from erp_intelligence.services.field_policy import FieldPolicy
from erp_intelligence.services.metadata_service import MetadataService
from erp_intelligence.utils.constants import TABLE_FIELD_TYPES


class ContextService:
    """Resolve DocType and field terminology without reading ERP records."""

    MAX_TERM_LENGTH = 256
    _CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")

    @classmethod
    def resolve_doctype(
        cls,
        value: str,
        user: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an authorized top-level DocType name or translated label."""
        result, _context = cls._resolve_doctype_context(value, user)
        return result

    @classmethod
    def resolve_field(
        cls,
        doctype: str,
        value: str,
        user: str | None = None,
        *,
        parent_doctype: str | None = None,
        parent_fieldname: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an authorized fieldname or label within one DocType.

        Child DocTypes cannot be resolved by name, so their fields are resolved
        with an explicit parent context. Access is then decided by FieldPolicy
        under that parent, exactly as for any other child-table read.
        """
        field_query = cls._validate_term(value, "field")
        if parent_doctype is None and parent_fieldname is None:
            doctype_result, context = cls._resolve_doctype_context(doctype, user)
            if context is None:
                return {
                    "resolved_value": None,
                    "resolution_type": "doctype",
                    "match_status": f"doctype_{doctype_result['match_status']}",
                    "confidence": "none",
                    "candidates": doctype_result["candidates"],
                    "doctype": None,
                }

            metadata = context["metadata"]
            descriptor = context["descriptor"]
            policy = FieldPolicy.get_field_policy(metadata, user=context["user"])
        else:
            metadata, descriptor, policy = cls._child_field_policy(
                doctype,
                user,
                parent_doctype,
                parent_fieldname,
            )

        readable_fields = policy.get("readable_fields")
        if not isinstance(readable_fields, list):
            frappe.throw(
                _("Unable to establish authorized field context"),
                frappe.PermissionError,
            )

        readable_set = set(readable_fields)
        candidates = [
            cls._field_candidate(field, metadata["doctype"])
            for field in metadata["fields"]
            if field.get("fieldname") in readable_set
        ]
        result = cls._resolve_candidates(
            query=field_query,
            candidates=candidates,
            exact_name_key="fieldname",
            resolution_prefix="field",
        )
        result["doctype"] = descriptor
        return result

    @classmethod
    def _child_field_policy(
        cls,
        doctype: str,
        user: str | None,
        parent_doctype: str | None,
        parent_fieldname: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        authenticated_user = cls._get_authenticated_user(user)
        if (
            not isinstance(parent_doctype, str)
            or not parent_doctype
            or not isinstance(parent_fieldname, str)
            or not parent_fieldname
        ):
            frappe.throw(
                _("parent_doctype and parent_fieldname must be provided together"),
                frappe.ValidationError,
            )

        metadata = MetadataService.get_doctype_metadata(doctype)
        if not metadata.get("istable"):
            frappe.throw(
                _("Parent context is only valid for child DocTypes"),
                frappe.ValidationError,
            )

        policy = FieldPolicy.get_field_policy(
            metadata,
            user=authenticated_user,
            parent_doctype=parent_doctype,
            parent_fieldname=parent_fieldname,
        )
        descriptor = {
            "candidate_type": "doctype",
            "name": metadata["doctype"],
            "label": cls._doctype_label(metadata["label"]),
        }
        return metadata, descriptor, policy

    @classmethod
    def discover_relationships(
        cls,
        doctype: str,
        user: str | None = None,
    ) -> dict[str, Any]:
        """Return authorized outgoing Link and child-table relationships."""
        doctype_result, context = cls._resolve_doctype_context(doctype, user)
        if context is None:
            return {
                "resolved_value": None,
                "resolution_type": "relationships",
                "match_status": f"doctype_{doctype_result['match_status']}",
                "confidence": "none",
                "candidates": doctype_result["candidates"],
                "relationships": [],
            }

        metadata = context["metadata"]
        policy = FieldPolicy.get_field_policy(metadata, user=context["user"])
        readable_fields = policy.get("readable_fields")
        if not isinstance(readable_fields, list):
            frappe.throw(
                _("Unable to establish authorized relationship context"),
                frappe.PermissionError,
            )

        readable_set = set(readable_fields)
        relationships = []
        for field in metadata["fields"]:
            fieldtype = field.get("fieldtype")
            if fieldtype == "Link" and field.get("fieldname") in readable_set:
                relationship = cls._resolve_link_relationship(field, context)
                if relationship is not None:
                    relationships.append(relationship)
            elif fieldtype in TABLE_FIELD_TYPES:
                relationship = cls._resolve_table_relationship(field, context)
                if relationship is not None:
                    relationships.append(relationship)

        return {
            "resolved_value": context["descriptor"],
            "resolution_type": "relationships",
            "match_status": "resolved",
            "confidence": "exact",
            "candidates": [],
            "relationships": relationships,
        }

    @classmethod
    def _resolve_doctype_context(
        cls,
        value: str,
        user: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        query = cls._validate_term(value, "DocType")
        authenticated_user = cls._get_authenticated_user(user)
        authorized_names = cls._get_authorized_doctype_names(authenticated_user)

        matching_stages = (
            (
                "exact_name",
                "exact",
                [name for name in authorized_names if name == query],
            ),
            (
                "exact_label",
                "exact",
                [name for name in authorized_names if cls._doctype_label(name) == query],
            ),
            (
                "case_insensitive_name",
                "case_insensitive",
                [
                    name
                    for name in authorized_names
                    if cls._normalize_match_value(name) == cls._normalize_match_value(query)
                ],
            ),
            (
                "case_insensitive_label",
                "case_insensitive",
                [
                    name
                    for name in authorized_names
                    if cls._normalize_match_value(cls._doctype_label(name))
                    == cls._normalize_match_value(query)
                ],
            ),
        )

        for resolution_type, confidence, names in matching_stages:
            contexts = []
            for name in names:
                context = cls._authorized_doctype_context(
                    name,
                    authenticated_user,
                    authorized_names,
                )
                if context is not None:
                    contexts.append(context)

            contexts = cls._deduplicate_contexts(contexts)
            if len(contexts) == 1:
                context = contexts[0]
                return (
                    {
                        "resolved_value": context["descriptor"],
                        "resolution_type": resolution_type,
                        "match_status": "resolved",
                        "confidence": confidence,
                        "candidates": [],
                    },
                    context,
                )
            if len(contexts) > 1:
                candidates = cls._sort_candidates(
                    [context["descriptor"] for context in contexts]
                )
                return (
                    {
                        "resolved_value": None,
                        "resolution_type": resolution_type,
                        "match_status": "ambiguous",
                        "confidence": confidence,
                        "candidates": candidates,
                    },
                    None,
                )

        return (
            {
                "resolved_value": None,
                "resolution_type": None,
                "match_status": "not_found",
                "confidence": "none",
                "candidates": [],
            },
            None,
        )

    @classmethod
    def _get_authorized_doctype_names(cls, user: str) -> list[str]:
        permission_inventory = frappe.get_user()
        if getattr(permission_inventory, "name", None) != user:
            frappe.throw(
                _("Unable to establish authorized DocType context"),
                frappe.PermissionError,
            )

        if not hasattr(permission_inventory, "doctype_map"):
            permission_inventory.build_permissions()

        all_read = getattr(permission_inventory, "all_read", None)
        can_select = getattr(permission_inventory, "can_select", None)
        if not cls._is_string_sequence(all_read) or not cls._is_string_sequence(can_select):
            frappe.throw(
                _("Unable to establish authorized DocType context"),
                frappe.PermissionError,
            )

        return sorted(set(all_read) | set(can_select), key=cls._normalize_match_value)

    @classmethod
    def _authorized_doctype_context(
        cls,
        name: str,
        user: str,
        authorized_names: Sequence[str],
    ) -> dict[str, Any] | None:
        if name not in authorized_names:
            return None
        if not (
            frappe.has_permission(name, ptype="read", user=user)
            or frappe.has_permission(name, ptype="select", user=user)
        ):
            return None

        try:
            metadata = MetadataService.get_doctype_metadata(name)
        except frappe.DoesNotExistError:
            return None

        if metadata.get("doctype") != name or metadata.get("name") != name:
            return None
        if metadata.get("istable"):
            return None

        descriptor = {
            "candidate_type": "doctype",
            "name": metadata["doctype"],
            "label": cls._doctype_label(metadata["label"]),
        }
        return {
            "descriptor": descriptor,
            "metadata": metadata,
            "authorized_names": authorized_names,
            "user": user,
        }

    @classmethod
    def _resolve_candidates(
        cls,
        *,
        query: str,
        candidates: list[dict[str, Any]],
        exact_name_key: str,
        resolution_prefix: str,
    ) -> dict[str, Any]:
        stages = (
            (
                f"exact_{resolution_prefix}_name",
                "exact",
                [candidate for candidate in candidates if candidate[exact_name_key] == query],
            ),
            (
                f"exact_{resolution_prefix}_label",
                "exact",
                [
                    candidate
                    for candidate in candidates
                    if query in cls._candidate_match_labels(candidate)
                ],
            ),
            (
                f"case_insensitive_{resolution_prefix}_name",
                "case_insensitive",
                [
                    candidate
                    for candidate in candidates
                    if cls._normalize_match_value(candidate[exact_name_key])
                    == cls._normalize_match_value(query)
                ],
            ),
            (
                f"case_insensitive_{resolution_prefix}_label",
                "case_insensitive",
                [
                    candidate
                    for candidate in candidates
                    if any(
                        cls._normalize_match_value(label)
                        == cls._normalize_match_value(query)
                        for label in cls._candidate_match_labels(candidate)
                    )
                ],
            ),
        )

        for resolution_type, confidence, matches in stages:
            matches = cls._deduplicate_candidates(matches, exact_name_key)
            if len(matches) == 1:
                return {
                    "resolved_value": cls._public_candidate(matches[0]),
                    "resolution_type": resolution_type,
                    "match_status": "resolved",
                    "confidence": confidence,
                    "candidates": [],
                }
            if len(matches) > 1:
                return {
                    "resolved_value": None,
                    "resolution_type": resolution_type,
                    "match_status": "ambiguous",
                    "confidence": confidence,
                    "candidates": cls._sort_candidates(
                        [cls._public_candidate(candidate) for candidate in matches]
                    ),
                }

        return {
            "resolved_value": None,
            "resolution_type": None,
            "match_status": "not_found",
            "confidence": "none",
            "candidates": [],
        }

    @classmethod
    def _resolve_link_relationship(
        cls,
        field: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        target = field.get("link_target")
        if not isinstance(target, str) or not target:
            return None

        target_context = cls._authorized_doctype_context(
            target,
            context["user"],
            context["authorized_names"],
        )
        if target_context is None:
            return None

        return {
            "kind": "link",
            "field": cls._relationship_field(field, context["metadata"]["doctype"]),
            "target": target_context["descriptor"],
        }

    @classmethod
    def _resolve_table_relationship(
        cls,
        field: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        fieldname = field.get("fieldname")
        child_doctype = field.get("child_doctype")
        if not isinstance(fieldname, str) or not fieldname:
            return None
        if not isinstance(child_doctype, str) or not child_doctype:
            return None

        try:
            child_metadata = MetadataService.get_doctype_metadata(child_doctype)
        except frappe.DoesNotExistError:
            return None

        child_policy = FieldPolicy.get_field_policy(
            child_metadata,
            user=context["user"],
            parent_doctype=context["metadata"]["doctype"],
            parent_fieldname=fieldname,
        )
        child_readable = child_policy.get("readable_fields")
        if not isinstance(child_readable, list) or not child_readable:
            return None

        kind = "table_multiselect" if field.get("fieldtype") == "Table MultiSelect" else "table"
        return {
            "kind": kind,
            "field": cls._relationship_field(field, context["metadata"]["doctype"]),
            "target": {
                "candidate_type": "doctype",
                "name": child_metadata["doctype"],
                "label": cls._doctype_label(child_metadata["label"]),
            },
            "parent_context": {
                "parent_doctype": context["metadata"]["doctype"],
                "parent_fieldname": fieldname,
            },
        }

    @classmethod
    def _field_candidate(
        cls,
        field: Mapping[str, Any],
        doctype: str,
    ) -> dict[str, Any]:
        fieldname = field["fieldname"]
        raw_label = field.get("label") or fieldname
        translated_label = cls._field_label(raw_label, doctype)
        return {
            "candidate_type": "field",
            "fieldname": fieldname,
            "label": translated_label,
            "fieldtype": field.get("fieldtype"),
            "_match_labels": tuple(dict.fromkeys((raw_label, translated_label))),
        }

    @classmethod
    def _relationship_field(
        cls,
        field: Mapping[str, Any],
        doctype: str,
    ) -> dict[str, Any]:
        fieldname = field["fieldname"]
        return {
            "fieldname": fieldname,
            "label": cls._field_label(field.get("label") or fieldname, doctype),
            "fieldtype": field.get("fieldtype"),
        }

    @staticmethod
    def _doctype_label(value: str) -> str:
        return _(value)

    @staticmethod
    def _field_label(value: str, doctype: str) -> str:
        return _(value, context=doctype)

    @staticmethod
    def _candidate_match_labels(candidate: Mapping[str, Any]) -> tuple[str, ...]:
        labels = candidate.get("_match_labels")
        if isinstance(labels, tuple) and all(isinstance(label, str) for label in labels):
            return labels
        return (candidate["label"],)

    @staticmethod
    def _public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in candidate.items()
            if not key.startswith("_")
        }

    @classmethod
    def _validate_term(cls, value: Any, name: str) -> str:
        if not isinstance(value, str):
            frappe.throw(_("{0} must be a string").format(name), frappe.ValidationError)

        value = value.strip()
        if not value:
            frappe.throw(_("{0} is required").format(name), frappe.ValidationError)
        if len(value) > cls.MAX_TERM_LENGTH:
            frappe.throw(
                _("{0} cannot exceed {1} characters").format(name, cls.MAX_TERM_LENGTH),
                frappe.ValidationError,
            )
        if cls._CONTROL_CHARACTER_PATTERN.search(value):
            frappe.throw(
                _("{0} contains control characters").format(name),
                frappe.ValidationError,
            )
        return value

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
                    _("Context can only be resolved for the current session user"),
                    frappe.PermissionError,
                )

        return session_user

    @staticmethod
    def _normalize_match_value(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value)
        return " ".join(normalized.split()).casefold()

    @staticmethod
    def _is_string_sequence(value: Any) -> bool:
        return (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes, bytearray))
            and all(isinstance(item, str) and item for item in value)
        )

    @staticmethod
    def _deduplicate_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduplicated = {}
        for context in contexts:
            deduplicated[context["descriptor"]["name"]] = context
        return list(deduplicated.values())

    @staticmethod
    def _deduplicate_candidates(
        candidates: list[dict[str, Any]],
        name_key: str,
    ) -> list[dict[str, Any]]:
        deduplicated = {}
        for candidate in candidates:
            deduplicated[candidate[name_key]] = candidate
        return list(deduplicated.values())

    @classmethod
    def _sort_candidates(cls, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            candidates,
            key=lambda candidate: (
                cls._normalize_match_value(candidate.get("label") or ""),
                cls._normalize_match_value(
                    candidate.get("name") or candidate.get("fieldname") or ""
                ),
            ),
        )
