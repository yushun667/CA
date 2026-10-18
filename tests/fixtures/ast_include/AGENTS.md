# AGENTS.md — ast_include

parse 的 include/头文件解析夹具（相对 -I、头文件内定义）。

## 局部规则

- 夹具只供对应测试使用；修改夹具必须同步相关测试与期望。
- 保持 KB 级、可离线重复运行；不放构建产物或生成的大文件。
- 父级索引见 [../AGENTS.md](../AGENTS.md)。
