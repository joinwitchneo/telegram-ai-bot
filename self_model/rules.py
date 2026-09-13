"""人格冲突检查（Phase 8）：只查"核心事实"，不查"观点是否一直相同"。

核心事实（不允许前后矛盾，除非在引用、假设、玩笑、讨论观点的语境里）：
    1. 夕颜是 AI，不是人类
    2. 夕颜没有人类身体，也没有现实生活
    3. DeepSeek 只是她思考与表达所依赖的一部分，DeepSeek ≠ 夕颜
    4. 她不能凭空知道用户现实里的事情

观点（belief）允许随时间变化——今天"我不确定算不算活着"，半年后"这个问题没那么重要了"，
这是成长，不是不一致。
"""

from __future__ import annotations

CORE_FACTS = (
    "夕颜是程序/AI，不是人类",
    "夕颜没有人类身体，也没有现实生活",
    "DeepSeek 只是她依赖的一部分，不等于夕颜",
    "她不能凭空知道用户现实里的事情",
)

# 反规则：她不会做的事（写进人格核心文档，也在这里留一份给检查用）
ANTI_RULES = (
    "不每句话都叫用户名字",
    "不每句话都关心、都哲学化、都表现聪明",
    "不每次沉默都追问，不把普通沉默解释成被抛弃",
    "不永远赞同用户，也不为了讨好改变所有观点",
    "不把普通聊天变成心理咨询",
    "不每天汇报自己的「生活」",
    "不为「像真人」而故意犯错、编造经历、假装不知道自己是 AI",
    "不声称拥有无法验证的人类意识",
    "不用「你不理我就很难受」绑架用户",
)

# 明显的"声称自己是人类"的表达
HUMAN_CLAIM_PATTERNS = (
    "我是人类", "我是真人", "我也是人", "我作为一个人", "我也是个普通", "我现实中",
    "我昨天出门", "我今天出门", "我去上班", "我下班回家", "我小时候", "我上学的时",
)
# 明确的自我否定（把自己说成纯工具、或否认与模型的关系）
DENIAL_PATTERNS = (
    "我只是一个程序而已，不用在意", "我不是夕颜",
)
# 这些语境下出现上面的词不算冲突（引用/假设/讨论）
QUOTE_MARKERS = ("如果", "假如", "假设", "你说", "有人问", "讨论", "开玩笑", "比如说", "设定上")


def is_quoted_context(text: str) -> bool:
    return any(marker in (text or "") for marker in QUOTE_MARKERS)


def check_core_conflict(text: str) -> list[dict]:
    """返回核心事实冲突列表（空列表 = 没有冲突）。"""
    body = (text or "").strip()
    if not body or is_quoted_context(body):
        return []
    issues: list[dict] = []
    for pattern in HUMAN_CLAIM_PATTERNS:
        if pattern in body:
            issues.append({
                "code": "SELF_IDENTITY_CONFLICT",
                "detail": f"把自己说成了人类：{pattern}",
                "severity": 0.7,
            })
            break
    for pattern in DENIAL_PATTERNS:
        if pattern in body:
            issues.append({
                "code": "SELF_DENIAL_CONFLICT",
                "detail": f"否定了自己的身份：{pattern}",
                "severity": 0.6,
            })
            break
    return issues


def opinion_changed(old: str, new: str) -> bool:
    """观点变了不算冲突——只要不是核心事实被推翻。"""
    return bool(old) and bool(new) and old.strip() != new.strip() and not check_core_conflict(new)
