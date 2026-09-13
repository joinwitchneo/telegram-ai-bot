"""表情包：本地贴纸文件 + 情绪标签，0 API。

配置在 stickers.json：{"开心": ["data/stickers/happy_01.webp"], ...}
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from tools.core.result import ToolResult

TAG_ALIASES = {
    "开心": ("开心", "高兴", "joy", "happy"),
    "生气": ("生气", "angry", "annoyed"),
    "害羞": ("害羞", "shy", "embarrassed"),
    "无语": ("无语", "无奈", "speechless"),
    "委屈": ("委屈", "sad", "低落"),
    "得意": ("得意", "smug", "proud"),
    "疑问": ("疑问", "问号", "confused"),
}


class StickerBook:
    def __init__(self, path: Path | None = None, *, max_recent: int = 5) -> None:
        self.path = path
        self.max_recent = max(1, int(max_recent))
        self.tags: dict[str, list[str]] = {}
        self._recent: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        for tag, items in data.items():
            if isinstance(items, list):
                self.tags[str(tag)] = [str(item) for item in items if str(item).strip()]
            elif isinstance(items, str) and items.strip():
                self.tags[str(tag)] = [items.strip()]

    def available(self) -> bool:
        return bool(self.tags)

    def count(self) -> int:
        return sum(len(items) for items in self.tags.values())

    def resolve_tag(self, tag: str) -> str:
        raw = (tag or "").strip()
        if raw in self.tags:
            return raw
        for canonical, aliases in TAG_ALIASES.items():
            if raw in aliases or any(alias in raw for alias in aliases):
                if canonical in self.tags:
                    return canonical
        return ""

    def pick(self, tag: str) -> ToolResult:
        if not self.available():
            return ToolResult.failure("sticker", "还没有准备表情包（stickers.json 是空的）")
        resolved = self.resolve_tag(tag) or next(iter(self.tags))
        candidates = [item for item in self.tags[resolved] if item not in self._recent] or list(self.tags[resolved])
        chosen = random.choice(candidates)
        self._recent.append(chosen)
        self._recent = self._recent[-self.max_recent :]
        return ToolResult(
            name="sticker", ok=True, text=f"（发了一个「{resolved}」的表情包）",
            data={"tag": resolved, "path": chosen, "source_type": "sticker"},
            source_type="sticker", confidence=1.0,
        )
