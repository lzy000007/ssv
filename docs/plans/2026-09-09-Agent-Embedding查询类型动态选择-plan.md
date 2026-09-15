# Agent Embedding 查询类型动态选择计划

## 步骤 1：动态构造查询请求

- 在主 YAML 的 `agent.indexing` 中声明 `query_text_type`，启动时传给 embedding 后端。
- 未配置时不发送 `extra_body`，配置后发送 `text_type`。
- 更新单元检查与 Agent 配置文档。

## 验证

- 检查默认请求的 `extra_body` 为空。
- 检查运行时设置后发送指定 `text_type`。
