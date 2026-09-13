"""Proactive Scheduler（Phase 6）：所有"能不能主动"的硬条件都在这里。

顺序固定：结算未回复 → quiet hours → 每日上限 → 生成候选（0 token）
→ 冷却/间隔/刚聊过（特殊事件可越级）→ 分数阈值 → LLM Policy（预算）
→ 一次模型调用 → 发送并记账。

凡是任何一步不成立：直接返回，**0 token**。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

from core import llm_policy


def _parse_time(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def parse_clock(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hour, _, minute = str(text).partition(":")
        return max(0, min(23, int(hour))), max(0, min(59, int(minute or 0)))
    except (TypeError, ValueError):
        return default


class ProactiveState:
    """data/proactive_state.json：只放"现在还能不能发"需要的短期状态。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.data = {
            "last_sent_at": "",
            "sent_dates": {},
            "nonresponse_streak": 0,
            "cooldown_until": "",
            "pending": [],
            "last_candidate": {},
        }
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("写入主动状态失败：%s", exc)

    # ── 便捷读写 ────────────────────────────────────────────────────
    def sent_count(self, day: str) -> int:
        return int((self.data.get("sent_dates") or {}).get(day, 0))

    def bump_sent(self, day: str) -> int:
        dates = self.data.setdefault("sent_dates", {})
        dates[day] = int(dates.get(day, 0)) + 1
        # 只保留最近 30 天
        if len(dates) > 30:
            for key in sorted(dates)[:-30]:
                dates.pop(key, None)
        return dates[day]

    def last_sent_at(self) -> datetime.datetime | None:
        return _parse_time(self.data.get("last_sent_at", ""))

    def cooldown_until(self) -> datetime.datetime | None:
        return _parse_time(self.data.get("cooldown_until", ""))


class ProactiveScheduler:
    def __init__(
        self,
        *,
        engine,
        state: ProactiveState,
        history=None,
        runner=None,
        signals_provider=None,
        quiet_start: str = "23:30",
        quiet_end: str = "08:00",
        daily_limit: int = 3,
        min_interval_minutes: float = 90.0,
        recent_chat_minutes: float = 45.0,
        nonresponse_hours: float = 3.0,
        nonresponse_limit: int = 2,
        cooldown_hours: float = 6.0,
        threshold_full: float = 0.75,
        threshold_short: float = 0.50,
        allow_special_override: bool = True,
        enabled: bool = True,
        tick_minutes: float = 15.0,
        policy=None,
        budget=None,
        usage=None,
        stats=None,
        now_fn=None,
    ) -> None:
        self.engine = engine
        self.state = state
        self.history = history
        self.runner = runner
        self.signals_provider = signals_provider
        self.quiet_start = parse_clock(quiet_start, (23, 30))
        self.quiet_end = parse_clock(quiet_end, (8, 0))
        self.daily_limit = max(1, int(daily_limit))
        self.min_interval_minutes = float(min_interval_minutes)
        self.recent_chat_minutes = float(recent_chat_minutes)
        self.nonresponse_hours = float(nonresponse_hours)
        self.nonresponse_limit = max(1, int(nonresponse_limit))
        self.cooldown_hours = float(cooldown_hours)
        self.threshold_full = float(threshold_full)
        self.threshold_short = float(threshold_short)
        self.allow_special_override = bool(allow_special_override)
        self.enabled = bool(enabled)
        self.tick_minutes = max(1.0, float(tick_minutes))
        self.policy = policy or llm_policy.decide
        self.budget = budget
        self.usage = usage
        self.stats = stats
        self._now = now_fn or datetime.datetime.now
        self.last_user_at: datetime.datetime | None = None
        self.last_result: dict = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── 硬条件 ──────────────────────────────────────────────────────
    def quiet_now(self, moment: datetime.datetime) -> bool:
        start = moment.replace(hour=self.quiet_start[0], minute=self.quiet_start[1], second=0, microsecond=0)
        end = moment.replace(hour=self.quiet_end[0], minute=self.quiet_end[1], second=0, microsecond=0)
        if start <= end:
            return start <= moment < end
        return moment >= start or moment < end

    def sent_today(self, moment: datetime.datetime) -> int:
        return self.state.sent_count(moment.date().isoformat())

    def in_cooldown(self, moment: datetime.datetime) -> bool:
        until = self.state.cooldown_until()
        return bool(until and moment < until)

    def _minutes_since(self, moment: datetime.datetime, other: datetime.datetime | None) -> float | None:
        if other is None:
            return None
        return (moment - other).total_seconds() / 60.0

    # ── 结算 ────────────────────────────────────────────────────────
    def settle_pending(self, moment: datetime.datetime) -> dict:
        """超过 nonresponse_hours 还没回 → 记一次"没回"，连续两次进冷却。"""
        pending = list(self.state.data.get("pending") or [])
        if not pending:
            return {"ignored": 0, "streak": int(self.state.data.get("nonresponse_streak", 0))}
        remaining, ignored = [], 0
        for item in pending:
            sent = _parse_time(item.get("ts", ""))
            if sent is None:
                continue
            if (moment - sent).total_seconds() >= self.nonresponse_hours * 3600:
                ignored += 1
            else:
                remaining.append(item)
        if ignored:
            streak = int(self.state.data.get("nonresponse_streak", 0)) + ignored
            self.state.data["nonresponse_streak"] = streak
            if streak >= self.nonresponse_limit:
                until = moment + datetime.timedelta(hours=self.cooldown_hours)
                self.state.data["cooldown_until"] = until.isoformat(timespec="seconds")
            self.state.data["pending"] = remaining
            self.state.save()
        return {
            "ignored": ignored,
            "streak": int(self.state.data.get("nonresponse_streak", 0)),
            "cooldown_until": self.state.data.get("cooldown_until", ""),
        }

    def on_user_message(self, *, now: datetime.datetime | None = None) -> dict:
        """用户回来了：解除冷却 + 重置连续未回复。"""
        moment = now or self._now()
        self.last_user_at = moment
        marked = 0
        if self.history is not None:
            marked = self.history.mark_replied(now=moment)
        had_cooldown = bool(self.state.data.get("cooldown_until"))
        self.state.data["pending"] = []
        self.state.data["nonresponse_streak"] = 0
        self.state.data["cooldown_until"] = ""
        self.state.save()
        return {"marked_replied": marked, "cooldown_reset": had_cooldown}

    # ── 主循环 ──────────────────────────────────────────────────────
    def tick(self, *, now: datetime.datetime | None = None, force: bool = False) -> dict:
        moment = now or self._now()
        if not self.enabled and not force:
            return self._done({"sent": False, "reason": "disabled"})

        settled = self.settle_pending(moment)
        if self.quiet_now(moment):
            return self._done({"sent": False, "reason": "quiet_hours", "settled": settled})

        sent_today = self.sent_today(moment)
        if sent_today >= self.daily_limit:
            return self._done({"sent": False, "reason": "daily_limit", "sent_today": sent_today})

        signals = self._signals()
        candidates = self.engine.build_candidates(
            signals=signals,
            last_user_at=self.last_user_at,
            sent_today=sent_today,
            daily_limit=self.daily_limit,
            last_sent_at=self.state.last_sent_at(),
            min_interval_minutes=self.min_interval_minutes,
            nonresponse_streak=int(self.state.data.get("nonresponse_streak", 0)),
            nonresponse_limit=self.nonresponse_limit,
            now=moment,
        )
        best = self.engine.best(candidates)
        if best is None:
            return self._done({"sent": False, "reason": "no_candidate", "settled": settled})

        special = bool(best.special and self.allow_special_override)
        # 连续未回复的冷却对谁都生效：特殊事件也不能在她被无视两次后继续发
        if self.in_cooldown(moment):
            return self._done({
                "sent": False, "reason": "cooldown", "candidate": best.to_dict(), "settled": settled,
            })
        if not special:
            since_sent = self._minutes_since(moment, self.state.last_sent_at())
            if since_sent is not None and since_sent < self.min_interval_minutes:
                return self._done({
                    "sent": False, "reason": "min_interval", "candidate": best.to_dict(),
                    "since_sent_minutes": round(since_sent, 1),
                })
            since_user = self._minutes_since(moment, self.last_user_at)
            if since_user is not None and since_user < self.recent_chat_minutes:
                return self._done({
                    "sent": False, "reason": "recent_chat", "candidate": best.to_dict(),
                    "since_user_minutes": round(since_user, 1),
                })
            if best.score < self.threshold_short:
                return self._done({
                    "sent": False, "reason": "low_score", "candidate": best.to_dict(),
                    "settled": settled,
                })

        # LLM Policy：预算/静默时段的总闸门（特殊事件也不能绕过预算）
        decision = self.policy(
            best.hint,
            category="proactive",
            budget_state=self.budget.state() if self.budget is not None else None,
        )
        if not getattr(decision, "use_llm", True):
            return self._done({
                "sent": False, "reason": f"policy:{getattr(decision, 'reason', '')}",
                "candidate": best.to_dict(), "tokens": 0,
            })

        if self.runner is None:
            return self._done({"sent": False, "reason": "no_runner", "candidate": best.to_dict()})
        try:
            result = self.runner(best)
        except Exception as exc:  # noqa: BLE001 - 主动失败不能影响主流程
            logging.warning("主动消息发送失败：%s", exc)
            result = {"sent": []}
        sent = list((result or {}).get("sent") or [])
        if not sent:
            return self._done({
                "sent": False, "reason": "generation_failed", "candidate": best.to_dict(),
                "detail": result,
            })

        entry = None
        if self.history is not None:
            entry = self.history.record(candidate=best, message="\n".join(sent), now=moment)
        self.state.data["last_sent_at"] = moment.isoformat(timespec="seconds")
        self.state.data["last_candidate"] = best.to_dict()
        self.state.bump_sent(moment.date().isoformat())
        self.state.data.setdefault("pending", []).append(
            {"id": best.id, "ts": moment.isoformat(timespec="seconds"), "topic": best.topic}
        )
        self.state.save()
        if self.stats is not None:
            try:
                self.stats.record(
                    mode="PROACTIVE", length=str((result or {}).get("plan", {}).get("length", "")),
                    tone=str((result or {}).get("plan", {}).get("tone", "")),
                    score=best.score, reply_message_count=len(sent), replied=True,
                )
            except Exception:  # noqa: BLE001
                pass
        return self._done({
            "sent": True,
            "candidate": best.to_dict(),
            "messages": sent,
            "score": best.score,
            "special": special,
            "level": "full" if best.score >= self.threshold_full or special else "short",
            "input_tokens": (result or {}).get("input_tokens", 0),
            "output_tokens": (result or {}).get("output_tokens", 0),
            "history_entry": entry,
        })

    def _signals(self) -> dict:
        if self.signals_provider is None:
            return {}
        try:
            return self.signals_provider() or {}
        except Exception:  # noqa: BLE001
            return {}

    def _done(self, payload: dict) -> dict:
        self.last_result = payload
        return payload

    # ── 后台线程 ────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - 线程不能挂
                    logging.warning("主动调度循环异常：%s", exc)
                self._stop.wait(self.tick_minutes * 60)

        self._thread = threading.Thread(target=loop, name="proactive-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    # ── 状态 ────────────────────────────────────────────────────────
    def status(self, *, now: datetime.datetime | None = None) -> dict:
        moment = now or self._now()
        last_sent = self.state.last_sent_at()
        since_user = self._minutes_since(moment, self.last_user_at)
        return {
            "enabled": self.enabled,
            "quiet": self.quiet_now(moment),
            "sent_today": self.sent_today(moment),
            "daily_limit": self.daily_limit,
            "cooldown_until": self.state.data.get("cooldown_until", ""),
            "nonresponse_streak": int(self.state.data.get("nonresponse_streak", 0)),
            "last_sent_at": self.state.data.get("last_sent_at", ""),
            "pending": len(self.state.data.get("pending") or []),
            "since_user_minutes": round(since_user, 1) if since_user is not None else None,
            "last_result": self.last_result,
            "history": self.history.stats() if self.history is not None else {},
        }
