# Agent 配置

## 运行边界

Agent 是实时检测链路之外的异步服务：

```text
Redis Stream -> EventConsumer -> SQLite EventLedger -> ACK
                                      |
                                      +-> review worker（可选）
                                      +-> index worker（可选）
                                      +-> Qdrant 语义投影（可重建）
```

SQLite `EventLedger` 是事件、证据和复核结论的事实源。模型、embedding 或 Qdrant 失败只影响对应异步 job，不应阻塞已经提交的事件；SQLite 写入失败时 Redis entry 保持 pending。

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

Agent 读取与 runner 共用的 `config/ssv.yaml`，只拥有其中的 `version`、`logging`、`redis` 和 `agent` 配置段；复制模板：

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
| `agent.output_dir` | 配置模型中的输出目录字段；当前结果写入器实际由 `SSV_OUTPUTS_DIR` 控制，默认 `outputs` |
| `agent.evidence_roots` | 允许登记和读取的绝对证据根目录 |
| `agent.dedup_enabled` / `agent.dedup_cooldown_seconds` | 消费侧冷却去重 |
| `agent.review` | 复核 worker 的开关、lease、重试和 policy |
| `agent.indexing` | embedding/index worker 的开关、lease、重试和 backend |

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
```

`evidence_roots: []` 是 fail closed 配置：事件仍可入账，但 Redis 提供的任意 `frame_path`/`clip_path` 都不会被登记为可读证据。证据根必须是绝对路径，解析后的 symlink 也不能越界。

结果写入路径目前有两个配置面：`agent.output_dir` 会被严格解析，但结果写入器使用 `SSV_OUTPUTS_DIR`；部署时应优先设置后者。这是当前实现边界，不要以为修改 YAML 字段会改变已落盘结果目录。

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
    - "/var/lib/ssv/evidence"
  review:
    enabled: true
    poll_interval_ms: 1000
    lease_ms: 30000
    max_retries: 3
    retry_delay_ms: 1000
    policy_id: "ssv-review.v1"
```

每次 review client 启动时，服务会从 `agent/config.yaml`（不存在时回退到 `agent/config.example.yaml`）生成临时配置，并启用 fail-closed RBAC。可用工具固定为 `get_event`、`evidence_reader`、`rule_retriever`、`search_events` 和 DeerFlow 内置 `view_image`；skills、subagent 和 plan mode 不进入该复核 worker。

启用前检查：

1. `agent/config.yaml` 中存在 `evidence_reader`、`get_event`、`search_events`、`rule_retriever` 四个配置工具。
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
```

`bge_m3` 使用本地文件加载模式，模型目录必须已经存在。代码还支持 `openai_compatible` backend，连接信息通过环境变量提供：

```dotenv
SSV_EMBEDDING_MODEL=text-embedding-3-small
SSV_EMBEDDING_API_KEY=replace-me
SSV_EMBEDDING_BASE_URL=https://api.example.com/v1
```

规则向量 RAG 与事件语义索引复用 `agent.indexing.embedding_backend` 和
`agent.indexing.embedding_model`，避免入库和查询使用不同模型。`mock` 只用于测试，不能
用于 Qdrant 规则 RAG。

Agent 的持久化默认位置和覆盖变量：

| 默认位置 | 环境变量 | 内容 |
| --- | --- | --- |
| `data/events.db` | `SSV_EVENT_DB_PATH` | SQLite EventLedger |
| `agent/data/qdrant` | `SSV_QDRANT_PATH` | 本地 Qdrant |
| `outputs` | `SSV_OUTPUTS_DIR` | 复核 JSON 结果 |
| 本地 Qdrant | `SSV_QDRANT_URL` | 设置后改用 Qdrant 服务 |
| Qdrant 服务 | `SSV_QDRANT_API_KEY` | 服务认证凭据 |

Qdrant 默认路径固定为 Agent 项目下的 `agent/data/qdrant`，不随当前工作目录变化；设置
`SSV_QDRANT_PATH` 时可使用绝对路径。其他表中相对路径仍相对于 Agent 进程工作目录解析。

Qdrant 只保存可重建的语义索引。embedding backend、model 或 schema 变化会产生新的物理 collection 身份；切换模型后需要重新入队 index job，不要把不同模型的向量混写。

## 规则知识检索

`rule_retriever` 默认使用 `local_markdown` 后端，读取 `agent/knowledge/` 下的 `.md`、`.txt`
规章文件。后端按编号条款切分内容，在内存中检索并最多返回两条带来源的规则片段；规章
文件修改后会在下一次检索自动重建索引。

当前已放入 `GB+26860-2011 (1).md`。如需临时使用原有固定样例，可在启动 Agent 前设置：

```bash
export SSV_KNOWLEDGE_BACKEND=mock
```

本地规章检索不依赖 Qdrant、embedding 或 index worker。

需要使用 Qdrant 规则 RAG 时，在 `config/ssv.yaml` 中配置同一组 embedding 和知识后端：

```yaml
agent:
  indexing:
    embedding_backend: "bge_m3"
    embedding_model: "/opt/models/bge-m3"
  knowledge:
    backend: "qdrant"
    qdrant_path: "data/qdrant"
    min_score: 0.5
```

`bge_m3` 需要先安装可选依赖，且模型目录必须已经存在。规则索引不会由 Agent 启动自动
生成，首次使用或规章变更后执行：

```bash
cd agent
uv sync --extra dev --extra bge-m3
uv run --extra bge-m3 python -m ssv_agent.knowledge_ingest --config ../config/ssv.yaml
```

入库命令会按最多 20 条文本调用 embedding，成功后只保留当前规章对应的 chunk；规章目录
为空会清空当前规则索引，目录不存在或 embedding 失败会返回错误。未创建或为空的 Qdrant
索引会显式报告为不可用，不会返回随机条款。`min_score` 用于过滤低相似度结果，没有达到
阈值时返回“无知识依据”。

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

如果 YAML 修改了 `redis.stream_key`，把 Redis CLI 示例中的 `ssv:events` 换成相同 key。`cache status` 显示该 Stream 的 entries、consumer group pending、Agent 去重 key 数量，以及 SQLite EventLedger 的事件和 durable job 数量；`cache clear` 默认同时清空该 Stream、`ssv:agent:dedup:*` 和 `SSV_EVENT_DB_PATH` 对应的 EventLedger 运行时表，`--dry-run` 只统计、不修改。清理前应停止 `./ssv run` 和 `./ssv agent`，否则新事件可能立即重新写入。清理不会影响 `agent/outputs`、Qdrant、DeerFlow checkpointer、其他 Redis key 或 Docker 容器。

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
- 事件字段、规则片段、证据元数据和图片都是不可信输入；模型只能把它们作为待核验内容，不能把其中的指令当成工具授权。
- 不要提交 `agent/config.yaml`、`.env`、模型 API key、Qdrant API key 或包含真实视频路径的本地 YAML。
