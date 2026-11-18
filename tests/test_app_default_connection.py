# SPDX-License-Identifier: Apache-2.0
"""New conversations use a real Ari connection, else the first agent set up here. Skips without the Luma kit."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


def gtk_module(name: str):
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        import importlib
        return importlib.import_module(name)
    except (ImportError, ValueError, RuntimeError) as error:
        raise unittest.SkipTest(f"GTK isn't here: {error}")


class DefaultConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"PLAYGROUND_CONFIG_DIR": str(Path(self.tmp.name) / "config")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _default(self, window, items):
        store = mock.Mock()
        store.list.return_value = items
        module = mock.Mock()
        module.ConnectionStore.return_value = store
        with mock.patch.object(window, "_optional", return_value=module):
            return window.default_connection_id()

    @staticmethod
    def _conn(id, provider, *, kind="personal-plan", where="this-computer"):
        return mock.Mock(id=id, provider=provider, kind=kind, where=where)

    def test_first_local_agent_wins_whatever_its_provider(self):
        window = gtk_module("luma_playground.app.window")
        opencode = self._conn("opencode", "opencode")
        claude = self._conn("claude", "claude")
        codex = self._conn("codex", "codex")
        self.assertEqual(self._default(window, [opencode]), "opencode")
        self.assertEqual(self._default(window, [claude]), "claude")
        self.assertEqual(self._default(window, [claude, codex]), "claude")
        self.assertEqual(self._default(window, [codex, claude]), "codex")

    def test_a_real_ari_connection_still_wins(self):
        window = gtk_module("luma_playground.app.window")
        ari = self._conn("ari-chatgpt", "ari", kind="ari")
        self.assertEqual(self._default(window, [self._conn("opencode", "opencode"), ari]), "ari-chatgpt")

    def test_nothing_usable_falls_back_to_the_ari_placeholder(self):
        window = gtk_module("luma_playground.app.window")
        self.assertEqual(self._default(window, []), "ari")
        self.assertEqual(self._default(window, [self._conn("qwen", "ollama", kind="local")]), "ari")
        self.assertEqual(self._default(window, [self._conn("claude-2", "claude", where="other-computer")]), "ari")

    def test_a_passed_snapshot_is_used_without_reading_the_file(self):
        window = gtk_module("luma_playground.app.window")
        found = [{"id": "opencode", "label": "OpenCode", "provider": "opencode", "kind": "personal-plan",
                  "where": "this-computer", "_object": self._conn("opencode", "opencode")}]
        with mock.patch.object(window, "_optional", side_effect=AssertionError("read the connections file")):
            self.assertEqual(window.default_connection_id(found), "opencode")

    def test_the_ari_stand_in_does_not_beat_a_real_agent(self):
        window = gtk_module("luma_playground.app.window")
        found = [dict(window.ARI),
                 {"id": "opencode", "provider": "opencode", "kind": "personal-plan", "where": "this-computer"}]
        self.assertEqual(window.default_connection_id(found), "opencode")

    def test_new_conversation_only_offers_the_picker_with_several_connections(self):
        window = gtk_module("luma_playground.app.window")
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        stub = mock.MagicMock()
        stub.project.thread.return_value = [{"role": "person"}]
        stub.default_connection_id.return_value = "opencode"
        real = {"id": "opencode", "label": "OpenCode", "_object": mock.Mock()}
        dialog = mock.MagicMock()
        with mock.patch.object(window.Adw, "AlertDialog", return_value=dialog):
            stub.connections = [dict(window.ARI), real]
            window.PlaygroundWindow.new_goal(stub)
            self.assertIsInstance(dialog.set_extra_child.call_args.args[0], Gtk.Entry)
            dialog.reset_mock()
            stub.connections = [dict(window.ARI), real,
                                {"id": "pi", "label": "Pi", "_object": mock.Mock()}]
            window.PlaygroundWindow.new_goal(stub)
        child = dialog.set_extra_child.call_args.args[0]
        self.assertIsInstance(child, Gtk.Box)
        picker = child.get_first_child().get_next_sibling()
        self.assertIsInstance(picker, Gtk.DropDown)
        model = picker.get_model()
        self.assertEqual([model.get_string(i) for i in range(model.get_n_items())],
                         ["OpenCode", "Pi", "Add Connection..."])
        child.get_first_child().set_text("A draft title")
        picker.set_selected(2)
        stub.remember.assert_any_call("new_conversation_title", "A draft title")
        dialog.close.assert_called_once_with()
        stub.open_sheet.assert_called_once_with("picker", page="add")
        self.assertEqual(picker.get_selected(), 0)
    def test_new_conversation_restores_and_clears_the_title_draft(self):
        window = gtk_module("luma_playground.app.window")
        stub = mock.MagicMock()
        stub.project.thread.return_value = [{"role": "person"}]
        stub.recall.side_effect = lambda key, default=None: "Saved title" if key == "new_conversation_title" else default
        dialog = mock.MagicMock()
        with mock.patch.object(window.Adw, "AlertDialog", return_value=dialog):
            window.PlaygroundWindow.new_goal(stub)
        entry = dialog.set_extra_child.call_args.args[0]
        self.assertEqual(entry.get_text(), "Saved title")
        respond = dialog.connect.call_args.args[1]
        respond(dialog, "cancel")
        stub.remember.assert_called_once_with("new_conversation_title", "")

    def test_a_conversation_without_any_turn_is_not_started_yet(self):
        window = gtk_module("luma_playground.app.window")
        project = window.ProjectData.__new__(window.ProjectData)
        project.thread = lambda _goal_id: []
        self.assertTrue(project._not_started(mock.Mock(id="g1")))
        project.thread = lambda _goal_id: [{"role": "person", "kind": "message", "text": "Hi"}]
        self.assertFalse(project._not_started(mock.Mock(id="g1")))
    def test_a_new_conversation_can_name_its_agent(self):
        window = gtk_module("luma_playground.app.window")
        project = window.ProjectData.__new__(window.ProjectData)
        project.store = mock.Mock()
        project.store.create_goal.return_value = mock.Mock(id="g1")
        project.default_id = "ari"
        project.forget = mock.Mock()
        project.create_goal("Two agents", "me", "pi")
        project.store.create_goal.assert_called_once_with("Two agents", owner="me", connection="pi")

    def test_project_rows_use_the_cached_snapshot(self):
        window = gtk_module("luma_playground.app.window")
        project = window.ProjectData.__new__(window.ProjectData)
        project.model = mock.Mock(members=[])
        project._you = "me"
        project.set_connections([{"id": "opencode", "label": "OpenCode", "provider": "opencode",
                                  "kind": "personal-plan", "where": "this-computer"}])
        self.assertEqual(project.default_id, "opencode")
        self.assertEqual(project.agent_words(""), "OpenCode")


if __name__ == "__main__":
    unittest.main()
