"""能力层：接口统一、状态、生命周期、降级（方案第十五～三十节）。"""

import unittest

from capabilities.base import Capability, CapabilityResult
from capabilities.manager import CapabilityManager
from capabilities.services import OcrCapability, VisionCapability, WhisperCapability


class FakeVision:
    def __init__(self, *, ok=True, running=True, exe="C:/ollama.exe"):
        self.ok = ok
        self.running = running
        self.exe_path = exe
        self.enabled = True
        self.start_calls = 0

    def list_models(self):
        return ["qwen2.5vl:3b"] if self.running else []

    def ensure_running(self):
        self.start_calls += 1
        self.running = True
        return True

    def describe(self, path, prompt=""):
        if not self.ok:
            return type("R", (), {"ok": False, "error": "模型炸了", "text": ""})()
        return type("R", (), {"ok": True, "error": "", "text": "一只猫趴在沙发上"})()

    def resolve_model(self):
        return "qwen2.5vl:3b"


class DownCapability(Capability):
    name = "down"
    description = "总是不可用"

    def installed(self):
        return False

    def execute(self, **request):
        return CapabilityResult(success=True, capability=self.name)


class BaseTest(unittest.TestCase):
    def test_result_is_structured(self):
        result = CapabilityResult(success=True, capability="vision", data={"description": "猫"})
        payload = result.to_dict()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["description"], "猫")
        self.assertIn("latency_ms", payload)

    def test_unavailable_capability_degrades_without_exception(self):
        manager = CapabilityManager()
        manager.register(DownCapability())
        self.assertFalse(manager.has("down"))
        result = manager.execute("down")
        self.assertFalse(result.success)
        self.assertTrue(result.degraded)

    def test_unknown_capability(self):
        result = CapabilityManager().execute("ghost")
        self.assertFalse(result.success)
        self.assertIn("没有登记", result.error)

    def test_disabled_manager_reports_nothing(self):
        manager = CapabilityManager(enabled=False)
        manager.register(DownCapability())
        self.assertFalse(manager.has("down"))


class VisionCapabilityTest(unittest.TestCase):
    def test_available_and_execute(self):
        capability = VisionCapability(FakeVision())
        self.assertTrue(capability.available())
        result = capability.call(path="a.jpg")
        self.assertTrue(result.success)
        self.assertEqual(result.data["description"], "一只猫趴在沙发上")

    def test_service_started_on_demand(self):
        vision = FakeVision(running=False)
        capability = VisionCapability(vision)
        self.assertTrue(capability.available())
        self.assertGreaterEqual(vision.start_calls, 1)

    def test_failure_degrades_not_fabricates(self):
        capability = VisionCapability(FakeVision(ok=False))
        result = capability.call(path="a.jpg")
        self.assertFalse(result.success)
        self.assertTrue(result.degraded)
        self.assertEqual(result.data, {})           # 绝不编造画面内容

    def test_no_exe_means_not_installed(self):
        capability = VisionCapability(FakeVision(exe=""))
        self.assertFalse(capability.installed())

    def test_state_tracks_calls_and_errors(self):
        capability = VisionCapability(FakeVision(ok=False))
        capability.call(path="a.jpg")
        capability.call(path="b.jpg")
        state = capability.state.to_dict()
        self.assertEqual(state["calls"], 2)
        self.assertGreaterEqual(state["failures"], 1)
        self.assertTrue(state["last_error"])


class ManagerTest(unittest.TestCase):
    def test_has_and_execute(self):
        manager = CapabilityManager()
        manager.register(VisionCapability(FakeVision()))
        self.assertTrue(manager.has("vision"))
        self.assertTrue(manager.execute("vision", path="a.jpg").success)

    def test_report_shape(self):
        manager = CapabilityManager()
        manager.register(VisionCapability(FakeVision()))
        report = manager.report()["capabilities"]["vision"]
        for key in ("description", "installed", "available", "state"):
            self.assertIn(key, report)

    def test_names_sorted(self):
        manager = CapabilityManager()
        manager.register(VisionCapability(FakeVision()))
        manager.register(DownCapability())
        self.assertEqual(manager.names(), ["down", "vision"])


class RealServiceSmokeTest(unittest.TestCase):
    """用真实环境探测一次（没有依赖时也必须安全返回 False，不抛异常）。"""

    def test_ocr_capability_probe(self):
        capability = OcrCapability()
        self.assertIn(capability.installed(), (True, False))
        self.assertIsInstance(capability.health_check(), bool)

    def test_whisper_missing_model_is_not_installed(self):
        capability = WhisperCapability(model_path="")
        self.assertFalse(capability.health_check())

    def test_whisper_with_model_path_checks_binary(self):
        from tools.perception import whisper as whisper_mod

        capability = WhisperCapability(model_path=__file__)
        self.assertEqual(capability.health_check(), bool(whisper_mod.find_binary()))


if __name__ == "__main__":
    unittest.main()
