import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import inspect_screen, RetryPolicy, Evidence
from runtime import Pebrel, Binding, RuntimeErrorInfo

FAULT = "■ stream disconnected before completion: error sending request\n\n› Ask Codex to do anything\n\n  GPT-6-Astra high · Desktop\n  ? for shortcuts"
RATE_MESSAGE = "Rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in koreacentral have exceeded token rate limit."
RECONNECTING = "Reconnecting... 1/10 (9m 48s • esc to interrupt)\nL " + RATE_MESSAGE
RATE_FAULT = "■ " + RATE_MESSAGE + "\n\n› Ask Codex to do anything\n  ? for shortcuts"


class ScreenTests(unittest.TestCase):
    def test_transient_at_ready_prompt(self):
        self.assertEqual(inspect_screen(FAULT, "failed").kind, "retry")

    def test_stale_running_hook_with_foreground_error(self):
        self.assertEqual(inspect_screen(FAULT, "running").kind, "retry")

    def test_active_work_not_interrupted(self):
        self.assertEqual(inspect_screen(FAULT + "\n• Working (esc to interrupt)", "running").kind, "busy")

    def test_previous_reconnecting_does_not_hide_terminal_failure(self):
        self.assertEqual(inspect_screen("Reconnecting 5/5\n" + FAULT, "failed").kind, "retry")

    def test_prose_is_not_error(self):
        text = "• Network error fixed; tests passed.\n› Ask Codex to do anything"
        self.assertEqual(inspect_screen(text, "idle").kind, "normal")

    def test_old_fault_followed_by_answer(self):
        text = FAULT.replace("\n\n›", "\n• All done, fixed it.\n\n›")
        self.assertEqual(inspect_screen(text, "finished").kind, "normal")

    def test_nonempty_draft(self):
        self.assertEqual(inspect_screen(FAULT.replace("Ask Codex to do anything", "部署到生产环境"), "idle").kind, "manual")

    def test_multiline_draft(self):
        self.assertEqual(inspect_screen(FAULT.replace("Ask Codex to do anything", "\n  用户尚未发送的草稿"), "idle").kind, "manual")

    def test_quota_and_auth_require_human(self):
        for reason in ("usage limit reached", "network error 401 unauthorized", "quota exceeded", "rate limit 429: insufficient_quota", "context window full"):
            with self.subTest(reason=reason):
                self.assertEqual(inspect_screen("■ " + reason + "\n› Ask Codex to do anything", "failed").kind, "manual")

    def test_approval_not_accepted(self):
        self.assertEqual(inspect_screen(FAULT.replace("error sending request", "network error. Would you like to approve this?"), "attention").kind, "manual")

    def test_unknown_and_exited(self):
        self.assertEqual(inspect_screen("■ something unusual\n› ", "failed").kind, "manual")
        self.assertEqual(inspect_screen(FAULT, "finished", True).kind, "manual")

    def test_missing_ready_prompt(self):
        self.assertEqual(inspect_screen("■ network error", "failed").kind, "manual")

    def test_live_reconnecting_rate_limit_without_final_error(self):
        for prefix in ("", "• ", "⠴ "):
            with self.subTest(prefix=prefix):
                ev = inspect_screen(prefix + RECONNECTING, "running")
                self.assertEqual((ev.kind, ev.signal), ("busy", "rate_limit"))
                self.assertIn("1/10", ev.reason)
                self.assertIn("限流", ev.reason)

    def test_reconnecting_before_placeholder_and_wrapped_rate_text(self):
        screen = RECONNECTING.replace("rate limit.", "rate\n  limit.") + "\n› Ask Codex to do anything"
        self.assertEqual(inspect_screen(screen, "failed").signal, "rate_limit")

    def test_completed_answer_supersedes_old_reconnecting_status(self):
        screen = RECONNECTING + "\n• 已完成任务\n› Ask Codex to do anything"
        self.assertEqual(inspect_screen(screen, "finished").kind, "normal")
        screen = RECONNECTING + "\n› 处理下一个任务"
        self.assertEqual(inspect_screen(screen, "running").signal, "")

    def test_prose_mentioning_reconnect_and_rate_limit_is_not_fault(self):
        screen = "• Reconnecting... 1/10 表示重连；Rate limit exceeded 表示限流。\n› Ask Codex to do anything"
        self.assertEqual(inspect_screen(screen, "finished").kind, "normal")

    def test_final_rate_limit_is_retryable_after_input_ready(self):
        ev = inspect_screen(RECONNECTING + "\n" + RATE_FAULT, "failed")
        self.assertEqual((ev.kind, ev.signal), ("retry", "rate_limit"))
        self.assertTrue(ev.fingerprint)

    def test_rate_limit_with_draft_or_missing_prompt_remains_manual(self):
        for screen in (RATE_FAULT.replace("Ask Codex to do anything", "未发送的草稿"), "■ " + RATE_MESSAGE):
            self.assertEqual(inspect_screen(screen, "failed").kind, "manual")

    def test_reconnecting_network_and_account_errors_are_distinct(self):
        self.assertEqual(inspect_screen(RECONNECTING.replace(RATE_MESSAGE, "network error"), "running").signal, "reconnecting")
        self.assertEqual(inspect_screen(RECONNECTING.replace(RATE_MESSAGE, "429 insufficient_quota"), "running").signal, "account")


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.ev = inspect_screen(FAULT, "failed")
        self.p = RetryPolicy(delay=30)

    def test_grace_period_and_backoff(self):
        self.assertEqual(self.p.observe(self.ev, 2, 100)[0], "countdown")
        self.assertEqual(self.p.observe(self.ev, 2, 129)[0], "countdown")
        self.assertEqual(self.p.observe(self.ev, 2, 130)[0], "send")
        self.p.dispatching(2)
        self.p.acknowledged()
        self.assertEqual(self.p.observe(Evidence("busy", "running"), 3, 140)[0], "watch")
        self.assertEqual(self.p.observe(self.ev, 4, 150)[0], "countdown")
        self.assertEqual(self.p.due, 210)

    def test_unknown_delivery_is_never_retried(self):
        self.p.observe(self.ev, 2, 0)
        self.p.dispatching(2)
        self.assertEqual(self.p.observe(self.ev, 4, 9999)[0], "hold")

    def test_ack_does_not_resend_same_fault(self):
        self.p.observe(self.ev, 2, 0)
        self.p.dispatching(2)
        self.p.acknowledged()
        for stamp in (60, 300, 9999):
            self.assertEqual(self.p.observe(self.ev, 2, stamp)[0], "watch")

    def test_no_reset_from_old_idle(self):
        self.p.dispatching(5)
        self.p.acknowledged()
        self.p.observe(Evidence("normal", "idle"), 5, 100)
        self.assertEqual(self.p.attempts, 1)

    def test_new_success_resets_budget(self):
        self.p.dispatching(5)
        self.p.acknowledged()
        self.p.observe(Evidence("normal", "finished"), 7, 100)
        self.assertEqual(self.p.attempts, 0)

    def test_limit(self):
        self.p.attempts = 6
        self.assertEqual(self.p.observe(self.ev, 20, 100)[0], "hold")

    def test_grace_restarts_on_state_change(self):
        self.p.observe(self.ev, 2, 0)
        self.p.observe(self.ev, 3, 29)
        self.assertEqual(self.p.due, 59)

    def test_temporary_rate_limit_uses_at_least_one_minute(self):
        ev = inspect_screen(RATE_FAULT, "failed")
        self.assertEqual(self.p.observe(ev, 2, 0)[0], "countdown")
        self.assertEqual(self.p.due, 60)
        self.assertEqual(self.p.observe(ev, 2, 59)[0], "countdown")
        self.assertEqual(self.p.observe(ev, 2, 60)[0], "send")
        self.p.dispatching(2)
        self.p.acknowledged()
        self.p.observe(ev, 3, 70)
        self.assertEqual(self.p.due, 190)

    def test_server_wait_hint_can_exceed_normal_backoff_cap(self):
        for hint, seconds in (("Retry-After: 900", 900), ("Please retry after 12 minutes", 720), ("Try again in 80 seconds", 80)):
            with self.subTest(hint=hint):
                ev = inspect_screen(RATE_FAULT.replace("\n\n›", "\n" + hint + "\n\n›"), "failed")
                policy = RetryPolicy()
                policy.observe(ev, 2, 100)
                self.assertEqual(policy.due, 100 + seconds)

    def test_awaiting_delivery_still_explains_reconnection(self):
        self.p.dispatching(2)
        self.p.acknowledged()
        action, note = self.p.observe(inspect_screen(RECONNECTING, "running"), 3, 100)
        self.assertEqual(action, "watch")
        self.assertIn("限流", note)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.client = Pebrel("unused")
        self.binding = Binding(1, 2, "original", 100, "C:/repo", "test")
        self.row = dict(window_id=1, pane_id=2, cwd="C:/repo", agent={"session_id": "original"})

    def test_replaced_session_rejected(self):
        self.row["agent"]["session_id"] = "replacement"
        with patch.object(self.client, "agents", return_value=[self.row]):
            with self.assertRaises(RuntimeErrorInfo):
                self.client.verify(self.binding)

    def test_replaced_process_rejected(self):
        with patch.object(self.client, "agents", return_value=[self.row]), patch.object(self.client, "processes", return_value=[{"pid": 101, "agent_kind": "codex"}]):
            with self.assertRaises(RuntimeErrorInfo):
                self.client.verify(self.binding, processes=True)

    def test_control_characters_rejected(self):
        for text in ("继续\n", "\x1b", "", "a" * 501):
            with self.assertRaises(RuntimeErrorInfo):
                self.client.send(self.binding, text)


class MonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Simulated dispatches must never appear in the user's production log.
        patcher = patch("app.logger")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_monitor(self, directory):
        import app
        m = app.Monitor.__new__(app.Monitor)
        m.events = queue.Queue()
        m.stop_requested = threading.Event()
        m.dispatch_lock = threading.Lock()
        m.enabled = True
        m.binding = Binding(1, 2, "test-session", 100, "C:/repo", "test")
        m.policy = RetryPolicy(delay=30)
        m.journal = {}
        m.last_note = ""
        m.message = "继续"
        m.runtime = self.FakeRuntime()
        return m

    class FakeRuntime:
        def __init__(self):
            self.seq = 2
            self.screen = FAULT
            self.state = "failed"
            self.sends = []
            self.failure = None
            self.before_verify = None

        def verify(self, binding, processes=False):
            if processes and self.before_verify:
                self.before_verify()
            return {"state_change_seq": self.seq, "task_state": self.state}

        def read(self, binding):
            return {"text": self.screen, "task_state": self.state, "exited": False}

        def send(self, binding, text):
            self.sends.append(text)
            if self.failure:
                raise self.failure
            return {"submitted": True}

    def test_failure_send_recovery_end_to_end(self):
        import app
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "JOURNAL", Path(tmp) / "journal.json"):
            m = self.make_monitor(tmp)
            m.poll()
            self.assertEqual(m.runtime.sends, [])
            m.policy.due = 0
            m.poll()
            self.assertEqual(m.runtime.sends, ["继续"])
            for _ in range(3):
                m.poll()
            self.assertEqual(m.runtime.sends, ["继续"])
            m.runtime.seq = 4
            m.runtime.state = "finished"
            m.runtime.screen = "• 修复完成\n› Ask Codex to do anything"
            m.poll()
            self.assertEqual(m.policy.attempts, 0)

    def test_timeout_persisted_before_send(self):
        import app
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "JOURNAL", Path(tmp) / "journal.json"):
            m = self.make_monitor(tmp)
            m.runtime.failure = TimeoutError("unknown delivery")
            m.poll()
            m.policy.due = 0
            with self.assertRaises(TimeoutError):
                m.poll()
            record = json.loads(app.JOURNAL.read_text())["test-session"]
            self.assertTrue(record["uncertain"])
            self.assertEqual(record["attempts"], 1)
            self.assertFalse(m.enabled)

    def test_pause_during_validation_cancels_dispatch(self):
        import app
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "JOURNAL", Path(tmp) / "journal.json"):
            m = self.make_monitor(tmp)
            m.poll()
            m.policy.due = 0
            m.runtime.before_verify = lambda: m.stop_requested.set()
            m.poll()
            self.assertEqual(m.runtime.sends, [])

    def test_new_draft_cancels_dispatch(self):
        import app
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "JOURNAL", Path(tmp) / "journal.json"):
            m = self.make_monitor(tmp)
            m.poll()
            m.policy.due = 0
            m.runtime.before_verify = lambda: setattr(m.runtime, "screen", FAULT.replace("Ask Codex to do anything", "我的新消息"))
            m.poll()
            self.assertEqual(m.runtime.sends, [])

    def test_reconnection_is_visible_logged_once_and_never_interrupted(self):
        import app
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "JOURNAL", Path(tmp) / "journal.json"):
            m = self.make_monitor(tmp)
            m.runtime.screen = RECONNECTING
            m.runtime.state = "running"
            for _ in range(4):
                m.poll()
            events = list(m.events.queue)
            self.assertEqual(m.runtime.sends, [])
            self.assertTrue(m.enabled)
            self.assertEqual(len([v for k, v in events if k == "checked"]), 4)
            self.assertEqual(len([v for k, v in events if k == "log"]), 1)
            self.assertIn(("health", {"title": "限流，正在重连", "tone": "warning"}), events)
            m.runtime.screen = "• 任务已完成\n› Ask Codex to do anything"
            m.runtime.state = "finished"
            m.poll()
            self.assertIn(("health", {"title": "", "tone": "normal"}), list(m.events.queue))

    def test_rate_limit_waits_then_sends_once_after_codex_stops_retrying(self):
        import app
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "JOURNAL", Path(tmp) / "journal.json"):
            m = self.make_monitor(tmp)
            m.runtime.screen = RECONNECTING
            m.poll()
            m.runtime.screen = RATE_FAULT
            m.runtime.seq += 1
            m.poll()
            self.assertEqual(m.runtime.sends, [])
            self.assertGreater(m.policy.due - time.monotonic(), 58)
            m.policy.due = 0
            m.poll()
            m.poll()
            self.assertEqual(m.runtime.sends, ["继续"])

    def test_quota_error_ends_monitoring_with_visible_manual_status(self):
        import app
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "JOURNAL", Path(tmp) / "journal.json"):
            m = self.make_monitor(tmp)
            m.runtime.screen = RATE_FAULT.replace(RATE_MESSAGE, "429 insufficient_quota")
            m.poll()
            self.assertFalse(m.enabled)
            self.assertEqual(m.runtime.sends, [])
            self.assertIn(("health", {"title": "需要处理", "tone": "error"}), list(m.events.queue))


if __name__ == "__main__":
    unittest.main()
