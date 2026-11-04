"""Argument parser and top-level dispatch for ``./ssv``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .commands import agent, build, clean, infra, inspect, model, run, runtime, test
from .context import ProjectContext
from .output import CliError, error


def _profile(value: str) -> str:
    allowed = "auto, cpu, nvidia, intel, amd"
    if value not in {"auto", "cpu", "nvidia", "intel", "amd"}:
        raise argparse.ArgumentTypeError(f"runtime profile must be {allowed}: {value}")
    return value


class _StoreOnce(argparse.Action):
    """Reject repeated options where the legacy shell CLI rejected them."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        value: str,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may be specified only once")
        setattr(namespace, self.dest, value)


def _add_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=argparse.SUPPRESS, help="SSV 运行配置路径")
    parser.add_argument("--host", default=argparse.SUPPRESS, help="Redis 主机")
    parser.add_argument("--port", type=int, default=argparse.SUPPRESS, help="Redis 端口")
    parser.add_argument("--db", type=int, default=argparse.SUPPRESS, help="Redis database 编号")


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    _add_connection_options(parser)
    parser.add_argument(
        "--stream-key",
        dest="stream_key",
        default=argparse.SUPPRESS,
        help="Stream key",
    )
    parser.add_argument("--group", default=argparse.SUPPRESS, help="Consumer group")


def _validate_build_arguments(parser: argparse.ArgumentParser, arguments: Sequence[str]) -> None:
    """Preserve the shell CLI's explicit error for missing profile values."""

    if not arguments or arguments[0] != "build":
        return
    for index, argument in enumerate(arguments[1:], 1):
        if argument == "--profile":
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                parser.error("--profile requires a value")
        elif argument == "--profile=":
            parser.error("--profile requires a value")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./ssv",
        description="Site Safety Vision 项目入口",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    build_parser_ = commands.add_parser("build", help="编译 C++ runner、插件和测试")
    build_parser_.add_argument(
        "--profile",
        type=_profile,
        action=_StoreOnce,
        default=None,
        help="runtime profile",
    )
    build_parser_.set_defaults(handler=build.run)

    clean_parser = commands.add_parser("clean", help="删除 Meson 构建目录")
    clean_parser.set_defaults(handler=clean.run)

    infra_parser = commands.add_parser("infra", help="管理 Docker Redis、Qdrant 基础设施")
    infra_actions = infra_parser.add_subparsers(dest="infra_action", metavar="ACTION")
    infra_actions.required = True
    infra_start = infra_actions.add_parser("start", help="启动 Docker Redis、Qdrant")
    _add_connection_options(infra_start)
    infra_start.set_defaults(handler=infra.start)
    infra_stop = infra_actions.add_parser("stop", help="停止 Docker Redis、Qdrant")
    infra_stop.set_defaults(handler=infra.stop)
    infra_status = infra_actions.add_parser("status", help="查看 Docker Redis、Qdrant 状态")
    infra_status.set_defaults(handler=infra.status)

    runtime_parser = commands.add_parser("runtime", help="管理 SSV 运行态")
    runtime_actions = runtime_parser.add_subparsers(dest="runtime_action", metavar="ACTION")
    runtime_actions.required = True
    runtime_status = runtime_actions.add_parser("status", help="查看 SSV 运行态")
    _add_runtime_options(runtime_status)
    runtime_status.set_defaults(handler=runtime.status)
    runtime_reset = runtime_actions.add_parser("reset", help="重置 SSV 运行态数据")
    _add_runtime_options(runtime_reset)
    reset_confirmation = runtime_reset.add_mutually_exclusive_group()
    reset_confirmation.add_argument(
        "--dry-run", action="store_true", help="只统计，不修改运行态数据"
    )
    reset_confirmation.add_argument(
        "--yes",
        action="store_true",
        help="确认删除 Redis 运行态记录和 SQLite EventLedger 数据",
    )
    runtime_reset.set_defaults(handler=runtime.reset)

    test_parser = commands.add_parser("test", help="运行代码测试和链路冒烟测试后退出")
    test_parser.set_defaults(handler=test.run)

    # The runner owns its own options, including --help. Keep them opaque here.
    run_parser = commands.add_parser(
        "run",
        help=(
            "运行 C++ 实时链路；示例: run --display、run --headless、"
            "run --display --overlay --display-backend gtkglsink|gtksink"
        ),
        add_help=False,
    )
    run_parser.set_defaults(handler=run.run)

    model_parser = commands.add_parser("model", help="导出、验证模型并生成 TensorRT manifest")
    model_actions = model_parser.add_subparsers(dest="model_action", metavar="ACTION")
    model_actions.required = True
    export_parser = model_actions.add_parser("export", help="导出 YOLOv8n ONNX 模型")
    export_parser.set_defaults(handler=model.export)

    verify_parser = model_actions.add_parser("verify", help="验证安全帽 YOLO 模型", add_help=False)
    verify_parser.set_defaults(handler=model.verify)

    manifest_parser = model_actions.add_parser(
        "manifest", help="生成 TensorRT engine manifest", add_help=False
    )
    manifest_parser.set_defaults(handler=model.write_manifest)

    agent_parser = commands.add_parser("agent", help="启动 Python Agent 服务")
    agent_parser.add_argument("--config", default=None, help="Agent YAML 配置路径")
    agent_parser.add_argument("--log-level", default=None, help="Agent 日志级别")
    agent_parser.set_defaults(handler=agent.run)

    inspect_parser = commands.add_parser("inspect", help="查看 GStreamer 插件信息")
    inspect_parser.set_defaults(handler=inspect.run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "help":
        parser.print_help()
        return 0
    _validate_build_arguments(parser, arguments)
    args, unknown = parser.parse_known_args(arguments)
    if unknown and args.command == "run":
        args.runner_args = unknown
    elif unknown and args.command == "model" and args.model_action in {"verify", "manifest"}:
        args.model_args = unknown
    elif unknown:
        parser.error("unrecognized arguments: " + " ".join(unknown))
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    context = ProjectContext.discover()
    try:
        return int(args.handler(context, args))
    except CliError as exc:
        error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        error("已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
