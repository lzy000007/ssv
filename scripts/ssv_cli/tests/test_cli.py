from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.ssv_cli.cli import build_parser, main
from scripts.ssv_cli.commands import build as build_command
from scripts.ssv_cli.commands import cache as cache_command
from scripts.ssv_cli.commands import test as test_command
from scripts.ssv_cli.commands.clean import run as clean_command
from scripts.ssv_cli.config import RedisSettings, RuntimeConfig, load_runtime_config
from scripts.ssv_cli.context import ProjectContext, load_dotenv
from scripts.ssv_cli.output import CliError
from scripts.ssv_cli.services.event_db import EventDbCleanupResult, EventDbError, EventDbStatus
from scripts.ssv_cli.services.redis_admin import (
    RedisCleanupError,
    RedisCleanupResult,
    RedisError,
    RedisStatus,
)
from scripts.ssv_cli.services.runtime_env import _SNAPSHOT_KEYS, load_dependency_snapshot

ROOT = Path(__file__).resolve().parents[3]


class CliParserTest(unittest.TestCase):
    def test_help_is_available_without_a_command(self) -> None:
        with patch("sys.stdout") as output:
            self.assertEqual(main([]), 0)
            output.write.assert_called()

    def test_build_profile_is_forwarded_as_a_value(self) -> None:
        args = build_parser().parse_args(["build", "--profile", "intel"])
        self.assertEqual(args.profile, "intel")

    def test_invalid_build_profile_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["build", "--profile", "bogus"])
        self.assertEqual(raised.exception.code, 2)

    def test_duplicate_build_profile_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["build", "--profile", "cpu", "--profile", "intel"])
        self.assertEqual(raised.exception.code, 2)

    def test_build_profile_requires_a_value(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["build", "--profile"])
        self.assertEqual(raised.exception.code, 2)

    def test_empty_build_profile_requires_a_value(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["build", "--profile="])
        self.assertEqual(raised.exception.code, 2)

    def test_unknown_build_argument_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["build", "--jobs", "2"])
        self.assertEqual(raised.exception.code, 2)

    def test_help_command_remains_an_alias(self) -> None:
        with patch("sys.stdout") as output:
            self.assertEqual(main(["help"]), 0)
            output.write.assert_called()

    def test_runner_arguments_are_preserved_after_run(self) -> None:
        parser = build_parser()
        args, unknown = parser.parse_known_args(
            ["run", "--config", "config/custom.yaml", "--display", "--headless"]
        )
        self.assertEqual(args.command, "run")
        self.assertEqual(unknown, ["--config", "config/custom.yaml", "--display", "--headless"])

    def test_runner_arguments_default_to_empty(self) -> None:
        args = build_parser().parse_args(["run"])
        self.assertEqual(args.command, "run")
        self.assertFalse(hasattr(args, "runner_args"))

    def test_model_arguments_are_preserved_after_manifest(self) -> None:
        parser = build_parser()
        args, unknown = parser.parse_known_args(
            ["model", "manifest", "--model", "source.onnx", "--engine", "model.engine"]
        )
        self.assertEqual(args.command, "model")
        self.assertEqual(args.model_action, "manifest")
        self.assertEqual(unknown, ["--model", "source.onnx", "--engine", "model.engine"])

    def test_model_arguments_default_to_empty(self) -> None:
        args = build_parser().parse_args(["model", "manifest"])
        self.assertEqual(args.command, "model")
        self.assertEqual(args.model_action, "manifest")
        self.assertFalse(hasattr(args, "model_args"))

    def test_runner_help_is_preserved_for_native_runner(self) -> None:
        parser = build_parser()
        args, unknown = parser.parse_known_args(["run", "--help"])
        self.assertEqual(args.command, "run")
        self.assertEqual(unknown, ["--help"])

    def test_model_help_is_preserved_for_manifest_tool(self) -> None:
        parser = build_parser()
        args, unknown = parser.parse_known_args(["model", "manifest", "--help"])
        self.assertEqual(args.command, "model")
        self.assertEqual(args.model_action, "manifest")
        self.assertEqual(unknown, ["--help"])

    def test_model_prepare_is_not_a_command(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["model", "prepare"])
        self.assertEqual(raised.exception.code, 2)

    def test_redis_stop_is_an_explicit_action(self) -> None:
        args = build_parser().parse_args(["redis", "stop"])
        self.assertEqual(args.command, "redis")
        self.assertEqual(args.redis_action, "stop")

    def test_redis_requires_an_explicit_action(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["redis"])
        self.assertEqual(raised.exception.code, 2)

    def test_cache_status_is_an_explicit_action(self) -> None:
        args = build_parser().parse_args(["cache", "status"])
        self.assertEqual(args.command, "cache")
        self.assertEqual(args.cache_action, "status")

    def test_cache_requires_an_explicit_action(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["cache"])
        self.assertEqual(raised.exception.code, 2)

    def test_cache_clear_targets_runtime_cache_without_confirmation_flags(self) -> None:
        args = build_parser().parse_args(["cache", "clear"])
        self.assertFalse(args.dry_run)
        self.assertFalse(hasattr(args, "include_stream"))
        self.assertFalse(hasattr(args, "yes"))

    def test_cache_clear_supports_dry_run(self) -> None:
        args = build_parser().parse_args(["cache", "clear", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_redis_runtime_actions_are_rejected(self) -> None:
        for action in ("status", "clear", "clean"):
            with self.subTest(action=action), self.assertRaises(SystemExit) as raised:
                build_parser().parse_args(["redis", action])
            self.assertEqual(raised.exception.code, 2)

    def test_cache_clear_rejects_legacy_flags(self) -> None:
        for flag in ("--stream", "--yes"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit) as raised:
                build_parser().parse_args(["cache", "clear", flag])
            self.assertEqual(raised.exception.code, 2)

class ContextTest(unittest.TestCase):
    def test_dotenv_does_not_replace_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("EXISTING=from-file\nNEW=\"hello world\"\n", encoding="utf-8")
            environment = {"EXISTING": "from-process"}
            load_dotenv(path, environment)
        self.assertEqual(environment["EXISTING"], "from-process")
        self.assertEqual(environment["NEW"], "hello world")

    def test_context_discovers_config_and_relative_build_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "ssv.yaml").write_text(
                "version: '2.0'\nredis:\n  stream_key: custom:events\n", encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {"SSV_CONFIG_PATH": "", "SSV_BUILD_DIR": "out"},
                clear=False,
            ):
                context = ProjectContext.discover(root)
        self.assertEqual(context.config_path, root / "config" / "ssv.yaml")
        self.assertEqual(context.build_dir, root / "out")

    def test_runtime_config_applies_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "ssv.yaml"
            config.write_text(
                "version: '2.0'\nredis:\n  host: redis.local\n  port: 6380\n"
                "  stream_key: source:events\n  consumer_group: source-group\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"SSV_CONFIG_PATH": ""}, clear=False):
                context = ProjectContext.discover(root)
                settings = load_runtime_config(
                    context,
                    host="127.0.0.1",
                    port=6379,
                    stream="override:events",
                ).redis
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 6379)
        self.assertEqual(settings.stream_key, "override:events")
        self.assertEqual(settings.consumer_group, "source-group")

    def test_runtime_config_resolves_event_db_like_agent_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "ssv.yaml"
            config.write_text(
                "version: '2.0'\nagent:\n  event_db_path: custom/events.db\n"
                "  knowledge:\n    qdrant_url: http://localhost:7444\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"SSV_CONFIG_PATH": ""}, clear=False):
                context = ProjectContext.discover(root)
                runtime = load_runtime_config(context)

        self.assertEqual(runtime.event_db_path, root / "agent" / "custom" / "events.db")
        self.assertEqual(runtime.qdrant_url, "http://localhost:7444")

    def test_runtime_config_preserves_absolute_event_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            absolute_path = root / "absolute" / "events.db"
            config = root / "ssv.yaml"
            config.write_text(
                f"version: '2.0'\nagent:\n  event_db_path: {absolute_path.as_posix()}\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"SSV_CONFIG_PATH": ""}, clear=False):
                context = ProjectContext.discover(root)
                runtime = load_runtime_config(context)

        self.assertEqual(runtime.event_db_path, absolute_path)


class CleanCommandTest(unittest.TestCase):
    def _context(self, root: Path, build_dir: Path) -> ProjectContext:
        return ProjectContext(
            root=root,
            environment={},
            config_path=None,
            build_dir=build_dir,
            compose_file=root / "docker" / "compose.yaml",
        )

    def test_clean_removes_a_marked_build_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_dir = root / "out"
            build_dir.mkdir()
            (build_dir / "build.ninja").write_text("", encoding="utf-8")

            clean_command(self._context(root, build_dir), Namespace())

        self.assertFalse(build_dir.exists())

    def test_clean_rejects_an_unmarked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_dir = root / "unrelated"
            build_dir.mkdir()
            keep = build_dir / "keep.txt"
            keep.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(CliError, "未识别的目录"):
                clean_command(self._context(root, build_dir), Namespace())

            self.assertTrue(keep.exists())

    def test_clean_rejects_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            with self.assertRaisesRegex(CliError, "项目根目录或其祖先"):
                clean_command(self._context(root, root), Namespace())


class CacheCommandTest(unittest.TestCase):
    def test_clear_reports_partial_statistics_before_returning_error(self) -> None:
        context = ProjectContext(
            root=ROOT,
            environment={},
            config_path=None,
            build_dir=ROOT / "build",
            compose_file=ROOT / "docker" / "compose.yaml",
        )
        args = Namespace(
            config=None,
            host=None,
            port=None,
            db=None,
            stream_key=None,
            group=None,
            dry_run=False,
        )
        failure = RedisCleanupError(
            "DEL failed",
            result=RedisCleanupResult(
                stream_entries_before=4,
                pending_before=3,
                dedup_keys_before=5,
                stream_deleted=1,
                dedup_deleted=2,
                dry_run=False,
            ),
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection

        with (
            patch.object(
                cache_command,
                "_runtime_config",
                return_value=RuntimeConfig(
                    redis=RedisSettings(),
                    event_db_path=ROOT / "agent" / "data" / "events.db",
                ),
            ),
            patch.object(cache_command, "RedisConnection", return_value=connection),
            patch.object(
                cache_command.RedisAdmin, "clear_runtime_cache", side_effect=failure
            ) as clear,
            patch.object(cache_command, "info") as output,
            self.assertRaises(CliError),
        ):
            cache_command.clear(context, args)

        clear.assert_called_once_with(dry_run=False)
        messages = [call.args[0] for call in output.call_args_list]
        self.assertTrue(any("stream_deleted=1" in message for message in messages))
        self.assertTrue(any("dedup_deleted=2" in message for message in messages))
        self.assertTrue(any("清理失败" in message for message in messages))

    def test_status_reports_both_backends_independently(self) -> None:
        context = ProjectContext(
            root=ROOT,
            environment={},
            config_path=None,
            build_dir=ROOT / "build",
            compose_file=ROOT / "docker" / "compose.yaml",
        )
        args = Namespace(
            config=None,
            host=None,
            port=None,
            db=None,
            stream_key=None,
            group=None,
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection
        redis_state = RedisStatus(stream_length=4, pending=2, group="ssv-agent", dedup_keys=1)
        event_db_state = EventDbStatus(
            path=ROOT / "agent" / "data" / "events.db",
            exists=True,
            events=3,
            durable_jobs=4,
        )

        with (
            patch.object(
                cache_command,
                "_runtime_config",
                return_value=RuntimeConfig(
                    redis=RedisSettings(),
                    event_db_path=event_db_state.path,
                ),
            ),
            patch.object(cache_command, "RedisConnection", return_value=connection),
            patch.object(cache_command.RedisAdmin, "status", return_value=redis_state),
            patch.object(cache_command.EventDbAdmin, "status", return_value=event_db_state),
            patch.object(cache_command, "info") as output,
        ):
            self.assertEqual(cache_command.status(context, args), 0)

        messages = [call.args[0] for call in output.call_args_list]
        self.assertTrue(any("| Redis" in message for message in messages))
        self.assertTrue(any("| SQLite" in message for message in messages))
        self.assertTrue(any("| 范围" in message for message in messages))
        self.assertTrue(any("| 事件流" in message for message in messages))
        self.assertTrue(any("| 事件" in message for message in messages))
        table_lines = [message for message in messages if message.startswith(("+", "|"))]
        self.assertGreater(len(table_lines), 3)
        self.assertEqual(len({cache_command._display_width(line) for line in table_lines}), 1)

    def test_status_reports_sqlite_when_redis_status_fails(self) -> None:
        context = ProjectContext(
            root=ROOT,
            environment={},
            config_path=None,
            build_dir=ROOT / "build",
            compose_file=ROOT / "docker" / "compose.yaml",
        )
        args = Namespace(
            config=None,
            host=None,
            port=None,
            db=None,
            stream_key=None,
            group=None,
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection
        event_db_state = EventDbStatus(path=ROOT / "agent" / "data" / "events.db", exists=True)

        with (
            patch.object(
                cache_command,
                "_runtime_config",
                return_value=RuntimeConfig(
                    redis=RedisSettings(),
                    event_db_path=event_db_state.path,
                ),
            ),
            patch.object(cache_command, "RedisConnection", return_value=connection),
            patch.object(cache_command.RedisAdmin, "status", side_effect=RedisError("offline")),
            patch.object(cache_command.EventDbAdmin, "status", return_value=event_db_state),
            patch.object(cache_command, "info") as output,
            self.assertRaises(CliError),
        ):
            cache_command.status(context, args)

        messages = [call.args[0] for call in output.call_args_list]
        self.assertTrue(any("| SQLite" in message for message in messages))
        self.assertTrue(any("| 状态" in message and "不可用" in message for message in messages))
        self.assertTrue(any("Redis: offline" in message for message in messages))

    def test_clear_continues_to_event_db_after_redis_failure(self) -> None:
        context = ProjectContext(
            root=ROOT,
            environment={},
            config_path=None,
            build_dir=ROOT / "build",
            compose_file=ROOT / "docker" / "compose.yaml",
        )
        args = Namespace(
            config=None,
            host=None,
            port=None,
            db=None,
            stream_key=None,
            group=None,
            dry_run=False,
        )
        event_db_result = EventDbCleanupResult(
            status_before=EventDbStatus(path=ROOT / "agent" / "data" / "events.db", exists=True),
            deleted_rows=4,
            dry_run=False,
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection

        with (
            patch.object(
                cache_command,
                "_runtime_config",
                return_value=RuntimeConfig(
                    redis=RedisSettings(),
                    event_db_path=event_db_result.status_before.path,
                ),
            ),
            patch.object(cache_command, "RedisConnection", return_value=connection),
            patch.object(
                cache_command.RedisAdmin,
                "clear_runtime_cache",
                side_effect=RedisError("offline"),
            ) as clear_redis,
            patch.object(
                cache_command.EventDbAdmin, "clear", return_value=event_db_result
            ) as clear_db,
            patch.object(cache_command, "info") as output,
            self.assertRaises(CliError),
        ):
            cache_command.clear(context, args)

        clear_redis.assert_called_once_with(dry_run=False)
        clear_db.assert_called_once_with(dry_run=False)
        messages = [call.args[0] for call in output.call_args_list]
        self.assertTrue(any("event_rows_deleted=4" in message for message in messages))
        self.assertTrue(any("Redis: offline" in message for message in messages))

    def test_clear_reports_sqlite_failure_after_redis_result(self) -> None:
        context = ProjectContext(
            root=ROOT,
            environment={},
            config_path=None,
            build_dir=ROOT / "build",
            compose_file=ROOT / "docker" / "compose.yaml",
        )
        args = Namespace(
            config=None,
            host=None,
            port=None,
            db=None,
            stream_key=None,
            group=None,
            dry_run=False,
        )
        redis_result = RedisCleanupResult(4, 2, 1, 1, 1, False)
        connection = MagicMock()
        connection.__enter__.return_value = connection

        with (
            patch.object(
                cache_command,
                "_runtime_config",
                return_value=RuntimeConfig(
                    redis=RedisSettings(),
                    event_db_path=ROOT / "agent" / "data" / "events.db",
                ),
            ),
            patch.object(cache_command, "RedisConnection", return_value=connection),
            patch.object(
                cache_command.RedisAdmin,
                "clear_runtime_cache",
                return_value=redis_result,
            ) as clear_redis,
            patch.object(
                cache_command.EventDbAdmin,
                "clear",
                side_effect=EventDbError("database locked"),
            ) as clear_db,
            patch.object(cache_command, "info") as output,
            self.assertRaises(CliError),
        ):
            cache_command.clear(context, args)

        clear_redis.assert_called_once_with(dry_run=False)
        clear_db.assert_called_once_with(dry_run=False)
        messages = [call.args[0] for call in output.call_args_list]
        self.assertTrue(any("stream_deleted=1" in message for message in messages))
        self.assertTrue(any("SQLite: database locked" in message for message in messages))

    def test_clear_clears_redis_and_event_ledger(self) -> None:
        context = ProjectContext(
            root=ROOT,
            environment={},
            config_path=None,
            build_dir=ROOT / "build",
            compose_file=ROOT / "docker" / "compose.yaml",
        )
        args = Namespace(
            config=None,
            host=None,
            port=None,
            db=None,
            stream_key=None,
            group=None,
            dry_run=False,
        )
        redis_result = RedisCleanupResult(
            stream_entries_before=4,
            pending_before=3,
            dedup_keys_before=5,
            stream_deleted=1,
            dedup_deleted=5,
            dry_run=False,
        )
        event_db_result = EventDbCleanupResult(
            status_before=EventDbStatus(
                path=ROOT / "agent" / "data" / "events.db",
                exists=True,
                events=2,
                durable_jobs=4,
            ),
            deleted_rows=6,
            dry_run=False,
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection

        with (
            patch.object(
                cache_command,
                "_runtime_config",
                return_value=RuntimeConfig(
                    redis=RedisSettings(),
                    event_db_path=ROOT / "agent" / "data" / "events.db",
                ),
            ),
            patch.object(cache_command, "RedisConnection", return_value=connection),
            patch.object(
                cache_command.RedisAdmin,
                "clear_runtime_cache",
                return_value=redis_result,
            ) as clear_redis,
            patch.object(
                cache_command.EventDbAdmin,
                "clear",
                return_value=event_db_result,
            ) as clear_event_db,
        ):
            self.assertEqual(cache_command.clear(context, args), 0)

        clear_redis.assert_called_once_with(dry_run=False)
        clear_event_db.assert_called_once_with(dry_run=False)

    def test_clear_dry_run_passes_through_to_both_backends(self) -> None:
        context = ProjectContext(
            root=ROOT,
            environment={},
            config_path=None,
            build_dir=ROOT / "build",
            compose_file=ROOT / "docker" / "compose.yaml",
        )
        args = Namespace(
            config=None,
            host=None,
            port=None,
            db=None,
            stream_key=None,
            group=None,
            dry_run=True,
        )
        redis_result = RedisCleanupResult(0, 0, 0, 0, 0, True)
        event_db_result = EventDbCleanupResult(
            status_before=EventDbStatus(path=ROOT / "agent" / "data" / "events.db", exists=False),
            deleted_rows=0,
            dry_run=True,
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection

        with (
            patch.object(
                cache_command,
                "_runtime_config",
                return_value=RuntimeConfig(
                    redis=RedisSettings(),
                    event_db_path=ROOT / "agent" / "data" / "events.db",
                ),
            ),
            patch.object(cache_command, "RedisConnection", return_value=connection),
            patch.object(
                cache_command.RedisAdmin,
                "clear_runtime_cache",
                return_value=redis_result,
            ) as clear_redis,
            patch.object(
                cache_command.EventDbAdmin,
                "clear",
                return_value=event_db_result,
            ) as clear_event_db,
        ):
            self.assertEqual(cache_command.clear(context, args), 0)

        clear_redis.assert_called_once_with(dry_run=True)
        clear_event_db.assert_called_once_with(dry_run=True)


class RuntimeSnapshotTest(unittest.TestCase):
    def test_snapshot_decodes_shell_escaped_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ssv-deps.env"
            values = {key: "" for key in _SNAPSHOT_KEYS}
            values.update(
                {
                    "SSV_DEPS_PROFILE": "intel",
                    "SSV_DEPS_RUNTIME_PATH": "/opt/a:/opt/b",
                    "SSV_DEPS_ONNXRUNTIME_PROVIDERS": r"CPUExecutionProvider\,OpenVINOExecutionProvider",
                }
            )
            path.write_text(
                "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
                encoding="utf-8",
            )
            values = load_dependency_snapshot(path)
        self.assertEqual(values["SSV_DEPS_PROFILE"], "intel")
        self.assertEqual(
            values["SSV_DEPS_ONNXRUNTIME_PROVIDERS"],
            "CPUExecutionProvider,OpenVINOExecutionProvider",
        )

    def test_snapshot_rejects_unknown_or_missing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ssv-deps.env"
            path.write_text("SSV_DEPS_UNKNOWN=value\n", encoding="utf-8")
            with self.assertRaisesRegex(CliError, "unknown variable"):
                load_dependency_snapshot(path)

            path.write_text("SSV_DEPS_PROFILE=cpu\n", encoding="utf-8")
            with self.assertRaisesRegex(CliError, "missing variable"):
                load_dependency_snapshot(path)

    def test_snapshot_rejects_shell_injection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ssv-deps.env"
            path.write_text("SSV_DEPS_PROFILE=cpu;touch\\ /tmp/pwned\n", encoding="utf-8")
            with self.assertRaises(CliError):
                load_dependency_snapshot(path)

    def test_missing_snapshot_explains_build_prerequisite(self) -> None:
        with self.assertRaises(CliError) as raised:
            load_dependency_snapshot(ROOT / "does-not-exist" / "ssv-deps.env")
        self.assertIn("run ./ssv build first", str(raised.exception))


class CliWrapperTest(unittest.TestCase):
    def test_wrapper_runs_python_entrypoint_from_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            fake_bin = work / "bin"
            fake_bin.mkdir()
            record = work / "uv.json"
            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['SSV_TEST_RECORD']).write_text(\n"
                "    json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}),\n"
                "    encoding='utf-8',\n"
                ")\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            environment = os.environ | {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "SSV_TEST_RECORD": str(record),
            }
            result = subprocess.run(
                [str(ROOT / "ssv"), "redis", "status", "--db", "4"],
                cwd=work,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            captured = json.loads(record.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            captured["args"],
            [
                "run",
                "--project",
                str(ROOT),
                "python",
                str(ROOT / "ssv.py"),
                "redis",
                "status",
                "--db",
                "4",
            ],
        )
        self.assertEqual(captured["cwd"], str(ROOT))


class CommandHandoffTest(unittest.TestCase):
    def test_test_command_reports_missing_model_contract_dependency(self) -> None:
        missing = ModuleNotFoundError("No module named 'onnx'")
        missing.name = "onnx"
        with (
            patch(
                "scripts.ssv_cli.commands.test.importlib.import_module",
                side_effect=missing,
            ),
            self.assertRaisesRegex(CliError, r"\[model\]"),
        ):
            test_command._require_model_test_dependencies()

    def _snapshot(self, path: Path, *, runtime_path: Path) -> None:
        values = {key: "" for key in _SNAPSHOT_KEYS}
        values["SSV_DEPS_RUNTIME_PATH"] = str(runtime_path)
        path.write_text(
            "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
            encoding="utf-8",
        )

    def test_build_enters_python_build_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            context = ProjectContext(
                root=work,
                environment={},
                config_path=None,
                build_dir=work / "build",
                compose_file=work / "compose.yaml",
            )
            with patch("scripts.ssv_cli.commands.build.NativeBuildService.build", return_value=23) as mocked_build:
                result = build_command.run(context, Namespace(profile="intel"))
        self.assertEqual(result, 23)
        mocked_build.assert_called_once_with("intel")

    def test_run_forwards_runner_arguments_and_runtime_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            build_dir = work / "build"
            runtime_dir = work / "runtime"
            runner = build_dir / "runner" / "ssv-runner"
            capture = work / "runner.args"
            runner.parent.mkdir(parents=True)
            runtime_dir.mkdir()
            self._snapshot(build_dir / "ssv-deps.env", runtime_path=runtime_dir)
            runner.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SSV_CAPTURE_PATH\"\n"
                "printf 'LD=%s\\nGST=%s\\n' \"$LD_LIBRARY_PATH\" \"$GST_PLUGIN_PATH\" >> \"$SSV_CAPTURE_PATH\"\n"
                "exit 23\n",
                encoding="utf-8",
            )
            runner.chmod(0o755)
            environment = os.environ | {
                "SSV_BUILD_DIR": str(build_dir),
                "SSV_CAPTURE_PATH": str(capture),
                "LD_LIBRARY_PATH": "",
                "GST_PLUGIN_PATH": "",
            }
            result = subprocess.run(
                [str(ROOT / "ssv"), "run", "--config", "config/custom.yaml", "--headless"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            lines = capture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(lines[:3], ["--config", "config/custom.yaml", "--headless"])
        self.assertEqual(lines[3], f"LD={build_dir / 'gst' / 'ssv-common'}:{runtime_dir}")
        self.assertIn(f"{build_dir / 'gst' / 'ssv-template'}", lines[4])


if __name__ == "__main__":
    unittest.main()
