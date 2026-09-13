"""天气：wttr.in，无需 API Key。"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    "https://www.36kr.com/feeds/feed",
]


def _http_get(url: str, timeout: int = 15, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "xiyan-v2/1.0", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def get_weather(city: str) -> str:
    """返回一行天气摘要；失败返回空字符串。"""
    if not city:
        return ""
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%l:+%c+%t+%h"
    try:
        return _http_get(url, timeout=12).decode("utf-8", errors="replace").strip()
    except Exception as exc:  # noqa: BLE001
        logging.warning("weather fetch failed: %s", exc)
        return ""


def get_news_headlines(limit: int = 3) -> list[str]:
    for feed in NEWS_FEEDS:
        try:
            root = ET.fromstring(_http_get(feed, timeout=10))
            titles = [item.findtext("title", "").strip() for item in root.iter("item")]
            titles = [t for t in titles if t]
            if titles:
                return titles[:limit]
        except Exception as exc:  # noqa: BLE001
            logging.warning("news feed %s failed: %s", feed, exc)
    return []
