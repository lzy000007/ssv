# Agent 规则向量 RAG 规格

## 需求

为 `rule_retriever` 增加可靠的 Qdrant 向量检索后端。规则文档仍位于
`agent/knowledge/`，通过显式入库命令切分、embedding 后写入本地或远程 Qdrant；
查询时返回原文、来源、条款编号和相似度。默认后端继续使用本地词法检索，只有显式
启用 Qdrant 且完成规则入库后才使用向量检索。

## 约束

- 默认 `local_markdown` 行为不变。
- 向量后端由 `SSV_KNOWLEDGE_BACKEND=qdrant` 显式启用。
- `config/ssv.yaml` 同时由 Python Agent 和 C++ runner 解析；`agent.knowledge` 必须在两端采用相同字段、类型和取值约束，未知字段必须 fail closed。
- `agent.knowledge` 负责规则后端、Qdrant 路径和最低相似度；embedding backend/model
  复用 `agent.indexing` 的配置，入库与查询只能使用同一组配置。
- 规则 collection 与 embedding 身份绑定，不混用不同模型或 endpoint 的向量。
- `mock` embedding 仅用于单元测试；Qdrant 规则检索和正式入库拒绝使用 mock，避免把
  哈希向量误当成语义向量。
- 外部 embedding API 的单请求文本数不得超过 20；规则入库需按原始条款顺序分批并合并结果。
- 入库幂等；文档删除、内容变更或条款数量变化后，当前规则 collection 只能保留本次
  入库得到的 chunk，旧 chunk 必须删除。
- embedding 失败时不修改现有索引；索引未创建、为空或查询结果低于阈值时必须显式
  返回失败或无依据状态，不能伪装成正常命中。
- 默认 Qdrant 路径解析为 Agent 项目下的稳定路径；命令和服务不得因当前工作目录不同
  而使用两套索引。
- 返回结构保持 `RetrievalResult` / `Chunk` 契约。

## 非本阶段范围

- 不改变事件向量索引与 `search_events`。
- 不自动下载 BGE-M3 模型；模型路径由现有 embedding 配置提供。
- 不实现在线上传规章、重排序模型或多租户知识库。
