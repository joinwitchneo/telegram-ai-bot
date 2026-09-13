"""联网搜索：Tavily（可选功能）。"""

from __future__ import annotations

import json
import urllib.request


def _http_post_json(url: str, payload: dict, timeout: int = 25) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", "User-Agent": "xiyan-v2/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def web_search(query: str, api_key: str, api_url: str = "https://api.tavily.com/search") -> str:
    if not api_key:
        raise RuntimeError("SEARCH_API_KEY 未配置")
    result = _http_post_json(
        api_url,
        {"api_key": api_key, "query": query, "max_results": 5, "search_depth": "basic"},
    )
    chunks = []
    for item in result.get("results", []):
        title = item.get("title", "")
        content = item.get("content", "")
        if title:
            chunks.append(f"{title}：{content[:300]}")
    return "\n".join(chunks)
