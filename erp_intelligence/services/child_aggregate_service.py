"""Child-table aggregation executor: rank parent documents by an aggregate of their child rows.

Example question: "Which Purchase Order has the most item rows?". The executor counts
rows of ``Purchase Order Item`` per ``parent`` inside the database and returns only
``(parent, value, rank)``. No child row is ever returned.

Execution (resolve-then-aggregate)
    1. Validate everything, with no record reads: request shape, aggregations, ranking,
       the parent/child relationship, the child Table-field permission and field
       permissions (``QueryValidator`` and ``FieldPolicy`` under the parent context),
       and the parent filters.
    2. Phase A. Resolve ALL accessible parent names with ``frappe.get_list`` on the
       parent DocType, by keyset pagination on ``name``. Frappe applies role
       permissions, User Permissions, owner rules, shares and permission query
       conditions. The result is sealed into an ``AccessibleParents`` grant.
    3. Phase B. Count child rows grouped by parent, in batches, with
       ``frappe.get_list`` on the child DocType. The child query verifies the grant
       first (HMAC seal, session user, site, this call, and the exact child DocType,
       parent DocType and Table field) and refuses anything else with
       ``frappe.PermissionError`` before a query is built. So a child query can only
       name parents that phase A returned for this user in this call. A bare child
       ``get_list`` does NOT hide inaccessible parents (verified on a live site), which
       is why this ordering is mandatory.
    4. Merge per-batch results with ``RankingMerger`` (bounded top-N with ties).

Outcomes (this classification is deliberate and is pinned by tests)
    Success
        ``result_type == "child_aggregation"``, ``status == "completed"``.
    Structured failure, returned and NOT raised: the three expected operational limits
        scan limit, time budget and tie cap. ``result_type ==
        "child_aggregation_failure"``, ``status == "failed"``, ``complete == False``, an
        ``error_code``, the applicable ``limit`` and ``guidance``. Never a ranking,
        never ``results`` or ``returned_count``, never any claim of completeness.
        Codes: ``child_aggregation_scan_limit_exceeded``,
        ``child_aggregation_time_budget_exceeded``,
        ``child_aggregation_tie_cap_exceeded``. Returning them keeps the normal
        exception path, which FAC logs as an Error Log entry, for genuine faults.
    ``frappe.ValidationError``, raised: a detected transaction interruption
        (``child_aggregation_snapshot_interrupted``). It is an abnormal condition
        somewhere else in the request, not an expected limit, so it must stay visible
        on the exception path. Also raised: an invalid request and a broken database
        invariant.
    ``frappe.PermissionError``, raised: permission denial, including a parent grant
        that is forged, tampered with, stale or belongs to another user or call.

Snapshot guarantee and its limits
    A request runs on one database connection in one transaction (MariaDB
    REPEATABLE READ, autocommit off), and nothing in ERP Intelligence commits or rolls
    back. This module issues no commit, rollback, begin, savepoint or isolation
    change. It only registers two inert callbacks (see ``_TransactionTripwire``) that
    record a ``frappe.db.commit()`` or ``frappe.db.rollback()`` made by other code
    during the scan. If one is recorded, the request FAILS and returns no ranking.

    The detector does NOT detect every possible transaction boundary. See the class
    docstring for what it cannot see. Nothing here verifies the isolation level.
"""

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe import _

from erp_intelligence.services.aggregate_service import AggregateService
from erp_intelligence.services.config_service import ConfigService
from erp_intelligence.services.metadata_service import MetadataService
from erp_intelligence.services.query_validator import QueryValidator
from erp_intelligence.services.ranking_merger import RankingMerger, TieCapExceededError
from erp_intelligence.utils.constants import (
    CHILD_AGGREGATION_LIMIT_NAMES,
    LIMIT_DEFINITIONS,
    NUMERIC_AGGREGATION_FIELD_TYPES,
)
from erp_intelligence.utils.response_utils import build_collection_response, build_response

RESULT_TYPE = "child_aggregation"
FAILURE_RESULT_TYPE = "child_aggregation_failure"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

ERROR_SCAN_LIMIT = "child_aggregation_scan_limit_exceeded"
ERROR_TIME_BUDGET = "child_aggregation_time_budget_exceeded"
ERROR_TIE_CAP = "child_aggregation_tie_cap_exceeded"
# Not a structured failure: a detected interruption is an abnormal condition and
# uses the exception path.
ERROR_SNAPSHOT_INTERRUPTED = "child_aggregation_snapshot_interrupted"

_SHARED_LIMIT_NAMES = ("aggregations", "returned_groups", "aggregate_complexity")



# --------------------------------------------------------------------------- permission grant
def _make_sealer():
    """Create the signing functions. The key lives only in this closure.

    It is random per process, never stored, logged or returned, and not reachable as a
    module attribute. A grant sealed by one process cannot be verified by another.
    """
    key = secrets.token_bytes(32)

    def seal(*fields: Any) -> str:
        message = json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def matches(candidate: Any, *fields: Any) -> bool:
        return isinstance(candidate, str) and hmac.compare_digest(candidate, seal(*fields))

    return seal, matches


_seal, _seal_matches = _make_sealer()


@dataclass(frozen=True, repr=False)
class AccessibleParents:
    """A grant: these parent names were returned by ``frappe.get_list`` for this user.

    Anyone can construct this object, so constructing it proves nothing. What makes it
    usable is ``seal``: an HMAC, under a per-process secret, over every field below.
    The child query verifies it, together with the live execution context, before it
    runs (see ``_ExecutionContext.verify``). A grant is therefore refused when it
    * was not issued by ``_ExecutionContext.issue`` (forged, or sealed by another process),
    * has had any field changed after issue (including through ``object.__setattr__``),
    * belongs to another user, another site, or another call, including a finished one,
    * is for a different child DocType, parent DocType or Table field than the query.

    Limits of this control: it defends against accidental reuse, stale reuse, cross-user
    and cross-request reuse, and tampering. It cannot defend against code that runs in
    the same process and deliberately reads the closure that holds the signing key. It
    is not a substitute for Frappe's own permission checks. It is evidence that parent
    resolution through those checks ran for this user, in this call.
    """

    child_doctype: str
    parent_doctype: str
    parent_fieldname: str
    names: tuple[str, ...]
    user: str
    site: str
    context_id: str
    seal: str

    def __post_init__(self) -> None:
        strings = (
            self.child_doctype, self.parent_doctype, self.parent_fieldname,
            self.user, self.site, self.context_id, self.seal,
        )
        if not all(isinstance(value, str) and value for value in strings):
            raise RuntimeError("AccessibleParents requires non-empty string fields")
        if not isinstance(self.names, tuple) or not all(
            isinstance(name, str) and name for name in self.names
        ):
            raise RuntimeError("AccessibleParents requires a tuple of non-empty names")

    def __repr__(self) -> str:
        # Names are never printed: there can be tens of thousands, and logs are not the place.
        return (
            f"AccessibleParents({self.parent_doctype}.{self.parent_fieldname}, "
            f"{len(self.names)} names, sealed)"
        )

    def __reduce__(self):
        raise TypeError("AccessibleParents cannot be copied or serialized")

    def signed_fields(self) -> tuple[Any, ...]:
        return (
            self.child_doctype, self.parent_doctype, self.parent_fieldname,
            list(self.names), self.user, self.site, self.context_id,
        )

    def restrict(self, context: "_ExecutionContext", names: Sequence[str]) -> "AccessibleParents":
        """Narrow a verified grant to a subset. Names that were not granted cannot be added."""
        context.verify(self)
        requested = tuple(names)
        if not set(requested) <= set(self.names):
            raise RuntimeError("restrict() received a name that was not resolved as accessible")
        return context.issue(self.child_doctype, self.parent_doctype, self.parent_fieldname, requested)

    def chunks(self, context: "_ExecutionContext", size: int) -> Iterator["AccessibleParents"]:
        context.verify(self)
        for start in range(0, len(self.names), size):
            yield context.issue(
                self.child_doctype,
                self.parent_doctype,
                self.parent_fieldname,
                self.names[start : start + size],
            )


class _ExecutionContext:
    """One authenticated execution: a single ``aggregate_child_records`` call.

    It captures the session user and the site, gets a random id, and is closed in a
    ``finally``. Grants are issued and verified against it, so a grant cannot outlive the
    call, move to another call or user, or be used after ``frappe.set_user`` changed the
    session. Note that the id is random per call, never derived from request data.
    """

    def __init__(self, user: str, site: str) -> None:
        self.user = user
        self.site = site
        self.id = secrets.token_hex(16)
        self.active = True

    @classmethod
    def open(cls) -> "_ExecutionContext":
        user = cls._current_user()
        site = cls._current_site()
        if not user or not site:
            frappe.throw(_("An authenticated Frappe user is required"), frappe.PermissionError)
        return cls(user, site)

    def close(self) -> None:
        self.active = False

    @staticmethod
    def _current_user() -> str | None:
        user = getattr(getattr(frappe, "session", None), "user", None)
        return user if isinstance(user, str) and user else None

    @staticmethod
    def _current_site() -> str | None:
        site = getattr(frappe.local, "site", None)
        return site if isinstance(site, str) and site else None

    def _refuse(self) -> None:
        frappe.throw(
            _("The parent permission grant is not valid for this request"),
            frappe.PermissionError,
        )

    def _require_current(self) -> None:
        if (
            not self.active
            or self._current_user() != self.user
            or self._current_site() != self.site
        ):
            self._refuse()

    def issue(
        self,
        child_doctype: str,
        parent_doctype: str,
        parent_fieldname: str,
        names: Sequence[str],
    ) -> AccessibleParents:
        """Seal a grant. Called only by parent resolution and by narrowing a verified grant."""
        self._require_current()
        names = tuple(names)
        return AccessibleParents(
            child_doctype,
            parent_doctype,
            parent_fieldname,
            names,
            self.user,
            self.site,
            self.id,
            _seal(child_doctype, parent_doctype, parent_fieldname, list(names), self.user, self.site, self.id),
        )

    def verify(
        self,
        grant: Any,
        *,
        child_doctype: str | None = None,
        parent_doctype: str | None = None,
        parent_fieldname: str | None = None,
    ) -> None:
        """Refuse, with PermissionError, any grant that is not valid right now."""
        if not isinstance(grant, AccessibleParents):
            raise RuntimeError("Child queries require parents resolved by parent resolution")
        self._require_current()
        if (
            grant.user != self.user
            or grant.site != self.site
            or not hmac.compare_digest(grant.context_id, self.id)
            or not _seal_matches(grant.seal, *grant.signed_fields())
        ):
            self._refuse()
        for expected, actual in (
            (child_doctype, grant.child_doctype),
            (parent_doctype, grant.parent_doctype),
            (parent_fieldname, grant.parent_fieldname),
        ):
            if expected is not None and expected != actual:
                self._refuse()


# --------------------------------------------------------------------------- snapshot detector
class _TransactionTripwire:
    """Record, never raise on, a commit or rollback made through ``frappe.db`` during a scan.

    Frappe runs ``before_commit`` callbacks inside ``Database.commit()`` and
    ``before_rollback`` callbacks inside ``Database.rollback()``. We register one inert
    callback on each. A callback only appends to a list. It cannot raise, so it cannot
    abort or alter the commit or rollback it observes. The service calls ``check()`` at
    safe points and fails the request if anything was recorded.

    What it detects: calls to ``frappe.db.commit()`` or ``frappe.db.rollback()`` (and
    helpers that call them, such as ``frappe.db.sql_ddl`` or ``sql(auto_commit=...)``)
    that happen after ``arm()``.

    What it does NOT detect (it must not be read as a guarantee of snapshot integrity):
      * a transaction ended by raw SQL (``COMMIT``, ``ROLLBACK``, ``START TRANSACTION``,
        DDL) sent through ``frappe.db.sql`` or any other path that skips
        ``Database.commit()`` and ``Database.rollback()``;
      * a lost and re-established connection (the driver is configured to reconnect),
        which silently starts a new session and snapshot;
      * a change of isolation level, or any use of a different connection;
      * code that clears ``frappe.db.before_commit`` or ``before_rollback`` after
        ``arm()``, which silently removes the callbacks;
      * ``rollback(save_point=...)``. It does not end the transaction or its snapshot,
        so ignoring it is correct;
      * anything that happened before ``arm()``, including when the snapshot started.

    Detection is after the fact. It cannot undo a commit. It only stops this request
    from returning a ranking. The callbacks stay queued until Frappe's next commit or
    rollback (normally the end of the request) and are no-ops once ``disarm()`` ran.
    """

    def __init__(self) -> None:
        self._armed = False
        self._events: list[str] = []

    def arm(self) -> None:
        if self._armed:
            return
        self._armed = True
        frappe.db.before_commit.add(self._on_commit)
        frappe.db.before_rollback.add(self._on_rollback)

    def disarm(self) -> None:
        self._armed = False

    @property
    def tripped(self) -> bool:
        return bool(self._events)

    def check(self) -> None:
        if self._events:
            frappe.throw(
                _(
                    "{0}: A database commit or rollback happened while the ranking was being "
                    "computed, so a consistent result cannot be guaranteed. The result was "
                    "discarded. Retry the request."
                ).format(ERROR_SNAPSHOT_INTERRUPTED),
                frappe.ValidationError,
            )

    def _on_commit(self) -> None:
        if self._armed:
            self._events.append("commit")

    def _on_rollback(self) -> None:
        if self._armed:
            self._events.append("rollback")


# --------------------------------------------------------------------------- small helpers
class _Deadline:
    def __init__(self, clock: Any, seconds: int) -> None:
        self._clock = clock
        self.seconds = seconds
        self._start = clock()

    def elapsed(self) -> float:
        return self._clock() - self._start

    def exceeded(self) -> bool:
        return self.elapsed() > self.seconds


@dataclass
class _Stats:
    parent_pages: int = 0
    child_batches: int = 0
    parents_scanned: int = 0


class _ExpectedFailure(Exception):
    """Internal signal for the structured outcomes. It never leaves ``aggregate_child_records``."""

    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class _Prepared:
    child_doctype: str
    parent_doctype: str
    parent_fieldname: str
    aggregations: list[dict[str, str]]
    select_fields: list[str]
    parent_filters: list[dict[str, Any]]
    explicit_names: tuple[str, ...] | None
    ranking_field: str
    top_n: int
    include_ties: bool


class ChildAggregateService:
    """Rank parents by an aggregate of their child rows, for the current Frappe user."""

    # Replaceable in tests. Must be monotonic.
    _clock = staticmethod(time.monotonic)

    _ALLOWED_REQUEST_KEYS = frozenset(
        {
            "doctype",
            "parent_doctype",
            "parent_fieldname",
            "parent_filters",
            "parent_names",
            "aggregations",
            "group_by",
            "ranking",
        }
    )
    _REQUIRED_REQUEST_KEYS = frozenset(
        {"doctype", "parent_doctype", "parent_fieldname", "aggregations", "group_by", "ranking"}
    )
    _ALLOWED_RANKING_KEYS = frozenset({"by", "direction", "top_n", "include_ties"})
    _SELECTION_RULE = "aggregation descending, then parent ascending"

    # ------------------------------------------------------------------ entry point
    @classmethod
    def aggregate_child_records(
        cls,
        request: Mapping[str, Any],
        user: str | None = None,
        limit_snapshot: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Return a ranking, or a structured failure. Raises for anything unexpected."""
        cls._assert_authenticated_user(user)
        cls._validate_request_shape(request)
        # The child-aggregation limits are not part of the base snapshot. They are merged
        # in here, so they are resolved and validated only because a child aggregation
        # is running. A snapshot that already carries them (the request's, once the
        # orchestrator merges them) is used as it is.
        limit_snapshot = ConfigService.get_child_aggregation_limit_snapshot(limit_snapshot)
        limits = cls._get_limits(limit_snapshot)

        deadline = _Deadline(cls._clock, limits["child_agg_max_seconds"])
        prepared = cls._prepare(request, user, limit_snapshot, limits)

        stats = _Stats()
        tripwire = _TransactionTripwire()
        context = _ExecutionContext.open()
        tripwire.arm()
        try:
            try:
                accessible = cls._resolve_accessible_parents(
                    prepared, limits, deadline, tripwire, stats, context
                )
                ranking, values = cls._rank(
                    prepared, accessible, limits, deadline, tripwire, stats, context
                )
                tripwire.check()
                return cls._build_success(prepared, limits, deadline, stats, ranking, values)
            except _ExpectedFailure as failure:
                # A failure computed on an inconsistent view must not be reported as fact.
                tripwire.check()
                return cls._build_failure(failure, prepared, limits, deadline, stats)
        finally:
            tripwire.disarm()
            context.close()  # every grant issued for this call is dead from here on

    # ------------------------------------------------------------------ limits
    @classmethod
    def _get_limits(cls, limit_snapshot: Mapping[str, int]) -> dict[str, int]:
        """Resolve every limit this service needs from one request snapshot.

        ``limit_snapshot`` must already carry the child-aggregation limits. The entry
        point guarantees that through ``ConfigService.get_child_aggregation_limit_snapshot``.
        """
        return ConfigService.resolve_limits(
            *_SHARED_LIMIT_NAMES, *CHILD_AGGREGATION_LIMIT_NAMES, snapshot=limit_snapshot
        )

    # ------------------------------------------------------------------ validation (no record reads)
    @staticmethod
    def _assert_authenticated_user(user: str | None) -> str:
        session_user = getattr(getattr(frappe, "session", None), "user", None)
        if not isinstance(session_user, str) or not session_user:
            frappe.throw(_("An authenticated Frappe user is required"), frappe.PermissionError)
        if user is not None:
            if not isinstance(user, str):
                frappe.throw(_("User must be a string"), frappe.ValidationError)
            if user != session_user:
                frappe.throw(
                    _("Child aggregations can only be evaluated for the current session user"),
                    frappe.PermissionError,
                )
        return session_user

    @classmethod
    def _validate_request_shape(cls, request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping):
            frappe.throw(
                _("Child aggregation request must be a mapping"), frappe.ValidationError
            )
        unknown = set(request) - cls._ALLOWED_REQUEST_KEYS
        if unknown:
            frappe.throw(
                _("Unsupported child aggregation request keys: {0}").format(
                    ", ".join(sorted(str(key) for key in unknown))
                ),
                frappe.ValidationError,
            )
        missing = cls._REQUIRED_REQUEST_KEYS - set(request)
        if missing:
            frappe.throw(
                _("Child aggregation request requires: {0}").format(", ".join(sorted(missing))),
                frappe.ValidationError,
            )
        for key in ("doctype", "parent_doctype", "parent_fieldname"):
            value = request[key]
            if not isinstance(value, str) or not value.strip():
                frappe.throw(_("{0} must be a non-empty string").format(key), frappe.ValidationError)
        if request["group_by"] != ["parent"]:
            frappe.throw(
                _("Child aggregation must group by exactly [\"parent\"]"),
                frappe.ValidationError,
            )
        filters = request.get("parent_filters", [])
        if not isinstance(filters, list):
            frappe.throw(_("parent_filters must be a list"), frappe.ValidationError)

    @classmethod
    def _prepare(
        cls,
        request: Mapping[str, Any],
        user: str | None,
        limit_snapshot: Mapping[str, int],
        limits: Mapping[str, int],
    ) -> _Prepared:
        """Validate permissions and shape. Reads metadata and permissions, never records."""
        child_doctype = request["doctype"].strip()
        parent_doctype = request["parent_doctype"].strip()
        parent_fieldname = request["parent_fieldname"].strip()

        aggregations = AggregateService.normalize_aggregations(
            request["aggregations"], limits["aggregations"]
        )
        ranking = cls._normalize_ranking(request["ranking"], aggregations, limits)

        referenced = list(dict.fromkeys(["parent", *(a["field"] for a in aggregations)]))
        # FieldPolicy under the parent context: child read permission, the parent's
        # Table-field permission (including permlevel) and field readability. Any denial
        # raises here, before a single get_list call.
        child_plan = QueryValidator.validate_query(
            {
                "doctype": child_doctype,
                "fields": referenced,
                "filters": [],
                "sort": [],
                "page_length": 1,
                "offset": 0,
                "parent_doctype": parent_doctype,
                "parent_fieldname": parent_fieldname,
                "parent_names": request.get("parent_names"),
            },
            user=user,
            limit_snapshot=limit_snapshot,
            parent_names_required=False,
        )
        parent_plan = QueryValidator.validate_query(
            {
                "doctype": parent_doctype,
                "fields": ["name"],
                "filters": request.get("parent_filters", []),
                "sort": [],
                "page_length": 1,
                "offset": 0,
            },
            user=user,
            limit_snapshot=limit_snapshot,
        )
        if child_plan["doctype"] != child_doctype or parent_plan["doctype"] != parent_doctype:
            frappe.throw(
                _("DocType names must be canonical for child aggregation"),
                frappe.ValidationError,
            )

        child_meta = frappe.get_meta(child_doctype)
        parent_meta = frappe.get_meta(parent_doctype)
        if not bool(getattr(child_meta, "istable", False)):
            frappe.throw(_("{0} is not a child DocType").format(child_doctype), frappe.ValidationError)
        for meta, label in ((child_meta, child_doctype), (parent_meta, parent_doctype)):
            if bool(getattr(meta, "issingle", False)) or bool(getattr(meta, "is_virtual", False)):
                frappe.throw(
                    _("{0} is not supported by child aggregation").format(label),
                    frappe.ValidationError,
                )
        if bool(getattr(parent_meta, "istable", False)):
            frappe.throw(
                _("{0} cannot be a parent DocType").format(parent_doctype), frappe.ValidationError
            )

        AggregateService.validate_aggregation_types(child_meta, aggregations)
        ranking_aggregation = aggregations[ranking["index"]]
        live = MetadataService.get_live_field(child_meta, ranking_aggregation["field"])
        if ranking_aggregation["operation"] != "count" and (
            getattr(live, "fieldtype", None) not in NUMERIC_AGGREGATION_FIELD_TYPES
        ):
            frappe.throw(
                _("Ranking requires a numeric aggregation"), frappe.ValidationError
            )
        AggregateService.enforce_complexity_limit(
            aggregations, ["parent"], parent_plan["filters"], limits["aggregate_complexity"]
        )

        explicit = child_plan["parent_names"]
        return _Prepared(
            child_doctype=child_doctype,
            parent_doctype=parent_doctype,
            parent_fieldname=parent_fieldname,
            aggregations=aggregations,
            select_fields=AggregateService.build_select_fields(aggregations, ["parent"]),
            parent_filters=parent_plan["filters"],
            explicit_names=tuple(explicit) if explicit else None,
            ranking_field=ranking_aggregation["result_field"],
            top_n=ranking["top_n"],
            include_ties=ranking["include_ties"],
        )

    @classmethod
    def _normalize_ranking(
        cls,
        ranking: Any,
        aggregations: list[dict[str, str]],
        limits: Mapping[str, int],
    ) -> dict[str, Any]:
        if not isinstance(ranking, Mapping) or set(ranking) - cls._ALLOWED_RANKING_KEYS:
            frappe.throw(
                _("ranking may only contain: {0}").format(
                    ", ".join(sorted(cls._ALLOWED_RANKING_KEYS))
                ),
                frappe.ValidationError,
            )
        if ranking.get("direction", "desc") != "desc":
            frappe.throw(
                _(
                    "Ascending ranking is not supported because parents with no child rows "
                    "produce no group"
                ),
                frappe.ValidationError,
            )
        top_n = ranking.get("top_n")
        maximum = min(limits["child_agg_top_n"], limits["returned_groups"])
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= maximum:
            frappe.throw(
                _("ranking.top_n must be an integer between 1 and {0}").format(maximum),
                frappe.ValidationError,
            )
        include_ties = ranking.get("include_ties", True)
        if not isinstance(include_ties, bool):
            frappe.throw(_("ranking.include_ties must be a boolean"), frappe.ValidationError)

        by = ranking.get("by")
        if by is None:
            if len(aggregations) != 1:
                frappe.throw(
                    _("ranking.by is required when there is more than one aggregation"),
                    frappe.ValidationError,
                )
            index = 0
        else:
            if not isinstance(by, Mapping) or set(by) != {"operation", "field"}:
                frappe.throw(
                    _("ranking.by must contain exactly: field, operation"), frappe.ValidationError
                )
            operation = by["operation"].lower() if isinstance(by["operation"], str) else None
            matches = [
                i
                for i, item in enumerate(aggregations)
                if item["operation"] == operation and item["field"] == by["field"]
            ]
            if len(matches) != 1:
                frappe.throw(
                    _("ranking.by must match one of the aggregations"), frappe.ValidationError
                )
            index = matches[0]
        return {"index": index, "top_n": top_n, "include_ties": include_ties}

    # ------------------------------------------------------------------ phase A
    @classmethod
    def _resolve_accessible_parents(
        cls,
        prepared: _Prepared,
        limits: Mapping[str, int],
        deadline: _Deadline,
        tripwire: _TransactionTripwire,
        stats: _Stats,
        context: _ExecutionContext,
    ) -> AccessibleParents:
        """Return every parent name the current user may read within the requested scope.

        Native ``frappe.get_list`` on the parent DocType, keyset-paginated on ``name``.
        There is no offset anywhere. Parent filters and explicit names are AND-ed, so
        they can only narrow the scope.
        """
        parent = prepared.parent_doctype
        scan_limit = limits["child_agg_parent_scan"]
        batch = limits["child_agg_parent_batch"]

        base_filters: list[list[Any]] = [
            [parent, item["field"], item["operator"], item["value"]]
            for item in prepared.parent_filters
        ]
        if prepared.explicit_names:
            base_filters.append([parent, "name", "in", list(prepared.explicit_names)])

        names: list[str] = []
        seen: set[str] = set()
        last: str | None = None
        while True:
            cls._enforce_deadline(deadline, "resolving accessible parent records")
            remaining = scan_limit - len(names)
            requested = min(batch, remaining + 1)  # the extra row detects "over the limit"
            filters = list(base_filters)
            if last is not None:
                filters.append([parent, "name", ">", last])

            rows = frappe.get_list(
                parent,
                fields=["name"],
                filters=filters,
                order_by="`name` asc",
                limit_page_length=requested,
                as_list=False,
                strict=True,
            )
            stats.parent_pages += 1
            tripwire.check()
            page = cls._parse_parent_page(rows, requested, seen)
            cls._enforce_deadline(deadline, "resolving accessible parent records")

            if len(page) > remaining:
                raise _ExpectedFailure(ERROR_SCAN_LIMIT, found_at_least=scan_limit + 1)
            names.extend(page)
            stats.parents_scanned = len(names)
            if len(page) < requested:
                break
            last = page[-1]

        # The only place a grant is issued from query results.
        return context.issue(prepared.child_doctype, parent, prepared.parent_fieldname, names)

    @staticmethod
    def _parse_parent_page(rows: Any, requested: int, seen: set[str]) -> list[str]:
        if not isinstance(rows, list) or len(rows) > requested:
            frappe.throw(
                _("Parent resolution returned an invalid result"), frappe.ValidationError
            )
        page = []
        for row in rows:
            name = row.get("name") if isinstance(row, Mapping) else None
            if not isinstance(name, str) or not name:
                frappe.throw(
                    _("Parent resolution returned an invalid row"), frappe.ValidationError
                )
            if name in seen:
                # A repeated name means keyset pagination is not advancing.
                frappe.throw(
                    _("Parent resolution did not advance"), frappe.ValidationError
                )
            seen.add(name)
            page.append(name)
        return page

    # ------------------------------------------------------------------ phase B
    @classmethod
    def _rank(
        cls,
        prepared: _Prepared,
        accessible: AccessibleParents,
        limits: Mapping[str, int],
        deadline: _Deadline,
        tripwire: _TransactionTripwire,
        stats: _Stats,
        context: _ExecutionContext,
    ) -> tuple[Any, dict[str, dict[str, Any]]]:
        merger = RankingMerger(prepared.top_n, prepared.include_ties, limits["returned_groups"])

        for chunk in accessible.chunks(context, limits["child_agg_parent_batch"]):
            cls._enforce_deadline(deadline, "counting child rows")
            groups = cls._aggregate_batch(prepared, chunk, context)
            stats.child_batches += 1
            tripwire.check()
            cls._enforce_deadline(deadline, "counting child rows")
            for parent, values in groups.items():
                merger.add(parent, values[prepared.ranking_field])

        try:
            ranking = merger.finalize()
        except TieCapExceededError:
            raise _ExpectedFailure(ERROR_TIE_CAP) from None

        winners = [row.parent for row in ranking.rows]
        if len(prepared.aggregations) == 1 or not winners:
            return ranking, {row.parent: {prepared.ranking_field: row.value} for row in ranking.rows}

        # Several aggregations: one more query, restricted to the winners, fetches the rest.
        cls._enforce_deadline(deadline, "counting child rows")
        groups = cls._aggregate_batch(prepared, accessible.restrict(context, winners), context)
        stats.child_batches += 1
        tripwire.check()
        for row in ranking.rows:
            got = groups.get(row.parent)
            if got is None or got[prepared.ranking_field] != row.value:
                frappe.throw(
                    _("Ranking values changed between queries"), frappe.ValidationError
                )
        return ranking, groups

    @classmethod
    def _aggregate_batch(
        cls, prepared: _Prepared, batch: AccessibleParents, context: _ExecutionContext
    ) -> dict[str, dict[str, Any]]:
        """Count child rows grouped by parent for one batch of ACCESSIBLE parents.

        The grant is verified before anything else: its seal, its user, its site, its
        call, and that it is for exactly this child DocType, parent DocType and Table
        field. Any mismatch raises before a query is built.
        """
        context.verify(
            batch,
            child_doctype=prepared.child_doctype,
            parent_doctype=prepared.parent_doctype,
            parent_fieldname=prepared.parent_fieldname,
        )
        if not batch.names:
            return {}

        child = prepared.child_doctype
        requested = set(batch.names)
        rows = frappe.get_list(
            child,
            parent_doctype=batch.parent_doctype,
            fields=prepared.select_fields,
            filters=[
                [child, "parent", "in", list(batch.names)],
                [child, "parenttype", "=", batch.parent_doctype],
                [child, "parentfield", "=", batch.parent_fieldname],
            ],
            group_by="`parent`",
            order_by="`parent` asc",
            limit_page_length=len(batch.names) + 1,
            as_list=False,
            strict=True,
        )
        if not isinstance(rows, list) or len(rows) > len(batch.names):
            frappe.throw(
                _("Child aggregation returned an invalid result"), frappe.ValidationError
            )

        expected = [a["result_field"] for a in prepared.aggregations]
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping) or any(f not in row for f in ["parent", *expected]):
                frappe.throw(
                    _("Child aggregation returned an invalid row"), frappe.ValidationError
                )
            parent = row["parent"]
            if parent not in requested or parent in groups:
                # Never accept a group for a parent that was not asked for.
                frappe.throw(
                    _("Child aggregation returned an unexpected parent"), frappe.ValidationError
                )
            groups[parent] = {f: row[f] for f in expected}
        return groups

    # ------------------------------------------------------------------ outcomes
    @classmethod
    def _enforce_deadline(cls, deadline: _Deadline, stage: str) -> None:
        if deadline.exceeded():
            raise _ExpectedFailure(ERROR_TIME_BUDGET, stage=stage)

    @classmethod
    def _build_success(
        cls,
        prepared: _Prepared,
        limits: Mapping[str, int],
        deadline: _Deadline,
        stats: _Stats,
        ranking: Any,
        values: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        results = []
        for row in ranking.rows:
            entry: dict[str, Any] = {"parent": row.parent}
            for aggregation in prepared.aggregations:
                entry[aggregation["result_field"]] = values[row.parent][aggregation["result_field"]]
            entry["rank"] = row.rank
            results.append(entry)

        try:
            return build_collection_response(
                RESULT_TYPE,
                results,
                item_key="results",
                doctype=prepared.child_doctype,
                truncated=False,
                extra={
                    "status": STATUS_COMPLETED,
                    "complete": True,
                    "parent_doctype": prepared.parent_doctype,
                    "parent_fieldname": prepared.parent_fieldname,
                    "group_by": ["parent"],
                    "aggregations": prepared.aggregations,
                    "ranking": {
                        "by": prepared.ranking_field,
                        "direction": "desc",
                        "top_n": prepared.top_n,
                        "include_ties": prepared.include_ties,
                        "tied_at_boundary": ranking.tied_at_boundary,
                        "tie_count": ranking.tie_count,
                        "selection_rule": cls._SELECTION_RULE,
                    },
                    "scan": {
                        "parents_scanned": stats.parents_scanned,
                        "parents_with_child_rows": ranking.rows_offered,
                        "null_metric_parents": ranking.rows_skipped_null,
                        "parent_pages": stats.parent_pages,
                        "child_batches": stats.child_batches,
                        "scan_limit": limits["child_agg_parent_scan"],
                        "batch_size": limits["child_agg_parent_batch"],
                        "time_budget_seconds": limits["child_agg_max_seconds"],
                        "elapsed_seconds": round(deadline.elapsed(), 3),
                        "parent_filters_applied": prepared.parent_filters,
                        "explicit_parent_names": prepared.explicit_names is not None,
                        "scope": "records the current user may read",
                    },
                },
            )
        except (TypeError, ValueError):
            frappe.throw(
                _("Child aggregation returned a value that cannot be represented as JSON"),
                frappe.ValidationError,
            )

    @classmethod
    def _build_failure(
        cls,
        failure: _ExpectedFailure,
        prepared: _Prepared,
        limits: Mapping[str, int],
        deadline: _Deadline,
        stats: _Stats,
    ) -> dict[str, Any]:
        """A structured failure. It carries no ranking and never reads as complete."""
        parent = prepared.parent_doctype
        observed: dict[str, Any] = {
            "elapsed_seconds": round(deadline.elapsed(), 3),
            "parent_pages": stats.parent_pages,
            "child_batches": stats.child_batches,
        }

        if failure.code == ERROR_SCAN_LIMIT:
            limit_name, unit = "child_agg_parent_scan", "parent documents"
            value = limits[limit_name]
            observed["parents_in_scope_at_least"] = failure.details["found_at_least"]
            message = (
                f"More than {value} {parent} records are in scope for this request, so a "
                "highest-count ranking cannot be established. No ranking was computed."
            )
            guidance = [
                f"Narrow parent_scope.parent_filters (for example a date range, company or "
                f"status) so that {value} or fewer {parent} records match.",
                "Or name the records to compare with parent_scope.parent_names.",
                "Do not repeat the same request: it will fail the same way.",
            ]
        elif failure.code == ERROR_TIME_BUDGET:
            limit_name, unit = "child_agg_max_seconds", "seconds"
            value = limits[limit_name]
            observed["stage"] = failure.details["stage"]
            observed["parents_resolved_so_far"] = stats.parents_scanned
            message = (
                f"The request exceeded its {value} second time budget while "
                f"{failure.details['stage']}. No ranking was computed."
            )
            guidance = [
                f"Narrow parent_scope.parent_filters or provide parent_scope.parent_names so "
                f"that fewer {parent} records are scanned.",
                "Repeating the same request may succeed if the server was briefly busy, but a "
                "narrower request is more reliable.",
            ]
        elif failure.code == ERROR_TIE_CAP:
            limit_name, unit = "returned_groups", "result rows"
            value = limits[limit_name]
            observed["top_n"] = prepared.top_n
            message = (
                f"More than {value} {parent} records tie at or above the top {prepared.top_n} "
                "boundary, so the complete tie group cannot be returned. No ranking was returned."
            )
            guidance = [
                "Retry with ranking.include_ties set to false to receive exactly top_n rows "
                "together with a flag that shows a tie at the boundary.",
                "Or reduce ranking.top_n, or narrow parent_scope.parent_filters.",
            ]
        else:  # pragma: no cover - guarded by construction
            raise RuntimeError(f"Unknown structured failure: {failure.code}")

        definition = LIMIT_DEFINITIONS[limit_name]
        return build_response(
            FAILURE_RESULT_TYPE,
            doctype=prepared.child_doctype,
            payload={
                "status": STATUS_FAILED,
                "complete": False,
                "partial_results_returned": False,
                "error_code": failure.code,
                "message": message,
                "limit": {
                    "name": limit_name,
                    "config_key": definition.config_key,
                    "value": value,
                    "ceiling": definition.ceiling,
                    "unit": unit,
                },
                "observed": observed,
                "guidance": guidance,
                "parent_doctype": parent,
                "parent_fieldname": prepared.parent_fieldname,
            },
        )
