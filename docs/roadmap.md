# 安全帽佩戴视频监测分析系统 Roadmap

本路线图采用“里程碑优先，任务包领取，主线作为领域标签”的组织方式。团队按阶段目标推进，每个阶段拆成互不冲突的并行任务包，由 4 名成员领取；`T1-T5` 只用于标记领域边界和接口影响，不绑定固定成员。

当前仓库已经具备单机工程基线：`./ssv` 和 `scripts/` 提供开发入口，`gst/` 中的 GStreamer C++ 插件承载实时视频分析节点，`Redis Streams` 是实时链路和 Agent 链路的异步边界，`agent/` 提供 Python Agent 服务基线。后续目标是把“视频输入 -> YOLO 检测 -> 跟踪 -> 事件 -> 证据 -> Agent 复核 -> 结果输出”逐步打通成可演示、可验证、可交接的单机原型。

## 协作原则

1. 按里程碑推进，每个里程碑进入实现前必须有中文 spec 和 plan。
2. 每个里程碑拆成 4 个左右任务包，成员按任务包领取，尽量避免多人同时修改同一文件。
3. 跨领域标签的接口变更必须先更新中文 spec，再进入实现。
4. Python `./ssv` 入口统一承担构建、清理、依赖检查、本地 Redis、调试入口和测试编排；长期运行时由 C++ pipeline runner 承担。
5. Python Agent 不进入每帧同步检测链路，只消费 Redis 中的 `rule.v1` 事件和受控证据引用；证据字节通过只读工具按 ID 获取。

## 文档规则

每个里程碑进入实现前必须补齐：

1. `docs/specs/YYYY-MM-DD-Mx-名称-spec.md`。
2. `docs/plans/YYYY-MM-DD-Mx-名称-plan.md`。
3. spec 必须说明目标、范围、接口契约、数据结构、错误处理和验证方式。
4. plan 必须说明实施步骤、文件改动、测试命令、兼容性和回滚方式。

spec 和 plan 使用中文；代码标识、命令、路径、配置键保持英文原文。文档中不要保留未完成占位内容；暂不展开的内容写入“非本阶段范围”。

## 领域标签

| 标签 | 领域边界 | 主要目录 | 关键接口 |
| --- | --- | --- | --- |
| T1 | 实时视频链路与运行时：输入、解码、显示、pipeline runner、运行状态 | `scripts/`、`config/`、`runner/` | YAML 配置、GStreamer pipeline、退出码、日志字段 |
| T2 | 感知算法与元数据：YOLO 推理、后处理、检测元数据、跟踪、overlay | `gst/ssv-infer`、`gst/ssv-track`、`gst/ssv-overlay`、`gst/ssv-common`、`gst/tests` | `ssv_meta`、插件属性、测试素材 |
| T3 | 事件与异步边界：事件判定、证据采集、Redis 消息、证据状态 | `gst/ssv-pub`、后续事件/证据模块、`config/` | 事件 schema、SSV evidence cache、Redis Streams |
| T4 | Agent 与知识复核：事件账本、异步 worker、只读工具、模型 provider | `agent/`、后续知识库/工具模块 | Agent 输入输出、账本/job 协议、工具协议、provider 抽象 |
| T5 | 工程集成与质量：测试矩阵、文档模板、CI、本地验证、demo 和交付检查 | `scripts/ssv_cli/`、`docs/`、CI 配置 | 测试命令、集成验收、文档和发布检查清单 |

领域标签只说明任务影响范围，不绑定具体成员。一个成员可以领取跨多个领域的任务包；同一领域也可以被多人并行处理，只要文件边界、接口契约和合流顺序清楚。

## 任务领取规则

1. 每个里程碑默认拆成 `A-D` 四个任务包，4 名成员各领取一个任务包。
2. 任务包按文件边界和接口边界拆分，避免多人同时改同一核心文件。
3. 每个任务包必须写清楚输出和验收，不用“负责某领域”代替具体工作。
4. 涉及接口冻结的任务包先完成文档，再改代码。
5. T5 类质量工作按任务包轮值承担，不固定压给某一个人。

## 当前基线 M0

**状态**：已完成。

已经具备：

- `./ssv build` / `./ssv clean` / `./ssv test` / `./ssv run` / `./ssv run --display` / `./ssv agent` / `./ssv infra start|stop|status` / `./ssv runtime status|reset` / `./ssv inspect` / `./ssv model ...`。
- Meson 构建输出目录固定为 `build`。
- GStreamer C++ 插件构建基线：`ssv-template`、`ssv-infer`、`ssv-track`、`ssv-pub`、`ssv-overlay`、`ssv-common`。
- Python Agent 服务、配置加载、Redis Streams 消费基线。
- Docker Redis 开发环境。
- C++ 插件单元测试、Agent 单元测试以及 CLI、依赖和模型服务测试基线。
- GitHub Actions CI 基线：PR 到 `main` 和 push 到 `main` 时运行 Python CLI 检查与 `./ssv test`；CI 不依赖 RTSP、模型 smoke 或显示环境。

已识别缺口：

- 当前模型 `models/yolov8n.onnx` 是 COCO 模型，只能验证 `person` 检测链路，不能直接判断安全帽。
- `rule.v1` 事件生产、媒体证据采集和生产事件到 Agent 的端到端接入尚未完成；Agent 账本、异步复核和只读工具已有基础实现，仍需接入真实事件链路。

2026-07-31 增量状态：长期实时链路已迁移到 C++ runner，生产入口通过 `./ssv run` 调用
runner；严格配置、VA/DMABuf/GL memory contract、ONNX Runtime Provider、
模型尺寸 host boundary、GTK 独立框层、结构化日志和退出码已完成自动回归。
原生 Linux 各 GPU profile 的真机强验收仍待分机记录；WSL2/WSLg 只完成了 GTK sink
与 GL context 的兼容性诊断，不作为 VA 强验收。

## 里程碑总览

| 里程碑 | 阶段目标 | 主要输出 |
| --- | --- | --- |
| M0 | 工程基线确认 | 已完成的构建、测试、插件、Redis、Agent 和 CLI 基线 |
| M1 | YOLO 工程化实践与安全帽模型训练预研 | YOLO 推理链路说明、`ssv_meta` 检测契约、mock/真实模型 smoke、安全帽训练最小闭环、事件输入草案 |
| M2 | 跟踪算法工程化实践 | 跟踪算法说明、`track_id` 契约、mock/真实跟踪 smoke、事件输入扩展 |
| M3 | 规则事件消息与 Agent ingress 契约 | `rule.v1`、事件生命周期、Redis Streams 消息、Agent 消费样例、字段一致性验收 |
| M4 | 事件证据采集与引用层 | SSV evidence cache、episode 聚合、精确 PTS 窗口、受控证据引用和采集失败降级 |
| M5 | Agent 事件账本与可靠 ingress | `EventLedger`、Redis ACK、`ReviewContext`、持久 review/index job |
| M6 | Agent 异步复核与只读工具 | `ReviewWorker`、结构化复核、证据读取、受控模型调用 |
| M7 | Agent 知识索引与报告输出 | 规则/SOP、`IndexWorker`、Qdrant 检索、规则解释和报告 |
| M8 | 端到端 replay 和交付收口 | 可重复演示、故障回放、运行手册、验收脚本、发布检查清单 |

推进顺序：

```text
M0 工程基线
  |
  v
M1 YOLO 工程化实践与安全帽模型训练预研
  |
  v
M2 跟踪算法工程化实践
  |
  v
M3 规则事件消息与 Agent ingress 契约
  |
  v
M4 事件证据采集与引用层
  |
  v
M5 Agent 事件账本与可靠 ingress
  |
  v
M6 Agent 异步复核与只读工具
  |
  v
M7 Agent 知识索引与报告输出
  |
  v
M8 端到端 replay 和交付收口
```

## M1: YOLO 工程化实践与安全帽模型训练预研

**目标**：在现有 `ssvinfer` 基础上完成 YOLO 工程化梳理和契约冻结，形成可复现的 mock 和真实模型验证方式；同时启动安全帽模型自主训练的最小闭环预研。本阶段允许用小规模数据集完成训练、导出和工程接入验证，但不承诺安全帽识别准确率；`models/yolov8n.onnx` 仍只用于验证 COCO `person` 检测链路。

现有实现基线：

- runner-owned `SsvInferenceService` 已具备原始 ONNX 输入契约校验、ONNX Runtime/TensorRT backend、模型尺寸 RGBA canvas、C++ float32 NCHW 预处理、letterbox 坐标变换、YOLOv5/YOLOv8/Nx6 输出解析、置信度过滤、类别过滤、NMS 和最新帧调度能力；`ssvinfer` 负责 GStreamer 适配和 metadata 发布。
- `SsvDetection`、`SsvFrameDetections` 和检测结果写入 `SsvDetectionStore` 的基础结构已存在。
- `./ssv run` 和 `./ssv test` 已具备真实模型 smoke 的环境依赖分支，运行参数统一来自 YAML 配置；本阶段不重复实现这些基础能力，只补齐说明、契约和验收。

建议文档：

- `docs/specs/YYYY-MM-DD-M1-YOLO工程化与安全帽训练预研-spec.md`
- `docs/plans/YYYY-MM-DD-M1-YOLO工程化与安全帽训练预研-plan.md`

并行任务包：

| 领取 | 领域 | 任务包 | 输出 | 验收 |
| --- | --- | --- | --- | --- |
| A | T2 | YOLO 推理链路梳理：基于 runner-owned inference service 分析原始 ONNX 输入契约、RGBA canvas 到 float32 NCHW 的预处理、YOLO 输出格式、置信度过滤、类别过滤和 NMS | YOLO 推理链路说明，明确当前支持的输出格式和限制 | `./ssv build`、`meson test -C build` |
| B | T2 | 检测元数据契约：基于现有结构固化 `SsvDetection`、`SsvFrameDetections` 字段语义和坐标规则，补齐边界测试 | `ssv_meta` 检测字段契约，坐标统一为归一化坐标 | `gst/tests` 覆盖元数据基本行为和异常输入边界 |
| C | T2 | 安全帽训练最小闭环：定义类别和 label map，用小规模数据集跑通 YOLO baseline 训练、ONNX 导出和单图推理 | 类别表、class_id 顺序、数据目录约定、训练命令、导出命令、ONNX 产物说明 | 不以准确率验收，至少完成数据到 ONNX 的闭环；如时间允许接入 `ssvinfer` smoke |
| D | T1/T3/T5 | 工程验证与下游输入：整理 `./ssv run` 与 YAML 配置中的 mock/真实模型 smoke 参数，定义事件输入草案，形成 M1 验收清单 | mock/真实模型验证命令、YAML 运行参数清单、事件输入字段草案、测试矩阵增量 | mock smoke 和真实模型 smoke 的环境边界清楚；文档评审通过 |

冻结接口：

- `ssv_meta` 检测字段和坐标语义。
- `ssvinfer` 适配器属性：`source-id`、`source-context`、`inference-service`、`mock-detect`；模型路径、runtime、输出格式、类别表和阈值由 YAML 的 `inference` 段管理。
- 安全帽训练预研的 label map、类别顺序和 ONNX 导出约束。
- T3 消费检测结果所需的最小字段。

退出标准：

- 团队能说明当前 YOLO 模型输入、输出、后处理和限制。
- mock 推理链路稳定通过自动测试。
- 真实 `yolov8n.onnx` 原始模型链路有可复现命令，并明确依赖模型文件、预处理配置、视频源和显示环境。
- 安全帽训练预研至少完成类别定义、数据目录约定、训练命令和 ONNX 导出命令。
- 理想结果是自训练安全帽 ONNX 能跑一次单图推理或 `ssvinfer` smoke；如果未完成，必须记录阻塞原因和下一步。

非本阶段范围：

- 安全帽业务准确率评估。
- 大规模数据集治理和正式模型评估。
- 完整事件判定和 Agent 复核。

## M2: 跟踪算法工程化实践

**目标**：在现有 `ssvtrack` 基础上完成跟踪算法工程化梳理和契约冻结，形成可复现的 mock 和真实跟踪验证方式。本阶段参考 M1 的 YOLO 工程化实践组织方式，重点不是引入复杂跟踪框架或重写跟踪插件，而是把现有 `ssvtrack` 的 IoU 匹配逻辑、参数、状态流转、元数据写回和下游消费边界讲清楚、测清楚。

现有实现基线：

- `ssvtrack` 已具备 IoU 匹配、类别约束、简单速度预测、轨迹保留、ID 分配和 `mock-track` 能力。
- `track_id` 字段、`SsvDetectionStore` 的 `HAS_DETECTIONS -> HAS_TRACKS` 状态流转、overlay 读取最新跟踪结果的路径已存在。
- 本阶段不重复开发跟踪插件，重点是契约冻结、测试补强、验证命令和 T3 输入边界。

建议文档：

- `docs/specs/YYYY-MM-DD-M2-跟踪算法工程化实践-spec.md`
- `docs/plans/YYYY-MM-DD-M2-跟踪算法工程化实践-plan.md`

并行任务包：

| 领取 | 领域 | 任务包 | 输出 | 验收 |
| --- | --- | --- | --- | --- |
| A | T2 | 跟踪算法链路梳理：基于现有 `ssvtrack` 分析输入元数据、IoU 匹配、类别约束、速度预测、轨迹保留和 ID 分配策略 | 跟踪算法链路说明，明确当前支持的跟踪能力和限制 | `./ssv build`、`meson test -C build` |
| B | T2 | 跟踪元数据契约：基于现有 `track_id` 和 `SsvDetectionStore` 流转固化语义，补齐边界测试 | 跟踪字段契约，明确检测、跟踪、overlay、发布之间的数据交接 | `gst/tests` 覆盖 track ID 写回、未跟踪默认值和元数据状态流转 |
| C | T2/T5 | mock/真实跟踪验证：补齐 `mock-track` 和 IoU 跟踪的最小 smoke，用人工构造检测序列验证 ID 递增、匹配、丢失保留和重建 | mock/真实跟踪验证命令、测试素材或构造方式、验收清单 | mock 跟踪可在无模型环境跑通；IoU 跟踪行为有自动测试或可复现手工命令 |
| D | T3/T5 | 下游事件输入扩展：梳理 T3 消费跟踪结果所需字段，定义事件消息中 `detections` 和 `tracks` 的最小输入草案 | 事件输入字段扩展、测试矩阵增量、M2 验收清单 | 文档评审通过；C++、CLI 基础测试通过 |

冻结接口：

- `track_id` 字段语义：`-1` 表示未跟踪，非负值表示 `ssvtrack` 分配的轨迹 ID。
- `ssvtrack` 基础插件属性：`frame-rate`、`track-thresh`、`track-buffer`、`match-thresh`、`mock-track`。
- `SsvDetectionStore` 中检测结果进入跟踪、跟踪结果进入发布和 overlay 的状态流转。
- T3 消费检测和跟踪结果所需的最小字段。

退出标准：

- 团队能说明当前 `ssvtrack` 的 IoU 匹配、ID 分配、轨迹保留和限制。
- `track_id` 字段契约和元数据状态流转被文档化，并有基础测试覆盖。
- mock 跟踪链路稳定通过自动测试。
- 真实跟踪链路有可复现命令，并明确依赖模型文件、视频源和显示环境。
- T3 事件消息能够基于 M1 检测字段和 M2 跟踪字段继续设计。

非本阶段范围：

- 引入 DeepSORT、ByteTrack、OC-SORT 等外部跟踪框架。
- ReID 特征提取和跨摄像头跟踪。
- 完整事件判定和 Agent 复核。
- 多路视频调度。

## M3: 规则事件消息与 Agent ingress 契约

**目标**：把 M1/M2 已冻结的检测和跟踪元数据规范化为 T3 的 `rule.v1` 结构化事件，并通过 Redis Streams 给 T4 提供稳定消费契约。事件必须表达规则语义和生命周期，不由每条 detection 消息直接代替。

现有实现基线：

- `ssvpub` 当前只能向 Redis Streams 发布包含 `type`、`source`、`timestamp_ms`、`frame_id`、`detections`、`bbox` 和 `track_id` 的稀疏检测消息；这不是完整的 `rule.v1` 事件。
- Python Agent 已有 Redis Streams 消费、JSON 解析、日志输出和 ACK 的最小样例。
- 本阶段不重复实现基础发布/消费链路，重点是正式冻结 schema、补稳定事件身份和生命周期、增加安全帽事件规则，并让 Agent 能区分事件语义与 publisher ingress。

建议文档：

- `docs/specs/YYYY-MM-DD-M3-规则事件消息与Agent-ingress契约-spec.md`
- `docs/plans/YYYY-MM-DD-M3-规则事件消息与Agent-ingress契约-plan.md`

并行任务包：

| 领取 | 领域 | 任务包 | 输出 | 验收 |
| --- | --- | --- | --- | --- |
| A | T2/T3 | 字段交接：确认 M1 检测字段和 M2 跟踪字段进入事件层的映射关系 | 检测/跟踪到事件字段映射表 | C++ 发布字段和文档字段一致 |
| B | T3 | 规则事件消息：规范化 Redis 消息为 `rule.v1`，补齐 `event_id`、`event_type`、`event_phase`、`rule_id`、`rule_version`、`severity`、`rule_facts` 和错误语义；真实存在时传播 `source_pts`、`stream_generation` | 规则事件 schema、兼容字段和版本策略 | Redis Stream 中消息可被 Agent 稳定解析，并有字段一致性测试 |
| C | T3 | 安全帽事件规则：定义连续命中、低置信度、检测冲突和 `START/UPDATE/END` 生命周期 | 规则说明、事件状态转换和测试入口 | 支持 mock/no-op 降级；同一语义事件不会仅因 track ID 抖动而重复创建 |
| D | T4/T5 | 消费与验收：对齐现有 Agent 消费样例和 M3 schema，增加 M3 集成验收 | 事件消费样例、解析测试、C++ 发布和 Python 消费字段一致性检查 | C++、CLI、Agent 基础测试通过 |

冻结接口：

- Redis Stream key、字段名、字段类型、事件身份、生命周期和错误语义。
- `detections`、`tracks` 在事件消息中的序列化格式。
- Agent 消费事件所需的最小输入，以及缺失 `source_pts`/`stream_generation` 时保持未知的规则。

退出标准：

- 检测/跟踪结果能形成结构化 Redis 消息。
- 规则事件具备稳定 `event_id`，能表达 `START/UPDATE/END`，并能关联事件触发帧。
- Agent 侧能解析消息并完成最小消费测试。
- 后续新增证据路径和复核结果时有兼容扩展位置。

非本阶段范围：

- 证据文件生成。
- Agent 事件账本、异步复核和只读工具。
- 外部通知和报告。

## M4: 事件证据采集与引用层

**目标**：为 M3 的规则事件提供可追溯的媒体证据，并冻结 Agent 的证据引用边界。本阶段由 T3 负责采集和落盘，Agent 只接收受控引用，不实现 Agent 状态机、工具调用或模型复核。

建议文档：

- `docs/specs/YYYY-MM-DD-M4-事件证据采集与引用层-spec.md`
- `docs/plans/YYYY-MM-DD-M4-事件证据采集与引用层-plan.md`

并行任务包：

| 领取 | 领域 | 任务包 | 输出 | 验收 |
| --- | --- | --- | --- | --- |
| A | T3 | 证据提取契约：以事件锚点关联 SSV cache，明确独立 GStreamer evidence branch、sidecar 和外部长期录像的责任边界 | `event_id`/帧锚点/时间字段映射、extractor 接口和能力约束 | 能说明采集来源、时间精度和不可用条件，不把墙上时间当媒体 PTS |
| B | T3 | 证据采集 MVP：优先实现关键帧快照；使用 bounded queue、受控目录、临时文件和原子 rename，不能阻塞检测/跟踪 | 证据目录、命名规则、采集状态、hash 和配额策略 | 规则事件触发后可异步生成快照；队列满或写入失败不会拖垮实时链路 |
| C | T3/T4 | Agent 引用契约：定义 `EvidenceRef`、`evidence_id`、`kind`、`mime_type`、`available`、`sha256`、可选 PTS 区间和 `stream_generation` | 证据引用 schema、ID-only 工具边界、虚拟路径适配 | Agent 只使用 `event_id + evidence_id`；不向模型暴露任意宿主机路径 |
| D | T3/T5 | 证据失败降级与验收：覆盖文件缺失、越界路径、symlink、重启、过期和可选短片段不可用 | `requested/capturing/available/unavailable/expired` 语义、样例和验收脚本 | 事件仍可入账；Agent 能得到明确缺失原因并按策略进入 `uncertain` 或人工复核 |

冻结接口：

- `event_id`、事件触发帧、`source_pts`、`stream_generation` 和证据关联规则。
- `EvidenceRef` 的 `evidence_id`、证据类型、状态、MIME、大小、hash 和可选时间范围。
- Agent 只按 `event_id + evidence_id` 读取证据；宿主机路径只属于账本/取证 extractor 内部实现。
- 证据保存失败、证据缺失和 Redis 发布失败的降级语义。

退出标准：

- 规则事件能关联一个或多个证据引用；证据可以稍后出现，但状态变化可被观察。
- 证据文件可访问，或明确标记 `unavailable`/`expired` 及失败原因。
- 证据缺失、写入失败、Redis 发布失败都有明确状态和错误字段。

非本阶段范围：

- Agent 事件账本和持久任务状态机。
- OpenAI SDK、视觉复核和文本解释。
- 向量检索、报告生成和通知。
- WVP/NVR/MediaMTX 的长期录像、历史回放和跨设备录像管理；本阶段只负责 SSV 的短时 cache。

## M5: Agent 事件账本与可靠 ingress

**目标**：Agent 将 `rule.v1` 事件可靠写入 SQLite 权威账本，并把复核和索引拆成可恢复的持久任务。本阶段不让模型调用进入 Redis 消费线程。

建议文档：

- `docs/specs/YYYY-MM-DD-M5-Agent事件账本与可靠ingress-spec.md`
- `docs/plans/YYYY-MM-DD-M5-Agent事件账本与可靠ingress-plan.md`

当前实现基线：

- `EventLedger` 已提供事件、检测、证据引用、revision 和 durable job 的 SQLite 封装；`evidence_roots` 对证据路径执行 fail-closed 校验。
- `ReviewContext` 已能保留 `event_id`、规则字段、`source_pts` 和 `stream_generation` 的缺失语义；仍需与正式 `rule.v1` 生产消息对接。
- `EventConsumer` 已按“账本事务成功后 ACK”的方向实现；生产事件生命周期、证据采集结果和回放验收仍待接入。

并行任务包：

| 领取 | 领域 | 任务包 | 输出 | 验收 |
| --- | --- | --- | --- | --- |
| A | T4 | `ReviewContext` 与 `rule.v1` 映射：解析事件、检测、轨迹、规则事实和证据 ID；缺失时间线字段保持未知 | 上下文构造和兼容解析 | 单测覆盖完整事件、缺证据事件、坏 JSON 和旧消息 |
| B | T4 | `EventConsumer` ingress：处理 Redis Stream、poison input、重复投递、pending reclaim 和 ACK 语义 | 消费循环、幂等身份和错误分类 | SQLite 写入失败不 ACK；账本提交成功后立即 ACK，不等待模型/Qdrant |
| C | T4 | `EventLedger` 事务与持久任务：写入 EventCase/EvidenceRef，创建 review/index job，提供 lease、retry、dead 和 fencing | SQLite schema/migration、账本接口和任务状态 | 进程重启、重复消息和 worker 接管不会产生重复或乱写 |
| D | T4/T5 | ingress 回放验收：用固定 `rule.v1` fixtures 验证事件、证据引用和任务创建 | fixture、日志字段、验收脚本 | Agent 能稳定接收可用、缺证据和时间字段未知的事件 |

冻结接口：

- `ReviewContext`、`EventCase`、`EvidenceRef` 和 `DurableJob` 的接口。
- Redis ACK、SQLite 事务、幂等和 pending reclaim 语义。
- `pending -> processing -> completed/pending/dead` 的持久任务迁移规则。
- 账本错误、证据缺失和 poison input 的错误分类。

退出标准：

- Agent 能从 `rule.v1` Redis 事件构造上下文并提交权威账本。
- 有效事件在 SQLite 事务提交后 ACK；模型、embedding 或 Qdrant 故障不阻塞 ingress。
- 重复投递只产生一个案件和每个 revision 对应的一组持久任务。

非本阶段范围：

- 模型复核、只读工具和视觉输入。
- 规则知识检索、向量索引和报告生成。
- 外部通知和工单系统。

## M6: Agent 异步复核与只读工具

**目标**：由独立 `ReviewWorker` 领取持久任务，按事件和证据引用构造复核输入，通过受控只读工具调用模型，并以结构化结果原子回写。模型不能直接访问 SQLite、Qdrant 或任意宿主机路径。

建议文档：

- `docs/specs/YYYY-MM-DD-M6-Agent异步复核与只读工具-spec.md`
- `docs/plans/YYYY-MM-DD-M6-Agent异步复核与只读工具-plan.md`

当前实现基线：

- `ReviewWorker`、结构化 `ReviewResult` 和证据引用校验已具备基础实现；非 `uncertain` 结论必须引用本案件当前可用证据。
- `get_event`、`evidence_reader`、`search_events` 和 `rule_retriever` 已按只读工具方向接入；其中规则检索仍为 mock backend。
- DeerFlow 复核资源、模型调用和工具 allowlist 已有隔离约束；仍需用真实 `rule.v1` + 采集证据完成端到端验证。

并行任务包：

| 领取 | 领域 | 任务包 | 输出 | 验收 |
| --- | --- | --- | --- | --- |
| A | T4 | `ReviewWorker`：领取 review job、刷新证据、构造上下文、调用模型、解析结果并完成/重试任务 | worker、lease heartbeat、retry/dead 和原子提交 | Redis 消费线程不执行模型调用；worker 丢 lease 后不能追加结果 |
| B | T4 | 只读工具：实现 `get_event`、`evidence_reader`、`search_events` 和 `view_image` 的 ID/虚拟路径边界 | 工具协议、路径校验、证据元数据和缺失结果 | 工具不能读取未登记、根外或越界 symlink 证据 |
| C | T4 | 模型 Provider：保留窄接口，支持 mock 和 OpenAI-compatible/DeerFlow 调用，配置超时、限流和错误映射 | provider、结构化 JSON 和结果 artifact | 测试不依赖真实 API；外部服务不可用时任务可重试或进入人工复核 |
| D | T4/T5 | 复核与引用验收：要求 claims 引用 `evidence_id`，区分 `available`、`missing` 和 `uncertain` | `ReviewResult`、review history、验收清单 | 非 `uncertain` 结论不能引用不可用证据；模型输出不能改变权限和事件事实 |

冻结接口：

- 只读工具协议和 `event_id + evidence_id` 参数边界。
- 模型 Provider 输入输出结构和结构化 `ReviewResult`。
- `claims -> evidence_ids` 的可追溯引用规则。
- 模型超时、限流、无效响应、证据缺失和不可用时的任务语义。

退出标准：

- `ReviewWorker` 能独立完成成功、重试、dead、缺证据和丢 lease 路径。
- 测试默认使用 mock provider，不依赖真实 API；真实模型调用有明确配置、超时、失败降级和本地跳过策略。
- Agent 复核结果可按 `event_id` 查询，并保留结构化证据引用和 review history。

非本阶段范围：

- 多厂商 provider 路由平台。
- 生产级规则知识库治理。
- 外部通知实际发送。

## M7: Agent 知识索引与报告输出

**目标**：把版本化规则/SOP、事件索引和复核结果组织成可追溯的 Agent 上下文与报告，仍保持单机原型边界。向量库是可重建投影，SQLite 账本是事实源。

建议文档：

- `docs/specs/YYYY-MM-DD-M7-Agent知识索引与报告输出-spec.md`
- `docs/plans/YYYY-MM-DD-M7-Agent知识索引与报告输出-plan.md`

当前实现基线：

- `IndexWorker`、embedding provider、Qdrant event/rule collection 和 SQLite hydrate 查询已有基础实现。
- embedding identity、Qdrant 失败降级和旧 revision 处理已有约束；生产规则/SOP 内容、审批和持续更新管线尚未完成。

并行任务包：

| 领取 | 领域 | 任务包 | 输出 | 验收 |
| --- | --- | --- | --- | --- |
| A | T4 | 规则知识输入：整理版本化安全规则、制度片段和历史处置样例 | 规则文档目录、版本/来源字段、加载方式 | 可按 `rule_id`/事件类型取到可追溯规则片段 |
| B | T4 | `IndexWorker` 与检索边界：构造规范事件文本，写入可重建 Qdrant，命中后回 SQLite hydrate | embedding identity、索引任务、属性/语义检索和降级 | Qdrant 不可用时属性检索仍可工作；不把本地路径写入向量文本 |
| C | T4 | 规则解释：基于事件事实、证据引用和规则片段生成说明 | 规则解释 schema、来源和 evidence IDs | 输出同时标明规则来源和现场证据来源 |
| D | T4/T5 | 报告输出：生成事件摘要、复核结论、处置建议和告警升级内容 | 报告 JSON/Markdown、样例和验收清单 | 报告不补造 PTS、唯一计数或缺失证据事实 |

冻结接口：

- 规则片段格式、版本和来源字段。
- 属性/语义检索结果格式及 SQLite hydrate 规则。
- 规则解释结果格式和证据引用格式。
- 报告输出格式与缺失知识/证据时的降级语义。

退出标准：

- Agent 可把版本化规则片段纳入上下文和解释，并保留来源。
- `IndexWorker` 可重试、重建并隔离不同 embedding identity；检索不可用时流程可降级。
- 输出包含复核结论、规则依据、现场证据引用、处置建议和摘要。

非本阶段范围：

- 完整知识库治理和人工审批平台。
- 长期事件库、检索页面和统计报表。
- 真实外部通知发送。

## M8: 端到端 replay 和交付收口

**目标**：把 M1-M7 的能力合流成可演示、可排障、可交接的单机端到端原型。以 `./ssv run` 调用 C++ pipeline runner 作为演示和长期运行基线，收口 `rule.v1`、证据引用、Redis、`EventLedger`、异步复核、索引和结果链路，并用固定 fixtures 完成 replay 验收。

建议文档：

- `docs/specs/YYYY-MM-DD-M8-端到端replay和交付收口-spec.md`
- `docs/plans/YYYY-MM-DD-M8-端到端replay和交付收口-plan.md`

并行任务包：

| 领取 | 领域 | 任务包 | 输出 | 验收 |
| --- | --- | --- | --- | --- |
| A | T1/T5 | 演示运行时：收口 `./ssv run` 的 headless/display 路径和 YAML 配置，确认 C++ pipeline runner 的退出码与日志边界 | demo 运行命令、运行时边界说明 | `./ssv run` 至少有一条路径可稳定演示 |
| B | T2/T3 | 演示事件与证据：可重复生成未佩戴、低置信度和检测冲突的 `rule.v1` 事件，并关联可用或不可用证据 | demo 配置、事件样例、EvidenceRef 样例 | Redis 中可看到稳定 `event_id`、生命周期和证据状态，不依赖宿主机路径给 Agent |
| C | T4 | 演示 Agent：跑通 `EventConsumer -> EventLedger -> ReviewWorker/IndexWorker -> 只读工具 -> 结果` | demo Agent 流程、复核/索引结果样例 | mock 流程和固定事件 fixtures 可重复执行 |
| D | T5 | 交付文档：更新 README、运行手册、故障排查、测试矩阵和发布检查清单 | 交付清单和验收脚本 | 新成员可按文档复现 demo |

冻结接口：

- demo 配置文件和运行命令。
- 端到端日志字段和退出码。
- `event_id`、`evidence_id`、job/review revision 和失败状态的关联日志。
- 交付验收清单。

退出标准：

- 从视频输入到检测、事件、证据、Agent 复核、规则解释、结果输出形成闭环。
- replay 能覆盖重复事件、证据缺失、队列/写盘失败、Agent 重启、Qdrant 不可用和时间字段未知，并保留可定位的失败原因。
- 复核结论中的每条 claim 都能回到本案件当前可用的 `evidence_id`；缺证据时不会生成确定性结论。
- 新成员可以按 README 和运行手册复现 demo。
- CI 和本地验证边界清楚，无法自动化的验收项有手工步骤。

非本阶段范围：

- WVP、ZLMediaKit 等平台级接入。
- 大规模多路视频调度、集群部署和高可用治理。
- 完整前端业务系统。

## 接口冻结表

| 接口 | 冻结里程碑 | 负责 | 消费 | 说明 |
| --- | --- | --- | --- | --- |
| `ssv_meta` 检测字段和坐标语义 | M1 | T2 | T1、T3、T5 | 检测框统一使用归一化坐标 |
| `ssvinfer` 适配器与推理配置 | M1 | T2 | T1、T5 | 插件使用 `source-id`、`source-context`、`inference-service`、`mock-detect`；模型和后处理使用 YAML `inference` 段 |
| 安全帽训练产物契约 | M1 | T2 | T1、T3、T5 | label map、类别顺序、ONNX 导出约束和产物说明 |
| 事件输入字段草案 | M1 | T3 | T4、T5 | 先冻结最小字段，M3 扩展为 Redis schema |
| 跟踪字段和 track ID 语义 | M2 | T2 | T3、T5 | `track_id`、未跟踪默认值、跨帧稳定性预期 |
| `ssvtrack` 基础插件属性 | M2 | T2 | T1、T3、T5 | `frame-rate`、`track-thresh`、`track-buffer`、`match-thresh`、`mock-track` |
| `rule.v1` Redis 事件 schema | M3 | T3 | T4、T5 | 稳定 `event_id`、`START/UPDATE/END`、规则事实、字段类型和错误语义；缺失 PTS/generation 保持未知 |
| EvidenceExtractor 与 `EvidenceRef` | M4 | T3/T4 | T4、T5 | 以事件锚点关联 SSV cache 证据；Agent 只按 `event_id + evidence_id` 读取，并识别证据状态和降级原因 |
| `ReviewContext`、`EventCase` 和 `DurableJob` | M5 | T4 | T3、T5 | SQLite 是事实源；Redis ACK、幂等、lease、retry 和 dead 语义明确 |
| 只读工具与 `ReviewResult` | M6 | T4 | T5 | 工具不接收任意宿主机路径；claims 必须引用本案件可用 `evidence_id` |
| 规则知识、索引和报告格式 | M7 | T4 | T5 | Qdrant 只作可重建投影；输出包含规则来源、复核结论、证据引用、处置建议和摘要 |
| 端到端运行命令和验收清单 | M8 | T5 | T1、T2、T3、T4 | README 和运行手册同步 |

接口一旦被后续里程碑采用，修改时必须保留兼容路径，或在对应 spec 中明确迁移方式。

## 验证矩阵

| 范围 | 命令 | 说明 |
| --- | --- | --- |
| C++ 插件、元数据、Meson | `./ssv build` | 构建共享库、插件和测试 |
| C++ 单元测试 | `meson test -C build` | 覆盖插件注册、元数据和 C++ 测试 |
| Python Agent | `cd agent && uv run --extra dev pytest` | 覆盖 Agent 配置、消费和服务测试 |
| CLI/依赖/模型服务 | `uv run python -m unittest discover -s scripts/ssv_cli/tests -p 'test_*.py'` | 覆盖 `./ssv` CLI、依赖策略、模型服务和 Redis 管理入口 |
| 综合测试入口 | `./ssv test` | 代码测试和链路 smoke 编排，部分项依赖 RTSP、模型和 Redis |
| 本地显示链路 | `./ssv run --display` | 依赖视频源、模型、Redis 和显示环境 |

每个 plan 必须说明本阶段实际需要运行哪些命令，以及哪些命令因本地环境缺失无法运行。PR 合入 `main` 前至少要求 GitHub Actions CI 通过；本地链路 smoke 和显示窗口验证保留为里程碑验收项。

## 分支和合流

- 每个里程碑可以使用短生命周期分支，例如 `feature/m1-yolo-engineering` 或 `integration/m3-events-redis`。
- 成员按任务包拆短分支，例如 `feature/m1-a-infer-notes`、`feature/m1-c-helmet-training`。
- 同一里程碑内优先按文件边界拆分；确实需要多人改同一文件时，先约定合流顺序。
- 跨领域标签接口改动先提文档 PR，再提实现 PR。
- 集成分支只做接口适配、测试补齐和小范围修复，不承载大功能开发。
- 不要回退他人或既有未提交改动；不要使用 `git reset --hard` 或 `git checkout --` 清理文件。
