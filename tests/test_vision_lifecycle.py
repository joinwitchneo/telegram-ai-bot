"""本地视觉服务的自愈能力（Phase 7 上线后暴露的真实问题：重启后 Ollama 不在）。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.perception.vision import OllamaVision, normalize_model_name


class EnsureRunningTest(unittest.TestCase):
    def build(self, **kwargs):
        return OllamaVision("http://127.0.0.1:11434", "", **kwargs)

    def test_uses_running_service(self):
        vision = self.build()
        with mock.patch.object(vision, "list_models", return_value=["qwen2.5vl:3b"]):
            self.assertTrue(vision.ensure_running())

    def test_disabled_never_starts(self):
        vision = self.build(enabled=False, exe_path="C:/nope/ollama.exe")
        with mock.patch.object(vision, "list_models", return_value=[]):
            self.assertFalse(vision.ensure_running())

    def test_missing_exe_is_honest(self):
        vision = self.build(exe_path="C:/definitely/not/here/ollama.exe")
        with mock.patch.object(vision, "list_models", return_value=[]):
            self.assertFalse(vision.ensure_running())

    def test_autostart_disabled(self):
        vision = self.build(exe_path=__file__, autostart=False)
        with mock.patch.object(vision, "list_models", return_value=[]):
            self.assertFalse(vision.ensure_running())

    def test_starts_process_and_waits(self):
        vision = self.build(exe_path=__file__, start_timeout=5.0)
        calls = {"n": 0}

        def fake_list():
            calls["n"] += 1
            return ["qwen2.5vl:3b"] if calls["n"] >= 2 else []

        with mock.patch.object(vision, "list_models", side_effect=fake_list), \
                mock.patch("tools.perception.vision.subprocess.Popen") as popen, \
                mock.patch("tools.perception.vision.time.sleep", return_value=None):
            self.assertTrue(vision.ensure_running())
        popen.assert_called_once()
        self.assertIn("serve", popen.call_args[0][0])

    def test_start_attempt_is_rate_limited(self):
        vision = self.build(exe_path=__file__, start_timeout=0.1)
        clock = {"t": 100.0}

        def fake_time():
            clock["t"] += 0.01      # 每调用一次推进一点，避免等待循环空转
            return clock["t"]

        with mock.patch.object(vision, "list_models", return_value=[]), \
                mock.patch("tools.perception.vision.subprocess.Popen") as popen, \
                mock.patch("tools.perception.vision.time.sleep", return_value=None), \
                mock.patch("tools.perception.vision.time.time", side_effect=fake_time):
            vision.ensure_running()
            vision.ensure_running()
        self.assertEqual(popen.call_count, 1)

    def test_model_cache_recovers_after_service_starts(self):
        """服务一开始没起来，后来起来了，模型必须能被认出来（不能永久缓存空结果）。"""
        vision = self.build()
        with mock.patch.object(vision, "list_models", return_value=[]):
            self.assertEqual(vision.resolve_model(), "")
        with mock.patch.object(vision, "list_models", return_value=["qwen2.5vl:3b"]):
            self.assertEqual(vision.resolve_model(), "qwen2.5vl:3b")

    def test_available_triggers_start(self):
        vision = self.build(exe_path=__file__)
        with mock.patch.object(vision, "ensure_running", return_value=True) as ensure, \
                mock.patch.object(vision, "list_models", return_value=[]):
            self.assertFalse(vision.available())     # 拉不起来就还是不可用
        ensure.assert_called_once()

    def test_model_name_normalisation(self):
        self.assertEqual(normalize_model_name("qwen2.5vl:3b"), "qwen25vl")
        self.assertEqual(normalize_model_name("llava:7b"), "llava")


if __name__ == "__main__":
    unittest.main()
