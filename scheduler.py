"""Proactive greeting scheduler for the Telegram bot (threading-based)."""

from __future__ import annotations

import datetime
import logging
import random
import threading
import time
from typing import TYPE_CHECKING

import special_events

if TYPE_CHECKING:
    from bot import Bot


def kind_for_hour(hour: int) -> str:
    if hour < 11:
        return "morning"
    if hour < 15:
        return "noon"
    if hour < 21:
        return "evening"
    return "night"


class ProactiveScheduler(threading.Thread):
    """Sends in-character greetings at random daily checkpoints, idle check-ins,
    and a roughly weekly special event."""

    def __init__(
        self,
        bot: "Bot",
        fixed_times: list[str],
        daily_count: int,
        window: str,
        idle_hours: float,
        jitter_minutes: int,
        event_days: int,
        enabled: bool,
        meal_enabled: bool = True,
        meal_windows: dict[str, str] | None = None,
        active_window: str = "07:00-22:00",
    ) -> None:
        super().__init__(daemon=True, name="proactive-scheduler")
        self.bot = bot
        self.fixed_times = sorted(fixed_times)
        self.daily_count = max(0, daily_count)
        self.window = window
        self.idle_hours = idle_hours
        self.jitter = max(0, jitter_minutes)
        self.event_days = max(0, event_days)
        self.enabled = enabled
        self.meal_enabled = meal_enabled
        self.meal_windows = meal_windows or {
            "morning": "07:00-08:00",  # 早饭
            "noon": "11:30-12:30",     # 午饭
            "evening": "18:00-19:00",  # 晚饭
        }
        self.active_window = active_window
        self._sent_dates: dict[str, set[str]] = {}
        self._idle_dates: set[str] = set()
        self._booted = False
        self._last_event_day: str | None = None
        self._special_sent: set[str] = set()
        self._last_char_event_day: str | None = None
        self._last_short_event_day: str | None = None
        self._silence_flags: dict[str, bool] = {"busy": False, "leave": False}
        self._meal_slot_kinds: dict[str, dict[str, str]] = {}

    def run(self) -> None:
        if not self.enabled or not self.bot.proactive_chat_id:
            logging.info("proactive scheduler disabled (enabled=%s chat=%s)", self.enabled, self.bot.proactive_chat_id)
            return
        logging.info(
            "proactive scheduler started: chat=%s fixed=%s random=%s window=%s idle=%sh jitter=%smin event=%sd",
            self.bot.proactive_chat_id,
            self.fixed_times or "none",
            self.daily_count,
            self.window,
            self.idle_hours,
            self.jitter,
            self.event_days,
        )
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - keep scheduler alive
                logging.error("proactive tick failed: %s", exc)
            time.sleep(30)

    def _times_for(self, day: str) -> list[str]:
        """只保留饭点问候（早7-8/午12/晚6-7）与固定时间表；随机问候已取消。"""
        if self.fixed_times:
            return self.fixed_times
        rng = random.Random(f"tg-bot-{day}")
        slots: list[int] = []
        meal_kinds: dict[str, str] = {}
        if self.meal_enabled:
            for kind, window in self.meal_windows.items():
                bounds = self._window_minutes(window)
                if bounds:
                    minute = rng.randint(bounds[0], max(bounds[0], bounds[1] - 1))
                    slots.append(minute)
                    meal_kinds[f"{minute // 60:02d}:{minute % 60:02d}"] = kind
        self._meal_slot_kinds[day] = meal_kinds
        slots = sorted(set(slots))
        return [f"{minute // 60:02d}:{minute % 60:02d}" for minute in slots]

    def _kind_for_slot(self, day: str, slot: str) -> str:
        if self.fixed_times:
            return kind_for_hour(int(slot.split(":")[0]))
        meal_kinds = self._meal_slot_kinds.get(day, {})
        if slot in meal_kinds:
            return meal_kinds[slot]
        hour = int(slot.split(":")[0])
        return "afternoon" if hour < 21 else "night"

    def _meal_slot_valid(self, kind: str, now: datetime.datetime) -> bool:
        """饭点问候只在各自窗口内有效（前后留一点余量），窗口过了不补发。"""
        window = self.meal_windows.get(kind)
        if not window:
            return True
        bounds = self._window_minutes(window)
        if not bounds:
            return True
        minutes = now.hour * 60 + now.minute
        return bounds[0] - 5 <= minutes <= bounds[1] + 20

    @staticmethod
    def _window_minutes(window: str) -> tuple[int, int] | None:
        try:
            start_text, end_text = (part.strip() for part in window.split("-"))
            start_hour, start_minute = (int(part) for part in start_text.split(":"))
            end_hour, end_minute = (int(part) for part in end_text.split(":"))
        except ValueError:
            return None
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if end <= start:
            end = start + 1
        return start, end

    def _tick(self) -> None:
        if not self.bot.proactive_chat_id:
            return
        now = datetime.datetime.now()
        if not self._can_send_proactive(now):
            return  # 主动消息只在每天 07:00-22:00（北京时间）之间发
        day = now.strftime("%Y-%m-%d")
        sent = self._sent_dates.setdefault(day, set())
        due: list[tuple[datetime.datetime, str]] = []
        for slot in self._times_for(day):
            if slot in sent:
                continue
            try:
                hour, minute = (int(part) for part in slot.split(":"))
            except ValueError:
                logging.warning("bad proactive time slot: %s", slot)
                sent.add(slot)
                continue
            kind = self._kind_for_slot(day, slot)
            is_meal = (not self.fixed_times) and kind in self.meal_windows
            if is_meal and not self._meal_slot_valid(kind, now):
                sent.add(slot)  # 饭点窗口已过，当天不补发
                continue
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if not is_meal:
                target += datetime.timedelta(minutes=random.randint(0, self.jitter))
            if now >= target:
                due.append((target, slot))
        if due:
            due.sort(key=lambda item: item[0])
            if not self._booted:
                # 启动补发：只发最近一条，其余当天跳过，避免连发轰炸
                self._booted = True
                for _, slot in due[:-1]:
                    sent.add(slot)
                _, latest_slot = due[-1]
                self.bot.send_proactive(self._kind_for_slot(day, latest_slot))
                sent.add(latest_slot)
            else:
                for _, slot in due:
                    self.bot.send_proactive(self._kind_for_slot(day, slot))
                    sent.add(slot)

        if self.idle_hours > 0 and 9 <= now.hour <= 23 and day not in self._idle_dates:
            silent_hours = (time.time() - self.bot.last_activity) / 3600
            if silent_hours >= self.idle_hours:
                self.bot.send_proactive("idle")
                self._idle_dates.add(day)

        if self.event_days > 0:
            self._maybe_event(day)
        if getattr(self.bot, "char_event_days", 0) > 0:
            self._maybe_char_event(day)
        if getattr(self.bot, "short_event_days", 0) > 0:
            self._maybe_short_event(day)
        if (
            getattr(self.bot, "ask_busy_after_minutes", 0) > 0
            or getattr(self.bot, "leave_after_minutes", 0) > 0
        ):
            self._maybe_idle_followup()
        self._maybe_special_date(day)

    def _can_send_proactive(self, now: datetime.datetime) -> bool:
        bounds = self._window_minutes(self.active_window)
        if not bounds:
            return True
        minutes = now.hour * 60 + now.minute
        return bounds[0] <= minutes < bounds[1]

    def _maybe_idle_followup(self) -> None:
        """用户 10 分钟没回先问"在忙吗"，15 分钟没回再发结束聊天的人设语句。
        每个阶段各掷一次骰子，默认 25% 概率触发，不是每次都发。"""
        silent = (time.time() - self.bot.last_activity) / 60
        busy_min = int(getattr(self.bot, "ask_busy_after_minutes", 10) or 0)
        leave_min = int(getattr(self.bot, "leave_after_minutes", 15) or 0)
        prob = float(getattr(self.bot, "idle_followup_prob", 0.25) or 0.25)
        outgoing_age = (time.time() - getattr(self.bot, "last_outgoing_at", 0)) / 3600
        has_conversation = 0 < outgoing_age < 2
        if silent < max(busy_min, 1) or not has_conversation:
            # 用户还在聊天，或最近根本没有对话：重置标记，等待下一次沉默
            self._silence_flags = {"busy": False, "leave": False}
            return
        leave_sent = False
        if leave_min > 0 and silent >= leave_min and not self._silence_flags["leave"]:
            self._silence_flags["leave"] = True
            if random.random() < prob:
                self.bot.send_leave_event()
                leave_sent = True
                logging.info("leave event sent (silent %.0f min)", silent)
        if leave_sent:
            return
        if busy_min > 0 and silent >= busy_min and not self._silence_flags["busy"]:
            self._silence_flags["busy"] = True
            if random.random() < prob:
                self.bot.send_ask_busy()
                logging.info("ask-busy sent (silent %.0f min)", silent)

    def _maybe_event(self, day: str) -> None:
        if self._last_event_day is None:
            self._last_event_day = day
            return
        try:
            days_since = (datetime.date.today() - datetime.date.fromisoformat(self._last_event_day)).days
        except ValueError:
            days_since = self.event_days
        if days_since >= self.event_days + 3:
            roll = True
        elif days_since >= self.event_days - 2:
            roll = random.random() < 0.5
        else:
            return
        if roll:
            self.bot.send_proactive("event")
            self._last_event_day = day
            logging.info("proactive event sent on %s", day)

    def _maybe_special_date(self, day: str) -> None:
        """节日/角色生日/对方生日：短祝福 + 整段长事件（一天只发一次）。"""
        if day in self._special_sent:
            return
        if self._user_active():
            return  # 优先聊天：聊得正热就等会儿再发
        try:
            event = special_events.pick_top_event()
        except Exception as exc:  # noqa: BLE001
            logging.warning("special date lookup failed: %s", exc)
            return
        if not event:
            return
        self._special_sent.add(day)
        logging.info("special date event triggered: %s on %s", event.get("name"), day)
        threading.Thread(target=self.bot.send_special_event, args=(event,), daemon=True).start()

    def _maybe_char_event(self, day: str) -> None:
        """每隔几天随机来一次"世界书角色近况"事件，角色主动提起并表达惊讶/开心。"""
        interval = int(getattr(self.bot, "char_event_days", 4) or 0)
        if interval <= 0:
            return
        if self._last_char_event_day is None:
            self._last_char_event_day = day
            return
        if self._user_active():
            return  # 优先聊天：聊得正热就等会儿再发
        try:
            days_since = (datetime.date.today() - datetime.date.fromisoformat(self._last_char_event_day)).days
        except ValueError:
            days_since = interval
        if days_since >= interval + 3:
            roll = True
        elif days_since >= interval - 2:
            roll = random.random() < 0.5
        else:
            return
        if roll:
            self.bot.send_daily_event()
            self._last_char_event_day = day
            logging.info("random character event sent on %s", day)

    def _maybe_short_event(self, day: str) -> None:
        """短事件（每月 10~15 次）：角色主动找对方短聊，每天最多一次。"""
        interval = int(getattr(self.bot, "short_event_days", 2) or 0)
        if interval <= 0:
            return
        if self._last_short_event_day is None:
            self._last_short_event_day = day
            return
        if self._user_active():
            return
        try:
            days_since = (datetime.date.today() - datetime.date.fromisoformat(self._last_short_event_day)).days
        except ValueError:
            days_since = interval
        if days_since >= interval + 3:
            roll = True
        elif days_since >= interval - 1:
            roll = random.random() < 0.5
        else:
            return
        if roll:
            self.bot.send_short_event()
            self._last_short_event_day = day
            logging.info("short event sent on %s", day)

    def _user_active(self) -> bool:
        window = int(getattr(self.bot, "event_active_window", 90) or 0)
        if window <= 0:
            return False
        return (time.time() - self.bot.last_activity) < window * 60
