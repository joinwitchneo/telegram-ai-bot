"""Utility tools: weather and web search."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    "https://www.36kr.com/feeds/feed",
]


def _http_get(url: str, timeout: int = 15, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "telegram-ai-bot/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_post_json(url: str, payload: dict, timeout: int = 20, headers: dict[str, str] | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def get_weather(city: str) -> str:
    """Return a one-line weather summary from wttr.in (no API key needed)."""
    if not city:
        return ""
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%l:+%c+%t+%h"
    try:
        text = _http_get(url, timeout=12).decode("utf-8", errors="replace").strip()
        return text if text else ""
    except Exception as exc:  # noqa: BLE001
        logging.warning("weather fetch failed: %s", exc)
        return ""


def get_news_headlines(limit: int = 3) -> list[str]:
    """Try a few RSS feeds, return today's top headlines (empty on failure)."""
    for feed in NEWS_FEEDS:
        try:
            raw = _http_get(feed, timeout=10)
            root = ET.fromstring(raw)
            titles = [item.findtext("title", "").strip() for item in root.iter("item")]
            titles = [t for t in titles if t]
            if titles:
                return titles[:limit]
        except Exception as exc:  # noqa: BLE001
            logging.warning("news feed %s failed: %s", feed, exc)
    return []


def web_search(query: str, api_key: str, api_url: str = "https://api.tavily.com/search") -> str:
    """Search the web via Tavily; returns concatenated result snippets."""
    if not api_key:
        raise RuntimeError("SEARCH_API_KEY 未配置")
    result = _http_post_json(
        api_url,
        {"api_key": api_key, "query": query, "max_results": 5, "search_depth": "basic"},
        timeout=25,
    )
    chunks = []
    for item in result.get("results", []):
        title = item.get("title", "")
        content = item.get("content", "")
        if title:
            chunks.append(f"{title}：{content[:300]}")
    return "\n".join(chunks) if chunks else ""
