"""Workload and statistics data model.

All ratios are expressed as floats in ``[0.0, 1.0]``.  Frequencies are
relative weights (e.g. queries per day); only their ratios matter.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ColumnStats:
    """Statistics for a single column.

    ``cardinality`` is the estimated distinct-value count; ``None`` means
    the statistic is missing.  ``stats_age_days`` is the age of the
    statistics snapshot; ``None`` means unknown (treated as stale).
    """

    name: str
    cardinality: int | None = None
    null_ratio: float = 0.0
    stats_age_days: float | None = None
    avg_value_bytes: float = 8.0

    def clamped_null_ratio(self) -> float:
        return min(1.0, max(0.0, self.null_ratio))


@dataclass
class Predicate:
    """A predicate applied by a query pattern.

    ``kind`` is one of ``"equality"``, ``"range"`` or ``"in"``.
    ``selectivity`` is the fraction of rows matched; when ``None`` it is
    derived from the column cardinality (equality / IN) or a default
    range selectivity.
    """

    column: str
    kind: str = "equality"
    selectivity: float | None = None


@dataclass
class QueryPattern:
    """A recurring query shape and its relative frequency."""

    name: str
    frequency: float
    predicates: list[Predicate] = field(default_factory=list)
    join_keys: list[str] = field(default_factory=list)
    is_full_scan: bool = False


@dataclass
class UpdatePattern:
    """Update behaviour of the table.

    ``column_rates`` maps a column name to the fraction of table rows in
    which that column is updated per period.  ``rows_updated_per_day`` is
    the overall fraction of rows touched per day.
    """

    column_rates: dict[str, float] = field(default_factory=dict)
    rows_updated_per_day: float = 0.0


@dataclass
class Workload:
    """Full workload description consumed by the advisor."""

    table_rows: int
    columns: dict[str, ColumnStats]
    queries: list[QueryPattern] = field(default_factory=list)
    updates: UpdatePattern = field(default_factory=UpdatePattern)
    file_count: int = 1000
