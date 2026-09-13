"""RSS/Atom：免费网络能力，只取标题，用来做"今日要闻"这类简报。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from tools.core.result import ToolResult
from tools.world import http

TAG_RE = re.compile(r"<[^>]+>")


def fetch(feed_url: str, *, limit: int = 6) -> ToolResult:
    if not feed_url:
        return ToolResult.failure("rss", "没有给订阅地址")
    raw = http.get(feed_url, timeout=15.0)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return ToolResult.failure("rss", f"订阅解析失败：{exc}")
    items = root.iter()
    titles: list[str] = []
    for node in items:
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        for child in node:
            if child.tag.split("}")[-1] in ("title", "名称"):
                text = TAG_RE.sub("", child.text or "").strip()
                if text:
                    titles.append(text)
                break
        if len(titles) >= limit:
            break
    if not titles:
        return ToolResult.failure("rss", "这个源里没读到条目")
    return ToolResult(
        name="rss", ok=True, text="\n".join(f"· {title}" for title in titles),
        data={"feed": feed_url, "titles": titles}, http_requests=1, source_type="rss",
    )
