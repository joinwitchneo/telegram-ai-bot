"""夕颜 V3 —— 自主生命系统（MVP：Phase 0–5）。

边界（全程置顶）：
    LLM 负责提出，Python 负责裁决。
    Thought 可以自由产生，但 Preference 必须有证据（MVP 不做 Preference）。
    Desires != Actions；WakeIntent != 真实调度。
    V3 是叠加层：不写 V2 的 Memory / Emotion / Relationship / Self-Model 核心算法。
    MVP 不向用户发送任何消息（只写 inner_journal）。
"""
