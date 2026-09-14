"""Reminder service: store, schedule, and in-character delivery."""

from __future__ import annotations

from core.atomic_io import atomic_write_text

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

# 口语里的时段词。长词必须排在短词前面（"晚上" 要在 "晚" 之前），否则 "晚" 会先吃掉一半。
PERIOD_RE = "凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|夜晚|晚|早"
PM_PERIODS = ("下午", "傍晚", "晚上", "晚", "夜里", "夜晚")
# 小时可以写阿拉伯数字，也可以写中文数字（"三点"、"十一点"）
HOUR_RE = "十一|十二|一|二|两|三|四|五|六|七|八|九|十|\\d{1,2}"
CN_HOURS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
            "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
MINUTE_WORDS = {"半": 30, "一刻": 15, "三刻": 45}
# 只说了"周六晚上"而没写几点时，按时段的常识时间，而不是一律早上 9 点
PERIOD_DEFAULT_HOUR = (("凌晨", 6), ("清晨", 7), ("早上", 8), ("早晨", 8), ("上午", 10),
                       ("中午", 12), ("下午", 15), ("傍晚", 18), ("晚上", 20),
                       ("夜里", 21), ("夜晚", 21), ("晚", 20))
# 只用来清理"提醒内容"的时段词：不含光杆的"晚/早"，避免把"早点睡"削成"点睡"
LEADING_PERIOD_RE = "凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|夜晚"


def _to_hour(token: str) -> int | None:
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    return CN_HOURS.get(token)


def _extract_day(text: str, now: datetime.datetime) -> tuple[int, bool]:
    if "大后天" in text:
        return 3, True
    if "后天" in text:
        return 2, True
    if any(word in text for word in ("明天", "明早", "明晚", "明儿")):
        return 1, True
    if any(word in text for word in ("今天", "今晚", "今早", "今儿")):
        return 0, True
    match = re.search(r"(?:周|星期)([一二三四五六日天])", text)
    if match:
        target = WEEKDAY_MAP[match.group(1)]
        day_delta = (target - now.weekday()) % 7
        return (7 if day_delta == 0 else day_delta), True
    return 0, False


def _extract_clock(text: str) -> tuple[str, int | None, int]:
    """返回 (时段词, 小时, 分钟)。小时为 None 表示句子里没写具体几点。"""
    match = re.search(rf"({PERIOD_RE})?\s*({HOUR_RE})\s*[:：]\s*(\d{{1,2}})\s*分?", text)
    if match:
        period = match.group(1) or ""
        hour = _to_hour(match.group(2))
        minute = int(match.group(3))
    else:
        match = re.search(rf"({PERIOD_RE})?\s*({HOUR_RE})\s*点\s*(半|一刻|三刻|(\d{{1,2}})\s*分?)?", text)
        if not match:
            return "", None, 0
        period = match.group(1) or ""
        hour = _to_hour(match.group(2))
        tail = match.group(3) or ""
        if tail in MINUTE_WORDS:
            minute = MINUTE_WORDS[tail]
        elif tail:
            minute = int(re.sub(r"\D", "", tail) or 0)
        else:
            minute = 0

    if hour is None:
        return period, None, 0
    if period in PM_PERIODS:
        # 下午/晚上 3 点 = 15 点；晚上 12 点 = 0 点
        if hour == 12:
            hour = 0
        elif hour < 12:
            hour += 12
    elif period == "中午" and hour < 12:
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0
    return period, hour, minute


def _extract_when(text: str, now: datetime.datetime) -> datetime.datetime | None:
    match = re.search(r"(\d+)\s*分钟后", text)
    if match:
        return now + datetime.timedelta(minutes=int(match.group(1)))
    match = re.search(r"(\d+)\s*小时后", text)
    if match:
        return now + datetime.timedelta(hours=int(match.group(1)))

    day_delta, found_day = _extract_day(text, now)
    _period, hour, minute = _extract_clock(text)
    found_time = hour is not None

    if not found_day and not found_time:
        return None
    base_day = now.date() + datetime.timedelta(days=day_delta)
    if hour is None:
        default_hour = 9
        for word, value in PERIOD_DEFAULT_HOUR:
            if word in text:
                default_hour = value
                break
        return datetime.datetime.combine(base_day, datetime.time(default_hour, 0))
    result = datetime.datetime.combine(base_day, datetime.time(hour % 24, minute))
    if day_delta == 0 and result <= now:
        result += datetime.timedelta(days=1)
    return result


def _strip_time_expr(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r"\d+\s*分钟后", "", cleaned)
    cleaned = re.sub(r"\d+\s*小时后", "", cleaned)
    # 频次词先摘掉，否则会剩下一个孤零零的"每"
    cleaned = re.sub(r"每\s*(?:晚|夜|天|日|早|周|星期|次|回|个月)", "", cleaned)
    cleaned = re.sub(r"[下这本上]?\s*(?:周|星期)[一二三四五六日天]", "", cleaned)
    cleaned = re.sub(r"(?:大后天|后天|明天|明早|明晚|明儿|今天|今晚|今早|今儿)", "", cleaned)
    cleaned = re.sub(r"(?:提醒我|提醒|叫我|记得|别忘了|帮我记|记一下|记住)", "", cleaned)
    cleaned = re.sub(rf"(?:{PERIOD_RE})?\s*(?:{HOUR_RE})\s*[:：]\s*\d{{1,2}}\s*分?", "", cleaned)
    cleaned = re.sub(rf"(?:{PERIOD_RE})?\s*(?:{HOUR_RE})\s*点\s*(?:半|一刻|三刻|\d{{1,2}}\s*分?)?", "", cleaned)
    # 只说了"周六晚上"没写几点时，时段词在内容里是多余的
    cleaned = re.sub(rf"^\s*(?:{LEADING_PERIOD_RE})", "", cleaned)
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
            atomic_write_text(self.path, snapshot, encoding="utf-8")
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
