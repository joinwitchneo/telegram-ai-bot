"""人格一致性检查：生成完先自己审一遍，不合格就重写一次。

只做机器能可靠判断的事：客服腔、Markdown 残留、口头禅过频、句式雷同、单条过长。
语言是否"像角色"仍然交给模型，这里只兜底。
"""

from __future__ import annotations

import re

# 命中就说明"AI 味"很重
CUSTOMER_SERVICE_PATTERNS = [
    "作为一个 AI",
    "作为人工智能",
    "作为AI",
    "我理解你的",
    "我明白你的感受",
    "以下是",
    "希望对你有帮助",
    "希望这些建议",
    "当然可以，我可以帮助",
    "根据你的描述",
    "需要注意的是",
    "建议你",
    "随时可以找我",
    "有什么需要帮助的",
    "总的来说",
    "首先，",
]

# 角色的口癖：偶尔用是味道，连续用就是废
CLICHE_WORDS = ["哼", "才没有", "笨蛋", "切", "真是的", "无语", "离谱"]

# 从她的回复里抽出来的"短语"，用来统计频率
TOKEN_WATCHLIST = [
    "哼", "才没有", "笨蛋", "切", "真是的", "无语", "离谱", "笑死", "行吧",
    "随便", "活该", "欠", "滚", "服了", "？？？", "……", "真的假的", "6",
]

LAUGH_RE = re.compile(r"哈{3,}")
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
LISTY_RE = re.compile(r"^\s*[-*]\s+", re.M)


def extract_tokens(text: str) -> list[str]:
    """抽出这条回复里用到的口癖，用于频率统计。"""
    return [token for token in TOKEN_WATCHLIST if token in (text or "")]


def check_reply(messages: list[str], recent_tokens: list[str] | None = None) -> dict:
    """返回 {score, issues, banned}；score 低于 0.65 或 banned 非空就值得重写。"""
    text = "\n".join(messages or [])
    recent_tokens = recent_tokens or []
    issues: list[str] = []
    banned: list[str] = []
    score = 1.0

    for pattern in CUSTOMER_SERVICE_PATTERNS:
        if pattern in text:
            issues.append(f"客服腔：{pattern}")
            score -= 0.45

    for word in CLICHE_WORDS:
        count = text.count(word)
        if count >= 2:
            issues.append(f"口头禅重复：{word}×{count}")
            banned.append(word)
            score -= 0.35

    # 最近刚用过的口癖，这轮又用 → 提醒
    for token in set(recent_tokens):
        if token and token in text:
            issues.append(f"最近用太多次：{token}")
            if token not in banned:
                banned.append(token)
            score -= 0.2

    if len(LAUGH_RE.findall(text)) >= 2:
        issues.append("哈哈哈用太密")
        banned.append("哈哈哈哈")
        score -= 0.15

    if TABLE_RE.search(text) or LISTY_RE.search(text):
        issues.append("出现了列表或表格排版")
        score -= 0.4

    meaningful = [m for m in (messages or []) if m.strip()]
    if len(meaningful) == 1 and len(meaningful[0]) > 400:
        issues.append("单条太长，像小作文")
        score -= 0.15

    openings = [m.strip()[:4] for m in meaningful if m.strip()]
    if len(openings) >= 2 and len(set(openings)) < len(openings):
        issues.append("分条开头雷同")
        score -= 0.15

    return {"score": max(0.0, round(score, 2)), "issues": issues, "banned": banned}


def retry_hint(result: dict) -> str:
    """把检查结果变成一句给模型的重写要求。"""
    parts = []
    if result.get("issues"):
        parts.append("刚才那版有问题：" + "；".join(result["issues"]) + "。")
    if result.get("banned"):
        parts.append("这次不要用这些词：" + "、".join(result["banned"]) + "。")
    parts.append("换一种说法重写，保持同样的意思和情绪。")
    return "\n".join(parts)
