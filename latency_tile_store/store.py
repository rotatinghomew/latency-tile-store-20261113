"""带 revision 状态机的持久化分区存储与查询。

状态目录布局（全部 UTF-8 JSON，原子替换写入）：

* schema.json   —— 全局唯一的对数分箱边缘，摘要只存频数不存边缘
* index.json    —— 分区索引：当前最高 revision、active/tombstone 状态、
                    分区摘要文件名与字节预算
* part-<id>.json —— 每个活跃分区一份摘要；撤回后文件删除，索引保留墓碑

可撤回性的取舍：不做跨分区的预聚合物化文件，所有查询都从当前有效分区
重建累加。替换/撤回只改单分区文件，天然不会让旧数据在聚合体里复活，
代价是查询需要读全部命中分区（见 docs/data.md 的取舍说明）。
"""

import hashlib
import json
import os
from pathlib import Path

from . import summary
from .io import read_json, write_json
from .summary import (
    SCHEMA_KIND,
    SCHEMA_VERSION,
    TOTAL_BINS,
    accumulate,
    build_edges,
    build_summary,
    quantile_bounds,
    threshold_count_bounds,
    validate_operation,
)

INDEX_FILE = 'index.json'
SCHEMA_FILE = 'schema.json'


class BudgetExceeded(ValueError):
    """持久化后的分区摘要超过字节预算。"""


class RevisionConflict(ValueError):
    """同一最高 revision 出现不同内容。"""


def _partition_filename(partition_id):
    safe = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in partition_id)
    return 'part-' + safe + '.json'


def canonical_payload(operation):
    """不含 partition_id/revision/operation 的业务内容指纹。

    用于判断同 revision 的重发是幂等重放还是冲突改写。
    """
    if operation['operation'] == 'retract':
        payload = None
    else:
        payload = {
            'firmware': operation['firmware'],
            'app': operation['app'],
            'start_ms': operation['start_ms'],
            'end_ms': operation['end_ms'],
            'observations': operation['observations'],
        }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


class TileStore:
    def __init__(self, state_dir, budget):
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 64:
            raise ValueError('字节预算必须是不小于 64 的整数')
        self.state_dir = Path(state_dir)
        self.budget = budget
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.schema_path = self.state_dir / SCHEMA_FILE
        self.index_path = self.state_dir / INDEX_FILE
        if self.schema_path.exists():
            stored = read_json(self.schema_path)
            if stored.get('kind') != SCHEMA_KIND or stored.get('version') != SCHEMA_VERSION:
                raise ValueError('状态目录的摘要 schema 与当前程序不兼容')
            self.edges = stored['edges']
        else:
            self.edges = build_edges()
            write_json(self.schema_path, {
                'kind': SCHEMA_KIND,
                'version': SCHEMA_VERSION,
                'edge_low': summary.EDGE_LOW,
                'edge_high': summary.EDGE_HIGH,
                'value_bins': summary.VALUE_BINS,
                'time_buckets': summary.TIME_BUCKETS,
                'bin_convention': '[lower, upper) 左闭右开，上下溢出桶分别收纳区间外取值',
                'edges': self.edges,
            })
        if self.index_path.exists():
            self.index = read_json(self.index_path)
            if self.index.get('index_version') != 1:
                raise ValueError('索引版本不兼容')
        else:
            self.index = {'index_version': 1, 'budget_bytes': budget, 'partitions': {}}
        if self.index['budget_bytes'] != budget:
            raise ValueError('字节预算在同一状态目录内不可变更')

    # ---- 状态机 -------------------------------------------------------

    def apply(self, operation):
        validate_operation(operation)
        pid = operation['partition_id']
        revision = operation['revision']
        kind = operation['operation']
        entry = self.index['partitions'].get(pid)
        fingerprint = canonical_payload(operation)

        if entry is not None and revision == entry['revision']:
            if fingerprint != entry['content_hash']:
                raise RevisionConflict(
                    '分区 ' + pid + ' 的 revision ' + str(revision) + ' 出现冲突内容')
            return {'partition_id': pid, 'revision': revision, 'effect': 'duplicate_replay'}
        if entry is not None and revision < entry['revision']:
            # 旧 revision 一律忽略：upsert 不能复活已撤回/已替换的分区，
            # 旧 retract 也不能撤销后来的新事实。
            return {'partition_id': pid, 'revision': revision, 'effect': 'stale_revision_ignored'}

        target = self.state_dir / _partition_filename(pid)
        if kind == 'retract':
            if target.exists():
                target.unlink()
            self.index['partitions'][pid] = {
                'revision': revision,
                'status': 'tombstone',
                'content_hash': fingerprint,
                'file': None,
            }
            effect = 'retracted'
        else:
            document = build_summary(operation, self.edges)
            write_json(target, document)
            size = target.stat().st_size
            if size > self.budget:
                target.unlink()
                raise BudgetExceeded(
                    '分区 ' + pid + ' 摘要序列化后 ' + str(size)
                    + ' 字节，超过预算 ' + str(self.budget))
            self.index['partitions'][pid] = {
                'revision': revision,
                'status': 'active',
                'content_hash': fingerprint,
                'file': target.name,
                'bytes': size,
                'firmware': document['firmware'],
                'app': document['app'],
                'window_ms': [document['start_ms'], document['end_ms']],
                'total_count': document['total_count'],
            }
            effect = 'replaced' if entry is not None else 'inserted'
        write_json(self.index_path, self.index)
        return {'partition_id': pid, 'revision': revision, 'effect': effect}

    # ---- 查询 ---------------------------------------------------------

    def _active_summaries(self):
        documents = []
        for entry in self.index['partitions'].values():
            if entry['status'] != 'active':
                continue
            documents.append(read_json(self.state_dir / entry['file']))
        # 业务身份排序，保证输出与重放顺序无关。
        documents.sort(key=lambda doc: (doc['start_ms'], doc['partition_id']))
        return documents

    def query(self, query):
        required = {'query_id', 'start_ms', 'end_ms', 'quantiles', 'thresholds'}
        missing = required - set(query)
        if missing:
            raise ValueError('查询缺少字段：' + ','.join(sorted(missing)))
        qid = query['query_id']
        if not isinstance(qid, str) or not qid:
            raise ValueError('query_id 必须是非空字符串')
        start = query['start_ms']
        end = query['end_ms']
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) \
                or not isinstance(end, int) or start < 0 or end <= start:
            raise ValueError('查询时间窗必须是非负且 start < end 的整数区间')
        if not isinstance(query['quantiles'], list) or not query['quantiles']:
            raise ValueError('quantiles 必须是非空列表')
        if not isinstance(query['thresholds'], list) or not query['thresholds']:
            raise ValueError('thresholds 必须是非空列表')
        firmware = query.get('firmware')
        app = query.get('app')
        if firmware is not None and (not isinstance(firmware, str) or not firmware):
            raise ValueError('firmware 过滤值必须是非空字符串')
        if app is not None and (not isinstance(app, str) or not app):
            raise ValueError('app 过滤值必须是非空字符串')

        candidates = []
        for document in self._active_summaries():
            if firmware is not None and document['firmware'] != firmware:
                continue
            if app is not None and document['app'] != app:
                continue
            if document['end_ms'] <= start or document['start_ms'] >= end:
                continue
            candidates.append(document)

        lower_counts, upper_counts, rows = accumulate(candidates, start, end)
        n_lower = sum(lower_counts)
        n_upper = sum(upper_counts)
        quantiles = [quantile_bounds(lower_counts, upper_counts, float(q), self.edges)
                     for q in query['quantiles']]
        threshold_counts = [
            threshold_count_bounds(lower_counts, upper_counts, float(t), self.edges)
            for t in query['thresholds']
        ]
        coverage = {
            'requested_window_ms': [start, end],
            'window_convention': '[start, end) 左闭右开；边缘子桶只计入上界',
            'active_partitions': [
                {'partition_id': row['partition_id'], 'revision': row['revision']}
                for row in rows
            ],
            'partition_detail': rows,
            'sample_count': {
                'lower_bound': n_lower,
                'upper_bound': n_upper,
                'exact': n_lower == n_upper,
            },
            'observed_event_range_ms': [
                min((row['retained_bucket_event_range_ms'][0] for row in rows
                     if row['retained_bucket_event_range_ms'][0] is not None), default=None),
                max((row['retained_bucket_event_range_ms'][1] for row in rows
                     if row['retained_bucket_event_range_ms'][1] is not None), default=None),
            ],
            'partially_covered': any(row['relation'] == 'partial' for row in rows),
        }
        return {
            'query_id': qid,
            'quantiles': quantiles,
            'threshold_counts': threshold_counts,
            'coverage': coverage,
            'error_contract': {
                'summary': SCHEMA_KIND + ' v' + str(SCHEMA_VERSION),
                'rank_method': 'nearest_rank: k = ceil(quantile * N)，1 基秩',
                'quantile_semantics': '对数分箱导致的值域界限；target_rank 给出不确定秩区间，'
                                       'rank_uncertainty 区分分箱内秩与时间边缘秩，不与阈值计数互换',
                'threshold_semantics': '严格大于阈值的整数计数上下界；exact 为真才可当精确值',
                'bin_convention': '[lower, upper) 左闭右开；上溢出桶只有有限下界，分位数上界返回 null',
                'partial_window': '查询边缘压住的时间子桶整体只进入上界；信息不足时返回界限或 no_data，不声称精确',
                'merge_semantics': '同构固定分箱按频数相加；替换/撤回后从当前有效分区重新累加，摘要不支持直接相减',
                'budget_bytes': self.budget,
                'retained_partition_bytes': {
                    entry['file']: entry['bytes']
                    for entry in self.index['partitions'].values()
                    if entry['status'] == 'active'
                },
            },
        }
