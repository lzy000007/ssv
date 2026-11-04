# Agent 可观测性增强 spec

## 目标

为 Agent 验证提供可复核的运行状态证据，覆盖 durable job 积压/过期租约、复核前规则 RAG 召回、后台线程失败、Ingress 日志隐私和死信摘要。

## 需求

### 任务状态诊断

`./ssv cache status` 在现有 Redis Stream、consumer pending、去重键和 SQLite 总量之外，按 `kind/state` 展示 durable job 数量、最大尝试次数、最早更新时间（Unix 毫秒）及过期 processing 租约数，并单独显示过期租约总数。

状态统计使用 SQLite 只读连接，不修改账本。没有任务时不输出不存在的任务分组。若兼容数据库没有 `lease_expires_ms` 列，仍提供任务分组统计并将过期租约记为零。

### RAG 检索审计

`ReviewWorker` 每次规则检索写一条结构化日志，字段包括：

- `event_id`、`job_id`、`entity_revision`；
- `backend`、`success`、`available`、`chunk_count`、`retrieval_ms`；
- 命中片段的 `chunk_id`、`score`、相对 `source`、`rule_id`、`rule_version` 和 `section`；
- 查询长度与 SHA-256，不记录原始查询文本或片段正文；
- 异常类型及归一化 `failure_code`，不记录异常原文或宿主机路径。

检索成功但无命中、检索异常和未配置 retriever 均可由日志区分。记录日志不改变“检索失败仍继续复核、由规则引用校验决定结果”的现有语义。

### 后台线程失败

`AgentService` 管理的 consumer 和 worker 线程统一捕获异常及停止请求之外的正常退出。发生异常或意外退出时记录线程名及安全的错误类别，通知 consumer 停止并设置服务停止信号；`wait()` 在后台故障出现时尽快抛出不含异常原文的 `RuntimeError`，线程善后仍由有限等待的 `stop()` 负责。收到停止请求后退出属于正常关闭，不自动重启线程。

### Ingress 日志脱敏

畸形 payload、事件校验失败和账本写入失败日志不得记录原始 payload 或自由格式异常文本。保留排查所需的 ingress/event 关联 ID、payload 长度和异常类型；Redis ACK 与失败后保留 pending 的既有语义不变。消费组创建只忽略 Redis `BUSYGROUP`，其他初始化错误向上失败。

### 死信摘要

`./ssv cache status` 只读列出最近 5 条 `dead` durable job 的任务 ID、类型、revision、尝试次数、更新时间及白名单错误码，不输出 entity ID 或自由格式 `last_error`。历史非白名单错误统一显示 `legacy_unclassified`；worker 新写入的 `last_error` 仅使用 `review_failed`、`index_failed`、`evidence_extraction_failed`、`lease_heartbeat_failed` 或 `max_retries_exceeded`。兼容 schema 缺少 `last_error` 时仍输出死信摘要并使用 `legacy_unclassified`。

## 非本阶段范围

- 不新增 Web API、Prometheus/OpenTelemetry exporter 或实时逐帧推理状态协议；
- 不把 worker 的持久租约统计解释为操作系统线程仍存活；
- 不修改 RAG 的切分、embedding、相似度阈值、过滤或引用校验算法；
- 不改变 Redis ACK、SQLite durable job 状态转换、重试与死信保留语义；
- 不记录规则正文、原始查询文本、异常原文或宿主机证据路径。

## 验收标准

1. 临时 EventLedger 能按任务类型和状态分组，并区分过期和未过期租约；旧 schema 仍可只读统计。
2. `cache status` 输出分组、更新时间、最大尝试次数和过期租约计数，保留现有 Redis/SQLite 错误语义。
3. RAG 命中、空结果、异常及未配置路径产生可关联 `event_id/job_id/revision` 的结构化日志，且不包含查询或规则正文。
4. 后台线程异常、意外退出及消费组初始化非 `BUSYGROUP` 错误均能唤停服务并通过 `wait()` 报告；正常停止不产生失败报告。
5. Ingress 失败日志不含原始 payload 或异常原文；死信查询只读、最多返回 5 条且仅显示白名单错误码。
6. 相关 CLI、Agent worker、consumer 和 service 测试通过；新增用例不依赖真实 Redis、Qdrant 或模型服务。
