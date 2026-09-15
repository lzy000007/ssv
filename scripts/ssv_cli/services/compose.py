"""Docker Compose adapter for local development services."""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

from ..config import RedisSettings
from ..context import ProjectContext
from ..output import CliError, info
from ..process import require_command, run_command
from .redis_admin import RedisConnection, RedisError

_LOCAL_QDRANT_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _compose_argv(context: ProjectContext, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(context.compose_file), *args]


def _compose(
    context: ProjectContext,
    *args: str,
    capture_output: bool = False,
    environment: dict[str, str] | None = None,
):
    require_command("docker", "请安装 Docker 和 Docker Compose plugin")
    result = run_command(
        context,
        _compose_argv(context, *args),
        capture_output=capture_output,
        environment=environment,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() if capture_output else ""
        raise CliError(f"docker compose 命令失败: {' '.join(args)}{': ' + detail if detail else ''}")
    return result


def _is_running(context: ProjectContext) -> bool:
    result = run_command(
        context,
        _compose_argv(context, "ps", "--format", "json"),
        capture_output=True,
    )
    if result.returncode != 0:
        return False
    output = result.stdout.strip()
    if not output:
        return False
    records: list[object] = []
    try:
        parsed = json.loads(output)
        records = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in output.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for record in records:
        if isinstance(record, dict):
            state = str(record.get("State", record.get("state", ""))).lower()
            status = str(record.get("Status", record.get("status", ""))).lower()
            if state == "running" or status.startswith("up") or "healthy" in status:
                return True
    return False


def _local_qdrant_port(qdrant_url: str | None) -> int:
    if not qdrant_url:
        return 6333
    try:
        parsed = urlsplit(qdrant_url)
        if parsed.hostname not in _LOCAL_QDRANT_HOSTS:
            return 6333
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise CliError(f"Qdrant URL 无效: {qdrant_url}") from exc


def _qdrant_ready(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1) as response:
            return 200 <= response.status < 300
    except OSError:
        return False


def start_redis(
    context: ProjectContext,
    settings: RedisSettings,
    *,
    qdrant_url: str | None = None,
) -> int:
    qdrant_port = _local_qdrant_port(qdrant_url)
    compose_environment = context.child_environment(
        REDIS_PORT=str(settings.port),
        QDRANT_PORT=str(qdrant_port),
    )
    _compose(context, "up", "-d", environment=compose_environment)
    info("等待 Redis 就绪...")
    last_error: Exception | None = None
    for _ in range(15):
        try:
            with RedisConnection(settings) as connection:
                connection.execute("PING")
            info("Redis 已就绪")
            break
        except RedisError as exc:  # connection can race container startup
            last_error = exc
            time.sleep(1)
    else:
        raise CliError(f"Redis 启动超时: {last_error}")

    info("等待 Qdrant 就绪...")
    for _ in range(15):
        if _qdrant_ready(qdrant_port):
            info("Qdrant 已就绪")
            return 0
        time.sleep(1)
    raise CliError("Qdrant 启动超时")


def stop_redis(context: ProjectContext) -> int:
    if not _is_running(context):
        info("Redis 未在运行")
        return 0
    _compose(context, "down")
    info("Redis 已停止")
    return 0
