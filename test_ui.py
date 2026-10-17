"""Interaction checks against an isolated window; never send to a real agent."""
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from runtime import Binding
from ui import row_key


class FakeMonitor:
    def __init__(self, events):
        self.events = events
        self.commands = queue.Queue()
        self.stop_requested = threading.Event()


def task(pane, session, title="示例任务"):
    return dict(window_id=1, pane_id=pane, title=title, cwd="C:/Projects/example",
                task_state="running", agent={"session_id": session})


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for name, value in (("Monitor", FakeMonitor), ("CONFIG", Path(self.directory.name) / "settings.json")):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.view = app.App()
        self.view.withdraw()
        self.addCleanup(self.destroy_view)
        self.view.rows = [task(1, "first"), task(2, "second")]
        self.view.render_rows()

    def destroy_view(self):
        for timer in self.view.tk.call("after", "info"):
            self.view.tk.call("after", "cancel", timer)
        self.view.destroy()

    def bind(self):
        self.view.select_row(row_key(self.view.rows[0]))
        self.view.events.put(("bound", Binding(1, 1, "first", 42, "C:/Projects/example", "示例任务")))
        self.view.drain()

    def test_start_disabled_until_bound_and_settings_locked_while_running(self):
        self.assertEqual(self.view.start_button.cget("state"), "disabled")
        self.bind()
        self.assertEqual(self.view.start_button.cget("state"), "normal")
        self.view.start_button.invoke()
        name, values = self.view.monitor.commands.get_nowait()
        self.assertEqual(name, "start")
        self.assertEqual(values, (10, 5, 30, 6, "继续"))
        self.view.events.put(("enabled", True))
        self.view.drain()
        self.assertEqual(self.view.message_entry.cget("state"), "disabled")
        self.assertEqual(self.view.bind_button.cget("state"), "disabled")
        self.view.start_button.invoke()
        self.assertTrue(self.view.monitor.stop_requested.is_set())
        self.assertEqual(self.view.monitor.commands.get_nowait()[0], "pause")
        self.view.events.put(("enabled", False))
        self.view.drain()
        self.assertEqual(self.view.message_entry.cget("state"), "normal")

    def test_refresh_reorder_preserves_session_and_replacement_clears_selection(self):
        self.view.select_row(row_key(self.view.rows[0]))
        self.view.rows.reverse()
        self.view.render_rows()
        self.assertEqual(self.view.selected_row()["agent"]["session_id"], "first")
        self.view.rows[1] = task(1, "replacement")
        self.view.render_rows()
        self.assertIsNone(self.view.selected_row())
        self.assertEqual(self.view.bind_button.cget("state"), "disabled")

    def test_filtered_out_selection_cannot_be_bound(self):
        self.view.select_row(row_key(self.view.rows[0]))
        self.view.search.set("no-match")
        self.assertIsNone(self.view.selected_row())
        self.view.bind_selected()
        self.assertTrue(self.view.monitor.commands.empty())
        self.view.search.set("first")
        self.assertEqual(len(self.view.row_widgets), 1)
        self.assertIsNotNone(self.view.selected_row())

    def test_binding_locks_controls_and_targets_selected_session(self):
        self.view.select_row(row_key(self.view.rows[1]))
        self.view.bind_selected()
        self.assertTrue(self.view.binding_pending)
        self.assertEqual(self.view.bind_button.cget("state"), "disabled")
        self.view.bind_selected()
        self.assertEqual(self.view.monitor.commands.qsize(), 2)
        self.assertEqual(self.view.monitor.commands.get_nowait()[0], "pause")
        name, data = self.view.monitor.commands.get_nowait()
        self.assertEqual(name, "bind")
        self.assertEqual(data[1]["agent"]["session_id"], "second")
        self.view.events.put(("binding_done", None))
        self.view.drain()
        self.assertEqual(self.view.bind_button.cget("state"), "normal")

    def test_refresh_reenabled_after_error_and_preview_is_read_only(self):
        self.view.refresh()
        self.assertEqual(self.view.refresh_button.cget("state"), "disabled")
        self.view.events.put(("refresh_done", None))
        self.view.events.put(("status", "连接失败"))
        self.view.drain()
        self.assertEqual(self.view.refresh_button.cget("state"), "normal")
        self.bind()
        self.view.show_activity("窗格预览")
        self.view.events.put(("preview", "preview text"))
        self.view.drain()
        self.assertEqual(self.view.previewbox.get("1.0", "end-1c"), "preview text")
        self.assertEqual(self.view.previewbox.cget("state"), "disabled")
        self.assertIn("preview", [item[0] for item in self.view.monitor.commands.queue])

    def test_invalid_settings_do_not_start_or_save(self):
        self.bind()
        self.view.interval_var.set("0")
        with patch.object(app.messagebox, "showerror") as error:
            self.view.start()
        error.assert_called_once()
        self.assertTrue(self.view.monitor.commands.empty())
        self.assertFalse(app.CONFIG.exists())

    def test_wait_range_must_be_ordered(self):
        self.bind()
        self.view.wait_min_var.set("30")
        self.view.wait_max_var.set("5")
        with patch.object(app.messagebox, "showerror") as error:
            self.view.start()
        error.assert_called_once()
        self.assertTrue(self.view.monitor.commands.empty())
        self.assertFalse(app.CONFIG.exists())

    def test_rate_limit_warning_and_last_check_are_visible_and_can_clear(self):
        self.bind()
        self.view.events.put(("enabled", True))
        self.view.events.put(("checked", 1000000000))
        self.view.events.put(("health", {"title": "限流，正在重连", "tone": "warning"}))
        self.view.events.put(("status", "Codex 正在自行重连（1/10）"))
        self.view.drain()
        self.assertEqual(self.view.state_heading.cget("text"), "限流，正在重连")
        self.assertEqual(self.view.status_card.cget("fg_color"), "#FFF6E5")
        self.assertIn("最近检查", self.view.checked_var.get())
        self.view.events.put(("health", {"title": "", "tone": "normal"}))
        self.view.drain()
        self.assertEqual(self.view.state_heading.cget("text"), "正在监测")

    def test_disconnected_runtime_is_not_shown_as_monitoring(self):
        self.bind()
        self.view.events.put(("enabled", True))
        self.view.events.put(("connection_error", None))
        self.view.events.put(("enabled", False))
        self.view.drain()
        self.assertEqual(self.view.state_heading.cget("text"), "Pebrel 未连接")
        self.assertEqual(self.view.status_card.cget("fg_color"), "#FBEFEC")

    def test_automatic_discovery_retries_without_binding_or_starting(self):
        self.view.connection_ok = False
        self.view.reconnect_if_needed()
        self.assertEqual(self.view.monitor.commands.get_nowait()[0], "refresh")
        self.assertIsNone(self.view.bound)
        self.assertFalse(self.view.enabled)
        self.view.reconnect_if_needed()
        self.assertTrue(self.view.monitor.commands.empty())


if __name__ == "__main__":
    unittest.main()
