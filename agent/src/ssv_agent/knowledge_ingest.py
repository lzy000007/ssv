"""Build the local Qdrant rule index from ``agent/knowledge``."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from ssv_agent.config import SsvConfig, load_config
from ssv_agent.knowledge.backends.qdrant import ingest_rules


_AGENT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_agent_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else _AGENT_ROOT / path


def _select_qdrant_target(
    config: SsvConfig | None,
    requested: Path | None,
) -> tuple[Path, str | None]:
    """选择互斥的 Qdrant 本地路径或服务 URL。"""
    if requested is not None:
        return requested, None
    if config is not None:
        knowledge = config.agent.knowledge
        return Path(knowledge.qdrant_path), knowledge.qdrant_url
    return Path("data/qdrant"), None


def _discover_config() -> Path | None:
    candidates = []
    if env_path := os.getenv("SSV_CONFIG_PATH"):
        candidates.append(Path(env_path))
    candidates.extend(
        (
            Path("ssv.yaml"),
            Path("config/ssv.yaml"),
            _AGENT_ROOT.parent / "config" / "ssv.yaml",
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_agent_dotenv() -> None:
    """加载当前目录、Agent 目录或仓库根目录的本地环境配置。"""
    for candidate in (
        Path.cwd() / ".env",
        _AGENT_ROOT / ".env",
        _AGENT_ROOT.parent / ".env",
    ):
        if candidate.is_file():
            load_dotenv(candidate)
            return


def main() -> None:
    _load_agent_dotenv()
    parser = argparse.ArgumentParser(description="Ingest local rules into Qdrant")
    parser.add_argument("--config", type=Path, default=None, help="Agent YAML 配置路径")
    parser.add_argument("--knowledge-dir", type=Path, default=None)
    parser.add_argument(
        "--embedding-backend",
        choices=("mock", "openai_compatible", "bge_m3"),
        default=None,
        help="覆盖配置中的 embedding backend",
    )
    parser.add_argument("--embedding-model", default=None, help="覆盖配置中的 embedding model")
    parser.add_argument("--qdrant-path", type=Path, default=None, help="覆盖 Qdrant 本地路径")
    args = parser.parse_args()

    config_path = args.config or _discover_config()
    config = load_config(config_path) if config_path else None
    backend = args.embedding_backend or (
        config.agent.indexing.embedding_backend
        if config is not None
        else os.getenv("SSV_EMBEDDING_BACKEND", "mock")
    )
    model = args.embedding_model
    if model is None and config is not None and args.embedding_backend is None:
        model = config.agent.indexing.embedding_model
    if model is None and config is None:
        model = os.getenv("SSV_EMBEDDING_MODEL") or None
    os.environ["SSV_EMBEDDING_BACKEND"] = backend
    if model is None:
        os.environ.pop("SSV_EMBEDDING_MODEL", None)
    else:
        os.environ["SSV_EMBEDDING_MODEL"] = model
    embedding_base_url = (
        config.agent.indexing.embedding_base_url if config is not None else None
    )
    if embedding_base_url is None:
        os.environ.pop("SSV_EMBEDDING_BASE_URL", None)
    else:
        os.environ["SSV_EMBEDDING_BASE_URL"] = embedding_base_url
    query_text_type = (
        config.agent.indexing.query_text_type if config is not None else None
    )
    if query_text_type is None:
        os.environ.pop("SSV_EMBEDDING_QUERY_TEXT_TYPE", None)
    else:
        os.environ["SSV_EMBEDDING_QUERY_TEXT_TYPE"] = query_text_type
    qdrant_path, qdrant_url = _select_qdrant_target(config, args.qdrant_path)
    os.environ["SSV_QDRANT_PATH"] = str(_resolve_agent_path(qdrant_path).resolve())
    if qdrant_url is None:
        os.environ.pop("SSV_QDRANT_URL", None)
    else:
        os.environ["SSV_QDRANT_URL"] = qdrant_url

    knowledge_dir = (
        _resolve_agent_path(args.knowledge_dir)
        if args.knowledge_dir is not None
        else None
    )
    result = asyncio.run(ingest_rules(knowledge_dir) if knowledge_dir else ingest_rules())
    if not result.success:
        raise SystemExit(result.error_message or "rule ingestion failed")
    print(
        f"ingested rule chunks={result.chunks_count} source={result.document_id} "
        f"backend={backend} qdrant={qdrant_url or os.environ['SSV_QDRANT_PATH']}"
    )


if __name__ == "__main__":
    main()
