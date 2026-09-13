import unittest

from personality.consistency_checker import ConsistencyChecker


def signals(intimacy: float = 0.05) -> dict:
    return {"signals": {"relationship": {"intimacy": intimacy, "familiarity": 0.05}}}


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self.checker = ConsistencyChecker()

    def codes(self, messages, **kwargs) -> list[str]:
        return self.checker.check(messages, **kwargs).codes

    # ── 正常回复不该被误判 ──────────────────────────────────────────
    def test_normal_reply_passes(self):
        result = self.checker.check(["在的，你说"], user_text="在吗")
        self.assertTrue(result.ok)
        self.assertEqual(result.issues, [])

    def test_empty_reply_passes_through(self):
        self.assertTrue(self.checker.check([]).ok)

    def test_disabled_checker_passes_everything(self):
        checker = ConsistencyChecker(enabled=False)
        self.assertTrue(checker.check(["感谢您的分享"]).ok)

    # ── 八类 AI 味 ──────────────────────────────────────────────────
    def test_customer_service_tone(self):
        self.assertIn("CS_TONE", self.codes(["感谢你的分享，我很高兴能够帮助你。"]))

    def test_summary_tone(self):
        self.assertIn("SUMMARY_TONE", self.codes(["综上来看，你今天的主要问题是休息不够。"]))

    def test_bullet_summary_tone(self):
        self.assertIn("SUMMARY_TONE", self.codes(["1. 你先休息\n2. 然后我们再说"]))

    def test_echo_user(self):
        codes = self.codes(["你今天考试挂了啊"], user_text="我今天考试挂了")
        self.assertIn("ECHO_USER", codes)

    def test_over_complete(self):
        long_reply = "嗯" * 200
        self.assertIn("OVER_COMPLETE", self.codes([long_reply], user_text="你吃了吗"))

    def test_ultra_short_plan_over_complete(self):
        plan = {"length": "ULTRA_SHORT"}
        self.assertIn(
            "OVER_COMPLETE",
            self.codes(["我懂你的意思了，不过这件事还是得慢慢来，你先别急，等明天再看看情况吧"], plan=plan),
        )

    def test_over_polite(self):
        self.assertIn("OVER_POLITE", self.codes(["请问您现在方便吗"]))

    def test_persona_conflict_on_intimacy(self):
        codes = self.codes(["亲爱的，我在呢"], state=signals(intimacy=0.05))
        self.assertIn("PERSONA_CONFLICT", codes)

    def test_no_persona_conflict_when_close(self):
        codes = self.codes(["亲爱的，我在呢"], state=signals(intimacy=0.7))
        self.assertNotIn("PERSONA_CONFLICT", codes)

    def test_memory_conflict(self):
        memories = [{"content": "用户正在学习Python"}]
        codes = self.codes(["你不是完全没学过Python吗"], memories=memories)
        self.assertIn("MEMORY_CONFLICT", codes)

    def test_fabrication(self):
        self.assertIn("FABRICATION", self.codes(["我刚才去楼下买了杯咖啡"]))

    def test_fabrication_not_triggered_by_hypothesis(self):
        self.assertNotIn("FABRICATION", self.codes(["我要是能去买杯咖啡就好了"]))

    # ── 阈值与重试提示 ──────────────────────────────────────────────
    def test_severity_threshold(self):
        checker = ConsistencyChecker(max_severity=0.3)
        result = checker.check(["请问您现在方便吗"])
        self.assertFalse(result.ok)
        self.assertGreaterEqual(result.severity, 0.4)

    def test_severity_is_max_of_issues(self):
        result = self.checker.check(["我刚才去买了杯咖啡，感谢你的分享"])
        self.assertAlmostEqual(result.severity, 0.8, places=2)

    def test_correction_instruction_mentions_issues(self):
        result = self.checker.check(["感谢你的分享，我很高兴能够帮助你。"])
        text = self.checker.correction_instruction(result.issues)
        self.assertIn("客服腔", text)
        self.assertTrue(text.startswith("重写"))

    def test_correction_instruction_handles_unknown(self):
        text = self.checker.correction_instruction([])
        self.assertIn("像真人", text)


if __name__ == "__main__":
    unittest.main()
