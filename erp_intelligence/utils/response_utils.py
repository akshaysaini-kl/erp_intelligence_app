"""Generic response construction and JSON-safe value conversion utilities."""

import datetime
import math
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from itertools import islice
from typing import Any

from erp_intelligence.utils.constants import RESPONSE_METADATA_KEYS


def build_response_metadata(
    result_type: str,
    *,
    doctype: str | None = None,
    returned_count: int | None = None,
    has_more: bool | None = None,
    truncated: bool | None = None,
) -> dict[str, Any]:
    """Build validated metadata without inventing counts or completion state."""
    result_type = _require_non_empty_string(result_type, "result_type")
    metadata: dict[str, Any] = {"result_type": result_type}

    if doctype is not None:
        metadata["doctype"] = _require_non_empty_string(doctype, "doctype")
    if returned_count is not None:
        metadata["returned_count"] = _require_non_negative_integer(
            returned_count,
            "returned_count",
        )
    if has_more is not None:
        metadata["has_more"] = _require_boolean(has_more, "has_more")
    if truncated is not None:
        metadata["truncated"] = _require_boolean(truncated, "truncated")

    return metadata


def build_response(
    result_type: str,
    *,
    payload: Mapping[str, Any] | None = None,
    doctype: str | None = None,
    returned_count: int | None = None,
    has_more: bool | None = None,
    truncated: bool | None = None,
) -> dict[str, Any]:
    """Combine response metadata with a JSON-safe domain payload."""
    metadata = build_response_metadata(
        result_type,
        doctype=doctype,
        returned_count=returned_count,
        has_more=has_more,
        truncated=truncated,
    )
    if payload is None:
        return metadata
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    conflicting_keys = set(payload) & RESPONSE_METADATA_KEYS
    if conflicting_keys:
        raise ValueError(
            "payload cannot override response metadata keys: "
            + ", ".join(sorted(str(key) for key in conflicting_keys))
        )

    safe_payload = to_json_safe(payload)
    return {**metadata, **safe_payload}


def build_collection_response(
    result_type: str,
    items: Sequence[Any],
    *,
    item_key: str = "items",
    doctype: str | None = None,
    has_more: bool | None = None,
    truncated: bool | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a collection response whose returned count is derived from items."""
    item_key = _validate_item_key(item_key)
    materialized_items = _materialize_sequence(items)
    payload = _copy_extra(extra, reserved_keys={item_key})
    payload[item_key] = materialized_items

    return build_response(
        result_type,
        payload=payload,
        doctype=doctype,
        returned_count=len(materialized_items),
        has_more=has_more,
        truncated=truncated,
    )


def build_paginated_response(
    result_type: str,
    items: Sequence[Any],
    *,
    page_length: int,
    offset: int,
    has_more: bool,
    item_key: str = "records",
    doctype: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a page response while preserving the caller's has_more result."""
    page_length = _require_positive_integer(page_length, "page_length")
    offset = _require_non_negative_integer(offset, "offset")
    has_more = _require_boolean(has_more, "has_more")
    materialized_items = _materialize_sequence(items)
    if len(materialized_items) > page_length:
        raise ValueError("items exceed page_length; truncate before building the response")

    page_extra = _copy_extra(extra, reserved_keys={"page_length", "offset"})
    page_extra.update({"page_length": page_length, "offset": offset})
    return build_collection_response(
        result_type,
        materialized_items,
        item_key=item_key,
        doctype=doctype,
        has_more=has_more,
        extra=page_extra,
    )


def build_truncated_response(
    result_type: str,
    items: Iterable[Any],
    *,
    limit: int,
    item_key: str = "items",
    doctype: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume at most limit plus one item and report truncation explicitly."""
    limit = _require_non_negative_integer(limit, "limit")
    if isinstance(items, (str, bytes, bytearray, Mapping)):
        raise TypeError("items must be an iterable of response values")

    iterator = iter(items)
    bounded_items = list(islice(iterator, limit + 1))
    truncated = len(bounded_items) > limit
    if truncated:
        bounded_items.pop()

    return build_collection_response(
        result_type,
        bounded_items,
        item_key=item_key,
        doctype=doctype,
        truncated=truncated,
        extra=extra,
    )


def to_json_safe(value: Any) -> Any:
    """Recursively convert supported values into standard JSON-compatible data.

    Decimal and timedelta values become exact strings so conversion does not
    introduce binary floating-point rounding. Unsupported values and cyclic
    containers raise rather than being silently stringified or omitted.
    """
    return _to_json_safe(value, seen=set())


def _to_json_safe(value: Any, *, seen: set[int]) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not JSON-safe")
        return value

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimals are not JSON-safe")
        return format(value, "f")

    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        seconds = (
            Decimal(value.days) * Decimal(86_400)
            + Decimal(value.seconds)
            + Decimal(value.microseconds) / Decimal(1_000_000)
        )
        return format(seconds, "f")

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ValueError("cyclic mappings are not JSON-safe")
        seen.add(identity)
        try:
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("JSON object keys must be strings")
                result[key] = _to_json_safe(item, seen=seen)
            return result
        finally:
            seen.remove(identity)

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise ValueError("cyclic sequences are not JSON-safe")
        seen.add(identity)
        try:
            return [_to_json_safe(item, seen=seen) for item in value]
        finally:
            seen.remove(identity)

    raise TypeError(f"Unsupported JSON response value: {type(value).__name__}")


def _materialize_sequence(items: Sequence[Any]) -> list[Any]:
    if isinstance(items, (str, bytes, bytearray)) or not isinstance(items, Sequence):
        raise TypeError("items must be a sequence")
    return list(items)


def _copy_extra(
    extra: Mapping[str, Any] | None,
    *,
    reserved_keys: set[str],
) -> dict[str, Any]:
    if extra is None:
        return {}
    if not isinstance(extra, Mapping):
        raise TypeError("extra must be a mapping")

    conflicting_keys = set(extra) & (reserved_keys | RESPONSE_METADATA_KEYS)
    if conflicting_keys:
        raise ValueError(
            "extra cannot override response keys: "
            + ", ".join(sorted(str(key) for key in conflicting_keys))
        )
    return dict(extra)


def _validate_item_key(item_key: str) -> str:
    item_key = _require_non_empty_string(item_key, "item_key")
    if item_key in RESPONSE_METADATA_KEYS:
        raise ValueError(f"item_key is reserved: {item_key}")
    return item_key


def _require_non_empty_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _require_boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _require_non_negative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _require_positive_integer(value: Any, name: str) -> int:
    value = _require_non_negative_integer(value, name)
    if value == 0:
        raise ValueError(f"{name} must be greater than zero")
    return value
