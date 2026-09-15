# Docker Qdrant 部署规格

## 需求

本地开发环境默认通过 Docker Compose 运行独立 Qdrant 服务，Agent 使用 HTTP 连接，
向量数据由 Docker named volume 持久化。

## 约束

- Qdrant 默认监听宿主机 `6333` 端口，本地端口从 `agent.knowledge.qdrant_url` 派生。
- Agent 主配置默认连接 `http://localhost:6333`。
- Qdrant 数据写入 `qdrant-data` volume，容器重建不删除数据。
- `SSV_QDRANT_API_KEY` 仅用于连接启用认证的外部 Qdrant，不写入 YAML。
- 保留代码层本地嵌入模式，供测试或无 Docker 环境显式使用。

## 非本阶段范围

- 不为本地开发 Qdrant 启用认证或 TLS。
- 不配置集群、副本和远程备份。
