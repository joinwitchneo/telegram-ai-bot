"""主动消息调度（线程实现）。

四类主动开口：
1. 待办跟进：有当天到期/逾期的待办才催；
2. 每日汇总：早/晚各一次，没有待办就不发；
3. 作息关心：饭点问候 + 深夜赶人睡觉；
4. AI 牢骚：每周最多几次随机吐槽，只基于真实信息。

另外负责定期检查记忆书（补齐总结、压缩、重建）。
"""

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
    """按时间点发主动消息，并定期维护记忆书。"""

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
        summary_morning: str = "08:30",
        summary_night: str = "22:00",
        rant_per_week: int = 3,
        late_care_window: str = "23:30-01:00",
        memory_check_minutes: int = 60,
        memory_min_interval_hours: float = 6.0,
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
            "morning": "07:00-08:00",
            "noon": "11:30-12:30",
            "evening": "18:00-19:00",
        }
        self.active_window = active_window
        self.summary_morning = summary_morning
        self.summary_night = summary_night
        self.rant_per_week = max(0, rant_per_week)
        self.late_care_window = late_care_window
        self.memory_check_minutes = max(0, memory_check_minutes)
        self.memory_min_interval_hours = max(0.0, memory_min_interval_hours)

        self._sent_dates: dict[str, set[str]] = {}
        self._idle_dates: set[str] = set()
        self._booted = False
        self._special_sent: set[str] = set()
        self._last_short_event_day: str | None = None
        self._silence_flags: dict[str, bool] = {"busy": False, "leave": False}
        self._meal_slot_kinds: dict[str, dict[str, str]] = {}
        self._rant_days: list[str] = []
        self._rant_plan: dict[str, str] = {}
        self._summary_sent: set[str] = set()
        self._nudge_days: set[str] = set()
        self._late_care_days: set[str] = set()
        self._last_memory_check = 0.0

    def run(self) -> None:
        if not self.enabled or not self.bot.proactive_chat_id:
            logging.info(
                "proactive scheduler disabled (enabled=%s chat=%s)",
                self.enabled,
                self.bot.proactive_chat_id,
            )
            # 即使不主动发消息，记忆书维护也要跑
            if self.bot.proactive_chat_id:
                self._memory_loop()
            return
        logging.info(
            "proactive scheduler started: chat=%s fixed=%s random=%s window=%s idle=%sh jitter=%smin",
            self.bot.proactive_chat_id,
            self.fixed_times or "none",
            self.daily_count,
            self.window,
            self.idle_hours,
            self.jitter,
        )
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - 保持调度器存活
                logging.error("proactive tick failed: %s", exc)
            time.sleep(30)

    def _memory_loop(self) -> None:
        while True:
            try:
                self.bot.maintain_memory()
            except Exception as exc:  # noqa: BLE001
                logging.warning("memory maintenance loop failed: %s", exc)
            time.sleep(3600)

    # ── 时间切片 ────────────────────────────────────────────────────
    def _times_for(self, day: str) -> list[str]:
        """只保留饭点问候（早7-8 / 午11:30-12:30 / 晚18-19）与固定时间表。"""
        if self.fixed_times:
            return self.fixed_times
        rng = random.Random(f"zero-bot-{day}")
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

    def _in_window(self, window: str, now: datetime.datetime) -> bool:
        """支持跨零点的窗口（比如 23:30-01:00）。"""
        try:
            start_text, end_text = (part.strip() for part in window.split("-"))
            start_h, start_m = (int(part) for part in start_text.split(":"))
            end_h, end_m = (int(part) for part in end_text.split(":"))
        except ValueError:
            return False
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        cur = now.hour * 60 + now.minute
        if start <= end:
            return start <= cur <= end
        return cur >= start or cur <= end

    # ── 主循环 ──────────────────────────────────────────────────────
    def _tick(self) -> None:
        if not self.bot.proactive_chat_id:
            return
        now = datetime.datetime.now()
        self._maybe_memory_maintenance()
        day = now.strftime("%Y-%m-%d")
        self._maybe_daily_summary(day, now)
        self._maybe_late_care(day, now)
        self._maybe_todo_nudge(day, now)
        self._maybe_rant(day, now)
        self._maybe_special_date(day)
        self._maybe_short_event(day)
        if (
            getattr(self.bot, "ask_busy_after_minutes", 0) > 0
            or getattr(self.bot, "leave_after_minutes", 0) > 0
        ):
            self._maybe_idle_followup()

        if not self._can_send_proactive(now):
            return  # 饭点问候只在 07:00-22:00 之间
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

    def _can_send_proactive(self, now: datetime.datetime) -> bool:
        bounds = self._window_minutes(self.active_window)
        if not bounds:
            return True
        minutes = now.hour * 60 + now.minute
        return bounds[0] <= minutes < bounds[1]

    # ── 记忆书维护 ──────────────────────────────────────────────────
    def _maybe_memory_maintenance(self) -> None:
        if self.memory_check_minutes <= 0:
            return
        if (time.time() - self._last_memory_check) < self.memory_check_minutes * 60:
            return
        self._last_memory_check = time.time()
        threading.Thread(target=self.bot.maintain_memory, daemon=True).start()
        # 顺手准备"想找他说话的理由"，供主动聊天使用
        if self.bot.proactive_chat_id:
            threading.Thread(
                target=self.bot.maybe_generate_thoughts,
                args=(self.bot.proactive_chat_id,),
                daemon=True,
            ).start()

    # ── 每日汇总 ────────────────────────────────────────────────────
    def _maybe_daily_summary(self, day: str, now: datetime.datetime) -> None:
        slots = (("morning", self.summary_morning), ("night", self.summary_night))
        for kind, slot in slots:
            if not slot:
                continue
            key = f"{day}-{kind}"
            if key in self._summary_sent:
                continue
            try:
                hour, minute = (int(part) for part in slot.split(":"))
            except ValueError:
                continue
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now < target:
                continue
            self._summary_sent.add(key)
            if self._user_active():
                continue  # 正在聊天就不插嘴，等他停下来
            threading.Thread(target=self.bot.send_daily_summary, args=(kind,), daemon=True).start()
            logging.info("daily summary(%s) triggered on %s", kind, day)

    # ── 待办跟进 ────────────────────────────────────────────────────
    def _maybe_todo_nudge(self, day: str, now: datetime.datetime) -> None:
        if day in self._nudge_days or now.hour < 9:
            return
        if not self._can_send_proactive(now) or self._user_active():
            return
        chat_id = self.bot.proactive_chat_id
        if not chat_id or not self.bot._due_todo_lines(chat_id):
            return
        self._nudge_days.add(day)
        threading.Thread(target=self.bot.send_todo_nudge, daemon=True).start()
        logging.info("todo nudge triggered on %s", day)

    # ── 深夜关心 ────────────────────────────────────────────────────
    def _maybe_late_care(self, day: str, now: datetime.datetime) -> None:
        if day in self._late_care_days:
            return
        if not self._in_window(self.late_care_window, now):
            return
        if (time.time() - self.bot.last_activity) > 3600:
            return  # 一小时没动静，说明人已经睡了
        self._late_care_days.add(day)
        threading.Thread(target=self.bot.send_late_night_care, daemon=True).start()
        logging.info("late-night care triggered on %s", day)

    # ── AI 牢骚 ─────────────────────────────────────────────────────
    def _maybe_rant(self, day: str, now: datetime.datetime) -> None:
        if self.rant_per_week <= 0:
            return
        try:
            today = datetime.date.fromisoformat(day)
            self._rant_days = [
                d for d in self._rant_days
                if (today - datetime.date.fromisoformat(d)).days < 7
            ]
        except ValueError:
            self._rant_days = []
        if len(self._rant_days) >= self.rant_per_week:
            return
        if not self._can_send_proactive(now):
            return
        if (time.time() - self.bot.last_activity) < 15 * 60:
            return  # 刚聊完，别插嘴
        planned = self._rant_plan.get(day)
        if planned is None:
            planned = ""
            motivation = 1.0
            if hasattr(self.bot, "proactive_motivation"):
                try:
                    motivation = float(self.bot.proactive_motivation())
                except Exception:  # noqa: BLE001
                    motivation = 1.0
            probability = min(0.85, (self.rant_per_week / 7.0) * motivation)
            if random.random() < probability:
                hour = random.randint(9, 21)
                planned = f"{hour:02d}:{random.randint(0, 59):02d}"
            self._rant_plan = {day: planned}
        if not planned or day in self._rant_days:
            return
        if now.strftime("%H:%M") < planned:
            return
        self._rant_days.append(day)
        threading.Thread(target=self.bot.send_rant, daemon=True).start()
        logging.info("rant triggered on %s (planned %s)", day, planned)

    def _maybe_idle_followup(self) -> None:
        """对方 10 分钟没回先问一句，15 分钟没回再收尾。每档各掷一次骰子。"""
        silent = (time.time() - self.bot.last_activity) / 60
        busy_min = int(getattr(self.bot, "ask_busy_after_minutes", 10) or 0)
        leave_min = int(getattr(self.bot, "leave_after_minutes", 15) or 0)
        prob = float(getattr(self.bot, "idle_followup_prob", 0.25) or 0.25)
        outgoing_age = (time.time() - getattr(self.bot, "last_outgoing_at", 0)) / 3600
        has_conversation = 0 < outgoing_age < 2
        if silent < max(busy_min, 1) or not has_conversation:
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

    def _maybe_special_date(self, day: str) -> None:
        """节日/生日：发一条真心话式的短消息（一天只发一次）。"""
        if day in self._special_sent:
            return
        if self._user_active():
            return  # 优先聊天
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

    def _maybe_short_event(self, day: str) -> None:
        """主动短聊：每隔几天找一个话题开口，每天最多一次。"""
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
            self._last_short_event_day = day
            threading.Thread(target=self.bot.send_short_event, daemon=True).start()
            logging.info("short event sent on %s", day)

    def _user_active(self) -> bool:
        window = int(getattr(self.bot, "event_active_window", 90) or 0)
        if window <= 0:
            return False
        return (time.time() - self.bot.last_activity) < window * 60
