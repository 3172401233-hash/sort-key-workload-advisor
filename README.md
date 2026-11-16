# sort-key-workload-advisor

Python 数据湖布局建议库：针对同一张宽表上的时间范围扫描、维度 join
和批量更新等冲突负载，评估候选排序键顺序，输出推荐排序键与选择理由。

## 快速开始

```python
from sortkey_advisor import (
    ColumnStats, Predicate, QueryPattern, UpdatePattern, Workload, advise,
)

workload = Workload(
    table_rows=1_000_000_000,
    columns={
        "ts": ColumnStats("ts", cardinality=1_000_000, stats_age_days=1),
        "customer_id": ColumnStats("customer_id", cardinality=100_000,
                                   stats_age_days=1),
    },
    queries=[
        QueryPattern("time_range", frequency=50,
                     predicates=[Predicate("ts", kind="range",
                                           selectivity=0.05)]),
        QueryPattern("customer_lookup", frequency=50,
                     predicates=[Predicate("customer_id")],
                     join_keys=["customer_id"]),
    ],
    updates=UpdatePattern(column_rates={"customer_id": 0.02}),
)

rec = advise(workload, candidates=["ts", "customer_id"])
print(rec.order)       # 推荐排序键顺序
print(rec.rationale)   # 选择理由
print(rec.warnings)    # 统计过期 / 负载冲突 / 高更新列等警告
```

运行测试：

```bash
python -m unittest discover -s tests
```

## 评分模型

每个候选排序键顺序按四个分量打分，全部归一化到约 `[0, 1]`：

```
total = w_pruning * pruning
      + w_compression * compression
      - w_write * write_cost
      - w_update * update_amplification
```

- **文件级裁剪 (pruning)**：谓词命中排序位置 `i` 的列时，裁剪有效性为
  `prune_decay ** i`（越靠后的键只在前面键的分组内聚簇）。等值谓词的选择率
  默认为 `1 / cardinality`，范围谓词默认 `0.3`，均可显式覆盖。按查询频率
  加权求和；join key 按等值谓词处理。
- **块内字典压缩 (compression)**：低基数、高 null 比例的列聚簇后产生长
  重复段，利于字典 / RLE 编码。单列可压缩性为
  `1 / (1 + log10(cardinality + 1)) + 0.3 * null_ratio`，按位置衰减
  （`compression_decay ** i`）。
- **写入成本 (write_cost)**：每个排序键的基础成本
  `write_per_key_base` 加上随基数增长的排序成本
  `log10(cardinality + 1) / log10(table_rows + 1)`。
- **更新放大 (update_amplification)**：被更新的列若参与排序键，更新会
  破坏文件有序性、触发文件重写，贡献为
  `update_rate * (1 + update_decay ** position)`，位置越靠前放大越严重。

## 默认权重与阈值

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `w_pruning` | 1.0 | 文件裁剪收益权重（主导项） |
| `w_compression` | 0.3 | 块内压缩收益权重 |
| `w_write` | 0.2 | 写入成本惩罚权重 |
| `w_update` | 0.5 | 更新放大惩罚权重 |
| `prune_decay` | 0.5 | 裁剪有效性的位置衰减 |
| `compression_decay` | 0.7 | 压缩收益的位置衰减 |
| `update_decay` | 0.8 | 更新放大的位置衰减 |
| `write_per_key_base` | 0.1 | 每个排序键的基础写成本 |
| `max_sort_keys` | 3 | 排序键最大长度 |
| `stale_stats_days` | 30 | 统计信息过期阈值 |
| `full_scan_dominance` | 0.6 | 全表扫描主导阈值 |
| `high_update_rate` | 0.1 | 高更新列阈值（每期更新行占比） |
| `conflict_share` | 0.2 | 判定负载冲突的最小查询占比 |

所有参数均可通过 `AdvisorConfig` 覆盖后传入 `advise(..., config=...)`。

## 特殊场景策略

- **统计过期 / 缺失**：列基数缺失或统计快照超过
  `stale_stats_days` 时发出警告，置信度降为 `low`，建议先跑
  `ANALYZE` 再采纳推荐。
- **查询模式冲突**：当两个及以上占比 ≥ `conflict_share` 的查询模式
  各自偏好不同的首列（如时间范围 vs 客户等值）时发出冲突警告，输出
  复合排序键作为折中，置信度降为 `medium`。
- **高更新列**：更新率 ≥ `high_update_rate` 的候选列直接从排序键中
  排除并说明原因，避免排序文件被频繁重写。
- **全表扫描主导**：全表扫描频率占比 ≥ `full_scan_dominance` 时推荐
  不排序（append-only），因为排序只增加写成本而无法裁剪扫描。

## 项目结构

- `sortkey_advisor/model.py` — 负载与统计数据模型
- `sortkey_advisor/scoring.py` — 四个评分分量的实现
- `sortkey_advisor/advisor.py` — 候选枚举、特殊场景处理、推荐理由生成
- `tests/test_advisor.py` — 时间扫描、客户查询、联合过滤、低基数列、
  统计缺失、全表扫描主导、高更新列等场景测试
