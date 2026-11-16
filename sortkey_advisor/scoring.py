"""Scoring model for candidate sort-key orders.

Every component is normalised to roughly ``[0, 1]`` so the weights in
:class:`~sortkey_advisor.advisor.AdvisorConfig` stay interpretable.

Components
----------
pruning
    File-level pruning benefit.  A predicate on the column at sort
    position ``i`` prunes with effectiveness ``decay ** i`` because
    later sort keys only cluster within groups of earlier keys.
compression
    In-block dictionary / run-length compression gain from clustering
    low-cardinality, high-null columns early.
write_cost
    Sort cost paid on every write; grows with key cardinality and the
    number of key columns.
update_amplification
    Extra rewrite work when updated columns participate in the sort
    key, because touching them invalidates file ordering.
"""

from __future__ import annotations

import math

from .model import ColumnStats, Predicate, QueryPattern, Workload

DEFAULT_RANGE_SELECTIVITY = 0.3
UNKNOWN_CARDINALITY = 10_000


def effective_cardinality(stats: ColumnStats | None) -> float:
    if stats is None or stats.cardinality is None or stats.cardinality <= 0:
        return float(UNKNOWN_CARDINALITY)
    return float(stats.cardinality)


def predicate_selectivity(pred: Predicate, stats: ColumnStats | None) -> float:
    if pred.selectivity is not None:
        return min(1.0, max(0.0, pred.selectivity))
    if pred.kind == "range":
        return DEFAULT_RANGE_SELECTIVITY
    # equality / IN: one (or a few) of ``cardinality`` distinct values.
    return min(1.0, 1.0 / effective_cardinality(stats))


def _position_decay(position: int, decay: float) -> float:
    return decay ** position


def pruning_score(
    workload: Workload, order: tuple[str, ...], decay: float
) -> float:
    """Frequency-weighted fraction of files prunable by ``order``."""
    total_freq = sum(q.frequency for q in workload.queries)
    if total_freq <= 0:
        return 0.0
    benefit = 0.0
    for query in workload.queries:
        if query.is_full_scan:
            continue
        residual = 1.0  # fraction of files *not* pruned
        for pred in query.predicates:
            if pred.column not in order:
                continue
            stats = workload.columns.get(pred.column)
            sel = predicate_selectivity(pred, stats)
            eff = _position_decay(order.index(pred.column), decay)
            residual *= 1.0 - (1.0 - sel) * eff
        for key in query.join_keys:
            if key not in order:
                continue
            stats = workload.columns.get(key)
            sel = min(1.0, 1.0 / effective_cardinality(stats))
            eff = _position_decay(order.index(key), decay)
            residual *= 1.0 - (1.0 - sel) * eff
        benefit += query.frequency * (1.0 - residual)
    return benefit / total_freq


def _compressibility(stats: ColumnStats | None) -> float:
    card = effective_cardinality(stats)
    null_ratio = stats.clamped_null_ratio() if stats else 0.0
    # Low cardinality -> long runs -> dictionary/RLE friendly.  Nulls
    # compress almost for free, so a high null ratio adds a bonus.
    card_term = 1.0 / (1.0 + math.log10(card + 1.0))
    return min(1.0, card_term + 0.3 * null_ratio)


def compression_score(
    workload: Workload, order: tuple[str, ...], decay: float
) -> float:
    """In-block compression gain from the clustering of ``order``."""
    if not order:
        return 0.0
    score = 0.0
    for position, column in enumerate(order):
        stats = workload.columns.get(column)
        score += _compressibility(stats) * _position_decay(position, decay)
    # Normalise by the best achievable score for this many positions.
    best = sum(_position_decay(i, decay) for i in range(len(order)))
    return score / best if best else 0.0


def write_cost(
    workload: Workload, order: tuple[str, ...], per_key_base: float
) -> float:
    """Relative write-path cost of maintaining ``order``."""
    if not order:
        return 0.0
    scale = math.log10(max(workload.table_rows, 10) + 1.0)
    cost = 0.0
    for column in order:
        stats = workload.columns.get(column)
        card = effective_cardinality(stats)
        # Sorting high-cardinality keys costs more comparisons and
        # produces wider, less compressible sort metadata.
        cost += per_key_base + min(1.0, math.log10(card + 1.0) / scale)
    return cost / len(order)


def update_amplification(
    workload: Workload, order: tuple[str, ...], decay: float
) -> float:
    """Extra rewrite amplification caused by updated key columns."""
    if not order or not workload.updates.column_rates:
        return 0.0
    amplification = 0.0
    for position, column in enumerate(order):
        rate = workload.updates.column_rates.get(column, 0.0)
        if rate <= 0.0:
            continue
        # Updating a sorted column forces re-sorting / rewriting the
        # affected files; earlier positions amplify more.
        amplification += rate * (1.0 + _position_decay(position, decay))
    return min(1.0, amplification)
