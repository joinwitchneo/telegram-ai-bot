"""WakeQueue：文件队列 + 互斥锁 + 任务租约（三者职责严格分开）。

queue.lock  只保护 queue.json 的读-改-写（毫秒级持有，执行 cycle 期间绝不持有）
lease       只表达某个 wake 任务的执行权（claimed_by / claimed_at / lease_until）
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from core.atomic_io import atomic_write_text

WAKE_STATES = ("PENDING", "RUNNING", "COMPLETED", "FAILED_PERMANENT")
BACKOFF_MINUTES = (5, 15, 60)
LOCK_STALE_SECONDS = 30


def _now() -> datetime.datetime:
    return datetime.datetime.now()


class WakeQueue:
    def __init__(self, path: Path, *, lock_path: Path | None = None,
                 default_max_attempts: int = 3) -> None:
        self.path = Path(path)
        self.lock_path = Path(lock_path) if lock_path else self.path.with_name("queue.lock")
        self.default_max_attempts = max(1, int(default_max_attempts))
        self._lock = threading.RLock()
        self.owner = f"{socket.gethostname()}-{os.getpid()}"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logging.warning("[v3] 队列目录不可用：%s", exc)
        self.tasks: list = []
        self._load()

    # ── 持久化与互斥锁 ──────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.tasks = list(data.get("tasks") or [])
        except (OSError, json.JSONDecodeError):
            logging.warning("[v3] wake_queue.json 损坏，从空开始")

    def _save_unlocked(self) -> None:
        try:
            atomic_write_text(self.path, json.dumps({"tasks": self.tasks}, ensure_ascii=False, indent=1))
        except OSError as exc:
            logging.warning("[v3] 写 wake_queue 失败：%s", exc)

    def _acquire_lock(self) -> bool:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logging.warning("[v3] 锁目录不可写：%s", exc)
            return False
        for _ in range(2):
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"{self.owner} {_now().isoformat(timespec='seconds')}")
                return True
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                except OSError:
                    return False
                if age > LOCK_STALE_SECONDS:
                    try:
                        self.lock_path.unlink()
                        continue
                    except OSError:
                        return False
                return False
            except OSError:
                return False
        return False

    def _release_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except OSError:
            pass

    @contextmanager
    def _queue_lock(self):
        """毫秒级持锁，只包住 queue.json 的读-改-写；执行 cycle 期间绝不持有。"""
        with self._lock:
            locked = self._acquire_lock()
            try:
                yield locked
            finally:
                if locked:
                    self._release_lock()

    # ── 入队 ────────────────────────────────────────────────────────
    def enqueue(self, *, reason: str, earliest_at: str, priority: str = "normal",
                cycle_id: str = "", max_attempts: int | None = None) -> dict:
        cap = self.default_max_attempts if max_attempts is None else int(max_attempts)
        with self._queue_lock() as locked:
            if not locked:
                logging.warning("[v3] 队列锁未获取（其它进程在写或目录不可写），本次入队跳过")
                return {}
            self._load()
            task = {
                "id": f"wake_{len(self.tasks) + 1:06d}",
                "reason": str(reason)[:60],
                "priority": str(priority),
                "earliest_at": str(earliest_at),
                "status": "PENDING",
                "attempts": 0,
                "max_attempts": max(1, cap),
                "cycle_id": str(cycle_id),
                "claimed_by": "",
                "claimed_at": "",
                "lease_until": "",
                "last_error": "",
                "created_at": _now().isoformat(timespec="seconds"),
            }
            self.tasks.append(task)
            self._save_unlocked()
        logging.info("[v3] 排入唤醒任务 %s（%s，%s）", task["id"], task["priority"], task["earliest_at"])
        return dict(task)

    # ── 查询 ────────────────────────────────────────────────────────
    def all(self) -> list:
        with self._lock:
            self._load()
            return [dict(item) for item in self.tasks]

    def pending_count(self) -> int:
        return sum(1 for item in self.all() if item.get("status") == "PENDING")

    def next_pending_at(self) -> str:
        items = [str(item.get("earliest_at", "")) for item in self.all() if item.get("status") == "PENDING"]
        return min(items) if items else ""

    def due(self, *, now: datetime.datetime | None = None) -> list:
        moment = now or _now()
        ready = []
        for item in self.all():
            if item.get("status") != "PENDING":
                continue
            try:
                earliest = datetime.datetime.fromisoformat(str(item.get("earliest_at")))
            except ValueError:
                continue
            if earliest <= moment:
                ready.append(item)
        ready.sort(key=lambda item: ({"high": 0, "normal": 1, "low": 2}.get(str(item.get("priority")), 1),
                                     str(item.get("earliest_at"))))
        return ready

    # ── 租约与状态机 ────────────────────────────────────────────────
    def claim(self, task_id: str, *, lease_minutes: int = 30,
              now: datetime.datetime | None = None) -> bool:
        moment = now or _now()
        with self._queue_lock() as locked:
            if not locked:
                return False
            self._load()
            for item in self.tasks:
                if item.get("id") != task_id:
                    continue
                if item.get("status") != "PENDING":
                    return False
                item["status"] = "RUNNING"
                item["attempts"] = int(item.get("attempts", 0)) + 1
                item["claimed_by"] = self.owner
                item["claimed_at"] = moment.isoformat(timespec="seconds")
                item["lease_until"] = (moment + datetime.timedelta(minutes=max(1, int(lease_minutes)))
                                       ).isoformat(timespec="seconds")
                self._save_unlocked()
                return True
            return False

    def _set_status(self, task_id: str, status: str,
                    now: datetime.datetime | None = None) -> bool:
        with self._queue_lock() as locked:
            if not locked:
                logging.warning("[v3] 队列锁未获取，状态变更跳过：%s -> %s", task_id, status)
                return False
            self._load()
            for item in self.tasks:
                if item.get("id") != task_id:
                    continue
                item["status"] = status
                item["claimed_by"] = ""
                item["lease_until"] = ""
                item["finished_at"] = (now or _now()).isoformat(timespec="seconds")
                self._save_unlocked()
                return True
        return False

    def complete(self, task_id: str, *, now: datetime.datetime | None = None) -> bool:
        return self._set_status(task_id, "COMPLETED", now=now)

    def fail(self, task_id: str, *, error: str = "",
             now: datetime.datetime | None = None) -> dict:
        moment = now or _now()
        with self._queue_lock() as locked:
            if not locked:
                logging.warning("[v3] 队列锁未获取，失败状态未落盘：%s", task_id)
                return {}
            self._load()
            for item in self.tasks:
                if item.get("id") != task_id:
                    continue
                attempts = int(item.get("attempts", 0))
                max_attempts = int(item.get("max_attempts", 3))
                item["last_error"] = str(error)[:200]
                if attempts >= max_attempts:
                    item["status"] = "FAILED_PERMANENT"
                else:
                    backoff = BACKOFF_MINUTES[min(max(1, attempts) - 1, len(BACKOFF_MINUTES) - 1)]
                    item["status"] = "PENDING"
                    item["earliest_at"] = (moment + datetime.timedelta(minutes=backoff)
                                           ).isoformat(timespec="seconds")
                item["claimed_by"] = ""
                item["lease_until"] = ""
                self._save_unlocked()
                return dict(item)
        return {}

    def recover_stale(self, *, now: datetime.datetime | None = None) -> int:
        """租约过期说明上一次可能已经死了 -> 回到 PENDING（不重复正在运行的实例）。"""
        moment = now or _now()
        recovered = 0
        with self._queue_lock() as locked:
            if not locked:
                logging.warning("[v3] 队列锁未获取，本次跳过过期租约回收")
                return 0
            self._load()
            for item in self.tasks:
                if item.get("status") != "RUNNING":
                    continue
                try:
                    lease = datetime.datetime.fromisoformat(str(item.get("lease_until")))
                except ValueError:
                    continue
                if lease < moment:
                    item["status"] = "PENDING"
                    item["stale_recovered"] = int(item.get("stale_recovered", 0)) + 1
                    item["claimed_by"] = ""
                    item["lease_until"] = ""
                    recovered += 1
            if recovered:
                self._save_unlocked()
        if recovered:
            logging.warning("[v3] 回收了 %d 个过期租约任务", recovered)
        return recovered

    def requeue(self, task_id: str, *, now: datetime.datetime | None = None) -> bool:
        moment = now or _now()
        with self._queue_lock() as locked:
            if not locked:
                logging.warning("[v3] 队列锁未获取，requeue 跳过：%s", task_id)
                return False
            self._load()
            for item in self.tasks:
                if item.get("id") != task_id:
                    continue
                item["status"] = "PENDING"
                item["attempts"] = 0
                item["earliest_at"] = moment.isoformat(timespec="seconds")
                item["last_error"] = ""
                self._save_unlocked()
                return True
        return False

    # ── 静默保险 ────────────────────────────────────────────────────
    def ensure_silence_guard(self, *, now: datetime.datetime | None = None,
                             max_silence_hours: int = 48,
                             min_interval_minutes: int = 60) -> dict:
        moment = now or _now()
        if any(item.get("status") in ("PENDING", "RUNNING") for item in self.all()):
            return {}
        latest = ""
        for item in self.all():
            latest = max(latest, str(item.get("finished_at") or item.get("created_at") or ""))
        reference = moment
        if latest:
            try:
                reference = datetime.datetime.fromisoformat(latest)
            except ValueError:
                reference = moment
        if (moment - reference).total_seconds() < max(1, int(max_silence_hours)) * 3600:
            return {}
        earliest = moment + datetime.timedelta(minutes=max(1, int(min_interval_minutes)))
        return self.enqueue(reason="max_silence_guard",
                            earliest_at=earliest.isoformat(timespec="seconds"), priority="low")

    def stats(self) -> dict:
        items = self.all()
        counts: dict = {}
        for item in items:
            status = str(item.get("status", ""))
            counts[status] = counts.get(status, 0) + 1
        return {
            "total": len(items),
            "by_status": counts,
            "next_at": self.next_pending_at(),
            "failed_permanent": [item["id"] for item in items
                                 if item.get("status") == "FAILED_PERMANENT"][:5],
        }
