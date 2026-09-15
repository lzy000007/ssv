# MediaMTX 录像上下文证据提取设计

## 文档定位

本文定义在不修改 GStreamer 实时检测、跟踪和 Redis 发布链路的前提下，使用
MediaMTX 本地录像为 Agent 生成事件上下文证据的首阶段设计。实现目标是验证视觉
复核收益，不将录像抽帧描述为与检测框严格同帧的证据。

本文补充现有事件证据设计中“准确 `analysis_frame` 异步复制”的后续能力。两者的
证据精度和所有权不同，录像方案不能替代准确分析帧方案。

## 目标

1. 每个新入账事件自动生成事件前 2.5 秒至后 2.5 秒的上下文短视频，以及事件时刻
   附近三张帧图像。
2. 将原始录像和派生证据放在既有 `agent.evidence_roots[0]` 下，保持账本是模型读取
   证据的唯一授权来源。
3. 用持久、幂等、可重试的 Agent worker 执行取证；取证完成后才创建视觉复核任务。
4. 记录输入分段、时间窗口、实际媒体区间和 SHA-256，使复核结果可以追溯其上下文。
5. 原始录像按 MediaMTX 保留期清理，派生 clip 与帧不由本阶段自动清理。

## 非本阶段范围

- 不修改 GStreamer pipeline、插件、检测帧元数据、跟踪器或 Redis 发布 payload。
- 不宣称 clip 或帧与检测所属分析帧严格一致，不为它们绘制检测框。
- 不让语言模型调用 `ffmpeg`、访问录像目录或选择任意宿主机路径。
- 不接入 MediaMTX Control API、playback HTTP API、`runOnRecordSegmentComplete` 或外部
  录像微服务。
- 不实现录像、派生证据或事件的自动归档和删除策略。
- 不支持跨 source、跨 stream generation 的录像关联。

## 目录与配置边界

现有配置中的首个证据根目录必须存在，且是唯一的录像与派生文件父目录：

```text
agent.evidence_roots[0]
  recordings/<rtsp_path>/
    <unix-seconds>-<microseconds>.mp4
  derived/<event_id>/
    context.mp4
    frame-01.jpg
    frame-02.jpg
    frame-03.jpg
    manifest.json
```

当前部署的根目录为：

```text
/mnt/work/ai-video-analysis/ssv/artifacts/evidence
```

`<rtsp_path>` 从共享 SSV 配置内该 source 的 RTSP URI 解析。例如
`rtsp://localhost:8554/stream` 映射为 `stream`，因此原始录像目录为
`artifacts/evidence/recordings/stream`。解析结果必须是单个安全路径组件；空路径、含有
目录穿越语义的路径或无法匹配 source 的 URI 都是不可重试的配置错误。

MediaMTX 的运行配置位于仓库外的
`/mnt/work/ai-video-analysis/mediamtx/mediamtx.yml`。实际分析与录像必须共享
`paths.stream`：该 path 保持 `source: publisher`，并持有当前测试视频 `runOnInit`。
录制配置为：

```yaml
record: true
recordPath: /mnt/work/ai-video-analysis/ssv/artifacts/evidence/recordings/%path/%s-%f
recordFormat: fmp4
recordPartDuration: 1s
recordSegmentDuration: 5s
recordDeleteAfter: 1d
```

`recordings` 是 MediaMTX 管理的原始循环录像目录；`derived` 是 Agent 管理的事件
派生产物目录。两者虽位于 evidence root 内，只有 SQLite 中已登记到某一事件的普通
文件可由 `evidence_reader` 复制给复核模型。

Agent 新增以下严格校验的配置段，不包含录像根目录或 source 映射：

```yaml
agent:
  recording_evidence:
    enabled: true
    clip_before_ms: 2500
    clip_after_ms: 2500
    frame_offsets_ms: [-1000, 0, 1000]
    max_retries: 3
    retry_delay_ms: 2000
```

时间参数不得为负；`clip_before_ms`、`clip_after_ms` 必须为正；三个 frame offset 必须
严格递增且全部落在 clip 窗口内；重试次数和延迟必须为正；启用时
`agent.evidence_roots` 必须至少包含一个绝对路径。

## 数据流与时间语义

```text
MediaMTX path /stream
  -> fMP4 原始分段（recordings/stream）
  -> EventConsumer 事件入账
  -> evidence_extract durable job
  -> 等待事件后窗口和分段完成
  -> RecordingEvidenceWorker
  -> context.mp4 + 三张 JPEG + manifest
  -> SQLite EvidenceRef
  -> review durable job
  -> ReviewWorker / evidence_reader / view_image
```

事件入账的同一 SQLite 事务创建 `evidence_extract` job，而不创建 `review` job。任务的
唯一身份使用既有 `(kind, entity_id, entity_revision)`，其中 kind 为
`evidence_extract`。工作者在 `timestamp_ms + clip_after_ms` 后才可领取任务，并从
录像目录选择覆盖 `[T - 2500ms, T + 2500ms]` 的 fMP4 分段。

当前 `timestamp_ms` 由发布端的墙钟生成，不是检测对应的媒体 PTS。因此 manifest、
账本元数据与日志均标记 `time_basis=wall_clock_approximate`。所选分段的文件名提供
Unix 起始时间，`ffprobe` 提供实际时长；二者共同决定实际可提取区间。无法覆盖完整
窗口时不得用缩短窗口静默代替。

工作者以不经 shell 的参数数组调用 `ffprobe` 和 `ffmpeg`。它只允许使用解析后位于
`recordings/<rtsp_path>` 的普通文件，拒绝符号链接、目录和越界文件。它使用覆盖分段
生成精确 5 秒、重新编码的 `context.mp4`，并在相对事件时间 `-1000ms`、`0ms`、
`+1000ms` 解码出三张 JPEG。具体编码参数是内部实现细节，但输出必须可由当前
DeerFlow `view_image` 使用。

每个事件在 `derived/<event_id>` 的同级临时目录生成全部产物；只有 clip、三帧和
manifest 均完成、关闭并计算 SHA-256 后，才原子重命名为最终目录。随后在一个账本
事务内登记一个 `kind=clip` 和三个 `kind=frame` 的 EvidenceRef，并创建 `review` job。
复核模型只可通过现有 `evidence_reader(event_id, evidence_id=None)` 获取这些登记项。

## 持久化与兼容性

`durable_jobs.kind` 扩展为 `evidence_extract`。当前 SQLite CHECK 约束只允许
`review` 与 `index`，迁移必须在事务中重建 `durable_jobs` 表：保留既有 job ID、
任务字段、唯一约束、索引、状态、租约和历史 review/index 语义，再加入新 kind。

新事件的复核准入规则改为：

1. `recording_evidence.enabled=true` 时，先创建 `evidence_extract`。
2. 该任务成功后才登记 EvidenceRef 并创建一次 `review`。
3. 同一 job 重试、重复 Redis ingress 或 worker lease reclaim 不得生成重复目录、
EvidenceRef 或 review job。
4. `recording_evidence.enabled=false` 时保留当前事件入账与复核行为，避免影响现有
部署和历史测试。

`ReviewContext` 继续携带一个默认 `frame_path` 和一个 `clip_path`；三张帧和 clip 的
完整集合由 `evidence_reader` 的已登记 evidence 列表暴露。提示词需要要求模型先读取
可用证据，并为非 uncertain 结论引用相应 evidence ID。

## 失败语义

可重试失败包括：事件后窗口尚未闭合、覆盖分段尚未完成、尚无完整窗口、短暂文件
读取失败，以及 `ffprobe`/`ffmpeg` 的瞬时失败。任务按 `max_retries` 和
`retry_delay_ms` 重试。

不可重试失败包括：录像功能关闭、evidence root 缺失、source 无法解析到安全 RTSP
path、输入路径越界或符号链接、分段编码无法读取、输出目录不可用。实现可将同类
ffmpeg 错误经一次重新扫描后判定为不可重试，避免无意义重试。

达到上限或发生不可重试失败时，取证 job 状态为 `dead`，事件进入
`manual_review`，并保存不包含敏感绝对路径的失败原因。事件事实不回滚，且不得创建
无图 review job。临时目录在成功、失败和进程恢复扫描时清理；任何半成品都不登记为
EvidenceRef。

## 验收与验证

自动测试至少覆盖：

1. `recording_evidence` 的默认值、未知键与范围校验，以及启用时 evidence root 缺失
   的失败。
2. 从 RTSP URI 安全解析录像 path，拒绝空、越界和多段 path。
3. 固定分段文件名和 `ffprobe` 时长下的窗口覆盖选择、延迟领取和重试调度。
4. `durable_jobs` 迁移后历史 review/index 任务保持可领取，新 kind 可持久化且保持
   幂等。
5. subprocess 参数不使用 shell，根外/符号链接/非普通输入被拒绝。
6. 固定 fMP4 fixture 可生成 5 秒 clip、三张 JPEG、manifest 和 SHA-256；任一产物
   失败时最终目录和 EvidenceRef 均不存在。
7. 成功取证只创建一次 review job；证据失败或未 ready 时不创建 review job；
   `evidence_reader` 只能读取登记后的派生产物。

人工验收使用当前 `/stream`：确认 MediaMTX 实际生成 5 秒 fMP4 分段，向 Redis 注入
一个测试事件后确认派生目录、manifest、SQLite EvidenceRef、review job 和模型引用
一致。验收同时记录实际事件时间与抽取帧的可见偏差，作为决定是否引入准确分析帧
证据的依据。

## 交付边界

本阶段交付 MediaMTX 录像配置、Agent 配置、持久取证 worker、账本迁移、派生证据
登记、复核准入改造、测试和部署文档。完成后不自动暂存、提交、推送或修改原始录像
保留期以外的清理策略。
