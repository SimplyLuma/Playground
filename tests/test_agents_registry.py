# SPDX-License-Identifier: Apache-2.0
"""The ACP registry: parsing, launch commands, caching and offline mode. No network."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from luma_playground.agents import registry

SAMPLE = {
    "version": "1.0.0",
    "agents": [
        {"id": "claude-acp", "name": "Claude Agent", "version": "0.88.0", "authors": ["Anthropic"],
         "distribution": {"npx": {"package": "@agentclientprotocol/claude-agent-acp@0.88.0"}}},
        {"id": "gemini", "name": "Gemini CLI", "version": "0.63.0",
         "distribution": {"npx": {"package": "@google/gemini-cli@0.63.0", "args": ["--acp"]}}},
        {"id": "fast-agent", "name": "fast-agent", "version": "0.10.1",
         "distribution": {"uvx": {"package": "fast-agent-acp==0.10.1", "args": ["-x"],
                                  "env": {"FAST_AGENT_MODEL": "codexplan"}}}},
        {"id": "goose", "name": "goose", "version": "1.54.0",
         "distribution": {"binary": {
             "linux-x86_64": {"archive": "https://example.invalid/goose.tar.bz2", "cmd": "./goose",
                              "args": ["acp"], "sha256": "ab" * 32},
             "darwin-aarch64": {"archive": "https://example.invalid/goose-mac.tar.bz2", "cmd": "./goose",
                                "args": ["acp"]}}}},
        {"id": "broken"},
        {"id": "no-dist", "distribution": {"carrier-pigeon": {}}},
    ],
}


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache" / "registry.json"
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_DATA_DIR": str(Path(self.tmp.name) / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_cache(self, age_s: float) -> None:
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps({"fetched_at": time.time() - age_s, "document": SAMPLE}))

    def test_parse_skips_bad_entries(self):
        agents = registry.parse(SAMPLE)
        self.assertEqual(sorted(agents), ["claude-acp", "fast-agent", "gemini", "goose"])
        self.assertEqual(agents["claude-acp"].authors, ["Anthropic"])
        with self.assertRaises(registry.RegistryError):
            registry.parse({"agents": "nope"})

    def test_npx_uvx_launch(self):
        agents = registry.parse(SAMPLE)
        claude = registry.launch_command(agents["claude-acp"], path_env="")
        # Not installed yet: Playground sets it up itself, nothing for the person to install.
        self.assertEqual(claude.kind, "npm")
        self.assertTrue(claude.needs_install)
        self.assertEqual(claude.missing, "")
        self.assertEqual(claude.package, "@agentclientprotocol/claude-agent-acp@0.88.0")
        gemini = registry.launch_command(agents["gemini"])
        self.assertEqual(gemini.argv[-1], "--acp")
        fast = registry.launch_command(agents["fast-agent"])
        self.assertEqual(fast.argv, ["uvx", "fast-agent-acp==0.10.1", "-x"])
        self.assertEqual(fast.env, {"FAST_AGENT_MODEL": "codexplan"})

    def test_binary_launch_per_platform(self):
        goose = registry.parse(SAMPLE)["goose"]
        cmd = registry.launch_command(goose, plat="linux-x86_64", path_env="")
        self.assertTrue(cmd.needs_install)
        self.assertEqual(cmd.sha256, "ab" * 32)
        self.assertEqual(cmd.argv[-1], "acp")
        self.assertIn(os.path.join("agents", "goose", "1.54.0"), cmd.argv[0])
        with self.assertRaises(registry.RegistryError):
            registry.launch_command(goose, plat="windows-x86_64")

    def test_binary_on_path_is_preferred(self):
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        exe = bindir / "goose"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        cmd = registry.launch_command(registry.parse(SAMPLE)["goose"], plat="linux-x86_64", path_env=str(bindir))
        self.assertEqual(cmd.argv, [str(exe), "acp"])
        self.assertFalse(cmd.needs_install)

    def test_offline_without_cache_uses_builtins(self):
        reg = registry.load(offline=True, cache=self.cache)
        self.assertEqual(reg.source, "builtin")
        for provider, package in (("claude", "@agentclientprotocol/claude-agent-acp@0.89.1"),
                                  ("codex", "@agentclientprotocol/codex-acp@2.2.2"),
                                  ("gemini", "@google/gemini-cli@0.63.0")):
            cmd = registry.launch_command(reg.for_provider(provider), path_env="")
            self.assertEqual((cmd.kind, cmd.package, cmd.needs_install), ("npm", package, True))
        for provider, argv in (("opencode", ["opencode", "acp"]),
                               ("goose", ["goose", "acp"])):
            cmd = registry.launch_command(reg.for_provider(provider), path_env="")
            self.assertEqual(cmd.argv, argv)
            self.assertEqual(cmd.missing, argv[0])

    def test_pi_builtin_runs_the_program_on_path(self):
        reg = registry.load(offline=True, cache=self.cache)
        entry = reg.for_provider("pi")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.distribution, {"path": {"cmd": "pi"}})
        command = registry.launch_command(entry, path_env="")
        self.assertEqual((command.kind, command.argv, command.missing), ("path", ["pi"], "pi"))

    def test_fresh_cache_is_used_without_network(self):
        self.write_cache(age_s=60)
        with mock.patch.object(registry, "_fetch", side_effect=AssertionError("no network in tests")):
            reg = registry.load(offline=False, cache=self.cache)
        self.assertEqual(reg.source, "cache")
        self.assertEqual(reg.get("claude-acp").version, "0.88.0")

    def test_stale_cache_when_fetch_fails(self):
        self.write_cache(age_s=registry.CACHE_TTL_S + 60)
        with mock.patch.object(registry, "_fetch", side_effect=OSError("offline")):
            reg = registry.load(offline=False, cache=self.cache)
        self.assertEqual(reg.source, "stale-cache")
        self.assertIn("gemini", reg.agents)

    def test_fetch_writes_cache(self):
        with mock.patch.object(registry, "_fetch", return_value=SAMPLE):
            reg = registry.load(offline=False, force=True, cache=self.cache)
        self.assertEqual(reg.source, "network")
        self.assertTrue(self.cache.exists())
        reg2 = registry.load(offline=True, cache=self.cache)
        self.assertEqual(reg2.source, "cache")

    def test_environment_offline_switch(self):
        with mock.patch.dict(os.environ, {"PLAYGROUND_OFFLINE": "1"}), \
                mock.patch.object(registry, "_fetch", side_effect=AssertionError("no network")):
            self.assertEqual(registry.load(cache=self.cache).source, "builtin")


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_DATA_DIR": str(Path(self.tmp.name) / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def archive(self, members: dict[str, bytes], name="agent.tar.gz", link: tuple | None = None) -> tuple[str, str]:
        path = Path(self.tmp.name) / name
        with tarfile.open(path, "w:gz") as tf:
            for member, data in members.items():
                info = tarfile.TarInfo(member)
                info.size = len(data)
                info.mode = 0o755
                tf.addfile(info, io.BytesIO(data))
            if link:
                info = tarfile.TarInfo(link[0])
                info.type = tarfile.SYMTYPE
                info.linkname = link[1]
                tf.addfile(info)
        return path.as_uri(), hashlib.sha256(path.read_bytes()).hexdigest()

    def entry(self, url: str, sha: str) -> registry.AgentEntry:
        return registry.AgentEntry.from_json({"id": "toy", "name": "Toy", "version": "1.0", "distribution": {
            "binary": {"linux-x86_64": {"archive": url, "cmd": "./bin/toy", "args": ["acp"], "sha256": sha}}}})

    def test_install_checks_sha_and_unpacks(self):
        url, sha = self.archive({"bin/toy": b"#!/bin/sh\necho toy\n"})
        cmd = registry.install_binary(self.entry(url, sha), plat="linux-x86_64")
        self.assertFalse(cmd.needs_install)
        self.assertTrue(os.access(cmd.argv[0], os.X_OK))

    def test_bad_checksum_is_thrown_away(self):
        url, _ = self.archive({"bin/toy": b"x"})
        with self.assertRaises(registry.RegistryError):
            registry.install_binary(self.entry(url, "00" * 32), plat="linux-x86_64")
        self.assertFalse(any((Path(self.tmp.name) / "data" / "agents" / "toy").glob("*/bin/toy")))

    def test_path_traversal_in_archive_is_refused(self):
        url, sha = self.archive({"../../evil": b"x", "bin/toy": b"x"}, name="evil.tar.gz")
        with self.assertRaises(registry.RegistryError):
            registry.install_binary(self.entry(url, sha), plat="linux-x86_64")
        url, sha = self.archive({"bin/toy": b"x"}, name="link.tar.gz", link=("bin/escape", "../../../../etc"))
        with self.assertRaises(registry.RegistryError):
            registry.install_binary(self.entry(url, sha), plat="linux-x86_64")

    def test_plain_http_is_refused(self):
        entry = self.entry("http://example.invalid/toy.tar.gz", "")
        with self.assertRaises(registry.RegistryError):
            registry.install_binary(entry, plat="linux-x86_64")


class NpmInstallTests(unittest.TestCase):
    """npm agents install into Playground's folder; no npx, no network (npm is faked)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_DATA_DIR": str(Path(self.tmp.name) / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        from luma_playground.agents import runtime
        self.node = runtime.Node("/fake/bin/node", "/fake/npm-cli.js", True)
        for target in ("find_node", "ensure_node"):
            p = mock.patch.object(runtime, target, return_value=self.node)
            p.start()
            self.addCleanup(p.stop)
        self.calls = []

    def entry(self, version: str) -> registry.AgentEntry:
        return registry.AgentEntry.from_json({"id": "claude-acp", "name": "Claude Agent", "version": version,
                                              "distribution": {"npx": {"package": f"@acp/claude@{version}",
                                                                       "args": ["--acp"]}}})

    def fake_npm(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get("env", {})))
        prefix = Path(argv[argv.index("--prefix") + 1])
        pkg = prefix / "node_modules" / "@acp" / "claude"
        (pkg / "dist").mkdir(parents=True)
        (pkg / "dist" / "index.js").write_text("console.log('hi')\n")
        (pkg / "package.json").write_text(json.dumps({"name": "@acp/claude", "bin": {"claude": "dist/index.js"}}))
        return mock.Mock(returncode=0, stdout="", stderr="")

    def test_install_then_launch_with_playgrounds_node(self):
        cmd = registry.install_package(self.entry("1.0.0"), runner=self.fake_npm)
        self.assertFalse(cmd.needs_install)
        self.assertEqual(cmd.argv[0], "/fake/bin/node")
        self.assertTrue(cmd.argv[1].endswith("dist/index.js"))
        self.assertEqual(cmd.argv[2:], ["--acp"])
        self.assertEqual(cmd.path_prepend, "/fake/bin")
        argv, env = self.calls[0]
        self.assertIn("--ignore-scripts", argv)
        self.assertEqual(argv[-1], "@acp/claude@1.0.0")
        # The person's own npm settings can't redirect the install.
        self.assertEqual(env["npm_config_registry"], "https://registry.npmjs.org/")
        self.assertNotIn(str(Path.home() / ".npmrc"), env["npm_config_userconfig"])

    def test_new_version_keeps_the_old_one_running_until_installed(self):
        registry.install_package(self.entry("1.0.0"), runner=self.fake_npm)
        newer = registry.launch_command(self.entry("1.1.0"))
        self.assertFalse(newer.needs_install)
        self.assertTrue(newer.update_available)
        self.assertIn("1.0.0", newer.install_dir)
        done = registry.install_package(self.entry("1.1.0"), runner=self.fake_npm)
        self.assertIn("1.1.0", done.install_dir)
        self.assertFalse(done.update_available)
        left = sorted(p.name for p in (Path(self.tmp.name) / "data" / "agents" / "claude-acp").iterdir())
        self.assertEqual(left, ["1.1.0"])

    def test_failed_install_leaves_nothing_behind(self):
        def broken(argv, **kwargs):
            return mock.Mock(returncode=1, stdout="", stderr="npm error code ENOTFOUND\n")
        with self.assertRaises(registry.RegistryError) as raised:
            registry.install_package(self.entry("1.0.0"), runner=broken)
        self.assertIn("internet connection", str(raised.exception))
        self.assertTrue(registry.launch_command(self.entry("1.0.0")).needs_install)
        left = list((Path(self.tmp.name) / "data" / "agents" / "claude-acp").iterdir())
        self.assertEqual(left, [])

    def test_split_package(self):
        self.assertEqual(registry.split_package("@a/b@1.2.3"), ("@a/b", "1.2.3"))
        self.assertEqual(registry.split_package("@a/b"), ("@a/b", ""))
        self.assertEqual(registry.split_package("plain@2"), ("plain", "2"))


if __name__ == "__main__":
    unittest.main()
