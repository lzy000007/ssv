"""Docker infrastructure lifecycle commands."""

from __future__ import annotations

from argparse import Namespace

from ..config import load_runtime_config
from ..context import ProjectContext
from ..output import header
from ..services.compose import (
    start_infrastructure,
    status_infrastructure,
    stop_infrastructure,
)


def _infrastructure_config(context: ProjectContext, args: Namespace):
    return load_runtime_config(
        context,
        path=getattr(args, "config", None),
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        db=getattr(args, "db", None),
    )


def start(context: ProjectContext, args: Namespace) -> int:
    header("启动 Docker Redis 和 Qdrant")
    settings = _infrastructure_config(context, args)
    return start_infrastructure(context, settings.redis, qdrant_url=settings.qdrant_url)


def stop(context: ProjectContext, _args: Namespace) -> int:
    header("停止 Docker Redis 和 Qdrant")
    return stop_infrastructure(context)


def status(context: ProjectContext, _args: Namespace) -> int:
    header("Docker Redis 和 Qdrant 状态")
    return status_infrastructure(context)
