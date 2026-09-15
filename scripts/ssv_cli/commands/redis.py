"""Docker development-service lifecycle commands."""

from __future__ import annotations

from argparse import Namespace

from ..config import load_runtime_config
from ..context import ProjectContext
from ..output import header
from ..services.compose import start_redis, stop_redis


def _runtime_config(context: ProjectContext, args: Namespace):
    return load_runtime_config(
        context,
        path=getattr(args, "config", None),
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        db=getattr(args, "db", None),
        stream=getattr(args, "stream_key", None),
        group=getattr(args, "group", None),
    )


def start(context: ProjectContext, args: Namespace) -> int:
    header("启动 Docker Redis 和 Qdrant")
    runtime = _runtime_config(context, args)
    return start_redis(context, runtime.redis, qdrant_url=runtime.qdrant_url)


def stop(context: ProjectContext, _args: Namespace) -> int:
    header("停止 Docker Redis 和 Qdrant")
    return stop_redis(context)
