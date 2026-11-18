# SPDX-License-Identifier: Apache-2.0
"""The connection's Model row: 'Last used model' is the default choice."""
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


class ConnectionModelRowTests(unittest.TestCase):
    def test_last_used_model_stays_the_default_and_picking_a_model_saves_it(self):
        usage = gtk_module("luma_playground.app.sheets.usage")
        stub = mock.MagicMock()
        stub.detail = mock.Mock(model="")
        rows = [{"value": "m1", "name": "Model 1"}, {"value": "m2", "name": "Model 2"}]
        captured = {}

        def fake_option(_title, _sub, end):
            captured["end"] = end
            return mock.Mock()

        with mock.patch.object(usage, "option", side_effect=fake_option):
            usage.UsageSheet.model_row(stub, stub.detail, rows)
        drop = captured["end"]
        self.assertEqual(drop.get_selected(), 0)
        drop.set_selected(2)
        stub.change.assert_called_once_with(stub.detail, model="m2")

    def test_a_saved_model_is_selected(self):
        usage = gtk_module("luma_playground.app.sheets.usage")
        stub = mock.MagicMock()
        conn = mock.Mock(model="m1")
        stub.detail = conn
        captured = {}
        with mock.patch.object(usage, "option",
                               side_effect=lambda _t, _s, end: (captured.setdefault("end", end), mock.Mock())[1]):
            usage.UsageSheet.model_row(stub, conn, [{"value": "m1", "name": "Model 1"}])
        self.assertEqual(captured["end"].get_selected(), 1)


if __name__ == "__main__":
    unittest.main()
