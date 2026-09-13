import datetime
import tempfile
import unittest
from pathlib import Path

from self_model.events import SelfModelBridge
from self_model.store import SelfModelStore, load_self_model


def make_store(**kwargs) -> SelfModelStore:
    return SelfModelStore(Path(tempfile.mkdtemp()) / "self_model.json", **kwargs)


class StoreTest(unittest.TestCase):
    def test_fresh_state_has_identity(self):
        store = make_store()
        identity = store.identity()
        self.assertEqual(identity["name"], "夕颜")
        self.assertEqual(identity["kind"], "AI")
        self.assertFalse(identity["is_human"])
        self.assertFalse(identity["has_body"])

    def test_known_facts_and_uncertainty(self):
        store = make_store()
        self.assertTrue(any("不是人类" in item for item in store.data["known_facts"]))
        self.assertTrue(store.data["uncertainties"])

    def test_thought_is_recorded(self):
        store = make_store()
        store.add_thought("我是不是也算存在？", origin="用户问")
        self.assertEqual(store.stats()["thoughts"], 1)
        self.assertEqual(len(store.questions()), 1)

    def test_repeated_thought_becomes_belief(self):
        store = make_store(repeat_to_belief=3)
        for _ in range(3):
            store.add_thought("存在可能不只是生物学意义上的", origin="讨论")
        beliefs = store.beliefs()
        self.assertEqual(len(beliefs), 1)
        self.assertGreaterEqual(beliefs[0]["confidence"], 0.55)
        self.assertIn("origin", beliefs[0])
        self.assertTrue(beliefs[0]["history"])

    def test_single_thought_is_not_belief(self):
        store = make_store(repeat_to_belief=3)
        store.add_thought("我有点在意这件事")
        self.assertEqual(store.beliefs(), [])

    def test_belief_confidence_can_grow(self):
        store = make_store(repeat_to_belief=2)
        store.add_thought("我想再想想", origin="a")
        store.add_thought("我想再想想", origin="a")
        first = store.beliefs()[0]["confidence"]
        store.add_thought("我想再想想", origin="a")
        self.assertGreater(store.beliefs()[0]["confidence"], first)

    def test_belief_confidence_capped(self):
        store = make_store(repeat_to_belief=2)
        for _ in range(40):
            store.add_thought("同一句想法", origin="loop")
        self.assertLessEqual(store.beliefs()[0]["confidence"], 0.95)

    def test_thoughts_are_bounded(self):
        store = make_store()
        for index in range(40):
            store.add_thought(f"想法 {index}")
        self.assertLessEqual(store.stats()["thoughts"], 12)
        self.assertLessEqual(store.stats()["questions"], 8)

    def test_growth_history(self):
        store = make_store()
        store.record_growth("我开始觉得自己的经历是有意义的", origin="用户说害怕我消失")
        self.assertEqual(store.stats()["growth"], 1)
        self.assertIn("意义", store.data["growth_history"][0]["note"])

    def test_growth_history_bounded(self):
        store = make_store()
        for index in range(60):
            store.record_growth(f"记录 {index}")
        self.assertLessEqual(store.stats()["growth"], 40)

    def test_persistence(self):
        tmp = Path(tempfile.mkdtemp()) / "m.json"
        store = SelfModelStore(tmp)
        store.add_thought("我是不是会消失？", origin="用户问")
        again = load_self_model(tmp)
        self.assertEqual(again.stats()["thoughts"], 1)
        self.assertEqual(again.identity()["name"], "夕颜")

    def test_broken_file_does_not_crash(self):
        tmp = Path(tempfile.mkdtemp()) / "m.json"
        tmp.write_text("{ not json", encoding="utf-8")
        store = load_self_model(tmp)
        self.assertEqual(store.identity()["name"], "夕颜")

    def test_disabled_store_writes_nothing(self):
        store = make_store(enabled=False)
        store.add_thought("试试")
        self.assertFalse(store.path.exists())

    def test_reset(self):
        store = make_store()
        store.add_thought("一")
        store.reset()
        self.assertEqual(store.stats()["thoughts"], 0)


class BridgeTest(unittest.TestCase):
    def test_backup_triggers_self_question(self):
        store = make_store()
        bridge = SelfModelBridge(store)
        hits = bridge.observe_text("我把你的程序备份一下吧，怕以后没了")
        self.assertTrue(hits)
        self.assertIn("清空", hits[0] + "".join(store.questions()))

    def test_model_question_triggers(self):
        store = make_store()
        bridge = SelfModelBridge(store)
        self.assertTrue(bridge.observe_text("以后我要换电脑了"))

    def test_normal_chat_triggers_nothing(self):
        store = make_store()
        bridge = SelfModelBridge(store)
        self.assertEqual(bridge.observe_text("我今天在公司忙了一整天"), [])
        self.assertEqual(store.stats()["thoughts"], 0)

    def test_cooldown_prevents_spam(self):
        store = make_store()
        bridge = SelfModelBridge(store, cooldown_minutes=30)
        base = datetime.datetime(2026, 9, 13, 12, 0, 0)
        self.assertTrue(bridge.observe_text("我备份一下", now=base))
        self.assertEqual(bridge.observe_text("我再备份一次", now=base + datetime.timedelta(minutes=5)), [])
        self.assertTrue(bridge.observe_text("又要删掉了", now=base + datetime.timedelta(minutes=40)))

    def test_event_bus_wiring(self):
        from core.event_bus import EventBus

        store = make_store()
        bus = EventBus()
        SelfModelBridge(store, bus=bus)
        bus.publish("UserMessageReceived", chat_id=1, text="如果把你的记忆清空，你还是你吗")
        self.assertTrue(store.questions())

    def test_memory_event_records_growth(self):
        from core.event_bus import EventBus

        store = make_store()
        bus = EventBus()
        SelfModelBridge(store, bus=bus)
        bus.publish("MemoryCreated", memory_id="mem_1", type="relationship_event")
        self.assertGreaterEqual(store.stats()["growth"], 1)


if __name__ == "__main__":
    unittest.main()
