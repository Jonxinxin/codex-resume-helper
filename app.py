from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from ui import ResumeView, GREEN, MUTED, WHITE, LINE, TEXT
from dataclasses import asdict
import logging
from logging.handlers import RotatingFileHandler

import notify
from core import RetryPolicy, inspect_screen
from runtime import Pebrel, Binding, RuntimeErrorInfo

APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexResumeHelper"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG = APP_DIR / "settings.json"
JOURNAL = APP_DIR / "retry-state.json"
LOG_DIR = APP_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger = logging.getLogger("resume-helper")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_DIR / "helper.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger.addHandler(handler)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cli_default(config):
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "bootstrap.json"
    return (config.get("cli") or os.environ.get("PEBREL_CLI") or os.environ.get("NEBULA_CLI")
            or os.path.expandvars(read_json(bundled).get("cli", "")))


class Monitor:
    """Single worker owns all runtime operations and durable send state."""
    def __init__(self, events):
        self.events = events
        self.commands = queue.Queue()
        self.stop_requested = threading.Event()
        self.stop_requested.set()
        self.dispatch_lock = threading.Lock()
        self.enabled = False
        self.binding = None
        self.runtime = None
        self.policy = RetryPolicy()
        self.interval = 10
        self.message = "继续"
        self.webhook = ""
        self.next_poll = 0
        self.last_note = ""
        self.last_observation = None
        self.countdown_key = None
        self.journal_error = ""
        try:
            self.journal = read_json(JOURNAL)
        except (ValueError, OSError) as e:
            self.journal = {}
            self.journal_error = f"重试记录无法读取，请查看日志并手动重置：{e}"
        threading.Thread(target=self.run, daemon=True).start()

    def emit(self, kind, value):
        self.events.put((kind, value))

    def note(self, text):
        if text != self.last_note:
            logger.info(text)
            self.emit("log", text)
            self.last_note = text

    def status(self, text):
        self.emit("status", text)
        self.emit("attempts", self.policy.attempts)

    def summary(self, headline):
        binding = self.binding
        return (f"{headline}\n"
                f"任务：{binding.title or 'Codex'}\n"
                f"目录：{binding.cwd}\n"
                f"会话：{binding.session_id}\n"
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")

    def notify(self, text):
        if not self.webhook:
            return
        threading.Thread(target=self._notify, args=(self.webhook, text), daemon=True).start()

    def _notify(self, webhook, text):
        try:
            notify.send(webhook, text)
            self.note("飞书通知已发送")
        except Exception as e:
            logger.warning("飞书通知发送失败：%s", e)
            self.note(f"飞书通知发送失败：{e}")

    def persist(self):
        if self.binding:
            self.journal[self.binding.session_id] = asdict(self.policy)
            write_json(JOURNAL, self.journal)

    def restore(self):
        self.last_observation = None
        saved = self.journal.get(self.binding.session_id, {})
        values = {k: v for k, v in saved.items() if k in RetryPolicy.__dataclass_fields__}
        for k in ("candidate", "sent_key"):
            if values.get(k):
                values[k] = tuple(values[k])
        self.policy = RetryPolicy(**values)
        self.policy.candidate = None  # always apply a fresh grace period after re-opening

    def pause(self, text, alert=False):
        self.enabled = False
        self.stop_requested.set()
        self.emit("enabled", False)
        self.emit("health", {"title": "需要处理" if alert else "", "tone": "error" if alert else "normal"})
        self.status(text)
        self.note(text)

    def command(self, name, data):
        if name == "refresh":
            try:
                runtime = Pebrel(data)
                self.emit("rows", runtime.agents())
                self.note("已刷新本机 Codex 窗格")
            except Exception:
                self.emit("connection_error", None)
                raise
            finally:
                self.emit("refresh_done", None)
        elif name == "bind":
            try:
                self.pause("正在绑定")
                self.runtime = Pebrel(data[0])
                self.binding = self.runtime.bind(data[1])
                self.restore()
                self.emit("bound", self.binding)
                self.note(f"已绑定窗格 {self.binding.pane_id} · 会话 {self.binding.session_id}")
                self.status("已绑定，点击开始监测")
                self.preview()
            finally:
                self.emit("binding_done", None)
        elif name == "start":
            if not self.binding:
                raise RuntimeErrorInfo("请先选择并绑定一个 Codex 窗格")
            if self.journal_error:
                raise RuntimeErrorInfo(self.journal_error)
            self.runtime.verify(self.binding, processes=True)
            self.interval, self.policy.wait_min, self.policy.wait_max, self.policy.limit, self.message, self.webhook = data
            if self.policy.uncertain:
                raise RuntimeErrorInfo("上次发送结果未确认，请先在原窗格检查，再点击重置重试")
            self.stop_requested.clear()
            self.enabled = True
            self.next_poll = 0
            self.emit("enabled", True)
            self.emit("health", {"title": "", "tone": "normal"})
            note = f"开始监测，每 {self.interval} 秒检查；等待 {self.policy.wait_min}–{self.policy.wait_max} 秒后重试，最多连续 {self.policy.limit} 次"
            self.note(note + ("；飞书通知已开启" if self.webhook else ""))
        elif name == "webhook":
            self.webhook = data or ""
            self.note("飞书通知已开启，续接成功或连续重试达到上限时推送" if self.webhook else "飞书通知已关闭")
        elif name == "pause":
            self.pause("监测已暂停")
        elif name == "preview":
            self.preview()
        elif name == "reset":
            self.pause("重试记录已重置；点击开始监测可重新启用")
            self.policy = RetryPolicy()
            self.journal_error = ""
            self.persist()

    def preview(self):
        if self.binding:
            self.runtime.verify(self.binding)
            result = self.runtime.read(self.binding)
            self.emit("preview", result.get("text", ""))

    def poll(self):
        row = self.runtime.verify(self.binding)
        read = self.runtime.read(self.binding)
        self.emit("preview", read.get("text", ""))
        ev = inspect_screen(read.get("text", ""), read.get("task_state", row["task_state"]), read.get("exited", False))
        seq = row.get("state_change_seq", 0)
        action, note = self.policy.observe(ev, seq, time.monotonic())
        self.emit("checked", time.time())
        if action in ("hold", "exhausted"):
            title, tone = "需要处理", "error"
        elif ev.signal == "account":
            title, tone = "额度或账号异常", "error"
        elif ev.signal == "rate_limit":
            title, tone = ("限流，正在重连" if ev.kind == "busy" else "限流冷却中"), "warning"
        elif ev.signal == "overloaded":
            title, tone = ("上游繁忙，等待续接" if action == "countdown" else "上游繁忙，正在续接"), "warning"
        elif ev.signal == "reconnecting":
            title, tone = "Codex 正在重连", "warning"
        elif action in ("countdown", "send"):
            title, tone = "等待续接" if action == "countdown" else "正在续接", "warning"
        else:
            title, tone = "", "normal"
        self.emit("health", {"title": title, "tone": tone})
        observation = (ev.kind, ev.signal, ev.reason)
        previous = getattr(self, "last_observation", None)
        if observation != previous:
            if ev.signal:
                self.note(ev.reason)
            elif previous and previous[1] and action == "watch":
                self.note("限流 / 重连提示已消失，继续监测：" + ev.reason)
            self.last_observation = observation
        self.status(note)
        self.persist()
        if action == "resumed":
            self.note(note)
            self.notify(self.summary(f"✅ Codex 续接成功（第 {self.policy.completed} 次生效）"))
        elif action == "exhausted":
            self.pause(note, alert=True)
            self.notify(self.summary(f"⚠️ Codex 连续续接 {self.policy.limit} 次仍未成功，已停止自动续接"))
        elif action == "hold":
            self.pause(note, alert=True)
        elif action == "countdown":
            mark = (self.policy.attempts, self.policy.candidate)
            if self.countdown_key != mark:
                self.countdown_key = mark
                self.note(f"发现可续接的临时故障，倒计时 {max(1, int(self.policy.grace))} 秒后复核并续接")
        elif action == "send":
            # Re-read identity, process and input immediately before the one mutation.
            fresh = self.runtime.verify(self.binding, processes=True)
            latest = self.runtime.read(self.binding)
            evidence = inspect_screen(latest.get("text", ""), latest.get("task_state", ""), latest.get("exited", False))
            if evidence.kind != "retry" or evidence.fingerprint != ev.fingerprint or fresh.get("state_change_seq", 0) != seq:
                self.policy.candidate = None
                self.note("目标状态已变化，取消此次发送并继续检查")
                return
            # A pause/close arriving while the runtime was read cancels the dispatch.
            with self.dispatch_lock:
                if self.stop_requested.is_set():
                    return
                self.policy.dispatching(seq)
                self.persist()
                self.note(f"正在向已绑定会话发送续接消息（第 {self.policy.attempts} 次）")
                try:
                    self.runtime.send(self.binding, self.message)
                except Exception:
                    self.pause("发送结果未确认：已停止自动发送，请检查原窗格后重置重试", alert=True)
                    raise
                self.policy.acknowledged()
                self.persist()
                self.status("续接消息已提交，等待 Codex 状态变化")
                self.note("Pebrel 已确认提交；同一次故障不会重复发送")

    def run(self):
        while True:
            try:
                name, data = self.commands.get(timeout=0.25)
            except queue.Empty:
                name = None
            try:
                if name:
                    self.command(name, data)
                if self.commands.empty() and self.enabled and not self.stop_requested.is_set() and time.monotonic() >= self.next_poll:
                    self.next_poll = time.monotonic() + self.interval
                    self.poll()
            except Exception as e:
                if not isinstance(e, RuntimeErrorInfo):
                    logger.exception("操作失败")
                if isinstance(e, RuntimeErrorInfo) and e.code in ("runtime_unavailable", "runtime_timeout", "cli_unavailable"):
                    self.emit("connection_error", None)
                if name in ("refresh", "preview") and not self.enabled:
                    self.emit("health", {"title": "需要处理", "tone": "error"})
                    self.status(str(e))
                    self.note(str(e))
                else:
                    self.pause(str(e), alert=True)


class App(ResumeView):
    def __init__(self):
        ctk.set_appearance_mode("light")
        super().__init__()
        self.title("Codex 续接助手 · Pebrel")
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "app.ico"
        if icon.exists():
            self.iconbitmap(str(icon))
        self.geometry("1100x780+60+30")
        self.minsize(960, 720)
        self.events = queue.Queue()
        self.monitor = Monitor(self.events)
        try:
            self.config_data = read_json(CONFIG)
        except (ValueError, OSError):
            self.config_data = {}
        self.cli = cli_default(self.config_data)
        self.rows = []
        self.bound = None
        self.enabled = False
        self.has_started = False
        self.connection_ok = None
        self.health = {"title": "", "tone": "normal"}
        self.checked_var = tk.StringVar(value="尚未开始检查 · 关闭窗口即停止监测")
        self.search = tk.StringVar()
        self.status_var = tk.StringVar(value="正在连接 Pebrel…")
        self.target_var = tk.StringVar(value="尚未绑定 · 从列表选择一个 Codex 窗格")
        self.counter_var = tk.StringVar(value="已重试 0 次")
        self.message_var = tk.StringVar(value=self.config_data.get("message", "继续"))
        self.interval_var = tk.StringVar(value=str(self.config_data.get("interval", 10)))
        self.wait_min_var = tk.StringVar(value=str(self.config_data.get("wait_min", 5)))
        self.wait_max_var = tk.StringVar(value=str(self.config_data.get("wait_max", 30)))
        self.limit_var = tk.StringVar(value=str(self.config_data.get("limit", 6)))
        self.webhook_var = tk.StringVar(value=self.config_data.get("webhook", ""))
        self.notify_window = None
        self.notify_status = None
        self.build_ui()
        self.refresh_notify_hint()
        self.search.trace_add("write", lambda *_: self.render_rows())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.drain)
        self.after(200, self.refresh)
        self.after(5000, self.reconnect_if_needed)

    def reconnect_if_needed(self):
        if (not self.bound or not self.connection_ok) and not self.refreshing and not self.binding_pending:
            self.refresh()
        self.after(5000, self.reconnect_if_needed)

    def refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        self.update_controls()
        self.monitor.commands.put(("refresh", self.cli))

    def toggle_monitor(self):
        if self.enabled:
            self.pause()
        else:
            self.start()

    def open_logs(self):
        os.startfile(str(LOG_DIR))

    def refresh_preview(self):
        if self.bound:
            self.monitor.commands.put(("preview", None))

    def refresh_notify_hint(self):
        if self.config_data.get("webhook"):
            self.notify_button.configure(text="飞书通知 ●", text_color=GREEN)
        else:
            self.notify_button.configure(text="飞书通知", text_color=MUTED)

    def notify_settings(self):
        if self.notify_window is not None and self.notify_window.winfo_exists():
            self.notify_window.lift()
            return
        dialog = ctk.CTkToplevel(self, fg_color=WHITE)
        self.notify_window = dialog
        dialog.title("飞书通知")
        dialog.geometry(f"560x318+{max(0, self.winfo_rootx() + 110)}+{max(0, self.winfo_rooty() + 150)}")
        dialog.minsize(480, 300)
        dialog.transient(self)
        self.label(dialog, "飞书通知", size=17, bold=True).pack(anchor="w", padx=24, pady=(18, 6))
        self.label(dialog, "粘贴飞书群自定义机器人的 Webhook 地址。续接成功、或连续重试达到上限时各推送一条文本消息。\n留空即关闭通知；只会发送本助手的状态摘要，终端内容不会上传。",
                   size=12, color=MUTED, justify="left", height=48, wraplength=500).pack(anchor="w", padx=24)
        entry = ctk.CTkEntry(dialog, textvariable=self.webhook_var, height=36, fg_color="#FAFBFA",
                             border_color=LINE, corner_radius=8, font=self.font(12), text_color=TEXT)
        entry.pack(fill="x", padx=24, pady=(4, 8))
        self.notify_status = self.label(dialog, "", size=12, color=MUTED, height=22)
        self.notify_status.pack(anchor="w", padx=24)
        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(6, 16))
        self.button(row, "测试发送", self.test_notify, width=96).pack(side="left")
        self.button(row, "保存", self.save_notify, primary=True, width=96).pack(side="right")
        self.button(row, "关闭", dialog.destroy, width=76).pack(side="right", padx=(0, 8))

    def test_notify(self):
        try:
            webhook = notify.validate_webhook(self.webhook_var.get())
        except ValueError as e:
            messagebox.showerror("通知设置无效", str(e), parent=self.notify_window or self)
            return
        if not webhook:
            self.notify_status.configure(text="请先填写 Webhook 地址。", text_color="#A47A34")
            return
        self.notify_status.configure(text="正在发送测试通知…", text_color=MUTED)
        threading.Thread(target=self._test_notify, args=(webhook,), daemon=True).start()

    def _test_notify(self, webhook):
        try:
            notify.send(webhook, f"✅ Codex 续接助手：飞书通知测试成功\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
            self.events.put(("notify_result", ("测试通知已发送，请查看飞书群。", "normal")))
        except Exception as e:
            self.events.put(("notify_result", (f"测试通知发送失败：{e}", "error")))

    def save_notify(self):
        try:
            webhook = notify.validate_webhook(self.webhook_var.get())
            self.config_data["webhook"] = webhook
            write_json(CONFIG, self.config_data)
        except (ValueError, OSError) as e:
            messagebox.showerror("通知设置无效", str(e), parent=self.notify_window or self)
            return
        self.webhook_var.set(webhook)
        self.monitor.commands.put(("webhook", webhook))
        self.refresh_notify_hint()
        self.notify_status.configure(text="已保存。" + ("续接成功或放弃时会推送。" if webhook else "通知已关闭。"),
                                     text_color=GREEN if webhook else MUTED)

    def choose_cli(self):
        name = filedialog.askopenfilename(parent=self, title="选择 Pebrel 的 pebrel.exe", filetypes=[("Pebrel", "pebrel.exe"), ("可执行文件", "*.exe")])
        if name:
            self.pause()
            self.cli = name
            self.config_data["cli"] = name
            write_json(CONFIG, self.config_data)
            self.refresh()

    def bind_selected(self):
        if self.enabled or self.binding_pending:
            return
        selected = self.selected_row()
        if not selected:
            return
        self.binding_pending = True
        self.update_controls()
        self.pause()
        self.monitor.commands.put(("bind", (self.cli, selected)))

    def start(self):
        if self.enabled or self.binding_pending or not self.bound:
            return
        try:
            vals = tuple(int(v.get()) for v in (self.interval_var, self.wait_min_var, self.wait_max_var, self.limit_var))
            interval, wait_min, wait_max, limit = vals
            if not (3 <= interval <= 120 and 1 <= wait_min <= 300 and 5 <= wait_max <= 600
                    and wait_min <= wait_max and 1 <= limit <= 100):
                raise ValueError("检查间隔 3–120 秒；等待区间 1–600 秒且最短不大于最长（最长至少 5 秒）；重试上限 1–100 次")
            msg = self.message_var.get().strip()
            if not msg or len(msg) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in msg):
                raise ValueError("请输入 1–500 字的单行续接消息")
            webhook = notify.validate_webhook(self.webhook_var.get())
            self.config_data.update(cli=self.cli, interval=interval, wait_min=wait_min, wait_max=wait_max,
                                    limit=limit, message=msg, webhook=webhook)
            write_json(CONFIG, self.config_data)
            self.monitor.commands.put(("start", (*vals, msg, webhook)))
        except (ValueError, OSError) as e:
            messagebox.showerror("设置无效", str(e), parent=self)

    def pause(self):
        self.monitor.stop_requested.set()
        self.monitor.commands.put(("pause", None))

    def reset(self):
        if not self.bound:
            messagebox.showinfo("尚未绑定", "请先绑定需要重置的会话。", parent=self)
            return
        if messagebox.askyesno("重置当前会话", "请先确认原窗格的消息发送结果。\n\n重置后将允许对同一故障再次发送续接消息。是否重置？", parent=self):
            self.pause()
            self.monitor.commands.put(("reset", None))

    def drain(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "rows":
                self.rows = value
                self.render_rows()
                self.connection_badge.configure(text="●  Pebrel 已连接", text_color="#237A60")
                self.connection_ok = True
                if not self.bound:
                    self.health = {"title": "", "tone": "normal"}
                self.update_controls()
                if not self.bound:
                    self.status_var.set("选择下方任务，绑定后即可开始监测。" if value else "暂无任务，在 Pebrel 中启动 Codex 后刷新。")
            elif kind == "refresh_done":
                self.refreshing = False
                self.update_controls()
            elif kind == "binding_done":
                self.binding_pending = False
                self.update_controls()
            elif kind == "connection_error":
                self.connection_ok = False
                self.connection_badge.configure(text="●  Pebrel 未连接", text_color="#A47A34")
                self.update_controls()
            elif kind == "checked":
                self.connection_ok = True
                self.connection_badge.configure(text="●  Pebrel 已连接", text_color="#237A60")
                self.checked_var.set(f"最近检查 {time.strftime('%H:%M:%S', time.localtime(value))} · 关闭窗口即停止监测")
            elif kind == "health":
                self.health = value
                self.update_controls()
            elif kind == "status":
                self.status_var.set(value if len(value) <= 110 else value[:107] + "…")
            elif kind == "attempts":
                self.attempts = value
                self.update_controls()
            elif kind == "bound":
                self.bound = value
                self.checked_var.set("已绑定，等待开始检查 · 关闭窗口即停止监测")
                self.update_target()
                self.update_controls()
            elif kind == "enabled":
                self.enabled = value
                if value:
                    self.has_started = True
                self.update_controls()
            elif kind == "log":
                self.append_log(value)
            elif kind == "preview":
                self.previewbox.configure(state="normal")
                self.previewbox.delete("1.0", "end")
                self.previewbox.insert("end", value)
                self.previewbox.see("end")
                self.previewbox.configure(state="disabled")
            elif kind == "notify_result":
                text, tone = value
                if self.notify_status is not None and self.notify_status.winfo_exists():
                    self.notify_status.configure(text=text, text_color=GREEN if tone == "normal" else "#B35842")
                else:
                    (messagebox.showinfo if tone == "normal" else messagebox.showerror)("飞书通知", text, parent=self)
        self.after(100, self.drain)

    def close(self):
        self.monitor.stop_requested.set()
        self.destroy()


def main():
    if os.name == "nt":
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, "Local\\CodexResumeHelper-Pebrel-v1")
        if ctypes.get_last_error() == 183:
            ctypes.windll.user32.MessageBoxW(None, "续接助手已经运行，请查看任务栏中的窗口。", "Codex 续接助手", 0x40)
            return
    App().mainloop()


if __name__ == "__main__":
    main()
