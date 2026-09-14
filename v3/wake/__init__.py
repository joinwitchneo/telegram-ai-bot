"""自主唤醒模块（Phase 6 主动唤醒专项）。

    Scheduler → （没有排队任务时）WakeManager 判断"要不要醒来" → 入队 → Scheduler 正常执行
             → WakeQueue → cycle_runner → LifeCycle

铁律：
  - 唤醒判断是纯 Python，绝不调模型；
  - 唤醒 ≠ 聊天：醒来的结果是 NO_ACTION / JOURNAL / MESSAGE 之一；
  - 时间只是"醒来看看"的机会，不能直接变成消息；
  - 所有唤醒都走 wake_queue 这一个入口。
"""

from v3.wake.wake_reason import (  # noqa: F401
    AUTONOMOUS_REASONS,
    NEVER_MESSAGE_REASONS,
    REASONS,
    can_lead_to_message,
    normalize_reason,
)
from v3.wake.wake_policy import WakePolicy  # noqa: F401
from v3.wake.wake_manager import WakeCandidate, WakeDecision, WakeManager  # noqa: F401
from v3.wake.wake_result import WakeLog, WakeResult  # noqa: F401
