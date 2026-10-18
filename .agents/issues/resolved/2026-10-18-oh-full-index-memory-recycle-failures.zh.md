# 问题：OpenHarmony 全量索引 OOM、进程池损坏与回收边界卡死

Status: resolved
Opened: 2026-10-18
Resolved: 2026-10-18

## 症状与影响

在 62GB 服务器上索引 214k 条 OpenHarmony 编译数据库时，三次验收失败：
1. `2 shard × jobs10`：内存从 38GB 涨到 54GB、free 归零，SSH 失联约 9 分钟，
   `BrokenProcessPool` 使 S2 的 17,571 TU 全失败，S1/S3 无提交。
2. `2 shard × jobs4`：worker RSS 涨到 4–10GB，S4 的失败率从 17% 升到 29%，
   available 降到 5GB。
3. 加入 `ProcessPoolExecutor(max_tasks_per_child=16)` 后，S3 在 1521/11039 TU
   卡死：无 worker 存活、父进程 sleeping futex、load 0.12、内存正常；400 TU
   短测未跨过回收边界，无法暴露。

## 根因

- libclang AST 内存随 worker 处理的 TU 累积，长期存活的 worker 最终达数 GB。
- 长生命进程池把一次 OOM 放大成整池失败；pipeline 起初没有 broken-pool 重试与
  细粒度 checkpoint。
- CPython 3.11 的 `max_tasks_per_child` 在真实负载下与 call-queue feeder 在首个
  回收边界死锁，是运行时缺陷，不只是内存问题。

## 修复

- 每 chunk ≤16 TU；broken pool 以减半 workers 重试，再退化为逐 TU 串行；只返回
  成功 chunk 结果，稳定 id 去重保证幂等。
- 弃用 `max_tasks_per_child`，改为 wave 级重建池（`CA_POOL_RECYCLE_CHUNKS=2`）：
  每 worker 最多处理 2 个 chunk 后重建，fresh worker 释放 libclang AST。
- 只接受完整 shard DB（`.done + exit0 + stats`）；partial 归档并保留 EVIDENCE；
  单 shard 串行或 2×jobs6，配合 MemAvailable/load 守门（20GB/36）、回收 env 与
  `MALLOC_ARENA_MAX=2`。

## 证据与验证

- 归档：`index/failed_20261018/`、`index/failed_20261018_hang/`（DB、日志、
  progress、EVIDENCE.md）。
- 6 个“无记录”S4 TU 用 lean libclang15 单独重跑均成功（0.03–1.18s/TU），证明是
  运行时内存失败而非解析缺口。
- 400 TU 稳定性：jobs6 153s、min_avail 54GB、无 broken；1600 TU 长程 gate：
  540s、2.96 TU/s、min_avail 49GB、peak worker 2.0GB、broken=0、无卡死。
- 最终验收 profile：2 shard × jobs6、wave 回收、guard 20GB/36。

## 经验

- 长期存活的 parser worker 必须有生命周期上限；400 TU 样本不能覆盖回收/OOM 边界。
- “parsed” 计数与进程存活不能证明进度：必须看每 shard done + stats，并监控 5 分钟
  停滞。
- shard DB 独立保存可避免整次验收被一次失败污染；partial 归档不复用。
