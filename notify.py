"""Best-effort Feishu (Lark) custom-bot notifications.

Only the helper's own short summary is ever sent; terminal text is never uploaded.
A missing webhook disables notifications, and a failed send never breaks monitoring.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

TIMEOUT = 8
HOSTS = ("feishu.cn", "larksuite.com")


def validate_webhook(url: str) -> str:
    """Return a cleaned webhook URL ('' when unset); raise ValueError when malformed."""
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    if parsed.scheme != "https" or not host:
        raise ValueError("飞书 Webhook 需要以 https:// 开头的完整地址")
    if not any(host == name or host.endswith("." + name) for name in HOSTS):
        raise ValueError("请填写飞书自定义机器人的 Webhook 地址（open.feishu.cn 或 open.larksuite.com）")
    return url


def send(webhook: str, text: str) -> None:
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(webhook, data=payload, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = response.read().decode("utf-8", "replace")
    try:
        result = json.loads(body)
    except ValueError:
        return
    if not isinstance(result, dict):
        return
    code = result.get("code", result.get("StatusCode"))
    if code not in (0, None):
        detail = result.get("msg") or result.get("StatusMessage") or body[:120]
        raise RuntimeError(f"{code}: {detail}")
