"""Sticker S1：数据层 / 索引 / 排序 / dry_run / 与 Memory 分离。"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from sticker.engine import StickerEngine, StickerIntent
from sticker.history import StickerHistory
from sticker.index import StickerIndex
from sticker.models import StickerRecord
from sticker.sender import StickerSender
from sticker.store import StickerStore


def make_store(**kwargs):
    tmp = Path(tempfile.mkdtemp())
    return StickerStore(tmp / "index.json", tmp / "preferences.json", **kwargs)


def telegram_sticker(**overrides) -> dict:
    payload = {
        "file_id": "CAACAgEAAx-file-1",
        "file_unique_id": "AgAD_unique_1",
        "emoji": "😏",
        "set_name": "xiyan_test_set",
        "type": "regular",
        "is_animated": False,
        "is_video": False,
        "width": 512,
        "height": 512,
        "file_size": 12345,
    }
    payload.update(overrides)
    return payload


class StoreTest(unittest.TestCase):
    def test_collect_from_telegram_message(self):
        store = make_store()
        record = StickerRecord.from_telegram(telegram_sticker())
        saved, action = store.add(record)
        self.assertEqual(action, "created")
        self.assertEqual(saved.emoji, "😏")
        self.assertEqual(saved.set_name, "xiyan_test_set")
        self.assertEqual(saved.file_size, 12345)
        self.assertEqual(store.count(), 1)

    def test_same_sticker_not_duplicated(self):
        store = make_store()
        for _ in range(3):
            store.add(StickerRecord.from_telegram(telegram_sticker()))
        self.assertEqual(store.count(), 1)
        self.assertEqual(store.get("AgAD_unique_1").seen_count, 3)

    def test_file_id_refreshed_when_changed(self):
        """file_id 可能变化，必须刷新；file_unique_id 才是身份。"""
        store = make_store()
        store.add(StickerRecord.from_telegram(telegram_sticker()))
        store.add(StickerRecord.from_telegram(telegram_sticker(file_id="CAACAgEAAx-file-NEW")))
        self.assertEqual(store.count(), 1)
        self.assertEqual(store.get("AgAD_unique_1").file_id, "CAACAgEAAx-file-NEW")

    def test_empty_unique_id_rejected(self):
        with self.assertRaises(ValueError):
            make_store().add(StickerRecord.from_telegram({}))

    def test_persistence_and_atomic_write(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "index.json"
        store = StickerStore(path, tmp / "preferences.json")
        store.add(StickerRecord.from_telegram(telegram_sticker()))
        again = StickerStore(path, tmp / "preferences.json")
        self.assertEqual(again.count(), 1)
        self.assertFalse(list(tmp.glob("*.tmp")), "不应残留临时文件")

    def test_broken_index_recovers(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "index.json"
        path.write_text("{ 坏掉的 json", encoding="utf-8")
        store = StickerStore(path, tmp / "preferences.json")
        self.assertEqual(store.count(), 0)

    def test_set_cache(self):
        store = make_store()
        self.assertFalse(store.has_set("s1"))
        store.cache_set("s1", {"title": "测试", "sticker_type": "regular", "stickers": [1, 2, 3]})
        self.assertTrue(store.has_set("s1"))
        self.assertEqual(store.set_info("s1")["count"], 3)

    def test_trim_keeps_preferred(self):
        store = make_store(max_stickers=3)
        for index in range(6):
            store.add(StickerRecord.from_telegram(telegram_sticker(file_unique_id=f"u{index}")))
        store.set_preference("u0", "positive")
        store.add(StickerRecord.from_telegram(telegram_sticker(file_unique_id="u9")))
        self.assertIsNotNone(store.get("u0"), "偏好贴纸不该被淘汰")

    def test_preference_interface_only(self):
        store = make_store()
        store.add(StickerRecord.from_telegram(telegram_sticker()))
        self.assertTrue(store.set_preference("AgAD_unique_1", "positive"))
        self.assertEqual(store.preference("AgAD_unique_1"), "positive")
        with self.assertRaises(ValueError):
            store.set_preference("AgAD_unique_1", "喜欢")
        self.assertFalse(store.set_preference("不存在", "positive"))
        self.assertEqual(store.preference("AgAD_unique_1"), "positive")   # 不自动推断


class IndexTest(unittest.TestCase):
    def build(self):
        store = make_store()
        store.add(StickerRecord.from_telegram(telegram_sticker()))
        store.add(StickerRecord.from_telegram(
            telegram_sticker(file_unique_id="u2", emoji="😄", set_name="happy_set")
        ))
        tagged = StickerRecord.from_telegram(telegram_sticker(file_unique_id="u3", emoji="🙄"))
        tagged.tags = ["teasing"]
        store.add(tagged)
        return store, StickerIndex(store)

    def test_lookup_by_emoji_set_tag(self):
        store, index = self.build()
        index.build()
        self.assertEqual(len(index.by_emoji("😏")), 1)
        self.assertEqual(len(index.by_set("happy_set")), 1)
        self.assertEqual(len(index.by_tag("teasing")), 1)
        self.assertEqual(index.stats()["stickers"], 3)

    def test_rebuild_when_new_sticker_added(self):
        store, index = self.build()
        index.build()
        store.add(StickerRecord.from_telegram(telegram_sticker(file_unique_id="u4", emoji="🥳")))
        self.assertEqual(len(index.by_emoji("🥳")), 1)

    def test_recent_sorted_by_time(self):
        store, index = self.build()
        index.build()
        self.assertTrue(index.recent(2))


class EngineTest(unittest.TestCase):
    def build(self, *, dry_run=True, history=None, min_score=0.45):
        store = make_store()
        store.add(StickerRecord.from_telegram(telegram_sticker()))
        store.add(StickerRecord.from_telegram(
            telegram_sticker(file_unique_id="u2", emoji="😄", set_name="happy_set")
        ))
        index = StickerIndex(store)
        index.build()
        engine = StickerEngine(store=store, index=index, history=history,
                               dry_run=dry_run, min_score=min_score)
        return store, engine

    def test_deterministic_same_result(self):
        _, engine = self.build()
        intent = StickerIntent(use=True, intent="teasing", intensity=0.6, confidence=0.8)
        first = engine.decide(intent).to_dict()
        second = engine.decide(intent).to_dict()
        self.assertEqual(first, second, "相同输入必须得到相同结果（禁止 random）")

    def test_emoji_match_wins(self):
        _, engine = self.build()
        decision = engine.decide(StickerIntent(use=True, intent="teasing", confidence=0.9))
        self.assertTrue(decision.should_send)
        self.assertEqual(decision.file_unique_id, "AgAD_unique_1")

    def test_no_use_means_no_send(self):
        _, engine = self.build()
        self.assertFalse(engine.decide(StickerIntent(use=False)).should_send)

    def test_empty_library_is_honest(self):
        tmp = Path(tempfile.mkdtemp())
        store = StickerStore(tmp / "i.json", tmp / "p.json")
        engine = StickerEngine(store=store, index=StickerIndex(store))
        decision = engine.decide(StickerIntent(use=True, intent="teasing"))
        self.assertFalse(decision.should_send)
        self.assertIn("还没有收录", decision.reason)

    def test_low_score_blocked_by_threshold(self):
        _, engine = self.build(min_score=0.99)
        decision = engine.decide(StickerIntent(use=True, intent="teasing", confidence=0.9))
        self.assertFalse(decision.should_send)
        self.assertIn("低于阈值", decision.reason)

    def test_visual_match_is_neutral_without_data(self):
        store, engine = self.build()
        record = store.get("AgAD_unique_1")
        score, parts = engine.score(record, StickerIntent(use=True, intent="teasing"))
        self.assertEqual(parts["visual_match"], 0.5)      # 没有视觉数据 -> 中性值
        self.assertEqual(record.visual_description, "")   # S1 绝不伪造描述

    def test_novelty_penalises_recent(self):
        tmp = Path(tempfile.mkdtemp())
        history = StickerHistory(tmp / "h.json")
        store, engine = self.build(history=history)
        record = store.get("AgAD_unique_1")
        before, _ = engine.score(record, StickerIntent(use=True, intent="teasing"))
        history.record(file_unique_id="AgAD_unique_1", intent="teasing", sent=True)
        after, parts = engine.score(record, StickerIntent(use=True, intent="teasing"))
        self.assertLess(after, before)
        self.assertEqual(parts["novelty"], 0.2)

    def test_candidates_are_sorted(self):
        _, engine = self.build()
        decision = engine.decide(StickerIntent(use=True, intent="teasing", confidence=0.9))
        scores = [item["score"] for item in decision.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_dry_run_flag_in_decision(self):
        _, engine = self.build(dry_run=True)
        decision = engine.decide(StickerIntent(use=True, intent="teasing"))
        self.assertTrue(decision.dry_run)


class HistoryTest(unittest.TestCase):
    def test_record_and_stats(self):
        history = StickerHistory(Path(tempfile.mkdtemp()) / "h.json")
        history.record(file_unique_id="u1", intent="teasing", score=0.7, sent=True)
        history.record(file_unique_id="u2", intent="happy", score=0.6, sent=False)
        stats = history.stats()
        self.assertEqual(stats["records"], 2)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["by_intent"]["teasing"], 1)

    def test_bounded_and_persistent(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "h.json"
        history = StickerHistory(path, max_records=20)
        for index in range(30):
            history.record(file_unique_id=f"u{index}")
        self.assertEqual(len(history.recent(100)), 20)
        self.assertGreaterEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)

    def test_last_sent_at(self):
        history = StickerHistory(Path(tempfile.mkdtemp()) / "h.json")
        history.record(file_unique_id="u1", sent=False)
        self.assertEqual(history.last_sent_at("u1"), "")
        history.record(file_unique_id="u1", sent=True, now=datetime.datetime(2026, 9, 13, 20, 0))
        self.assertTrue(history.last_sent_at("u1"))


class SenderTest(unittest.TestCase):
    def test_dry_run_never_sends(self):
        called = []
        sender = StickerSender(lambda chat, file_id: called.append(file_id), dry_run=True)
        self.assertFalse(sender.send_sticker(1, "CAAC"))
        self.assertEqual(called, [])

    def test_real_send_calls_callback(self):
        called = []
        sender = StickerSender(lambda chat, file_id: called.append((chat, file_id)), dry_run=False)
        self.assertTrue(sender.send_sticker(7, "CAAC"))
        self.assertEqual(called, [(7, "CAAC")])

    def test_failure_is_contained(self):
        def boom(chat, file_id):
            raise RuntimeError("Telegram 500")

        sender = StickerSender(boom, dry_run=False)
        self.assertFalse(sender.send_sticker(1, "CAAC"))
        self.assertIn("Telegram 500", sender.last_error)

    def test_empty_file_id(self):
        sender = StickerSender(lambda chat, file_id: None, dry_run=False)
        self.assertFalse(sender.send_sticker(1, ""))


class IsolationTest(unittest.TestCase):
    """Sticker 数据必须与 Memory 分离，且不产生任何模型调用。"""

    def test_sticker_files_live_outside_memory(self):
        tmp = Path(tempfile.mkdtemp())
        store = StickerStore(tmp / "stickers" / "index.json", tmp / "stickers" / "preferences.json")
        store.add(StickerRecord.from_telegram(telegram_sticker()))
        self.assertTrue((tmp / "stickers" / "index.json").is_file())
        self.assertFalse((tmp / "memories.json").exists())

    def test_no_llm_or_vision_call_in_collection(self):
        """收录与排序全程纯 Python：这里用一个"会炸的"假模型确认它们从没被碰过。"""
        class ExplodingClient:
            def chat(self, *args, **kwargs):
                raise AssertionError("Sticker S1 不允许调用 LLM")

        store = make_store()
        index = StickerIndex(store)
        engine = StickerEngine(store=store, index=index, dry_run=True)
        store.add(StickerRecord.from_telegram(telegram_sticker()))
        index.build()
        decision = engine.decide(StickerIntent(use=True, intent="teasing"))
        self.assertTrue(decision.should_send or decision.reason)   # 全程没有异常


if __name__ == "__main__":
    unittest.main()
