import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.perception import document as document_mod
from tools.perception import image as image_mod
from tools.perception import video as video_mod
from tools.perception.router import PerceptionRequest, PerceptionRouter


def write_png(path: Path, width: int = 640, height: int = 480) -> Path:
    header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height)
    path.write_bytes(header + b"\x00" * 32)
    return path


class ImageTest(unittest.TestCase):
    def test_png_dimensions(self):
        tmp = Path(tempfile.mkdtemp())
        result = image_mod.inspect(write_png(tmp / "a.png", 800, 600))
        self.assertTrue(result.ok)
        self.assertIn("800×600", result.text)
        self.assertEqual(result.data["metadata"]["format"], "png")

    def test_gif_dimensions(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "a.gif"
        path.write_bytes(b"GIF89a" + struct.pack("<HH", 12, 34) + b"\x00" * 10)
        result = image_mod.inspect(path)
        self.assertIn("12×34", result.text)

    def test_unknown_format_still_ok(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "a.bin"
        path.write_bytes(b"whatever")
        result = image_mod.inspect(path)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["metadata"]["format"], "unknown")

    def test_missing_file(self):
        self.assertFalse(image_mod.inspect("不存在的文件.png").ok)


class DocumentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_txt(self):
        path = self.tmp / "a.txt"
        path.write_text("这是正文内容，用来测试本地解析。", encoding="utf-8")
        result = document_mod.parse(path)
        self.assertTrue(result.ok)
        self.assertIn("这是正文内容", result.text)

    def test_json_and_code(self):
        path = self.tmp / "a.py"
        path.write_text("print('hi')", encoding="utf-8")
        self.assertTrue(document_mod.parse(path).ok)
        data = self.tmp / "a.json"
        data.write_text('{"a": 1}', encoding="utf-8")
        self.assertTrue(document_mod.parse(data).ok)

    def test_docx(self):
        path = self.tmp / "a.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                "<w:document><w:p><w:r><w:t>会议纪要：周三下午两点</w:t></w:r></w:p></w:document>",
            )
        result = document_mod.parse(path)
        self.assertTrue(result.ok)
        self.assertIn("会议纪要", result.text)

    def test_unsupported_extension(self):
        path = self.tmp / "a.xlsx"
        path.write_bytes(b"PK\x03\x04")
        result = document_mod.parse(path)
        self.assertFalse(result.ok)

    def test_missing_file(self):
        self.assertFalse(document_mod.parse(self.tmp / "nope.txt").ok)

    def test_text_is_truncated(self):
        path = self.tmp / "big.txt"
        path.write_text("啊" * 10000, encoding="utf-8")
        result = document_mod.parse(path)
        self.assertLessEqual(len(result.data["text"]), document_mod.MAX_CHARS)
        self.assertTrue(result.data["truncated"])


class VideoTest(unittest.TestCase):
    def test_frame_times_short_video(self):
        times = video_mod.frame_times(10)
        self.assertGreaterEqual(len(times), 2)
        self.assertLessEqual(len(times), 6)
        self.assertTrue(all(0 < moment < 10 for moment in times))

    def test_frame_times_medium_video(self):
        self.assertEqual(len(video_mod.frame_times(120)), 4)

    def test_long_video_not_sampled(self):
        self.assertEqual(video_mod.frame_times(600), [])

    def test_metadata_from_telegram(self):
        result = video_mod.build_result("不存在.mp4", telegram_meta={"duration": 12, "width": 1080, "height": 1920})
        self.assertIn("12.0 秒", result.text)
        self.assertIn("1080×1920", result.text)

    def test_long_video_message(self):
        result = video_mod.build_result("x.mp4", telegram_meta={"duration": 900})
        self.assertIn("太长", result.text)


class RouterDegradeTest(unittest.TestCase):
    """没有本地能力时必须诚实降级，不能假装看到了。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.router = PerceptionRouter(vision=None, use_ocr=False)

    def test_image_without_vision_degrades(self):
        path = write_png(self.tmp / "a.png", 100, 100)
        result = self.router.perceive(PerceptionRequest(source_type="image", path=str(path)))
        self.assertFalse(result.ok)
        self.assertIn("看不到画面", result.text)
        self.assertIn("别假装", result.text)
        self.assertEqual(
            PerceptionRouter.describe_for_context(result), result.text
        )

    def test_document_passes_through(self):
        path = self.tmp / "a.md"
        path.write_text("# 标题\n正文", encoding="utf-8")
        result = self.router.perceive(PerceptionRequest(source_type="document", path=str(path)))
        self.assertTrue(result.ok)
        self.assertIn("正文", result.text)

    def test_voice_without_engine_degrades(self):
        path = self.tmp / "a.ogg"
        path.write_bytes(b"OggS" + b"\x00" * 20)
        result = self.router.perceive(
            PerceptionRequest(source_type="voice", path=str(path), meta={"duration": 14})
        )
        self.assertFalse(result.ok)
        self.assertIn("听不到", result.text)

    def test_video_without_ffmpeg_degrades(self):
        result = self.router.perceive(
            PerceptionRequest(source_type="video", path="x.mp4", meta={"duration": 8})
        )
        self.assertIn("没看到画面", result.text)

    def test_unknown_source(self):
        self.assertFalse(self.router.perceive(PerceptionRequest(source_type="hologram")).ok)


class RouterWithFakeVisionTest(unittest.TestCase):
    class FakeVision:
        def __init__(self, text="一只猫趴在沙发上"):
            self.text = text
            self.calls = 0

        def available(self):
            return True

        def describe(self, path, prompt=""):
            from tools.core.result import ToolResult

            self.calls += 1
            return ToolResult(name="vision", ok=True, text=self.text, source_type="image")

    def test_image_uses_local_vision(self):
        tmp = Path(tempfile.mkdtemp())
        path = write_png(tmp / "cat.png", 200, 200)
        vision = self.FakeVision()
        router = PerceptionRouter(vision=vision, use_ocr=False)
        result = router.perceive(PerceptionRequest(source_type="image", path=str(path)))
        self.assertTrue(result.ok)
        self.assertIn("一只猫趴在沙发上", result.text)
        self.assertEqual(vision.calls, 1)

    def test_video_uses_frames_when_available(self):
        from unittest import mock

        from tools.core.result import ToolResult

        vision = self.FakeVision("画面里是街道")
        router = PerceptionRouter(vision=vision, max_frames=2)
        fake = ToolResult(
            name="video", ok=True, text="一段视频（8.0 秒）",
            data={"frames": ["f1.jpg", "f2.jpg", "f3.jpg"], "metadata": {"duration": 8}},
            source_type="video",
        )
        with mock.patch("tools.perception.video.build_result", return_value=fake):
            result = router.perceive(PerceptionRequest(source_type="video", path="x.mp4"))
        self.assertTrue(result.ok)
        self.assertEqual(vision.calls, 2)
        self.assertIn("街道", result.text)


if __name__ == "__main__":
    unittest.main()
