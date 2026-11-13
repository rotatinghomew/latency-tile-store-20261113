import math


def validate_bins(bins):
    previous_upper = None
    for lower, upper, count in bins:
        if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
            raise ValueError('直方图边界无效')
        if previous_upper is not None and lower < previous_upper:
            raise ValueError('直方图区间重叠')
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError('频数必须是非负整数')
        previous_upper = upper
    return bins


def merge_histograms(histograms):
    if not histograms:
        return []
    for histogram in histograms:
        validate_bins(histogram)
    edges = [(lower, upper) for lower, upper, count in histograms[0]]
    if any([(lower, upper) for lower, upper, count in histogram] != edges for histogram in histograms):
        raise ValueError('直方图边界不一致')
    return [[lower, upper, sum(histogram[index][2] for histogram in histograms)]
            for index, (lower, upper) in enumerate(edges)]


def threshold_bounds(bins, threshold):
    validate_bins(bins)
    if not math.isfinite(threshold):
        raise ValueError('阈值必须有限')
    lower_count = sum(count for lower, upper, count in bins if lower > threshold)
    upper_count = sum(count for lower, upper, count in bins if upper > threshold)
    return {'lower': lower_count, 'upper': upper_count}


def inspect(document):
    names = [tile['tile_id'] for tile in document['tiles']]
    if len(set(names)) != len(names):
        raise ValueError('分区编号重复')
    merged = merge_histograms([tile['bins'] for tile in document['tiles']])
    return {'count': sum(count for lower, upper, count in merged), 'bins': merged,
            'threshold': threshold_bounds(merged, document['threshold']), 'tiles': sorted(names)}
