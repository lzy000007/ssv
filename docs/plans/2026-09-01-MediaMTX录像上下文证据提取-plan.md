# MediaMTX 录像上下文证据提取 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 每个 Task 通过验证后保持未暂存，报告 diff 并等待人工确认；未经明确要求不提交。

**Goal:** 在不改变 GStreamer 实时链路的前提下，从 MediaMTX 本地录像生成每个事件的 5 秒上下文 clip 与三张帧，并在证据就绪后才调度 Agent 复核。

**Architecture:** MediaMTX 在现有 evidence root 的 `recordings/<rtsp_path>` 写入 5 秒 fMP4 分段。Agent 将新事件原子投影为一个 `evidence_extract` durable job；独立 worker 在窗口闭合后以受限本地文件和无 shell 的 `ffprobe`/`ffmpeg` 调用生成 `derived/<event_id>`，账本原子登记四个 EvidenceRef 并创建 review job。

**Tech Stack:** Python 3.12、Pydantic v2、SQLite、pytest、`subprocess`、系统 `ffmpeg` 6.1+/`ffprobe`、MediaMTX v1.18.2。

**Spec:** `docs/specs/2026-09-01-MediaMTX录像上下文证据提取-spec.md`

## Global Constraints

- 不修改 GStreamer、跟踪器、Redis 发布 payload 或实时 pipeline。
- `agent.evidence_roots[0]` 是录像和派生证据共同父目录；不新增录像根目录或 source 映射配置。
- RTSP URI 只接受单个安全 path component；当前 `/stream` 映射为 `recordings/stream`。
- clip 固定为 `[timestamp_ms - 2500ms, timestamp_ms + 2500ms]`；帧固定为 `T-1000ms`、`T`、`T+1000ms`。
- `timestamp_ms` 是 `wall_clock_approximate`，不得描述为检测分析帧 PTS。
- 录像输入、派生产物和账本路径均须解析后保留在 evidence root 内；拒绝符号链接和非普通文件。
- 所有 ffmpeg/ffprobe 调用使用参数数组、超时和受控文件清单，不使用 shell。
- 成功必须原子登记一个 clip、三个 frame 与唯一 review job；失败不得创建无图 review job。
- 原始录像由 MediaMTX 按 1 天清理；本阶段不自动删除 `derived`。
- 每个 Task 完成后保持未暂存、运行 `git diff --check` 和定向测试，等待人工审阅后再进入下一 Task。

## File Structure

- `agent/src/ssv_agent/config.py`：解析保留最小 source URI 映射，定义并校验录像证据配置。
- `agent/src/ssv_agent/event_store/schema.py`：迁移 durable job 的 CHECK 约束以允许 `evidence_extract`。
- `agent/src/ssv_agent/event_store/ledger.py`：创建、完成、失败 evidence extract job，并以事务控制 review 准入。
- `agent/src/ssv_agent/recording_evidence.py`：独立的安全路径解析、分段选择、ffprobe/ffmpeg 执行、临时目录与派生产物元数据。
- `agent/src/ssv_agent/workers.py`：领取 `evidence_extract` job 的 worker。
- `agent/src/ssv_agent/service.py`：创建并管理 recording evidence worker 生命周期。
- `agent/tests/test_recording_evidence.py`：提取器单元与 ffmpeg fixture 测试。
- `agent/tests/test_config.py`、`agent/tests/test_event_ledger.py`、`agent/tests/test_workers.py`、`agent/tests/test_service.py`：配置、迁移、任务准入和生命周期覆盖。
- `config/ssv.yaml`、`config/ssv.example.yaml`：运行时配置默认值与示例。
- `/mnt/work/ai-video-analysis/mediamtx/mediamtx.yml`：实际 MediaMTX path `/stream` 录像配置。
- `docs/Agent配置.md`：面向部署者说明证据目录、时间精度、故障语义和验收命令。

---

### Task 1: 共享配置与 source URI 边界

**Files:**
- Modify: `agent/src/ssv_agent/config.py`
- Modify: `agent/tests/test_config.py`
- Modify: `config/ssv.yaml`
- Modify: `config/ssv.example.yaml`

**Interfaces:**
- Produces: `AgentSourceConfig(id: str, uri: str)`；`RecordingEvidenceConfig`；`SsvConfig.sources`；`AgentConfig.recording_evidence`。
- Consumes: 既有根级 YAML `sources` 与 `agent.evidence_roots`。

- [ ] **Step 1: 写配置失败测试**

```python
def test_recording_evidence_requires_a_root_and_valid_window() -> None:
    with pytest.raises(ValidationError, match="evidence_roots"):
        SsvConfig.model_validate({"agent": {"recording_evidence": {"enabled": True}}})

    config = SsvConfig.model_validate({
        "sources": [{"id": "camera-01", "uri": "rtsp://localhost:8554/stream"}],
        "agent": {"evidence_roots": ["/var/lib/ssv/evidence"],
                  "recording_evidence": {"enabled": True}},
    })
    assert config.sources[0].uri == "rtsp://localhost:8554/stream"
    assert config.agent.recording_evidence.frame_offsets_ms == [-1000, 0, 1000]
```

补充未知 `recording_evidence` 键、`clip_before_ms=0`、`frame_offsets_ms=[-1000, 0, 3000]` 和 source 缺 `id`/`uri` 的失败断言。

- [ ] **Step 2: 运行失败测试**

Run: `uv run --project agent --extra dev pytest agent/tests/test_config.py -q`

Expected: FAIL，原因是 `recording_evidence` 和最小 source 模型尚不存在，或启用时未拒绝空 evidence roots。

- [ ] **Step 3: 实现最小严格模型**

在 `config.py` 增加：

```python
class AgentSourceConfig(_StrictConfigModel):
    id: str = Field(min_length=1)
    uri: str = Field(min_length=1)

class RecordingEvidenceConfig(WorkerConfig):
    enabled: bool = False
    clip_before_ms: int = Field(default=2500, gt=0)
    clip_after_ms: int = Field(default=2500, gt=0)
    frame_offsets_ms: list[int] = Field(default_factory=lambda: [-1000, 0, 1000])
```

将 `sources` 加入 Agent 保留的根键并在 `validate_shared_root()` 中把每个 source 缩减为
`id`、`uri`，使 runner 专有的 codec/decode 字段不触发 Agent strict 校验。为
`AgentConfig` 增加 `recording_evidence`，使用 model validator 保证启用时至少一个 evidence root、
三个严格递增 offset 全部落在 `[-clip_before_ms, clip_after_ms]`。

- [ ] **Step 4: 写入部署默认值**

在两个 `config/ssv*.yaml` 的 `agent` 下写入：

```yaml
recording_evidence:
  enabled: true
  clip_before_ms: 2500
  clip_after_ms: 2500
  frame_offsets_ms: [-1000, 0, 1000]
  poll_interval_ms: 1000
  lease_ms: 30000
  max_retries: 3
  retry_delay_ms: 2000
```

仅在实际 `config/ssv.yaml` 保留的 evidence root 非空时启用；示例配置的空 root 必须将
`enabled` 设为 `false`，并在注释中说明启用前的前提。

- [ ] **Step 5: 运行通过测试与审阅门**

Run: `uv run --project agent --extra dev pytest agent/tests/test_config.py -q`

Expected: PASS。

Run: `git diff --check`

Expected: PASS。报告配置字段、source 数据最小化行为和 YAML 默认值，保持变更未暂存，等待人工确认。

### Task 2: 受控录像提取器

**Files:**
- Create: `agent/src/ssv_agent/recording_evidence.py`
- Create: `agent/tests/test_recording_evidence.py`

**Interfaces:**
- Produces: `RecordingEvidenceArtifact`、`RecordingEvidenceExtractor.extract(case) -> tuple[RecordingEvidenceArtifact, ...]`、`RecordingEvidenceError(retryable: bool)`。
- Consumes: `EventCase`、`Mapping[str, AgentSourceConfig]`、`RecordingEvidenceConfig`、首个 evidence root 和注入的 `CommandRunner`。

- [ ] **Step 1: 写提取器失败测试**

```python
def test_extract_rejects_nested_rtsp_path_and_symlink_segment(tmp_path):
    extractor = _extractor(tmp_path, uri="rtsp://host/a/b")
    with pytest.raises(RecordingEvidenceError, match="safe path") as error:
        extractor.extract(_case())
    assert error.value.retryable is False

def test_extract_selects_covering_segments_and_commits_four_artifacts(tmp_path):
    runner = FakeCommandRunner(probe_durations={"1700000000-1.mp4": 5.0})
    artifacts = _extractor(tmp_path, runner=runner).extract(_case(timestamp_ms=1_700_000_002_500))
    assert [item.kind for item in artifacts] == ["clip", "frame", "frame", "frame"]
    assert all(item.path.is_file() for item in artifacts)
    assert runner.calls[0][0] == "ffprobe"
    assert all("shell" not in call.kwargs for call in runner.calls)
```

补充窗口未覆盖时 `retryable=True`、根外普通文件/符号链接拒绝、ffmpeg 非零退出清理临时目录、
哈希稳定和错误不暴露绝对路径。用 `pytest.mark.skipif(shutil.which("ffmpeg") is None)` 的
小型 lavfi 视频生成 fixture 验证真实 ffmpeg 可写 5 秒 MP4 和三张 JPEG。

- [ ] **Step 2: 运行失败测试**

Run: `uv run --project agent --extra dev pytest agent/tests/test_recording_evidence.py -q`

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现路径、分段与 subprocess seam**

定义不可变数据模型与命令 seam：

```python
@dataclass(frozen=True)
class RecordingEvidenceArtifact:
    kind: Literal["clip", "frame"]
    path: Path
    sha256: str
    mime_type: str
    size: int
    mtime: float

class RecordingEvidenceError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None: ...
```

`RecordingEvidenceExtractor` 解析 source URI、选取 `recordings/<path>` 下以
`<epoch>-<microseconds>.mp4` 命名且可由 `ffprobe` 读取的普通文件。文件名 epoch 与
probe duration 必须覆盖完整窗口；未覆盖或最后段未完成返回可重试错误。所有 resolved
输入必须仍在录像目录内且不是符号链接。

使用 `tempfile.mkdtemp(dir=derived_parent, prefix=f".{safe_event_id}.")` 创建临时目录。
为 concat demuxer 写入仅包含受控绝对文件名的 list 文件；调用
`ffmpeg -f concat -safe 0 -i list -ss ... -t 5 -c:v libx264 -an context.mp4`，再用同一
拼接输入在三个 offset 各生成一张 JPEG。任一命令超时或非零时删除临时目录并抛出
`RecordingEvidenceError`；成功时 fsync 文件、计算 SHA-256、写 `manifest.json`（含
`time_basis: wall_clock_approximate`、输入文件基名、请求/实际区间），最后 `os.replace()`
为 `derived/<event_id>`。

- [ ] **Step 4: 运行通过测试与审阅门**

Run: `uv run --project agent --extra dev pytest agent/tests/test_recording_evidence.py -q`

Expected: PASS。

Run: `git diff --check`

Expected: PASS。报告 command runner seam、所有路径验证与真实 ffmpeg fixture 状态；保持变更未暂存，等待人工确认。

### Task 3: durable job 迁移与取证准入事务

**Files:**
- Modify: `agent/src/ssv_agent/event_store/schema.py`
- Modify: `agent/src/ssv_agent/event_store/ledger.py`
- Modify: `agent/tests/test_event_ledger.py`

**Interfaces:**
- Produces: `JobKind.EVIDENCE_EXTRACT`；`EventLedger.complete_evidence_extract_job(job, worker_id, evidence)`；`EventLedger.fail_evidence_extract_job(...)`。
- Consumes: `RecordingEvidenceConfig`、`RecordingEvidenceArtifact(kind, path, sha256, mime_type, size, mtime)` 与既有 lease fence。

- [ ] **Step 1: 写迁移与准入失败测试**

```python
def test_enabled_recording_evidence_creates_only_delayed_extract_job(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "_now_ms", lambda: 1_000)
    with EventLedger(tmp_path / "events.db", recording_evidence=_enabled_config()) as ledger:
        outcome = ledger.record(_context())
    assert {job.kind for job in outcome.jobs} == {JobKind.EVIDENCE_EXTRACT}
    assert outcome.jobs[0].available_at_ms == _context().timestamp_ms + 2500

def test_schema_migration_preserves_legacy_review_and_index_jobs(tmp_path):
    db_path = _create_legacy_job_database(tmp_path)
    with EventLedger(db_path) as ledger:
        assert ledger.claim_job(JobKind.REVIEW, "r", 1000) is not None
        assert ledger.claim_job(JobKind.INDEX, "i", 1000) is not None
```

再覆盖完成取证后恰好登记四条 evidence 并创建一次 review、重复完成不重复创建、取证 job
失败到上限后事件为 `manual_review` 且不存在 review job。

- [ ] **Step 2: 运行失败测试**

Run: `uv run --project agent --extra dev pytest agent/tests/test_event_ledger.py -q`

Expected: FAIL，原因是 `evidence_extract` 不是合法 job kind，历史 CHECK 约束和 ledger
准入逻辑尚未迁移。

- [ ] **Step 3: 迁移 SQLite CHECK 约束**

在 `migrate_schema()` 中检查 `sqlite_master.sql` 的 `durable_jobs` 定义；旧表缺少
`evidence_extract` 时，在一个事务中执行：创建带
`CHECK (kind IN ('review', 'index', 'evidence_extract'))` 的 `durable_jobs_new`、按全部列
复制旧行、删除旧表、重命名新表、重建 `idx_durable_jobs_claim`。复制列必须包含
`job_id`、kind、entity identity、state、attempts、lease、available、last_error、created 和
updated，保留 existing foreign key 与唯一约束。

- [ ] **Step 4: 改造 EventLedger 准入和完成事务**

`EventLedger.__init__()` 接受 `recording_evidence: RecordingEvidenceConfig`。`record()` 在
启用时只插入 `JobKind.EVIDENCE_EXTRACT`，`available_at_ms` 为
`context.timestamp_ms + clip_after_ms`；禁用时保持原有 review/index 行为。

实现以下责任明确的方法：

```python
def complete_evidence_extract_job(
    self, job: DurableJob, worker_id: str,
    artifacts: tuple[RecordingEvidenceArtifact, ...],
) -> None: ...

def fail_evidence_extract_job(
    self, job: DurableJob, worker_id: str, error: str,
    max_retries: int, retry_delay_ms: int,
) -> JobState: ...
```

前者验证 `EVIDENCE_EXTRACT` lease、事件 revision 和恰好 1 个 clip/3 个 frame；调用同一
transaction 内的受控 evidence 注册函数、插入 review job、完成 extract job。后者复用
fenced `fail_job()`；返回 `DEAD` 时在同一 transaction 将 event status 改为
`manual_review`。Artifact 路径必须走 `resolve_evidence_path()`，并保存 MIME、size、mtime、
SHA-256；禁止信任未解析路径。

- [ ] **Step 5: 运行通过测试与审阅门**

Run: `uv run --project agent --extra dev pytest agent/tests/test_event_ledger.py -q`

Expected: PASS。报告迁移 DDL、旧任务保留测试与 review 准入变化；保持变更未暂存，等待人工确认。

Run: `git diff --check`

Expected: PASS。


### Task 4: evidence extract worker 与 Agent 生命周期

**Files:**
- Modify: `agent/src/ssv_agent/workers.py`
- Modify: `agent/src/ssv_agent/service.py`
- Modify: `agent/tests/test_workers.py`
- Modify: `agent/tests/test_service.py`
- Modify: `agent/src/ssv_agent/prompt.py`

**Interfaces:**
- Produces: `RecordingEvidenceWorker.run_once() -> bool`、`AgentService._start_recording_evidence_worker()`。
- Consumes: `RecordingEvidenceExtractor`、`EventLedger.complete_evidence_extract_job()`、`AgentConfig.recording_evidence`。

- [ ] **Step 1: 写 worker 与服务失败测试**

```python
def test_recording_worker_completes_extract_before_review_is_claimable(tmp_path):
    worker = RecordingEvidenceWorker(ledger_factory=_ledger_factory(tmp_path), extractor=_fake_extractor(), ...)
    assert worker.run_once() is True
    with _ledger(tmp_path) as ledger:
        assert ledger.claim_job(JobKind.REVIEW, "review", 1000) is not None

def test_agent_service_starts_enabled_recording_worker_before_consumer():
    runtime = AgentService(_config(recording_evidence={"enabled": True}),
                           recording_evidence_worker_factory=lambda **_: recording_worker, ...)
    runtime.start()
    assert recording_worker.started.wait(1)
```

补充 retryable 与不可重试错误分别调用 `fail_evidence_extract_job()`、达到重试上限不调用
extractor、停止 event 阻止下一个领取；提示词断言要求先调用 `evidence_reader` 并将
`wall_clock_approximate` 当作近似上下文而非检测同帧。

- [ ] **Step 2: 运行失败测试**

Run: `uv run --project agent --extra dev pytest agent/tests/test_workers.py agent/tests/test_service.py -q`

Expected: FAIL，`RecordingEvidenceWorker` 和 service factory seam 不存在。

- [ ] **Step 3: 实现 worker、服务注入与提示词约束**

参照 `ReviewWorker.run_once()` 的 lease 生命周期实现 `RecordingEvidenceWorker`：领取
`JobKind.EVIDENCE_EXTRACT`，调用 `_mark_exhausted_attempt_dead()`，读取 `EventCase`，在
extract 前后使用 `_LeaseHeartbeat.checkpoint()`，成功时调用
`complete_evidence_extract_job()`，异常时调用 `fail_evidence_extract_job()`。日志只包含
`event_id`、job ID、source 和错误类别，不能输出原始绝对路径。

为 `AgentService.__init__()` 增加 `recording_evidence_worker_factory` 与
`recording_evidence_extractor_factory` 注入 seam。在 `_start_recording_evidence_worker()` 中以
`config.sources` 建立 source ID 映射、传入 evidence root 和 recording worker 参数，并在
启动 review/index 前启动该 worker；禁用时不创建它。`_ledger_factory()` 必须把 recording
config 传给每个独立 SQLite connection。

在提示词的证据步骤中明确：先使用 `evidence_reader` 读取所有可用 evidence；录像帧/clip
只支持事件墙钟附近的上下文，不得声称是检测帧或补造不可见事实。

- [ ] **Step 4: 运行通过测试与审阅门**

Run: `uv run --project agent --extra dev pytest agent/tests/test_workers.py agent/tests/test_service.py agent/tests/test_review_context.py -q`

Expected: PASS。

Run: `git diff --check`

Expected: PASS。报告 worker 所有权、lease 边界、启动顺序与提示词语义；保持变更未暂存，等待人工确认。

### Task 5: MediaMTX 部署配置、文档与端到端验收

**Files:**
- Modify: `/mnt/work/ai-video-analysis/mediamtx/mediamtx.yml`
- Modify: `docs/Agent配置.md`
- Modify: `agent/tests/test_evidence_reader.py`

**Interfaces:**
- Consumes: `paths.stream`、`agent.evidence_roots[0]`、已登记的 clip/frame EvidenceRef。
- Produces: MediaMTX fMP4 `recordings/stream` 与部署者可执行的验收步骤。

- [ ] **Step 1: 写 evidence reader 回归测试**

```python
def test_evidence_reader_copies_all_registered_context_frames_and_clip(tmp_path, monkeypatch):
    evidence_ids = _register_derived_context_evidence(tmp_path)
    payload = json.loads(evidence_reader_tool.func(None, "case-1"))
    assert {item["evidence_id"] for item in payload["found"]} == set(evidence_ids)
    assert payload["view_image_path"] is not None
```

验证模型仍只能得到虚拟输出路径而非 `recordings` 或宿主机绝对路径。

- [ ] **Step 2: 运行失败测试**

Run: `uv run --project agent --extra dev pytest agent/tests/test_evidence_reader.py -q`

Expected: 如果现有 reader 已满足回归，PASS；记录此为现有安全 seam 的行为锁定测试，不为制造失败修改代码。

- [ ] **Step 3: 改造实际 MediaMTX path**

将现有 `paths.camera` 的 `runOnInit` 测试视频启动器迁移为 `paths.stream`，保持其发布目标
`rtsp://localhost:8554/stream`。在同一 `paths.stream` 增加：

```yaml
source: publisher
record: true
recordPath: /mnt/work/ai-video-analysis/ssv/artifacts/evidence/recordings/%path/%s-%f
recordFormat: fmp4
recordPartDuration: 1s
recordSegmentDuration: 5s
recordDeleteAfter: 1d
```

不启用 playback/API，不在 `all_others` 开启全局录像。先通过 MediaMTX 配置校验启动方式
验证语法，确认 `/stream` 是唯一录制 path。

- [ ] **Step 4: 补充中文部署文档**

在 `docs/Agent配置.md` 增加“录像上下文证据”一节，列出目录布局、MediaMTX 外部配置文件、
`recording_evidence` 字段、5 秒窗口、三帧偏移、1 天原始保留和派生长期保留。明确
`timestamp_ms` 是墙钟近似值；缺段/超时进入 `manual_review`，不能作为模型无图结论。

- [ ] **Step 5: 运行完整验证与人工验收**

Run: `uv run --project agent --extra dev pytest agent/tests/test_config.py agent/tests/test_event_ledger.py agent/tests/test_recording_evidence.py agent/tests/test_workers.py agent/tests/test_service.py agent/tests/test_evidence_reader.py -q`

Expected: PASS。

Run: `git diff --check`

Expected: PASS。

人工验收：启动 MediaMTX，确认 `artifacts/evidence/recordings/stream` 产生约 5 秒 fMP4；
运行 Agent 并注入一个带当前墙钟 `timestamp_ms` 的 Redis 事件；确认生成
`derived/<event_id>/context.mp4`、三张 JPEG、manifest、四条 EvidenceRef 和一个 review job。记录
抽取帧与肉眼事件时刻的偏差，不将其记为检测帧精度验收。

报告实际修改的外部配置、完整测试结果和人工验收结果；保持所有变更未暂存，等待人工确认。

## Plan Self-Review

- Spec coverage：Task 1 覆盖严格配置和 URI 映射；Task 2 覆盖持久 job 迁移、幂等和准入；Task 3 覆盖安全输入、原子产物、哈希、manifest、5 秒 clip 和三帧；Task 4 覆盖 worker、重试、lease、服务生命周期和模型语义；Task 5 覆盖真实 MediaMTX `/stream`、部署文档和端到端验收。
- Placeholder scan：所有任务均包含具体文件、接口、测试和命令；没有 TODO、TBD 或未定义的后续工作。
- Type consistency：`RecordingEvidenceConfig` 由 Task 1 定义；`RecordingEvidenceArtifact` 和 extractor 由 Task 2 定义；`JobKind.EVIDENCE_EXTRACT` 和 ledger 完成/失败接口由 Task 3 定义；Task 4 只消费前述接口。
