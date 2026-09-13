"""提醒工具：把已有的 ReminderStore 接进 Tool Layer（L0，0 token）。"""

from __future__ import annotations

from tools.core.result import ToolResult


def build(reminder_store, *, chat_id: int, text: str) -> ToolResult:
    if reminder_store is None:
        return ToolResult.failure("reminder", "提醒功能没接上")
    try:
        from tools.reminders import parse_reminder

        content, due = parse_reminder(text)
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure("reminder", f"没看懂这条提醒：{exc}")
    if not content:
        return ToolResult.failure("reminder", "没听出要提醒什么")
    try:
        record = reminder_store.add(
            chat_id=chat_id, content=content, due=due.isoformat(timespec="minutes") if due else ""
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure("reminder", f"提醒没存上：{exc}")
    when = due.strftime("%m-%d %H:%M") if due else "未定时间"
    return ToolResult(
        name="reminder", ok=True, text=f"记下了：{content}（{when}）",
        data={"content": content, "due": when, "record": record, "source_type": "reminder"},
        source_type="reminder", confidence=1.0,
    )
