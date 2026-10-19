# AGENTS.md — GitHub 自动化

CI 门禁与流水线定义；不包含分析逻辑。

## 关键文件

- [workflows/AGENTS.md](workflows/AGENTS.md)：CI 工作流目录索引与本地复现规则。
- `workflows/binaries.yml`：Windows、Linux、macOS 原生二进制与安装器构建及安装后核验。
- `workflows/ci.yml`：Python 3.11；安装 `requirements.txt` 固定依赖 + `pip install -e .`（console script `ca`）；运行全量 `pytest`；精度门禁 `benchmarks/run_accuracy.py --fail-under 0.9`（macro F1 阈值）；上传精度 JSON。语料（`.tmp/`）不进 Git，CI 中 xv6/FreeRTOS 端到端测试自动 skip。

## 局部规则

- CI 必须可从公开依赖离线复现失败原因；不引入需要 secret 的步骤。
- 门禁阈值变更需同步 `README*`、`docs/usage*` 与 `.agents/decisions/`。
- 修改本文件或 `workflows/` 后，在本地复现对应命令（pytest、accuracy）再报告完成。
