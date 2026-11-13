# latency_tile_store

按终端、应用和时间分区保存延迟直方图，对同一边界的独立分区做计数汇总。

当前公开命令是 `inspect`，用于核对数据和查看已记录的观测，不对观测结果作超出数据支持的推断。输入和输出使用 UTF-8 JSON。示例数据均为本地虚构记录，不连接终端、供应方或个人账户。

`query_service` 在 `inspect` 沿用的直方图读取与校验之上，提供有字节预算的摘要写入、
带 revision 的分区替换/撤回与分位数、超阈值计数查询。它把原始调用压缩成固定对数
分箱 × 时间子桶的频数摘要持久化到状态目录，原始样本不进入保留状态；每次结果都
附参与分区、覆盖范围与误差约定，详见 `docs/data.md`。

## 安装与运行

使用 Python 3.11.2，依赖精确版本在 `requirements.txt`。依赖可预先装好；运行和测试不执行下载。

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
python3 -m compileall -q latency_tile_store
python3 -m unittest discover -s tests -v
python3 -m latency_tile_store inspect --input fixtures/histograms.json --output /tmp/latency-counts.json
python3 -m latency_tile_store.query_service \
    --input fixtures/workload.json --state /tmp/lts-state --output /tmp/lts-result.json
```

`--state` 目录可跨进程重复使用：同 revision 重放幂等，旧 revision 被忽略，同 revision
冲突内容以非零状态退出。复用前如需全新事实集，应清理状态目录。

正常运行会写出完整结果文件，标准输出给出保存位置。格式冲突、数据边界错误、revision
冲突或摘要超过字节预算会以非零状态退出；不要将失败输出当作空业务结果。复用临时目录前应清理同名输出与数据库，以免混淆不同运行。

## 数据与目录

`latency_tile_store/histogram.py` 提供当前观测计算；`latency_tile_store/summary.py`
定义有界摘要的分箱、时间子桶累加与分位数/阈值界限；`latency_tile_store/store.py`
实现 revision 状态机、预算核算与查询；`latency_tile_store/io.py` 负责严格 JSON 读取
和原子输出；`fixtures/` 是可重放的演练记录；`tests/` 验证领域边界、输入输出行为，
并用原始观测做只存在于测试进程内的暴力真值对照。

直方图 bins 为 [lower, upper, count]，区间左闭右开，count 为频数。流中 occurred_ms 是事件时间，partition_id、revision、operation 描述分区事实，操作为 upsert 或 retract。预算只计算保留的分布数据及索引文件，不含程序和只读输入。

演练规模可在普通 CPU 上完成，运行说明不要求外部服务或真实设备。输出中的未定义值使用 JSON null，不用零替代。涉及版本的输入应保留原文件，方便复查当时的事实。
