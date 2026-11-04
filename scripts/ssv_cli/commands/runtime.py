"""SSV runtime state inspection and reset commands."""

from __future__ import annotations

import unicodedata
from argparse import Namespace
from collections.abc import Sequence

from ..config import RuntimeConfig, load_runtime_config
from ..context import ProjectContext
from ..output import CliError, header, info
from ..services.event_db import EventDbAdmin, EventDbError
from ..services.redis_admin import (
    RedisAdmin,
    RedisCleanupError,
    RedisConnection,
    RedisError,
)


def _runtime_config(context: ProjectContext, args: Namespace) -> RuntimeConfig:
    return load_runtime_config(
        context,
        path=getattr(args, "config", None),
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        db=getattr(args, "db", None),
        stream=getattr(args, "stream_key", None),
        group=getattr(args, "group", None),
    )


def _display_width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in value
    )


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    values = [[str(cell) for cell in row] for row in (headers, *rows)]
    widths = [
        max(_display_width(row[column]) for row in values)
        for column in range(len(headers))
    ]
    border = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    info(border)
    for row_index, row in enumerate(values):
        cells = " | ".join(
            cell + " " * (widths[column] - _display_width(cell))
            for column, (cell, width) in enumerate(zip(row, widths))
        )
        info(f"| {cells} |")
        if row_index == 0:
            info(border)
    info(border)


def status(context: ProjectContext, args: Namespace) -> int:
    runtime_config = _runtime_config(context, args)
    settings = runtime_config.redis
    header("SSV 运行态")
    redis_state = None
    event_db_state = None
    errors: list[str] = []
    try:
        with RedisConnection(settings) as connection:
            admin = RedisAdmin(
                connection,
                stream_key=settings.stream_key,
                consumer_group=settings.consumer_group,
            )
            admin.ping()
            redis_state = admin.status()
    except RedisError as exc:
        errors.append(f"Redis: {exc}")
    try:
        event_db_state = EventDbAdmin(runtime_config.event_db_path).status()
    except EventDbError as exc:
        errors.append(f"SQLite: {exc}")
    rows: list[tuple[str, str, object]] = [
        ("Redis", "地址", f"{settings.host}:{settings.port}/{settings.db}"),
    ]
    if redis_state is not None:
        rows.extend(
            (
                ("Redis", "事件流", settings.stream_key),
                ("Redis", "消息数", redis_state.stream_length),
                ("Redis", "待处理", redis_state.pending),
                ("Redis", "消费组", redis_state.group),
                ("Redis", "去重键", redis_state.dedup_keys),
            )
        )
    else:
        rows.append(("Redis", "状态", "不可用"))
    if event_db_state is not None:
        rows.extend(
            (
                ("SQLite", "数据库路径", context.display_path(event_db_state.path)),
                ("SQLite", "事件", event_db_state.events),
                ("SQLite", "检测结果", event_db_state.detections),
                ("SQLite", "持久化任务", event_db_state.durable_jobs),
                ("SQLite", "审核任务", event_db_state.reviews),
                ("SQLite", "审核结果", event_db_state.review_results),
            )
        )
        for job_state in event_db_state.durable_job_states:
            details = (
                f"数量={job_state.count}; 最大尝试={job_state.max_attempts}; "
                f"最早更新时间(ms)={job_state.oldest_updated_ms}; "
                f"过期租约={job_state.expired_leases}"
            )
            rows.append(
                ("SQLite", f"任务 {job_state.kind}/{job_state.state}", details)
            )
        expired_lease_count = sum(
            job_state.expired_leases for job_state in event_db_state.durable_job_states
        )
        rows.append(("SQLite", "过期 processing 租约总数", expired_lease_count))
        if event_db_state.dead_letters:
            for dead_letter in event_db_state.dead_letters:
                details = (
                    f"{dead_letter.kind}; revision={dead_letter.entity_revision}; "
                    f"尝试={dead_letter.attempts}; 更新时间(ms)={dead_letter.updated_ms}; "
                    f"错误码={dead_letter.error_code}"
                )
                rows.append(
                    ("SQLite", f"最近死信 #{dead_letter.job_id}", details)
                )
        else:
            rows.append(("SQLite", "最近死信", "无"))
    else:
        rows.append(("SQLite", "状态", "不可用"))
    _print_table(("范围", "指标", "值"), rows)
    if errors:
        info("状态读取失败: " + "; ".join(errors))
        raise CliError("; ".join(errors))
    return 0


def reset(context: ProjectContext, args: Namespace) -> int:
    if not args.dry_run and not getattr(args, "yes", False):
        raise CliError(
            "重置会删除 Redis 运行态和 SQLite EventLedger 数据；"
            "预览请使用 --dry-run，确认执行请使用 --yes"
        )
    runtime_config = _runtime_config(context, args)
    settings = runtime_config.redis
    header("重置 SSV 运行态")
    redis_result = None
    event_db_result = None
    errors: list[str] = []
    try:
        with RedisConnection(settings) as connection:
            admin = RedisAdmin(
                connection,
                stream_key=settings.stream_key,
                consumer_group=settings.consumer_group,
            )
            redis_result = admin.reset_runtime_state(dry_run=args.dry_run)
    except RedisCleanupError as exc:
        redis_result = exc.result
        errors.append(f"Redis: {exc}")
    except RedisError as exc:
        errors.append(f"Redis: {exc}")
    try:
        event_db_result = EventDbAdmin(runtime_config.event_db_path).reset(dry_run=args.dry_run)
    except EventDbError as exc:
        errors.append(f"SQLite: {exc}")
    mode = "预览" if args.dry_run else ("部分完成" if errors else "完成")
    if redis_result is not None:
        info(
            f"{mode}: redis stream={settings.stream_key} "
            f"entries_before={redis_result.stream_entries_before} "
            f"pending_before={redis_result.pending_before} "
            f"dedup_keys_before={redis_result.dedup_keys_before} "
            f"stream_deleted={redis_result.stream_deleted} "
            f"dedup_deleted={redis_result.dedup_deleted}"
        )
    if event_db_result is not None:
        info(
            f"{mode}: event_db={context.display_path(event_db_result.status_before.path)} "
            f"event_rows_before={event_db_result.status_before.total_rows} "
            f"event_rows_deleted={event_db_result.deleted_rows}"
        )
    if errors:
        info("重置失败: " + "; ".join(errors))
        raise CliError("; ".join(errors))
    return 0
