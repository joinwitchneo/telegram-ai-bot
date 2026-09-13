"""Tool Schema（Phase 7）：每个工具都必须声明自己是哪种成本等级。

execution_mode：
    LOCAL        完全在电脑上跑（0 API、0 网络）
    FREE_NETWORK 要联网但不用付费 AI API（HTTP / 网页 / 公开接口）
    PAID_API     真要花 AI API 的钱（只有主聊天模型之类）

level（Tool Policy 的三层）：
    L0 → Python 直接处理，0 token
    L1 → 本地模型处理（本地视觉 / OCR / Whisper / TTS）
    L2 → 主 LLM 才需要（自然语言理解、角色表达、复杂推理）
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

EXECUTION_MODES = ("LOCAL", "FREE_NETWORK", "PAID_API")
LEVELS = ("L0", "L1", "L2")

# 便携版二进制目录（没有管理员权限时，ffmpeg / whisper / ollama 都放这里）
EXTRA_BIN_DIRS: list[str] = []


def register_bin_dir(path: str) -> None:
    """把一个目录加进"找可执行文件"的搜索范围（由 config 的 TOOL_BIN_DIR 提供）。"""
    text = str(path or "").strip()
    if text and text not in EXTRA_BIN_DIRS:
        EXTRA_BIN_DIRS.insert(0, text)


def which(binary: str) -> str:
    """本机有没有这个外部程序（0 成本探测）。"""
    if not binary:
        return ""
    for directory in EXTRA_BIN_DIRS:
        try:
            candidate = Path(directory) / binary
            if candidate.is_file():
                return str(candidate)
            for suffix in (".exe", ".cmd", ".bat"):
                with_suffix = Path(directory) / f"{binary}{suffix}"
                if with_suffix.is_file():
                    return str(with_suffix)
        except OSError:
            continue
    return shutil.which(binary) or ""


@dataclass
class ToolSpec:
    name: str
    mode: str = "LOCAL"
    level: str = "L0"
    description: str = ""
    requires: tuple = ()          # 需要的可执行程序 / 服务
    output_kind: str = "text"     # text / perception / media / json
    cache_ttl: float = 0.0        # 秒；0 表示不缓存
    keywords: tuple = ()          # 规则命中的关键词（L0 分流用）
    cost_note: str = ""
    configured: bool = True       # 需要额外配置（模型路径、贴纸目录）时置 False

    def __post_init__(self) -> None:
        if self.mode not in EXECUTION_MODES:
            raise ValueError(f"未知成本等级：{self.mode}")
        if self.level not in LEVELS:
            raise ValueError(f"未知层级：{self.level}")

    def missing_requirements(self) -> list[str]:
        return [binary for binary in self.requires if not which(binary)]

    def available(self) -> bool:
        return bool(self.configured) and not self.missing_requirements()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mode": self.mode,
            "level": self.level,
            "description": self.description,
            "requires": list(self.requires),
            "output_kind": self.output_kind,
            "cache_ttl": self.cache_ttl,
            "available": self.available(),
            "missing": self.missing_requirements(),
            "configured": self.configured,
        }
