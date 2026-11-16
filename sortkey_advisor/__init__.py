"""Data lake sort-key layout advisor.

Evaluates candidate sort-key orders for a wide columnar table against a
workload description (query predicates, join keys, update patterns) and
column statistics (cardinality, null ratio, stats freshness).

The public entry point is :func:`advise`.
"""

from .model import (
    ColumnStats,
    Predicate,
    QueryPattern,
    UpdatePattern,
    Workload,
)
from .advisor import (
    AdvisorConfig,
    CandidateEvaluation,
    Recommendation,
    advise,
)

__all__ = [
    "ColumnStats",
    "Predicate",
    "QueryPattern",
    "UpdatePattern",
    "Workload",
    "AdvisorConfig",
    "CandidateEvaluation",
    "Recommendation",
    "advise",
]

__version__ = "0.1.0"
