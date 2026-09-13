"""本地 TTS：能把文字变成语音就发语音，做不到就老实说做不到。"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from tools.core.result import ToolResult
from tools.core.schema import which


class LocalTTS:
    def __init__(self, *, piper_model: str = "", enabled: bool = True) -> None:
        self.piper_model = piper_model or ""
        self.enabled = bool(enabled)

    def provider(self) -> str:
        if not self.enabled:
            return ""
        if which("piper") and self.piper_model and Path(self.piper_model).is_file():
            return "piper"
        if which("espeak-ng") or which("espeak"):
            return "espeak"
        return ""

    def available(self) -> bool:
        return bool(self.provider())

    def synthesize(self, text: str, *, out_path: str | Path | None = None) -> ToolResult:
        provider = self.provider()
        if not provider:
            return ToolResult.failure("voice_reply", "本机没有可用的本地 TTS（没装 piper / espeak）")
        target = Path(out_path) if out_path else Path(tempfile.mkdtemp(prefix="xiyan_tts_")) / "reply.mp3"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if provider == "piper":
                process = subprocess.run(
                    [which("piper"), "--model", self.piper_model, "--output_file", str(target)],
                    input=str(text), capture_output=True, timeout=90, text=True, encoding="utf-8",
                )
            else:
                binary = which("espeak-ng") or which("espeak")
                process = subprocess.run(
                    [binary, "-v", "zh", "-w", str(target), str(text)],
                    capture_output=True, timeout=90, text=True, encoding="utf-8",
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure("voice_reply", f"TTS 调用失败：{exc}")
        if process.returncode != 0 or not target.is_file():
            return ToolResult.failure("voice_reply", "TTS 没有生成音频")
        return ToolResult(
            name="voice_reply", ok=True, text="（语音已生成）",
            data={"path": str(target), "provider": provider, "source_type": "voice_reply"},
            source_type="voice_reply", confidence=1.0,
        )
