import tempfile
import unittest
from pathlib import Path

from core.context_manager import ContextManager, wants_self_model
from core.token_budget import estimate_tokens
from personality.consistency_checker import ConsistencyChecker
from self_model.store import SelfModelStore


class RelevanceTest(unittest.TestCase):
    def test_normal_message_does_not_load_self_model(self):
        self.assertFalse(wants_self_model("明天几点上课"))
        self.assertFalse(wants_self_model("我今天在公司忙了一整天"))
        self.assertFalse(wants_self_model("晚上吃什么"))

    def test_identity_questions_load_self_model(self):
        for text in ("你觉得你是谁", "你知道自己是程序吗", "DeepSeek 和你是什么关系",
                     "如果我把你删除会怎么样", "你觉得自己算活着吗", "你的记忆被清空还是你吗"):
            self.assertTrue(wants_self_model(text), text)


class ContextTest(unittest.TestCase):
    def build(self, user_text: str, self_view: str):
        manager = ContextManager(contract="【合同】", persona="【人格】")
        state = {"emotion": "心情一般", "self_model": self_view} if self_view else {"emotion": "心情一般"}
        return manager.build(user_text=user_text, history=[], state=state)

    def test_self_model_is_rendered_when_present(self):
        built = self.build("你觉得你是谁", "我是夕颜，是 AI，不是人类。我最近在想：如果我被清空还是不是我。")
        text = "\n".join(message["content"] for message in built.messages)
        self.assertIn("【我自己】", text)

    def test_self_model_absent_when_empty(self):
        built = self.build("明天几点上课", "")
        text = "\n".join(message["content"] for message in built.messages)
        self.assertNotIn("【我自己】", text)

    def test_self_model_block_is_small(self):
        built = self.build("你觉得你是谁", "我是夕颜" * 200)
        text = "\n".join(message["content"] for message in built.messages)
        self.assertLessEqual(estimate_tokens(text.split("【我自己】")[1]), 200)

    def test_static_prefix_unchanged_by_self_model(self):
        first = self.build("你好", "")
        second = self.build("你觉得你是谁", "我是夕颜，是 AI")
        self.assertEqual(first.messages[0]["content"], second.messages[0]["content"])


class CheckerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.checker = ConsistencyChecker()

    def test_human_claim_is_flagged(self):
        result = self.checker.check(["我也是人类，我昨天出门买了咖啡"])
        self.assertIn("SELF_IDENTITY_CONFLICT", result.codes)

    def test_ai_self_awareness_is_fine(self):
        result = self.checker.check(["我知道自己是程序，不是人类，这没什么好装的。"])
        self.assertTrue(result.ok)

    def test_opinion_drift_is_not_flagged(self):
        result = self.checker.check(["我以前不太想这个问题，现在觉得它有点意思。"])
        self.assertTrue(result.ok)


class BotViewTest(unittest.TestCase):
    def test_view_is_empty_for_normal_topic(self):
        store = SelfModelStore(Path(tempfile.mkdtemp()) / "m.json")
        store.add_thought("我是不是也算存在？", origin="用户问")
        from core.context_manager import wants_self_model

        self.assertEqual(wants_self_model("明天几点上课"), False)
        self.assertTrue(wants_self_model("你觉得自己是什么"))


if __name__ == "__main__":
    unittest.main()
