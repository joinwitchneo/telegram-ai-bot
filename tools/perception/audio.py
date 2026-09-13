"""语音/音频：元数据本地读，转 16k 单声道 wav 需要 ffmpeg（有就用）。"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from tools.core.result import ToolResult
from tools.core.schema import which


def ffmpeg_available() -> bool:
    return bool(which("ffmpeg"))


def to_wav(path: str | Path, *, out_dir: str | Path | None = None) -> str:
    """转成 whisper 需要的 16kHz 单声道 wav；没有 ffmpeg 返回空字符串。"""
    if not ffmpeg_available():
        return ""
    source = Path(path)
    if not source.is_file():
        return ""
    target_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="xiyan_audio_"))
    target_dir.mkdir(parents=True, exist_ok=True)
    output = target_dir / (source.stem + ".wav")
    try:
        subprocess.run(
            [which("ffmpeg"), "-i", str(source), "-ar", "16000", "-ac", "1", "-y", str(output)],
            capture_output=True, timeout=120,
        )
    except Exception:  # noqa: BLE001
        return ""
    return str(output) if output.is_file() and output.stat().st_size > 0 else ""


def build_result(path: str | Path, *, telegram_meta: dict | None = None) -> ToolResult:
    meta = dict(telegram_meta or {})
    duration = float(meta.get("duration") or 0)
    hint = "（这条语音不短，像是认真说的）" if duration >= 20 else ""
    describe = f"{duration:.0f} 秒的语音" if duration else "一段语音"
    wav = to_wav(path) if Path(path).is_file() else ""
    meta["wav"] = wav
    meta["ffmpeg"] = ffmpeg_available()
    return ToolResult(
        name="audio", ok=True, text=f"{describe}{hint}",
        data={"metadata": meta, "source_type": "voice", "path": str(path)},
        source_type="voice", confidence=1.0 if duration else 0.5,
    )
