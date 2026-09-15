# Docker Qdrant 部署计划

## 步骤 1：启用服务

- 在现有 Compose 中启用 Qdrant 服务、端口和持久卷。
- 将示例和本机 YAML 的 Qdrant URL 指向 Docker 服务。

## 步骤 2：更新操作说明

- 说明现有服务启动命令会同时启动 Redis 和 Qdrant。
- 删除 `.env.example` 中已迁入 YAML 的旧配置入口。

## 验证

- 检查 Compose 展开配置和 YAML 语法。
- 启动服务并检查容器状态。
