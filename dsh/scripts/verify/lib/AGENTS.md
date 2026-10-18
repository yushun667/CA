# AGENTS.md — 清单检查库

`manifest-lib.mjs`：`dsh/test/` 与 `verify-install.mjs` 共用的只读纯函数（读取并结构判定 package.json、patch、client 产物、locale、icon）。

## 规则

- 只依赖 Node 内置模块；不写文件、不启动进程、不访问网络。
- `dsh/test/manifest.test.mjs` 覆盖这里的判定；改判定同步测试。
