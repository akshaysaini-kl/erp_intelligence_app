"""Strict access to native Frappe site configuration values."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import frappe
from frappe import _

from erp_intelligence.utils.constants import LIMIT_DEFINITIONS, QUERY_LIMIT_NAMES

_MISSING = object()


class ConfigService:
    """Read and validate values from ``frappe.conf`` without fallback coercion."""

    @classmethod
    def get_value(cls, key: str, default: Any = _MISSING) -> Any:
        """Return a site configuration value or an explicit absent-key default.

        A configured value is returned unchanged, including ``None``. The
        default is used only when the key is not present in ``frappe.conf``.
        """
        key = cls._validate_key(key)
        site_config = cls._get_site_config()

        if key in site_config:
            return site_config[key]
        if default is not _MISSING:
            return default

        frappe.throw(
            _("Required site configuration is missing: {0}").format(key),
            frappe.ValidationError,
        )

    @classmethod
    def get_bounded_int(
        cls,
        key: str,
        *,
        default: Any = _MISSING,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """Return a strictly typed integer constrained to inclusive bounds."""
        cls._validate_integer_bounds(minimum, maximum)
        value = cls.get_value(key, default)

        if isinstance(value, bool) or not isinstance(value, int):
            frappe.throw(
                _("Site configuration {0} must be an integer").format(key),
                frappe.ValidationError,
            )
        if minimum is not None and value < minimum:
            frappe.throw(
                _("Site configuration {0} must be at least {1}").format(key, minimum),
                frappe.ValidationError,
            )
        if maximum is not None and value > maximum:
            frappe.throw(
                _("Site configuration {0} must not exceed {1}").format(key, maximum),
                frappe.ValidationError,
            )

        return value

    @classmethod
    def get_limit(cls, name: str) -> int:
        """Return the effective value of a limit defined in ``LIMIT_DEFINITIONS``.

        The site may set the limit's configuration key to lower or tune it. A
        configured value that is not an integer, is below the limit's minimum,
        or exceeds the application ceiling is rejected, never coerced or clamped.
        """
        definition = LIMIT_DEFINITIONS.get(name) if isinstance(name, str) else None
        if definition is None:
            frappe.throw(
                _("Unknown ERP Intelligence limit: {0}").format(name),
                frappe.ValidationError,
            )

        if not definition.minimum <= definition.default <= definition.ceiling:
            frappe.throw(
                _("ERP Intelligence limit definition is inconsistent: {0}").format(name),
                frappe.ValidationError,
            )

        return cls.get_bounded_int(
            definition.config_key,
            default=definition.default,
            minimum=definition.minimum,
            maximum=definition.ceiling,
        )

    @classmethod
    def get_limits(cls, *names: str) -> dict[str, int]:
        """Return effective limits for the given names, keyed by limit name."""
        return {name: cls.get_limit(name) for name in names}

    @classmethod
    def get_limit_snapshot(cls) -> Mapping[str, int]:
        """Resolve every query-related limit once and return a read-only snapshot.

        Intended to be taken once per request and passed to each service that
        enforces limits, so planning and execution cannot disagree if site
        configuration changes mid-request. It is never stored or reused.
        """
        return MappingProxyType(cls.get_limits(*QUERY_LIMIT_NAMES))

    @classmethod
    def resolve_limits(
        cls,
        *names: str,
        snapshot: Mapping[str, int] | None = None,
    ) -> dict[str, int]:
        """Return the named limits from ``snapshot``, or resolve them from config.

        Without a snapshot this is identical to ``get_limits``. A supplied
        snapshot must come from ``get_limit_snapshot``. It is re-validated
        against the application ceilings, so it cannot raise a limit.
        """
        if snapshot is None:
            return cls.get_limits(*names)

        cls._validate_limit_snapshot(snapshot)
        missing = [name for name in names if name not in snapshot]
        if missing:
            frappe.throw(
                _("Limit snapshot is missing: {0}").format(", ".join(missing)),
                frappe.ValidationError,
            )
        return {name: snapshot[name] for name in names}

    @staticmethod
    def _validate_limit_snapshot(snapshot: Any) -> None:
        if not isinstance(snapshot, MappingProxyType):
            frappe.throw(
                _("Limit snapshot must be a read-only snapshot"),
                frappe.ValidationError,
            )

        for name, value in snapshot.items():
            definition = LIMIT_DEFINITIONS.get(name) if isinstance(name, str) else None
            if (
                definition is None
                or isinstance(value, bool)
                or not isinstance(value, int)
                or not definition.minimum <= value <= definition.ceiling
            ):
                frappe.throw(
                    _("Limit snapshot contains an invalid limit: {0}").format(name),
                    frappe.ValidationError,
                )

    @classmethod
    def get_bounded_string(
        cls,
        key: str,
        *,
        maximum_length: int,
        default: Any = _MISSING,
        minimum_length: int = 0,
    ) -> str:
        """Return a string constrained to inclusive character-length bounds."""
        cls._validate_length_bounds(minimum_length, maximum_length)
        value = cls.get_value(key, default)

        if not isinstance(value, str):
            frappe.throw(
                _("Site configuration {0} must be a string").format(key),
                frappe.ValidationError,
            )

        value_length = len(value)
        if value_length < minimum_length:
            frappe.throw(
                _("Site configuration {0} must contain at least {1} characters").format(
                    key,
                    minimum_length,
                ),
                frappe.ValidationError,
            )
        if value_length > maximum_length:
            frappe.throw(
                _("Site configuration {0} must not exceed {1} characters").format(
                    key,
                    maximum_length,
                ),
                frappe.ValidationError,
            )

        return value

    @staticmethod
    def _get_site_config() -> Mapping[str, Any]:
        site_config = getattr(frappe, "conf", None)
        if not isinstance(site_config, Mapping):
            frappe.throw(
                _("Frappe site configuration is unavailable"),
                frappe.ValidationError,
            )
        return site_config

    @staticmethod
    def _validate_key(key: Any) -> str:
        if not isinstance(key, str) or not key.strip():
            frappe.throw(
                _("Configuration key must be a non-empty string"),
                frappe.ValidationError,
            )
        if key != key.strip():
            frappe.throw(
                _("Configuration key cannot contain surrounding whitespace"),
                frappe.ValidationError,
            )
        return key

    @staticmethod
    def _validate_integer_bounds(
        minimum: int | None,
        maximum: int | None,
    ) -> None:
        for name, bound in (("minimum", minimum), ("maximum", maximum)):
            if bound is not None and (
                isinstance(bound, bool) or not isinstance(bound, int)
            ):
                frappe.throw(
                    _("Integer configuration {0} bound must be an integer").format(name),
                    frappe.ValidationError,
                )

        if minimum is not None and maximum is not None and minimum > maximum:
            frappe.throw(
                _("Integer configuration minimum cannot exceed maximum"),
                frappe.ValidationError,
            )

    @staticmethod
    def _validate_length_bounds(minimum: int, maximum: int) -> None:
        for name, bound in (("minimum_length", minimum), ("maximum_length", maximum)):
            if isinstance(bound, bool) or not isinstance(bound, int):
                frappe.throw(
                    _("String configuration {0} must be an integer").format(name),
                    frappe.ValidationError,
                )
            if bound < 0:
                frappe.throw(
                    _("String configuration {0} cannot be negative").format(name),
                    frappe.ValidationError,
                )

        if minimum > maximum:
            frappe.throw(
                _("String configuration minimum length cannot exceed maximum length"),
                frappe.ValidationError,
            )
