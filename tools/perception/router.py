"""感知路由器：图片 / 语音 / 视频 / 文档 统一走这里，输出同一个结构。

原则：能本地就本地；本地没有能力时**诚实降级**，不假装看到了，
也不偷偷把图片上传给云端。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path

from tools.core.result import ToolResult
from tools.perception import audio as audio_mod
from tools.perception import document as document_mod
from tools.perception import image as image_mod
from tools.perception import ocr as ocr_mod
from tools.perception import video as video_mod
from tools.perception import whisper as whisper_mod


@dataclass
class PerceptionRequest:
    source_type: str
    path: str = ""
    meta: dict = field(default_factory=dict)


class PerceptionRouter:
    def __init__(
        self,
        *,
        vision=None,
        use_ocr: bool = True,
        whisper_model_path: str = "",
        whisper_prompt: str = "",
        language: str = "zh",
        max_frames: int = 4,
        vision_prompt: str = "",
    ) -> None:
        self.vision = vision
        self.use_ocr = bool(use_ocr)
        self.whisper_model_path = whisper_model_path
        self.whisper_prompt = whisper_prompt
        self.language = language
        self.max_frames = max(1, int(max_frames))
        self.vision_prompt = vision_prompt

    # ── 对外 ────────────────────────────────────────────────────────
    def perceive(self, request: PerceptionRequest) -> ToolResult:
        source = str(request.source_type or "").lower()
        if source == "image":
            return self._image(request)
        if source in ("voice", "audio"):
            return self._audio(request)
        if source == "video":
            return self._video(request)
        if source == "document":
            return self._document(request)
        return ToolResult.failure("perception", f"不认识这种内容：{request.source_type}")

    @staticmethod
    def describe_for_context(result: ToolResult) -> str:
        """把感知结果写成给主 LLM 看的一句话（不含任何内部细节）。"""
        if result.ok:
            return result.text
        note = result.data.get("degraded_note") if isinstance(result.data, dict) else ""
        return str(note or result.error or "没看懂这条内容")

    # ── 各类型 ──────────────────────────────────────────────────────
    def _image(self, request: PerceptionRequest) -> ToolResult:
        logging.info("[perception] image received")
        path = Path(request.path) if request.path else None
        if path is not None:
            exists = path.is_file()
            size = path.stat().st_size if exists else 0
            logging.info("[perception] image path=%s exists=%s bytes=%d", path, exists, size)
        else:
            logging.warning("[perception] image 没有本地文件路径")
        info = image_mod.inspect(request.path)
        if not info.ok:
            logging.warning("[perception] 图片元数据读取失败：%s", info.error)
            return info
        pieces = [f"用户发来{info.text}"]
        ocr_text = ""
        if self.use_ocr:
            ocr_result = ocr_mod.recognize(request.path)
            logging.info("[ocr] success=%s chars=%d error=%s",
                         ocr_result.ok, len(ocr_result.text or ""), ocr_result.error or "-")
            if ocr_result.ok:
                ocr_text = ocr_result.text
                pieces.append(f"图片里的文字：{ocr_text[:600]}")
        vision_text = ""
        vision_available = bool(self.vision is not None and getattr(self.vision, "available", lambda: False)())
        model = ""
        if vision_available and hasattr(self.vision, "resolve_model"):
            model = self.vision.resolve_model() or ""
        logging.info("[perception] router=vision available=%s model=%s", vision_available, model or "-")
        if vision_available:
            vision_result = self.vision.describe(request.path, prompt=self.vision_prompt)
            logging.info("[vision] success=%s latency_ms=%s text=%s error=%s",
                         vision_result.ok, getattr(vision_result, "elapsed_ms", 0),
                         (vision_result.text or "")[:120], vision_result.error or "-")
            if vision_result.ok:
                vision_text = vision_result.text
                pieces.append(f"画面内容：{vision_text}")
        if ocr_text or vision_text:
            return ToolResult(
                name="perception.image", ok=True, text="；".join(pieces),
                data={
                    "metadata": info.data.get("metadata", {}),
                    "text": ocr_text,
                    "vision": vision_text,
                    "source_type": "image",
                },
                source_type="image", confidence=0.85 if vision_text else 0.7,
            )
        degraded = (
            f"用户发来{info.text}。本机现在没有可用的视觉/OCR 能力，"
            "所以你看不到画面内容——别假装看到了，也别编内容，可以请他讲讲这是什么。"
        )
        return ToolResult(
            name="perception.image", ok=False, error="本机没有视觉/OCR 能力",
            text=degraded, data={"metadata": info.data.get("metadata", {}), "degraded_note": degraded},
            source_type="image", confidence=0.0,
        )

    def _audio(self, request: PerceptionRequest) -> ToolResult:
        info = audio_mod.build_result(request.path, telegram_meta=request.meta)
        wav = str(info.data.get("metadata", {}).get("wav", ""))
        if wav and whisper_mod.available(self.whisper_model_path):
            spoken = whisper_mod.transcribe(
                wav, model_path=self.whisper_model_path, language=self.language,
                prompt=self.whisper_prompt,
            )
            if spoken.ok:
                return ToolResult(
                    name="perception.voice", ok=True,
                    text=f"用户发来{info.text}，说的是：「{spoken.text}」",
                    data={"metadata": info.data.get("metadata", {}), "text": spoken.text,
                          "source_type": "voice"},
                    source_type="voice", confidence=0.8,
                )
        reason = "本机没有可用的语音识别" if not whisper_mod.find_binary() else "语音转码不可用（缺 ffmpeg）"
        if not audio_mod.ffmpeg_available():
            reason = "缺 ffmpeg，暂时没法把语音转成可识别的音频"
        degraded = (
            f"用户发来{info.text}，但{reason}，你听不到内容。"
            "别假装听懂了，可以请他打字说一遍。"
        )
        return ToolResult(
            name="perception.voice", ok=False, error=reason, text=degraded,
            data={"metadata": info.data.get("metadata", {}), "degraded_note": degraded},
            source_type="voice", confidence=0.0,
        )

    def _video(self, request: PerceptionRequest) -> ToolResult:
        info = video_mod.build_result(request.path, telegram_meta=request.meta)
        frames = list(info.data.get("frames", []))
        seen: list[str] = []
        if frames and self.vision is not None and getattr(self.vision, "available", lambda: False)():
            for frame in frames[: self.max_frames]:
                result = self.vision.describe(frame, prompt=self.vision_prompt)
                if result.ok and result.text:
                    seen.append(result.text)
        if seen:
            return ToolResult(
                name="perception.video", ok=True,
                text=f"{info.text}。画面大概是：{' / '.join(seen)}",
                data={**info.data, "vision": seen, "source_type": "video"},
                source_type="video", confidence=0.7,
            )
        extra = ""
        if not video_mod.ffmpeg_available():
            extra = "（本机没装 ffmpeg，所以抽不了帧、也看不到画面）"
        elif not frames and float(info.data.get("metadata", {}).get("duration") or 0) > video_mod.MEDIUM_MAX_SECONDS:
            extra = "（视频太长，按设定不自动逐帧分析）"
        degraded = f"用户发来{info.text}{extra}。你没看到画面内容，别假装看过。"
        return ToolResult(
            name="perception.video", ok=info.ok, error="" if info.ok else info.error, text=degraded,
            data={**info.data, "degraded_note": degraded}, source_type="video",
            confidence=info.confidence,
        )

    def _document(self, request: PerceptionRequest) -> ToolResult:
        return document_mod.parse(request.path)
