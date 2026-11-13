import json
import math
import tempfile
import unittest
from pathlib import Path

from latency_tile_store.io import read_json
from latency_tile_store.store import RevisionConflict, TileStore
from latency_tile_store.summary import (
    accumulate,
    build_edges,
    build_summary,
    quantile_bounds,
    threshold_count_bounds,
    validate_operation,
)

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'workload.json'


def load_workload():
    return read_json(FIXTURE)


def effective_observations(workload):
    """按 revision 状态机在内存里复算当前有效分区的原始观测（仅测试参照）。"""
    heads = {}
    for operation in workload['operations']:
        pid = operation['partition_id']
        revision = operation['revision']
        current = heads.get(pid)
        if current is not None and revision <= current['revision']:
            continue
        heads[pid] = operation
    result = []
    for operation in heads.values():
        if operation['operation'] == 'upsert':
            result.append(operation)
    return result


def exact_quantile(values, quantile):
    ordered = sorted(values)
    rank = math.ceil(quantile * len(ordered))
    return ordered[rank - 1], rank


class FixtureBoundTests(unittest.TestCase):
    """摘要界限必须夹住基于原始观测的暴力真值（明细不进入持久状态）。"""

    def setUp(self):
        self.workload = load_workload()
        self.edges = build_edges()

    def _store_with_all_operations(self, directory):
        store = TileStore(directory, self.workload['bytes_per_partition'])
        for operation in self.workload['operations']:
            store.apply(operation)
        return store

    def test_bounds_contain_truth_for_both_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_all_operations(directory)
            active = effective_observations(self.workload)
            for query in self.workload['queries']:
                answer = store.query(query)
                firmware = query.get('firmware')
                raw = []
                for operation in active:
                    if firmware is not None and operation['firmware'] != firmware:
                        continue
                    if 'app' in query and operation['app'] != query['app']:
                        continue
                    raw.extend(
                        observation['latency_ms']
                        for observation in operation['observations']
                        if query['start_ms'] <= observation['occurred_ms'] < query['end_ms'])
                self.assertTrue(raw)
                for entry, quantile in zip(answer['quantiles'], query['quantiles']):
                    truth, rank = exact_quantile(raw, quantile)
                    # lower 为 null 表示边缘信息不足以证明有限下界。
                    if entry['lower'] is not None:
                        self.assertTrue(entry['lower'] <= truth)
                    if entry['upper'] is not None:
                        self.assertTrue(truth <= entry['upper'],
                                        (query['query_id'], quantile, truth, entry))
                    self.assertLessEqual(entry['target_rank']['lower'], rank)
                    self.assertLessEqual(rank, entry['target_rank']['upper'])
                for entry, threshold in zip(answer['threshold_counts'], query['thresholds']):
                    truth_count = sum(1 for value in raw if value > threshold)
                    self.assertTrue(entry['lower'] <= truth_count <= entry['upper'])
                    self.assertTrue(entry['lower'] <= entry['upper'])

    def test_full_window_threshold_exactness_against_raw_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_all_operations(directory)
            answer = store.query(self.workload['queries'][0])
            active = effective_observations(self.workload)
            raw = [observation['latency_ms'] for operation in active
                   for observation in operation['observations']]
            for entry, threshold in zip(answer['threshold_counts'],
                                        self.workload['queries'][0]['thresholds']):
                truth_count = sum(1 for value in raw if value > threshold)
                # 阈值恰好落在 bin 边缘且窗口完整：界限应收紧为精确值。
                self.assertTrue(entry['exact'])
                self.assertEqual(entry['lower'], truth_count)
                self.assertEqual(entry['upper'], truth_count)

    def test_edge_window_does_not_swallow_whole_partitions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_all_operations(directory)
            answer = store.query(self.workload['queries'][1])
            coverage = answer['coverage']
            self.assertTrue(coverage['partially_covered'])
            self.assertEqual([row['partition_id'] for row in coverage['partition_detail']], ['p1'])
            detail = coverage['partition_detail'][0]
            self.assertEqual(detail['partial_time_buckets'], [0])
            self.assertEqual(detail['full_time_buckets'], list(range(1, 10)))
            self.assertFalse(coverage['sample_count']['exact'])
            # 暴力真值必须落在报告的样本量界限内。
            raw = [observation['latency_ms'] for operation in effective_observations(self.workload)
                   if operation['firmware'] == 'v2'
                   for observation in operation['observations']
                   if 10500 <= observation['occurred_ms'] < 30700]
            self.assertTrue(
                coverage['sample_count']['lower_bound'] <= len(raw)
                <= coverage['sample_count']['upper_bound'])

    def test_budget_is_observed_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_all_operations(directory)
            budget = self.workload['bytes_per_partition']
            for entry in store.index['partitions'].values():
                if entry['status'] == 'active':
                    self.assertLessEqual(entry['bytes'], budget)
                    self.assertLessEqual(
                        (Path(directory) / entry['file']).stat().st_size, budget)

    def test_revision_conflict_and_stale_resend_with_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TileStore(directory, self.workload['bytes_per_partition'])
            for operation in self.workload['operations']:
                store.apply(operation)
            # 模拟进程重启：重新打开同一状态目录，整份输入再放一遍。
            restarted = TileStore(directory, self.workload['bytes_per_partition'])
            effects = [restarted.apply(operation)['effect']
                       for operation in self.workload['operations']]
            # 同 revision 的幂等重放原样识别；p1 rev1、p3 rev1 相对当前
            # 更高 head 一律是旧 revision，重启后也不能降级或复活分区。
            expected = ['duplicate_replay'] * len(effects)
            expected[1] = 'stale_revision_ignored'    # p1 rev1 不能覆盖 rev2
            expected[3] = 'stale_revision_ignored'    # p3 rev1 不能覆盖 rev2 墓碑
            expected[12] = 'stale_revision_ignored'   # 撤回后的旧重发同样被忽略
            self.assertEqual(effects, expected)
            self.assertEqual(restarted.index['partitions']['p1']['revision'], 2)
            self.assertEqual(restarted.index['partitions']['p3']['status'], 'tombstone')
            # 同 revision 不同内容必须报错。
            conflicting = dict(self.workload['operations'][0])
            conflicting['observations'] = conflicting['observations'] + [
                {'occurred_ms': 1, 'latency_ms': 123.0}]
            with self.assertRaises(RevisionConflict):
                restarted.apply(conflicting)
            # 旧 revision 的重发不能复活被撤回的 p3。
            stale = self.workload['operations'][-1]
            self.assertEqual(stale['partition_id'], 'p3')
            self.assertEqual(stale['revision'], 1)
            effect = restarted.apply(stale)['effect']
            self.assertEqual(effect, 'stale_revision_ignored')
            self.assertEqual(restarted.index['partitions']['p3']['status'], 'tombstone')

    def test_persisted_state_holds_no_raw_samples_or_distinct_values(self):
        with tempfile.TemporaryDirectory() as directory:
            self._store_with_all_operations(directory)
            forbidden_values = {'146.061', '1884.73', '490.671'}
            for path in Path(directory).iterdir():
                if path.name == 'schema.json':
                    continue  # schema 只存固定分箱边缘，不含任何样本取值
                document = read_json(path)
                text = path.read_text(encoding='utf-8')
                self.assertNotIn('observations', text)
                # 原始延迟取值不能作为 JSON 数值出现（哈希串巧合包含数字子串不算）。
                def walk(node):
                    if isinstance(node, dict):
                        for value in node.values():
                            yield from walk(value)
                    elif isinstance(node, list):
                        for value in node:
                            yield from walk(value)
                    else:
                        yield node
                values = {str(value) for value in walk(document)
                          if isinstance(value, (int, float))}
                self.assertTrue(forbidden_values.isdisjoint(values))


class SmallSampleDetailTests(unittest.TestCase):
    """小样本明细对照：重复值、重尾与不完整窗口，仅作测试参照不落盘。"""

    def _operation(self, partition_id, values, start=0, end=10000):
        span = end - start
        observations = [
            {'occurred_ms': start + (index * span // (len(values) + 1)),
             'latency_ms': value}
            for index, value in enumerate(values)]
        return {
            'partition_id': partition_id,
            'revision': 1,
            'operation': 'upsert',
            'firmware': 'v1',
            'app': 'travel',
            'start_ms': start,
            'end_ms': end,
            'observations': observations,
        }

    def test_duplicate_mass_keeps_rank_in_low_bins(self):
        values = [40] * 85 + [120] * 10 + [2000] * 5
        operation = self._operation('s0', values)
        validate_operation(operation)
        summary = build_summary(operation, build_edges())
        self.assertEqual(summary['total_count'], 100)
        lower, upper, rows = accumulate([summary], 0, 10000)
        self.assertEqual(lower, upper)
        answer = quantile_bounds(lower, upper, 0.95, build_edges())
        truth, rank = exact_quantile(values, 0.95)
        self.assertTrue(answer['lower'] <= truth <= answer['upper'])
        self.assertEqual(answer['target_rank'], {'lower': 95, 'upper': 95})

    def test_heavy_tail_lands_in_overflow_with_open_upper_bound(self):
        values = [40] * 90 + [12000] * 10
        summary = build_summary(self._operation('s1', values), build_edges())
        lower, upper, _ = accumulate([summary], 0, 10000)
        answer = quantile_bounds(lower, upper, 0.99, build_edges())
        truth, _ = exact_quantile(values, 0.99)
        self.assertIsNone(answer['upper'])          # 上溢出桶不给有限上界
        self.assertTrue(answer['lower'] <= truth)
        self.assertEqual(answer['basis'], 'overflow_lower_edge_conservative')
        threshold = threshold_count_bounds(lower, upper, 10000.0, build_edges())
        # 10000 是溢出桶下边缘：桶内取值全部严格大于 10000，计数精确。
        self.assertEqual(threshold['lower'], 10)
        self.assertEqual(threshold['upper'], 10)
        self.assertTrue(threshold['exact'])

    def test_partial_window_only_moves_upper_bound(self):
        values = [50 + index for index in range(100)]
        operation = self._operation('s2', values)
        summary = build_summary(operation, build_edges())
        # 只完整覆盖到第一个子桶之后，边缘子桶 [0,1000) 被压住。
        lower, upper, rows = accumulate([summary], 500, 10000)
        self.assertNotEqual(lower, upper)
        self.assertEqual(rows[0]['relation'], 'partial')
        span = 10000
        event_times = [index * span // (len(values) + 1) for index in range(len(values))]
        raw_inside = [value for index, value in enumerate(values)
                      if 500 <= event_times[index] < 10000]
        for quantile in (0.5, 0.95):
            answer = quantile_bounds(lower, upper, quantile, build_edges())
            truth, rank = exact_quantile(raw_inside, quantile)
            if answer['lower'] is not None:
                self.assertTrue(answer['lower'] <= truth)
            self.assertTrue(truth <= answer['upper'])
            self.assertLessEqual(answer['target_rank']['lower'], rank)
            self.assertLessEqual(rank, answer['target_rank']['upper'])

    def test_empty_window_reports_no_data(self):
        summary = build_summary(self._operation('s3', [1, 2, 3]), build_edges())
        lower, upper, rows = accumulate([summary], 50000, 60000)
        self.assertEqual(rows, [])
        self.assertEqual(sum(lower), 0)
        self.assertEqual(sum(upper), 0)
        answer = quantile_bounds(lower, upper, 0.99, build_edges())
        self.assertEqual(answer['status'], 'no_data')
        self.assertIsNone(answer['estimate'])

    def test_histogram_count_conservation(self):
        values = [40] * 50 + [300] * 50
        summary = build_summary(self._operation('s4', values), build_edges())
        self.assertEqual(sum(summary['counts']), 100)
        self.assertEqual(summary['total_count'], 100)


if __name__ == '__main__':
    unittest.main()
