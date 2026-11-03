# Agent 配置与运行手册

> 配置文件角色、默认路径和最小启动流程见 [运行与配置](运行与配置.md)；架构、硬件选择和实现状态见 [Agent 架构与实现](Agent架构与实现.md)。本文只展开 Agent 的事件、证据、复核、索引和运维字段，不重复定义模块所有权。

## 运行边界

Agent 是实时检测链路之外的异步服务：

```text
Redis Stream -> EventConsumer -> SQLite EventLedger -> ACK
                                      |
                                      +-> review job -> ReviewWorker -> ReviewRecord
                                                               |
                                                +--------------+--------------+
                                                |                             |
                                                v                             v
                                           IndexWorker                  ReportWorker
                                                |                             |
                                                v                             v
                                      Qdrant 投影（可重建）          Markdown artifact
```

SQLite `EventLedger` 是事件、证据和复核结论的事实源。模型、embedding 或 Qdrant 失败只影响对应异步 job，不应阻塞已经提交的事件；SQLite 写入失败时 Redis entry 保持 pending。

Review 成功提交后，账本同一事务创建 index 与 report job；两者独立运行。报告生成失败不会回滚或改写已完成的 review。

## 安装和启动

Agent 要求 Python >= 3.12：

```bash
git submodule update --init --recursive
cd agent
uv sync --extra dev
```

推荐从项目根目录启动：

```bash
uv run ./ssv agent
uv run ./ssv agent --config config/ssv.yaml --log-level DEBUG
```

首次执行 `uv run ./ssv agent` 且 `agent/.venv` 不存在时，统一 CLI 会先执行 `uv sync`。需要直接运行时：

```bash
cd agent
uv run python -m ssv_agent
```

## 主运行配置

Agent 读取与 runner 共用的 `config/ssv.yaml`，拥有其中的 `version`、`logging`、`redis`、最小化的 `sources`（`id`、`uri`）和 `agent` 配置段；复制模板：

```bash
cp config/ssv.example.yaml config/ssv.yaml
```

搜索顺序为显式 `--config`、`SSV_CONFIG_PATH`、`ssv.yaml`、`config/ssv.yaml`。通过 `uv run ./ssv agent` 启动时，统一 CLI 还会按项目配置发现顺序传入 `/etc/ssv/ssv.yaml`。Agent 配置模型是严格的，版本必须为字符串 `"2.0"`，未知字段和错误类型会拒绝启动。

常用字段：

| 字段 | 作用 |
| --- | --- |
| `redis.host` / `redis.port` / `redis.db` | Redis 连接 |
| `redis.stream_key` | 事件 Stream，默认 `ssv:events` |
| `redis.consumer_group` | 消费组，默认 `ssv-agent` |
| `redis.reclaim_idle_ms` | `XAUTOCLAIM` 回收 pending 的最小 idle 时间 |
| `redis.consumer_name` | 固定 consumer 名称；为空时每个进程生成唯一名称 |
| `agent.model_name` | review worker 使用的默认模型名 |
| `agent.event_db_path` | SQLite EventLedger 路径 |
| `agent.output_dir` | 复核 JSON 与报告 Markdown 的 outputs root |
| `agent.evidence_roots` | 允许登记和读取的绝对证据根目录 |
| `agent.dedup_enabled` / `agent.dedup_cooldown_seconds` | 消费侧冷却去重 |
| `agent.review` | 复核 worker 的开关、lease、重试和 policy |
| `agent.review.recursion_limit` | 单次复核图节点执行步数上限，严格正整数，默认 256 |
| `agent.indexing` | embedding/index worker 的开关、lease、重试和 backend |
| `agent.reporting` | 报告 worker 的开关、轮询、lease 和重试配置 |

最小配置示例：

```yaml
version: "2.0"

redis:
  host: "localhost"
  port: 6379
  stream_key: "ssv:events"
  consumer_group: "ssv-agent"

agent:
  evidence_roots: []
  review:
    enabled: false
  indexing:
    enabled: false
    embedding_backend: "mock"
  reporting:
    enabled: true
    poll_interval_ms: 1000
    lease_ms: 30000
    max_retries: 3
    retry_delay_ms: 1000
```

`agent.reporting.enabled` 默认是 `true`。报告 worker 只消费新提交 review 所创建的 report job；禁用时停止领取，已存在的 pending job 保留，重新启用后继续处理。历史 review 不自动回填报告。

报告由固定模板从账本案件快照与指定历史 `ReviewRecord` 确定性渲染，不再调用 DeerFlow/LLM，也不启用 subagent。每份成功报告在 SQLite `reports` 表登记 review ID、revision、模板版本、SHA-256 和 artifact 路径。Markdown 是展示用派生产物；结构化复核 JSON 与 SQLite review 历史仍是结论依据。报告失败只影响自己的 durable job，可重试并最终进入 `dead`，不会改变 review、index 或 Redis ingress 状态。

报告与复核 JSON 写入 `agent.output_dir` 指向的 outputs root，按事件分目录并以内容寻址，重试产生相同内容时复用同一路径，不同内容不会覆盖既有 artifact。正文包含账本事件事实、复核结论、claims、规则引用和 evidence IDs，不暴露宿主机证据路径。`get_event.reports` 可以查询已登记报告的安全元数据；Review Agent 可调用只读 `get_report(event_id, review_id?)` 获取指定或最新报告的正文、当前 DeerFlow thread outputs 中的 Markdown 虚拟路径，以及可交给 `evidence_reader` 的 `event_id/evidence_id` 引用。读取时重新校验文件在 outputs root 内及内容 SHA-256，缺失或被篡改时返回不可用，不泄露宿主机路径。当前没有 HTTP 报告 API、PDF、历史回填或 artifact 自动清理；部署方需自行管理输出目录的长期保留。

`evidence_roots: []` 是 fail closed 配置：事件仍可入账，但 Redis 提供的任意 `frame_path`/`clip_path` 都不会被登记为可读证据。证据根必须是绝对路径，解析后的 symlink 也不能越界。空列表时，runner 仍可使用默认 evidence cache；这不会把该目录加入 Agent 的证据允许根。

## 只读 Web API

Web API 独立读取 SQLite EventLedger，不启动 Redis consumer 或 Agent worker。事件中的 `source`
是 SSV 配置的 `sources[].id`，用于标识 SSV 输入源；它不表示 WVP 通道编码。若 WVP 页面需要
关联通道，由 WVP 适配层或 BFF 维护映射，不要求普通 SSV 部署在共享 source 配置中填写 WVP 字段。

可在两个终端分别运行：

```bash
./ssv agent --config config/ssv.yaml
```

```bash
cd agent
uv run python -m ssv_agent web --config ../config/ssv.yaml
```

监听地址由 `agent.web.host` 和 `agent.web.port` 控制，默认 `127.0.0.1:8765`。Web API 不负责提供
WVP 通道目录；事件查询通过 `query` 对 event ID、source 和 event type 做文本子串搜索，并支持
工作流状态和分页：

```http
GET /api/v1/events?query=camera-01&status=pending&page=1&page_size=20
```

事件摘要保留原始 `source`，并返回工作流状态和 Agent 持久任务状态：

```json
{
  "event_id": "event-123",
  "source": "camera-01",
  "status": "pending",
  "analysis_task": {"kind": "review", "state": "queued"}
}
```

工作流 `status` 的含义如下：

- `pending`：事件已入账，事件流程尚未结束；不表示 worker 当前正在执行。
- `manual_review`：自动取证失败，或只保留锚点证据而未创建自动复核任务，需要人工跟进。
- `completed`：复核结果已持久化；不表示结论一定合规，也不表示人工已经处理。

`analysis_task` 来自 SQLite `durable_jobs` 中最近的 `review` 或 `evidence_extract` 任务：`waiting` 表示
`available_at_ms` 尚未到，可能在等待首轮取证的 post-roll 窗口或失败后的退避时间；`queued` 表示已到可领取时间；
`processing` 表示账本中有未过期租约；`lease_expired` 表示租约已过期；`completed` 和 `failed` 分别对应任务完成
和进入 dead 状态。若没有匹配任务但已有复核 revision 或事件状态为 `completed`，返回 `review/completed`；其他
未安排这些任务的事件返回 `kind: null, state: not_scheduled`。`processing` 反映持久任务租约，不保证某个操作系统线程
此刻仍在工作。

当前 EventLedger 和 Web API 只包含历史事件、复核结论及 Agent 持久任务状态，不提供 SSV 实时推理帧、当前运行阶段
或逐帧检测状态。若页面中的“当前分析”指实时推理，后端还需要 runner 侧的运行状态/遥测接口；WVP 通道关联由外部
适配层提供，不属于共享 SSV 配置。

只读 Web 服务使用 SQLite `mode=ro`，不执行迁移、不读取 Redis，也不接受客户端文件路径。图片仍通过事件 ID 与证据 ID
的账本关联读取。

## 录像上下文证据

启用 `agent.recording_evidence` 后，Agent 只从 SSV GStreamer pipeline 的短时 cache 读取事件上下文。完整窗口生成一个 clip 和三张帧，取证完成后才创建视觉复核任务；完整窗口缺失且重试耗尽时，同代际有效分段若覆盖事件锚点，可降级留存一张附近图片并保持 `manual_review`，不创建自动复核任务。这里不再配置取证 provider，也不读取外部录像服务的目录。

### SSV cache 取证

先在 runner 配置中启用 `evidence_cache`，再启用 Agent 取证 worker：

```yaml
evidence_cache:
  directory: "/var/lib/ssv/evidence-cache"
  segment_duration_ms: 10000
  retention_ms: 120000
  max_bytes_mb: 512

agent:
  evidence_roots:
    - "/var/lib/ssv/evidence-cache"
  recording_evidence:
    clip_before_ms: 2500
    clip_after_ms: 2500
    merge_gap_ms: 3000
    lost_grace_ms: 3000
    silence_timeout_ms: 30000
    max_episode_ms: 30000
```

`evidence_cache.directory` 必须是绝对路径。`agent.evidence_roots` 非空时，缓存目录必须位于允许根内；列表为空时，只接受默认 `/var/lib/ssv/evidence-cache` 根目录本身或其子目录。这个默认目录例外只允许 runner 创建短时缓存，不会开放 Agent 读取 Redis 任意路径或发布派生证据。证据缓存和录像取证始终启用。runner 为每个 source 创建独立目录，分段和 sidecar 只有在分段完整关闭且元数据包含同一 `stream_generation`、`source_pts_start`、`source_pts_end` 时才可被 Agent 使用。Agent 通过临时目录和原子 rename 发布派生证据，读取中的分段不会被当作完整窗口。

取证 worker 选中完整窗口后，会先在事件临时目录获取所有 MP4 输入：同一文件系统优先建立硬链接，跨文件系统时复制已 finalized 文件，然后才把临时路径交给 ffmpeg。获取完成后 runner 可以删除原缓存文件名，临时输入仍会保持到本次取证结束；取证成功或失败都会清理临时输入。这个保护只覆盖已经成功获取的分片，不是事件级缓存豁免。分片在获取前被清理时，worker 会按重试策略等待下一次机会，重试耗尽后进入 `manual_review`。

缓存只保留 `retention_ms` 内的分段，并受 `max_bytes_mb` 限制；它不是长期录像。缓存运行中的分段收尾或 sidecar 写入失败时，runner 的解码、检测、跟踪和 Redis 发布仍继续；Agent 会按 worker 重试策略等待窗口，最终失败则进入 `manual_review`。首次启动时若缓存目录无法创建或 GStreamer 能力缺失，runner 会报告 capability failure 并拒绝启动，避免启用后静默丢失全部证据。

`recording_evidence` 使用持久 Worker 的轮询、lease 和重试参数接口，但没有启停开关。录像取证默认轮询 1 秒、lease 30 秒、最多重试 3 次，配置的默认重试间隔为 2 秒；实际重试间隔取 `recording_evidence.retry_delay_ms` 与 `evidence_cache.segment_duration_ms` 中的较大值，避免缓存分段尚未 finalized 就耗尽重试。窗口字段只控制录像上下文范围，不再承担 Worker 调度配置。

Agent 不再为每一条重复检测单独取证。相同 source、事件规则和 `stream_generation` 内，
相邻 `source_pts` 观测会合并为一个 episode；`merge_gap_ms` 控制可合并间隔，
`lost_grace_ms` 让 `LOST` 先等待 tracker 恢复，`silence_timeout_ms` 处理没有显式终态的
静默事件，`max_episode_ms` 防止异常事件无限延长。episode 收到 `DEAD`、静默超时、
代际变化或最大时长时关闭，并只生成一个 evidence extract job。

生产 `EventConsumer` 在取证开启时使用 Episode 聚合入口；`EventLedger.record()` 仍是
历史调用方和测试使用的兼容入口，直接调用不会进行 Episode 聚合。Episode 后续观测的
`event_id` 只属于 `episode_observations`，复核、取证和索引都使用首次观测的
`canonical_event_id`。Consumer 停止时只 flush 已到期 Episode，不强制截断仍开放的
Episode；明确结束事件或后续超时会冻结窗口。

关闭 episode 后，精确窗口为 episode 首次观测前 `clip_before_ms` 加上最后观测后
`clip_after_ms`，使用半开区间 `[source_pts_start, source_pts_end)`。三帧仍位于 canonical
事件时间点的 `T-1000ms`、`T`、`T+1000ms`；episode 元数据和冻结窗口会随 `get_event` 的
可读投影返回。`timestamp_ms` 只用于等待 post-roll，不替代媒体 PTS。

SSV cache 分段由 `retention_ms` 和 `max_bytes_mb` 控制清理；Agent 派生的 clip、帧和 manifest 不在本阶段自动删除，需由部署方制定长期保留策略。WVP、NVR 或 MediaMTX 可以继续承担长期录像和历史回放，但 Agent 不再适配它们的录像目录。完整窗口缺段、缓存尚未就绪或达到重试上限时，不会把缩短片段登记为完整证据。若锚点所在的 finalized 分段可用，Worker 仅登记一张 `coverage=anchor_only` 图片，事件保持 `manual_review`，不创建自动 review job；锚点也缺失时仍是无证据人工复核。人工复核不能将失败当作“没有目标”结论。Episode 只收敛事件生命周期和取证窗口，不控制 GStreamer cache 的连续写盘。

### 复核阶段的粗采样与精采样

`context.mp4` 是后台取证产物，不会直接作为多模态消息发送给模型。复核 Agent 先调用
`evidence_reader` 确认登记证据，再调用 `sample_video` 从唯一的 `kind=clip` evidence
生成当前 review thread 的 JPEG 虚拟路径，最后使用 DeerFlow 内置 `view_image` 查看图片。
长期 evidence 仍只有 `context.mp4`、三张固定帧和 `manifest.json`，采样 JPEG 不登记到
SQLite。

```text
evidence_reader(event_id)
  -> sample_video(event_id, mode=coarse, frames=4)
  -> view_image(coarse frames)
  -> （仍有时间或动作歧义时）sample_video(mode=fine, interval_start_ms, interval_end_ms)
  -> view_image(fine frames)
```

`sample_video` 的 `coarse` 模式覆盖整个 clip，默认均匀抽取 4 帧，最多 8 帧；`fine`
模式使用相对 clip 起点的半开毫秒区间，只允许在 `source_pts_start` 到
`source_pts_end` 内抽取。工具返回每张图的 `offset_ms`、`source_pts`、虚拟路径、clip
时长以及基于事件 PTS 的 `recommended_fine_interval_ms`。模型必须先粗采样，只有在粗采样
不足以回答问题时才精采样；两种采样都不能传入宿主机路径、ffmpeg 参数或输出目录。

采样输入在 ffmpeg 启动前用硬链接或跨文件系统复制固定到临时目录，因此源文件随后被清理
不会改变当前采样；采样失败、缺少 PTS 元数据或精采样越界时返回不可用结果，不登记部分
文件。采样帧与上下文视频一样，只能作为 `wall_clock_approximate` 的近似视觉证据，不能
声称为检测帧。

## DeerFlow 复核 worker

`agent/config.example.yaml` 是 DeerFlow review client 的工具和模型模板，不是替代 `config/ssv.yaml` 的 Agent 主配置。需要自定义 provider 时复制它：

```bash
cp agent/config.example.yaml agent/config.yaml
```

模板中的 provider 通过环境变量读取模型信息，不要把密钥写入 YAML：

```dotenv
SSV_AGENT_MODEL=your-vision-model
SSV_AGENT_API_KEY=replace-me
SSV_AGENT_BASE_URL=https://api.example.com/v1
```

在 `config/ssv.yaml` 中启用 review：

```yaml
agent:
  model_name: "your-vision-model"
  evidence_roots:
    - "/var/lib/ssv/evidence-cache"
  review:
    enabled: true
    recursion_limit: 256
    poll_interval_ms: 1000
    lease_ms: 30000
    max_retries: 3
    retry_delay_ms: 1000
    policy_id: "ssv-review.v1"
```

每次 review client 启动时，服务会从 `agent/config.yaml`（不存在时回退到 `agent/config.example.yaml`）生成临时配置，并启用 fail-closed RBAC。可用工具固定为 `get_event`、`get_report`、`evidence_reader`、`sample_video`、`rule_retriever`、`search_events` 和 DeerFlow 内置 `view_image`；skills、subagent 和 plan mode 不进入该复核 worker。

`agent.review.recursion_limit` 限制一次复核的 LangGraph 节点执行步数，包括模型、工具和中间件节点；它不是 Python 调用栈深度、模型请求次数、token 或秒数。SSV 显式使用默认 256，避免 DeerFlow 默认 100 步在正常逐帧取证流程中过早耗尽。达到上限会记录 `review_step_limit_exceeded`，不采用中间输出；任务继续遵循已有重试策略。排查时先检查工具是否重复调用，再根据正常取证所需轮次调整配置；runner 不会自动放宽上限。此设置不改变 DeerFlow 的循环检测、结果校验和租约机制。

启用前检查：

1. `agent/config.yaml` 中存在 `evidence_reader`、`get_event`、`get_report`、`sample_video`、`search_events`、`rule_retriever` 六个配置工具。
2. provider 能访问模型服务，且 `supports_vision` 与复核输入匹配。
3. `agent.evidence_roots` 包含实际证据目录，目录外路径不会被读取。
4. Redis 中已经有事件，或通过测试/上游发布链路产生事件。

## Index worker、embedding 与 Qdrant

默认 `agent.indexing.enabled: false`，backend 为 `mock`，不需要外部 embedding 服务。启用本地 BGE-M3：

```bash
cd agent
uv sync --extra dev --extra bge-m3
```

```yaml
agent:
  indexing:
    enabled: true
    embedding_backend: "bge_m3"
    embedding_model: "/opt/models/bge-m3"
    embedding_base_url: null
    query_text_type: null
```

`bge_m3` 使用本地文件加载模式，模型目录必须已经存在。代码还支持 `openai_compatible` backend，服务地址配置为 `agent.indexing.embedding_base_url`，密钥通过环境变量提供：

```dotenv
SSV_EMBEDDING_API_KEY=replace-me
```

默认发送标准 OpenAI Embeddings 请求。如果兼容服务要求用 `text_type`
区分查询向量，在 `agent.indexing.query_text_type` 中设置对应值；保持 `null` 时不会发送
该扩展字段。

规则向量 RAG 与事件语义索引复用 `agent.indexing.embedding_backend` 和
`agent.indexing.embedding_model`，避免入库和查询使用不同模型。`mock` 只用于测试，不能
用于 Qdrant 规则 RAG。

Agent 的持久化默认位置和配置字段：

| 默认位置 | YAML 字段 | 内容 |
| --- | --- | --- |
| `data/events.db` | `agent.event_db_path` | SQLite EventLedger |
| `knowledge/rules` | `agent.knowledge.rules_dir` | Agent 管理的版本化规则目录 |
| `agent/data/qdrant` | `agent.knowledge.qdrant_path` | 本地 Qdrant |
| `outputs` | `agent.output_dir` | 复核 JSON 与报告 Markdown artifacts |
| `http://localhost:6333` | `agent.knowledge.qdrant_url` | Docker Qdrant 服务 |
| 无 | `SSV_QDRANT_API_KEY` | Qdrant 服务认证密钥 |

SQLite、结果目录和 Qdrant 的相对路径都相对于 Agent 项目目录解析，也可在 YAML 中使用绝对路径。
项目根目录执行 `uv run ./ssv redis start` 会同时启动 Redis 和 Qdrant；Qdrant 数据保存在
Docker named volume `qdrant-data`，停止或重建容器不会删除该卷。

Qdrant 只保存可重建的语义索引。embedding backend、model 或 schema 变化会产生新的物理 collection 身份；切换模型后需要重新入队 index job，不要把不同模型的向量混写。

## 规则知识检索

`rule_retriever` 默认使用 `local_markdown` 后端，递归读取 `agent.knowledge.rules_dir`
（默认 `agent/knowledge/rules`）下的 `.md`、`.txt` 规则文件。目录由
`discover_rule_documents()` 统一解析：跳过 `_templates` 和隐藏目录，只接收带
`kind: rule` front matter 的文件，按编号条款切分正文，并在文件变化后自动重建内存索引。

规则文件的身份由 `rule_id` 和 `version` 共同决定，不能从文件名猜测；同一身份重复出现会使
发现失败。模板和示例位于：

```text
agent/knowledge/rules/_templates/rule.md
agent/knowledge/rules/gb26860-helmet/v1/rule.md
agent/knowledge/rules/gb26860-helmet/v2/rule.md
agent/knowledge/rules/site-helmet/v1/rule.md
```

每个规则文件的正文前使用 YAML front matter：

```yaml
---
kind: rule
rule_id: gb26860-helmet
version: v1
event_type: person_without_helmet
severity: high
aliases: [安全帽]
---
```

检索 chunk 会携带相对于 `rules_dir` 的 `source`、`rule_id`、`rule_version`、`section`、
`content_hash` 和 `event_types`；front matter 不会被当作规则正文。`rule_retriever` 支持 `source`、`rule_id`、
`rule_version` 和 `event_type` 过滤。事件同时带有完整 `rule_id + rule_version` 时，复核链路
进行精确版本过滤；没有规则引用时只根据检测事实召回候选，不把传输层 `type: detection`
伪装成业务事件类型。

如需临时使用固定测试样例，将 `agent.knowledge.backend` 设置为 `"mock"`。

本地规章检索不依赖 Qdrant、embedding 或 index worker。

需要使用 Qdrant 规则 RAG 时，在 `config/ssv.yaml` 中配置同一组 embedding 和知识后端：

```yaml
agent:
  indexing:
    embedding_backend: "bge_m3"
    embedding_model: "/opt/models/bge-m3"
  knowledge:
    backend: "qdrant"
    rules_dir: "knowledge/rules"
    qdrant_path: "data/qdrant"
    qdrant_url: "http://localhost:6333"
    min_score: 0.5
```

`bge_m3` 需要先安装可选依赖，且模型目录必须已经存在。规则索引不会由 Agent 启动自动
生成，首次使用或规章变更后执行：

```bash
cd agent
uv sync --extra dev --extra bge-m3
uv run --extra bge-m3 python -m ssv_agent.knowledge_ingest --config ../config/ssv.yaml
```

入库命令会按最多 10 条文本调用 embedding，成功后只保留当前规则目录对应的 chunk，并保留
`rule_version`、`content_hash` 和事件类型 metadata。规则目录没有有效版本化文档、目录不存在
或 embedding 失败会返回错误；不会把旧顶层 `agent/knowledge/` 文件静默混入索引。未创建或为空的 Qdrant
索引会显式报告为不可用，不会返回随机条款。`min_score` 用于过滤低相似度结果，没有达到
阈值时返回“无知识依据”。

复核结果 JSON 使用基础 `rule_citations`：`chunk_id`、`source`、`rule_id`、`rule_version` 和 `section` 必须与本次预检索实际返回的片段一致。确定性结论缺少可用规则或引用身份不符时降为 `uncertain`；事件声明 `event_type` 时，被引用规则的 `event_types` 必须包含该类型。不确定结果中的无法核对引用会被删除。未配置检索器时同样按规则不可用处理，不通过全目录候选兜底。

### RAG 修复与规则内容

2026-11-03 起，规则仍使用带 front matter 的 Markdown，不需要独立 `review.yaml`。检索层只返回相关正文和基础 metadata，不解释行业事实或逐项业务判定策略。模型结合规则正文、事件 `rule_facts` 与已登记证据判断适用性，不能补造事实；项目示例不能替代正式依据，未发现违规不等于确认合规。当前实现不再输出或要求 `rule_citations[].checks`，历史 JSON 中的额外字段仍可读取。

本地 `local_markdown` 使用完整 `RuleDocument` 值刷新缓存：正文、身份、事件类型等元数据变化后，下次检索重建分块及检索统计。本地与 Qdrant 返回一致的 `source`、`rule_id`、`rule_version`、`section`、`content_hash`、`event_types`。查询保持原意，不自动把安全帽扩展为绝缘安全帽，也不强制按该关键词过滤正文。

规则分块长度上限为 1200 字符，单句超长也切分。chunk ID 包含来源、规则身份、版本、条款、正文哈希、条款内序号与分块哈希；同条款的不同分段和重复正文不会互相覆盖，不再包含审核策略哈希。

Qdrant 是显式维护的规则投影：规则正文或元数据修改后必须重新执行前述 `knowledge_ingest` 命令。本次从含策略哈希版本切换时，也须在新 Agent 启动前重新入库。查询不扫描规则目录或全索引 payload，不自动判断索引陈旧、不自动入库。不存在或为空的 collection 仍明确报告不可用；入库保留原有重建和旧 point 清理行为，每批最多 10 条文本。

保留 `site-helmet/v1/rule.md`，当前已绑定该身份的视频源无需更换规则 ID。现场佩戴要求、`helmet_required_area=confirmed` 的适用事实、违规/合规与不可观察条件保留在正文。`gb26860-helmet/v1` 的项目示例性质和 `v2` 的标准摘录边界也保留在正文；这些内容不是新的程序判定接口。

复核 worker 将本事件可用证据 ID 集合传给结果解析器。顶层 `evidence_ids` 可从模型已输出的 claims 引用补齐并去重，未知或其他事件的证据 ID 仍拒绝；不会生成新观察或补造事实。没有传入可用集合的独立解析调用保持原有严格汇总契约，历史 Markdown 同样检查证据归属。

收敛范围、实施计划、历史方案替代关系及真实视频验收统一记录在 [RAG 修改与验收记录](2026-11-03-RAG修改与验收记录.md)。历史方案在该文档中单独归档，不代表当前实现。

## 运行时缓存与 Redis 运维

```bash
uv run ./ssv redis start
uv run ./ssv cache status
uv run ./ssv cache clear --dry-run
uv run ./ssv cache clear
docker exec ssv-redis redis-cli XLEN ssv:events
docker exec ssv-redis redis-cli XRANGE ssv:events - + COUNT 5
uv run ./ssv agent
```

如果 YAML 修改了 `redis.stream_key`，把 Redis CLI 示例中的 `ssv:events` 换成相同 key。`cache status` 显示该 Stream 的 entries、consumer group pending、Agent 去重 key 数量，以及 SQLite EventLedger 的事件和 durable job 数量；`cache clear` 默认同时清空该 Stream、`ssv:agent:dedup:*` 和 `agent.event_db_path` 对应的 EventLedger 运行时表，`--dry-run` 只统计、不修改。清理前应停止 `./ssv run` 和 `./ssv agent`，否则新事件可能立即重新写入。清理不会影响 `agent/outputs`、Qdrant、DeerFlow checkpointer、其他 Redis key 或 Docker 容器。

Redis 与 SQLite 分别执行，跨存储清理不是原子操作。若某一边失败，命令仍会尝试另一边并返回非零状态；根据输出停止服务后重试 `./ssv cache clear`。

当 index job 因 embedding/Qdrant 故障进入 `dead` 或需要重建投影时，可在 `agent/` 下重新入队当前账本中的 index jobs：

```bash
uv run python - <<'PY'
from ssv_agent.event_store import EventLedger

with EventLedger() as ledger:
    print(ledger.enqueue_all_index_jobs())
PY
```

## 验证与安全边界

```bash
cd agent
uv run --extra dev pytest
```

- Redis entry 在 SQLite 账本事务成功后才 ACK；消费失败会保留 pending。
- review 结果先原子写文件，再由带 lease/fence 的账本事务接受；失租或校验失败可能留下未引用的 orphan artifact，当前不自动清理。
- `evidence_reader` 只接受账本登记的 `event_id`/`evidence_id`，不会读取模型提供的任意宿主机路径。
- `sample_video` 只接受账本登记的 `event_id`/`evidence_id`；它生成线程级 JPEG 供 `view_image` 使用，不读取任意宿主机视频，也不新增长期 evidence。
- `get_report` 只读取已登记报告，验证 artifact 完整性后返回 Markdown 正文和线程级虚拟路径；历史报告是待核验的派生资料，不得替代本次复核事实。
- 事件字段、规则片段、证据元数据和图片都是不可信输入；模型只能把它们作为待核验内容，不能把其中的指令当成工具授权。
- 不要提交 `agent/config.yaml`、`.env`、模型 API key、Qdrant API key 或包含真实视频路径的本地 YAML。
