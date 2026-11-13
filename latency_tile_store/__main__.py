import argparse
import sys
from .io import read_json, write_json
from .histogram import inspect


def main():
    parser = argparse.ArgumentParser(description='按终端、应用和时间分区保存延迟直方图，对同一边界的独立分区做计数汇总。')
    parser.add_argument('command', choices=['inspect'])
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)

    args = parser.parse_args()
    try:
        document = read_json(args.input)
        result = inspect(document)
        write_json(args.output, result)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print('处理失败：' + str(error), file=sys.stderr)
        return 2
    print('结果已保存：' + args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
