"""单实例锁与 PID 存活判定：启动器"替换旧进程"依赖它，必须准确。"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import bot as bot_module   # noqa: E402


class PidAliveTest(unittest.TestCase):
    def test_live_and_missing_pids(self):
        self.assertTrue(bot_module._pid_alive(os.getpid()))
        self.assertFalse(bot_module._pid_alive(999999))
        self.assertFalse(bot_module._pid_alive(0))
        self.assertFalse(bot_module._pid_alive(-1))

    def test_force_killed_process_is_reported_dead(self):
        """回归：刚被强杀的进程不能被误判成"还活着"，否则新实例永远起不来。

        Windows 上 OpenProcess 对被强杀、pid 还没回收的进程仍会成功，
        旧实现只看句柄就判"活着"，于是"替换旧进程"这一步必然卡住。
        """
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertTrue(bot_module._pid_alive(proc.pid), "刚起来的子进程应该判为活着")
            proc.kill()
            proc.wait(timeout=15)
            time.sleep(0.3)
            self.assertFalse(bot_module._pid_alive(proc.pid), "被强杀的进程必须判为已死")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=15)


class SingleInstanceTest(unittest.TestCase):
    def build(self):
        return Path(tempfile.mkdtemp()) / "bot.lock"

    def test_acquire_writes_fingerprint(self):
        lock = bot_module.SingleInstance(self.build())
        ok, info = lock.acquire()
        self.assertTrue(ok)
        self.assertEqual(info["pid"], os.getpid())
        self.assertTrue(info["path"].endswith("bot.py"))

    def test_second_instance_is_refused_while_first_lives(self):
        path = self.build()
        other = os.getppid()
        self.assertNotEqual(other, os.getpid())
        path.write_text(json.dumps({"pid": other, "started_at": "x"}), encoding="utf-8")
        ok, existing = bot_module.SingleInstance(path).acquire()
        self.assertFalse(ok, "锁被一个活着的进程占着，不能悄悄开第二个")
        self.assertEqual(int(existing["pid"]), other)

    def test_stale_lock_is_taken_over(self):
        path = self.build()
        path.write_text(json.dumps({"pid": 999999, "started_at": "x"}), encoding="utf-8")
        ok, info = bot_module.SingleInstance(path).acquire()
        self.assertTrue(ok, "上一个实例已经死了，必须能接管")
        self.assertEqual(info["pid"], os.getpid())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pid"], os.getpid())

    def test_release_removes_lock(self):
        path = self.build()
        lock = bot_module.SingleInstance(path)
        lock.acquire()
        lock.release()
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
