# AGENTS.md — build_kernel

Linux Kbuild `.cmd` 重建与 include/define 保留的夹具树。

## 直属子目录

- [arch/AGENTS.md](arch/AGENTS.md)：读取该子目录夹具的职责与约束时使用。
- [drivers/AGENTS.md](drivers/AGENTS.md)：读取该子目录夹具的职责与约束时使用。
- [generated/AGENTS.md](generated/AGENTS.md)：读取该子目录夹具的职责与约束时使用。
- [kernel/AGENTS.md](kernel/AGENTS.md)：读取该子目录夹具的职责与约束时使用。
- [net/AGENTS.md](net/AGENTS.md)：读取该子目录夹具的职责与约束时使用。

## 局部规则

- 夹具只供对应测试使用；修改夹具必须同步相关测试与期望。
- 保持 KB 级、可离线重复运行；不放构建产物或生成的大文件。
- 父级索引见 [../AGENTS.md](../AGENTS.md)。
