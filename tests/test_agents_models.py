# SPDX-License-Identifier: Apache-2.0
"""Available model lists come from the agent's program once, then from a cache."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class AvailableModelsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(Path(self.tmp.name) / "config")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def _conn(provider):
        return mock.Mock(id=provider, provider=provider, is_acp=True, config_home="",
                         account_key=lambda: (provider, "default"))

    def test_opencode_rows_come_from_its_option(self):
        from luma_playground.agents import models
        option = {"id": "model", "options": [{"value": "m1", "name": "Model 1"},
                                             {"value": "m2", "name": "Model 2"}]}
        with mock.patch("luma_playground.agents.opencode.model_option", return_value=option) as probe:
            rows = models.available(self._conn("opencode"), offline=True)
        self.assertEqual([r["value"] for r in rows], ["m1", "m2"])
        self.assertTrue(probe.call_args.kwargs["cached_only"])

    def test_pi_rows_are_cached_per_account(self):
        from luma_playground.agents import models
        conn = self._conn("pi")
        found = [{"id": "small", "name": "Small", "provider": "local"},
                 {"id": "big", "name": "Big", "provider": "cloud"}]
        with mock.patch("luma_playground.agents.connections.launch_spec", return_value=(["pi"], {})), \
                mock.patch("luma_playground.agents.connections._pi_rpc_models", return_value=found) as probe:
            rows = models.available(conn)
            self.assertEqual([r["value"] for r in rows], ["local/small", "cloud/big"])
            self.assertEqual(models.available(conn, offline=True), rows)
            self.assertEqual(probe.call_count, 1)

    def test_pi_probe_failure_falls_back_to_an_empty_list(self):
        from luma_playground.agents import models
        with mock.patch("luma_playground.agents.connections.launch_spec", side_effect=RuntimeError("missing")), \
                mock.patch("luma_playground.agents.connections._pi_rpc_models", return_value=None):
            self.assertEqual(models.available(self._conn("pi")), [])

    def test_other_providers_have_no_early_list(self):
        from luma_playground.agents import models
        for provider in ("claude", "codex", "goose"):
            self.assertEqual(models.available(self._conn(provider)), [])

    def test_any_agent_module_can_offer_a_list(self):
        from luma_playground.agents import ari, models

        def offer(connection, *, offline=False, refresh=False):
            return [{"value": "a", "name": "A", "description": ""}]

        with mock.patch.object(ari, "available_models", offer, create=True):
            self.assertEqual(models.available(self._conn("ari")), [{"value": "a", "name": "A", "description": ""}])


if __name__ == "__main__":
    unittest.main()
