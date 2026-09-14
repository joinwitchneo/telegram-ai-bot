"""SchedulerRunner：闹钟循环——巡检、认领、拉起一次性 cycle 进程、落状态。"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path


class SchedulerRunner:
    def __init__(
        self,
        *,
        queue,
        base_dir: Path,
        lease_minutes: int = 30,
        max_silence_hours: int = 48,
        min_interval_minutes: int = 60,
        wake_manager=None,
        spawn=None,
        cycle_timeout_seconds: int = 300,
        now_fn=None,
    ) -> None:
        self.queue = queue
        self.base_dir = Path(base_dir)
        self.lease_minutes = int(lease_minutes)
        self.max_silence_hours = int(max_silence_hours)
        self.min_interval_minutes = int(min_interval_minutes)
        self.wake_manager = wake_manager
        self.cycle_timeout_seconds = int(cycle_timeout_seconds)
        self._spawn = spawn or self._default_spawn
        self._now = now_fn or (lambda: None)

    def _default_spawn(self, task_id: str) -> tuple:
        """拉起一次性认知进程：执行完就退出（不存在常驻的 Cycle 进程）。"""
        proc = subprocess.run(
            [sys.executable, "-m", "v3.cycle_runner", "--wake-id", task_id],
            cwd=str(self.base_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=self.cycle_timeout_seconds,
        )
        return proc.returncode, (proc.stdout or "")[-400:], (proc.stderr or "")[-400:]

    def tick(self, *, now=None) -> dict:
        """一轮巡检：回收过期租约 -> 静默保险 -> 执行到期任务 -> 没有活时可自主唤醒。"""
        moment = now
        recovered = self.queue.recover_stale(now=moment)
        guarded = self.queue.ensure_silence_guard(
            now=moment, max_silence_hours=self.max_silence_hours,
            min_interval_minutes=self.min_interval_minutes,
        )
        executed = []
        for task in self.queue.due(now=moment):
            task_id = str(task.get("id"))
            if not self.queue.claim(task_id, lease_minutes=self.lease_minutes, now=moment):
                continue
            try:
                code, out, err = self._spawn(task_id)
            except Exception as exc:  # noqa: BLE001
                logging.warning("[v3] 拉起 cycle 失败 %s：%s", task_id, exc)
                self.queue.fail(task_id, error=f"{type(exc).__name__}: {exc}", now=moment)
                executed.append({"id": task_id, "ok": False, "error": str(exc)[:120]})
                continue
            if code == 0:
                self.queue.complete(task_id, now=moment)
                executed.append({"id": task_id, "ok": True, "stdout": out})
            else:
                self.queue.fail(task_id, error=err or f"exit={code}", now=moment)
                executed.append({"id": task_id, "ok": False, "error": (err or f"exit={code}")[:120]})
        # 队列空了才考虑"自己醒来"：唤醒判断是纯 Python，绝不调 LLM
        woke: dict = {}
        if self.wake_manager is not None and not executed and not guarded:
            try:
                woke = self.wake_manager.tick(now=moment,
                                              queue_busy=bool(self.queue.pending_count()))
            except Exception as exc:  # noqa: BLE001 - 唤醒判断失败不能让调度循环挂掉
                logging.warning("[v3] 自主唤醒判断异常：%s", exc)
                woke = {"created": False, "reason": f"判断异常：{exc}"}
        return {"recovered": recovered, "guard": bool(guarded), "executed": executed,
                "woke": woke}

    def loop(self, *, poll_seconds: int = 300, stop_event=None) -> None:
        logging.info("[v3] Scheduler 启动：每 %d 秒巡检一次 wake_queue", poll_seconds)
        while True:
            if stop_event is not None and stop_event.is_set():
                logging.info("[v3] Scheduler 收到停止信号")
                return
            try:
                result = self.tick()
                if result["executed"]:
                    logging.info("[v3] 本轮执行：%s", result["executed"])
            except Exception as exc:  # noqa: BLE001 - 调度循环不能挂
                logging.warning("[v3] 巡检异常：%s", exc)
            time.sleep(max(5, int(poll_seconds)))
