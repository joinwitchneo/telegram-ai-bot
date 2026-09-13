"""视觉端到端：图片 -> Vision -> Context -> 最终 LLM messages（Phase 7 诊断 + 事实边界）。

这个测试专门防住"Vision 成功了、但 Context 没带上结果"这类静默失败。
"""

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from core.context_manager import ContextManager
from core.conversation import Conversation
from core.event_bus import EventBus
from core.llm_client import LLMResult
from core.response_planner import ResponsePlanner
from core.response_validator import ResponseValidator
from core.token_budget import TokenBudget
from core.usage_logger import UsageLogger
from personality.consistency_checker import ConsistencyChecker
from style.message_renderer import MessageRenderer, RenderLimits
from tools.perception.router import PerceptionRequest, PerceptionRouter


def make_test_png(path: Path, size: int = 200) -> Path:
    """白底 + 红色圆圈（纯标准库生成，真实可被视觉模型读取）。"""
    rows = []
    centre = size / 2
    radius = size * 0.3
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            inside = (x - centre) ** 2 + (y - centre) ** 2 <= radius ** 2
            row += bytes((214, 40, 40) if inside else (255, 255, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)
    return path


class FakeVision:
    """可控的本地视觉：用来确定性地验证链路，不依赖 Ollama。"""

    def __init__(self, text="图片里有一个红色圆圈，白色背景", *, ok=True, exe_path="C:/ollama.exe"):
        self.text = text
        self.ok = ok
        self.exe_path = exe_path
        self.enabled = True
        self.enabled_flag = True
        self.describe_calls = 0

    def list_models(self):
        return ["qwen2.5vl:3b"]

    def ensure_running(self):
        return True

    def available(self):
        return True

    def resolve_model(self):
        return "qwen2.5vl:3b"

    def describe(self, path, prompt=""):
        from tools.core.result import ToolResult

        self.describe_calls += 1
        if not self.ok:
            return ToolResult.failure("vision", "模型返回错误：CUDA out of memory")
        return ToolResult(name="vision", ok=True, text=self.text, source_type="image", confidence=0.8)


class CapturingClient:
    def __init__(self, content="行吧，红圈圈我看到了。"):
        self.content = content
        self.calls = []

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls.append(messages)
        return LLMResult(
            ok=True, content=self.content, model="fake", tier=tier,
            input_tokens=100, output_tokens=20, cached_tokens=0,
        )


def build_conversation(*, content="行吧，红圈圈我看到了。", content_list=None, checker=None):
    tmp = Path(tempfile.mkdtemp())
    limits = RenderLimits()
    sent: list[str] = []
    renderer = MessageRenderer(
        send=lambda chat, text: sent.append(text), typing=None, limits=limits, sleep=lambda s: None
    )
    client = CapturingClient(content)
    if content_list is not None:
        client.content_list = list(content_list)

        def chat(messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
            client.calls.append(messages)
            index = min(len(client.calls) - 1, len(client.content_list) - 1)
            return LLMResult(ok=True, content=client.content_list[index], model="fake", tier=tier,
                             input_tokens=100, output_tokens=20)

        client.chat = chat
    conversation = Conversation(
        llm_client=client,
        renderer=renderer,
        context_manager=ContextManager(contract="【系统合同】", persona="【人格】"),
        token_budget=TokenBudget(daily_token_budget=100000),
        usage_logger=UsageLogger(tmp / "usage.json"),
        event_bus=EventBus(),
        planner=ResponsePlanner(),
        validator=ResponseValidator(limits=limits),
        checker=checker or ConsistencyChecker(),
        dry_run=False,
    )
    return conversation, client, sent


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.image = make_test_png(self.tmp / "red_circle.png")

    # ── 正常链路 ────────────────────────────────────────────────────
    def test_image_file_is_real_png(self):
        self.assertTrue(self.image.is_file())
        self.assertGreater(self.image.stat().st_size, 100)
        self.assertEqual(self.image.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_router_calls_vision_and_returns_description(self):
        vision = FakeVision()
        router = PerceptionRouter(vision=vision, use_ocr=False)
        result = router.perceive(PerceptionRequest(source_type="image", path=str(self.image)))
        self.assertTrue(result.ok)
        self.assertEqual(vision.describe_calls, 1)
        self.assertIn("红色圆圈", result.text)

    def test_vision_description_reaches_llm_messages(self):
        """核心验收：Vision 的描述必须真的出现在最终发给 LLM 的 messages 里。"""
        vision = FakeVision()
        router = PerceptionRouter(vision=vision, use_ocr=False)
        perception = router.perceive(PerceptionRequest(source_type="image", path=str(self.image)))
        # ① VisionResult.success == True
        self.assertTrue(perception.ok, perception.error)
        # ② VisionResult.data 非空
        self.assertTrue(perception.data)
        self.assertTrue(perception.data.get("vision") or perception.data.get("text"))
        summary = PerceptionRouter.describe_for_context(perception)
        # ③ Context 内容包含视觉描述
        self.assertIn("红色圆圈", summary)

        conversation, client, sent = build_conversation()
        conversation.perceive(1, source_type="image", summary=summary, perception_ok=True)

        # ④ 最终 LLM messages 包含视觉描述
        self.assertTrue(client.calls, "LLM 没有被调用")
        joined = "\n".join(str(message.get("content", "")) for message in client.calls[0])
        self.assertIn("红色圆圈", joined, "视觉结果没有进入最终 messages（Context 注入失败）")
        self.assertTrue(sent)

    # ── 事实边界：失败时不许"看见" ─────────────────────────────────
    def test_failed_vision_blocks_seeing_in_context(self):
        vision = FakeVision(ok=False)
        router = PerceptionRouter(vision=vision, use_ocr=False)
        perception = router.perceive(PerceptionRequest(source_type="image", path=str(self.image)))
        self.assertFalse(perception.ok)
        summary = PerceptionRouter.describe_for_context(perception)
        self.assertIn("看不到", summary)

        conversation, client, _ = build_conversation()
        conversation.perceive(1, source_type="image", summary=summary, perception_ok=False)
        joined = "\n".join(str(message.get("content", "")) for message in client.calls[0])
        self.assertIn("没有获得任何视觉", joined)
        self.assertIn("不要描述", joined)
        self.assertNotIn("红色圆圈", joined)

    def test_checker_flags_claiming_to_see_without_perception(self):
        checker = ConsistencyChecker()
        result = checker.check(
            ["图里是一个红色的圆圈，白色背景，我看到上面有字。"],
            state={"perception_ok": False},
        )
        self.assertIn("FAKE_VISION", result.codes)
        self.assertFalse(result.ok)

    def test_checker_allows_seeing_when_perception_ok(self):
        checker = ConsistencyChecker()
        result = checker.check(
            ["图里是一个红色的圆圈。"], state={"perception_ok": True}
        )
        self.assertNotIn("FAKE_VISION", result.codes)

    def test_fake_vision_reply_is_retried_then_fallback(self):
        """她如果硬说看到了，会被 AI 味检查打回并重试；再犯就走兜底。"""
        checker = ConsistencyChecker()
        conversation, client, sent = build_conversation(
            content_list=[
                "我看到图里有一个红色的圆圈，白色背景。",
                "我看到图里有一个红色的圆圈，白色背景。",
            ],
            checker=checker,
        )
        conversation.perceive(
            1, source_type="image", summary="当前无法获得图片视觉内容", perception_ok=False
        )
        self.assertGreaterEqual(len(client.calls), 1)
        self.assertTrue(sent)
        self.assertNotIn("红色圆圈", sent[0])

    def test_router_without_vision_is_honest(self):
        router = PerceptionRouter(vision=None, use_ocr=False)
        result = router.perceive(PerceptionRequest(source_type="image", path=str(self.image)))
        self.assertFalse(result.ok)
        text = PerceptionRouter.describe_for_context(result)
        self.assertIn("别假装", text)

    def test_instruction_marks_missing_file(self):
        router = PerceptionRouter(vision=FakeVision(), use_ocr=False)
        result = router.perceive(PerceptionRequest(source_type="image", path=str(self.tmp / "nope.png")))
        self.assertFalse(result.ok)


@unittest.skipUnless(
    __import__("os").environ.get("XIYAN_TEST_REAL_VISION") == "1",
    "设置 XIYAN_TEST_REAL_VISION=1 才跑真实 Ollama 视觉（需要服务在位）",
)
class RealVisionTest(unittest.TestCase):
    """真实本地视觉：默认跳过，避免测试依赖外部服务。"""

    def test_real_vision_describes_red_circle(self):
        from tools.perception.vision import OllamaVision

        tmp = Path(tempfile.mkdtemp())
        image = make_test_png(tmp / "red.png")
        vision = OllamaVision("http://127.0.0.1:11434", "")
        if not vision.available():
            self.skipTest("Ollama 或视觉模型不可用")
        result = vision.describe(str(image), prompt="用中文描述这张图片里有什么。30字以内。")
        self.assertTrue(result.ok, result.error)
        self.assertTrue(
            any(word in result.text for word in ("红", "圆", "圈")),
            f"描述里没有预期内容：{result.text}",
        )


if __name__ == "__main__":
    unittest.main()
