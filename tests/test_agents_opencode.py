# SPDX-License-Identifier: Apache-2.0
"""OpenCode's model list is fetched from its CLI and cached for the next start."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class FakeCP:
    def __init__(self, out: str, code: int = 0) -> None:
        self.stdout = out
        self.returncode = code


class OpenCodeModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(Path(self.tmp.name) / "config")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_models_are_cached_and_reused_when_the_program_is_missing(self):
        from luma_playground.agents import opencode

        connection = mock.Mock(id="opencode", provider="opencode", config_home="")
        connection.account_key.return_value = ("opencode", "default")
        calls = []

        def run(argv, env):
            calls.append(argv)
            return FakeCP("ollama-cloud/deepseek-v4-pro:0813\nopencode/hy4-flash-free\n")

        first = opencode.model_option(connection, run=run, now=1000.0)
        self.assertEqual(first["id"], "model")
        self.assertEqual(first["category"], "model")
        self.assertEqual([o["value"] for o in first["options"]],
                         ["ollama-cloud/deepseek-v4-pro:0813", "opencode/hy4-flash-free"])
        self.assertEqual(len(calls), 1)

        def missing(_argv, _env):
            return FakeCP("", code=1)

        second = opencode.model_option(connection, run=missing, now=1000.0 + 60)
        self.assertEqual([o["value"] for o in second["options"]],
                         ["ollama-cloud/deepseek-v4-pro:0813", "opencode/hy4-flash-free"])
        self.assertEqual(len(calls), 1)                 # no second fetch: cache is fresh

        # An explicit refresh goes to the program even while the cache is fresh.
        opencode.model_option(connection, run=run, now=1000.0 + 120, refresh=True)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
