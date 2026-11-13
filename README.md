# latency_tile_store

按终端、应用和时间分区保存延迟直方图，对同一边界的独立分区做计数汇总。

当前公开命令是 `inspect`，用于核对数据和查看已记录的观测，不对观测结果作超出数据支持的推断。输入和输出使用 UTF-8 JSON。示例数据均为本地虚构记录，不连接终端、供应方或个人账户。

## 安装与运行

使用 Python 3.11.2，依赖精确版本在 `requirements.txt`。依赖可预先装好；运行和测试不执行下载。

```bash
python3 -m pip install -r requirements.txt
python3 -m compileall -q latency_tile_store
python3 -m unittest discover -s tests -v
python3 -m latency_tile_store inspect --input fixtures/histograms.json --output /tmp/latency-counts.json
```

正常运行会写出完整结果文件，标准输出给出保存位置。格式冲突、数据边界错误或缺失关键字段会以非零状态退出；不要将失败输出当作空业务结果。复用临时目录前应清理同名输出与数据库，以免混淆不同运行。

## 数据与目录

`latency_tile_store/histogram.py` 提供当前观测计算；`latency_tile_store/io.py` 负责严格 JSON 读取和原子输出；`fixtures/` 是可重放的演练记录；`tests/` 验证领域边界和输入输出行为。

直方图 bins 为 [lower, upper, count]，区间左闭右开，count 为频数。流中 occurred_ms 是事件时间，partition_id、revision、operation 描述分区事实，操作为 upsert 或 retract。预算只计算保留的分布数据及索引文件，不含程序和只读输入。

演练规模可在普通 CPU 上完成，运行说明不要求外部服务或真实设备。输出中的未定义值使用 JSON null，不用零替代。涉及版本的输入应保留原文件，方便复查当时的事实。
