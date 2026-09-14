"""V3 Scheduler：闹钟巡检（认领、执行、失败退避、执行期不持锁）。"""

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path

import scheduler as scheduler_module
from v3.scheduler.queue import WakeQueue
from v3.scheduler.runner import SchedulerRunner


def stamp(moment: datetime.datetime) -> str:
    return moment.isoformat(timespec="seconds")


class SchedulerRunnerTest(unittest.TestCase):
    def build(self, spawn):
        tmp = Path(tempfile.mkdtemp())
        queue = WakeQueue(tmp / "wake_queue.json")
        runner = SchedulerRunner(
            queue=queue, base_dir=tmp, spawn=spawn,
            lease_minutes=30, max_silence_hours=48, min_interval_minutes=60,
        )
        return queue, runner, tmp

    def test_tick_runs_due_task_then_completes(self):
        seen = []

        def spawn(task_id):
            seen.append(task_id)
            return 0, "ok", ""

        queue, runner, _ = self.build(spawn)
        base = datetime.datetime.now()
        task = queue.enqueue(reason="t", earliest_at=stamp(base))
        result = runner.tick(now=base)
        self.assertEqual(seen, [task["id"]])
        self.assertTrue(result["executed"][0]["ok"])
        self.assertEqual(queue.all()[0]["status"], "COMPLETED")

    def test_execution_does_not_hold_queue_lock(self):
        state = {}

        def spawn(task_id):
            state["lock_exists"] = queue.lock_path.exists()
            return 0, "", ""

        queue, runner, _ = self.build(spawn)
        base = datetime.datetime.now()
        queue.enqueue(reason="t", earliest_at=stamp(base))
        runner.tick(now=base)
        self.assertFalse(state["lock_exists"], "执行 cycle 期间绝不能持有 queue.lock")

    def test_failure_backs_off_then_failed_permanent(self):
        def spawn(task_id):
            return 1, "", "boom"

        queue, runner, _ = self.build(spawn)
        base = datetime.datetime.now()
        task = queue.enqueue(reason="t", earliest_at=stamp(base), max_attempts=2)

        runner.tick(now=base)
        first = queue.all()[0]
        self.assertEqual(first["status"], "PENDING")          # 退避重试，不留在 RUNNING
        self.assertEqual(first["attempts"], 1)
        self.assertGreater(first["earliest_at"], stamp(base))

        runner.tick(now=base + datetime.timedelta(minutes=10))
        final = queue.all()[0]
        self.assertEqual(final["status"], "FAILED_PERMANENT")
        self.assertEqual(final["attempts"], 2)
        self.assertIn("boom", final["last_error"])
        self.assertEqual(queue.stats()["failed_permanent"], [task["id"]])

    def test_not_due_task_is_not_executed(self):
        calls = []

        def spawn(task_id):
            calls.append(task_id)
            return 0, "", ""

        queue, runner, _ = self.build(spawn)
        base = datetime.datetime.now()
        queue.enqueue(reason="t", earliest_at=stamp(base + datetime.timedelta(minutes=30)))
        result = runner.tick(now=base)
        self.assertEqual(calls, [])
        self.assertEqual(result["executed"], [])

    def test_spawn_exception_marks_task_failed(self):
        def spawn(task_id):
            raise RuntimeError("拉不起来")

        queue, runner, _ = self.build(spawn)
        base = datetime.datetime.now()
        queue.enqueue(reason="t", earliest_at=stamp(base))
        result = runner.tick(now=base)
        self.assertFalse(result["executed"][0]["ok"])
        self.assertEqual(queue.all()[0]["status"], "PENDING")   # 还能退避重试

    def test_enqueue_creates_missing_directories(self):
        """回归：队列目录还不存在时，唤醒任务不允许被静默丢掉（首次启动就会走这条路）。"""
        tmp = Path(tempfile.mkdtemp())
        queue = WakeQueue(tmp / "nested" / "deep" / "wake_queue.json")
        task = queue.enqueue(reason="first", earliest_at=stamp(datetime.datetime.now()))
        self.assertTrue(task, "目录不存在时必须自动建目录并入队")
        self.assertEqual(task["status"], "PENDING")
        self.assertEqual(queue.pending_count(), 1)
        self.assertTrue((tmp / "nested" / "deep" / "wake_queue.json").is_file())
        self.assertFalse((tmp / "nested" / "deep" / "queue.lock").exists(), "操作后必须释放锁")


class SchedulerLockTest(unittest.TestCase):
    """调度器实例锁：给启动器定位用，同时防止开第二个调度器。"""

    def build(self):
        return Path(tempfile.mkdtemp()) / "scheduler.lock"

    def test_lock_records_pid_path_and_started_at(self):
        path = self.build()
        self.assertTrue(scheduler_module._acquire_lock(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["pid"], os.getpid())
        self.assertTrue(data["path"].endswith("scheduler.py"))
        self.assertIn("started_at", data)

    def test_lock_blocks_when_another_live_process_holds_it(self):
        path = self.build()
        other = os.getppid() if os.getppid() and os.getppid() != os.getpid() else 4
        path.write_text(json.dumps({"pid": other, "started_at": "x"}), encoding="utf-8")
        self.assertFalse(scheduler_module._acquire_lock(path), "另一个调度器还活着就不能再开")

    def test_stale_lock_is_taken_over(self):
        path = self.build()
        path.write_text(json.dumps({"pid": 999999, "started_at": "x"}), encoding="utf-8")
        self.assertTrue(scheduler_module._acquire_lock(path), "上一个调度器已经死了要能接管")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pid"], os.getpid())

    def test_release_only_removes_own_lock(self):
        path = self.build()
        path.write_text(json.dumps({"pid": 999999}), encoding="utf-8")
        scheduler_module._release_lock(path)
        self.assertTrue(path.is_file(), "不能删掉别人的锁")
        scheduler_module._acquire_lock(path)
        scheduler_module._release_lock(path)
        self.assertFalse(path.is_file())

    def test_pid_alive_helper_is_safe_on_windows(self):
        """回归：Windows 上 os.kill(pid, 0) 会真的终止进程，必须用安全实现。"""
        self.assertTrue(scheduler_module._pid_alive(os.getpid()))
        self.assertFalse(scheduler_module._pid_alive(999999))


if __name__ == "__main__":
    unittest.main()
