from __future__ import annotations

from pathlib import Path

from ssv_agent.config import SsvConfig
from ssv_agent.knowledge_ingest import _select_qdrant_path


def test_select_qdrant_path_prefers_requested_path(monkeypatch) -> None:
    config = SsvConfig.model_validate(
        {"agent": {"knowledge": {"qdrant_path": "from-config"}}}
    )
    monkeypatch.setenv("SSV_QDRANT_PATH", "from-environment")

    assert _select_qdrant_path(config, Path("from-cli")) == Path("from-cli")


def test_select_qdrant_path_prefers_environment_over_config(monkeypatch) -> None:
    config = SsvConfig.model_validate(
        {"agent": {"knowledge": {"qdrant_path": "from-config"}}}
    )
    monkeypatch.setenv("SSV_QDRANT_PATH", "from-environment")

    assert _select_qdrant_path(config, None) == Path("from-environment")


def test_select_qdrant_path_uses_config_then_default(monkeypatch) -> None:
    config = SsvConfig.model_validate(
        {"agent": {"knowledge": {"qdrant_path": "from-config"}}}
    )
    monkeypatch.delenv("SSV_QDRANT_PATH", raising=False)

    assert _select_qdrant_path(config, None) == Path("from-config")
    assert _select_qdrant_path(None, None) == Path("data/qdrant")
