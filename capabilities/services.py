"""具体能力：视觉 / OCR / 语音 / 视频 / 天气 / 搜索。

每个能力只做三件事：探测自己能不能用、按需启动、返回结构化结果。
**不生成任何角色台词**——那永远属于统一回复链路。
"""

from __future__ import annotations

from capabilities.base import Capability, CapabilityResult


class VisionCapability(Capability):
    name = "vision"
    description = "本地视觉：看懂图片内容（Ollama，0 AI API）"
    persistent = True

    def __init__(self, vision, *, enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        self.vision = vision

    def installed(self) -> bool:
        return bool(self.vision and getattr(self.vision, "exe_path", ""))

    def health_check(self) -> bool:
        return bool(self.vision and getattr(self.vision, "enabled", False) and self.vision.list_models())

    def ensure_running(self) -> bool:
        return bool(self.vision and self.vision.ensure_running())

    def execute(self, *, path: str = "", prompt: str = "", **_ignored) -> CapabilityResult:
        result = self.vision.describe(path, prompt=prompt)
        if not result.ok:
            return CapabilityResult(success=False, capability=self.name, degraded=True, error=result.error)
        return CapabilityResult(
            success=True, capability=self.name, source_type="image",
            summary=result.text,
            data={"description": result.text, "model": self.vision.resolve_model()},
        )


class OcrCapability(Capability):
    name = "ocr"
    description = "本地 OCR：读图片里的文字（Windows 自带 / tesseract，0 AI API）"

    def __init__(self, *, enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        from tools.perception import ocr as ocr_mod

        self.ocr = ocr_mod

    def installed(self) -> bool:
        return bool(self.ocr.tesseract_available() or self.ocr.windows_ocr_available())

    def execute(self, *, path: str = "", **_ignored) -> CapabilityResult:
        result = self.ocr.recognize(path)
        if not result.ok:
            return CapabilityResult(success=False, capability=self.name, degraded=True, error=result.error)
        return CapabilityResult(
            success=True, capability=self.name, source_type="image",
            summary=result.text,
            data={"detected_text": result.text, "engine": result.data.get("engine", "")},
        )


class WhisperCapability(Capability):
    name = "whisper"
    description = "本地语音识别：whisper.cpp（0 AI API）"

    def __init__(self, *, model_path: str = "", language: str = "zh", prompt: str = "", enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        from tools.perception import whisper as whisper_mod

        self.whisper = whisper_mod
        self.model_path = model_path
        self.language = language
        self.prompt = prompt

    def installed(self) -> bool:
        return bool(self.whisper.find_binary())

    def health_check(self) -> bool:
        return bool(self.installed() and self.model_path)

    def execute(self, *, wav: str = "", path: str = "", **_ignored) -> CapabilityResult:
        target = wav or path
        result = self.whisper.transcribe(
            target, model_path=self.model_path, language=self.language, prompt=self.prompt
        )
        if not result.ok:
            return CapabilityResult(success=False, capability=self.name, degraded=True, error=result.error)
        return CapabilityResult(
            success=True, capability=self.name, source_type="voice",
            summary=result.text, data={"transcript": result.text},
        )


class VideoCapability(Capability):
    name = "video"
    description = "视频：元数据 + ffmpeg 抽帧（按需调用，不常驻）"

    def __init__(self, *, enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        from tools.perception import video as video_mod

        self.video = video_mod

    def installed(self) -> bool:
        return bool(self.video.ffmpeg_available())

    def execute(self, *, path: str = "", meta: dict | None = None, **_ignored) -> CapabilityResult:
        result = self.video.build_result(path, telegram_meta=meta or {})
        return CapabilityResult(
            success=bool(result.ok), capability=self.name, source_type="video",
            summary=result.text, data=dict(result.data),
            degraded=not bool(result.data.get("frames")),
        )


class WeatherCapability(Capability):
    name = "weather"
    description = "天气（免费 HTTP + 缓存，0 AI API）"

    def __init__(self, *, enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        from tools.world import weather as weather_mod

        self.weather = weather_mod

    def execute(self, *, city: str = "", latitude=None, longitude=None, **_ignored) -> CapabilityResult:
        result = self.weather.fetch(city, latitude=latitude, longitude=longitude)
        if not result.ok:
            return CapabilityResult(success=False, capability=self.name, degraded=True, error=result.error)
        current = result.data.get("current") or {}
        return CapabilityResult(
            success=True, capability=self.name, source_type="weather",
            summary=result.text,
            data={
                "temperature": current.get("temperature_2m"),
                "humidity": current.get("relative_humidity_2m"),
                "condition": current.get("weather_code"),
                "place": result.data.get("place", ""),
            },
        )


class SearchCapability(Capability):
    name = "search"
    description = "网页搜索（HTML 解析 + 相关性过滤，0 AI API）"

    def __init__(self, *, api_key: str = "", api_url: str = "", enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        from tools.world import web_search as search_mod

        self.search_mod = search_mod
        self.api_key = api_key
        self.api_url = api_url

    def execute(self, *, query: str = "", text: str = "", **_ignored) -> CapabilityResult:
        result = self.search_mod.search(
            query or text, api_key=self.api_key, api_url=self.api_url
        )
        if not result.ok:
            return CapabilityResult(success=False, capability=self.name, degraded=True, error=result.error)
        return CapabilityResult(
            success=True, capability=self.name, source_type="search",
            summary=result.text, data={"results": result.data.get("results", [])},
        )
