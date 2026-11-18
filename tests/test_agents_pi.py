# SPDX-License-Identifier: Apache-2.0
"""PiAgent against a scripted ``pi --mode rpc`` subprocess. No network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from luma_playground.agents import acp, sandbox
from luma_playground.agents.connections import Connection
from luma_playground.agents.pi import PiAgent, agent_dir
from luma_playground.core.events import EventBus
from luma_playground.core.model import Member
from luma_playground.core.project import create_project
from luma_playground.core.store import ProjectStore

FAKE = Path(__file__).resolve().parent / "fake_pi_rpc.py"


class PiBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(base / "config"),
                                               "PLAYGROUND_DATA_DIR": str(base / "data"),
                                               "PLAYGROUND_HOME": str(base / "projects")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bus = EventBus()
        project = create_project("Kiln", "A pottery class sign-up", root=base / "projects", git=False,
                                 bus=self.bus, owner=Member(id="noor", name="Noor", role="owner"))
        self.root = str(project.path)
        self.store = ProjectStore(self.root, bus=self.bus)
        self.goal = self.store.create_goal("Say hi", outcome="hello.txt says hi", owner="noor")
        self.updates: list[dict] = []
        self.state = base / "pi-state"
        self.state.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def agent(self, script: list[dict] | None = None) -> PiAgent:
        env = dict(os.environ)
        env["FAKE_PI_STATE"] = str(self.state)
        if script is not None:
            path = self.state / "script.json"
            path.write_text(json.dumps({"events": script}), encoding="utf-8")
            env["FAKE_PI_SCRIPT"] = str(path)
        handlers = acp.ProjectHandlers(self.root, on_update=lambda s, u: self.updates.append(u),
                                       policy=sandbox.SandboxPolicy(self.root, backend="none"))
        servers = [{"name": "playground", "command": "/usr/bin/playground-mcp", "args": [],
                    "env": [{"name": "PLAYGROUND_GOAL", "value": self.goal.id}]}]
        pi = PiAgent(self.root, handlers, argv=[sys.executable, str(FAKE)], env=env,
                     goal_id=self.goal.id, actor="pi", mcp_servers=servers)
        self.addCleanup(pi.close)
        return pi

    def wait_marker(self, name: str, timeout: float = 5.0) -> bool:
        path = self.state / "events" / name
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return False

    def calls(self) -> list[dict]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def text(self) -> str:
        return "".join(u["content"]["text"] for u in self.updates
                       if u["sessionUpdate"] == "agent_message_chunk")


class PiTurnTests(PiBase):
    def test_prompt_streams_text_thinking_tools_and_usage(self):
        pi = self.agent()
        sid = pi.new_session(self.root)
        self.assertEqual(pi.prompt(sid, [acp.text_block("Make it say hi")]), "end_turn")
        self.assertEqual(self.text(), "Hello world")
        thought = "".join(u["content"]["text"] for u in self.updates
                          if u["sessionUpdate"] == "agent_thought_chunk")
        self.assertEqual(thought, "thinking")
        usage = [u for u in self.updates if u["sessionUpdate"] == "usage_update"]
        self.assertTrue(usage)
        self.assertEqual(usage[-1]["used"], 14)
        self.assertEqual(usage[-1]["size"], 200000)
        self.assertEqual(usage[-1]["cost"], {"amount": 0.002, "currency": "USD"})
        calls = [u for u in self.updates if u["sessionUpdate"] == "tool_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        self.assertEqual(calls[0]["kind"], "execute")
        self.assertEqual(calls[0]["title"], "Run echo hi")
        done = [u for u in self.updates if u["sessionUpdate"] == "tool_call_update"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["status"], "completed")
        self.assertEqual(done[0]["content"][0]["content"]["text"], "hi")

    def test_agent_end_does_not_finish_a_turn(self):
        script = [
            {"type": "message_update", "assistantMessageEvent":
             {"type": "text_delta", "contentIndex": 0, "delta": "one"}},
            {"type": "agent_end", "messages": [], "willRetry": False},
            {"__wait__": "trigger"},
            {"type": "message_update", "assistantMessageEvent":
             {"type": "text_delta", "contentIndex": 0, "delta": "two"}},
            {"type": "agent_settled", "aborted": False},
        ]
        pi = self.agent(script)
        sid = pi.new_session(self.root)
        result: list[str] = []

        def run() -> None:
            result.append(pi.prompt(sid, [acp.text_block("go")]))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(self.wait_marker("agent_end"))
        time.sleep(0.1)
        self.assertTrue(thread.is_alive(), "a turn must not end on agent_end")
        pi._conn.send({"type": "__release__"})  # release the script's hold
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["end_turn"])
        self.assertEqual(self.text(), "onetwo")

    def test_abort_cancels_the_turn(self):
        pi = self.agent()
        sid = pi.new_session(self.root)
        result: list[str] = []

        def run() -> None:
            result.append(pi.prompt(sid, [acp.text_block("cancel-me")]))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(self.wait_marker("waiting-abort"))
        pi.cancel(sid)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["cancelled"])
        self.assertTrue(any(c["command"] == "abort" for c in self.calls()))

    def test_unexpected_exit_is_a_readable_error(self):
        pi = self.agent()
        sid = pi.new_session(self.root)
        with self.assertRaises(acp.AcpClosed):
            pi.prompt(sid, [acp.text_block("crash")])
        self.assertFalse(pi.alive)

    def test_extension_dialog_is_auto_cancelled(self):
        pi = self.agent()
        sid = pi.new_session(self.root)
        pi.prompt(sid, [acp.text_block("go")])
        responses = [c for c in self.calls() if c["command"] == "extension_ui_response"]
        self.assertTrue(responses)
        self.assertTrue(all(c["cancelled"] is True for c in responses))

    def test_request_ids_are_unique_and_correlated(self):
        pi = self.agent()
        sid = pi.new_session(self.root)
        pi.prompt(sid, [acp.text_block("go")])
        ids = [c["id"] for c in self.calls() if c.get("id")]
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))


class PiSessionTests(PiBase):
    def test_launch_argv_and_generated_mcp_config(self):
        pi = self.agent()
        pi.new_session(self.root)
        argv = json.loads((self.state / "argv.json").read_text())
        self.assertIn("--mode", argv)
        self.assertIn("rpc", argv)
        self.assertIn("--approve", argv)
        self.assertIn("--session-id", argv)
        config = json.loads((agent_dir(self.root, "pi") / "mcp.json").read_text())
        server = config["mcpServers"]["playground"]
        self.assertEqual(server["exposure"], "direct")
        self.assertEqual(server["env"]["PLAYGROUND_GOAL"], self.goal.id)
        self.assertEqual(server["cwd"], os.path.realpath(self.root))

    def test_reopen_uses_the_same_session(self):
        pi = self.agent()
        sid = pi.new_session(self.root)
        pi.close()
        pi2 = self.agent()
        self.assertEqual(pi2.reopen_session(sid, self.root), "resumed")
        argv = json.loads((self.state / "argv.json").read_text())
        self.assertIn(sid, argv)

    def test_config_options_round_trip(self):
        pi = self.agent()
        sid = pi.new_session(self.root)
        options = pi.config_options(sid)
        self.assertEqual([o["id"] for o in options], ["model", "thought_level"])
        model = next(o for o in options if o["id"] == "model")
        self.assertEqual(model["currentValue"], "anthropic/claude-sonnet-4")
        self.assertEqual(len(model["options"]), 2)
        options = pi.set_config_option(sid, "model", "openai/gpt-5")
        self.assertEqual(next(o for o in options if o["id"] == "model")["currentValue"], "openai/gpt-5")
        options = pi.set_config_option(sid, "thought_level", "high")
        self.assertEqual(next(o for o in options if o["id"] == "thought_level")["currentValue"], "high")
        calls = {c["command"]: c["record"] for c in self.calls()}
        self.assertEqual(calls["set_model"]["modelId"], "gpt-5")
        self.assertEqual(calls["set_model"]["provider"], "openai")
        self.assertEqual(calls["set_thinking_level"]["level"], "high")

    def test_terminal_sign_in_command(self):
        pi = self.agent()
        pi.new_session(self.root)   # providers come from get_available_models
        methods = pi.auth_methods()
        self.assertEqual({m["id"] for m in methods}, {"terminal:anthropic", "terminal:openai"})
        method = next(m for m in methods if m["id"] == "terminal:anthropic")
        argv, _env = pi.login_command(method["id"])
        self.assertEqual(argv[-3:], ["auth", "login", "anthropic"])
        with self.assertRaises(acp.AcpError):
            pi.login_command("terminal:ollama")   # not a Pi provider

    def test_detect_uses_pi_rpc_for_auth_status(self):
        from luma_playground.agents import connections
        env = dict(os.environ)
        env["FAKE_PI_STATE"] = str(self.state)
        models = connections._pi_rpc_models([sys.executable, str(FAKE)], env, 10)
        self.assertIsNotNone(models)
        self.assertEqual(len(models), 2)
        self.assertIn("anthropic", {m["provider"] for m in models})
        self.assertTrue(any(c["command"] == "get_available_models" for c in self.calls()))

    def test_detect_returns_none_when_pi_cannot_be_probed(self):
        from luma_playground.agents import connections
        self.assertIsNone(connections._pi_rpc_models(["/no/such/pi"], {}, 2))

    def test_connection_is_acp_for_pi(self):
        conn = Connection(id="pi", label="Pi", kind="personal-plan", provider="pi", owner="noor")
        conn.validate()
        self.assertTrue(conn.is_acp)


if __name__ == "__main__":
    unittest.main()
