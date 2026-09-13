"""按需运行的真实环境验证（不属于 unittest 套件，不会被自动发现）。

用法：python tests/verify_context.py
会读取 config.env、构建 Context、打印四层 token 与稳定性，可选做一次真实调用。
"""

from __future__ import annotations

import datetime
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import Bot, Config  # noqa: E402
from core.context_manager import ContextManager  # noqa: E402


def history(count: int, size: int = 20) -> list[dict]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"消息{index}-" + "字" * size,
        }
        for index in range(count)
    ]


def main() -> int:
    bot = Bot(Config(ROOT / "config.env"))
    manager = bot.context_manager
    prefix = manager.static_prefix()
    print(f"静态前缀：{len(prefix)} 字符，SHA256={hashlib.sha256(prefix.encode()).hexdigest()[:16]}")

    print("\n=== 四种模式的窗口与分层 token ===")
    cases = [
        ("在吗", "CASUAL"),
        ("其实我想认真跟你聊聊这件事", "DEEP"),
        ("我今天好烦", "EMOTIONAL"),
        ("帮我查一下火车票", "TASK"),
    ]
    for text, label in cases:
        built = manager.build(user_text=text, history=history(40))
        tokens = built.stats["tokens"]
        print(
            f"{label:10} 实测={built.mode:10} 历史={built.stats['history_count']:3}条 "
            f"L0={tokens['L0']:3} L1={tokens['L1']:3} L2={tokens['L2']:3} L3={tokens['L3']:3} "
            f"L4={tokens['L4']:4} 总计={built.stats['total_context_tokens']:4} 上限={built.stats['max_context_tokens']}"
        )

    print("\n=== 静态前缀稳定性（连续三次构建）===")
    digests = []
    for index in range(3):
        built = manager.build(
            user_text=f"第{index}句",
            history=history(index),
            now=datetime.datetime(2026, 9, 13, 10 + index, 0),
        )
        digests.append(hashlib.sha256(built.messages[0]["content"].encode()).hexdigest()[:16])
        dynamic_has_time = "【当前时间】" in built.messages[1]["content"]
    print(f"messages[0] 哈希：{digests} → 一致：{len(set(digests)) == 1}")
    print(f"动态消息含时间：{dynamic_has_time}")

    print("\n=== 预算裁剪（极小上限）===")
    tight = ContextManager(
        contract=manager.contract,
        persona=manager.persona,
        token_budget=bot.budget,
        max_context_tokens=500,
        min_history=2,
    )
    built = tight.build(user_text="在吗", history=history(60, 80), mode="CASUAL")
    print(f"保留 {built.stats['history_count']} 条，裁掉 {built.stats['history_dropped']} 条")
    print("L0 仍在：", any(b.name == "L0" for b in built.blocks), "｜L1 仍在：", any(b.name == "L1" for b in built.blocks))
    print("保留的是最近的消息：", built.messages[-2]["content"].startswith("消息59"))

    print("\n=== 一次真实调用（走完整 Context 管线）===")
    captured = []
    bot.renderer.send = lambda chat, text: captured.append(text)
    result = bot.conversation.process(99999, "我今天在公司忙了一整天，刚到家")
    record = bot.debugger.last()
    print("模式：", result["mode"], "｜模型档位：", result.get("tier"), "｜模型：", result.get("model"))
    print("输入/输出/缓存 token：", result.get("input_tokens"), "/", result.get("output_tokens"), "/", result.get("cached_tokens"))
    print("回复：", captured)
    if record:
        print("Debugger tokens：", record["tokens"], "｜total:", record["total_context_tokens"])

    print("\n=== Phase 3：记忆写入与召回（真实调用）===")
    chat_id = 88888
    for message in ("我最近在学Python", "我已经开始学爬虫了"):
        captured.clear()
        bot.conversation.process(chat_id, message)
        print(f"用户：{message}")
        print("  回复：", captured)
        print("  记忆：", [(m["id"], m["content"], m["temperature"]) for m in bot.memory_store.list()])
    captured.clear()
    result = bot.conversation.process(chat_id, "你还记得我在学什么吗")
    record = bot.debugger.last()
    print("用户：你还记得我在学什么吗")
    print("  回复：", captured)
    print("  L3 token：", result["context"]["tokens"]["L3"])
    if record:
        print("  召回记忆 id：", record.get("retrieved_memory_ids"))
        print("  召回分数：", record.get("retrieved_memory_scores"))
    print("  记忆仓库统计：", bot.memory_store.stats())
    print("  话题统计：", bot.topics.stats())

    print("\n=== Phase 4：情绪 / 关系 ===")
    import tempfile as _tempfile

    from core.event_bus import EventBus
    from emotion.emotion_engine import EmotionEngine
    from relationship.relationship_engine import RelationshipEngine

    tmp = Path(_tempfile.mkdtemp())
    bus = EventBus()
    emotion = EmotionEngine(tmp / "emotion.json", bus=bus)
    relationship = RelationshipEngine(tmp / "relationship.json", bus=bus)

    def snapshot(label):
        emo = emotion.emotions()
        rel = relationship.dimensions()
        print(
            f"  {label:14} joy={emo['joy']:.3f} anger={emo['anger']:.3f} affection={emo['affection']:.3f} "
            f"| 熟悉={rel['familiarity']:.3f} 信任={rel['trust']:.3f} 亲密={rel['intimacy']:.3f} "
            f"热度={rel['interaction_heat']:.3f} 共同经历={rel['shared_experience']:.3f}"
        )

    print("Case 1 普通聊天（连续三句）")
    snapshot("初始")
    for line in ("在吗", "今天上班有点忙", "刚到家"):
        bus.publish("UserMessageReceived", text=line)
    snapshot("三句之后")

    print("Case 2 正向事件")
    for _ in range(3):
        bus.publish("UserMessageReceived", text="你今天帮我解决这个问题，真的挺厉害的")
    snapshot("被夸之后")

    print("Case 3 负向事件")
    bus.publish("UserMessageReceived", text="我今天真的烦死了")
    snapshot("他情绪差")
    print(f"  → anger={emotion.value('anger'):.3f}（远小于 1，符合「不跳变」要求）")

    print("Case 4 长时间不聊天（时间推进）")
    rel_before = relationship.dimensions()
    for hours in (1, 6, 24, 72, 168):
        emotion.tick(hours=hours)
        relationship.tick(hours=hours)
        print(
            f"    +{hours:3}h：joy={emotion.value('joy'):.3f} 热度={relationship.value('interaction_heat'):.3f} "
            f"信任={relationship.value('trust'):.3f} 共同经历={relationship.value('shared_experience'):.3f}"
        )
    print(f"  → 信任从 {rel_before['trust']:.3f} 到 {relationship.value('trust'):.3f}（慢变量不归零）")

    print("Case 5 关系积累（50 次普通互动，纯 Python，0 token）")
    deep = RelationshipEngine(tmp / "relationship.json")
    deep.reset()
    for _ in range(50):
        deep.apply_event("USER_CHAT")
    dims = deep.dimensions()
    print(
        f"  50 次聊天后：熟悉={dims['familiarity']:.3f} 热度={dims['interaction_heat']:.3f} "
        f"信任={dims['trust']:.3f} 亲密={dims['intimacy']:.3f}"
    )

    print("Case 6 L2 实际注入内容（真实调用）")
    captured.clear()
    result = bot.conversation.process(chat_id, "在吗")
    print("  模式：", result["mode"], "｜L2 token：", result["context"]["tokens"]["L2"])
    print("  她的状态（给模型看的自然语言）：")
    print("    情绪：", bot.emotion.describe())
    print("    关系：", bot.relationship.describe())
    print("    行为：", "；".join(bot.emotion.behavior_hints() + bot.relationship.behavior_hints())[:120])
    print("  实际发出的消息：", captured)
    print("  情绪/关系文件：", (bot.emotion.path.name, bot.relationship.path.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
