from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, asdict
import json
from connection import runtime_environment


class RuntimeErrorInfo(Exception):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Binding:
    window_id: int
    pane_id: int
    session_id: str
    pid: int
    cwd: str
    title: str
    agent_id: str = ""
    generation: int | None = None

    def to_dict(self):
        return asdict(self)


class Pebrel:
    def __init__(self, cli: str):
        self.cli = cli
        # Keep a bound client's endpoint fixed across identity checks and sends.
        # New refresh/bind clients pick up a newer explicit registration.
        self.environment = runtime_environment(cli)

    def call(self, *args):
        if not self.cli or not os.path.isfile(self.cli):
            raise RuntimeErrorInfo("找不到 Pebrel CLI，请通过“连接设置”指定 pebrel.exe", "cli_unavailable")
        try:
            p = subprocess.run([self.cli, *map(str, args)], capture_output=True, encoding="utf-8",
                               errors="replace", timeout=15, env=self.environment,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as e:
            raise RuntimeErrorInfo("Pebrel 响应超时", "runtime_timeout") from e
        except OSError as e:
            raise RuntimeErrorInfo(str(e), "runtime_unavailable") from e
        try:
            data = json.loads(p.stdout)
        except ValueError as e:
            raise RuntimeErrorInfo("Pebrel 未返回有效 JSON：" + (p.stderr or p.stdout)[:250], "runtime_unavailable") from e
        if not data.get("ok"):
            error = data.get("error", {})
            code = error.get('code', 'runtime_unavailable')
            if code == "runtime_unavailable":
                raise RuntimeErrorInfo("未连接到 Pebrel。请在 Pebrel 新开一个 PowerShell 窗格，助手会自动重试连接。", code)
            raise RuntimeErrorInfo(f"{code}: {error.get('message', '')}", code)
        return data["result"]

    def agents(self):
        return [a for a in self.call("agent", "list").get("agents", [])
                if a.get("agent", {}).get("kind") == "codex"]

    def processes(self, window, pane):
        return self.call("ctl", "procs", "--window", window, "--pane", pane).get("processes", [])

    def bind(self, row):
        a = row.get("agent", {})
        if row.get("ssh_destination"):
            raise RuntimeErrorInfo("此版本只支持本机 Pebrel 窗格")
        if not a.get("session_id"):
            raise RuntimeErrorInfo("尚未获得 Codex 会话 ID，请在 Codex 完成初始化后刷新")
        procs = [p for p in self.processes(row["window_id"], row["pane_id"])
                 if p.get("agent_kind") == "codex"]
        if len(procs) != 1:
            raise RuntimeErrorInfo("无法唯一确认此窗格中的 Codex 进程")
        return Binding(row["window_id"], row["pane_id"], a["session_id"], procs[0]["pid"],
                       row.get("cwd", ""), row.get("title", ""),
                       a.get("agent_id", row.get("agent_id", "")),
                       a.get("generation", row.get("generation")))

    def verify(self, binding, processes=False):
        rows = [a for a in self.agents() if a["window_id"] == binding.window_id and a["pane_id"] == binding.pane_id]
        if len(rows) != 1 or rows[0].get("agent", {}).get("session_id") != binding.session_id:
            raise RuntimeErrorInfo("原 Codex 会话已退出或被替换，请重新绑定")
        row = rows[0]
        if row.get("ssh_destination") or row.get("cwd", "") != binding.cwd:
            raise RuntimeErrorInfo("目标工作目录或连接发生变化，请重新绑定")
        if processes:
            procs = self.processes(binding.window_id, binding.pane_id)
            if not any(p.get("pid") == binding.pid and p.get("agent_kind") == "codex" for p in procs):
                raise RuntimeErrorInfo("原 Codex 进程已退出，请重新绑定")
        return row

    def read(self, binding):
        return self.call("ctl", "read", "--window", binding.window_id, "--pane", binding.pane_id, "--lines", 100)

    def send(self, binding, text):
        if not text.strip() or len(text) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise RuntimeErrorInfo("续接消息必须是 1–500 字的单行纯文本")
        if binding.agent_id and binding.generation is not None:
            return self.call("ctl", "agent-prompt", "--agent", binding.agent_id,
                             "--generation", binding.generation, "--text", text, "--timeout-ms", 10000)
        return self.call("ctl", "prompt", "--window", binding.window_id,
                         "--pane", binding.pane_id, "--text", text, "--timeout-ms", 10000)
