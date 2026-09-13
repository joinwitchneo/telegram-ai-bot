"""网页阅读：HTTP + HTML 解析，完全本地后处理，0 AI API。

用标准库 HTMLParser 做"readability-lite"：去掉脚本/样式/导航，
取正文最长的一段，再截断给主 LLM。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from tools.core.result import ToolResult
from tools.world import http

SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header", "form", "svg", "iframe"}
BLOCK_TAGS = {"p", "div", "article", "section", "li", "br", "h1", "h2", "h3", "h4", "td"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks: list[str] = []
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
            return
        self._buffer.append(text)

    def _flush(self) -> None:
        text = " ".join(self._buffer).strip()
        if text:
            self.blocks.append(text)
        self._buffer = []

    def result(self) -> str:
        self._flush()
        if not self.blocks:
            return ""
        # readability-lite：正文通常是文本最长的那一段
        joined = "\n".join(block for block in self.blocks if len(block) >= 12)
        if len(joined) < 80:
            joined = "\n".join(self.blocks)
        return joined


def extract_html(html: str, *, limit: int = 4000) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - 坏 HTML 不该炸
        pass
    body = re.sub(r"\n{3,}", "\n\n", parser.result()).strip()
    return parser.title.strip(), body[:limit]


def read_url(url: str, *, limit: int = 4000) -> ToolResult:
    target = (url or "").strip()
    if not target.startswith(("http://", "https://")):
        return ToolResult.failure("web_reader", "这不是一个网页链接")
    raw = http.get(target, timeout=15.0)
    html = raw.decode("utf-8", errors="replace")
    title, body = extract_html(html, limit=limit)
    if not body:
        return ToolResult.failure("web_reader", "这个页面没读到正文（可能是纯 JS 页面）")
    text = f"《{title}》\n{body}" if title else body
    return ToolResult(
        name="web_reader",
        ok=True,
        text=text,
        data={"url": target, "title": title, "length": len(body)},
        http_requests=1,
        source_type="url",
        confidence=0.8,
    )
