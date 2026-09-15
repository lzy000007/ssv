# Agent 运行配置统一规格

## 需求

Agent 的非敏感运行参数统一由主 YAML 配置持有，包括 SQLite、结果输出、embedding
服务地址和 Qdrant 连接位置。环境变量只保存 API key 等密钥，或作为进程内部工具兼容桥。

## 约束

- 相对路径继续相对于 Agent 项目目录解析。
- 本地 Qdrant 路径与远程 URL 二选一；配置 URL 时优先连接远程服务。
- `SSV_QDRANT_API_KEY` 继续通过环境变量提供，不写入 YAML。
- DeerFlow 工具继续通过既有进程环境读取配置，不扩大接口改动。

## 非本阶段范围

- 不重写 DeerFlow 工具配置协议。
- 不迁移或删除已有 SQLite、Qdrant 和结果文件。
