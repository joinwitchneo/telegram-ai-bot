"""Sticker 表达层（S1：基础设施）。

边界（重要）：
    这是"表达器官"，不是人格、不是记忆、不是独立 Agent。
    它不拥有自己的情绪/性格/说话方式/LLM；它只负责：
        收录贴纸 -> 建立索引 -> 按意图确定性排序 -> 交给统一 Renderer 发送。
    LLM 只说"我想用什么感觉表达"，具体用哪个 file_id 永远由 Python 决定。
"""

from sticker.models import StickerRecord  # noqa: F401
from sticker.store import StickerStore  # noqa: F401
from sticker.index import StickerIndex  # noqa: F401
from sticker.engine import StickerEngine, StickerIntent, StickerDecision  # noqa: F401
from sticker.history import StickerHistory  # noqa: F401
from sticker.sender import StickerSender  # noqa: F401
