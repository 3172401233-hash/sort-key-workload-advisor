import unittest

from sortkey_advisor import (
    AdvisorConfig,
    ColumnStats,
    Predicate,
    QueryPattern,
    UpdatePattern,
    Workload,
    advise,
)


def make_columns(**overrides):
    columns = {
        "ts": ColumnStats("ts", cardinality=1_000_000, null_ratio=0.0,
                          stats_age_days=1),
        "customer_id": ColumnStats("customer_id", cardinality=100_000,
                                   null_ratio=0.0, stats_age_days=1),
        "status": ColumnStats("status", cardinality=5, null_ratio=0.1,
                              stats_age_days=1),
        "amount": ColumnStats("amount", cardinality=500_000, null_ratio=0.2,
                              stats_age_days=1),
    }
    columns.update(overrides)
    return columns


def time_range_query(freq):
    return QueryPattern(
        name="time_range",
        frequency=freq,
        predicates=[Predicate("ts", kind="range", selectivity=0.05)],
    )


def customer_query(freq):
    return QueryPattern(
        name="customer_lookup",
        frequency=freq,
        predicates=[Predicate("customer_id", kind="equality")],
        join_keys=["customer_id"],
    )


CANDIDATES = ["ts", "customer_id", "status", "amount"]


class TimeRangeScanTest(unittest.TestCase):
    def test_time_column_first(self):
        workload = Workload(
            table_rows=1_000_000_000,
            columns=make_columns(),
            queries=[time_range_query(100), customer_query(2)],
        )
        rec = advise(workload, CANDIDATES)
        self.assertEqual(rec.order[0], "ts")
        self.assertGreater(rec.evaluations[0].pruning, 0.5)


class CustomerQueryTest(unittest.TestCase):
    def test_customer_first_for_lookup_and_join(self):
        workload = Workload(
            table_rows=1_000_000_000,
            columns=make_columns(),
            queries=[customer_query(100), time_range_query(5)],
        )
        rec = advise(workload, CANDIDATES)
        self.assertEqual(rec.order[0], "customer_id")


class JointFilterTest(unittest.TestCase):
    def test_conflict_detected_and_composite_order(self):
        workload = Workload(
            table_rows=1_000_000_000,
            columns=make_columns(),
            queries=[time_range_query(50), customer_query(50)],
        )
        rec = advise(workload, CANDIDATES)
        self.assertIn("ts", rec.order)
        self.assertIn("customer_id", rec.order)
        self.assertTrue(
            any("conflicting" in w for w in rec.warnings),
            msg=f"expected conflict warning, got {rec.warnings}",
        )
        self.assertEqual(rec.confidence, "medium")


class LowCardinalityTest(unittest.TestCase):
    def test_low_cardinality_column_not_first_but_helps_compression(self):
        columns = make_columns()
        workload = Workload(
            table_rows=1_000_000_000,
            columns=columns,
            queries=[
                customer_query(90),
                QueryPattern(
                    name="status_filter",
                    frequency=10,
                    predicates=[Predicate("status", kind="equality")],
                ),
            ],
        )
        rec = advise(workload, CANDIDATES)
        self.assertEqual(rec.order[0], "customer_id")
        self.assertIn("status", rec.order)
        best = rec.evaluations[0]
        alone = next(
            e for e in rec.evaluations if e.order == ("customer_id",)
        )
        self.assertGreater(best.compression, alone.compression)


class MissingStatsTest(unittest.TestCase):
    def test_missing_and_stale_stats_warn_but_still_recommend(self):
        columns = make_columns(
            ts=ColumnStats("ts", cardinality=None, stats_age_days=None),
            customer_id=ColumnStats("customer_id", cardinality=100_000,
                                    stats_age_days=90),
        )
        workload = Workload(
            table_rows=1_000_000_000,
            columns=columns,
            queries=[time_range_query(60), customer_query(40)],
        )
        rec = advise(workload, CANDIDATES)
        self.assertTrue(
            any("stale or missing statistics" in w for w in rec.warnings)
        )
        self.assertTrue(any("ts" in w for w in rec.warnings))
        self.assertEqual(rec.confidence, "low")
        self.assertIsInstance(rec.order, list)


class FullScanDominanceTest(unittest.TestCase):
    def test_full_scan_dominance_recommends_no_sort_key(self):
        workload = Workload(
            table_rows=1_000_000_000,
            columns=make_columns(),
            queries=[
                QueryPattern(name="etl_scan", frequency=100,
                             is_full_scan=True),
                time_range_query(10),
            ],
        )
        rec = advise(workload, CANDIDATES)
        self.assertEqual(rec.order, [])
        self.assertTrue(
            any("append-only" in r for r in rec.rationale),
            msg=f"expected append-only rationale, got {rec.rationale}",
        )


class HighUpdateColumnTest(unittest.TestCase):
    def test_high_update_column_excluded(self):
        workload = Workload(
            table_rows=1_000_000_000,
            columns=make_columns(),
            queries=[customer_query(80), time_range_query(20)],
            updates=UpdatePattern(
                column_rates={"customer_id": 0.5},
                rows_updated_per_day=0.05,
            ),
        )
        rec = advise(workload, CANDIDATES)
        self.assertNotIn("customer_id", rec.order)
        self.assertTrue(
            any("customer_id" in w and "excluded" in w
                for w in rec.warnings),
            msg=f"expected exclusion warning, got {rec.warnings}",
        )


class UpdateAmplificationTest(unittest.TestCase):
    def test_moderate_update_rate_penalises_key_column(self):
        columns = make_columns()
        queries = [customer_query(60), time_range_query(40)]
        calm = advise(
            Workload(table_rows=1_000_000_000, columns=columns,
                     queries=queries),
            CANDIDATES,
        )
        updated = advise(
            Workload(
                table_rows=1_000_000_000,
                columns=columns,
                queries=queries,
                updates=UpdatePattern(
                    column_rates={"customer_id": 0.09},
                    rows_updated_per_day=0.02,
                ),
            ),
            CANDIDATES,
        )
        calm_eval = next(
            e for e in calm.evaluations if e.order == tuple(calm.order)
        )
        same_order = next(
            e for e in updated.evaluations
            if e.order == tuple(calm.order)
        )
        self.assertGreater(
            same_order.update_amplification,
            calm_eval.update_amplification,
        )
        self.assertLess(same_order.total, calm_eval.total)


class ConfigWeightTest(unittest.TestCase):
    def test_weights_are_respected(self):
        workload = Workload(
            table_rows=1_000_000_000,
            columns=make_columns(),
            queries=[time_range_query(50), customer_query(50)],
        )
        heavy_write = AdvisorConfig(w_write=5.0)
        rec = advise(workload, CANDIDATES, config=heavy_write)
        # With write cost dominating, fewer sort keys must win.
        self.assertLessEqual(len(rec.order), 1)


if __name__ == "__main__":
    unittest.main()
