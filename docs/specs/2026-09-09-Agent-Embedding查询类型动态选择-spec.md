# Agent Embedding 查询类型动态选择规格

## 需求

OpenAI-compatible embedding 默认发送标准 Embeddings 请求。仅当
`agent.indexing.query_text_type` 设置为非空值时，查询请求才携带
`extra_body.text_type`。

## 约束

- 不根据模型名或 endpoint 猜测服务能力。
- 非敏感运行参数由主 YAML 配置持有，不新增面向部署的环境变量。
- 文档入库请求不携带查询类型。
- 未设置或仅包含空白时等同于禁用该扩展。

## 非本阶段范围

- 不增加失败后自动重试。
- 不修改 BGE-M3 后端。
