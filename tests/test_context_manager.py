import datetime
import unittest

from core.context_manager import ContextManager, detect_mode
from core.token_budget import TokenBudget, estimate_tokens

CONTRACT = "【系统合同】\n你是角色，直接输出消息，禁止表格。"
PERSONA = "【人格】\n嘴硬心软，说话短。"


def make_history(count: int, size: int = 20) -> list[dict]:
    history = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        history.append({"role": role, "content": f"消息{index}-" + "字" * size})
    return history


def make_manager(**kwargs) -> ContextManager:
    params = dict(
        contract=CONTRACT,
        persona=PERSONA,
        token_budget=TokenBudget(max_context_tokens=6000, daily_token_budget=100000),
        max_context_tokens=6000,
    )
    params.update(kwargs)
    return ContextManager(**params)


class ModeTest(unittest.TestCase):
    def test_default_casual(self):
        self.assertEqual(detect_mode("在吗"), "CASUAL")
        self.assertEqual(detect_mode("今天天气不错"), "CASUAL")

    def test_emotional(self):
        for text in ("我今天好烦", "有点难过", "压力好大，快撑不住了"):
            self.assertEqual(detect_mode(text), "EMOTIONAL", text)

    def test_task(self):
        for text in ("帮我查一下这个", "提醒我明天买牛奶", "整理一下这段话"):
            self.assertEqual(detect_mode(text), "TASK", text)

    def test_deep_by_length_and_marker(self):
        self.assertEqual(detect_mode("其实我想跟你说一件事"), "DEEP")
        self.assertEqual(detect_mode("字" * 90), "DEEP")

    def test_deep_by_continuous_long_talk(self):
        history = [{"role": "user", "content": "字" * 70} for _ in range(4)]
        self.assertEqual(detect_mode("然后呢", history), "DEEP")

    def test_unknown_history_safe(self):
        self.assertEqual(detect_mode("嗯", None), "CASUAL")
        self.assertEqual(detect_mode("", []), "CASUAL")


class WindowTest(unittest.TestCase):
    def test_casual_window(self):
        built = make_manager().build(user_text="在吗", history=make_history(40))
        self.assertEqual(built.mode, "CASUAL")
        self.assertEqual(built.stats["history_count"], 12)

    def test_deep_window(self):
        built = make_manager().build(user_text="其实我想认真跟你聊聊", history=make_history(40))
        self.assertEqual(built.mode, "DEEP")
        self.assertEqual(built.stats["history_count"], 25)

    def test_emotional_window(self):
        built = make_manager().build(user_text="我今天好烦", history=make_history(40))
        self.assertEqual(built.mode, "EMOTIONAL")
        self.assertEqual(built.stats["history_count"], 25)

    def test_task_window(self):
        built = make_manager().build(user_text="帮我查一下火车票", history=make_history(40))
        self.assertEqual(built.mode, "TASK")
        self.assertEqual(built.stats["history_count"], 12)

    def test_custom_windows(self):
        manager = make_manager(windows={"CASUAL": 4, "DEEP": 6})
        built = manager.build(user_text="在吗", history=make_history(20))
        self.assertEqual(built.stats["history_count"], 4)

    def test_window_limited_by_budget(self):
        """条数是默认窗口，不是绝对保证：历史太长时会被预算裁掉。"""
        manager = make_manager(max_context_tokens=400, min_history=2)
        # 显式指定 CASUAL，避免"连续长消息"被规则判成 DEEP（那是另一条测试）
        built = manager.build(user_text="在吗", history=make_history(40, size=60), mode="CASUAL")
        self.assertLess(built.stats["history_count"], 12)
        self.assertGreaterEqual(built.stats["history_count"], 2)


class InvariantTest(unittest.TestCase):
    def test_l0_and_l1_survive_tiny_budget(self):
        manager = make_manager(max_context_tokens=10, min_history=0)
        built = manager.build(user_text="在吗", history=make_history(20))
        names = [b.name for b in built.blocks]
        self.assertIn("L0", names)
        self.assertIn("L1", names)
        self.assertIn(CONTRACT.split("\n")[0], built.messages[0]["content"])
        self.assertIn("嘴硬心软", built.messages[0]["content"])

    def test_static_prefix_is_byte_stable(self):
        manager = make_manager()
        prefixes = []
        for index in range(3):
            built = manager.build(
                user_text=f"第{index}次问",
                history=make_history(index),
                now=datetime.datetime(2026, 9, 13, 10 + index, 0),
            )
            prefixes.append(built.messages[0]["content"])
        self.assertEqual(prefixes[0], prefixes[1])
        self.assertEqual(prefixes[1], prefixes[2])

    def test_dynamic_content_after_static_prefix(self):
        built = make_manager().build(user_text="在吗", history=make_history(4))
        self.assertEqual(built.messages[0]["role"], "system")
        self.assertNotIn("【当前时间】", built.messages[0]["content"])
        self.assertNotIn("【当前模式】", built.messages[0]["content"])
        self.assertEqual(built.messages[1]["role"], "system")
        self.assertIn("【当前模式】", built.messages[1]["content"])
        self.assertIn("【当前时间】", built.messages[1]["content"])

    def test_static_prefix_has_no_time_or_random(self):
        built = make_manager().build(user_text="在吗")
        prefix = built.messages[0]["content"]
        for forbidden in ("2026-", "req_", "【当前时间】"):
            self.assertNotIn(forbidden, prefix)

    def test_empty_l2_l3_still_builds(self):
        built = make_manager().build(user_text="在吗", history=make_history(4))
        names = [b.name for b in built.blocks]
        self.assertNotIn("L2", names)
        self.assertNotIn("L3", names)
        for message in built.messages:
            self.assertNotIn("None", message["content"])
        self.assertEqual(built.messages[-1]["content"], "在吗")

    def test_state_and_memory_rendered_when_present(self):
        built = make_manager().build(
            user_text="在吗",
            history=make_history(2),
            state={"emotion": "有点累", "relationship": "熟悉", "topic": "工作"},
            memory_blocks=[{"content": "用户喜欢喝美式", "type": "preference"}],
        )
        names = [b.name for b in built.blocks]
        self.assertIn("L2", names)
        self.assertIn("L3", names)
        dynamic = built.messages[1]["content"]
        self.assertIn("当前情绪：有点累", built.dynamic_text)
        self.assertIn("用户喜欢喝美式", built.dynamic_text)
        # 这两段必须真的进入发给模型的消息，而不只是统计里
        self.assertIn("当前情绪：有点累", dynamic)
        self.assertIn("用户喜欢喝美式", dynamic)
        self.assertIn("【当前模式】", dynamic)

    def test_user_message_is_last(self):
        built = make_manager().build(user_text="最后一句", history=make_history(6))
        self.assertEqual(built.messages[-1], {"role": "user", "content": "最后一句"})

    def test_token_stats_present_for_all_layers(self):
        built = make_manager().build(user_text="在吗", history=make_history(4))
        tokens = built.stats["tokens"]
        for layer in ("L0", "L1", "L2", "L3", "L4", "user"):
            self.assertIn(layer, tokens)
        self.assertGreater(tokens["L0"], 0)
        self.assertGreater(tokens["L1"], 0)
        self.assertEqual(built.stats["mode"], "CASUAL")
        self.assertEqual(built.stats["max_context_tokens"], 6000)


class TrimTest(unittest.TestCase):
    def test_old_history_dropped_first(self):
        # 注：MAX_CONTEXT_TOKENS 有 500 的下限保护，所以用足够长的历史来触发裁剪
        manager = make_manager(max_context_tokens=500, min_history=2)
        history = make_history(40, size=80)
        built = manager.build(user_text="在吗", history=history, mode="CASUAL")
        kept_contents = [m["content"] for m in built.messages if m["role"] != "system"]
        self.assertIn(history[-1]["content"], kept_contents)      # 最近的消息一定保留
        self.assertNotIn(history[0]["content"], kept_contents)     # 最旧的会被裁掉
        self.assertGreater(built.stats["history_dropped"], 0)
        self.assertTrue(any(item.startswith("L4:") for item in built.dropped))

    def test_min_history_floor(self):
        manager = make_manager(max_context_tokens=200, min_history=4)
        built = manager.build(user_text="在吗", history=make_history(30, size=50))
        self.assertGreaterEqual(built.stats["history_count"], 4)

    def test_context_stays_under_budget(self):
        manager = make_manager(max_context_tokens=1200, min_history=0)
        built = manager.build(user_text="在吗", history=make_history(60, size=50))
        self.assertLessEqual(built.stats["total_context_tokens"], 1200 + 400)

    def test_l2_l3_dropped_before_history_is_touched(self):
        manager = make_manager(max_context_tokens=900)
        built = manager.build(
            user_text="在吗",
            history=make_history(10, size=40),
            state={"emotion": "字" * 200},
            memory_blocks=[{"content": "字" * 400}],
        )
        self.assertTrue(built.dropped)
        self.assertIn("L3", built.dropped)


if __name__ == "__main__":
    unittest.main()
