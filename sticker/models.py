"""Sticker 数据模型。

关键区分（Telegram 语义）：
    file_unique_id —— "这个贴纸是谁"，稳定，用作去重主键
    file_id        —— "现在怎么发送"，可能变化，每次收录都刷新
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


@dataclass
class StickerRecord:
    file_unique_id: str
    file_id: str = ""
    emoji: str = ""
    set_name: str = ""
    type: str = "regular"          # regular / mask / custom_emoji
    is_animated: bool = False
    is_video: bool = False
    width: int = 0
    height: int = 0
    file_size: int = 0
    tags: list = field(default_factory=list)      # 人工/后续(Vision)打标
    visual_description: str = ""                  # S2 才填，S1 一律留空
    source: str = "user"                          # user / set / manual
    seen_count: int = 1
    first_seen_at: str = field(default_factory=_now)
    last_seen_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StickerRecord":
        known = {field_name for field_name in cls.__dataclass_fields__}
        payload = {key: value for key, value in (data or {}).items() if key in known}
        return cls(**payload)

    @classmethod
    def from_telegram(cls, sticker: dict, *, source: str = "user") -> "StickerRecord":
        """从 Telegram update 里的 sticker 对象建记录（0 网络、0 LLM）。"""
        sticker = sticker or {}
        return cls(
            file_unique_id=str(sticker.get("file_unique_id") or sticker.get("file_id") or ""),
            file_id=str(sticker.get("file_id") or ""),
            emoji=str(sticker.get("emoji") or ""),
            set_name=str(sticker.get("set_name") or ""),
            type=str(sticker.get("type") or "regular"),
            is_animated=bool(sticker.get("is_animated")),
            is_video=bool(sticker.get("is_video")),
            width=int(sticker.get("width") or 0),
            height=int(sticker.get("height") or 0),
            file_size=int(sticker.get("file_size") or 0),
            source=source,
        )
