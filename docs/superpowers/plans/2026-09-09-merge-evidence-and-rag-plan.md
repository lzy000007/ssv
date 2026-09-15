# Evidence 与规则 RAG 合并实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前分支合并 T4 录像证据链和 `pull/29` 规则 RAG，使 Agent 在有证据的前提下先检索规则再完成可追溯复验。

**Architecture:** 以 `feat/T4-evidence-extraction` 的 `evidence_extract -> review -> index` 持久任务链为主流程，叠加 `pull/29` 的本地 Markdown/Qdrant 规则检索。在 `ReviewWorker` 调用 DeerFlow 前执行确定性规则预检索，并用本次检索结果校验模型的 `rule_citations`。Evidence、规则知识库和事件语义索引分别由专属组件持有，失败互相隔离。

**Tech Stack:** Python 3.12、Pydantic 2、SQLite、Redis Streams、Qdrant、LangChain tools、DeerFlow、FFmpeg/FFprobe、C++ YAML 配置校验、pytest、Ruff、Meson。

**Spec:** [2026-09-09-merge-evidence-and-rag-design.md](../specs/2026-09-09-merge-evidence-and-rag-design.md)

## Global Constraints

- 当前工作区已有 `.env.bak`、`config/ssv.yaml.bak`、`data/events.db`、`meson.build`、`agent/uv.lock`、`skills-lock.json` 变化，实施前后均不得覆盖、回退或夹带无关修改。
- 纳入 `feat/T4-evidence-extraction` 的 `0686f23` 和 `pull/29` 的 RAG 提交 `bb500ca`；必要时纳入 `cef5066` 的 CI 依赖安装修复；不纳入 `6348155` 的 `AGENTS.md` 修改。
- 面向人的新增文档使用中文；代码标识、命令、路径、配置键和 JSON 字段保持原文。
- `agent.recording_evidence.enabled=true` 时只能先创建 `evidence_extract`，四项证据成功登记后才能创建 `review`。
- ReviewWorker 在调用模型前必须调用 Retriever；无可用 evidence 或无适用规则时只能接受 `uncertain`。
- `compliant`/`violation` 必须引用有效 `evidence_ids` 和本次检索返回的 `rule_citations`；无效引用不得进入 `complete_review_job`。
- `ssv_rules` 只服务规则检索，`ssv_events` 只服务事件语义搜索；不同 embedding identity 必须使用不同物理 collection。
- 默认配置保持不依赖外部模型、远程 Qdrant 或 MediaMTX；测试使用 fake Retriever、fake runner、fake embedding 和测试 Qdrant。
- 每个任务完成后保持变更为未暂存状态；未经用户明确要求不暂存、不提交、不推送、不创建 PR。

## 文件与职责映射

### T4 Evidence 文件

- `agent/src/ssv_agent/recording_evidence.py`：安全选择 MediaMTX 分段并原子生成 clip、三张 frame 和 manifest。
- `agent/src/ssv_agent/event_store/schema.py`：迁移 `durable_jobs.kind`，加入 `evidence_extract`。
- `agent/src/ssv_agent/event_store/ledger.py`：创建、完成、失败 evidence job，登记 EvidenceRef 并创建 review job。
- `agent/src/ssv_agent/workers.py`：拥有 `RecordingEvidenceWorker`、`ReviewWorker`、`IndexWorker` 的租约、重试和停止语义。
- `agent/src/ssv_agent/event_consumer.py`：使用带 evidence 策略的账本持久化后再 ACK Redis 消息。

### RAG 文件

- `agent/src/ssv_agent/knowledge/backends/local_markdown.py`：本地规则文档切分和词法检索。
- `agent/src/ssv_agent/knowledge/backends/qdrant.py`：规则 embedding、Qdrant 入库和检索。
- `agent/src/ssv_agent/knowledge_ingest.py`：显式构建当前规则向量索引。
- `agent/src/ssv_agent/knowledge/registry.py`：规则后端懒加载和 Retriever/Ingester 工厂。
- `agent/src/ssv_agent/event_store/qdrant_store.py`：`ssv_rules`/`ssv_events` collection、identity 隔离及规则 point 操作。
- `agent/src/ssv_agent/embedding/registry.py`、`agent/src/ssv_agent/embedding/backends/openai_compatible.py`：embedding 身份、配置和实例缓存。
- `agent/src/ssv_agent/tools/rule_retriever.py`：DeerFlow 只读规则检索工具。

### 集成文件

- `agent/src/ssv_agent/config.py`：合并 `AgentSourceConfig`、`RecordingEvidenceConfig`、`KnowledgeConfig`。
- `agent/src/ssv_agent/service.py`：合并三个 worker 的生命周期和 knowledge/evidence 环境配置。
- `agent/src/ssv_agent/review_context.py`：保持权威事件上下文与一次性规则上下文的边界。
- `agent/src/ssv_agent/prompt.py`、`agent/src/ssv_agent/runner.py`：将证据和规则候选送入模型，并固定调用顺序与安全边界。
- `agent/src/ssv_agent/result.py`：增加 `RuleCitation` 和规则引用校验。
- `agent/src/ssv_agent/search/rule_query.py`：从 `EventCase` 构造稳定规则查询。
- `config/ssv.example.yaml`、`docs/Agent配置.md`、`.env.example`、`agent/pyproject.toml`、`agent/uv.lock`、`.github/workflows/ci.yml`：配置、依赖、部署和 CI。

## Task 1: 引入并验证 T4 Evidence 持久链路

**Files:**

- Create: `agent/src/ssv_agent/recording_evidence.py`
- Modify: `agent/src/ssv_agent/config.py`
- Modify: `agent/src/ssv_agent/event_consumer.py`
- Modify: `agent/src/ssv_agent/event_store/schema.py`
- Modify: `agent/src/ssv_agent/event_store/ledger.py`
- Modify: `agent/src/ssv_agent/prompt.py`
- Modify: `agent/src/ssv_agent/service.py`
- Modify: `agent/src/ssv_agent/workers.py`
- Create/Modify: `agent/tests/test_recording_evidence.py`, `agent/tests/test_event_ledger.py`, `agent/tests/test_event_consumer.py`, `agent/tests/test_workers.py`, `agent/tests/test_config.py`, `agent/tests/test_service.py`, `agent/tests/test_evidence_reader.py`
- Modify: `config/ssv.example.yaml`, `docs/Agent配置.md`, `gst/ssv-common/config/ssv_config.cpp`, `gst/ssv-common/tests/test_ssv_config.cpp`
- Create: `docs/specs/2026-09-01-MediaMTX录像上下文证据提取-spec.md`, `docs/plans/2026-09-01-MediaMTX录像上下文证据提取-plan.md`

**Interfaces:**

- `EventLedger.record(context: ReviewContext) -> RecordOutcome`：录像证据启用时创建一个 `evidence_extract`，关闭时保持 `review`/`index` 快速路径。
- `EventLedger.complete_evidence_extract_job(job: DurableJob, worker_id: str, artifacts: tuple[RecordingEvidenceArtifact, ...]) -> None`：在事务内登记一件 clip、三件 frame、创建 review job 并完成 extract job。
- `RecordingEvidenceExtractor.extract(case: EventCase) -> tuple[RecordingEvidenceArtifact, ...]`：只允许 evidence root 内安全输入，输出完整四件派生证据。
- `RecordingEvidenceWorker.run_once() -> bool`：完成 evidence job 的 claim、提取、租约确认和 fenced commit。

- [ ] **Step 1: 将 T4 分支新增文件与测试作为候选实现导入，并检查共同文件只保留 T4 语义。**

  使用 `git show feat/T4-evidence-extraction:<path>` 与当前文件逐项比对；对新增文件采用 T4 版本，对共同文件先保留 T4 的 evidence 逻辑，再等待 Task 3 合并 RAG 字段。不得使用 `git checkout` 或覆盖工作区全文件。

- [ ] **Step 2: 运行 T4 focused tests，确认 Evidence 基线行为。**

  Run: `cd agent && uv run pytest -q tests/test_recording_evidence.py tests/test_event_ledger.py tests/test_event_consumer.py tests/test_workers.py tests/test_evidence_reader.py`

  Expected: T4 相关测试通过；若因共同文件尚未加入 RAG 接口而失败，记录失败测试，暂不改变 T4 任务语义。

- [ ] **Step 3: 检查 T4 的事务和任务门控。**

  验证以下结果：启用录像证据时 `record()` 返回的首个任务是 `JobKind.EVIDENCE_EXTRACT`，没有 review job；`complete_evidence_extract_job()` 成功后恰有一个 review job；失败进入 retry/dead；旧 lease 不能登记证据或推进 review。

- [ ] **Step 4: 保持任务 1 变更未暂存并报告。**

  Run: `git status --short && git diff --stat`

  报告实际修改文件、`EventLedger`/`RecordingEvidenceWorker` 接口、证据文件所有权和 focused test 结果；不要执行 `git add` 或 `git commit`。

## Task 2: 引入并验证 RAG/Qdrant 规则知识库

**Files:**

- Create: `agent/knowledge/GB+26860-2011 (1).md`
- Create: `agent/src/ssv_agent/knowledge/backends/local_markdown.py`
- Create: `agent/src/ssv_agent/knowledge/backends/qdrant.py`
- Create: `agent/src/ssv_agent/knowledge_ingest.py`
- Modify: `agent/src/ssv_agent/knowledge/registry.py`
- Modify: `agent/src/ssv_agent/event_store/qdrant_store.py`
- Modify: `agent/src/ssv_agent/embedding/registry.py`
- Modify: `agent/src/ssv_agent/embedding/backends/openai_compatible.py`
- Modify: `agent/src/ssv_agent/tools/rule_retriever.py`
- Modify: `agent/tests/test_knowledge_ingest.py`, `agent/tests/test_local_markdown.py`, `agent/tests/test_qdrant_rule_retriever.py`, `agent/tests/test_qdrant_store.py`, `agent/tests/test_embedding.py`
- Modify: `agent/pyproject.toml`, `agent/uv.lock`, `.env.example`, `.github/workflows/ci.yml`
- Create: `docs/specs/2026-09-02-Agent-规则向量RAG-spec.md`, `docs/plans/2026-09-02-Agent-规则向量RAG-plan.md`（仅纳入 `pull/29` 中与 RAG 实现对应的文档）

**Interfaces:**

- `Retriever.retrieve(query: str, *, top_k: int = 5, filters: dict[str, Any] | None = None) -> RetrievalResult`：本地 Markdown 和 Qdrant 后端统一使用的异步只读接口。
- `QdrantRuleRetriever.retrieve(...) -> RetrievalResult`：返回含 `chunk_id`、`content`、`source`、`rule_id`、`section`、`score` 的规则候选。
- `knowledge_ingest.main()`：按配置选择 embedding 和 Qdrant path，批量入库当前规则文档。
- `derive_physical_collection_name(base_collection: str, embedding_identity: EmbeddingIdentityInput) -> str`：按 embedding identity 隔离规则和事件 collection。

- [ ] **Step 1: 导入 RAG 独有文件和测试，不导入 `6348155` 的 `AGENTS.md` 差异。**

  使用 `git show pull/29:<path>` 逐文件引入 RAG 独有代码、规则文档、测试、spec/plan 和必要依赖。`AGENTS.md` 保持当前工作区版本。

- [ ] **Step 2: 验证本地 Markdown 后端默认检索。**

  Run: `cd agent && uv run pytest -q tests/test_local_markdown.py tests/test_knowledge_ingest.py`

  Expected: 默认后端读取 `agent/knowledge/`，结果包含来源、规则标识和条款号；文档不存在或无命中时返回明确状态。

- [ ] **Step 3: 验证 Qdrant 规则 collection 和 embedding identity。**

  Run: `cd agent && uv run pytest -q tests/test_qdrant_rule_retriever.py tests/test_qdrant_store.py tests/test_embedding.py`

  Expected: 规则 collection 可单独创建和查询；不同 embedding identity 不互相命中；低于 `min_score`、空 collection 和缺失 collection 返回明确失败/无依据结果；mock embedding 不被正式 Qdrant 规则检索接受。

- [ ] **Step 4: 验证规则入库幂等和失败保护。**

  使用 fake embedding 检查：单批文本不超过 20 条；重复入库产生稳定 chunk ID；文档删除后旧 point 被清理；embedding 失败时旧完整投影保持不变。

- [ ] **Step 5: 保持任务 2 变更未暂存并报告。**

  Run: `git status --short && git diff --stat`

  报告 `Retriever`、`QdrantRuleRetriever`、`knowledge_ingest`、规则 collection 和 embedding identity 的实际验证结果；不要执行 `git add` 或 `git commit`。

## Task 3: 合并共同配置、Service 生命周期和 C++ 校验

**Files:**

- Modify: `agent/src/ssv_agent/config.py`
- Modify: `agent/src/ssv_agent/service.py`
- Modify: `config/ssv.example.yaml`
- Modify: `docs/Agent配置.md`
- Modify: `gst/ssv-common/config/ssv_config.cpp`
- Modify: `gst/ssv-common/tests/test_ssv_config.cpp`
- Modify: `agent/tests/test_config.py`, `agent/tests/test_service.py`

**Interfaces:**

- `AgentSourceConfig(id: str, uri: str)`：从共享配置中保留 source 身份和 RTSP URI。
- `RecordingEvidenceConfig`：保留 T4 的窗口、frame offset、lease 和 retry 字段。
- `KnowledgeConfig(backend: Literal["local_markdown", "qdrant", "mock"], qdrant_path: str, min_score: float)`：规则后端、Qdrant 路径和阈值配置。
- `AgentService.start()`：依次启动 recording evidence、review、index worker，最后创建 EventConsumer。
- `AgentService._ledger_factory() -> EventLedger`：所有 worker 使用独立账本连接，并携带 evidence 配置。
- `AgentService._configure_runtime_environment(include_deerflow: bool)`：设置 `SSV_EVIDENCE_ROOTS`、`SSV_EMBEDDING_*`、`SSV_KNOWLEDGE_*`。

- [ ] **Step 1: 在 `config.py` 合并配置模型和共享根过滤。**

  将 `_AGENT_CONFIG_KEYS` 扩展为包含 `sources`；`SsvConfig` 同时暴露 `sources`、`agent.recording_evidence` 和 `agent.knowledge`。保留 T4 对 source 只提取 `id`/`uri` 的行为，保留 RAG 的 knowledge 默认值 `local_markdown`、`data/qdrant`、`0.5`；未知字段继续由 strict Pydantic 模型拒绝。

- [ ] **Step 2: 先写共同配置的合并测试，再运行失败测试。**

  在 `agent/tests/test_config.py` 覆盖以下行为：

  ```python
  config = SsvConfig.model_validate({
      "sources": [{"id": "camera-01", "uri": "rtsp://host/stream", "codec": "h264"}],
      "agent": {
          "evidence_roots": ["/var/lib/ssv/evidence"],
          "recording_evidence": {"enabled": True},
          "knowledge": {"backend": "qdrant", "qdrant_path": "data/qdrant", "min_score": 0.65},
      },
  })
  assert config.sources[0].id == "camera-01"
  assert config.agent.recording_evidence.enabled is True
  assert config.agent.knowledge.backend == "qdrant"
  ```

  Run: `cd agent && uv run pytest -q tests/test_config.py -k 'recording or knowledge'`

  Expected: 在实现完整字段并集前，测试至少暴露缺失字段或校验差异；不得删除任一分支已有有效测试。

- [ ] **Step 3: 收口 Service worker 生命周期和环境变量。**

  保留 T4 的 `_start_recording_evidence_worker()`、`_create_consumer()` 和带 recording config 的 `_ledger_factory()`；合并 RAG 的 `_resolve_agent_path()`、knowledge backend/path/min score 环境设置。Review worker 构造时额外注入规则 Retriever 工厂，详见 Task 4。`request_stop()` 不获取生命周期锁，stop 时仍按现有超时保留共享资源。

- [ ] **Step 4: 合并 Python/C++ 示例配置校验。**

  C++ `validate_agent_extensions()` 同时调用 `validate_recording_evidence_extension()` 和 `validate_knowledge_extension()`；Python 和 C++ 对未知键、类型、范围使用相同字段名和边界。示例配置同时保留两个配置段，默认关闭录像并使用本地 Markdown。

- [ ] **Step 5: 运行共同配置和 Service 测试。**

  Run: `cd agent && uv run pytest -q tests/test_config.py tests/test_service.py`

  Run: `uv run ruff check agent/src/ssv_agent/config.py agent/src/ssv_agent/service.py agent/tests/test_config.py agent/tests/test_service.py`

  Expected: 配置字段并集、worker 启动顺序、evidence ledger 注入和 `SSV_KNOWLEDGE_*` 环境恢复测试通过。

- [ ] **Step 6: 保持任务 3 变更未暂存并报告。**

  Run: `git status --short && git diff --stat`

  报告实际共同文件、配置字段、worker 创建方/持有方/释放方和测试结果；不要执行 `git add` 或 `git commit`。

## Task 4: 实现 ReviewWorker 规则预检索和结果引用契约

**Files:**

- Create: `agent/src/ssv_agent/search/rule_query.py`
- Modify: `agent/src/ssv_agent/review_context.py`
- Modify: `agent/src/ssv_agent/prompt.py`
- Modify: `agent/src/ssv_agent/runner.py`
- Modify: `agent/src/ssv_agent/result.py`
- Modify: `agent/src/ssv_agent/workers.py`
- Modify: `agent/src/ssv_agent/service.py`
- Create/Modify: `agent/tests/test_rule_query.py`, `agent/tests/test_prompt.py`, `agent/tests/test_result.py`, `agent/tests/test_runner.py`, `agent/tests/test_workers.py`, `agent/tests/test_service.py`

**Interfaces:**

- `build_rule_query(case: EventCase) -> str`：按 event type、rule facts、detections、severity、question 生成规则查询。
- `RuleRetrievalContext`：一次复验的查询、`RetrievalResult`、候选 chunks、可用性和错误信息；不写入事件表或 Redis。
- `run_review(client: DeerFlowClient, context: ReviewContext, rule_context: RuleRetrievalContext | None = None) -> str`：兼容旧调用，prompt 包含一次性规则候选。
- `build_review_prompt(context: ReviewContext, rule_context: RuleRetrievalContext | None = None) -> str`：固定先 evidence_reader、再规则确认、最后输出 JSON。
- `RuleCitation(chunk_id: str, source: str, rule_id: str, section: str)`：模型规则引用结构。
- `validate_rule_citations(result: ReviewResult, rule_context: RuleRetrievalContext) -> ReviewResult`：确认 citation 来自本次实际检索结果，并在规则不可用时拒绝确定性结论。
- `ReviewWorker(..., rule_retriever: Retriever, ...)`：在 `_runner` 前调用异步 Retriever，异常转换为不可用的规则上下文。

- [ ] **Step 1: 先写规则查询和引用失败测试。**

  测试至少包含：查询包含 `event_type`、`rule_id`、`rule_facts`、检测对象和 severity；确定性结果缺少 citation 被拒绝；引用不存在的 chunk 被拒绝；同 chunk 的 source/section 不一致被拒绝；无规则候选时只允许 `uncertain`。

  ```python
  result = ReviewResult(
      verdict="violation",
      confidence=0.9,
      evidence_status="available",
      evidence_ids=["evidence-1"],
      rule_citations=[],
      claims=[],
      explanation="没有规则引用",
  )
  with pytest.raises(ResultParseError):
      validate_rule_citations(result, unavailable_rule_context)
  ```

- [ ] **Step 2: 运行失败测试，确认缺少规则契约。**

  Run: `cd agent && uv run pytest -q tests/test_rule_query.py tests/test_result.py -k 'rule or citation'`

  Expected: 新测试因 `RuleCitation`、`rule_citations` 或校验函数尚未实现而失败；保留失败输出作为实现基线。

- [ ] **Step 3: 增加 `RuleCitation` 和 `ReviewResult` 校验。**

  在 `result.py` 中增加 `RuleCitation` Pydantic 模型和 `ReviewResult.rule_citations: list[RuleCitation]`。保留现有 evidence contract；基础模型校验字段非空，跨本次检索候选的 chunk/source/section 一致性放入 `validate_rule_citations()`，避免模型层依赖 Retriever。

- [ ] **Step 4: 增加规则查询和一次性上下文。**

  `build_rule_query()` 对同一案件生成稳定文本；`RuleRetrievalContext` 保存 `query`、`result`、`chunks`、`available` 和 `error_message`。不得从模型输出反推或覆盖案件的权威 `rule_id`、`rule_version`、`rule_facts`。

- [ ] **Step 5: 将预检索接入 ReviewWorker。**

  `run_once()` 在 `runner(case.to_review_context())` 之前执行：

  ```python
  query = build_rule_query(case)
  try:
      retrieval = _run_retriever(self._rule_retriever, query)
  except Exception as exc:
      retrieval = unavailable_rule_context(query, type(exc).__name__)
  rule_context = RuleRetrievalContext.from_result(query, retrieval)
  text = self._runner(case.to_review_context(), rule_context)
  result = validate_rule_citations(parse_review_result(text), rule_context)
  ```

  保持 evidence refresh、lease heartbeat、artifact writer 和 `complete_review_job()` 的既有 fence 顺序。最终提交前同时执行既有 `evidence_ids` 范围校验和新增 `rule_citations` 范围校验；规则后端异常不重投 Redis ingress，模型输出确定性结论时由任一引用校验触发 review retry/dead。

- [ ] **Step 6: 更新 prompt 和 runner。**

  prompt 明确要求顺序为 evidence_reader、规则候选核验、视觉复验、结构化输出；把规则候选和检索失败标记为不可信输入。`run_review()` 保留旧的两参数调用兼容性，新增参数只影响 prompt。

- [ ] **Step 7: 更新 Service 注入真实 Retriever 和 fake seam。**

  Service 根据 `agent.knowledge.backend` 创建 Retriever；默认使用 `local_markdown`，显式 qdrant 使用 Qdrant backend。测试通过 `fake_retriever` 观察调用顺序，确认调用模型前 Retriever 已被调用。

- [ ] **Step 8: 运行 Review focused tests。**

  Run: `cd agent && uv run pytest -q tests/test_rule_query.py tests/test_prompt.py tests/test_result.py tests/test_runner.py tests/test_workers.py tests/test_service.py`

  Run: `uv run ruff check agent/src/ssv_agent/search/rule_query.py agent/src/ssv_agent/review_context.py agent/src/ssv_agent/prompt.py agent/src/ssv_agent/runner.py agent/src/ssv_agent/result.py agent/src/ssv_agent/workers.py agent/tests/test_rule_query.py agent/tests/test_prompt.py agent/tests/test_result.py agent/tests/test_runner.py agent/tests/test_workers.py`

  Expected: Retriever 前置调用、规则引用范围校验、无规则时 uncertain、evidence contract 和 lease fence 测试通过。

- [ ] **Step 9: 保持任务 4 变更未暂存并报告。**

  Run: `git status --short && git diff --stat`

  报告 `ReviewWorker`、`RuleRetrievalContext`、`RuleCitation` 和 `validate_rule_citations()` 的实际接口、调用顺序及测试结果；不要执行 `git add` 或 `git commit`。

## Task 5: 增加 Evidence + RAG + Review + Event Index 集成测试

**Files:**

- Create: `agent/tests/test_evidence_rag_review_integration.py`
- Modify: `agent/tests/test_workers.py`, `agent/tests/test_event_ledger.py`, `agent/tests/test_service.py`
- Modify: `agent/src/ssv_agent/workers.py`, `agent/src/ssv_agent/event_store/ledger.py` only when a failing integration test exposes an interface gap

**Interfaces:**

- 使用真实 `EventLedger` SQLite 临时库、fake `RecordingEvidenceExtractor`、fake `Retriever`、fake runner 和测试 Qdrant。
- 集成断言只通过公开行为观察：job state、EvidenceRef、ReviewResult、result artifact、index job 和 Qdrant point，不读取 worker 私有状态。

- [ ] **Step 1: 写成功链路测试。**

  测试构造一个事件并完成 extract job，断言四项 evidence 和 review job 存在；fake Retriever 返回一个带 `source`/`rule_id`/`section` 的 chunk；fake runner 返回带 evidence 和 rule citation 的 `violation`；完成 review 后断言创建 index job。

- [ ] **Step 2: 写失败与降级测试。**

  覆盖：无 evidence 时 runner 只能形成 uncertain；Retriever 空结果或异常时确定性结果被拒绝；错误 chunk citation 被拒绝；Qdrant 事件索引失败不回滚已接受 review，index job 保持可重试。

- [ ] **Step 3: 写幂等、租约和顺序测试。**

  覆盖重复 event ingress、extract retry、review retry、lease reclaim 和重复 completion；断言不会生成重复 EvidenceRef、review 或 index；旧 attempts 不能追加 artifact 或写入 Qdrant。

- [ ] **Step 4: 运行集成测试。**

  Run: `cd agent && uv run pytest -q tests/test_evidence_rag_review_integration.py tests/test_event_ledger.py tests/test_workers.py`

  Expected: 成功路径和所有失败隔离测试通过。

- [ ] **Step 5: 保持任务 5 变更未暂存并报告。**

  Run: `git status --short && git diff --stat`

  报告端到端任务状态、证据数量、规则引用、复验结果和事件索引行为；不要执行 `git add` 或 `git commit`。

## Task 6: 收口文档、依赖和运行说明

**Files:**

- Modify: `.env.example`
- Modify: `agent/pyproject.toml`
- Modify: `agent/uv.lock`
- Modify: `.github/workflows/ci.yml`
- Modify: `config/ssv.example.yaml`
- Modify: `docs/Agent配置.md`
- Modify: `docs/superpowers/specs/2026-09-09-merge-evidence-and-rag-design.md` only if implementation reveals a confirmed contract correction

**Interfaces:**

- `agent/pyproject.toml`：保留 `langchain>=1.3`、`openai>=1.0`、`qdrant-client>=1.9`，BGE-M3 仍为可选依赖。
- `config/ssv.example.yaml`：同时展示 `recording_evidence` 和 `knowledge`；默认 `recording_evidence.enabled=false`、`knowledge.backend=local_markdown`。
- `knowledge_ingest`：文档变更后显式执行规则入库，说明本地/远程 Qdrant、embedding model 和 identity 隔离。

- [ ] **Step 1: 更新配置和环境变量说明。**

  说明 `SSV_KNOWLEDGE_BACKEND`、`SSV_QDRANT_PATH`、`SSV_KNOWLEDGE_MIN_SCORE`、embedding 配置、evidence root、MediaMTX 录像目录以及 `rule_citations` 结果字段。

- [ ] **Step 2: 同步 lock 文件和 CI 安装命令。**

  Run: `cd agent && uv lock`

  Run: `uv run ruff check agent/src agent/tests`

  Expected: lock 文件只反映声明依赖；CI 使用仓库现有约定安装 dev/model-export 及新增运行时依赖。

- [ ] **Step 3: 检查 spec、plan 和文档无占位符。**

  Run: `rg -n 'TODO|TBD|待补充|待定' docs/superpowers/specs/2026-09-09-merge-evidence-and-rag-design.md docs/superpowers/plans/2026-09-09-merge-evidence-and-rag-plan.md docs/Agent配置.md || true`

  Expected: 无未完成占位符；文档明确规则库首次入库、Qdrant 不可用、MediaMTX 证据时间语义和人工验收边界。

- [ ] **Step 4: 保持任务 6 变更未暂存并报告。**

  Run: `git status --short && git diff --stat`

  报告依赖、配置、文档和 CI 的实际变更；不要执行 `git add` 或 `git commit`。

## Task 7: 全量验证与交付检查

**Files:**

- Test: `agent/tests/` 全部 Python 测试
- Test: `gst/ssv-common/tests/test_ssv_config.cpp`
- Verify: 所有实现文件、配置、文档和工作区差异

**Interfaces:**

- Python 测试验证 `EventLedger`、workers、Retriever、Qdrant、Service 和结果契约。
- C++ 测试验证共享 YAML 配置的 evidence/RAG 字段和 fail-closed 规则。
- Git 检查验证不包含 `6348155` 的 `AGENTS.md` 功能外变更，不覆盖用户已有修改。

- [ ] **Step 1: 运行 Python 全量测试和 Ruff。**

  Run: `cd agent && uv run pytest -q`

  Run: `uv run ruff check src tests`

  Expected: 全量测试和 Ruff 通过；失败必须区分环境问题、既有失败和本次合并回归。

- [ ] **Step 2: 运行 C++ 配置测试和构建相关检查。**

  Run: `meson setup build --wipe`（仅在仓库现有构建目录策略允许时执行；若会覆盖用户构建产物，改用现有构建目录的非破坏性 test 命令。）

  Run: `meson test -C build --print-errorlogs`

  Expected: `agent.recording_evidence` 与 `agent.knowledge` 的接受、未知键、错误类型和范围测试通过。

- [ ] **Step 3: 运行差异和工作区保护检查。**

  Run: `git diff --check`

  Run: `git status --short --branch`

  Run: `git diff --stat -- . ':!AGENTS.md'`

  Expected: 无本次新增的 whitespace 错误；预先存在的用户修改仍在；不产生 outputs、Qdrant 临时库、截图、日志或模型缓存交付物。

- [ ] **Step 4: 人工确认真实联调前置条件。**

  仅记录，不在自动验证中执行：MediaMTX `/stream` 已产生完整分段；规则文档已通过 `knowledge_ingest` 入库；配置使用相同 embedding identity；DeerFlow 能读取四项 evidence 并调用 `rule_retriever`；远程 Qdrant 和真实模型的失败隔离符合文档。

- [ ] **Step 5: 输出最终交付报告，保持不提交状态。**

  报告实际修改文件、实际接口、任务顺序、所有权影响、focused/full test、Ruff、C++ test、`git diff --check` 结果，以及未完成的真实环境风险。除非用户另行明确要求，不暂存、不提交、不推送。
