import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.interaction import game as game_mod
from tools.interaction import poll as poll_mod
from tools.interaction import voice_reply as voice_mod
from tools.interaction.sticker import StickerBook


class GameTest(unittest.TestCase):
    def test_dice_range(self):
        for _ in range(20):
            result = game_mod.GameHub().dice()
            self.assertTrue(1 <= result.data["rolls"][0] <= 6)

    def test_dice_count_clamped(self):
        self.assertEqual(len(game_mod.GameHub().dice(99).data["rolls"]), 6)

    def test_coin(self):
        self.assertIn(game_mod.GameHub().coin().data["side"], ("正面", "反面"))

    def test_guess_hand_draw(self):
        with mock.patch("random.choice", side_effect=lambda seq: seq[0]):
            result = game_mod.GameHub().guess_hand("石头剪刀布")
        self.assertEqual(result.data["verdict"], "平了")

    def test_number_game_flow(self):
        hub = game_mod.GameHub()
        start = hub.start_number(1)
        self.assertTrue(start.ok)
        self.assertTrue(hub.active(1))
        low = hub.guess(1, 1)
        self.assertIn(low.data["action"], ("hint", "win"))
        target = hub._guesses.get(1, {}).get("target")
        if target:
            win = hub.guess(1, int(target))
            self.assertEqual(win.data["action"], "win")
            self.assertFalse(hub.active(1))

    def test_guess_without_start(self):
        self.assertIn("还没开始", game_mod.GameHub().guess(9, 5).text)

    def test_handle_text_dispatch(self):
        hub = game_mod.GameHub()
        self.assertTrue(game_mod.handle_text("猜数字", chat_id=1, hub=hub).ok)
        self.assertTrue(game_mod.handle_text("抛硬币", chat_id=2, hub=hub).ok)
        self.assertTrue(game_mod.handle_text("掷骰子", chat_id=3, hub=hub).ok)
        self.assertFalse(game_mod.handle_text("今天天气不错", chat_id=4, hub=hub).ok)


class StickerTest(unittest.TestCase):
    def build_book(self, items=None):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "stickers.json"
        path.write_text(json.dumps(items or {"开心": ["a.webp", "b.webp"], "生气": ["c.webp"]}, ensure_ascii=False),
                        encoding="utf-8")
        return StickerBook(path, max_recent=1)

    def test_available_and_count(self):
        book = self.build_book()
        self.assertTrue(book.available())
        self.assertEqual(book.count(), 3)

    def test_empty_book(self):
        tmp = Path(tempfile.mkdtemp())
        book = StickerBook(tmp / "none.json")
        self.assertFalse(book.available())
        self.assertFalse(book.pick("开心").ok)

    def test_pick_avoids_repeats(self):
        book = self.build_book()
        first = book.pick("开心").data["path"]
        second = book.pick("开心").data["path"]
        self.assertNotEqual(first, second)

    def test_alias_resolution(self):
        book = self.build_book()
        self.assertEqual(book.resolve_tag("happy"), "开心")
        self.assertEqual(book.resolve_tag("生气"), "生气")

    def test_unknown_tag_falls_back(self):
        book = self.build_book()
        self.assertTrue(book.pick("莫名其妙的情绪").ok)


class PollTest(unittest.TestCase):
    def test_parse(self):
        result = poll_mod.parse("发起投票 晚饭吃什么 面 饭 沙拉")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["question"], "晚饭吃什么")
        self.assertEqual(result.data["options"], ["面", "饭", "沙拉"])

    def test_parse_without_prefix(self):
        self.assertFalse(poll_mod.parse("晚饭吃什么").ok)

    def test_parse_needs_two_options(self):
        self.assertFalse(poll_mod.parse("发起投票 只有一个选项").ok)

    def test_options_capped(self):
        result = poll_mod.parse("发起投票 选一个 " + " ".join(f"选项{index}" for index in range(20)))
        self.assertLessEqual(len(result.data["options"]), 10)


class VoiceReplyTest(unittest.TestCase):
    def test_no_engine_is_honest(self):
        with mock.patch("tools.interaction.voice_reply.which", return_value=""):
            tts = voice_mod.LocalTTS()
            self.assertFalse(tts.available())
            result = tts.synthesize("你好")
        self.assertFalse(result.ok)
        self.assertIn("TTS", result.error)

    def test_piper_path(self):
        tmp = Path(tempfile.mkdtemp())
        model = tmp / "voice.onnx"
        model.write_bytes(b"x")
        output = tmp / "out.mp3"

        def fake_run(*args, **kwargs):
            output.write_bytes(b"audio")

            class Done:
                returncode = 0

            return Done()

        with mock.patch("tools.interaction.voice_reply.which", return_value="C:/piper.exe"), \
                mock.patch("tools.interaction.voice_reply.subprocess.run", side_effect=fake_run):
            tts = voice_mod.LocalTTS(piper_model=str(model))
            self.assertEqual(tts.provider(), "piper")
            result = tts.synthesize("你好", out_path=output)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["provider"], "piper")

    def test_disabled(self):
        tts = voice_mod.LocalTTS(enabled=False)
        self.assertFalse(tts.available())


if __name__ == "__main__":
    unittest.main()
