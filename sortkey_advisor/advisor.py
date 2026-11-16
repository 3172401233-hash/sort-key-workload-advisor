"""Candidate enumeration, special-case handling and recommendations."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations

from . import scoring
from .model import Workload


@dataclass
class AdvisorConfig:
    """Tunable weights and thresholds for the scoring model."""

    # Component weights (see README for the full model description).
    w_pruning: float = 1.0
    w_compression: float = 0.3
    w_write: float = 0.2
    w_update: float = 0.5

    # Position decay for pruning / compression / update amplification.
    prune_decay: float = 0.5
    compression_decay: float = 0.7
    update_decay: float = 0.8

    # Write cost floor added per sort key column.
    write_per_key_base: float = 0.1

    # Search space limits.
    max_sort_keys: int = 3
    max_candidate_columns: int = 6

    # Special-case thresholds.
    stale_stats_days: float = 30.0
    full_scan_dominance: float = 0.6
    high_update_rate: float = 0.1
    conflict_share: float = 0.2


@dataclass
class CandidateEvaluation:
    """Score breakdown for one candidate sort-key order."""

    order: tuple[str, ...]
    total: float
    pruning: float
    compression: float
    write_cost: float
    update_amplification: float


@dataclass
class Recommendation:
    """Advisor output: chosen order plus human-readable explanation."""

    order: list[str]
    evaluations: list[CandidateEvaluation]
    rationale: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: str = "high"


def _evaluate(
    workload: Workload, order: tuple[str, ...], config: AdvisorConfig
) -> CandidateEvaluation:
    pruning = scoring.pruning_score(workload, order, config.prune_decay)
    compression = scoring.compression_score(
        workload, order, config.compression_decay
    )
    write = scoring.write_cost(workload, order, config.write_per_key_base)
    update = scoring.update_amplification(workload, order, config.update_decay)
    total = (
        config.w_pruning * pruning
        + config.w_compression * compression
        - config.w_write * write
        - config.w_update * update
    )
    return CandidateEvaluation(
        order=order,
        total=total,
        pruning=pruning,
        compression=compression,
        write_cost=write,
        update_amplification=update,
    )


def _full_scan_share(workload: Workload) -> float:
    total = sum(q.frequency for q in workload.queries)
    if total <= 0:
        return 0.0
    scan = sum(q.frequency for q in workload.queries if q.is_full_scan)
    return scan / total


def _preferred_first_column(workload: Workload, query, config) -> str | None:
    """Best standalone first sort key for a single query pattern."""
    best_col, best_score = None, 0.0
    columns = [p.column for p in query.predicates] + list(query.join_keys)
    for column in dict.fromkeys(columns):
        evaluation = _evaluate(workload, (column,), config)
        if evaluation.total > best_score:
            best_col, best_score = column, evaluation.total
    return best_col


def _detect_conflicts(
    workload: Workload, config: AdvisorConfig
) -> list[tuple[str, str]]:
    """Find dominant query patterns that disagree on the first key."""
    total = sum(
        q.frequency for q in workload.queries if not q.is_full_scan
    )
    if total <= 0:
        return []
    preferences: dict[str, float] = {}
    for query in workload.queries:
        if query.is_full_scan:
            continue
        first = _preferred_first_column(workload, query, config)
        if first is not None:
            preferences[first] = preferences.get(first, 0.0) + query.frequency
    dominant = [
        (col, share / total)
        for col, share in preferences.items()
        if share / total >= config.conflict_share
    ]
    if len(dominant) > 1:
        return dominant
    return []


def _stale_columns(workload: Workload, config: AdvisorConfig) -> list[str]:
    stale = []
    for name, stats in workload.columns.items():
        if stats.cardinality is None:
            stale.append(f"{name} (cardinality missing)")
        elif (
            stats.stats_age_days is None
            or stats.stats_age_days > config.stale_stats_days
        ):
            age = "unknown" if stats.stats_age_days is None else (
                f"{stats.stats_age_days:.0f}d old"
            )
            stale.append(f"{name} (stats {age})")
    return stale


def _candidate_columns(
    workload: Workload,
    candidates: list[str],
    config: AdvisorConfig,
    warnings: list[str],
) -> list[str]:
    eligible = []
    for column in candidates:
        if column not in workload.columns:
            warnings.append(f"candidate column '{column}' has no statistics; skipped")
            continue
        rate = workload.updates.column_rates.get(column, 0.0)
        if rate >= config.high_update_rate:
            warnings.append(
                f"column '{column}' is updated in {rate:.0%} of rows per "
                f"period; excluded from sort keys to avoid update "
                f"amplification (rewriting sorted files)"
            )
            continue
        eligible.append(column)
    return eligible[: config.max_candidate_columns]


def _enumerate_orders(
    columns: list[str], max_keys: int
) -> list[tuple[str, ...]]:
    orders: list[tuple[str, ...]] = [()]
    depth = min(max_keys, len(columns))
    for size in range(1, depth + 1):
        orders.extend(permutations(columns, size))
    return orders


def advise(
    workload: Workload,
    candidates: list[str],
    config: AdvisorConfig | None = None,
) -> Recommendation:
    """Recommend a sort-key order for ``workload``.

    ``candidates`` lists the columns allowed in the sort key, in no
    particular order.  Returns a :class:`Recommendation` with the chosen
    order, the full score table, rationale and warnings.
    """
    config = config or AdvisorConfig()
    rationale: list[str] = []
    warnings: list[str] = []

    # --- statistics freshness -------------------------------------------
    stale = _stale_columns(workload, config)
    if stale:
        warnings.append(
            "stale or missing statistics for: "
            + ", ".join(stale)
            + "; run ANALYZE before trusting this recommendation"
        )

    # --- full-scan dominance --------------------------------------------
    scan_share = _full_scan_share(workload)
    if scan_share >= config.full_scan_dominance:
        rationale.append(
            f"full scans account for {scan_share:.0%} of query frequency; "
            "sorting cannot prune them and only adds write cost"
        )
        rationale.append(
            "recommend append-only layout (no sort key); revisit if "
            "selective predicates become more common"
        )
        evaluation = _evaluate(workload, (), config)
        confidence = "low" if stale else "medium"
        return Recommendation(
            order=[],
            evaluations=[evaluation],
            rationale=rationale,
            warnings=warnings,
            confidence=confidence,
        )

    # --- candidate filtering --------------------------------------------
    eligible = _candidate_columns(workload, candidates, config, warnings)
    if not eligible:
        warnings.append("no eligible sort-key columns; returning empty order")
        evaluation = _evaluate(workload, (), config)
        return Recommendation(
            order=[],
            evaluations=[evaluation],
            rationale=["no usable sort key; keep append-only layout"],
            warnings=warnings,
            confidence="low",
        )

    # --- conflicting query patterns --------------------------------------
    conflicts = _detect_conflicts(workload, config)
    if conflicts:
        detail = ", ".join(
            f"'{col}' ({share:.0%} of selective queries)"
            for col, share in conflicts
        )
        warnings.append(
            f"conflicting query patterns prefer different first keys: "
            f"{detail}; the recommendation is a compromise order"
        )

    # --- enumerate and score ---------------------------------------------
    evaluations = [
        _evaluate(workload, order, config)
        for order in _enumerate_orders(eligible, config.max_sort_keys)
    ]
    evaluations.sort(key=lambda e: e.total, reverse=True)
    best = evaluations[0]

    # --- rationale --------------------------------------------------------
    if not best.order:
        rationale.append(
            "no sort key beats append-only: pruning benefit does not "
            "cover write and update costs"
        )
    else:
        rationale.append(
            "recommended order "
            + " -> ".join(best.order)
            + f" (score {best.total:.3f})"
        )
        rationale.append(
            f"file pruning {best.pruning:.2f}, compression "
            f"{best.compression:.2f}, write cost {best.write_cost:.2f}, "
            f"update amplification {best.update_amplification:.2f}"
        )
        first = best.order[0]
        stats = workload.columns.get(first)
        card = (
            "unknown"
            if stats is None or stats.cardinality is None
            else str(stats.cardinality)
        )
        rationale.append(
            f"first key '{first}' gives the strongest file-level pruning "
            f"(cardinality {card}); later keys refine clustering within "
            "its groups"
        )
        if len(best.order) > 1:
            rationale.append(
                f"secondary key(s) {list(best.order[1:])} still help "
                "queries filtering on them, with diminishing "
                "effectiveness per position"
            )

    confidence = "high"
    if stale:
        confidence = "low"
    elif conflicts:
        confidence = "medium"

    return Recommendation(
        order=list(best.order),
        evaluations=evaluations,
        rationale=rationale,
        warnings=warnings,
        confidence=confidence,
    )
