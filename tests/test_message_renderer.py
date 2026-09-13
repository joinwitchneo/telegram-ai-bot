import unittest

from style.message_renderer import (
    MessageRenderer,
    RenderLimits,
    clamp_plan,
    split_messages,
    typing_seconds,
)


class SplitTest(unittest.TestCase):
    def test_multiline_is_each_message(self):
        self.assertEqual(split_messages("在的\n你说\n我听着"), ["在的", "你说", "我听着"])

    def test_single_line_split_by_punctuation(self):
        messages = split_messages("今天真累。你那边呢？我先去洗澡了")
        self.assertEqual(len(messages), 3)

    def test_respects_max_messages(self):
        text = "\n".join(f"第{i}句" for i in range(10))
        messages = split_messages(text, max_messages=3)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0], "第0句")
        self.assertIn("第1句", messages[1])
        self.assertTrue(messages[2].startswith("第2句"))

    def test_long_message_chunked(self):
        messages = split_messages("啊" * 2500, max_messages=4, max_chars=800)
        self.assertTrue(all(len(m) <= 800 for m in messages))

    def test_empty(self):
        self.assertEqual(split_messages("   "), [])

    def test_typing_seconds_within_bounds(self):
        limits = RenderLimits()
        self.assertGreaterEqual(typing_seconds("啊", limits), 0.8)
        self.assertLessEqual(typing_seconds("啊" * 500, limits), 12.0)


class ClampPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.limits = RenderLimits()

    def test_clamps_wild_values(self):
        plan = clamp_plan(
            {"message_count": 15, "pause": 100, "sticker_probability": 0.99, "messages": ["一", "二"]},
            self.limits,
        )
        self.assertEqual(plan["message_count"], 4)
        self.assertEqual(plan["pause"], 8.0)
        self.assertEqual(plan["sticker_probability"], 0.35)
        self.assertTrue(plan["clamped"])

    def test_clamps_low_pause(self):
        self.assertEqual(clamp_plan({"pause": 0.01}, self.limits)["pause"], 0.5)

    def test_truncates_message_length(self):
        plan = clamp_plan({"messages": ["啊" * 2000]}, self.limits)
        self.assertLessEqual(len(plan["messages"][0]), self.limits.max_chars)

    def test_merges_extra_messages(self):
        plan = clamp_plan({"message_count": 2, "messages": ["一", "二", "三", "四"]}, self.limits)
        self.assertEqual(len(plan["messages"]), 2)

    def test_empty_plan_is_safe(self):
        plan = clamp_plan({}, self.limits)
        self.assertEqual(plan["message_count"], 1)
        self.assertEqual(plan["messages"], [])


class RendererTest(unittest.TestCase):
    def test_render_sends_each_message(self):
        sent, sleeps, typing = [], [], []
        renderer = MessageRenderer(
            send=lambda chat, text: sent.append((chat, text)),
            typing=lambda chat: typing.append(chat),
            limits=RenderLimits(),
            sleep=lambda seconds: sleeps.append(seconds),
        )
        result = renderer.render(1, "在的\n你说\n我听着")
        self.assertEqual(result, ["在的", "你说", "我听着"])
        self.assertEqual([t for _, t in sent], ["在的", "你说", "我听着"])
        self.assertEqual(len(typing), 3)
        self.assertGreaterEqual(len(sleeps), 5)

    def test_render_respects_interrupt(self):
        sent = []
        state = {"stop": False}

        def send(chat, text):
            sent.append(text)
            state["stop"] = True

        renderer = MessageRenderer(send=send, sleep=lambda s: None)
        renderer.render(1, "一\n二\n三", interrupt_check=lambda: state["stop"])
        self.assertEqual(sent, ["一"])

    def test_render_plan_input(self):
        sent = []
        renderer = MessageRenderer(send=lambda c, t: sent.append(t), sleep=lambda s: None)
        renderer.render(1, {"message_count": 2, "pause": 1.0, "messages": ["嗨", "在忙吗"]})
        self.assertEqual(sent, ["嗨", "在忙吗"])


class Phase5RenderTest(unittest.TestCase):
    """Phase 5：停顿区间、长度档上限、dry_run。"""

    def test_pause_range_is_respected(self):
        sent, sleeps = [], []
        renderer = MessageRenderer(
            send=lambda c, t: sent.append(t), sleep=lambda s: sleeps.append(s)
        )
        renderer.render(1, {"message_count": 2, "pause": [0.5, 1.0], "messages": ["一", "二"]})
        self.assertEqual(sent, ["一", "二"])
        gap = sleeps[1]
        self.assertGreaterEqual(gap, 0.5)
        self.assertLessEqual(gap, 1.2)

    def test_clamp_plan_keeps_pause_list(self):
        plan = clamp_plan({"pause": [0.1, 100], "messages": ["一"]}, RenderLimits())
        self.assertEqual(plan["pause"], [0.5, 8.0])

    def test_length_cap_limits_message_length(self):
        renderer = MessageRenderer(send=lambda c, t: None, sleep=lambda s: None)
        sent = renderer.render(1, {"length": "ULTRA_SHORT", "messages": ["啊" * 120]})
        self.assertLessEqual(len(sent[0]), 30)

    def test_long_length_allows_more_chars(self):
        renderer = MessageRenderer(send=lambda c, t: None, sleep=lambda s: None)
        sent = renderer.render(1, {"length": "LONG", "messages": ["啊" * 120]})
        self.assertEqual(len(sent[0]), 120)

    def test_dry_run_sends_nothing(self):
        sent, sleeps = [], []
        renderer = MessageRenderer(
            send=lambda c, t: sent.append(t), typing=lambda c: None, sleep=lambda s: sleeps.append(s)
        )
        result = renderer.render(
            1, {"message_count": 2, "pause": [0.5, 1.0], "messages": ["一", "二"]}, dry_run=True
        )
        self.assertEqual(result, ["一", "二"])
        self.assertEqual(sent, [])
        self.assertEqual(sleeps, [])

    def test_gap_never_below_min_pause(self):
        sleeps = []
        renderer = MessageRenderer(send=lambda c, t: None, sleep=lambda s: sleeps.append(s))
        renderer.render(1, {"message_count": 2, "pause": [0.0, 0.1], "messages": ["一", "二"]})
        self.assertGreaterEqual(sleeps[1], 0.5)


if __name__ == "__main__":
    unittest.main()
