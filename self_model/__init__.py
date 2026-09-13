"""Self Model（Phase 8）：夕颜目前怎么理解"我自己"。

边界（很重要）：
    Memory       记录"发生过什么"
    Emotion      记录"当时什么状态"
    Relationship 记录"我和用户之间变了什么"
    Character Life 记录"最近在进行什么"
    Self Model   只记录"这些经历让她怎么看待自己"

这里不放人格台词，也不放标准答案；它只提供"她此刻的自我理解状态"。
"""

from self_model.store import SelfModelStore, load_self_model  # noqa: F401
from self_model.rules import CORE_FACTS, ANTI_RULES, check_core_conflict  # noqa: F401
