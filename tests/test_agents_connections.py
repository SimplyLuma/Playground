# SPDX-License-Identifier: Apache-2.0
"""Connections: the picker rules, per-account homes, launch specs and detection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest
from unittest import mock

from luma_playground.agents import registry
from luma_playground.agents.connections import (
    Connection, ConnectionRuleError, ConnectionStore, LaunchError, TEAM_TASK_CAP_MAX, base_env, check_may_run,
    detect, launch_spec, suggest,
)


def plan(**kw) -> Connection:
    data = dict(id="claude", label="Claude", kind="personal-plan", provider="claude", owner="noor")
    data.update(kw)
    return Connection(**data)


class RuleTests(unittest.TestCase):
    def test_personal_plan_can_never_be_shared(self):
        with self.assertRaises(ConnectionRuleError) as caught:
            plan(sharing="team").validate()
        self.assertIn("just for you", str(caught.exception))
        # Ari on someone's ChatGPT plan is a personal plan too.
        with self.assertRaises(ConnectionRuleError):
            Connection(id="ari-chatgpt", label="Ari", kind="ari", provider="ari", endpoint="chatgpt",
                       sharing="team", spend_cap="$5").validate()

    def test_personal_plan_on_a_server_only_as_private_seat(self):
        with self.assertRaises(ConnectionRuleError):
            plan(where="dev-server").validate()
        plan(where="dev-server", private_seat=True).validate()
        with self.assertRaises(ConnectionRuleError):
            Connection(id="key", label="Key", kind="api-key", provider="claude", private_seat=True).validate()

    def test_team_tasks_are_capped_and_personal_only(self):
        plan(work_on_team_tasks=True).validate()  # default cap 20%
        for cap in (0, TEAM_TASK_CAP_MAX + 1, -5, 100):
            with self.subTest(cap=cap), self.assertRaises(ConnectionRuleError):
                plan(work_on_team_tasks=True, team_task_cap=cap).validate()
        with self.assertRaises(ConnectionRuleError):
            Connection(id="key", label="Key", kind="api-key", provider="claude",
                       work_on_team_tasks=True).validate()

    def test_shared_keys_need_a_spend_cap(self):
        key = Connection(id="team-key", label="Team key", kind="api-key", provider="claude", sharing="team")
        with self.assertRaises(ConnectionRuleError):
            key.validate()
        key.spend_cap = "$50 a month"
        key.validate()
        Connection(id="local", label="Local", kind="local", provider="ollama", sharing="team").validate()
        Connection(id="tk", label="Team", kind="team-key", provider="codex", sharing="team", spend_cap="$20").validate()
        with self.assertRaises(ConnectionRuleError):
            Connection(id="tk", label="Team", kind="team-key", provider="codex", spend_cap="$20").validate()

    def test_who_may_run(self):
        mine = plan()
        check_may_run(mine, "noor")
        check_may_run(mine, "noor", goal_owner="noor")
        with self.assertRaises(ConnectionRuleError) as caught:
            check_may_run(mine, "priya")
        self.assertIn("noor's own plan", str(caught.exception))
        with self.assertRaises(ConnectionRuleError):
            check_may_run(mine, "noor", goal_owner="priya")  # even with work_on_team_tasks
        shared = Connection(id="k", label="Key", kind="api-key", provider="claude", owner="noor",
                            sharing="team", spend_cap="$10")
        check_may_run(shared, "priya", goal_owner="priya")
        private_key = Connection(id="k2", label="Key", kind="api-key", provider="claude", owner="noor")
        with self.assertRaises(ConnectionRuleError):
            check_may_run(private_key, "priya")

    def test_run_labels(self):
        self.assertEqual(plan(owner="Priya").run_label(), "Runs on Priya's Claude")
        self.assertEqual(Connection(id="t", label="t", kind="team-key", provider="codex", sharing="team",
                                    spend_cap="$1").run_label(), "Runs on the team key")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(base / "config"),
                                               "PLAYGROUND_DATA_DIR": str(base / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = ConnectionStore()
        self.base = base

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_first_claude_uses_the_normal_login_and_the_second_gets_its_own_home(self):
        first = self.store.add(plan(id="claude-personal", label="Claude, personal"))
        second = self.store.add(plan(id="claude-luma", label="Claude, Luma"))
        self.assertEqual(first.config_home, "")
        self.assertEqual(second.config_home, str(self.base / "data" / "accounts" / "claude-luma"))
        codex = self.store.add(Connection(id="codex", label="Codex", kind="personal-plan", provider="codex", owner="noor"))
        self.assertEqual(codex.config_home, "")
        key = self.store.add(Connection(id="claude-key", label="Claude key", kind="api-key", provider="claude",
                                        owner="noor", key_ref="claude-key"))
        self.assertTrue(key.config_home.endswith("accounts/claude-key"))
        self.assertEqual([c.id for c in self.store.list()], ["claude-personal", "claude-luma", "codex", "claude-key"])

    def test_file_is_private_toml_with_no_secrets(self):
        self.store.add(plan(work_on_team_tasks=True, team_task_cap=15))
        path = self.base / "config" / "connections.toml"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        document = tomllib.loads(path.read_text())
        entry = document["connections"][0]
        self.assertEqual(entry["team_task_cap"], 15)
        self.assertTrue(entry["work_on_team_tasks"])
        again = self.store.get("claude")
        self.assertEqual(again.team_task_cap, 15)
        body = path.read_text().split("[[connections]]", 1)[1]
        self.assertNotIn("sk-", body)
        self.assertNotIn("token", body.lower())

    def test_store_enforces_rules(self):
        with self.assertRaises(ConnectionRuleError):
            self.store.add(plan(sharing="team"))
        self.store.add(plan())
        with self.assertRaises(ConnectionRuleError):
            self.store.add(plan())  # same id
        changed = self.store.get("claude")
        changed.sharing = "team"
        with self.assertRaises(ConnectionRuleError):
            self.store.update(changed)
        self.assertEqual(self.store.get("claude").sharing, "just-me")
        self.store.remove("claude")
        self.assertEqual(self.store.list(), [])

    def test_unknown_fields_survive(self):
        path = self.base / "config" / "connections.toml"
        path.parent.mkdir(parents=True)
        path.write_text('[[connections]]\nid = "x"\nlabel = "X"\nkind = "local"\nprovider = "ollama"\nfuture = "kept"\n')
        conn = self.store.get("x")
        self.assertEqual(conn.extra, {"future": "kept"})
        conn.label = "Y"
        self.store.update(conn)
        self.assertIn('future = "kept"', path.read_text())


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.bin = base / "bin"
        self.bin.mkdir()
        for name in ("npx", "goose", "pi"):
            exe = self.bin / name
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(base / "config"),
                                               "PLAYGROUND_DATA_DIR": str(base / "data"),
                                               "PLAYGROUND_OFFLINE": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reg = registry.load(offline=True, cache=base / "none.json")
        # Claude and Codex already set up by Playground, on Playground's own Node.
        from luma_playground.agents import runtime
        node = mock.patch.object(runtime, "find_node",
                                 return_value=runtime.Node("/pg/node/bin/node", "/pg/npm-cli.js", True))
        node.start()
        self.addCleanup(node.stop)
        self.scripts = {}
        for provider in ("claude", "codex"):
            entry = self.reg.for_provider(provider)
            name, version = registry.split_package(entry.distribution["npx"]["package"])
            pkg = base / "data" / "agents" / entry.id / version / "node_modules" / name
            (pkg / "dist").mkdir(parents=True)
            (pkg / "dist" / "index.js").write_text("")
            (pkg / "package.json").write_text(json.dumps({"bin": {name.split("/")[-1]: "dist/index.js"}}))
            self.scripts[provider] = str((pkg / "dist" / "index.js").resolve())
        self.env = {"PATH": str(self.bin), "HOME": str(base), "ANTHROPIC_API_KEY": "sk-should-go",
                    "ANTHROPIC_BASE_URL": "https://gateway", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x",
                    "CLAUDE_CODE_OAUTH_TOKEN": "tok", "CLAUDE_CODE_USE_BEDROCK": "0", "OPENAI_API_KEY": "sk-o"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_login_claude(self):
        argv, env = launch_spec(plan(), self.reg, env=self.env)
        self.assertEqual(argv, ["/pg/node/bin/node", self.scripts["claude"]])
        self.assertTrue(env["PATH"].startswith("/pg/node/bin" + os.pathsep))
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)    # the plan, not an API key, pays
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("CLAUDECODE", env)           # not part of a host agent's session
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")   # the person's own plan token stays
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "0")      # their own settings stay

    def test_second_account_gets_its_home(self):
        home = str(Path(self.tmp.name) / "data" / "accounts" / "claude-luma")
        argv, env = launch_spec(plan(id="claude-luma", config_home=home), self.reg, env=self.env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], home)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)  # it would override this home's sign-in
        self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)
        codex_home = str(Path(self.tmp.name) / "data" / "accounts" / "codex-2")
        argv, env = launch_spec(Connection(id="codex-2", label="Codex 2", kind="personal-plan", provider="codex",
                                           config_home=codex_home), self.reg, env=self.env)
        self.assertEqual(argv[-1], self.scripts["codex"])
        self.assertEqual(env["CODEX_HOME"], codex_home)
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_api_key_comes_from_the_keyring_lookup(self):
        key = Connection(id="claude-key", label="Claude key", kind="api-key", provider="claude")
        argv, env = launch_spec(key, self.reg, env=self.env, key_lookup=lambda c: "sk-from-keyring")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-from-keyring")
        with self.assertRaises(LaunchError):
            launch_spec(key, self.reg, env=self.env, key_lookup=lambda c: None)

    def test_missing_programs_are_plain(self):
        with self.assertRaises(LaunchError) as caught:
            launch_spec(Connection(id="gemini", label="Gemini", kind="api-key", provider="gemini"), self.reg,
                        env={"PATH": "/nonexistent"}, key_lookup=lambda c: "k")
        self.assertIn("isn't set up", str(caught.exception))
        with self.assertRaises(LaunchError):
            launch_spec(Connection(id="ari", label="Ari", kind="ari", provider="ari"), self.reg, env=self.env)
        argv, _ = launch_spec(Connection(id="goose", label="goose", kind="api-key", provider="goose",
                                         key_env="X"), self.reg, env=self.env, key_lookup=lambda c: "k")
        self.assertEqual(argv, [str(self.bin / "goose"), "acp"])

    def test_pi_launch_spec_uses_the_program_on_path(self):
        conn = Connection(id="pi", label="Pi", kind="personal-plan", provider="pi", owner="noor")
        argv, env = launch_spec(conn, self.reg, env=self.env)
        self.assertEqual(argv, [str(self.bin / "pi")])
        with self.assertRaises(LaunchError):
            launch_spec(conn, self.reg, env={"PATH": "/nonexistent"})

    def test_custom_command(self):
        conn = Connection(id="kiro", label="Kiro", kind="personal-plan", provider="kiro", command=["kiro-cli", "acp"])
        argv, _ = launch_spec(conn, self.reg, env=self.env)
        self.assertEqual(argv, ["kiro-cli", "acp"])

    def test_base_env_strips_host_session_only(self):
        env = base_env({"CLAUDECODE": "1", "CLAUDE_AGENT_SDK_VERSION": "1", "CLAUDE_CODE_ENTRYPOINT": "x",
                        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "100", "PATH": "/bin"})
        self.assertEqual(env, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "100", "PATH": "/bin"})


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_DATA_DIR": str(self.bin / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reg = registry.builtin_registry()
        from luma_playground.agents import connections as conn_mod
        self.conn_mod = conn_mod
        for name, value in (("BUNDLED", {}), ("APPS", {})):
            p = mock.patch.object(conn_mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        from luma_playground.agents import accounts
        p = mock.patch.object(accounts, "SYSTEM_BIN_DIRS", ())   # this computer's own programs stay out of it
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def detect(self, **env):
        env.setdefault("PATH", str(self.bin))
        env.setdefault("HOME", str(self.bin / "home"))
        return {d.provider: d for d in detect(env=env, check_ollama=False, registry=self.reg)}

    def stub(self, name: str, script: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)

    def test_detect_reads_status_without_secrets(self):
        self.stub("claude", 'case "$1" in --version) echo "2.1.273 (Claude Code)";; '
                            'auth) echo \'{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}\';; esac\n')
        self.stub("codex", 'case "$1" in --version) echo "codex-cli 0.153.4";; '
                           'login) echo "Logged in using ChatGPT";; esac\n')
        found = self.detect()
        self.assertTrue(found["claude"].signed_in)
        self.assertEqual(found["claude"].method, "subscription")
        self.assertEqual(found["claude"].account, "max")
        self.assertEqual(found["claude"].version, "2.1.273")
        self.assertTrue(found["codex"].signed_in)
        self.assertEqual(found["codex"].method, "ChatGPT")
        self.assertFalse(found["gemini"].installed)
        self.assertNotIn("node", found)          # Node.js is Playground's business, never the person's
        offered = suggest(list(found.values()), "noor")
        self.assertEqual([(c.provider, c.kind) for c in offered], [("claude", "personal-plan"), ("codex", "personal-plan")])

    def test_detect_signed_out_and_masked_key_is_not_kept(self):
        self.stub("codex", 'case "$1" in login) echo "Logged in using an API key - sk-proj-***ABCD";; esac\n')
        self.stub("claude", 'echo \'{"loggedIn": false, "authMethod": "none"}\'\n')
        found = self.detect()
        self.assertFalse(found["claude"].signed_in)
        self.assertEqual(found["codex"].method, "api key")
        self.assertNotIn("sk-", repr(found["codex"].to_dict()))

    def test_codex_inside_the_chatgpt_app_is_found_and_asked(self):
        app = self.bin / "app"
        app.mkdir()
        codex = app / "codex"
        codex.write_text('#!/bin/sh\ncase "$1" in login) echo "Logged in using ChatGPT";; esac\n')
        codex.chmod(0o755)
        with mock.patch.object(self.conn_mod, "BUNDLED", {"codex": (("the ChatGPT app", (str(codex),)),)}):
            found = self.detect(PATH="/nonexistent")
        item = found["codex"]
        self.assertEqual((item.program, item.via, item.signed_in, item.method),
                         (str(codex), "the ChatGPT app", True, "ChatGPT"))
        offered = suggest([item], "noor")
        self.assertEqual([(c.provider, c.kind) for c in offered], [("codex", "personal-plan")])

    def test_nothing_installed_is_still_offered_for_setup(self):
        with mock.patch("luma_playground.agents.registry.can_install", return_value=True):
            found = self.detect(PATH="/nonexistent")
        for provider in ("claude", "codex", "gemini"):
            self.assertFalse(found[provider].installed)
            self.assertFalse(found[provider].ready)
            self.assertTrue(found[provider].can_setup, provider)
        self.assertFalse(found["claude"].signed_in)

    def test_sign_in_files_are_only_checked_for_presence(self):
        home = self.bin / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "auth.json").write_text('{"tokens": "secret"}')
        (home / ".claude").mkdir()
        (home / ".claude" / ".credentials.json").write_text('{"secret": 1}')
        found = self.detect(PATH="/nonexistent")
        self.assertTrue(found["codex"].signed_in)
        self.assertTrue(found["claude"].signed_in)
        self.assertNotIn("secret", repr([d.to_dict() for d in found.values()]))

    def test_desktop_app_is_noticed(self):
        apps = self.bin / "share" / "applications"
        apps.mkdir(parents=True)
        (apps / "x.desktop").write_text("[Desktop Entry]\nName=Claude\nExec=claude-desktop\n")
        with mock.patch.object(self.conn_mod, "APPS", {"claude": ("Claude", "/nonexistent/Claude.app")}):
            found = self.detect(PATH="/nonexistent", XDG_DATA_HOME=str(self.bin / "share"))
        self.assertIn("Claude app", found["claude"].note)


if __name__ == "__main__":
    unittest.main()
