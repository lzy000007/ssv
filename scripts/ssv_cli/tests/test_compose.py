from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.ssv_cli.config import RedisSettings
from scripts.ssv_cli.context import ProjectContext
from scripts.ssv_cli.services.compose import start_redis


class ComposeRedisTest(unittest.TestCase):
    def test_start_passes_resolved_redis_port_to_compose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ProjectContext(
                root=root,
                environment={"PATH": "/usr/bin"},
                config_path=None,
                build_dir=root / "build",
                compose_file=root / "docker" / "compose.yaml",
            )
            settings = RedisSettings(port=6380)
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.execute.return_value = "PONG"
            compose_result = subprocess.CompletedProcess([], 0, "", "")

            with (
                patch("scripts.ssv_cli.services.compose._compose", return_value=compose_result) as compose,
                patch("scripts.ssv_cli.services.compose.RedisConnection", return_value=connection),
                patch("scripts.ssv_cli.services.compose._qdrant_ready", return_value=True) as ready,
            ):
                self.assertEqual(
                    start_redis(context, settings, qdrant_url="http://localhost:7444"),
                    0,
                )

            compose.assert_called_once_with(
                context,
                "up",
                "-d",
                environment={
                    "PATH": "/usr/bin",
                    "REDIS_PORT": "6380",
                    "QDRANT_PORT": "7444",
                },
            )
            ready.assert_called_once_with(7444)


if __name__ == "__main__":
    unittest.main()
