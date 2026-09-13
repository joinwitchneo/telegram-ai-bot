"""搜索：Level 3 通用搜索，靠网页 HTML，不买 AI 搜索 API。

每次查询只打 1~2 次 HTTP（按查询缓存 10 分钟），不做高频抓取。

实测（2026-09，本机）：
    Bing 返回可解析的 HTML 结果；
    DuckDuckGo / lite.ddg / Mojeek 都返回反爬验证页；
    Ecosia 直接 403。
所以默认用 Bing，DDG 只作为"能解析就用"的兜底，不再当作主力。
"""

from __future__ import annotations

import base64
import binascii
import html as html_lib
import re
from urllib.parse import parse_qs, unquote, urlparse

from tools.core.result import ToolResult
from tools.world import http

DDG_HTML = "https://html.duckduckgo.com/html/?q={query}"
BING_SEARCH = "https://www.bing.com/search?q={query}&setlang=zh-CN&mkt=zh-CN"

# 明显是垃圾/推广/成人内容的结果直接丢掉——不能拿这种东西喂角色
SPAM_WORDS = (
    "porn", "sex", "xxx", "nude", "creampie", "escort", "viagra", "casino",
    "betting", "slot", "adult", "amateur", "hardcore", "mydesi",
)

# 有问题时优先信这些站点
TRUSTED_DOMAINS = (
    "wikipedia.org", "github.com", "docs.python.org", "python.org", "stackoverflow.com",
    "zhihu.com", "bilibili.com", "csdn.net", "juejin.cn", "cnblogs.com", "medium.com",
    "developer.mozilla.org", "docs.microsoft.com", "learn.microsoft.com", "openai.com",
    "deepseek.com", "sspai.com", "36kr.com", "ithome.com", "gov.cn", "edu.cn",
)

RESULT_BLOCK_RE = re.compile(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
BING_BLOCK_RE = re.compile(r'<h2><a href="([^"]+)"[^>]*>(.*?)</a></h2>', re.S)
BING_ITEM_SPLIT = '<li class="b_algo"'
BING_TITLE_RE = re.compile(r"<h2[^>]*>\s*<a[^>]+href=\"(https?://[^\"]+)\"[^>]*>(.*?)</a>", re.S)
BING_ANY_LINK_RE = re.compile(r"<a[^>]+href=\"(https?://[^\"]+)\"[^>]*>(.*?)</a>", re.S)
BING_SNIPPET_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    cleaned = html_lib.unescape(TAG_RE.sub("", text or ""))
    cleaned = cleaned.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _clean_url(url: str) -> str:
    if url.startswith("//duckduckgo.com/l/?uddg=") or "uddg=" in url:
        query = parse_qs(urlparse(url).query).get("uddg")
        if query:
            return unquote(query[0])
    return url


def _unwrap_bing(url: str) -> str:
    """Bing 会把结果包成 bing.com/ck/a?...&u=a1<base64url>，这里还原真实地址。"""
    if "bing.com/ck/a" not in url:
        return url
    payload = (parse_qs(urlparse(url).query).get("u") or [""])[0]
    if payload.startswith("a1"):
        payload = payload[2:]
    if not payload:
        return url
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode(
            "utf-8", errors="replace"
        )
    except (binascii.Error, ValueError):
        return url
    return decoded if decoded.startswith("http") else url


def parse_duckduckgo(html: str, *, limit: int = 5) -> list[dict]:
    titles = RESULT_BLOCK_RE.findall(html)
    snippets = [_clean(item) for item in SNIPPET_RE.findall(html)]
    results = []
    for index, (url, title) in enumerate(titles[:limit]):
        results.append({
            "title": _clean(title),
            "url": _clean_url(html_lib.unescape(url)),
            "snippet": snippets[index] if index < len(snippets) else "",
        })
    return [item for item in results if item["title"] and item["url"]]


def parse_bing(html: str, *, limit: int = 5) -> list[dict]:
    """按 b_algo 分块解析，属性顺序变了也不会漏。"""
    blocks = html.split(BING_ITEM_SPLIT)[1:]
    if not blocks:
        # 兜底：老式 <h2><a href="...">
        return [
            {"title": _clean(title), "url": html_lib.unescape(url), "snippet": ""}
            for url, title in BING_BLOCK_RE.findall(html)[:limit]
        ]
    results: list[dict] = []
    for block in blocks[: limit * 2]:
        # Bing 会在每个结果块前面塞一大串 <link rel=stylesheet>，先清掉再找标题
        chunk = LINK_TAG_RE.sub("", block)[:8000]
        match = BING_TITLE_RE.search(chunk) or BING_ANY_LINK_RE.search(chunk)
        if not match:
            continue
        url = _unwrap_bing(html_lib.unescape(match.group(1)))
        title = _clean(match.group(2))
        if not title or not url.startswith("http") or "bing.com/ck/a" in url or "bing.com/aclk" in url:
            continue
        snippet = ""
        snippet_match = BING_SNIPPET_RE.search(chunk)
        if snippet_match:
            snippet = _clean(snippet_match.group(1))[:200]
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def is_block_page(html: str) -> bool:
    """识别反爬验证页，避免把"Unfortunately…"当成搜索结果。"""
    lowered = (html or "").lower()
    markers = ("anomaly-modal", "captcha", "unusual traffic", "verify you are human", "not a robot")
    return any(marker in lowered for marker in markers)


def looks_like_spam(item: dict) -> bool:
    text = f"{item.get('title', '')} {item.get('snippet', '')} {item.get('url', '')}".lower()
    return any(word in text for word in SPAM_WORDS)


def rank_results(results: list[dict]) -> list[dict]:
    """可信站点排前面；同档次保持搜索引擎原顺序（稳定排序）。"""
    def score(item: dict) -> int:
        url = str(item.get("url", "")).lower()
        return 0 if any(domain in url for domain in TRUSTED_DOMAINS) else 1

    return sorted(results, key=score)


def clean_results(results: list[dict], *, limit: int) -> list[dict]:
    kept = [item for item in results if not looks_like_spam(item)]
    return rank_results(kept)[:limit]


def relevant(results: list[dict], query: str) -> bool:
    """结果里至少要出现一个查询词，否则说明引擎给的是诱导页/不相关内容。"""
    words = [word for word in re.split(r"\s+", (query or "").strip()) if len(word) >= 2]
    if not words:
        return True
    haystack = " ".join(
        f"{item.get('title', '')} {item.get('snippet', '')} {item.get('url', '')}".lower()
        for item in results
    )
    return any(word.lower() in haystack for word in words)


def _api_search(query: str, *, api_key: str, api_url: str, limit: int) -> ToolResult | None:
    """配置了搜索 API（例如 Tavily 免费额度）就优先用它——比抓 HTML 稳得多。"""
    if not api_key or not api_url:
        return None
    try:
        data = http.post_json(
            api_url,
            {"api_key": api_key, "query": query, "max_results": limit, "search_depth": "basic"},
            timeout=20.0,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure("web_search", f"搜索接口调用失败：{exc}")
    items = data.get("results") or []
    results = [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", ""))[:200],
        }
        for item in items[:limit]
    ]
    if not results:
        return ToolResult.failure("web_search", "搜索接口没有返回结果")
    return ToolResult(
        name="web_search", ok=True,
        text="\n".join(
            f"{index}. {item['title']}\n   {item['url']}" + (f"\n   {item['snippet']}" if item["snippet"] else "")
            for index, item in enumerate(results, start=1)
        ),
        data={"query": query, "engine": "api", "results": results},
        http_requests=1, source_type="search", confidence=0.85,
    )


def search(query: str, *, limit: int = 5, api_key: str = "", api_url: str = "") -> ToolResult:
    keyword = (query or "").strip()
    if not keyword:
        return ToolResult.failure("web_search", "没有搜索词")

    api_result = _api_search(keyword, api_key=api_key, api_url=api_url, limit=limit)
    if api_result is not None:
        return api_result

    calls = 0
    results: list[dict] = []
    engine = ""
    blocked: list[str] = []

    # 主力：Bing（本机实测可解析）
    try:
        page = http.get(BING_SEARCH.format(query=http.quote(keyword)), timeout=15.0).decode(
            "utf-8", errors="replace"
        )
        calls += 1
        if is_block_page(page):
            blocked.append("bing")
        else:
            results = parse_bing(page, limit=limit)
            engine = "bing"
    except Exception:  # noqa: BLE001 - 换引擎再试
        results = []

    # 兜底：DuckDuckGo（有些网络环境不拦，拦了就跳过）
    if not results:
        try:
            page = http.get(DDG_HTML.format(query=http.quote(keyword)), timeout=15.0).decode(
                "utf-8", errors="replace"
            )
            calls += 1
            if is_block_page(page):
                blocked.append("duckduckgo")
            else:
                results = parse_duckduckgo(page, limit=limit)
                engine = "duckduckgo"
        except Exception:  # noqa: BLE001
            results = []

    if not results:
        note = "、".join(blocked) if blocked else "所有引擎"
        return ToolResult(
            name="web_search", ok=False, http_requests=calls,
            error=f"{note} 都返回了反爬验证页，这次搜不到（不会硬刷）",
            data={"query": keyword, "blocked": blocked},
        )
    raw_count = len(results)
    results = clean_results(results, limit=limit)
    if not results:
        return ToolResult(
            name="web_search", ok=False, http_requests=calls,
            error="搜到的都是推广/垃圾结果，这次就不给你看了",
            data={"query": keyword, "filtered": raw_count, "blocked": blocked},
        )
    if not relevant(results, keyword):
        return ToolResult(
            name="web_search", ok=False, http_requests=calls,
            error=(
                "这次拿到的结果和关键词对不上（搜索引擎对无登录的抓取会喂诱导页），"
                "所以不给你看；要稳定的搜索建议配一个免费额度的搜索 API"
            ),
            data={"query": keyword, "engine": engine, "results": results, "irrelevant": True},
        )
    lines = []
    for index, item in enumerate(results, start=1):
        line = f"{index}. {item['title']}\n   {item['url']}"
        if item.get("snippet"):
            line += f"\n   {item['snippet'][:120]}"
        lines.append(line)
    return ToolResult(
        name="web_search",
        ok=True,
        text="\n".join(lines),
        data={
            "query": keyword, "engine": engine, "results": results,
            "filtered": raw_count - len(results),
        },
        http_requests=calls,
        source_type="search",
        confidence=0.7,
    )
