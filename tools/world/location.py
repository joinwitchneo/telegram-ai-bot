"""地点：免费地理编码（Open-Meteo geocoding），带缓存，不做复杂地图。"""

from __future__ import annotations

import re

from tools.core.result import ToolResult
from tools.world import http

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# 免费地理编码是"城市级"的，机构名基本查不到；先按原名查，再退化成地名候选
INSTITUTION_SUFFIXES = ("大学", "学院", "公司", "集团", "酒店", "机场", "车站", "医院", "工厂", "广场", "校区")


def _candidates(query: str) -> list[str]:
    """把"湖南工业大学"这类机构名拆成可能是地名的候选：全名 → 去后缀 → 最短地名前缀。"""
    items = [query]
    stripped = query
    for suffix in INSTITUTION_SUFFIXES:
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)]
            items.append(stripped)
            break
    cjk = re.findall(r"[\u4e00-\u9fff]+", query)
    for run in cjk:
        if len(run) > 2:
            items.append(run[:2])      # 城市级地名通常就是两个字，先试最短的
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen[:3]     # 最多试 3 个候选，别把免费接口当搜索用


def _query(place: str) -> list[dict]:
    data = http.get_json(f"{GEOCODE_URL}?name={http.quote(place)}&count=3&language=zh&format=json")
    return list(data.get("results") or [])


def lookup(name: str) -> ToolResult:
    query = (name or "").strip()
    if not query:
        return ToolResult.failure("location", "没给地名")
    results: list[dict] = []
    calls = 0
    matched = query
    for candidate in _candidates(query):
        results = _query(candidate)
        calls += 1
        if results:
            matched = candidate
            break
    if not results:
        return ToolResult(
            name="location", ok=False, http_requests=calls,
            error=f"免费地名库里没有「{query}」（它只收录城市级地名，学校/公司这类查不到）",
        )
    lines = []
    for item in results[:3]:
        parts = [str(item.get("name", ""))]
        if item.get("admin1"):
            parts.append(str(item["admin1"]))
        if item.get("country"):
            parts.append(str(item["country"]))
        lines.append("· " + " / ".join(part for part in parts if part)
                     + f"（{item.get('latitude')}, {item.get('longitude')}）")
    return ToolResult(
        name="location",
        ok=True,
        text=(f"「{query}」没有直接收录，最接近的地名是：\n" if matched != query else f"「{query}」可能是指：\n")
        + "\n".join(lines),
        data={"results": results, "query": query, "matched": matched},
        http_requests=calls,
        source_type="location",
    )
