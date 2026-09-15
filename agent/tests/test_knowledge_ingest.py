from __future__ import annotations

from pathlib import Path

from ssv_agent.config import SsvConfig
from ssv_agent.knowledge_ingest import _discover_config, _select_qdrant_target


def test_discover_config_prefers_environment_path(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path("ssv.yaml").touch()
    environment_path = tmp_path / "environment.yaml"
    environment_path.touch()
    monkeypatch.setenv("SSV_CONFIG_PATH", str(environment_path))

    assert _discover_config() == environment_path


def test_select_qdrant_target_prefers_requested_path(monkeypatch) -> None:
    config = SsvConfig.model_validate(
        {
            "agent": {
                "knowledge": {
                    "qdrant_path": "from-config",
                    "qdrant_url": "https://qdrant.example.test",
                }
            }
        }
    )
    monkeypatch.setenv("SSV_QDRANT_PATH", "from-environment")

    assert _select_qdrant_target(config, Path("from-cli")) == (Path("from-cli"), None)


def test_select_qdrant_target_uses_config_instead_of_environment(monkeypatch) -> None:
    config = SsvConfig.model_validate(
        {"agent": {"knowledge": {"qdrant_path": "from-config"}}}
    )
    monkeypatch.setenv("SSV_QDRANT_PATH", "from-environment")

    assert _select_qdrant_target(config, None) == (Path("from-config"), None)


def test_select_qdrant_target_uses_config_then_default(monkeypatch) -> None:
    config = SsvConfig.model_validate(
        {"agent": {"knowledge": {"qdrant_path": "from-config"}}}
    )
    monkeypatch.delenv("SSV_QDRANT_PATH", raising=False)

    assert _select_qdrant_target(config, None) == (Path("from-config"), None)
    assert _select_qdrant_target(None, None) == (Path("data/qdrant"), None)
