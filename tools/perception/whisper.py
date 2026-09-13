"""本地语音识别：whisper.cpp（完全离线，0 AI API）。"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from tools.core.result import ToolResult
from tools.core.schema import which

# 注意：不要用裸 "main"——Windows 的 main.cpl（鼠标设置）会被 shutil.which 命中
BINARIES = ("whisper-cli", "whisper-cli.exe", "whisper", "whisper.exe", "main.exe", "whisper-cpp.exe")


def find_binary() -> str:
    for name in BINARIES:
        found = which(name)
        if found:
            return found
    return ""


def available(model_path: str = "") -> bool:
    if not find_binary():
        return False
    return not model_path or Path(model_path).is_file()


def transcribe(
    wav_path: str | Path,
    *,
    model_path: str = "",
    language: str = "zh",
    prompt: str = "",
    timeout: float = 120.0,
) -> ToolResult:
    binary = find_binary()
    if not binary:
        return ToolResult.failure("whisper", "本机没装 whisper.cpp（whisper-cli / main）")
    if not model_path or not Path(model_path).is_file():
        return ToolResult.failure("whisper", "没有配置 Whisper 模型文件（WHISPER_MODEL_PATH）")
    path = Path(wav_path)
    if not path.is_file():
        return ToolResult.failure("whisper", "音频文件不存在")
    base = [binary, "-m", str(model_path), "-f", str(path), "-l", language, "-nt"]
    # 带上提示词能让"夕颜"这类专有名词识别得更准；老版本不支持就自动去掉重试
    attempts = [base + ["--prompt", prompt]] if prompt else []
    attempts.append(base)
    result = None
    for command in attempts:
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=timeout, text=True,
                encoding="utf-8", errors="replace",
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("whisper 调用失败：%s", exc)
            return ToolResult.failure("whisper", f"whisper 调用失败：{exc}")
        if result.returncode == 0 and (result.stdout or "").strip():
            break
    if result is None:
        return ToolResult.failure("whisper", "whisper 没有输出")
    text = (result.stdout or "").strip()
    if not text:
        return ToolResult.failure("whisper", "没听出内容")
    return ToolResult(
        name="whisper", ok=True, text=text,
        data={"text": text, "source_type": "voice", "model": Path(model_path).name},
        source_type="voice", confidence=0.75,
    )
