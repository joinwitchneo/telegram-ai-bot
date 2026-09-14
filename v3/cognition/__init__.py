"""认知层（Phase 6 §十四/§十五/§十六）。

Core 只认识 CognitiveProvider 接口；DeepSeek 只是第一个实现。
Provider 的产出永远是 **Proposal**，不是执行命令。
"""

from v3.cognition.base import (  # noqa: F401
    INTENTION_TYPES,
    CognitiveContext,
    CognitiveProvider,
    CognitiveResult,
    Intention,
    MockCognitiveProvider,
)
