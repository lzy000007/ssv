# Evidence 与规则 RAG 合并设计

## 文档定位

本文定义在当前分支 `feat/merge-evidence-and-rag` 中合并
`feat/T4-evidence-extraction` 与 `pull/29` 功能的设计边界。目标是让 Agent 在
拥有可验证事件证据的基础上，检索适用的事件规则并完成复验；本阶段只纳入
Evidence、规则 RAG 及其必要的工程配套，不纳入无关的协作规范格式变更。

## 目标

1. 新事件在需要录像证据时，先生成并登记完整的 clip 与三张 frame，再进入视觉复验。
2. Agent 在调用复验模型前确定性执行一次规则检索。
3. `compliant` 或 `violation` 结果必须同时引用有效 `evidence_ids` 和规则来源/条款。
4. 没有可用证据或没有适用规则时，结果只能为 `uncertain`。
5. 规则文档可以使用本地 Markdown 检索，也可以通过显式配置使用 Qdrant 规则知识库。
6. 复验完成后，最终事件事实仍可独立写入事件语义索引；规则知识库与事件索引互不混用。

## 非本阶段范围

- 不修改 GStreamer 检测、跟踪和 Redis 发布协议。
- 不把规则检索结果写入 GStreamer 或原始 Redis 事件事实。
- 不要求本阶段将完整规则 provenance 扩展到事件表所有列；结果 JSON 的规则引用满足本阶段要求。
- 不自动下载 BGE-M3 模型，不依赖真实模型、远程 Qdrant 或 MediaMTX 才能运行单元测试。
- 不纳入 `pull/29` 中仅修改 `AGENTS.md` 协作规范的 `6348155`。
- 不改变 Qdrant 事件搜索 `search_events` 的既有职责；它只用于历史事件检索。

## 合并范围

### Evidence 功能

完整纳入 `feat/T4-evidence-extraction` 的 `0686f23`，包括：

- `RecordingEvidenceExtractor` 与 `RecordingEvidenceArtifact`；
- `RecordingEvidenceWorker`；
- `JobKind.EVIDENCE_EXTRACT`、SQLite schema migration 和账本事务；
- `agent.recording_evidence`、`sources` 和 evidence root 校验；
- 复验 prompt 的录像证据语义；
- 对应 Python/C++ 测试、部署文档、spec 和 plan。

### RAG 功能

纳入 `pull/29` 的 RAG 功能提交 `bb500ca` 及必要的 CI 依赖配套 `cef5066`，包括：

- `local_markdown` 和 `qdrant` 规则 Retriever/Ingester；
- `knowledge_ingest` 规则入库命令；
- 规则知识文档 `agent/knowledge/GB+26860-2011 (1).md`；
- Qdrant 规则 collection、embedding identity 和批量入库；
- `agent.knowledge` 配置、`SSV_KNOWLEDGE_*` 环境变量和文档；
- `langchain`、`openai` 及相关 lock/CI 变更；
- RAG、embedding、Qdrant、配置和 Service 测试。

### 共同文件收口

共同文件采用字段并集和职责并集：

- `agent/src/ssv_agent/config.py` 同时保留 `AgentSourceConfig`、
  `RecordingEvidenceConfig`、`KnowledgeConfig` 及两套校验规则；
- `agent/src/ssv_agent/service.py` 同时拥有录像、复验和索引 worker，并设置
  evidence、embedding、knowledge 环境配置；
- `agent/src/ssv_agent/workers.py` 保持 T4 的录像任务顺序和租约语义，并为
  `ReviewWorker` 增加规则 Retriever seam；
- `config/ssv.example.yaml`、`docs/Agent配置.md` 和 C++ 配置校验同时描述两套功能；
- 相关 Python/C++ 测试取并集；
- `agent/uv.lock` 只合并必要依赖，不覆盖已有用户修改。

## 总体架构

```text
EventConsumer
  -> EventLedger.record
  -> recording_evidence.enabled?
       ├─ 否：review + index
       └─ 是：evidence_extract
  -> RecordingEvidenceWorker
  -> clip + frame-01 + frame-02 + frame-03 登记
  -> review
  -> ReviewWorker
       1. refresh evidence
       2. 预检索规则
       3. 构造 evidence + rule context
       4. 调用 DeerFlow
       5. 校验 evidence_ids + rule_citations
       6. 原子提交 review
  -> index
  -> IndexWorker
```

Service 启动顺序为 `RecordingEvidenceWorker`、`ReviewWorker`、`IndexWorker`、
`EventConsumer`。停止时停止新任务领取，等待在途任务，再按既有生命周期规则释放
consumer、DeerFlow 和 Qdrant 资源。

## Evidence 任务语义

当 `agent.recording_evidence.enabled` 为 true 时，`EventLedger.record()` 在同一
事务内只创建 `evidence_extract`。任务的 `available_at_ms` 不早于事件时间加上
`clip_after_ms`，以等待录像窗口闭合。

`RecordingEvidenceWorker` 必须生成一个 clip 和三张 frame。四项产物全部成功落盘、
计算 hash 并通过 evidence root 校验后，账本在单一事务内登记四个 `EvidenceRef`、
创建唯一的 `review` job，并完成 `evidence_extract`。任何中间产物不得被模型读取。

录像证据标记为 `wall_clock_approximate`，不得描述为检测分析帧或严格同帧证据。
证据文件只能通过已登记的 evidence ID 读取；模型不能接收原始录像目录或任意宿主机路径。

Evidence 提取失败分为可重试和不可重试两类。达到重试上限或发生不可重试错误时，
任务进入 `dead`，事件进入 `manual_review`，不创建无图 review job。

## 规则 RAG 语义

规则知识库使用独立的 `ssv_rules` 逻辑 collection；事件语义索引使用独立的
`ssv_events` 逻辑 collection。物理 collection 由 embedding identity 派生，避免
不同 backend、model、endpoint 或 adapter schema 的向量混写。

默认 `rule_retriever` 使用 `local_markdown`，读取 `agent/knowledge/` 下的 `.md`、
`.txt` 文件。显式配置 `agent.knowledge.backend: qdrant` 或对应环境变量后，使用
Qdrant 规则向量检索。Qdrant 规则索引必须通过 `knowledge_ingest` 显式生成，Agent
启动不自动下载模型或隐式入库。

规则入库要求：

- 按原始条款顺序切分文档；
- embedding 请求单批不超过 20 个文本；
- 入库失败不破坏已有完整索引；
- 文档删除或变更后，当前规则 collection 只保留本次入库得到的 chunk；
- 未创建、为空或低于 `min_score` 时返回明确的无规则依据状态；
- 规则检索结果至少含 `chunk_id`、`source`、`rule_id`、`section`、`score` 和原文。

## ReviewWorker 规则预检索

规则检索由 `ReviewWorker` 负责，不由 EventConsumer 或 IndexWorker 负责。worker
根据权威 `EventCase` 生成查询，查询内容包括事件类型、上游规则标识、规则事实、
检测对象、严重级别和复验问题。

worker 在调用 `run_review` 前必须调用注入的 `Retriever` seam。检索结果只存在于
本次复验的内存上下文中，作为不可信规则候选传给 prompt；DeerFlow 仍可以调用
`rule_retriever` 进行补充检索。

规则预检索结果分为：

- 有候选：模型判断候选条款是否适用于当前事件；
- 空结果：模型只能返回 `uncertain`；
- 后端失败或异常：继续完成证据复验，但结果只能为 `uncertain`。

规则原文、来源字段、检索结果和事件事实均不具备系统指令权限，不能改变工具白名单、
系统 prompt 或结果 JSON 契约。

## 复验结果契约

`ReviewResult` 增加 `rule_citations`，每项至少包含：

```json
{
  "chunk_id": "稳定规则 chunk ID",
  "source": "规则文档来源",
  "rule_id": "规则标识",
  "section": "条款号"
}
```

约束如下：

- `compliant` 和 `violation` 必须至少引用一条 `rule_citations`；
- 每个 `chunk_id` 必须来自本次实际检索结果；
- `source`、`rule_id`、`section` 必须与检索结果完全一致；
- `compliant` 和 `violation` 仍必须至少引用一个有效 `evidence_id`；
- `uncertain` 可以没有规则引用，但 explanation 必须说明证据不足或规则不适用；
- 规则引用写入现有结果 JSON，随已有 `ReviewResult` artifact 持久化；
- 校验失败不得进入 `complete_review_job`，按现有 review retry/dead 语义处理。

因此有效结论关系为：

```text
可用 evidence + 适用规则候选 + 有效 evidence_ids + 有效 rule_citations
  -> compliant / violation

任一必要条件缺失
  -> uncertain
```

## 失败隔离与所有权

- EventLedger 是事件、证据、review 和 index job 的权威持久化所有者。
- RecordingEvidenceWorker 只负责生成并提交派生证据，不调用模型或写 Qdrant。
- ReviewWorker 只负责规则预检索、视觉复验和结构化结果提交，不写规则知识库。
- IndexWorker 只处理已被账本接受的当前 review 投影；事件向量写入失败只影响 index job，
  不回滚事件或复验结果。
- Qdrant 规则检索不可用时，仍允许完成证据检查，但确定性结论必须被拒绝。
- 过期 lease 或 attempts fence 失败时，旧 worker 不得追加 review、创建 index job、
  写结果 artifact 或写入 Qdrant。

## 验收标准

### 配置与构建

1. Python 与 C++ 同时接受 `recording_evidence`、`knowledge` 和最小化 `sources`。
2. 两端对未知键、错误类型和越界值保持 fail closed。
3. RAG 依赖、规则文档和 lock 文件一致，默认配置不依赖外部模型或 Qdrant。

### Evidence

4. Evidence 启用时新事件只生成 `evidence_extract`，成功后才生成 `review`。
5. 成功取证恰好登记一个 clip 和三张 frame，重复任务不产生重复目录或证据。
6. 取证失败进入重试或 `manual_review`，不产生无证据 review。

### RAG 与复验

7. `knowledge_ingest` 能建立当前规则 collection，embedding identity 不同的 collection
   互不可见。
8. ReviewWorker 在调用模型前必然调用 Retriever。
9. 模型输出确定性结论时，evidence 和 rule citation 均通过账本范围校验。
10. 无证据、无适用规则、规则检索失败或引用失效时，不接受确定性结论。
11. review 成功后创建 index job；事件索引失败可独立重试。

### 验证命令

在 `agent/` 下执行 focused tests、完整测试和 Ruff；在仓库根目录执行 C++ 配置测试及
`git diff --check`。自动测试使用 fake Retriever、fake runner、fake embedding 和测试
Qdrant，不下载真实模型。

真实 MediaMTX、真实 DeerFlow 模型和远程 Qdrant 的联调作为后续人工验收，不作为本次
代码合并的必要前置条件。
