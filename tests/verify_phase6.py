"""Phase 6 真实验证：真实模型 + 真实 Bot 接线，但不真的发 Telegram。

跑法：python tests/verify_phase6.py
结果写到 data/verify_phase6.txt（UTF-8）。
"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import bot as bot_mod  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    bot_mod.DATA_DIR = tmp  # 验证用独立数据目录，不污染真实状态
    config = bot_mod.Config(BASE / "config.env")
    bot = bot_mod.Bot(config)

    captured: list[tuple[int, str]] = []
    bot.send_message = lambda chat_id, text: captured.append((chat_id, text))
    bot.send_typing = lambda chat_id: None
    bot.renderer.send = bot.send_message
    bot.renderer.sleep = lambda seconds: None
    bot.renderer.typing = None

    lines: list[str] = []

    def out(text: str = "") -> None:
        lines.append(text)

    def usage_snapshot() -> tuple[int, int]:
        day = bot.usage.day()
        return day["requests"], day["input_tokens"]

    chat_id = bot.proactive_chat_id or 1
    real_now = datetime.datetime.now()

    def at(hours: float, hour: int = 14, minute: int = 0) -> datetime.datetime:
        """把"过了 N 小时"落到当天某个白天时刻，避免撞上静默时段。"""
        moment = real_now + datetime.timedelta(hours=hours)
        return moment.replace(hour=hour, minute=minute, second=0, microsecond=0)

    out("=== Phase 6 真实验证 ===")
    out(f"（模拟起点：{real_now:%Y-%m-%d %H:%M}）")

    # ── Case 1：用户说"我明天考试" → 第二天她想起来问 ──────────────
    out("\n--- Case 1：用户说“我明天考试”，第二天中午 ---")
    before = usage_snapshot()
    bot._update_character_life("我明天考试。")
    bot.proactive.on_user_message(now=real_now)
    out("角色生活：" + json.dumps(bot.life.active(), ensure_ascii=False)[:200])
    day2 = at(20, hour=12, minute=30)
    result = bot.proactive.tick(now=day2)
    out(f"是否主动：{result.get('sent')}　原因：{result.get('reason')}")
    if result.get("candidate"):
        out(f"候选：reason={result['candidate']['reason']} score={result['candidate']['score']} "
            f"special={result['candidate']['special']}")
        out(f"理由（内部）：{result['candidate']['hint']}")
    for message in result.get("messages") or []:
        out(f"  ·（{len(message)}字）{message}")
    after = usage_snapshot()
    out(f"token：请求 {after[0] - before[0]} 次，输入 {after[1] - before[1]}")

    # ── Case 2：用户说"Python 我考虑继续学" → 过一阵她问结果 ───────
    out("\n--- Case 2：用户说“Python 我考虑继续学”，过了三十小时 ---")
    before = usage_snapshot()
    bot._update_character_life("Python 我考虑继续学。")
    bot.proactive.on_user_message(now=real_now)
    day3 = at(30, hour=15)
    result = bot.proactive.tick(now=day3)
    out(f"是否主动：{result.get('sent')}　原因：{result.get('reason')}")
    if result.get("candidate"):
        out(f"候选：reason={result['candidate']['reason']} score={result['candidate']['score']}")
        out(f"理由（内部）：{result['candidate']['hint']}")
    for message in result.get("messages") or []:
        out(f"  ·（{len(message)}字）{message}")
    after = usage_snapshot()
    out(f"token：请求 {after[0] - before[0]} 次，输入 {after[1] - before[1]}")

    # ── Case 3：没有任何未完成事件 → 不能凭空生成 ─────────────────
    out("\n--- Case 3：没有任何未完成事件 ---")
    bot.life.reset()
    bot.memory_store.data = []
    before = usage_snapshot()
    result = bot.proactive.tick(now=at(60, hour=16))
    out(f"是否主动：{result.get('sent')}　原因：{result.get('reason')}")
    after = usage_snapshot()
    out(f"token：请求 {after[0] - before[0]} 次（应当为 0）")

    # ── Case 4：连续两次没回复 → 第三次不骚扰 ─────────────────────
    out("\n--- Case 4：连续两次主动都没被回复 ---")
    bot._update_character_life("我后天有个面试。")
    # 用当天中午做模拟：这样冷却期落在白天，不会被静默时段盖掉
    noon = at(0, hour=12)
    bot.proactive.on_user_message(now=noon - datetime.timedelta(hours=6))
    old = (noon - datetime.timedelta(hours=5)).isoformat()
    bot.proactive_state.data["pending"] = [{"id": "x", "ts": old, "topic": "x"}]
    bot.proactive.settle_pending(noon)
    bot.proactive_state.data["pending"] = [{"id": "y", "ts": old, "topic": "y"}]
    bot.proactive.settle_pending(noon)
    out(f"连续未回复：{bot.proactive_state.data['nonresponse_streak']}　"
        f"冷却到：{bot.proactive_state.data['cooldown_until']}")
    result = bot.proactive.tick(now=noon + datetime.timedelta(hours=1))
    out(f"第三次是否主动：{result.get('sent')}　原因：{result.get('reason')}")

    # ── Case 5：用户回来说"刚才在忙" → 冷却解除 ───────────────────
    out("\n--- Case 5：用户回来说“刚才在忙” ---")
    heat_before = bot.relationship.value("interaction_heat")
    cleared = bot.proactive.on_user_message(now=noon + datetime.timedelta(hours=1, minutes=10))
    bot.bus.publish("UserReturned", gap_hours=7.0)
    cleared2 = bot.proactive.on_user_message(now=noon + datetime.timedelta(hours=1, minutes=10))
    heat_after = bot.relationship.value("interaction_heat")
    out(f"冷却是否解除：{cleared['cooldown_reset'] or cleared2['cooldown_reset']}　"
        f"连续未回复：{bot.proactive_state.data['nonresponse_streak']}")
    out(f"互动热度：{heat_before:.3f} → {heat_after:.3f}")

    # ── Case 6：深夜不主动，但用户主动发消息仍然正常回 ────────────
    out("\n--- Case 6：凌晨两点 ---")
    bot._update_character_life("我下周要出差。")
    night = real_now.replace(hour=2, minute=0, second=0, microsecond=0)
    before = usage_snapshot()
    result = bot.proactive.tick(now=night)
    after = usage_snapshot()
    out(f"是否主动：{result.get('sent')}　原因：{result.get('reason')}　"
        f"token 增加：{after[0] - before[0]} 次请求")
    reply = bot.conversation.process(chat_id, "还没睡。")
    out(f"用户主动发消息 → 正常回复：{reply.get('sent')}")
    out(f"（这一轮输入 {reply.get('input_tokens')} token，缓存 {reply.get('cached_tokens')}）")

    out("\n=== 汇总 ===")
    out("主动历史：" + json.dumps(bot.proactive_history.stats(), ensure_ascii=False))
    out("主动状态：" + json.dumps(
        {key: value for key, value in bot.proactive.status(now=real_now).items()
         if key in ("sent_today", "nonresponse_streak", "cooldown_until", "last_sent_at")},
        ensure_ascii=False,
    ))
    day = bot.usage.day()
    out(f"总请求 {day['requests']} 次：输入 {day['input_tokens']}／输出 {day['output_tokens']}／缓存 {day['cached_tokens']}")
    out("分类：" + json.dumps(day.get("by_category", {}), ensure_ascii=False))
    out(f"角色生活：{json.dumps(bot.life.stats(), ensure_ascii=False)}")

    report = BASE / "data" / "verify_phase6.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines), encoding="utf-8")
    print("verify done ->", report)
    print("requests:", day["requests"], "in:", day["input_tokens"], "out:", day["output_tokens"],
          "cached:", day["cached_tokens"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
