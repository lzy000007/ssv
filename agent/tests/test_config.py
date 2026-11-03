from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ssv_agent import cli
from ssv_agent.config import (
    AgentSourceConfig,
    RecordingEvidenceConfig,
    RedisConfig,
    SsvConfig,
    load_config,
)


def test_load_config_uses_yaml_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSV_CONFIG_PATH", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    path = tmp_path / "ssv.yaml"
    path.write_text(
        """
version: "2.0"
logging:
  python_log_level: "DEBUG"
redis:
  host: "redis.local"
  port: 6380
  stream_key: "custom:events"
agent:
  state_machine_timeout: 120
  max_retries: 5
  dedup_enabled: false
  dedup_cooldown_seconds: 12.5
  knowledge:
    rules_dir: "knowledge/custom-rules"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.version == "2.0"
    assert cfg.logging.python_log_level == "DEBUG"
    assert cfg.redis.host == "redis.local"
    assert cfg.redis.port == 6380
    assert cfg.redis.stream_key == "custom:events"
    assert cfg.agent.state_machine_timeout == 120
    assert cfg.agent.max_retries == 5
    assert cfg.agent.dedup_enabled is False
    assert cfg.agent.dedup_cooldown_seconds == 12.5
    assert cfg.agent.knowledge.rules_dir == "knowledge/custom-rules"


def test_knowledge_rules_dir_defaults_to_versioned_catalog() -> None:
    assert SsvConfig().agent.knowledge.rules_dir == "knowledge/rules"

    for rules_dir in ("", "  "):
        with pytest.raises(ValidationError):
            SsvConfig.model_validate({"agent": {"knowledge": {"rules_dir": rules_dir}}})


def test_agent_config_evidence_roots_default_to_cache_and_requires_absolute_paths(
    tmp_path: Path,
) -> None:
    assert SsvConfig().agent.evidence_roots == ["/var/lib/ssv/evidence-cache"]

    root = tmp_path / "evidence"
    config = SsvConfig.model_validate(
        {
            "evidence_cache": {"directory": str(root)},
            "agent": {"evidence_roots": [str(root)]},
        }
    )

    assert config.agent.evidence_roots == [str(root)]
    for invalid in (["relative"], [""], [str(root), "../outside"]):
        with pytest.raises(ValidationError, match="absolute"):
            SsvConfig.model_validate({"agent": {"evidence_roots": invalid}})


def test_recording_evidence_requires_a_root_and_valid_window() -> None:
    empty_roots = SsvConfig.model_validate({"agent": {"evidence_roots": []}})
    assert empty_roots.agent.evidence_roots == []

    with pytest.raises(ValidationError, match="matching agent.evidence_roots"):
        SsvConfig.model_validate(
            {
                "evidence_cache": {"directory": "/tmp/custom-cache"},
                "agent": {"evidence_roots": []},
            }
        )

    config = SsvConfig.model_validate(
        {
            "evidence_cache": {"directory": "/var/lib/ssv/evidence"},
            "sources": [
                {
                    "id": "camera-01",
                    "uri": "rtsp://localhost:8554/stream",
                    "codec": "h264",
                    "decode": {"mode": "auto"},
                }
            ],
            "agent": {
                "evidence_roots": ["/var/lib/ssv/evidence"],
                "recording_evidence": {},
            },
        }
    )

    assert config.sources == [AgentSourceConfig(id="camera-01", uri="rtsp://localhost:8554/stream")]
    assert config.agent.recording_evidence == RecordingEvidenceConfig()

    for recording_evidence in (
        {"clip_before_ms": 0},
        {"clip_after_ms": 0},
        {"clip_after_ms": -1},
        {"clip_before_ms": 999},
        {"clip_after_ms": 999},
        {"merge_gap_ms": 0},
        {"lost_grace_ms": -1},
        {"silence_timeout_ms": 0},
        {"max_episode_ms": 3000, "merge_gap_ms": 3000},
        {"unknown": True},
    ):
        with pytest.raises(ValidationError):
            SsvConfig.model_validate(
                {
                    "agent": {
                        "evidence_roots": ["/var/lib/ssv/evidence"],
                        "recording_evidence": recording_evidence,
                    }
                }
            )


def test_recording_evidence_requires_shared_cache_inside_allowed_root() -> None:
    config = SsvConfig.model_validate(
        {
            "evidence_cache": {
                "directory": "/var/lib/ssv/evidence-cache",
            },
            "agent": {
                "evidence_roots": ["/var/lib/ssv"],
            },
        }
    )

    assert config.evidence_cache.directory == "/var/lib/ssv/evidence-cache"
    assert config.agent.recording_evidence == RecordingEvidenceConfig()

    default_cache_without_agent_roots = SsvConfig.model_validate(
        {"agent": {"evidence_roots": []}}
    )
    assert default_cache_without_agent_roots.evidence_cache.directory == (
        "/var/lib/ssv/evidence-cache"
    )

    with pytest.raises(ValidationError, match="recording_evidence"):
        SsvConfig.model_validate(
            {
                "evidence_cache": {"directory": "/tmp/evidence-cache"},
                "agent": {"evidence_roots": ["/var/lib/ssv"]},
            }
        )

    with pytest.raises(ValidationError):
        SsvConfig.model_validate(
            {
                "evidence_cache": {
                    "directory": "relative-cache",
                }
            }
        )


def test_recording_evidence_reuses_worker_runtime_settings() -> None:
    config = SsvConfig.model_validate(
        {
            "evidence_cache": {
                "directory": "/var/lib/ssv/evidence",
            },
            "agent": {
                "evidence_roots": ["/var/lib/ssv/evidence"],
                "recording_evidence": {
                    "poll_interval_ms": 250,
                    "lease_ms": 5_000,
                    "max_retries": 4,
                    "retry_delay_ms": 2_000,
                },
            },
        }
    )

    recording = config.agent.recording_evidence
    assert recording.poll_interval_ms == 250
    assert recording.lease_ms == 5_000
    assert recording.max_retries == 4
    assert recording.retry_delay_ms == 2_000


def test_recording_evidence_rejects_unknown_runtime_keys() -> None:
    with pytest.raises(ValidationError):
        SsvConfig.model_validate(
            {
                "agent": {
                    "evidence_roots": ["/var/lib/ssv/evidence"],
                    "recording_evidence": {
                        "frame_offsets_ms": [-1000, 0, 1000],
                    },
                }
            }
        )


def test_recording_evidence_rejects_provider_key() -> None:
    with pytest.raises(ValidationError):
        RecordingEvidenceConfig.model_validate({"provider": "ssv_cache"})


def test_recording_evidence_defaults_are_constant() -> None:
    defaults = RecordingEvidenceConfig()

    assert defaults.poll_interval_ms == 1000
    assert defaults.lease_ms == 30_000
    assert defaults.max_retries == 3
    assert defaults.retry_delay_ms == 2_000
    assert defaults.clip_before_ms == 2500
    assert defaults.clip_after_ms == 2500
    assert defaults.merge_gap_ms == 3_000
    assert defaults.lost_grace_ms == 3_000
    assert defaults.silence_timeout_ms == 30_000
    assert defaults.max_episode_ms == 30_000


@pytest.mark.parametrize(
    "source",
    [
        {"uri": "rtsp://localhost:8554/stream"},
        {"id": "camera-01"},
        {"id": "", "uri": "rtsp://localhost:8554/stream"},
        {"id": "camera-01", "uri": ""},
    ],
)
def test_agent_sources_require_non_empty_id_and_uri(source: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        SsvConfig.model_validate({"sources": [source]})


def test_source_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match=r"sources\[\]\.id values must be unique"):
        SsvConfig.model_validate(
            {
                "sources": [
                    {"id": "camera-01", "uri": "rtsp://localhost/1"},
                    {"id": "camera-01", "uri": "rtsp://localhost/2"},
                ]
            }
        )


def test_source_configuration_rejects_wvp_channel_mapping() -> None:
    with pytest.raises(ValidationError, match="wvp_channel_id"):
        SsvConfig.model_validate(
            {
                "sources": [
                    {
                        "id": "camera-01",
                        "uri": "rtsp://localhost/stream",
                        "wvp_channel_id": "34020000001320000001",
                    }
                ]
            }
        )


def test_redis_reclaim_settings_are_strict_and_support_an_optional_consumer_name() -> None:
    defaults = RedisConfig()
    config = RedisConfig(
        reclaim_idle_ms=12_000,
        reclaim_batch_size=25,
        consumer_name="site-a-consumer",
    )

    assert defaults.reclaim_idle_ms == 60_000
    assert defaults.reclaim_batch_size == 10
    assert defaults.consumer_name is None
    assert config.reclaim_idle_ms == 12_000
    assert config.reclaim_batch_size == 25
    assert config.consumer_name == "site-a-consumer"

    for invalid in (
        {"reclaim_idle_ms": 0},
        {"reclaim_batch_size": 0},
        {"reclaim_batch_size": 101},
        {"consumer_name": ""},
    ):
        with pytest.raises(ValidationError):
            RedisConfig(**invalid)


def test_load_config_parses_strict_review_and_index_worker_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ssv.yaml"
    path.write_text(
        """
version: "2.0"
agent:
  review:
    enabled: true
    poll_interval_ms: 250
    lease_ms: 5000
    max_retries: 4
    retry_delay_ms: 100
    policy_id: "site-safety.v2"
    recursion_limit: 384
  indexing:
    enabled: true
    poll_interval_ms: 500
    lease_ms: 6000
    max_retries: 5
    retry_delay_ms: 200
    embedding_backend: "bge_m3"
    embedding_model: "/models/bge-m3"
  reporting:
    enabled: true
    poll_interval_ms: 750
    lease_ms: 8000
    max_retries: 6
    retry_delay_ms: 250
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.agent.review.enabled is True
    assert cfg.agent.review.policy_id == "site-safety.v2"
    assert cfg.agent.review.recursion_limit == 384
    assert cfg.agent.indexing.enabled is True
    assert cfg.agent.indexing.embedding_backend == "bge_m3"
    assert cfg.agent.indexing.embedding_model == "/models/bge-m3"
    assert cfg.agent.reporting.enabled is True
    assert cfg.agent.reporting.poll_interval_ms == 750
    assert cfg.agent.reporting.lease_ms == 8000
    assert cfg.agent.reporting.max_retries == 6
    assert cfg.agent.reporting.retry_delay_ms == 250


def test_review_step_budget_defaults_to_256() -> None:
    assert SsvConfig().agent.review.recursion_limit == 256


@pytest.mark.parametrize("invalid", [0, -1, True, 256.0, "256", None])
def test_review_step_budget_rejects_non_positive_or_non_integer_values(invalid) -> None:
    with pytest.raises(ValidationError, match="recursion_limit"):
        SsvConfig.model_validate({"agent": {"review": {"recursion_limit": invalid}}})


def test_reporting_worker_defaults_enabled_and_rejects_invalid_settings() -> None:
    defaults = SsvConfig().agent.reporting
    assert defaults.enabled is True
    assert defaults.poll_interval_ms == 1000
    assert defaults.lease_ms == 30_000
    assert defaults.max_retries == 3
    assert defaults.retry_delay_ms == 1000

    for invalid in (
        {"enabled": "true"},
        {"poll_interval_ms": 0},
        {"lease_ms": 0},
        {"max_retries": 0},
        {"retry_delay_ms": -1},
    ):
        with pytest.raises(ValidationError):
            SsvConfig.model_validate({"agent": {"reporting": invalid}})


def test_knowledge_config_has_safe_defaults_and_validates_threshold() -> None:
    defaults = SsvConfig().agent.knowledge
    assert defaults.backend == "local_markdown"
    assert defaults.rules_dir == "knowledge/rules"
    assert defaults.qdrant_path == "data/qdrant"
    assert defaults.min_score == 0.5

    configured = SsvConfig.model_validate(
        {
            "agent": {
                "knowledge": {
                    "backend": "qdrant",
                    "qdrant_path": "/var/lib/ssv/qdrant",
                    "min_score": 0.65,
                }
            }
        }
    )
    assert configured.agent.knowledge.backend == "qdrant"
    assert configured.agent.knowledge.min_score == 0.65

    with pytest.raises(ValidationError):
        SsvConfig.model_validate({"agent": {"knowledge": {"min_score": 1.1}}})


def test_load_config_applies_environment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ssv.yaml"
    path.write_text(
        'version: "2.0"\nredis:\n  host: yaml-host\n  port: 1111\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIS_HOST", "env-host")
    monkeypatch.setenv("REDIS_PORT", "2222")

    cfg = load_config(path)

    assert cfg.redis.host == "env-host"
    assert cfg.redis.port == 2222


def test_load_config_validates_environment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ssv.yaml"
    path.write_text('version: "2.0"\n', encoding="utf-8")
    monkeypatch.setenv("REDIS_PORT", "0")

    with pytest.raises(ValidationError):
        load_config(path)


def test_load_config_uses_config_directory_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SSV_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ssv.yaml").write_text(
        'version: "2.0"\nredis:\n  host: config-dir-host\n',
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.redis.host == "config-dir-host"


def test_load_config_rejects_v1(tmp_path: Path) -> None:
    path = tmp_path / "ssv.yaml"
    path.write_text('version: "1.0"\nredis:\n  host: redis.local\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(path)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "redis:\n  host: redis.local",
        "version: 2.0",
        'version: "2.0"\npipeline: {}',
        'version: "2.0"\npipline: {}',
        'version: "2.0"\nredis:\n  porr: 6380',
        'version: "2.0"\nredis:\n  port: "6380"',
    ],
)
def test_load_config_rejects_non_contract_yaml(tmp_path: Path, yaml_text: str) -> None:
    path = tmp_path / "ssv.yaml"
    path.write_text(yaml_text + "\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(path)


@pytest.mark.parametrize(
    "fragment",
    [
        "redis:\n  port: 0",
        "redis:\n  port: 65536",
        "redis:\n  db: -1",
        "agent:\n  state_machine_timeout: 0",
        "agent:\n  max_retries: -1",
        "agent:\n  dedup_cooldown_seconds: 0",
        "agent:\n  review:\n    lease_ms: 0",
        "agent:\n  review:\n    unknown: true",
        "agent:\n  indexing:\n    embedding_backend: remote",
    ],
)
def test_load_config_rejects_invalid_agent_owned_ranges(tmp_path: Path, fragment: str) -> None:
    path = tmp_path / "ssv.yaml"
    path.write_text(f'version: "2.0"\n{fragment}\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(path)


def test_load_config_accepts_complete_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    path = Path(__file__).resolve().parents[2] / "config" / "ssv.example.yaml"

    cfg = load_config(path)

    assert cfg.version == "2.0"
    assert cfg.redis.stream_key == "ssv:events"
    assert cfg.agent.max_retries == 3


def test_deployment_recording_evidence_defaults_are_active() -> None:
    config = SsvConfig.model_validate(
        {
            "evidence_cache": {
                "directory": "/var/lib/ssv/evidence",
            },
            "agent": {
                "evidence_roots": ["/var/lib/ssv/evidence"],
                "recording_evidence": {
                    "clip_before_ms": 2500,
                    "clip_after_ms": 2500,
                },
            },
        }
    )

    recording_evidence = config.agent.recording_evidence
    assert recording_evidence.clip_before_ms == 2500
    assert recording_evidence.clip_after_ms == 2500


def test_example_recording_evidence_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    example = load_config(Path(__file__).resolve().parents[2] / "config" / "ssv.example.yaml")

    assert example.evidence_cache.directory == "/var/lib/ssv/evidence-cache"
    assert example.agent.evidence_roots == ["/var/lib/ssv/evidence-cache"]
    assert example.agent.recording_evidence.clip_before_ms == 2500
    assert example.agent.recording_evidence.clip_after_ms == 2500


def test_load_config_missing_explicit_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yaml")


def test_load_config_does_not_use_example_as_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SSV_CONFIG_PATH", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ssv.example.yaml").write_text(
        "redis:\n  host: example-only-host\n",
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.redis.host == "localhost"


def test_cli_does_not_pass_example_as_default_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_paths: list[str | Path | None] = []
    monkeypatch.delenv("SSV_CONFIG_PATH", raising=False)
    monkeypatch.setattr(sys, "argv", ["ssv-agent"])
    monkeypatch.setattr(cli, "find_dotenv", lambda: "")
    monkeypatch.setattr(cli, "load_dotenv", lambda _: False)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda path=None: observed_paths.append(path) or SsvConfig(),
    )
    monkeypatch.setattr(cli, "setup_logging", lambda _: None)
    monkeypatch.setattr(cli, "run", lambda _: None)

    cli.main()

    assert observed_paths == [None]


def test_removed_evidence_switches_are_rejected() -> None:
    with pytest.raises(ValidationError, match="enabled"):
        SsvConfig.model_validate({"agent": {"recording_evidence": {"enabled": True}}})
    with pytest.raises(ValidationError, match="enabled"):
        SsvConfig.model_validate({"evidence_cache": {"enabled": False}})
