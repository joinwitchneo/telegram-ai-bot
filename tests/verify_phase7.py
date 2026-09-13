"""Phase 7 真实验证：真实联网跑免费网络工具 + 真实模型跑感知回复。

跑法：python tests/verify_phase7.py
结果写到 data/verify_phase7.txt（UTF-8）。不真的发 Telegram。
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
from tools.perception import ocr as ocr_mod  # noqa: E402
from tools.perception import whisper as whisper_mod  # noqa: E402
from tools.perception.router import PerceptionRequest  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    bot_mod.DATA_DIR = tmp
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

    out("=== Phase 7 工具层真实验证 ===")
    out(f"（时间：{datetime.datetime.now():%Y-%m-%d %H:%M}）")

    # ── 1. 能力盘点 ────────────────────────────────────────────────
    out("\n--- 1. 工具能力盘点 ---")
    report = bot.registry.cost_report()
    labels = {"LOCAL": "本地 0 API", "FREE_NETWORK": "联网免费", "PAID_API": "付费"}
    for mode, names in report["by_mode"].items():
        out(f"{labels.get(mode, mode)}：{', '.join(names) or '（无）'}")
    out(f"暂时用不了：{', '.join(report['unavailable']) or '（无）'}")
    out(f"本地视觉（Ollama）：{'可用' if bot.vision.available() else '不可用'}")
    out(f"Windows 自带 OCR：{'可用' if ocr_mod.windows_ocr_available() else '不可用'}"
        f"　tesseract：{'有' if ocr_mod.tesseract_available() else '没有'}")
    out(f"whisper.cpp：{'有' if whisper_mod.find_binary() else '没有'}"
        f"　ffmpeg：{'有' if bot.registry.get('video').spec.available() else '没有'}")

    # ── 2. 本地工具（0 token）──────────────────────────────────────
    out("\n--- 2. 本地工具（应当 0 token）---")
    for name, kwargs in (
        ("clock", {"text": "现在几点了"}),
        ("game", {"text": "掷骰子", "chat_id": 1}),
        ("poll", {"text": "发起投票 晚饭吃什么 面 饭"}),
    ):
        result = bot.tools.run(name, **kwargs)
        out(f"· {name}: {result.text}（mode={result.mode} level={result.level} "
            f"llm_tokens={result.llm_tokens} 耗时 {result.elapsed_ms}ms）")

    doc = tmp / "会议.md"
    doc.write_text("# 周三会议\n\n下午两点，讨论下个季度的排期。", encoding="utf-8")
    doc_result = bot.tools.run("document", path=str(doc))
    out(f"· document: {doc_result.text[:60]}…（ok={doc_result.ok}）")

    img = tmp / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1024).to_bytes(4, "big")
                    + (768).to_bytes(4, "big") + b"\x00" * 32)
    img_result = bot.tools.run("image", path=str(img))
    out(f"· image: {img_result.text}")

    # ── 3. 免费网络工具（0 AI API，只有 HTTP）──────────────────────
    out("\n--- 3. 免费网络工具（0 AI API）---")
    for name, kwargs in (
        ("weather", {"city": "香港"}),
        ("location", {"name": "湖南工业大学"}),
        ("web_reader", {"url": "https://example.com"}),
        ("web_search", {"query": "Python 3.13 有什么新特性"}),
    ):
        result = bot.tools.run(name, **kwargs)
        head = (result.text or result.error).replace("\n", " ")[:160]
        out(f"· {name}: ok={result.ok} HTTP={result.http_requests} 缓存={result.cached} → {head}")
    cached = bot.tools.run("weather", city="香港")
    out(f"· weather 第二次（应命中缓存）：缓存={cached.cached}")

    # ── 4. 感知降级（本机没有视觉/语音能力时的表现）────────────────
    out("\n--- 4. 感知层（本地能力缺失时是否诚实降级）---")
    for source, path, meta in (
        ("image", str(img), {}),
        ("voice", str(doc), {"duration": 14}),
        ("video", str(img), {"duration": 8}),
    ):
        perception = bot.perception.perceive(
            PerceptionRequest(source_type=source, path=path, meta=meta)
        )
        summary = bot_mod.PerceptionRouter.describe_for_context(perception)
        out(f"· {source}: ok={perception.ok} → {summary[:100]}")

    # ── 5. 真实模型：感知结果如何变成她的话 ────────────────────────
    out("\n--- 5. 真实模型回复（1 次主 LLM 调用/条）---")
    for source, summary in (
        ("image", "用户发来一张 1024×768 的png图片。本机现在没有可用的视觉/OCR 能力，"
                  "所以你看不到画面内容——别假装看到了，也别编内容，可以请他讲讲这是什么。"),
        ("document", doc_result.text),
    ):
        before = bot.usage.day()
        result = bot.conversation.perceive(1, source_type=source, summary=summary)
        after = bot.usage.day()
        out(f"· {source} → {result.get('sent')}")
        out(f"  token：请求 {after['requests'] - before['requests']} 次，"
            f"输入 {after['input_tokens'] - before['input_tokens']}，"
            f"缓存 {after['cached_tokens'] - before['cached_tokens']}")

    # ── 6. 成本台账 ────────────────────────────────────────────────
    out("\n--- 6. 成本台账 ---")
    day = bot.tool_cost.day()
    out("工具调用：" + json.dumps(day, ensure_ascii=False))
    out("缓存：" + json.dumps(bot.tool_cache.stats(), ensure_ascii=False))
    llm_day = bot.usage.day()
    out(f"主 LLM：{llm_day['requests']} 次请求，输入 {llm_day['input_tokens']}，"
        f"输出 {llm_day['output_tokens']}，缓存 {llm_day['cached_tokens']}")
    out("分类：" + json.dumps(llm_day.get("by_category", {}), ensure_ascii=False))
    local_calls = sum(count for name, count in day.get("by_tool", {}).items()
                      if name in report["by_mode"]["LOCAL"])
    out(f"其中本地工具调用 {local_calls} 次，付费 AI 调用 {day.get('paid_calls', 0)} 次")

    report_path = BASE / "data" / "verify_phase7.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print("verify done ->", report_path)
    print("tool calls:", day["calls"], "http:", day["http_requests"],
          "llm requests:", llm_day["requests"], "llm tokens:", llm_day["input_tokens"] + llm_day["output_tokens"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
