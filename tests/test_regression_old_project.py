"""回归：确认旧项目没有被本次重构改动，且备份可用。"""

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT.parent
OLD_PROJECT = WORKSPACE / "telegram-bot"
BACKUP = WORKSPACE / "telegram-bot-backup-2026-09-13"
BASELINE = json.loads((ROOT / "tests" / "baseline_old_project.json").read_text(encoding="utf-8"))


class OldProjectRegressionTest(unittest.TestCase):
    def test_old_project_still_exists(self):
        self.assertTrue(OLD_PROJECT.is_dir())
        self.assertTrue((OLD_PROJECT / "bot.py").is_file())

    def test_baseline_recorded(self):
        self.assertIn("bot_py_sha256", BASELINE)
        self.assertIn("captured_at", BASELINE)

    def test_old_bot_py_unchanged(self):
        """Phase 1 不允许改旧项目核心代码。"""
        digest = hashlib.sha256((OLD_PROJECT / "bot.py").read_bytes()).hexdigest().upper()
        self.assertEqual(digest, BASELINE["bot_py_sha256"].upper())

    def test_backup_exists(self):
        self.assertTrue(BACKUP.is_dir(), "Phase 0 的备份目录应该存在")
        self.assertTrue((BACKUP / "bot.py").is_file())
        self.assertTrue((BACKUP / "config.env").is_file())

    def test_v2_is_separate(self):
        """V2 是独立目录，自己的 data/ 与 config.env 不共用旧项目。"""
        self.assertTrue((ROOT / "config.env").is_file())
        self.assertTrue((ROOT / "data").is_dir())
        self.assertNotEqual(ROOT, OLD_PROJECT)

    def test_v2_does_not_import_old_modules(self):
        """V2 不应依赖旧项目的模块文件。"""
        forbidden = ("pcstatus", "persona_check", "memory_book", "state_store")
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        for name in forbidden:
            self.assertNotIn(f"import {name}", text)


if __name__ == "__main__":
    unittest.main()
