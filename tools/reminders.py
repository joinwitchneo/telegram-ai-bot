"""Reminder service: store, schedule, and in-character delivery."""

from __future__ import annotations

import datetime
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from bot import Bot

WATER_INTERVAL_HOURS = 2
WATER_DAY_START = 9
WATER_DAY_END = 21
WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
REMINDER_PREFIXES = ("记一下", "记住", "提醒我", "别忘了", "帮我记", "记得")


def _extract_when(text: str, now: datetime.datetime) -> datetime.datetime | None:
    match = re.search(r"(\d+)\s*分钟后", text)
    if match:
        return now + datetime.timedelta(minutes=int(match.group(1)))
    match = re.search(r"(\d+)\s*小时后", text)
    if match:
        return now + datetime.timedelta(hours=int(match.group(1)))

    day_delta = 0
    found_day = False
    if "后天" in text:
        day_delta = 2
        found_day = True
    elif "明天" in text:
        day_delta = 1
        found_day = True
    elif "今天" in text:
        day_delta = 0
        found_day = True
    else:
        match = re.search(r"(?:周|星期)([一二三四五六日天])", text)
        if match:
            target = WEEKDAY_MAP[match.group(1)]
            day_delta = (target - now.weekday()) % 7
            if day_delta == 0:
                day_delta = 7
            found_day = True

    hour: int | None = None
    minute = 0
    found_time = False
    match = re.search(r"(\d{1,2})\s*[:：点]\s*(\d{1,2})\s*分?", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        found_time = True
    else:
        match = re.search(r"(上午|早上|中午|下午|晚上|傍晚)?\s*(\d{1,2})\s*点\s*(?:(\d{1,2})\s*分)?", text)
        if match:
            hour = int(match.group(2))
            minute = int(match.group(3) or 0)
            found_time = True
            period = match.group(1) or ""
            if period in ("下午", "晚上", "傍晚") and hour < 12:
                hour += 12
            elif period == "中午" and hour < 12:
                hour += 12

    if not found_day and not found_time:
        return None
    base_day = now.date() + datetime.timedelta(days=day_delta)
    if hour is None:
        return datetime.datetime.combine(base_day, datetime.time(9, 0))
    result = datetime.datetime.combine(base_day, datetime.time(hour % 24, minute))
    if day_delta == 0 and result <= now:
        result += datetime.timedelta(days=1)
    return result


def _strip_time_expr(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r"\d+\s*分钟后", "", cleaned)
    cleaned = re.sub(r"\d+\s*小时后", "", cleaned)
    cleaned = re.sub(r"(?:周|星期)[一二三四五六日天]", "", cleaned)
    cleaned = re.sub(r"(?:今天|明天|后天)", "", cleaned)
    cleaned = re.sub(r"提醒我", "", cleaned)
    cleaned = re.sub(r"(上午|早上|中午|下午|晚上|傍晚)?\s*\d{1,2}\s*[:：点]\s*\d{1,2}\s*分?", "", cleaned)
    cleaned = re.sub(r"(上午|早上|中午|下午|晚上|傍晚)?\s*\d{1,2}\s*点\s*(?:\d{1,2}\s*分)?", "", cleaned)
    cleaned = re.sub(r"[\s，,、。.]+", " ", cleaned).strip(" ，,、。")
    return cleaned or text.strip()


def parse_reminder(text: str, now: datetime.datetime | None = None) -> dict | None:
    """Deterministic parser for common Chinese reminder sentences."""
    now = now or datetime.datetime.now()
    cleaned = text.strip()
    for prefix in REMINDER_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    if not cleaned:
        return None
    repeat = ""
    if "喝水" in cleaned and ("每" in cleaned or "循环" in cleaned):
        repeat = "water"
    when = _extract_when(cleaned, now)
    if when is None:
        if repeat == "water":
            return {"when": None, "content": _strip_time_expr(cleaned) or cleaned, "repeat": "water"}
        return None
    content = _strip_time_expr(cleaned)
    if not content:
        content = cleaned
    return {"when": when, "content": content, "repeat": repeat}


class ReminderStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: list[dict] = []
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    self.data = loaded
            except (json.JSONDecodeError, OSError):
                self.data = []

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=2)
        try:
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("could not save reminders: %s", exc)

    def add(self, item: dict) -> int:
        with self._lock:
            item["id"] = max((r.get("id", 0) for r in self.data), default=0) + 1
            self.data.append(item)
        self.save()
        return item["id"]

    def remove(self, reminder_id: int) -> bool:
        with self._lock:
            before = len(self.data)
            self.data = [r for r in self.data if r.get("id") != reminder_id]
            removed = len(self.data) != before
        if removed:
            self.save()
        return removed

    def all(self) -> list[dict]:
        with self._lock:
            return list(self.data)

    def due(self, now: datetime.datetime) -> list[dict]:
        with self._lock:
            return [r for r in self.data if r.get("due") and datetime.datetime.fromisoformat(r["due"]) <= now]

    def remove_ids(self, ids: set[int]) -> None:
        with self._lock:
            self.data = [r for r in self.data if r.get("id") not in ids]
        self.save()


def parse_reminder_with_llm(
    text: str,
    llm_func: Callable[[str], str],
    now: datetime.datetime | None = None,
) -> dict | None:
    """Use the LLM to extract {when, content, repeat} from a Chinese reminder sentence."""
    now = now or datetime.datetime.now()
    prompt = (
        f"当前时间是 {now.strftime('%Y-%m-%d %H:%M')}（星期{'一二三四五六日'[now.weekday()]}）。\n"
        f"用户说：{text}\n"
        "请提取提醒时间和提醒内容。不要思考过程，不要解释，直接输出 JSON，格式：\n"
        '{"when": "YYYY-MM-DD HH:MM", "content": "提醒内容", "repeat": ""}\n'
        "规则：when 按当前时间推算成具体日期时间；如果时间无法确定，when 输出空字符串；"
        "repeat 只有用户明确要求周期性（比如每2小时喝水）时才填 'water'，否则为空。"
    )
    try:
        raw = llm_func(prompt)
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return None
        data = json.loads(raw[start : end + 1])
        when_text = (data.get("when") or "").strip()
        content = (data.get("content") or "").strip()
        repeat = (data.get("repeat") or "").strip()
        if not content:
            return None
        parsed = datetime.datetime.fromisoformat(when_text) if when_text else None
        return {"when": parsed, "content": content, "repeat": repeat}
    except Exception as exc:  # noqa: BLE001
        logging.warning("reminder parse failed: %s", exc)
        return None


def next_water_time(now: datetime.datetime | None = None) -> datetime.datetime:
    now = now or datetime.datetime.now()
    candidate = now.replace(second=0, microsecond=0) + datetime.timedelta(hours=WATER_INTERVAL_HOURS)
    if candidate.hour < WATER_DAY_START:
        candidate = candidate.replace(hour=WATER_DAY_START, minute=0)
    elif candidate.hour >= WATER_DAY_END:
        candidate = (candidate + datetime.timedelta(days=1)).replace(hour=WATER_DAY_START, minute=0)
    return candidate


class ReminderThread(threading.Thread):
    def __init__(self, store: ReminderStore, bot: "Bot") -> None:
        super().__init__(daemon=True, name="reminder-thread")
        self.store = store
        self.bot = bot

    def run(self) -> None:
        logging.info("reminder thread started (%d stored)", len(self.store.all()))
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                logging.error("reminder tick failed: %s", exc)
            time.sleep(20)

    def _tick(self) -> None:
        now = datetime.datetime.now()
        due = self.store.due(now)
        done_ids: set[int] = set()
        reschedule: list[dict] = []
        for reminder in due:
            reminder_id = reminder.get("id")
            try:
                self.bot.send_reminder(reminder)
            except Exception as exc:  # noqa: BLE001
                logging.error("reminder %s delivery failed: %s", reminder_id, exc)
                continue
            if reminder.get("repeat") == "water":
                reminder["due"] = next_water_time(now).isoformat()
                reschedule.append(reminder)
            else:
                done_ids.add(reminder_id)
        if done_ids:
            self.store.remove_ids(done_ids)
        if reschedule:
            self.store.save()
