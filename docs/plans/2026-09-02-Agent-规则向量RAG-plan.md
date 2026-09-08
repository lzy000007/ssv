# Agent 规则向量 RAG 实施计划

## 步骤 1：配置和路径收口

- 在 `AgentConfig` 增加规则知识配置：后端、Qdrant 路径和最低相似度。
- 让服务和独立入库命令读取同一份 Agent 配置，并把相对路径解析到 Agent 项目目录。
- 规则 RAG 复用 `agent.indexing` 的 embedding backend/model；mock 只保留给测试替身。
- 同步更新 C++ runner 对共享 `agent.knowledge` 配置的未知字段和取值校验，并补充配置回归测试。

## 步骤 2：可靠索引构建

- 保持规则切分和 chunk ID 稳定，按最多 20 条文本调用 embedding，并校验返回数量、维度
  和顺序。
- 新增规则 collection 的当前投影替换操作：新向量生成成功后写入当前集合，并删除不在
  当前文档集合中的旧 point；空目录也能清除旧规则，缺失目录则返回明确错误。
- 入库失败不清理旧索引；入库成功后 collection 只保留当前规则 chunk。

## 步骤 3：可靠查询和工具边界

- collection 不存在或为空时返回 `success=false` 及可操作错误信息。
- 对 Qdrant 命中应用可配置最低相似度，低于阈值返回无知识依据的成功结果，不输出随机条款。
- 修复同步工具在已有事件循环中调用 `asyncio.run` 的错误，并保持 DeerFlow 同步/异步调用兼容。
- 统一 endpoint 参与 embedding identity 和 provider 缓存键，防止同名模型跨服务混用。

## 步骤 4：测试和文档

- 增加临时 Qdrant 的真实入库、检索、source 过滤、重建删除、空索引、阈值和模型身份隔离测试。
- 保留 21 条文本拆分为 `20 + 1` 的批量测试，并增加配置、路径和工具事件循环测试。
- 运行 Agent 全量 pytest、Ruff、mypy，并用独立命令验证入库和查询链路。
