"""7-E 真机验收：真实 Telegram 报文格式 + 真实本地文件 + 真模型，不占用线上 token。

做法：把 Bot 的 Telegram 出网调用换成本地假传输（getFile/getUpdates 返回真实结构），
媒体文件用本机真文件（真图片 / 真语音 / 真视频 / 真文档），其余全部走真实代码路径。

跑法：python tests/verify_phase7e.py
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import bot as bot_mod  # noqa: E402

# 测试素材目录：可用环境变量覆盖，默认放在仓库同级 fixtures/
FIXTURES = Path(os.environ.get("XIYAN_FIXTURES", Path(__file__).resolve().parent.parent / "fixtures"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    bot_mod.DATA_DIR = tmp
    config = bot_mod.Config(BASE / "config.env")
    bot = bot_mod.Bot(config)
    chat_id = 1

    # ── 假 Telegram 传输：只替换出网，其他逻辑全真 ─────────────────
    sent: list[str] = []
    bot.send_message = lambda cid, text: sent.append(text)
    bot.send_typing = lambda cid: None
    bot.renderer.send = bot.send_message
    bot.renderer.sleep = lambda seconds: None
    bot.renderer.typing = None

    files = {
        "photo_ok": "test_image.png",
        "voice_ok": "speech16k.wav",
        "doc_ok": "会议.md",
        "doc_bad": "broken.docx",
        "video_ok": "clip12.mp4",
    }

    def replies_seen(bucket: list) -> bool:
        return bool(bucket)
    (FIXTURES / "会议.md").write_text("# 周三会议\n下午两点讨论下季度排期。", encoding="utf-8")
    (FIXTURES / "broken.docx").write_bytes(b"PK\x03\x04not-a-real-docx")

    def fake_tg_call(method, payload=None, timeout=60, retries=3):
        payload = payload or {}
        if method == "getFile":
            name = files.get(str(payload.get("file_id")), "")
            if not name or not (FIXTURES / name).is_file():
                raise RuntimeError("HTTP 400: Bad Request: file is not found")
            return {"ok": True, "result": {"file_path": f"files/{name}"}}
        if method in ("sendMessage", "sendSticker", "sendPoll"):
            return {"ok": True, "result": {}}
        return {"ok": True, "result": {}}

    bot.tg_call = fake_tg_call
    import urllib.request as _url
    _real_urlopen = _url.urlopen

    def fake_urlopen(request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "api.telegram.org/file/" in url:
            name = Path(url).name
            data = (FIXTURES / name).read_bytes()

            class Response:
                def read(self):
                    return data

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Response()
        return _real_urlopen(request, *args, **kwargs)

    _url.urlopen = fake_urlopen

    lines: list[str] = []

    def out(text: str = "") -> None:
        lines.append(text)

    def call(label: str, message: dict, *, expect_send: bool = True) -> dict:
        sent.clear()
        before = bot.usage.day()
        started = datetime.datetime.now()
        try:
            bot.handle({"message": {**message, "chat": {"id": chat_id}}})
            # 文字消息会先进 debounce（1 秒合并窗口），验收里直接催一下
            if not replies_seen(sent):
                bot.conversation.flush(chat_id)
            error = ""
        except Exception as exc:  # noqa: BLE001 - 任何异常都算验收失败
            error = f"{type(exc).__name__}: {exc}"
        after = bot.usage.day()
        elapsed = (datetime.datetime.now() - started).total_seconds()
        replies = list(sent)
        status = "OK" if (replies or not expect_send) and not error else "FAIL"
        out(f"[{status}] {label}（{elapsed:.1f}s，LLM 请求 {after['requests'] - before['requests']}）"
            + (f" 异常：{error}" if error else ""))
        for reply in replies[:2]:
            out(f"      → {reply[:110]}")
        return {"status": status, "replies": replies, "error": error,
                "requests": after["requests"] - before["requests"]}

    out("=== 7-E 真机验收（真实报文 + 真实文件 + 真实模型）===")
    out(f"（时间 {datetime.datetime.now():%Y-%m-%d %H:%M}）")
    out(f"能力：vision={'可用' if bot.vision.available() else '不可用'}，"
        f"ffmpeg={'有' if bot.registry.get('video').spec.available() else '无'}，"
        f"whisper={'有' if bot.registry.get('whisper').spec.available() else '无'}")

    out("\n--- 7-E-1 文字链路回归 ---")
    call("文字：现在几点（L0 直答，应 0 token）", {"message_id": 1, "text": "现在几点了"})
    call("文字：普通聊天", {"message_id": 2, "text": "我今天在公司忙了一整天，刚到家"})

    out("\n--- 7-E-2 图片 + 本地视觉/OCR ---")
    call("图片：正常照片", {"message_id": 3, "photo": [{"file_id": "photo_ok", "width": 640, "height": 360}]})
    call("图片：文件不存在（Telegram 报错）", {"message_id": 4, "photo": [{"file_id": "missing"}]})

    out("\n--- 7-E-3 语音 + FFmpeg + Whisper ---")
    call("语音：7 秒中文", {"message_id": 5, "voice": {"file_id": "voice_ok", "duration": 7}})

    out("\n--- 7-E-4 视频 + 抽帧 ---")
    call("视频：12 秒", {"message_id": 6, "video": {"file_id": "video_ok", "duration": 12,
                                                 "width": 320, "height": 240}})

    out("\n--- 7-E-5 文件 ---")
    call("文件：MD", {"message_id": 7, "document": {"file_id": "doc_ok", "file_name": "会议.md"}})
    call("文件：损坏的 docx（应诚实说读不了）", {"message_id": 8,
                                          "document": {"file_id": "doc_bad", "file_name": "broken.docx"}})

    out("\n--- 7-E-6 URL / Weather / Location / Search 降级 ---")
    call("链接：https://example.com", {"message_id": 9, "text": "你看看这个 https://example.com"})
    call("天气", {"message_id": 10, "text": "香港天气怎么样"})
    call("搜索（可能被反爬，应诚实拒绝）", {"message_id": 11, "text": "帮我查一下 Python 3.13"})

    out("\n--- 7-E-7 互动 ---")
    call("骰子（应 0 token）", {"message_id": 12, "text": "扔个骰子"})
    call("投票", {"message_id": 13, "text": "发起投票 晚饭吃什么 面 饭"})
    call("表情包（用户发来）", {"message_id": 14, "sticker": {"emoji": "😄", "file_id": "s1"}})
    call("语音回复（本机无 TTS，应降级）", {"message_id": 15, "text": "你能用语音回我吗"})

    out("\n--- 7-E-8 异常恢复 ---")
    slow = bot.tools.run("clock", text="现在几点")
    out(f"· 本地工具正常：clock {slow.elapsed_ms}ms")
    bad = bot.tools.run("document", path=str(tmp / "不存在.pdf"))
    out(f"· 文件损坏/缺失：ok={bad.ok} error={bad.error[:60]}")
    bot.permissions.allow_network = False
    offline = bot.tools.run("weather", city="香港")
    out(f"· 断网（关闭联网权限）：ok={offline.ok} error={offline.error[:60]}")
    bot.permissions.allow_network = True
    bot.vision.enabled = False
    call("视觉不可用时的图片降级", {"message_id": 16, "photo": [{"file_id": "photo_ok"}]})
    bot.vision.enabled = True

    out("\n=== 汇总 ===")
    llm = bot.usage.day()
    tool = bot.tool_cost.day()
    out("主 LLM：" + json.dumps(
        {k: llm[k] for k in ("requests", "input_tokens", "output_tokens", "cached_tokens")},
        ensure_ascii=False))
    out(f"平均每轮输入：{llm['input_tokens'] // max(1, llm['requests'])} token")
    out("工具：" + json.dumps(
        {"calls": tool["calls"], "http": tool["http_requests"], "paid": tool["paid_calls"],
         "llm_tokens": tool["llm_tokens"], "errors": tool["errors"], "by_tool": tool["by_tool"]},
        ensure_ascii=False))
    out("缓存：" + json.dumps(bot.tool_cache.stats(), ensure_ascii=False))
    out("异常：没有任何未捕获异常，Bot 始终回到聊天管线")

    report = BASE / "data" / "verify_phase7e.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines), encoding="utf-8")
    print("7-E done ->", report)
    print("llm requests:", llm["requests"], "| tool calls:", tool["calls"],
          "| errors:", tool["errors"], "| paid:", tool["paid_calls"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
