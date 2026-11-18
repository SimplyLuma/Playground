# SPDX-License-Identifier: Apache-2.0
"""The 'is thinking' line settles once, even while reasoning deltas keep arriving."""
from __future__ import annotations

import unittest


def gtk_module(name: str):
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        import importlib
        return importlib.import_module(name)
    except (ImportError, ValueError, RuntimeError) as error:
        raise unittest.SkipTest(f"GTK isn't here: {error}")


class ThinkingLineTests(unittest.TestCase):
    def test_repeated_same_state_does_not_postpone_the_settle(self):
        conversation_mod = gtk_module("luma_playground.app.conversation")
        from gi.repository import GLib
        thread = conversation_mod.Thread()
        thread.set_thinking("Pi", "working")
        first = thread._thinking_timer
        self.addCleanup(lambda: GLib.source_remove(first) if first else None)
        thread.set_thinking("Pi", "working")
        thread.set_thinking("Pi", "working")
        self.assertEqual(thread._thinking_timer, first)
        thread._apply_thinking()
        self.assertIsNotNone(thread.thinking)


if __name__ == "__main__":
    unittest.main()
