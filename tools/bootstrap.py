"""把所有工具登记进 Registry，并统一标注成本等级。

这张表就是 Phase 7 的"账本"：谁是 0 API、谁要联网、谁必须花钱。
"""

from __future__ import annotations

from pathlib import Path

from tools.core.registry import ToolRegistry
from tools.core.result import ToolResult
from tools.core.schema import ToolSpec
from tools.interaction import game as game_mod
from tools.interaction import poll as poll_mod
from tools.interaction import sticker as sticker_mod
from tools.interaction import voice_reply as voice_mod
from tools.perception import audio as audio_mod
from tools.perception import document as document_mod
from tools.perception import image as image_mod
from tools.perception import video as video_mod
from tools.perception import whisper as whisper_mod
from tools.world import clock as clock_mod
from tools.world import location as location_mod
from tools.world import rss as rss_mod
from tools.world import weather as weather_mod
from tools.world import web_reader, web_search


def build_registry(
    *,
    city: str = "",
    vision=None,
    perception=None,
    sticker_book: sticker_mod.StickerBook | None = None,
    tts: voice_mod.LocalTTS | None = None,
    whisper_model_path: str = "",
    search_api_key: str = "",
    search_api_url: str = "",
) -> ToolRegistry:
    registry = ToolRegistry()

    # ── 本地 0 API ─────────────────────────────────────────────────
    registry.register(
        ToolSpec(
            name="clock", mode="LOCAL", level="L0",
            description="当前时间 / 日期 / 星期 / 倒计时（纯 Python）",
            keywords=("几点", "星期几", "几号", "倒计时"), output_kind="text",
        ),
        lambda text="", **_: ToolResult(
            name="clock", ok=True, text=clock_mod.handle_text(text, city=city), data={}, source_type="time"
        ),
    )
    hub = game_mod.GameHub()
    registry.register(
        ToolSpec(
            name="game", mode="LOCAL", level="L0",
            description="骰子 / 抛硬币 / 猜拳 / 猜数字（纯 Python）",
            keywords=("骰子", "猜数字", "猜拳", "抛硬币"),
        ),
        lambda text="", chat_id=0, **_: game_mod.handle_text(text, chat_id=chat_id, hub=hub),
    )
    book = sticker_book or sticker_mod.StickerBook()
    registry.register(
        ToolSpec(
            name="sticker", mode="LOCAL", level="L0",
            description="按情绪发本地表情包",
            configured=book.available(),
            output_kind="media",
        ),
        lambda tag="", **_: book.pick(tag),
    )
    registry.register(
        ToolSpec(
            name="poll", mode="LOCAL", level="L0",
            description="发起 Telegram 原生投票",
            requires=(), output_kind="json",
        ),
        lambda text="", **_: poll_mod.parse(text),
    )
    registry.register(
        ToolSpec(
            name="image", mode="LOCAL", level="L1",
            description="图片元数据（本地解析文件头）+ 交给本地视觉/OCR",
            output_kind="perception",
        ),
        lambda path="", **_: image_mod.inspect(path),
    )
    registry.register(
        ToolSpec(
            name="audio", mode="LOCAL", level="L1",
            description="语音元数据 + 本地 Whisper 识别",
            requires=("ffmpeg",) if not audio_mod.ffmpeg_available() else (),
            output_kind="perception",
        ),
        lambda path="", meta=None, **_: audio_mod.build_result(path, telegram_meta=meta),
    )
    registry.register(
        ToolSpec(
            name="video", mode="LOCAL", level="L1",
            description="视频元数据（Telegram 自带）+ ffmpeg 抽帧 + 本地视觉",
            requires=() if video_mod.ffmpeg_available() else ("ffmpeg",),
            output_kind="perception",
        ),
        lambda path="", meta=None, **_: video_mod.build_result(path, telegram_meta=meta),
    )
    registry.register(
        ToolSpec(
            name="document", mode="LOCAL", level="L0",
            description="本地解析 txt/md/csv/json/代码/docx/pdf",
            output_kind="perception",
        ),
        lambda path="", **_: document_mod.parse(path),
    )
    registry.register(
        ToolSpec(
            name="whisper", mode="LOCAL", level="L1",
            description="whisper.cpp 本地语音识别",
            configured=whisper_mod.available(whisper_model_path),
            requires=() if whisper_mod.find_binary() else ("whisper-cli",),
            output_kind="text",
        ),
        lambda wav="", **_: whisper_mod.transcribe(wav, model_path=whisper_model_path),
    )
    tts_engine = tts or voice_mod.LocalTTS()
    registry.register(
        ToolSpec(
            name="voice_reply", mode="LOCAL", level="L1",
            description="本地 TTS 生成语音回复",
            configured=tts_engine.available(),
            output_kind="media",
        ),
        lambda text="", **_: tts_engine.synthesize(text),
    )

    # ── 免费网络（0 AI API）────────────────────────────────────────
    registry.register(
        ToolSpec(
            name="weather", mode="FREE_NETWORK", level="L1",
            description="Open-Meteo 天气（缓存 10 分钟）",
            cache_ttl=600, output_kind="text", cost_note="HTTP only",
        ),
        lambda city="", latitude=None, longitude=None, **_: weather_mod.fetch(
            city or "", latitude=latitude, longitude=longitude
        ),
    )
    registry.register(
        ToolSpec(
            name="web_reader", mode="FREE_NETWORK", level="L1",
            description="读取网页正文（HTTP + 本地解析，0 AI API）",
            cache_ttl=900, output_kind="text", cost_note="HTTP only",
        ),
        lambda url="", **_: web_reader.read_url(url),
    )
    registry.register(
        ToolSpec(
            name="web_search", mode="FREE_NETWORK", level="L1",
            description="网页搜索（HTML 解析，缓存 10 分钟）",
            cache_ttl=600, output_kind="text", cost_note="HTTP only",
        ),
        lambda text="", query="", **_: web_search.search(
            query or text, api_key=search_api_key, api_url=search_api_url
        ),
    )
    registry.register(
        ToolSpec(
            name="location", mode="FREE_NETWORK", level="L1",
            description="地名查询（免费地理编码 + 缓存）",
            cache_ttl=86400, output_kind="text", cost_note="HTTP only",
        ),
        lambda name="", text="", **_: location_mod.lookup(name or text),
    )
    registry.register(
        ToolSpec(
            name="rss", mode="FREE_NETWORK", level="L1",
            description="RSS/Atom 标题（用于简报）",
            cache_ttl=1800, output_kind="text", cost_note="HTTP only",
        ),
        lambda feed_url="", **_: rss_mod.fetch(feed_url),
    )

    # ── 付费：只有主聊天模型 ───────────────────────────────────────
    registry.register(
        ToolSpec(
            name="main_llm", mode="PAID_API", level="L2",
            description="主聊天模型（角色表达、自然语言理解、复杂推理）",
            output_kind="text", cost_note="DeepSeek API",
        ),
        lambda **_: ToolResult(
            name="main_llm", ok=True, text="主 LLM 由 Conversation 调用，不走 Tool Executor",
            data={"handled_by": "conversation"},
        ),
    )
    return registry
