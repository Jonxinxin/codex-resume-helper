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
from ui import ResumeView
from dataclasses import asdict
import logging
from logging.handlers import RotatingFileHandler

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
            self.interval, self.policy.delay, self.policy.limit, self.message = data
            if self.policy.uncertain:
                raise RuntimeErrorInfo("上次发送结果未确认，请先在原窗格检查，再点击重置重试")
            self.stop_requested.clear()
            self.enabled = True
            self.next_poll = 0
            self.emit("enabled", True)
            self.emit("health", {"title": "", "tone": "normal"})
            self.note(f"开始监测，每 {self.interval} 秒检查；最多连续重试 {self.policy.limit} 次")
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
        if action == "hold":
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
        if action == "hold":
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
        self.delay_var = tk.StringVar(value=str(self.config_data.get("delay", 30)))
        self.limit_var = tk.StringVar(value=str(self.config_data.get("limit", 6)))
        self.build_ui()
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
            vals = tuple(int(v.get()) for v in (self.interval_var, self.delay_var, self.limit_var))
            if not (3 <= vals[0] <= 120 and 10 <= vals[1] <= 300 and 1 <= vals[2] <= 20):
                raise ValueError("检查间隔 3–120 秒，首次等待 10–300 秒，重试上限 1–20 次")
            msg = self.message_var.get().strip()
            if not msg or len(msg) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in msg):
                raise ValueError("请输入 1–500 字的单行续接消息")
            self.config_data.update(cli=self.cli, interval=vals[0], delay=vals[1], limit=vals[2], message=msg)
            write_json(CONFIG, self.config_data)
            self.monitor.commands.put(("start", (*vals, msg)))
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
