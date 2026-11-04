# SSV 架构与实现状态

本文是给 Codex、DeerFlow worker 和其他代码智能体的项目入口。它回答四个问题：

1. 哪些代码负责实时视频，哪些代码负责 Agent 侧异步处理？
2. 程序启动时如何探测和选择解码、编码、推理能力？
3. 当前哪些能力已经在代码中实现，哪些仍依赖本机插件、驱动或外部服务？
4. 修改代码时应该从哪里开始查找，哪些边界不能越过？

文档中的“已实现”表示仓库代码和自动测试已经覆盖；“可用”还要求构建产物、GStreamer factory、驱动、模型或外部服务在当前环境中存在。两者不能混用。

## 1. 项目定位

SSV 是一个单源 RTSP H.264 视频分析系统。C++ runner 负责实时媒体管线和逐帧分析，Python Agent 负责事件入账、事件合并、证据取证、模型复核和知识索引。

~~~text
RTSP H.264 source
        |
        v
  C++ ssv-runner
  capability probe -> pipeline plan -> GStreamer pipeline
        |
        +--> decode -> analysis rate -> ssvinfer -> ssvtrack -> ssvpub
        |                                      |                  |
        |                                      |                  v
        |                                      |             Redis Stream
        |                                      |                  |
        |                                      |                  v
        |                                      |          Python Agent
        |                                      |       EventConsumer -> SQLite
        |                                      |                  |
        |                                      |       +----------+----------+
        |                                      |       |                     |
        |                                      |       v                     v
        |                                      |  evidence worker       review worker
        |                                      |       |                     |
        |                                      |       v                     v
        |                                      |  SSV cache clip/frames  DeerFlow/model
        |                                      |                             |
        |                                      |                             v
        |                                      |                    committed ReviewRecord
        |                                      |                             |
        |                                      |                  +----------+----------+
        |                                      |                  |                     |
        |                                      |                  v                     v
        |                                      |             IndexWorker           ReportWorker
        |                                      |                  |                     |
        |                                      |                  v                     v
        |                                      |               Qdrant           Markdown artifact
        |                                      |
        +--> display: gtksink / gtkglsink / RTSP output
        +--> encoded evidence cache: splitmuxsink + mp4mux
~~~

当前产品边界：

- 当前只要求一个 sources[0]，输入为 RTSP H.264 8-bit。
- SSV 自己维护短时循环证据缓存；它不是长期录像系统。
- WVP、NVR 和 MediaMTX 可以继续负责信令、长期录像或历史回放，但 Agent 不再依赖 MediaMTX provider 读取证据。
- Python Agent 不负责逐帧解码、推理、跟踪或硬件选择。

## 2. 所有权边界

| 领域 | 所有者 | 主要代码 | 不应由谁接管 |
| --- | --- | --- | --- |
| 配置解析 | C++ runner / Python Agent 各自解析同一 YAML 的所属部分 | gst/ssv-common/config/ssv_config.*、agent/src/ssv_agent/config.py | Agent 不应读取 runner 私有字段并重新解释硬件行为 |
| 硬件能力探测 | C++ runner 启动阶段 | runner/pipeline/ssv_hardware_capabilities.* | GStreamer element、ORT provider 不应在每帧或 Agent 侧重复探测 |
| 编解码路径计划 | C++ runner | runner/pipeline/ssv_pipeline_plan.* | Agent 不应决定 avdec_h264、VAAPI、NVDEC 或编码器 |
| 推理服务 | C++ runner-owned service | gst/ssv-infer/core/、gst/ssv-infer/backends/ | GStreamer element 不应各自创建一套推理 runtime |
| GStreamer 拓扑 | C++ runner | runner/pipeline/ssv_pipeline_builder.* | Agent 不应拼接 GStreamer pipeline |
| 检测/跟踪/事件发布 | GStreamer 插件链 | gst/ssv-infer、gst/ssv-track、gst/ssv-pub | Agent 不应从稀疏事件重新推断框、轨迹或媒体 PTS |
| 事件可靠入账 | Python Agent | event_consumer.py、event_store/ledger.py | review/index worker 不应绕过 SQLite 直接修改事实 |
| episode 合并与取证窗口 | Python Agent，但依据上游 event lifecycle 和 PTS | event_episode.py、event_store/ledger.py | 下游模型不应自行合并事件或修改窗口 |
| 短时视频留存 | C++ GStreamer evidence branch | runner/pipeline/ssv_evidence_cache.* | Agent 只读取 finalized cache，不负责连续录制 |
| 视觉复核 | Python ReviewWorker + 受控 DeerFlow client | workers.py、review_runtime.py、runner.py | Redis consumer 线程不应调用模型 |
| 分析报告 | Python ReportWorker + 确定性 renderer | workers.py、report.py、event_store/ledger.py | 报告 worker 不应再次判断或调用模型 |
| 规则知识 | Agent-owned rule directory / 可选 Qdrant projection | knowledge/catalog.py、knowledge/backends/ | 规则正文不应硬编码到 C++ 或固定 prompt |

## 3. C++ 启动流程

入口是 runner/main.cpp。它只负责命令行、配置加载、异常转换和最终结构化 fatal event；运行时编排在 SsvRunner。

~~~text
runner/main.cpp
  |
  v
SsvRunner::Impl::run()
  |
  +-- SsvRunAttemptFactory::prepare_run(config)
  |     +-- gst_init_check()
  |     +-- SsvSystemHardwareCapabilitiesProbe::detect()
  |     +-- SsvCapabilitySnapshot
  |     +-- GTK 初始化（仅本地窗口 backend）
  |
  +-- 每个 run attempt:
  |     +-- fallback_state.derive_effective_config()
  |     +-- SsvPipelinePlan::resolve(config, snapshot, options)
  |     +-- SsvSystemRunAttemptFactory::create(config, plan, context)
  |     |     +-- create runner-owned SsvInferenceService
  |     |     +-- SsvPipelineBuilder::build()
  |     |     +-- 创建本地 display window（需要时）
  |     +-- SsvRunAttempt::run()
  |     +-- 失败时按 fallback policy 重建 attempt
  |
  +-- 成功或不可恢复失败后返回 SsvRunnerResult
~~~

### 3.1 能力快照

SsvSystemHardwareCapabilitiesProbe::detect() 在 GStreamer 初始化后只做一次探测，记录：

- GStreamer element factory 名称，例如 vah264dec、nvh264dec、gtksink、rtspclientsink。
- ONNX Runtime 是否编译进来，以及当前 runtime 实际可用的 SsvProvider。
- TensorRT engine backend 是否编译可用。

SsvCapabilitySnapshot 被放入 SsvPipelinePlan，后续 plan、builder 和 inference service 只消费这个快照。新增硬件能力时，先修改 probe/snapshot，再修改 plan；不要在 builder 中偷偷重新探测，也不要让 Agent 通过 shell 命令替代 C++ 的能力事实。

### 3.2 计划对象

SsvPipelinePlan 是启动阶段的决策结果，主要包含：

- SsvInferencePlan：推理 backend 和启动时可用 ORT provider 列表。
- SsvCodecPlan：decoder、encoder、memory contract、路径类型和 fallback 决策。
- display_backend：GtkSink、GtkGlSink 或 RtspClientSink。
- expected_caps：解码、显示、编码和推理边界的像素格式/内存类型。
- capability_snapshot：本次 run 使用的能力快照。

plan 只做能力和配置决策；builder 才创建 GStreamer element、设置 property、link pad 和安装 contract probe。

## 4. GStreamer 实时拓扑

### 4.1 输入和证据分支

~~~text
rtspsrc -> capsfilter(application/x-rtp,H264) -> rtph264depay -> h264parse
                                                        |
                                                 encoded-tee（需要时）
                                                   |             |
                                                   |             +--> queue -> splitmuxsink
                                                   |                         +-> mp4mux
                                                   |
                                                   +--> decoder -> decode-memory-caps
                                                                    -> clocksync
                                                                    -> decoded-tee
~~~

encoded branch 始终持续写短时证据分段。分段和 sidecar 完整关闭后，Agent 才能读取。证据 cache 不依赖是否开启 display 或 inference。

### 4.2 分析分支

~~~text
decoded-tee
    |
    +--> queue -> videorate ->
          VAAPI: vapostproc
          NVDEC: cudadownload
          Software: videoscale -> videoconvert
        -> capsfilter(host model input)
        -> ssvinfer
        -> ssvtrack（tracking.enabled=true）
        -> ssvpub（tracking.enabled=true）
        -> fakesink
~~~

分析分支是 drop-only：inference.analysis_fps 限制处理帧率，不通过复制旧帧填满目标帧率。ssvinfer 接收 runner-owned SsvInferenceService，插件本身不拥有 runtime 生命周期。

### 4.3 显示和 RTSP 输出

| 配置 | 实际路径 | 说明 |
| --- | --- | --- |
| display.backend: auto + VAAPI 解码 | ... -> gtkglsink | 需要 gtkglsink、glupload、glcolorconvert，且 decoder 必须导出 DMABUF |
| display.backend: auto + 软件/NVDEC 解码 | ... -> videoconvert -> gtksink | auto 不会为了 GTK 显示强行重新选择 decoder |
| display.backend: gtksink | ... -> videoconvert -> gtksink | 系统内存显示路径 |
| display.backend: gtkglsink | DMABUF -> GLMemory -> gtkglsink | 显式配置不满足能力时直接失败，不静默改成 gtksink |
| display.backend: rtsp + encoded_passthrough: true | encoded tee -> rtph264pay -> rtspclientsink | 不解码、不烧录框；若启用推理仍会额外建立 decoded analysis branch |
| display.backend: rtsp + 普通编码 | decoded tee -> overlay/convert -> encoder -> h264parse -> rtph264pay -> rtspclientsink | 接收端只有在 burn_in_overlay: true 且 ssvoverlay 可用时看到像素框 |

RTSP 输出不是 RTSP server。rtspclientsink 是发布客户端，目标地址必须指向已经存在的 RTSP server，例如 WVP、MediaMTX 或其他服务。当前项目不负责在本机创建 RTSP server。

## 5. 硬件选择、编解码和 fallback

### 5.1 解码选择

sources[0].decode.mode 支持 auto、vaapi、nvdec、software；device 支持 auto、DRM device 或 CUDA device。

| 请求 | 选择逻辑 | 失败语义 |
| --- | --- | --- |
| auto + device:auto | VAAPI decoder/postproc -> NVDEC -> avdec_h264 | 可在启动计划中记录降级；运行失败时按计划允许 fallback |
| auto + DRM selector | 需要对应 vah264dec 和 vapostproc，否则软件解码 | selector 保留，失败原因写入 plan |
| auto + CUDA selector | 需要对应 nvh264dec，否则软件解码 | selector 保留，失败原因写入 plan |
| 显式 vaapi | 只允许 VAAPI | 缺 factory、device 或 contract 时失败，不静默改软件 |
| 显式 nvdec | 只允许 NVDEC | 缺 factory、device 或 contract 时失败，不静默改软件 |
| 显式 software | 使用 avdec_h264 | 不使用硬件 decoder |

当前代码的 auto 选择是能力快照驱动的，不是“检测到有 GPU 就认为 GStreamer 能用”。必须同时检查实际 factory 和 memory contract。

### 5.2 编码选择

编码只在 RTSP 非 passthrough 输出需要时参与计划：

1. 硬件 decoder 为 VAAPI 时，优先寻找同一 VAAPI backend/device 的 vah264enc。
2. 硬件 decoder 为 NVDEC 时，优先寻找同一 CUDA backend/device 的 nvh264enc。
3. 找不到匹配硬件 encoder，或 CPU overlay 要求 SystemMemory 时，选择软件编码器，顺序为 openh264enc、x264enc、avenc_h264。
4. 硬件解码 + 软件编码叫 SsvCodecPath::Mixed；软解码 + 软件编码叫 Software。

因此编码不是独立重新估算一套硬件，而是以已解析的 decoder backend/device 为基础建立 pair。只有当 pair 不成立或 overlay 改变了 memory contract 时才进入 mixed path。

### 5.3 推理 backend 和 provider

推理 backend 与视频编解码 backend 解耦：

- SsvInferenceBackend::OnnxRuntime：原始 ONNX 模型，provider chain 由构建 profile、配置和启动快照共同决定。
- SsvInferenceBackend::TensorRtEngine：TensorRT engine manifest/runtime 路径。
- ONNX Runtime provider auto chain：
  - cpu: CPU
  - nvidia: TensorRT -> CUDA -> CPU
  - intel: OpenVINO -> CPU
  - amd: MIGraphX -> CPU
- 显式 provider chain 不能重复，CPU 如果出现必须是最后一项；显式模式缺 provider 时失败，auto 模式允许按候选链回退。

SsvRunAttemptFactory::create() 把 plan.inference.available_providers 传给 ssv_inference_service_create()。推理服务在一次 attempt 内创建并持有 session；不要让每个 ssvinfer element、每帧或 Agent worker 创建 runtime。

### 5.4 fallback 顺序

~~~text
能力快照
  |
  v
SsvPipelinePlan
  |
  +-- auto decoder 不可用 -> 计划内硬件候选降级 -> software decoder
  |
  +-- RTSP hardware pair 建链失败 -> Mixed（硬件解码 + 软件编码）
  |
  +-- Mixed 仍失败且 decode.mode=auto -> Software decoder + software encoder
  |
  +-- auto display 的 gtkglsink 条件不满足 -> gtksink
  |
  +-- 显式 backend/provider 失败 -> 返回错误，不静默跨模式降级
~~~

fallback 是一次 run attempt 的重建，不是运行中替换正在工作的 GStreamer element。SsvRunFallbackState 防止同一种 fallback 无限重试，并为每次降级发出结构化事件。

## 6. Python Agent 运行流程

### 6.1 进程启动顺序

`ssv agent` 最终由 agent/src/ssv_agent/cli.py 调用 `load_config()` 加载严格的 SSV 配置，`AgentService.start()` 按以下顺序启动：

配置边界必须保持清楚：

- `config/ssv.yaml` 是 runner 和 Agent 共用的 SSV 配置入口。Agent 的搜索顺序是显式路径、`SSV_CONFIG_PATH`、`ssv.yaml`、`config/ssv.yaml`，并要求 `version: "2.0"`。
- C++ runner 解释完整的 `sources.decode`、`display`、`inference` 和 `tracking`；Agent 只保留 `version`、`logging`、`redis`、`evidence_cache`、`agent` 以及 `sources` 的 `id`/`uri`，不会重新解释硬件字段。
- `agent/config.yaml` 属于 DeerFlow，不是 SSV runtime 配置。只有启用 ReviewWorker 时，Agent 才会基于它创建受限的临时 DeerFlow 配置和 client。

~~~text
AgentService.start()
  |
  +--> runtime environment
  +--> RecordingEvidenceWorker（enabled 时）
  +--> ReviewWorker（enabled 时，创建隔离 DeerFlow config/client）
  +--> IndexWorker（enabled 时，创建 embedding provider/Qdrant factory）
  +--> ReportWorker（enabled 时，确定性生成 Markdown，不创建 DeerFlow client）
  +--> EventConsumer thread
~~~

worker 先启动、consumer 后启动，避免 Redis ingress 已进入账本而对应的异步任务没有消费者。ReviewWorker 完成后，账本在同一事务中创建 index 与 report job；IndexWorker 和 ReportWorker 是并行派生任务。报告 worker 不增加 subagent 权限。停止时先停止领取新 ingress/job，等待线程退出，再关闭 Redis、DeerFlow 和临时配置资源。

### 6.2 Redis ingress 与 SQLite 权威账本

~~~text
Redis Stream
  -> EventConsumer
     -> JSON / ReviewContext 校验
     -> EventDeduper（可选）
     -> EventEpisodeAggregator（recording_evidence 开启时）
     -> EventLedger SQLite transaction
        +-> EventCase / detections / EvidenceRef
        +-> review job
        +-> index job
        +-> evidence_extract job（episode 关闭时）
  -> SQLite commit 成功后 XACK

ReviewWorker 成功复核后，`complete_review_job()` 在同一 SQLite 事务中追加 ReviewRecord，并创建对应 revision 的 index/report job；两者之后并行处理。
~~~

关键语义：

- malformed event、无效事件和重复事件按代码策略 ACK；账本写入失败不 ACK，Redis entry 保持 pending。
- EventLedger 是事件、证据、复核结论和 durable job 的事实源。
- Qdrant 是可重建投影，不是事件事实源。
- EventDeduper 是入口负载保护；它不能代替 tracker 的目标关联，也不能把 track_id=-1 当作稳定身份。

### 6.3 Episode 与精确证据窗口

开启 agent.recording_evidence 后，连续观测不再每条都创建证据任务。EventEpisodeAggregator 按 source、规则身份、stream_generation 和 source_pts 管理 episode：

- merge_gap_ms：相邻 PTS 观测可合并的最大间隔。
- lost_grace_ms：tracker 报 LOST 后等待恢复的时间。
- silence_timeout_ms：没有新观测时的墙钟关闭条件。
- max_episode_ms：防止异常 episode 无限增长。
- clip_before_ms / clip_after_ms：关闭时冻结证据窗口。

冻结窗口是半开区间 [source_pts_start, source_pts_end)。timestamp_ms 只用于等待 post-roll，不替代媒体 PTS。episode 关闭后生成一个 evidence_extract job，由 RecordingEvidenceWorker 调用 SsvCacheEvidenceExtractor 读取 finalized SSV cache。完整窗口产出 clip、三帧和 manifest；缓存窗口在重试耗尽后仍不完整时，若同代际有效分段覆盖事件锚点，取证器只产出一张锚点附近图片和 manifest，账本标记 `coverage=anchor_only`，事件进入 `manual_review` 且不创建自动 review。

### 6.4 Durable workers

所有 worker 共用 _PollingWorker 的轮询骨架和 EventLedger lease：

| worker | 输入 | 外部副作用 | 成功提交点 |
| --- | --- | --- | --- |
| RecordingEvidenceWorker | evidence_extract job | 读取 SSV cache，原子生成完整 clip/帧或锚点单图及 manifest | complete_evidence_extract_job / complete_partial_evidence_extract_job |
| ReviewWorker | review job | 读取案件和规则，调用受控 DeerFlow/model，写 review JSON | complete_review_job |
| IndexWorker | index job | embedding 后写 Qdrant 事件索引 | Qdrant 写入并完成 job |
| ReportWorker | report job | 读取指定 revision 的 ReviewRecord，确定性渲染并写 Markdown | complete_report_job（lease fence 内登记 ReportRecord 并完成 job） |

job 具有 pending -> processing -> completed/dead 状态、lease、heartbeat、attempts 和 retry delay。取证 Worker 的实际 retry delay 不小于 `evidence_cache.segment_duration_ms`。失去 lease 的 worker 不能继续提交结果；模型、embedding、Qdrant、报告写入或证据缺失不会回滚已经提交的 EventCase 或 ReviewRecord。

review JSON 是复核结果的结构化 artifact；SQLite `reviews` 保存不可变复核历史，`reports` 保存成功报告的模板版本、hash 和 artifact 路径。Markdown 报告是派生产物，不是新的事实源；报告失败只影响 report job，不改变 review/index 状态。报告正文由固定模板生成，不调用 LLM，并绑定 job 指定的历史 revision。`get_event` 只投影已登记报告的安全元数据；`get_report` 校验 artifact 完整性后返回 Markdown 正文和当前 DeerFlow thread outputs 的虚拟路径，证据仅以 `event_id/evidence_id` 引用。它不是 HTTP 下载服务，也不生成 PDF。

## 7. 规则与 Agent 工具

### 7.1 规则目录是事实入口

默认规则目录是 agent/knowledge/rules，由 discover_rule_documents() 递归发现：

- 仅处理 .md / .txt。
- 跳过 _templates 和隐藏目录。
- 文件必须有 kind: rule front matter。
- rule_id + version 是稳定身份，重复身份直接报错。
- 正文按条款切分，检索结果携带 source、rule_id、rule_version、section、content_hash、event_types。

默认 local_markdown backend 在内存中做本地词法检索，按完整 RuleDocument 值比较 fingerprint，正文或元数据变化后重建分块及统计。它不依赖 Qdrant，也不需要 embedding。qdrant backend 是可选投影路径，规则入库必须显式执行 knowledge_ingest，不会因为 Agent 启动自动把任意 Markdown 写入向量库。

2026-11-03 的 RAG 收敛保留分块唯一身份、长句长度约束、缓存刷新和两种后端的基础 metadata 一致性。规则仍由 Markdown 保存身份、原文、适用说明和证据边界，不加载独立审核策略；没有规则引擎、checks 输出契约或全目录候选 provider。正文内容由模型结合登记事实和证据判断，通用代码不解释业务前提。

2026-11-04 合并后按用户要求采用远端 ReviewWorker：配置检索器时，使用本次预检索结果校验引用身份，不确定结果也清理不存在或身份不符的引用；没有检索器时传入 `rule_context=None`，跳过规则引用校验。worker 不向校验器传入事件上下文，也不向结果解析器传入可用证据集合，因此当前调用不执行事件类型约束或证据汇总补齐，模型必须在顶层 `evidence_ids` 显式列出 claims 引用。解析器可选上下文能力仍保留，历史报告仍检查证据归属；租约、重试、内容寻址结果落盘、后续索引和报告流程保持原有职责与所有权。

Qdrant 查询不再扫描规则目录与全量 payload 对账。规则目录或 chunk ID 格式变化需显式重新入库，不自动同步；本地后端下次检索自动刷新。复核 runner 继续显式使用可配置的有限图执行预算，触顶和上游模型服务错误沿用持久任务错误链路。当前规则配置与兼容说明见 [Agent 配置](Agent配置.md)，范围以 [RAG 修改与验收记录](2026-11-03-RAG修改与验收记录.md) 为准。

### 7.2 复核权限边界

Review worker 使用隔离的 DeerFlow 配置，固定只读工具边界：

- get_event
- get_report
- evidence_reader
- search_events
- rule_retriever
- sample_video
- DeerFlow 内置 view_image

模型不能直接访问 SQLite、Qdrant、任意宿主机路径或写入项目配置。证据读取只能基于账本登记的 event_id + evidence_id，并受 agent.evidence_roots 和 symlink 越界检查约束。复核结论必须能追溯到当前案件可用证据和规则引用；证据或规则不可用时应返回 uncertain，不能猜测为“没有目标”。

## 8. 实现状态矩阵

状态解释：

- 已实现：代码、单元测试或契约测试已有覆盖。
- 条件可用：代码路径存在，但依赖本机 GStreamer factory、驱动、构建 profile、模型、Redis 或外部服务。
- 未完成/限制：当前不能按生产能力宣称已闭环。

| 能力 | 状态 | 事实 |
| --- | --- | --- |
| 启动时硬件能力快照 | 已实现 | SsvCapabilitySnapshot 保存 GStreamer factory、ORT providers、TensorRT 编译能力 |
| 解码 backend plan | 已实现 | VAAPI、NVDEC、software 和 auto fallback 有 plan/test |
| 编解码 backend 解耦 | 已实现 | SsvInferencePlan 与 SsvCodecPlan 独立；RTSP 支持 HardwarePair/Mixed/Software |
| 推理 service 共享 | 已实现 | runner 创建一个 SsvInferenceService，传给 ssvinfer |
| ONNX Runtime provider fallback | 已实现 | profile/provider chain、显式配置校验、session 失败 fallback 有代码路径 |
| TensorRT engine backend | 已实现为条件路径 | 需要带 TensorRT engine 能力和有效 manifest/runtime |
| SSV evidence cache | 已实现 | GStreamer splitmuxsink + mp4mux，Agent 只消费 finalized segments |
| episode 精确窗口 | 已实现 | 基于 source PTS、stream generation 和生命周期状态 |
| Agent SQLite 账本 | 已实现 | SQLite 是事实源，事件/证据/review/index job 可恢复 |
| Review worker | 已实现为条件路径 | 需要启用配置、DeerFlow 配置、模型 provider 和可用证据 |
| Index/Qdrant worker | 已实现为条件路径 | 默认关闭；需要 embedding backend 和 Qdrant |
| Markdown report worker | 已实现 | `agent.reporting` 默认启用；按 ReviewRecord 历史 revision 确定性生成，失败独立重试/dead；不调用模型 |
| 多版本规则发现 | 已实现 | 递归目录、front matter、rule_id + version、local Markdown 检索 |
| 端到端真实 rule.v1 生产闭环 | 未完成/需现场验证 | 自动测试不能代替真实 RTSP、Redis、模型、证据和复核联调 |
| 长期录像与历史回放 | 非本阶段范围 | SSV cache 是短时证据上下文，不是 NVR/WVP 替代品 |

## 9. 主机能力快照（历史记录）

以下是 2026-09-21 在本工作区主机上执行的只读检查，不能当作所有部署机器的结论，也不是当前运行状态。部署或修改依赖后必须重新检查；当前检查命令和 RTSP 结论见 [运行与配置](运行与配置.md)。

~~~text
GStreamer: 1.28.7
VA-API: Intel iHD driver 26.2.4，存在 /dev/dri/renderD128
NVIDIA: 未检测到 nvidia-smi 输出
~~~

当前系统 GStreamer factory：

~~~text
可用: avdec_h264, openh264enc, gtksink, gtkglsink,
      rtph264pay, splitmuxsink, mp4mux, glupload, glcolorconvert

缺失: vah264dec, vah264enc, vapostproc,
      nvh264dec, nvh264enc, rtspclientsink,
      ssvoverlay, ssvinfer, ssvtrack, ssvpub
~~~

因此当前主机在不额外加载项目 plugin path 时，事实上的默认视频路径是：

~~~text
RTSP -> rtph264depay -> h264parse -> avdec_h264
     -> videoconvert -> gtksink
~~~

这表示当前主机不能据此宣称 VAAPI/NVDEC 硬件编解码、RTSP client 推流或完整 SSV 推理插件链已经运行。VA-API 驱动存在不等于 GStreamer vah264dec/vah264enc 已注册；项目插件源代码存在不等于当前 gst-inspect-1.0 已加载它们。

检查命令：

~~~bash
gst-inspect-1.0 --version
gst-inspect-1.0 vah264dec vah264enc vapostproc nvh264dec nvh264enc
gst-inspect-1.0 avdec_h264 openh264enc gtksink gtkglsink rtspclientsink
gst-inspect-1.0 ssvoverlay ssvinfer ssvtrack ssvpub
vainfo
nvidia-smi
./ssv inspect
~~~

## 10. 给智能体的查码规则

遇到问题时按下面顺序查，避免从旧文档或单个插件名称猜结论：

1. 先看工作区状态、当前分支和 config/ssv.yaml；不要覆盖既有未提交修改。
2. 查实时链路：runner/main.cpp -> runner/runtime/ssv_runner.cpp -> runner/runtime/ssv_run_attempt_factory.cpp。
3. 查硬件选择：runner/pipeline/ssv_hardware_capabilities.* -> ssv_pipeline_plan.* -> ssv_pipeline_builder.cpp。
4. 查推理 backend：gst/ssv-infer/core/ssv_inference_service.* -> backends/onnxruntime/ssv_provider_resolver.*。
5. 查 Agent 事件：event_consumer.py -> event_episode.py -> event_store/ledger.py。
6. 查异步任务：service.py 的启动顺序 -> workers.py 的 job 实现 -> EventLedger 的 lease/complete/fail 方法。
7. 查规则：knowledge/catalog.py -> knowledge/backends/local_markdown.py 或 qdrant.py。
8. 对硬件问题同时给出三层结论：代码是否支持、构建/profile 是否包含、当前机器 factory/driver 是否可用。
9. 对证据问题区分 trigger、dedup、episode、cache segment、evidence extract job、review job；不要把它们统称为“录像”。
10. 对推理问题区分 C++ runner-owned inference service、GStreamer ssvinfer adapter、ORT provider 和实际模型文件；不要把 provider 解析写进 Agent。

## 11. 最小验证矩阵

~~~bash
# C++ pipeline/runtime 契约
meson test -C build --print-errorlogs

# Python Agent
cd agent
uv run --extra dev pytest

# 回到仓库根目录
cd ..
./ssv inspect
git diff --check
~~~

真实能力验证必须额外说明环境：

- 无真实 RTSP 源：只能验证 plan/builder，不能宣称播放或推流成功。
- 无 Redis：只能验证 Agent 单测，不能宣称 ingress/ACK 闭环成功。
- 无已注册 ssvinfer/ssvtrack/ssvpub：不能宣称检测、跟踪、发布实时链路成功。
- 无 VAAPI/NVDEC factory：不能宣称硬件解码或硬件编码成功。
- 无模型或 provider：不能宣称推理成功。
- 无 finalized evidence segment：不能宣称 review 已获得视频证据。

配置字段和启动命令见 Agent配置.md；实时输入、显示、推理和跟踪字段见 检测前端配置.md；依赖/profile 选择见 依赖与构建.md。
