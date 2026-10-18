# AGENTS.md — CI 工作流

GitHub Actions 工作流定义目录；只放流水线 YAML，不包含分析逻辑。

## 关键文件

- `ci.yml`：Python 3.11 上安装固定依赖与可编辑包，运行全量 pytest 和精度门禁（macro F1 ≥ 0.9），并上传精度 JSON artifact。

## 局部规则

- 门禁命令必须可在本地以相同参数复现（pytest / `benchmarks/run_accuracy.py --fail-under 0.9`）。
- 不添加需要 secret 或大语料下载的步骤；真实语料在 CI 中自动 skip。
- 改工作流后同步 [../AGENTS.md](../AGENTS.md) 与门禁相关文档。
