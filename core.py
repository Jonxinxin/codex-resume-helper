"""Pebrel Codex recovery policy. No terminal text is ever executed."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
TRANSIENT = re.compile(
    r"stream.*(?:disconnect|interrupt|error|fail)|error.*stream|network|connection.*(?:reset|closed|lost|abort|error)|"
    r"timed?\s*out|timeout|error sending request|transport error|server error|internal error|"
    r"service unavailable|bad gateway|gateway timeout|unexpected status.*(?:500|502|503|504)|"
    r"exceeded retry|retry limit|retries exhausted|temporarily unavailable|网络|连接.*(?:断|失败)|超时|服务.*暂时", re.I)
MANUAL = re.compile(
    r"usage limit|quota|insufficient|\b401\b|\b403\b|unauthori|forbidden|log.?in|sign.?in|"
    r"authentication|billing|context.*(?:full|limit|exceed)|maximum context|额度|限额|登录|余额|上下文.*(?:满|限制)", re.I)
BUSY = re.compile(r"esc to interrupt|press esc to interrupt|ctrl\+c to interrupt|reconnecting|retrying|正在重连|正在重试|按.*中断", re.I)
RATE_LIMIT = re.compile(r"rate[\s_-]+limit|\b429\b|too many requests|请求频率|速率限制|限流", re.I)
RECONNECT = re.compile(
    r"^\s*(?:[•●\u2800-\u28ff]\s*)?(?:Reconnecting|Retrying)[.\s…]*"
    r"(?P<attempt>\d+\s*/\s*\d+)\b.*(?:esc to interrupt|ctrl\+c to interrupt)", re.I)
RETRY_AFTER = re.compile(
    r"(?:retry[- ]after\s*[:=]?\s*|try again in\s+)(\d+(?:\.\d+)?)"
    r"\s*(milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|m)?\b", re.I)
APPROVAL = re.compile(r"would you like to|do you want to (?:run|allow|approve)|approve this|requires? approval|press enter to confirm|select an option|是否允许|需要.*批准|确认执行|允许本次", re.I)
PLACEHOLDERS = re.compile(
    r'^(?:Ask Codex to do anything|Ask anything|Type a message|Find and fix a bug in @filename|'
    r'Implement \{feature\}|Improve documentation in @filename|Write tests for @filename|'
    r'Explain this codebase|Summarize recent commits|Run /review on my current changes|'
    r'Use /skills to list available skills|请.*Codex.*|输入消息.*)$', re.I)


@dataclass(frozen=True)
class Evidence:
    kind: str  # retry / manual / busy / normal
    reason: str
    fingerprint: str = ""
    signal: str = ""
    retry_after: float = 0


def active_reconnection(lines: list[str]) -> Evidence | None:
    """Only a live Codex status row is advisory evidence; prose is not a fault."""
    for idx in range(len(lines) - 1, -1, -1):
        match = RECONNECT.search(lines[idx])
        if not match:
            continue
        tail = lines[idx + 1:]
        # A subsequent answer, final error, or submitted user turn makes this old.
        if any(re.match(r"^\s*[■•]\s+", line) for line in tail):
            return None
        prompts = [re.sub(r"^\s*›\s?", "", line).strip() for line in tail if re.match(r"^\s*›(?:\s|$)", line)]
        if len(prompts) > 1 or any(p and not PLACEHOLDERS.fullmatch(p) for p in prompts):
            return None
        block = "\n".join(tail).split("›", 1)[0]
        attempt = re.sub(r"\s", "", match.group("attempt"))
        if MANUAL.search(block):
            return Evidence("busy", f"Codex 正在自行重连（{attempt}），但返回了额度或账号错误，请检查原窗格。", signal="account")
        if RATE_LIMIT.search(block):
            return Evidence("busy", f"检测到临时限流，Codex 正在自行重连（{attempt}）；助手等待重连结束后再判断续接。", signal="rate_limit")
        return Evidence("busy", f"Codex 正在自行重连（{attempt}）；助手继续检查，暂不额外发送消息。", signal="reconnecting")
    return None


def retry_after_seconds(body: str) -> float:
    match = RETRY_AFTER.search(body)
    if not match:
        return 0
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    if unit.startswith("millisecond") or unit == "ms":
        return value / 1000
    return value * 60 if unit in ("m", "min", "mins", "minute", "minutes") else value


def inspect_screen(text: str, state: str, exited: bool = False) -> Evidence:
    if exited:
        return Evidence("manual", "Codex 已退出，请重新启动并绑定会话")
    lines = ANSI.sub("", text).replace("\r", "").splitlines()
    reconnect = active_reconnection(lines)
    if reconnect:
        return reconnect
    # Codex renders actual terminal faults with ■; prose and tool output are not faults.
    errors = [i for i, line in enumerate(lines) if re.match(r"^\s*■\s+", line)]
    if not errors:
        if state in ("failed", "attention"):
            return Evidence("manual", "窗格需要处理，未发现可自动续接的临时故障")
        return Evidence("busy" if state == "running" else "normal", "运行中" if state == "running" else "正常等待 / 已完成")
    idx = errors[-1]
    if BUSY.search("\n".join(lines[idx + 1:])):
        return Evidence("busy", "Codex 正在运行或自行重试")
    prompts = [i for i in range(idx + 1, len(lines)) if re.match(r"^\s*›(?:\s|$)", lines[i])]
    if not prompts:
        return Evidence("manual", "发现故障，但未确认 Codex 输入框已就绪")
    p = prompts[0]
    body = "\n".join(lines[idx:p]).strip()
    # A later user turn or agent answer invalidates an old error in scrollback.
    if len(prompts) > 1 or re.search(r"^\s*•\s+", "\n".join(lines[idx + 1:p]), re.M):
        return Evidence("normal", "历史故障已被后续对话覆盖")
    prompt = re.sub(r"^\s*›\s?", "", lines[p]).strip()
    if prompt and not PLACEHOLDERS.fullmatch(prompt):
        return Evidence("manual", "输入框中有内容，等待你手动处理")
    # An empty first row may be followed by a multiline draft. Only known Codex
    # footer rows are accepted after the input marker.
    for line in lines[p + 1:]:
        clean = line.strip()
        if clean and not re.match(r"^(?:GPT-|gpt-|o[1-9]|\? for shortcuts|\d+% context|context left|\d+ tokens|shift\+enter|tab to queue|Tip:|提示[：:]|/\S+)", clean, re.I):
            return Evidence("manual", "输入框或底部区域包含未识别内容，等待手动处理")
    if APPROVAL.search(body) or MANUAL.search(body):
        return Evidence("manual", "故障需要人工处理：" + body[:200])
    if APPROVAL.search("\n".join(lines[p + 1:])):
        return Evidence("manual", "正在等待确认")
    fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]
    if RATE_LIMIT.search(body):
        return Evidence("retry", "Codex 自行重连已结束，仍提示临时限流；输入框已就绪，等待冷却后续接。",
                        fingerprint, "rate_limit", retry_after_seconds(body))
    if not TRANSIENT.search(body):
        return Evidence("manual", "未识别的故障，保留给你处理：" + body[:200])
    return Evidence("retry", body[:240], fingerprint)


@dataclass
class RetryPolicy:
    limit: int = 6
    delay: int = 30
    attempts: int = 0
    candidate: tuple | None = None
    due: float = 0
    sent_key: tuple | None = None
    uncertain: bool = False
    awaiting: bool = False
    confirmed_active: bool = False
    dispatch_seq: int = -1

    def observe(self, ev: Evidence, seq: int, now: float) -> tuple[str, str]:
        if self.uncertain:
            return "hold", "发送结果未确认；请在原窗格手动回复，再点击重新绑定"
        if self.awaiting:
            if seq > self.dispatch_seq and ev.kind == "busy":
                self.confirmed_active = True
            if seq > self.dispatch_seq and ev.kind == "normal":
                self.awaiting = False
                self.attempts = 0
                self.sent_key = None
                self.candidate = None
                return "watch", "已恢复并完成本轮，继续监测"
            if ev.kind == "retry" and (seq > self.dispatch_seq or self.confirmed_active):
                self.awaiting = False
            elif ev.kind == "manual":
                return "hold", ev.reason
            else:
                return "watch", ev.reason if ev.signal else "已发送，等待 Codex 新的状态变化；不重复发送"
        if ev.kind != "retry":
            self.candidate = None
            return ("hold" if ev.kind == "manual" else "watch"), ev.reason
        key = (seq, ev.fingerprint)
        if key == self.sent_key:
            return "watch", "同一次故障已处理，等待新的状态变化"
        if self.attempts >= self.limit:
            return "hold", f"已达到连续重试上限 {self.limit} 次"
        if key != self.candidate:
            self.candidate = key
            base = max(self.delay, 60) if ev.signal == "rate_limit" else self.delay
            self.due = now + max(ev.retry_after, min(300, base * (2 ** self.attempts)))
        if now < self.due:
            label = "临时限流冷却中" if ev.signal == "rate_limit" else "故障已确认"
            return "countdown", f"{label}，{max(1, int(self.due-now))} 秒后续接"
        return "send", "再次核验原会话后发送"

    def dispatching(self, seq: int):
        # Persist this BEFORE crossing the process boundary. A crash/timeout is ambiguous.
        self.attempts += 1
        self.sent_key = self.candidate
        self.dispatch_seq = seq
        self.uncertain = True
        self.awaiting = True
        self.confirmed_active = False

    def acknowledged(self):
        self.uncertain = False
