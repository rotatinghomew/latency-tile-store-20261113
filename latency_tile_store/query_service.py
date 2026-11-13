"""query_service 命令：重放分区操作、持久化摘要并回答查询。

用法：
    python3 -m latency_tile_store.query_service \
        --input <workload.json> --state <目录> --output <结果.json>

同一 --state 目录可被多次运行：revision 状态机保证重复重放幂等，旧
revision 被忽略，同 revision 冲突直接非零退出。原始调用样本只在构建
摘要时读取，从不写入状态目录。
"""

import argparse
import sys

from .io import read_json, write_json
from .store import BudgetExceeded, RevisionConflict, TileStore


def run(document, state_dir):
    if not isinstance(document, dict) or set(document) != {
            'bytes_per_partition', 'operations', 'queries'}:
        raise ValueError('输入必须恰好包含 bytes_per_partition、operations、queries')
    operations = document['operations']
    queries = document['queries']
    if not isinstance(operations, list) or not operations:
        raise ValueError('operations 必须是非空列表')
    if not isinstance(queries, list) or not queries:
        raise ValueError('queries 必须是非空列表')
    query_ids = [query['query_id'] for query in queries]
    if len(set(query_ids)) != len(query_ids):
        raise ValueError('query_id 重复')

    store = TileStore(state_dir, document['bytes_per_partition'])
    replay = []
    for operation in operations:
        replay.append(store.apply(operation))
    results = [store.query(query) for query in queries]
    return {
        'queries': results,
        'replay': replay,
        'active_partitions': sorted(
            pid for pid, entry in store.index['partitions'].items()
            if entry['status'] == 'active'),
        'tombstoned_partitions': sorted(
            pid for pid, entry in store.index['partitions'].items()
            if entry['status'] == 'tombstone'),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='带预算与可撤回分区的延迟摘要查询服务。')
    parser.add_argument('--input', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        document = read_json(args.input)
        result = run(document, args.state)
        write_json(args.output, result)
    except (ValueError, KeyError, TypeError, OSError, BudgetExceeded, RevisionConflict) as error:
        print('处理失败：' + str(error), file=sys.stderr)
        return 2
    print('结果已保存：' + args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
