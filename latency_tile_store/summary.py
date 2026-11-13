"""有界延迟摘要：对数延迟直方图 × 分区内时间子桶。

设计口径（与 docs/data.md 一致，区间一律左闭右开）：

* 延迟维度使用全仓库统一的固定对数分箱 SCHEMA，分箱边缘只在 schema.json
  存一次，分区状态里只保存频数，保证同构摘要才能相加。
* 时间维度把每个分区窗口切成固定数量的子桶。查询窗口切到子桶中间时，
  完整落入的子桶给出确定计数，被边缘压住的子桶只能给出 [0, 整桶] 的
  不确定贡献，最终以上下界表达，绝不把整天/整分区数据无条件纳入。
* 本模块只产出确定性的分箱/边界误差，不做抽样，因此误差可以给出严格
  界限而不是置信区间。
"""

import bisect
import math

SCHEMA_KIND = 'log_histogram'
SCHEMA_VERSION = 1

# 延迟分箱：1ms 到 10000ms 之间 40 个对数 bin，外加下溢出/上溢出桶。
# 公比 10^0.1 ≈ 1.2589，因此 100ms、1000ms 恰好是分箱边缘。
VALUE_BINS = 40
EDGE_LOW = 1.0
EDGE_HIGH = 10000.0

# 每个分区内部的时间子桶数量；fixture 的分区长度都能被 10 整除。
TIME_BUCKETS = 10

UNDERFLOW = 0
OVERFLOW = VALUE_BINS + 1
TOTAL_BINS = VALUE_BINS + 2


def build_edges():
    # 按十年段锚定构造：1、10、100、1000、10000 这五条边缘必须是精确
    # 浮点，阈值落在这些边缘时上下界才能收紧为精确计数；段内 9 条内边缘
    # 取 10^(1/10) 等比，整轴仍保持严格单调。
    ratio = 10.0 ** 0.1
    edges = []
    for decade in range(4):
        base = 10.0 ** decade
        for inner in range(10):
            edges.append(base * ratio ** inner)
    edges.append(10000.0)
    return edges


def value_bin_index(value, edges):
    if value < edges[0]:
        return UNDERFLOW
    if value >= edges[-1]:
        return OVERFLOW
    return bisect.bisect_right(edges, value)


def bin_bounds(bin_index, edges):
    """返回该 bin 的 [lower, upper)；上溢出桶没有有限上界，返回 None。"""
    if bin_index == UNDERFLOW:
        return 0.0, edges[0]
    if bin_index == OVERFLOW:
        return edges[-1], None
    return edges[bin_index - 1], edges[bin_index]


def _finite_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(field + '必须是数值')
    if not math.isfinite(value):
        raise ValueError(field + '必须有限')
    return value


def validate_operation(operation):
    if not isinstance(operation, dict):
        raise ValueError('操作必须是对象')
    common = {'partition_id', 'revision', 'operation'}
    upsert_fields = common | {'firmware', 'app', 'start_ms', 'end_ms', 'observations'}
    if not common <= set(operation):
        raise ValueError('操作缺少公共字段 partition_id/revision/operation')
    pid = operation['partition_id']
    if not isinstance(pid, str) or not pid or not all(
            char.isalnum() or char in '_.-' for char in pid):
        raise ValueError('partition_id 非法')
    revision = operation['revision']
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError('revision 必须是正整数')
    kind = operation['operation']
    if kind not in ('upsert', 'retract'):
        raise ValueError('operation 只能是 upsert 或 retract')
    if kind == 'retract':
        if set(operation) != common:
            raise ValueError('retract 只能携带公共字段')
        return
    if set(operation) != upsert_fields:
        raise ValueError('upsert 字段缺失或存在未知字段')
    for field in ('firmware', 'app'):
        if not isinstance(operation[field], str) or not operation[field]:
            raise ValueError(field + ' 必须是非空字符串')
    start = operation['start_ms']
    end = operation['end_ms']
    if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) \
            or not isinstance(end, int) or start < 0 or end <= start:
        raise ValueError('分区时间窗必须是非负且 start < end 的整数区间')
    observations = operation['observations']
    if not isinstance(observations, list) or not observations:
        raise ValueError('observations 必须是非空列表')
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) != {'occurred_ms', 'latency_ms'}:
            raise ValueError('观测字段必须恰好是 occurred_ms 与 latency_ms')
        occurred = observation['occurred_ms']
        if isinstance(occurred, bool) or not isinstance(occurred, int) \
                or occurred < start or occurred >= end:
            raise ValueError('occurred_ms 超出分区时间窗')
        latency = _finite_number(observation['latency_ms'], 'latency_ms')
        if latency < 0:
            raise ValueError('latency_ms 不能为负')


def build_summary(operation, edges):
    """把一次 upsert 的原始观测压缩成持久摘要（原始样本不进入返回结构）。"""
    validate_operation(operation)
    if operation['operation'] != 'upsert':
        raise ValueError('只有 upsert 能构建摘要')
    start = operation['start_ms']
    end = operation['end_ms']
    span = end - start
    width = span // TIME_BUCKETS
    if width <= 0:
        raise ValueError('分区窗口不足以划分时间子桶')

    counts = [0] * (TIME_BUCKETS * TOTAL_BINS)
    event_min = [None] * TIME_BUCKETS
    event_max = [None] * TIME_BUCKETS
    total = 0
    for observation in operation['observations']:
        occurred = observation['occurred_ms']
        latency = observation['latency_ms']
        offset = occurred - start
        bucket = min(offset // width, TIME_BUCKETS - 1)
        # 最后一个子桶吸收整除余量，右端点严格等于分区 end。
        row = bucket * TOTAL_BINS
        counts[row + value_bin_index(latency, edges)] += 1
        if event_min[bucket] is None or occurred < event_min[bucket]:
            event_min[bucket] = occurred
        if event_max[bucket] is None or occurred > event_max[bucket]:
            event_max[bucket] = occurred
        total += 1
    return {
        'schema_kind': SCHEMA_KIND,
        'schema_version': SCHEMA_VERSION,
        'partition_id': operation['partition_id'],
        'revision': operation['revision'],
        'firmware': operation['firmware'],
        'app': operation['app'],
        'start_ms': start,
        'end_ms': end,
        'time_bucket_count': TIME_BUCKETS,
        'time_bucket_width_ms': width,
        'value_bin_count': TOTAL_BINS,
        'order': 'bucket_major:counts[bucket * value_bin_count + bin]',
        'counts': counts,
        'bucket_event_min_ms': event_min,
        'bucket_event_max_ms': event_max,
        'total_count': total,
    }


def bucket_span(summary, bucket):
    start = summary['start_ms']
    width = summary['time_bucket_width_ms']
    lower = start + bucket * width
    if bucket == TIME_BUCKETS - 1:
        upper = summary['end_ms']
    else:
        upper = lower + width
    return lower, upper


def accumulate(summaries, query_start, query_end):
    """把命中查询窗口的摘要按完整/边缘子桶累加。

    返回 (lower_counts, upper_counts, coverage_rows)：
    lower 只含完整落入窗口的子桶，upper 再加上被边缘压住的子桶。
    真实分布逐桶满足 lower <= truth <= upper。
    """
    lower_counts = [0] * TOTAL_BINS
    upper_counts = [0] * TOTAL_BINS
    rows = []
    for summary in summaries:
        if summary['end_ms'] <= query_start or summary['start_ms'] >= query_end:
            continue
        full_buckets = []
        partial_buckets = []
        observed_min = None
        observed_max = None
        for bucket in range(TIME_BUCKETS):
            bucket_lower, bucket_upper = bucket_span(summary, bucket)
            if bucket_upper <= query_start or bucket_lower >= query_end:
                continue
            row = bucket * TOTAL_BINS
            if query_start <= bucket_lower and bucket_upper <= query_end:
                full_buckets.append(bucket)
                for bin_index in range(TOTAL_BINS):
                    lower_counts[bin_index] += summary['counts'][row + bin_index]
            else:
                partial_buckets.append(bucket)
            # 上界始终累加完整 + 边缘子桶。
            for bin_index in range(TOTAL_BINS):
                upper_counts[bin_index] += summary['counts'][row + bin_index]
            if summary['bucket_event_min_ms'][bucket] is not None:
                lo_event = summary['bucket_event_min_ms'][bucket]
                hi_event = summary['bucket_event_max_ms'][bucket]
                observed_min = lo_event if observed_min is None else min(observed_min, lo_event)
                observed_max = hi_event if observed_max is None else max(observed_max, hi_event)
        if full_buckets or partial_buckets:
            rows.append({
                'partition_id': summary['partition_id'],
                'revision': summary['revision'],
                'firmware': summary['firmware'],
                'app': summary['app'],
                'window_ms': [summary['start_ms'], summary['end_ms']],
                'relation': 'full' if not partial_buckets else 'partial',
                'full_time_buckets': full_buckets,
                'partial_time_buckets': partial_buckets,
                'retained_bucket_event_range_ms': [observed_min, observed_max],
            })
    return lower_counts, upper_counts, rows


def _locate_bin(counts, rank):
    """rank 为 1 基秩，返回包含该秩的 bin 下标；rank 超出总数时返回 None。"""
    if rank <= 0:
        return None
    cumulative = 0
    for bin_index, count in enumerate(counts):
        cumulative += count
        if cumulative >= rank:
            return bin_index
    return None


def quantile_bounds(lower_counts, upper_counts, quantile, edges):
    """最近秩约定下的分位数严格上下界。

    真实样本 Y 是超集直方图 X（upper）的子集，|Y| 满足
    N_lo <= N* <= N_hi，目标秩 k*=ceil(q N*) 落在 [k_lo, k_hi]，
    被边缘子桶排除的样本数 d = N_hi - N_lo。嵌套样本的阶统计量不等式：

        X_(k) <= Y_(k) <= X_(k + d)

    即删掉 d 个样本最多把同秩阶统计量抬高 d 个秩。因此下界在超集直方图
    上定位第 k_lo 阶的 bin 下边缘，上界定位第 min(k_hi + d, N_hi) 阶的
    bin 上边缘。上界秩落到上溢出桶时没有有限上界，upper 返回 null，
    绝不伪造精确值。
    """
    if not 0.0 < quantile < 1.0:
        raise ValueError('分位点必须在 (0, 1) 开区间内')
    n_lower = sum(lower_counts)
    n_upper = sum(upper_counts)
    if n_upper == 0:
        return {'quantile': quantile, 'status': 'no_data', 'estimate': None,
                'lower': None, 'upper': None, 'target_rank': {'lower': None, 'upper': None},
                'rank_uncertainty': {'bin_count': None, 'window_boundary_count': None},
                'value_bin': {'lower': None, 'upper': None}, 'basis': None}
    excluded = n_upper - n_lower
    k_upper = math.ceil(quantile * n_upper)
    k_lower = math.ceil(quantile * n_lower) if n_lower > 0 else 1
    upper_bound_rank = min(k_upper + excluded, n_upper)
    lower_bin = _locate_bin(upper_counts, k_lower)
    upper_bin = _locate_bin(upper_counts, upper_bound_rank)
    value_lower, _ = bin_bounds(lower_bin, edges)
    upper_low, upper_high = bin_bounds(upper_bin, edges)
    if upper_bin == OVERFLOW:
        estimate = upper_low
        basis = 'overflow_lower_edge_conservative'
    elif upper_bin == UNDERFLOW:
        estimate = upper_low / 2.0
        basis = 'underflow_arithmetic_midpoint'
    else:
        estimate = math.sqrt(upper_low * upper_high)
        basis = 'log_bin_geometric_mean'
    return {
        'quantile': quantile,
        'status': 'bounded',
        'estimate': round(estimate, 6),
        'lower': value_lower,
        'upper': upper_high,
        'target_rank': {'lower': k_lower, 'upper': k_upper},
        'rank_uncertainty': {
            'bin_count': upper_counts[upper_bin],
            'window_boundary_count': excluded,
            'upper_bound_rank': upper_bound_rank,
        },
        'value_bin': {'lower': upper_low, 'upper': upper_high},
        'basis': basis,
    }


def threshold_count_bounds(lower_counts, upper_counts, threshold, edges):
    """超过阈值（latency > threshold）的计数整数上下界。

    下界只统计整条 bin 都在阈值之上的确定部分；上界把任何可能含有
    超阈值样本的 bin 都计入。lower == upper 时方可标记 exact。
    """
    _finite_number(threshold, 'threshold')
    lower = 0
    upper = 0
    for bin_index in range(TOTAL_BINS):
        bin_lower, bin_upper = bin_bounds(bin_index, edges)
        # bin 下边缘 >= 阈值 => 桶内取值（左闭右开）全部严格大于阈值，
        # 阈值恰好落在分箱边缘时下界即可精确。
        if bin_lower >= threshold:
            lower += lower_counts[bin_index]
        if bin_upper is None or bin_upper > threshold:
            upper += upper_counts[bin_index]
    return {
        'threshold_ms': threshold,
        'semantics': 'latency_gt_threshold',
        'lower': lower,
        'upper': upper,
        'exact': lower == upper,
    }
