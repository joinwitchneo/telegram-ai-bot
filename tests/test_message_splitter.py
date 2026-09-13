"""中文消息拆分硬规则测试（方案第五～十一节）。"""

import unittest

from style.message_splitter import render_messages, split_sentences


class SentenceTest(unittest.TestCase):
    def test_period_splits_and_is_removed(self):
        self.assertEqual(split_sentences("你好。今天怎么样。"), ["你好", "今天怎么样"])

    def test_exclamation_splits_and_keeps(self):
        self.assertEqual(split_sentences("真的好烦！你怎么还没睡！"), ["真的好烦！", "你怎么还没睡！"])

    def test_question_splits_and_keeps(self):
        self.assertEqual(split_sentences("你在干嘛？怎么这么晚还没睡？"), ["你在干嘛？", "怎么这么晚还没睡？"])

    def test_comma_never_splits(self):
        text = "我刚才其实想了很久，但是后来又觉得没必要。"
        self.assertEqual(split_sentences(text), ["我刚才其实想了很久，但是后来又觉得没必要"])

    def test_ellipsis_is_atomic(self):
        self.assertEqual(
            split_sentences("我想了想……还是算了。你觉得呢？"),
            ["我想了想……还是算了", "你觉得呢？"],
        )

    def test_ascii_ellipsis_not_treated_as_periods(self):
        self.assertEqual(
            split_sentences("他叫 Bob... 我觉得挺好。你呢？"),
            ["他叫 Bob... 我觉得挺好", "你呢？"],
        )

    def test_consecutive_marks_are_one_boundary(self):
        self.assertEqual(split_sentences("真的假的？！你认真的？"), ["真的假的？！", "你认真的？"])
        self.assertEqual(split_sentences("什么？！你认真的？"), ["什么？！", "你认真的？"])
        self.assertEqual(split_sentences("不是吧！？！真的假的？"), ["不是吧！？！", "真的假的？"])

    def test_quote_interior_marks_do_not_split_inside(self):
        self.assertEqual(
            split_sentences("你说“真的可以吗？”我觉得可以。"),
            ["你说“真的可以吗？”", "我觉得可以"],
        )

    def test_parentheses_interior_marks(self):
        self.assertEqual(
            split_sentences("他昨天（其实我不确定？）没来。你信吗？"),
            ["他昨天（其实我不确定？）没来", "你信吗？"],
        )

    def test_bracket_types(self):
        for open_mark, close_mark in (("【", "】"), ("[", "]"), ("‘", "’")):
            text = f"他说{open_mark}行吧{close_mark}。我不管了。"
            self.assertEqual(len(split_sentences(text)), 2, text)

    def test_empty_and_whitespace(self):
        self.assertEqual(split_sentences(""), [])
        self.assertEqual(split_sentences("   "), [])

    def test_no_punctuation_stays_one(self):
        self.assertEqual(split_sentences("在的"), ["在的"])


class RenderTest(unittest.TestCase):
    def test_target_one_keeps_single_message(self):
        text = "我刚才想了很久。后来觉得还是算了。"
        self.assertEqual(render_messages(text, target_count=1), ["我刚才想了很久后来觉得还是算了"])

    def test_merges_to_target_count(self):
        text = "我刚才在想这个问题。其实我觉得挺有意思。而且你突然这么问，我有点没想到。"
        out = render_messages(text, target_count=2)
        self.assertEqual(len(out), 2)
        joined = "".join(out)
        self.assertIn("我刚才在想这个问题", joined)
        self.assertIn("有点没想到", joined)

    def test_does_not_force_split_when_fewer_sentences(self):
        """计划要 3 条但只有 2 句 → 不硬拆，就发 2 条。"""
        self.assertEqual(len(render_messages("在。说。", target_count=3)), 2)

    def test_merge_keeps_lengths_reasonable(self):
        text = "第一句还行。第二句也差不多长。第三句稍微长一点点。第四句同样。第五句收尾。"
        out = render_messages(text, target_count=2)
        self.assertEqual(len(out), 2)
        longest, shortest = max(len(item) for item in out), min(len(item) for item in out)
        self.assertLess(longest, shortest * 3)     # 不能出现 10 字配 800 字

    def test_model_line_breaks_are_kept(self):
        text = "在。\n不过 hello world 一般是拿来测试用的，\n你这是把我当编译器了？"
        self.assertEqual(len(render_messages(text, target_count=1)), 3)

    def test_max_messages_respected(self):
        text = "一。二。三。四。五。六。"
        self.assertLessEqual(len(render_messages(text, target_count=6, max_messages=4)), 4)

    def test_empty(self):
        self.assertEqual(render_messages(""), [])

    def test_english_and_numbers_survive(self):
        out = render_messages("Python 3.13 出来了。要不要装？", target_count=2)
        self.assertEqual(out, ["Python 3.13 出来了", "要不要装？"])

    def test_emoji_survive(self):
        out = render_messages("好啊😄。那你去吧。", target_count=2)
        self.assertIn("😄", out[0])


class ValidatorIntegrationTest(unittest.TestCase):
    def test_plan_count_two_splits_paragraph(self):
        from core.response_validator import ResponseValidator

        validator = ResponseValidator()
        result = validator.validate(
            ["我刚才在想这个问题。其实我觉得挺有意思。而且你突然这么问，我有点没想到。"],
            {"message_count": 2, "length": "NORMAL"},
            max_count=2,
        )
        self.assertEqual(len(result.messages), 2)

    def test_plan_count_one_keeps_one(self):
        from core.response_validator import ResponseValidator

        validator = ResponseValidator()
        result = validator.validate(
            ["我刚才想了很久。后来觉得还是算了。"], {"message_count": 1, "length": "SHORT"}
        )
        self.assertEqual(len(result.messages), 1)

    def test_period_removed_question_kept(self):
        from core.response_validator import ResponseValidator

        validator = ResponseValidator()
        result = validator.validate(["你好。今天怎么样。"], {"message_count": 2}, max_count=2)
        self.assertEqual(result.messages, ["你好", "今天怎么样"])


if __name__ == "__main__":
    unittest.main()
