"""Consistency Checker（Phase 5）：AI 味检查 + 事实一致性检查。

纯规则、0 token。只有严重问题才允许触发一次 Retry（由 Conversation 控制）。
检查项：
    CS_TONE        客服味
    SUMMARY_TONE   总结腔
    ECHO_USER      复述用户
    OVER_COMPLETE  过度完整/啰嗦
    OVER_POLITE    过度礼貌
    PERSONA_CONFLICT 人格/关系冲突
    MEMORY_CONFLICT  和记忆冲突
    FABRICATION    编造自己的现实经历
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SEVERITY = {
    "CS_TONE": 0.65,
    "SUMMARY_TONE": 0.55,
    "ECHO_USER": 0.45,
    "OVER_COMPLETE": 0.55,
    "FAKE_VISION": 0.8,
    "OVER_POLITE": 0.40,
    "PERSONA_CONFLICT": 0.60,
    "MEMORY_CONFLICT": 0.70,
    "FABRICATION": 0.80,
}

CS_PATTERNS = (
    "感谢您的", "很高兴能够帮", "很高兴能帮", "有什么可以帮", "请问有什么", "为您服务",
    "希望可以帮到您", "有什么需要帮", "乐意为您", "感谢您的分享", "希望这些对您有帮助",
)
SUMMARY_PATTERNS = (
    "综上", "总的来说", "总结来说", "综上所述", "总结一下", "希望以上",
)
POLITE_PATTERNS = (
    "您", "请问", "不好意思打扰", "非常感谢", "劳烦", "可否", "麻烦您",
)
FABRICATION_PATTERNS = (
    "我刚才", "我刚刚", "我刚去", "我刚吃", "我刚下班", "我今天去", "我今天吃",
    "我今天上班", "我昨天", "我昨晚", "我早上", "我下午去", "我晚上去",
    "我去买了", "我买了", "我吃了", "我喝了", "我逛了",
)
INTIMATE_ADDRESS = ("亲爱的", "宝贝", "老婆", "老公", "亲爱的你", "小可爱")
NEGATION_PATTERNS = ("没学过", "没说过", "没提过", "没告诉过", "没聊过", "从没说过", "完全没有", "根本不")

# 感知不可用时，任何"我看见/我听见"都算伪造（Phase 7 视觉事实边界）
SEEING_CLAIMS = (
    "我看到", "我看见了", "我看清", "我听得", "我听到了", "图里", "图上", "图中", "照片里",
    "照片上", "画面里", "画面中", "里面写", "上面写", "截图里", "语音里说",
)
DESCRIBING_WORDS = ("红色", "蓝色", "圆形", "圆圈", "文字", "写着", "显示", "背景", "颜色", "画着")

# Phase 8：核心事实冲突（身份层面）——观点变化不算冲突，交给 self_model.rules 判断
try:  # 允许在没有 self_model 包的环境里退化运行
    from self_model.rules import check_core_conflict as _check_core_conflict
except Exception:  # noqa: BLE001
    def _check_core_conflict(text: str) -> list[dict]:
        return []

LATIN_WORD_RE = re.compile(r"[A-Za-z0-9_]{3,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class CheckResult:
    ok: bool = True
    severity: float = 0.0
    issues: list[dict] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [str(item.get("code", "")) for item in self.issues]


def _bigrams(text: str) -> set[str]:
    raw = re.sub(r"\s+", "", text or "")
    if len(raw) < 2:
        return set(raw)
    return {raw[index : index + 2] for index in range(len(raw) - 1)}


def _overlap_ratio(user_text: str, reply_text: str) -> float:
    user_pairs = _bigrams(user_text)
    if not user_pairs:
        return 0.0
    reply_pairs = _bigrams(reply_text)
    if not reply_pairs:
        return 0.0
    shared = user_pairs & reply_pairs
    return len(shared) / max(1, min(len(user_pairs), len(reply_pairs)))


class ConsistencyChecker:
    def __init__(
        self,
        *,
        max_severity: float = 0.5,
        enabled: bool = True,
        echo_ratio: float = 0.6,
    ) -> None:
        self.max_severity = float(max_severity)
        self.enabled = bool(enabled)
        self.echo_ratio = float(echo_ratio)

    # ── 主入口 ──────────────────────────────────────────────────────
    def check(
        self,
        messages: list[str] | None,
        *,
        user_text: str = "",
        mode: str = "CASUAL",
        state: dict | None = None,
        memories: list[dict] | None = None,
        plan=None,
    ) -> CheckResult:
        if not self.enabled:
            return CheckResult(ok=True, severity=0.0, issues=[])
        texts = [str(item).strip() for item in (messages or []) if str(item).strip()]
        if not texts:
            return CheckResult(ok=True, severity=0.0, issues=[])
        joined = "\n".join(texts)
        issues: list[dict] = []

        self._check_keywords(joined, CS_PATTERNS, "CS_TONE", "客服味", issues)
        self._check_summary(joined, issues)
        self._check_echo(user_text, texts[0], issues)
        self._check_length(user_text, texts, plan, issues)
        self._check_keywords(joined, POLITE_PATTERNS, "OVER_POLITE", "过度礼貌", issues)
        self._check_persona(joined, state, issues)
        self._check_memory(joined, user_text, memories, issues)
        self._check_fabrication(joined, issues)
        self._check_self_identity(joined, issues)
        self._check_fake_perception(joined, state, issues)

        severity = max((float(item["severity"]) for item in issues), default=0.0)
        return CheckResult(ok=severity < self.max_severity, severity=severity, issues=issues)

    def _check_self_identity(self, joined: str, issues: list[dict]) -> None:
        """身份类硬冲突：把自己说成人类、或否认自己是夕颜。观点变化不在此列。"""
        for item in _check_core_conflict(joined):
            issues.append(dict(item))

    def _check_fake_perception(self, joined: str, state: dict | None, issues: list[dict]) -> None:
        """本轮没拿到视觉/听觉内容时，不允许她声称看到了（视觉事实边界）。"""
        if not state or state.get("perception_ok") is not False:
            return
        if any(word in joined for word in SEEING_CLAIMS):
            self._add(issues, "FAKE_VISION", "没有感知内容却声称看到了")
            return
        if sum(1 for word in DESCRIBING_WORDS if word in joined) >= 2:
            self._add(issues, "FAKE_VISION", "没有感知内容却在描述画面细节")

    # ── 单项 ────────────────────────────────────────────────────────
    @staticmethod
    def _add(issues: list[dict], code: str, detail: str) -> None:
        issues.append({"code": code, "detail": detail, "severity": SEVERITY.get(code, 0.5)})

    def _check_keywords(self, joined, patterns, code, detail, issues) -> None:
        for pattern in patterns:
            if pattern in joined:
                self._add(issues, code, f"{detail}：{pattern}")
                return

    def _check_summary(self, joined: str, issues: list[dict]) -> None:
        for pattern in SUMMARY_PATTERNS:
            if pattern in joined:
                self._add(issues, "SUMMARY_TONE", f"总结腔：{pattern}")
                return
        ordered = sum(1 for marker in ("首先", "其次", "最后") if marker in joined)
        if ordered >= 2 or re.search(r"^\s*[123][.、]", joined, flags=re.MULTILINE):
            self._add(issues, "SUMMARY_TONE", "分点式总结体")

    def _check_echo(self, user_text: str, first_reply: str, issues: list[dict]) -> None:
        user = CJK_RE.findall(user_text or "")
        if len(user) < 5:
            return
        ratio = _overlap_ratio(user_text, first_reply)
        if ratio >= self.echo_ratio:
            self._add(issues, "ECHO_USER", f"复述用户（重合度 {ratio:.2f}）")

    def _check_length(self, user_text: str, texts: list[str], plan, issues) -> None:
        total = sum(len(item) for item in texts)
        user_cjk = len(CJK_RE.findall(user_text or ""))
        length = ""
        if plan is not None:
            length = str(getattr(plan, "length", "") or (plan.get("length") if isinstance(plan, dict) else "") or "")
        if length in ("ULTRA_SHORT", "SHORT") and total > 90:
            self._add(issues, "OVER_COMPLETE", f"{length} 档却写了 {total} 字")
            return
        if user_cjk <= 20 and total >= 160:
            self._add(issues, "OVER_COMPLETE", f"对方只说了一句话，却回了 {total} 字")
            return
        if length == "ULTRA_SHORT" and total > 30:
            self._add(issues, "OVER_COMPLETE", "该极短回复却超长")

    def _check_persona(self, joined: str, state: dict | None, issues: list[dict]) -> None:
        if not state:
            return
        signals = state.get("signals") if isinstance(state, dict) else None
        relationship = (signals or {}).get("relationship") if isinstance(signals, dict) else None
        intimacy = 0.5
        if isinstance(relationship, dict):
            try:
                intimacy = float(relationship.get("intimacy", 0.5))
            except (TypeError, ValueError):
                intimacy = 0.5
        if intimacy < 0.25:
            for word in INTIMATE_ADDRESS:
                if word in joined:
                    self._add(issues, "PERSONA_CONFLICT", f"关系还不熟却用了亲密称呼：{word}")
                    return
        text = str(state.get("relationship") or "")
        if "还不算熟" in text and ("老朋友" in joined or "咱俩谁跟谁" in joined):
            self._add(issues, "PERSONA_CONFLICT", "关系状态与称呼不一致")

    def _check_memory(self, joined: str, user_text: str, memories: list[dict] | None, issues: list[dict]) -> None:
        if not memories:
            return
        haystack = joined.lower()
        for memory in memories:
            content = str(memory.get("content", ""))
            if not content:
                continue
            keywords = set(LATIN_WORD_RE.findall(content.lower()))
            keywords |= set(CJK_RE.findall(content))
            if not keywords:
                continue
            hits = sum(1 for keyword in keywords if keyword in haystack)
            if hits < 2:
                continue
            for pattern in NEGATION_PATTERNS:
                if pattern in joined:
                    self._add(issues, "MEMORY_CONFLICT", f"与记忆冲突（{content[:12]}）：{pattern}")
                    return
        # 用户自己刚否定过的事，回复里不能当成事实
        if user_text and any(pattern in user_text for pattern in NEGATION_PATTERNS):
            return

    def _check_fabrication(self, joined: str, issues: list[dict]) -> None:
        for pattern in FABRICATION_PATTERNS:
            if pattern in joined:
                self._add(issues, "FABRICATION", f"编造现实经历：{pattern}")
                return

    # ── Retry 提示 ──────────────────────────────────────────────────
    def correction_instruction(self, issues: list[dict]) -> str:
        codes = {str(item.get("code", "")) for item in (issues or [])}
        hints: list[str] = []
        if "CS_TONE" in codes:
            hints.append("不要客服腔、不要感谢和客套")
        if "SUMMARY_TONE" in codes:
            hints.append("不要总结体、不要分点")
        if "ECHO_USER" in codes:
            hints.append("不要复述对方刚说的话")
        if "OVER_COMPLETE" in codes:
            hints.append("短一点，像随手打字")
        if "OVER_POLITE" in codes:
            hints.append("去掉敬语，用平辈语气")
        if "PERSONA_CONFLICT" in codes:
            hints.append("称呼和语气要跟当前关系一致")
        if "MEMORY_CONFLICT" in codes:
            hints.append("不要否认已经知道的事")
        if "FABRICATION" in codes:
            hints.append("不要说自己今天去了哪、买了什么，你没有现实生活")
        if not hints:
            hints.append("重写得更像真人聊天")
        return "重写你上一条回复：" + "；".join(hints) + "。只输出要发的话。"
