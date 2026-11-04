# Agent 可观测性增强 plan

本计划拆分为两个可独立审阅的步骤。步骤 1 为当前已完成的运行/RAG 观测增强；步骤 2 补齐后台线程退出、Ingress 日志隐私和死信摘要。

## 步骤 1：运行与 RAG 观测增强

步骤 1 的范围包含以下文件：

- `scripts/ssv_cli/services/event_db.py`：新增 `DurableJobStatus` 和只读任务分组查询。
- `scripts/ssv_cli/commands/cache.py`：渲染任务细分及过期租约总数。
- `scripts/ssv_cli/tests/test_event_db.py`：覆盖分组统计、尝试数、更新时间、过期/未过期租约和旧 schema。
- `scripts/ssv_cli/tests/test_cli.py`：覆盖任务分组和过期租约展示。
- `agent/src/ssv_agent/workers.py`：为 RAG 命中、空结果和异常增加脱敏审计日志；未配置 retriever 时记录跳过原因。
- `agent/tests/test_workers.py`：覆盖日志关联字段、结果状态与隐私边界。
- `docs/Agent配置.md`：说明新增状态字段、RAG 日志语义和限制。

涉及标识符：`DurableJobStatus`、`EventDbStatus.durable_job_states`、`_durable_job_statuses`、`cache.status`、`_retrieval_chunk_observation`、`_retrieval_failure_code`、`_log_rule_retrieval`、`ReviewWorker.run_once`。

接口影响：不改 SQLite schema、Redis ACK、job lease、RAG 算法或复核结果；不新增 HTTP/metrics 接口。任务租约不是 worker 线程存活证明。

验证命令：

- `python -m unittest discover -s scripts/ssv_cli/tests -p 'test_*.py'`（仓库根目录）
- `uv run --extra dev pytest tests/test_workers.py tests/test_qdrant_rule_retriever.py tests/test_service.py`（`agent/`）
- `uv run --extra dev ruff check src/ssv_agent/workers.py tests/test_workers.py`（`agent/`）
- `uv run ruff check scripts/ssv_cli/services/event_db.py scripts/ssv_cli/commands/cache.py scripts/ssv_cli/tests/test_event_db.py scripts/ssv_cli/tests/test_cli.py`（仓库根目录）

## 步骤 2：后台线程、Ingress 与死信观测

涉及文件：

- `agent/src/ssv_agent/service.py`：统一包装 consumer/worker 线程，记录首个失败并使 `wait()` 尽快抛出脱敏错误；线程善后由有限等待的 `stop()` 负责。
- `agent/src/ssv_agent/event_consumer.py`：移除 ingress 失败日志中的原始 payload 和异常文本；消费组初始化只忽略 `BUSYGROUP`。
- `agent/src/ssv_agent/workers.py`：持久化受控 `last_error` 代码，保留重试/死信状态转换。
- `scripts/ssv_cli/services/event_db.py`：只读读取最近 5 条死信，并把历史自由文本映射为安全错误码。
- `scripts/ssv_cli/commands/cache.py`：在 `cache status` 展示脱敏死信摘要。
- `agent/tests/test_service.py`、`agent/tests/test_event_consumer.py`、`agent/tests/test_workers.py`：覆盖线程失败、日志脱敏和受控持久化错误码。
- `scripts/ssv_cli/tests/test_event_db.py`、`scripts/ssv_cli/tests/test_cli.py`：覆盖只读死信列表、数量上限、旧错误兼容和渲染。
- `docs/Agent配置.md`、本 spec/plan：说明边界、错误码及观测限制。

涉及标识符：`AgentService.wait`、`_run_background_thread`、`_record_background_failure`、`EventConsumer._ensure_group`、`EventConsumer.handle_event`、`_job_failure_code`、`DeadLetterStatus`、`EventDbStatus.dead_letters`、`_dead_letter_statuses`、`EventDbAdmin.status`、`cache.status`。

接口与所有权影响：服务仍拥有全部线程和 consumer 生命周期；不新增重启策略或 API。SQLite 只读返回最多 5 条死信摘要，不展示 `entity_id` 或原始 `last_error`；Redis ACK、任务重试和状态转换保持不变。

验证命令：

- `uv run --extra dev pytest tests/test_service.py tests/test_event_consumer.py tests/test_workers.py`（`agent/`）
- `python -m unittest scripts.ssv_cli.tests.test_event_db scripts.ssv_cli.tests.test_cli`（仓库根目录）
- `uv run --extra dev ruff check src/ssv_agent/service.py src/ssv_agent/event_consumer.py src/ssv_agent/workers.py tests/test_service.py tests/test_event_consumer.py tests/test_workers.py`（`agent/`）
- `uv run ruff check scripts/ssv_cli/services/event_db.py scripts/ssv_cli/commands/cache.py scripts/ssv_cli/tests/test_event_db.py scripts/ssv_cli/tests/test_cli.py`（仓库根目录）
- `git diff --check`
