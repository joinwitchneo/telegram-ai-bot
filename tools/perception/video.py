"""视频：元数据优先用 Telegram 自带的信息（0 依赖），抽帧需要 ffmpeg（有就用）。"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from tools.core.result import ToolResult
from tools.core.schema import which

SHORT_MAX_SECONDS = 30
MEDIUM_MAX_SECONDS = 300     # 5 分钟


def ffmpeg_available() -> bool:
    return bool(which("ffmpeg") and which("ffprobe"))


def probe(path: str | Path) -> dict:
    """ffprobe 读时长/分辨率；ffprobe 不在就返回空 dict。"""
    if not which("ffprobe"):
        return {}
    try:
        result = subprocess.run(
            [which("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,duration",
             "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, timeout=30, text=True, encoding="utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001
        return {}
    import json

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    stream = (data.get("streams") or [{}])[0]
    duration = float(stream.get("duration") or (data.get("format") or {}).get("duration") or 0)
    return {
        "width": stream.get("width", 0),
        "height": stream.get("height", 0),
        "fps": stream.get("r_frame_rate", ""),
        "duration": round(duration, 2),
    }


def frame_times(duration: float, *, max_frames: int = 6) -> list[float]:
    """按策略抽帧：短视频均匀抽，中长视频少抽，超 5 分钟默认不自动处理。"""
    if duration <= 0:
        return []
    if duration <= SHORT_MAX_SECONDS:
        count = max(2, min(max_frames, int(duration // 2) or 2))
        step = duration / (count + 1)
        return [round(step * (index + 1), 2) for index in range(count)]
    if duration <= MEDIUM_MAX_SECONDS:
        return [round(duration * ratio, 2) for ratio in (0.1, 0.35, 0.65, 0.9)]
    return []


def extract_frames(path: str | Path, *, duration: float = 0.0, out_dir: str | Path | None = None) -> list[str]:
    if not ffmpeg_available():
        return []
    times = frame_times(duration)
    if not times:
        return []
    target_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="xiyan_frames_"))
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for index, moment in enumerate(times):
        output = target_dir / f"frame_{index:02d}.jpg"
        try:
            subprocess.run(
                [which("ffmpeg"), "-ss", str(moment), "-i", str(path), "-frames:v", "1",
                 "-q:v", "4", "-y", str(output)],
                capture_output=True, timeout=60,
            )
        except Exception:  # noqa: BLE001
            continue
        if output.is_file() and output.stat().st_size > 0:
            saved.append(str(output))
    return saved


def describe_metadata(meta: dict) -> str:
    duration = float(meta.get("duration") or 0)
    parts = []
    if duration:
        parts.append(f"{duration:.1f} 秒")
    width, height = meta.get("width") or 0, meta.get("height") or 0
    if width and height:
        parts.append(f"{width}×{height}")
    if meta.get("fps"):
        parts.append(str(meta["fps"]))
    return "、".join(parts)


def build_result(path: str | Path, *, telegram_meta: dict | None = None) -> ToolResult:
    """视频第一版：元数据一定给，抽帧有 ffmpeg 才做。"""
    file_path = Path(path)
    meta = dict(telegram_meta or {})
    probed = probe(file_path) if file_path.is_file() else {}
    meta.update({key: value for key, value in probed.items() if value})
    duration = float(meta.get("duration") or 0)
    need_frames = duration and duration <= MEDIUM_MAX_SECONDS
    frames = extract_frames(file_path, duration=duration) if need_frames else []
    describe = describe_metadata(meta) or (f"{file_path.stat().st_size // 1024} KB" if file_path.is_file() else "")
    if duration > MEDIUM_MAX_SECONDS:
        summary = f"一段较长的视频（{describe}）——太长，默认不逐帧分析，需要时你说一声"
    elif frames:
        summary = f"一段视频（{describe}），已抽了 {len(frames)} 帧准备看"
    else:
        summary = f"一段视频（{describe}）"
    return ToolResult(
        name="video", ok=True, text=summary,
        data={"metadata": meta, "frames": frames, "source_type": "video", "path": str(file_path)},
        source_type="video", confidence=1.0 if meta else 0.5,
    )
