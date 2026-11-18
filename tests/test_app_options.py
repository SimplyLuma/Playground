# SPDX-License-Identifier: Apache-2.0
"""Agent-supplied option ids make a valid menu. Skips without the Luma kit."""
from __future__ import annotations

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


class OptionsMenuTests(unittest.TestCase):
    def test_opencode_model_ids_make_a_valid_menu(self):
        window = gtk_module("luma_playground.app.window")
        import gi
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gio, GLib
        from luma_appkit.menus import command_menu

        captured = {}

        def fake_popover(registry, **_kw):
            captured["registry"] = registry
            return mock.MagicMock()

        options = [{
            "id": "model", "name": "Model", "category": "model",
            "currentValue": "ollama-cloud/deepseek-v4-pro:0813",
            "options": [
                {"value": "ollama-cloud/deepseek-v4-pro", "name": "DeepSeek V4 Pro"},
                {"value": "ollama-cloud/deepseek-v4-pro:0813", "name": "DeepSeek V4 Pro 0813"},
                {"value": "ollama-cloud/deepseek-v4-pro", "name": "A duplicate id"},
                {"value": "opencode/hy4-flash-free", "name": "Hy4 Flash"},
            ],
        }]
        stub = mock.MagicMock()
        stub.goal_id = "g1"
        stub._set_option = lambda *_a: None
        stub._toast = lambda *_a: None
        stub.composer.options_button = object()

        with mock.patch.object(window, "command_popover", fake_popover):
            window.PlaygroundWindow._present_options(stub, options)
        registry = captured["registry"]
        # IDs are unique even though two choices sanitise to the same string.
        ids = list(registry._commands)
        self.assertEqual(len(ids), len(set(ids)))

        def walk(menu):
            for i in range(menu.get_n_items()):
                action = menu.get_item_attribute_value(i, "action", GLib.VariantType.new("s"))
                if action is not None:
                    Gio.Action.parse_detailed_name(action.get_string())  # the crash used to happen here
                submenu = menu.get_item_link(i, "submenu")
                if submenu is not None:
                    walk(submenu)
        # The kit builds its popover from exactly this menu model.
        walk(command_menu(registry, custom={}, own_rows=True))

    def test_creating_a_runner_does_not_warm_the_model_list(self):
        window = gtk_module("luma_playground.app.window")
        stub = mock.MagicMock()
        stub.project = mock.Mock(path="/tmp/project")
        stub.runners = {}
        runner = mock.Mock()
        module = mock.Mock(GoalRunner=mock.Mock(return_value=runner))
        with mock.patch.object(window, "_optional", return_value=module):
            made = window.PlaygroundWindow._runner(stub, "g1", create=True)
        self.assertIs(made, runner)
        runner.warm_options.assert_not_called()

    def test_a_running_step_by_another_agent_does_not_hide_thinking(self):
        window = gtk_module("luma_playground.app.window")
        stub = mock.MagicMock()
        stub.project = mock.Mock()
        stub.goal_id = "g1"
        stub.project.thread.return_value = [{"kind": "step", "by": "opencode", "data": {"state": "running"}}]
        self.assertFalse(window.PlaygroundWindow._step_running(stub, "pi"))
        self.assertTrue(window.PlaygroundWindow._step_running(stub, "opencode"))
    def test_stale_agent_events_from_the_old_connection_are_ignored(self):
        window = gtk_module("luma_playground.app.window")
        stub = mock.MagicMock()
        stub.goal_id = "g1"
        stub.agent_states = {}
        stub.project = mock.Mock(path="/tmp/project")
        stub.runners = {("/tmp/project", "g1"): mock.Mock(connection=mock.Mock(id="pi"))}
        stub._mine.return_value = True
        stub._step_running.return_value = False
        stub._notify = mock.Mock()
        stub._current_runner_is.side_effect = lambda goal, key: window.PlaygroundWindow._current_runner_is(stub, goal, key)
        window.PlaygroundWindow._on_agent(stub, goal_id="g1", state="working", connection="opencode")
        self.assertEqual(stub.agent_states, {})
        stub.conversation.set_thinking.assert_not_called()
        window.PlaygroundWindow._on_agent(stub, goal_id="g1", state="working", connection="pi")
        self.assertEqual(stub.agent_states["g1"], "working")
        stub.conversation.set_thinking.assert_called_once()

    def test_agent_state_names_the_connection_the_runner_is_using(self):
        window = gtk_module("luma_playground.app.window")
        stub = mock.MagicMock()
        stub.goal_id = "g1"
        stub.agent_states = {}
        stub._mine.return_value = True
        stub._step_running.return_value = False
        stub._notify = mock.Mock()
        stub.connection.return_value = {"id": "pi", "label": "Pi"}
        stub._agent_for.return_value = {"id": "opencode", "label": "OpenCode"}
        window.PlaygroundWindow._on_agent(stub, goal_id="g1", state="working", connection="pi")
        stub.connection.assert_called_once_with("pi")
        stub.conversation.set_thinking.assert_called_once_with("Pi", "working")
        stub._notify.assert_called_once_with("g1", "working", "", "pi")

    def test_opening_the_options_menu_warms_the_model_list(self):
        window = gtk_module("luma_playground.app.window")
        stub = mock.MagicMock()
        stub.goal_id = "g1"
        runner = mock.Mock()
        runner.options.return_value = [{"id": "model", "name": "Model", "category": "model",
                                        "options": [{"value": "m1", "name": "m1"}]}]
        stub._runner.return_value = runner
        stub._present_options = mock.Mock()
        window.PlaygroundWindow._options_menu(stub)
        runner.warm_options.assert_called_once_with()
        stub._present_options.assert_called_once()


if __name__ == "__main__":
    unittest.main()
