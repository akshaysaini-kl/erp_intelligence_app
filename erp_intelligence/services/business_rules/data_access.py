"""Read-only ERP data access for rule evaluators.

Every read goes through the existing ERP Intelligence services:

* record, child-table and aggregation reads use ``IntelligenceService.execute_request``
  (planner, field policy, query validator, permission-aware ``frappe.get_list``);
* field discovery uses ``MetadataService``.

Nothing here talks to the database directly and nothing bypasses permissions: reads
run as the current session user. A field or record the user cannot read is reported
as a data error, never as an empty successful result.
"""

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import frappe

from erp_intelligence.services.business_rules.errors import RuleDataError
from erp_intelligence.services.config_service import ConfigService
from erp_intelligence.services.intelligence_service import IntelligenceService
from erp_intelligence.services.metadata_service import MetadataService

Filter = tuple[str, str, Any]

_DEFAULT_PAGE_LENGTH = 100
_MAX_NAMES_PER_REQUEST = 50


class RuleDataAccess:
    """Retrieve ERP data for one engine run.

    Identical requests inside one run are answered from memory, so rules that need
    the same record do not query it twice. Create a new instance for every run so
    a later stage never reads a stale answer.
    """

    def __init__(
        self,
        *,
        user: str | None = None,
        intelligence_service: Any = None,
        metadata_service: Any = None,
    ) -> None:
        self._user = user
        self._intelligence = intelligence_service or IntelligenceService
        self._metadata = metadata_service or MetadataService
        self._memo: dict[str, Any] = {}
        self._field_cache: dict[str, set[str]] = {}
        self.request_count = 0

    # ------------------------------------------------------------------ metadata
    def available_fields(self, doctype: str) -> set[str]:
        """Return the fieldnames of ``doctype`` from live metadata."""
        if doctype not in self._field_cache:
            message_mark = self._message_mark()
            try:
                metadata = self._metadata.get_doctype_metadata(doctype)
            except Exception as exc:  # noqa: BLE001 - reported as a data error
                raise RuleDataError(
                    f"Metadata for {doctype} is unavailable: {exc}", code="metadata_unavailable"
                ) from None
            finally:
                self._discard_messages(message_mark)
            self._field_cache[doctype] = {
                field["fieldname"]
                for field in metadata["fields"]
                if isinstance(field.get("fieldname"), str)
            }
        return set(self._field_cache[doctype])

    def require_fields(self, doctype: str, fieldnames: Iterable[str]) -> None:
        """Raise ``RuleDataError`` if any field is missing from the live DocType."""
        missing = sorted(set(fieldnames) - self.available_fields(doctype))
        if missing:
            raise RuleDataError(
                f"{doctype} has no field(s): {', '.join(missing)}", code="field_unavailable"
            )

    # ------------------------------------------------------------------- records
    def fetch_records(
        self,
        doctype: str,
        fields: Sequence[str],
        filters: Sequence[Filter] = (),
        *,
        sort: Sequence[tuple[str, str]] = (),
        page_length: int | None = None,
        require_complete: bool = True,
    ) -> list[dict[str, Any]]:
        """Return rows of ``doctype`` the current user may read.

        With ``require_complete`` (the default) a result that did not fit in one page
        raises ``RuleDataError`` instead of returning a partial list.
        """
        self.require_fields(doctype, [*fields, *(item[0] for item in filters)])
        request = {
            "doctype": doctype,
            "fields": list(fields),
            "filters": self._filters(filters),
            "page_length": self._page_length(page_length),
        }
        if sort:
            request["sort"] = [{"field": field, "direction": direction} for field, direction in sort]
        result = self._execute(request)
        return self._records(doctype, result, require_complete)

    def fetch_by_names(
        self,
        doctype: str,
        names: Sequence[str],
        fields: Sequence[str],
        filters: Sequence[Filter] = (),
    ) -> list[dict[str, Any]]:
        """Return the rows whose ``name`` is in ``names``, requesting in bounded chunks."""
        unique = list(dict.fromkeys(names))
        chunk_size = min(_MAX_NAMES_PER_REQUEST, ConfigService.get_limit("filter_values"))
        rows: list[dict[str, Any]] = []
        for start in range(0, len(unique), chunk_size):
            chunk = unique[start : start + chunk_size]
            rows.extend(
                self.fetch_records(
                    doctype,
                    fields,
                    [("name", "in", chunk), *filters],
                    page_length=len(chunk),
                )
            )
        return rows

    def fetch_child_records(
        self,
        child_doctype: str,
        parent_doctype: str,
        parent_names: Sequence[str],
        fields: Sequence[str],
        filters: Sequence[Filter] = (),
    ) -> list[dict[str, Any]]:
        """Return child rows of explicitly named parents, through ``parent_context``.

        Rows are returned only for parents the current user may read. Include
        ``parent`` in ``fields`` to tell the rows apart.
        """
        unique = list(dict.fromkeys(parent_names))
        if not unique:
            return []
        self.require_fields(child_doctype, [*fields, *(item[0] for item in filters)])
        chunk_size = min(_MAX_NAMES_PER_REQUEST, ConfigService.get_limit("parent_names"))
        rows: list[dict[str, Any]] = []
        for start in range(0, len(unique), chunk_size):
            rows.extend(
                self._child_chunk(
                    child_doctype, parent_doctype, unique[start : start + chunk_size], fields, filters
                )
            )
        return rows

    def _child_chunk(
        self,
        child_doctype: str,
        parent_doctype: str,
        parents: list[str],
        fields: Sequence[str],
        filters: Sequence[Filter],
    ) -> list[dict[str, Any]]:
        """Read one chunk of parents. A chunk that overflows one page is split and retried.

        A single parent whose rows still overflow raises ``RuleDataError``: a partial
        child list is never returned.
        """
        request = {
            "doctype": child_doctype,
            "parent_context": {"parent_doctype": parent_doctype, "parent_names": parents},
            "fields": list(fields),
            "filters": self._filters(filters),
            "page_length": ConfigService.get_limit("page_length"),
        }
        result = self._execute(request)
        execution = result["execution_result"]
        if execution.get("has_more") is True and len(parents) > 1:
            middle = len(parents) // 2
            return [
                *self._child_chunk(child_doctype, parent_doctype, parents[:middle], fields, filters),
                *self._child_chunk(child_doctype, parent_doctype, parents[middle:], fields, filters),
            ]
        return self._records(child_doctype, result, True)

    def count_records(self, doctype: str, filters: Sequence[Filter] = ()) -> int:
        """Return how many rows of ``doctype`` the current user may read, using aggregation."""
        self.require_fields(doctype, ["name", *(item[0] for item in filters)])
        request = {
            "doctype": doctype,
            "aggregations": [{"operation": "count", "field": "name"}],
            "filters": self._filters(filters),
        }
        result = self._execute(request)
        execution = result["execution_result"]
        rows = execution.get("results")
        aggregations = execution.get("aggregations")
        alias = (
            aggregations[0].get("result_field")
            if isinstance(aggregations, list) and len(aggregations) == 1
            else None
        )
        if (
            execution.get("result_type") != "aggregation"
            or execution.get("truncated") is not False
            or not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(alias, str)
            or isinstance(rows[0].get(alias), bool)
            or not isinstance(rows[0].get(alias), int)
        ):
            raise RuleDataError("Count aggregation returned an unexpected result", code="incomplete")
        return rows[0][alias]

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _filters(filters: Sequence[Filter]) -> list[dict[str, Any]]:
        return [
            {"field": field, "operator": operator, "value": value}
            for field, operator, value in filters
        ]

    @staticmethod
    def _page_length(requested: int | None) -> int:
        maximum = ConfigService.get_limit("page_length")
        return max(1, min(requested or _DEFAULT_PAGE_LENGTH, maximum))

    @staticmethod
    def _records(
        doctype: str,
        result: Mapping[str, Any],
        require_complete: bool,
    ) -> list[dict[str, Any]]:
        execution = result["execution_result"]
        records = execution.get("records")
        if execution.get("result_type") != "records" or not isinstance(records, list):
            raise RuleDataError(f"{doctype} query returned an unexpected result", code="incomplete")
        if require_complete and execution.get("has_more") is not False:
            raise RuleDataError(
                f"{doctype} query has more rows than one page. The evidence is incomplete.",
                code="incomplete",
            )
        return [dict(record) for record in records]

    def _execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Run one request through IntelligenceService and return an ``executed`` response."""
        key = json.dumps(request, sort_keys=True, default=str)
        if key in self._memo:
            return copy.deepcopy(self._memo[key])

        message_mark = self._message_mark()
        self.request_count += 1
        try:
            response = self._intelligence.execute_request(dict(request), user=self._user)
        except frappe.PermissionError as exc:
            raise RuleDataError(
                f"Permission denied reading {request.get('doctype')}: {exc}",
                code="permission_denied",
            ) from None
        except frappe.ValidationError as exc:
            raise RuleDataError(
                f"ERP Intelligence rejected the {request.get('doctype')} request: {exc}",
                code="request_invalid",
            ) from None
        finally:
            # frappe.throw queues user-facing messages. They belong to a failed
            # internal read, not to the caller's response.
            self._discard_messages(message_mark)

        if not isinstance(response, Mapping) or response.get("status") != "executed":
            status = response.get("status") if isinstance(response, Mapping) else None
            raise RuleDataError(
                f"{request.get('doctype')} request was not executed (status {status!r}). "
                "A field may be ambiguous or not readable by the current user.",
                code="unresolved",
            )
        self._memo[key] = copy.deepcopy(dict(response))
        return dict(response)

    @staticmethod
    def _message_mark() -> int | None:
        log = getattr(frappe.local, "message_log", None)
        return len(log) if isinstance(log, list) else None

    @staticmethod
    def _discard_messages(mark: int | None) -> None:
        log = getattr(frappe.local, "message_log", None)
        if mark is not None and isinstance(log, list):
            del log[mark:]
