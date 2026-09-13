"""统一 HTTP 小工具：带上 UA、超时、返回字节；不做高频抓取。"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "xiyan-bot/2.0 (+personal use; 1 request per query)"


def get(url: str, *, timeout: float = 12.0, headers: dict | None = None, data: bytes | None = None) -> bytes:
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def post_bytes(
    url: str, data: bytes, *, timeout: float = 60.0, headers: dict | None = None
) -> bytes:
    """POST 原始字节（本地模型服务用，超时放宽）。"""
    request = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def post_json(url: str, payload: dict, *, timeout: float = 20.0, headers: dict | None = None) -> dict:
    import json

    raw = get(
        url,
        timeout=timeout,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    return json.loads(raw.decode("utf-8", errors="replace") or "{}")


def get_json(url: str, *, timeout: float = 12.0, headers: dict | None = None) -> dict:
    import json

    raw = get(url, timeout=timeout, headers=headers)
    return json.loads(raw.decode("utf-8", errors="replace"))


def quote(text: str) -> str:
    return urllib.parse.quote(str(text))
