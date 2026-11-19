"""Bounded descending top-N ranking with explicit tie handling.

This module is pure Python. It has no Frappe dependency, performs no I/O and
knows nothing about permissions. It receives ``(parent, value)`` pairs, in any
order and in any number of batches, and keeps only what is needed to answer the
ranking, so memory does not grow with the number of rows offered.

Ordering
    Rows are ordered by value descending, then by parent ascending. The
    parent tie-break makes every result deterministic.

Contract with the caller
    * Each parent is offered at most once. Parents are never merged. This holds
      for child-table aggregation because every parent belongs to exactly one
      batch and the database groups by parent. Duplicates are not detected.
    * Values are ``int``, ``float`` or ``Decimal``. ``None`` is skipped and
      counted. ``bool``, other types and non-finite numbers are rejected.
    * Ties are decided by exact equality. For ``float`` aggregates that means
      two values tie only if they are exactly equal.

Modes
    ``include_ties=False``
        Returns exactly ``top_n`` rows (fewer if fewer parents exist). Only
        ``top_n + 1`` rows are ever retained. The extra row exists solely to
        detect a tie across the boundary, so a tie group of any size costs no
        additional memory. Among tied rows, the lowest parent names are
        returned. ``tied_at_boundary`` is True when more than ``top_n`` parents
        share the ``top_n``-th value. The size of that tie group is unknown in
        this mode and ``tie_count`` is None.

    ``include_ties=True``
        Returns every row whose value is at or above the ``top_n``-th value. The
        result is complete or it fails: if that would exceed ``result_cap`` rows,
        ``finalize`` raises ``TieCapExceededError`` and returns nothing. A
        partial tie group is never returned. At most ``result_cap + 1`` rows
        are retained. ``tied_at_boundary`` is True when the result holds more
        than ``top_n`` rows, and ``tie_count`` is the number of returned rows
        sharing the ``top_n``-th value.

Why truncating retained rows is safe in ``include_ties=True``
    The ``top_n``-th value never decreases as rows are added, so a row below it
    can never re-enter the result. Retained rows above the threshold number at
    most ``top_n - 1``, which is below ``result_cap``, so the only rows ever
    dropped by the retention limit are tied at the threshold. If that retention
    limit is still full at ``finalize`` the true result exceeds ``result_cap``
    and the request fails. If a later, higher value raises the threshold, the
    dropped rows were irrelevant and the result is still exact.

Ranks use competition ranking: tied rows share the rank of the first of them,
and the next distinct value skips ahead (1, 1, 3).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "RankedRow",
    "RankingMerger",
    "RankingResult",
    "TieCapExceededError",
]


class TieCapExceededError(ValueError):
    """The rows tied at or above the ranking boundary exceed the result cap."""

    def __init__(self, result_cap: int) -> None:
        self.result_cap = result_cap
        super().__init__(
            f"More than {result_cap} rows tie at or above the ranking boundary, "
            "so the complete tie group cannot be returned"
        )


@dataclass(frozen=True)
class RankedRow:
    parent: str
    value: Any
    rank: int


@dataclass(frozen=True)
class RankingResult:
    rows: tuple[RankedRow, ...]
    top_n: int
    include_ties: bool
    tied_at_boundary: bool
    tie_count: int | None
    rows_offered: int
    rows_skipped_null: int
    peak_retained: int


class RankingMerger:
    """Accumulate ``(parent, value)`` pairs and return the top ``top_n`` rows."""

    def __init__(self, top_n: int, include_ties: bool, result_cap: int) -> None:
        if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
            raise ValueError("top_n must be an integer of at least 1")
        if isinstance(result_cap, bool) or not isinstance(result_cap, int):
            raise ValueError("result_cap must be an integer")
        if result_cap < top_n:
            raise ValueError("result_cap cannot be smaller than top_n")
        if not isinstance(include_ties, bool):
            raise ValueError("include_ties must be a boolean")

        self._top_n = top_n
        self._include_ties = include_ties
        self._result_cap = result_cap
        # Retained entries, best first: (negated value, parent, value). Tuple
        # ordering is therefore value descending, then parent ascending.
        self._entries: list[tuple[Any, str, Any]] = []
        self._offered = 0
        self._skipped_null = 0
        self._peak = 0
        self._finalized = False

    @property
    def retained(self) -> int:
        """Number of rows currently held in memory."""
        return len(self._entries)

    @property
    def peak_retained(self) -> int:
        """Largest number of rows held after any single ``add``."""
        return self._peak

    def add_batch(self, rows: Iterable[tuple[str, Any]]) -> None:
        for parent, value in rows:
            self.add(parent, value)

    def add(self, parent: str, value: Any) -> None:
        if self._finalized:
            raise RuntimeError("RankingMerger is finalized and accepts no more rows")
        if not isinstance(parent, str) or not parent:
            raise ValueError("parent must be a non-empty string")

        self._offered += 1
        if value is None:
            self._skipped_null += 1
            return
        self._require_rankable(value)

        entry = (-value, parent, value)
        if self._include_ties:
            self._add_with_ties(entry)
        else:
            self._add_strict(entry)
        self._peak = max(self._peak, len(self._entries))

    def finalize(self) -> RankingResult:
        """Return the ranking. Raises ``TieCapExceededError`` rather than a partial tie group."""
        if self._finalized:
            raise RuntimeError("RankingMerger is already finalized")
        self._finalized = True

        entries = self._entries
        top_n = self._top_n

        if self._include_ties:
            if len(entries) > self._result_cap:
                raise TieCapExceededError(self._result_cap)
            chosen = entries
            tied_at_boundary = len(chosen) > top_n
            tie_count = None
            if len(chosen) >= top_n:
                boundary = chosen[top_n - 1][0]
                tie_count = sum(1 for entry in chosen if entry[0] == boundary)
        else:
            chosen = entries[:top_n]
            tied_at_boundary = (
                len(entries) > top_n and entries[top_n][0] == entries[top_n - 1][0]
            )
            tie_count = None

        return RankingResult(
            rows=self._rank(chosen),
            top_n=top_n,
            include_ties=self._include_ties,
            tied_at_boundary=tied_at_boundary,
            tie_count=tie_count,
            rows_offered=self._offered,
            rows_skipped_null=self._skipped_null,
            peak_retained=self._peak,
        )

    def _add_strict(self, entry: tuple[Any, str, Any]) -> None:
        """Keep the best ``top_n + 1`` rows."""
        entries = self._entries
        limit = self._top_n + 1
        if len(entries) >= limit and entry >= entries[-1]:
            return  # ranks at or below the retained boundary row
        bisect.insort(entries, entry)
        if len(entries) > limit:
            entries.pop()

    def _add_with_ties(self, entry: tuple[Any, str, Any]) -> None:
        """Keep rows at or above the ``top_n``-th value, bounded by ``result_cap + 1``."""
        entries = self._entries
        top_n = self._top_n
        if len(entries) >= top_n and entry[0] > entries[top_n - 1][0]:
            return  # strictly below the top_n-th value: can never qualify again

        bisect.insort(entries, entry)
        if len(entries) >= top_n:
            threshold = entries[top_n - 1][0]
            while entries[-1][0] > threshold:
                entries.pop()

        limit = self._result_cap + 1
        if len(entries) > limit:
            # Only rows tied at the threshold can be beyond this index: rows above
            # it number at most top_n - 1, which is below the limit.
            del entries[limit:]

    @staticmethod
    def _require_rankable(value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise TypeError("value must be an int, float or Decimal")
        finite = value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)
        if not finite:
            raise ValueError("value must be finite")

    @staticmethod
    def _rank(entries: list[tuple[Any, str, Any]]) -> tuple[RankedRow, ...]:
        rows = []
        rank = 0
        previous = None
        for index, (negated, parent, value) in enumerate(entries):
            if index == 0 or negated != previous:
                rank = index + 1
            previous = negated
            rows.append(RankedRow(parent=parent, value=value, rank=rank))
        return tuple(rows)
