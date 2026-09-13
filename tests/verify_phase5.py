"""Phase 5 真实验证：走真实模型，但不下 Telegram（Renderer 用 dry_run）。

跑法：python tests/verify_phase5.py
结果同时写到 data/verify_phase5.txt（UTF-8），方便查看中文。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import bot as bot_mod  # noqa: E402
from core.context_debugger import ContextDebugger  # noqa: E402
from core.context_manager import ContextManager  # noqa: E402
from core.conversation import Conversation  # noqa: E402
from core.event_bus import EventBus  # noqa: E402
from core.llm_client import LLMClient  # noqa: E402
from core.response_planner import ResponsePlanner  # noqa: E402
from core.response_stats import ResponseStats  # noqa: E402
from core.response_validator import ResponseValidator  # noqa: E402
from core.token_budget import TokenBudget, estimate_tokens  # noqa: E402
from core.usage_logger import UsageLogger  # noqa: E402
from emotion.emotion_engine import EmotionEngine  # noqa: E402
from memory.memory_retriever import MemoryRetriever  # noqa: E402
from memory.memory_store import MemoryStore  # noqa: E402
from memory.topic_memory import TopicMemory  # noqa: E402
from personality.consistency_checker import ConsistencyChecker  # noqa: E402
from relationship.relationship_engine import RelationshipEngine  # noqa: E402
from style import style_adapter  # noqa: E402
from style.message_renderer import MessageRenderer, RenderLimits  # noqa: E402
from style.user_style import UserStyle  # noqa: E402


class CaptureRenderer(MessageRenderer):
    """只记录不发送（dry_run 由 Conversation 传入）。"""

    def __init__(self, limits):
        super().__init__(send=lambda chat, text: None, typing=None, limits=limits, sleep=lambda s: None)
        self.announced: list[list[str]] = []


def build_pipeline(tmp: Path):
    config = bot_mod.Config(BASE / "config.env")
    usage = UsageLogger(tmp / "usage.json")
    budget = TokenBudget(
        max_context_tokens=config.get_int("MAX_CONTEXT_TOKENS", 6000),
        max_output_tokens=config.get_int("MAX_OUTPUT_TOKENS", 400),
        daily_token_budget=config.get_int("DAILY_TOKEN_BUDGET", 300000),
    )
    client = LLMClient(
        backend=config.get("BACKEND", "deepseek").lower(),
        api_key=config.get("DEEPSEEK_API_KEY"),
        base_url=config.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        models={
            "cheap": config.get("SUMMARY_MODEL", "deepseek-flash"),
            "main": config.get("CHAT_MODEL", "deepseek-v4-flash"),
            "strong": config.get("STRONG_MODEL", ""),
        },
        fallback_order=("strong", "main", "cheap"),
        usage_logger=usage,
        token_budget=budget,
    )
    bus = EventBus()
    limits = RenderLimits(max_messages=4)
    renderer = CaptureRenderer(limits)
    context = ContextManager(
        contract=bot_mod.Bot._build_contract(),
        persona=bot_mod.Bot._load_persona(),
        token_budget=budget,
        max_context_tokens=config.get_int("MAX_CONTEXT_TOKENS", 6000),
    )
    memory_store = MemoryStore(tmp / "memories.json")
    topics = TopicMemory(tmp / "topics.json", event_bus=bus)
    retriever = MemoryRetriever(memory_store)
    emotion = EmotionEngine(tmp / "emotion.json", bus=bus)
    relationship = RelationshipEngine(tmp / "relationship.json", bus=bus)
    user_style = UserStyle(tmp / "user_style.json")
    stats = ResponseStats(tmp / "response_stats.json")
    planner = ResponsePlanner(max_messages=4)
    validator = ResponseValidator(limits=limits)
    checker = ConsistencyChecker()
    debugger = ContextDebugger(debug_dir=tmp / "debug", enabled=False, snapshot_enabled=False)
    conversation = Conversation(
        llm_client=client,
        renderer=renderer,
        context_manager=context,
        token_budget=budget,
        usage_logger=usage,
        event_bus=bus,
        debugger=debugger,
        memory_provider=lambda text, history: retriever.retrieve_blocks(text, mark_used=True),
        planner=planner,
        validator=validator,
        checker=checker,
        style_provider=lambda chat_id: {"style": style_adapter.render(user_style.profile(chat_id))},
        signal_provider=lambda: {
            "emotion": emotion.emotions(),
            "relationship": relationship.dimensions(),
        },
        response_stats=stats,
        max_retry=1,
        dry_run=True,
        history_limit=12,
    )
    return {
        "config": config, "usage": usage, "budget": budget, "client": client, "bus": bus,
        "renderer": renderer, "context": context, "conversation": conversation,
        "emotion": emotion, "relationship": relationship, "style": user_style, "stats": stats,
    }


CASES = [
    ("Case 1 在吗", ["在吗"]),
    ("Case 2 大笑", ["哈哈哈哈我今天真的笑死了"]),
    ("Case 3 考砸", ["我今天考试考砸了"]),
    ("Case 4 Python", ["你觉得我Python应该继续学吗"]),
    ("Case 5 连发", ["我", "跟你说", "个事", "算了"]),
    ("Case 6 超长", ["我最近一直在想一件事，从年初到现在都没想明白，" * 6]),
    ("Case 7 连发 8 条", ["你", "说", "我", "是不是", "该", "换个", "方向", "了"]),
    ("Case 8 短转长", ["嗯", "对了，我今天在公司遇到一件挺麻烦的事，跟我们组长有关，" * 3]),
    ("Case 9 长转嗯", ["我今天在公司忙了一整天，晚上还得加班改方案，真是有点撑不住", "嗯"]),
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    pipe = build_pipeline(tmp)
    conversation = pipe["conversation"]
    usage = pipe["usage"]
    lines: list[str] = []

    def out(text: str = "") -> None:
        lines.append(text)

    out("=== Phase 5 真实验证 ===")
    for title, messages in CASES:
        out(f"\n--- {title} ---")
        result = None
        for index, text in enumerate(messages):
            pipe["style"].observe(1, text)
            pipe["bus"].publish("UserMessageReceived", chat_id=1, text=text)
            result = conversation.process(1, text)
            if result.get("skipped"):
                out(f"  [{index + 1}] 「{text[:18]}」→ 选择不回（{result.get('reason')}）")
                continue
            if not result.get("sent"):
                out(f"  [{index + 1}] 「{text[:18]}」→ 未回复（规则路径：{result.get('decision').reason if result.get('decision') else '?'}）")
                continue
            plan = result.get("plan") or {}
            out(f"  [{index + 1}] 「{text[:18]}」→ mode={plan.get('reply_mode')} length={plan.get('length')} "
                f"tone={plan.get('tone')} 条数={len(result['sent'])} 分数={plan.get('score')} "
                f"重试={result.get('retry_used')} 兜底={result.get('fallback_used')} AI味={result.get('ai_flavor')} "
                f"问题={result.get('issues')}")
            for message in result["sent"]:
                out(f"        ·（{len(message)}字）{message}")
        if result is None:
            continue
        plan = result.get("plan") or {}
        out(f"最后一条的计划：length={plan.get('length')} 停顿={plan.get('pause')} 分数={plan.get('score')}")
        ctx = result.get("context") or {}
        tokens = ctx.get("tokens", {})
        out(f"token：L2={tokens.get('L2', 0)} 总上下文={ctx.get('total_context_tokens', 0)} "
            f"输入={result.get('input_tokens')} 输出={result.get('output_tokens')} 缓存={result.get('cached_tokens')}")

    stats = pipe["stats"].summary()
    out("\n=== 汇总 ===")
    out(json.dumps(stats, ensure_ascii=False))
    day = usage.day()
    out(f"请求数={day['requests']} 输入={day['input_tokens']} 输出={day['output_tokens']} "
        f"缓存={day['cached_tokens']} 重试={day['retries']}")
    profile = pipe["style"].profile(1)
    out("用户风格：" + json.dumps(
        {key: profile[key] for key in (
            "messages", "average_message_length", "short_message_ratio",
            "consecutive_message_count", "punctuation_style")},
        ensure_ascii=False,
    ))
    out("情绪：" + json.dumps(pipe["emotion"].emotions(), ensure_ascii=False))
    out("关系：" + json.dumps(pipe["relationship"].dimensions(), ensure_ascii=False))
    out("L2 示例 token：" + str(estimate_tokens("【当前状态】\n当前情绪：心情一般\n当前关系：关系还不算熟")))

    report = BASE / "data" / "verify_phase5.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines), encoding="utf-8")
    print("verify done ->", report)
    print("requests:", day["requests"], "in:", day["input_tokens"], "out:", day["output_tokens"],
          "cached:", day["cached_tokens"], "retries:", day["retries"], "fallbacks:", day.get("fallbacks", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
