from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ssv_agent import cli
from ssv_agent.config import RedisConfig, SsvConfig, load_config


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


def test_agent_config_evidence_roots_default_empty_and_requires_absolute_paths(
    tmp_path: Path,
) -> None:
    assert SsvConfig().agent.evidence_roots == []

    root = tmp_path / "evidence"
    config = SsvConfig.model_validate({"agent": {"evidence_roots": [str(root)]}})

    assert config.agent.evidence_roots == [str(root)]
    for invalid in (["relative"], [""], [str(root), "../outside"]):
        with pytest.raises(ValidationError, match="absolute"):
            SsvConfig.model_validate({"agent": {"evidence_roots": invalid}})


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
  indexing:
    enabled: true
    poll_interval_ms: 500
    lease_ms: 6000
    max_retries: 5
    retry_delay_ms: 200
    embedding_backend: "bge_m3"
    embedding_model: "/models/bge-m3"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.agent.review.enabled is True
    assert cfg.agent.review.policy_id == "site-safety.v2"
    assert cfg.agent.indexing.enabled is True
    assert cfg.agent.indexing.embedding_backend == "bge_m3"
    assert cfg.agent.indexing.embedding_model == "/models/bge-m3"


def test_knowledge_config_has_safe_defaults_and_validates_threshold() -> None:
    defaults = SsvConfig().agent.knowledge
    assert defaults.backend == "local_markdown"
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
