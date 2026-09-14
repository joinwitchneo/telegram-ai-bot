"""V3 Telegram 观察命令：只读性、/mode 立即生效、V3 绝不自动发消息。"""

import json
import tempfile
import unittest
from pathlib import Path

import v3_commands
from core.llm_client import LLMResult
from v3.service import V3Service

READONLY_COMMANDS = (
    ("/v3help", ""),
    ("/version", ""),
    ("/wakestatus", ""),
    ("/wakequeue", ""),
    ("/thoughts", ""),
    ("/interests", ""),
    ("/journal", "3"),
    ("/why", ""),
)


class FakeConfig:
    """最小 Config 替身：只实现 V3 用到的读取接口。"""

    def __init__(self, values: dict) -> None:
        self.values = dict(values)

    def get(self, name, default=""):
        return str(self.values.get(name, default))

    def get_int(self, name, default=0):
        try:
            return int(self.values.get(name, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, name, default=0.0):
        try:
            return float(self.values.get(name, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, name, default=False):
        return str(self.values.get(name, default)).lower() in ("1", "true", "yes", "on")


class FakeClient:
    def __init__(self, contents) -> None:
        self.contents = list(contents)
        self.calls = 0

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls += 1
        index = min(self.calls - 1, len(self.contents) - 1)
        return LLMResult(ok=True, content=self.contents[index], model="fake", tier=tier,
                         input_tokens=5, output_tokens=5)


class FakeBot:
    def __init__(self, service=None) -> None:
        self.v3_service = service
        self.sent: list = []
        self.build_info: dict = {}
        self._v3_last_user = ""
        self._v3_emotion_delta: dict = {}
        self._v3_relationship_delta: dict = {}

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def snapshots(root: Path) -> dict:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class V3CommandTest(unittest.TestCase):
    def build(self, *, enabled=True, mode="observe", thoughts=None):
        tmp = Path(tempfile.mkdtemp())
        client = FakeClient(thoughts or [json.dumps({"thoughts": []}, ensure_ascii=False)])
        config = FakeConfig({
            "V3_ENABLED": enabled, "V3_MODE": mode, "V3_DATA_DIR": str(tmp / "v3"),
        })
        service = V3Service(config, base_dir=tmp, client=client)
        return service, FakeBot(service), tmp

    def test_help_works_without_service(self):
        bot = FakeBot(None)
        self.assertTrue(v3_commands.dispatch(bot, 7, "/v3help", ""))
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("V3", bot.sent[0][1])

    def test_disabled_tells_user_how_to_enable(self):
        bot = FakeBot(None)
        self.assertTrue(v3_commands.dispatch(bot, 7, "/wakestatus", ""))
        self.assertIn("未启用", bot.sent[0][1])

    def test_non_v3_command_is_not_swallowed(self):
        bot = FakeBot(None)
        self.assertFalse(v3_commands.dispatch(bot, 7, "/help", ""))
        self.assertEqual(bot.sent, [])

    def test_readonly_commands_do_not_touch_disk(self):
        service, bot, tmp = self.build()
        before = snapshots(tmp)
        for command, rest in READONLY_COMMANDS:
            self.assertTrue(v3_commands.dispatch(bot, 7, command, rest), command)
        self.assertEqual(len(bot.sent), len(READONLY_COMMANDS))
        self.assertEqual(snapshots(tmp), before, "只读命令不允许改盘")

    def test_mode_writes_runtime_json_and_applies_immediately(self):
        service, bot, tmp = self.build()
        self.assertEqual(service.runtime.mode(), "observe")
        self.assertTrue(v3_commands.dispatch(bot, 7, "/mode", "dry_run"))
        self.assertEqual(service.runtime.mode(), "dry_run", "写完必须立即生效，不需要重启")
        payload = json.loads((tmp / "v3" / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(service.runtime.mode_report()["base_mode"], "observe")

        v3_commands.dispatch(bot, 7, "/mode", "wild")
        self.assertEqual(service.runtime.mode(), "dry_run", "非法档位不允许写盘")

    def test_cycle_command_goes_through_budget(self):
        service, bot, _ = self.build()
        service.budget.data["cycles"] = 99
        self.assertTrue(v3_commands.dispatch(bot, 7, "/cycle", ""))
        self.assertIn("没有执行", bot.sent[0][1])
        self.assertEqual(service.budget.data["cycles"], 99, "被拒绝时不能扣预算")

    def test_status_reports_all_three_modes(self):
        service, bot, _ = self.build(mode="dry_run")
        service.runtime.set_runtime_mode("observe", actor="test")
        v3_commands.dispatch(bot, 7, "/wakestatus", "")
        text = bot.sent[0][1]
        for label in ("基础=dry_run", "覆盖=observe", "生效=observe"):
            self.assertIn(label, text)

    # ── /version：我在跟 V2 说话还是跟 V3 说话 ──────────────────────
    def test_version_says_v2_when_v3_disabled(self):
        bot = FakeBot(None)
        self.assertTrue(v3_commands.dispatch(bot, 7, "/version", ""))
        text = bot.sent[0][1]
        self.assertIn("V2", text)
        self.assertIn("V3 未启用", text)

    def test_version_says_v3_with_fingerprint(self):
        service, bot, _ = self.build()
        bot.build_info = {"path": r"C:\x\telegram-bot-v3\bot.py", "sha256": "abcdef0123456789ff",
                          "mtime": "2026-09-13T19:00:00", "pid": 4242,
                          "started_at": "2026-09-13T19:00:01"}
        self.assertTrue(v3_commands.dispatch(bot, 7, "/version", ""))
        text = bot.sent[0][1]
        self.assertIn("在跑的是：V3", text)
        self.assertIn(v3_commands.version_label(), text)
        self.assertIn("生效=observe", text)
        self.assertIn("abcdef012345", text, "指纹要够长，方便和 bot.log 对照")
        self.assertIn("pid=4242", text)
        self.assertIn(str(service.runtime.data_dir), text)

    def test_version_handles_missing_fingerprint_and_alias(self):
        _service, bot, _ = self.build()
        self.assertTrue(v3_commands.dispatch(bot, 7, "/版本", ""))
        self.assertIn("没拿到", bot.sent[0][1])

    def test_version_is_registered_in_telegram_help(self):
        import bot as bot_module
        self.assertIn("/version", bot_module.Bot.COMMANDS)
        self.assertIn("/version", bot_module.Bot.HELP_TEXT)

    # ── 铁律：V3 永不自动发消息 ─────────────────────────────────────
    def test_phase5_compat_cycle_never_sends_telegram_message(self):
        """Phase 5 兼容路径（service.cycle）的行为保持不变。

        Phase 6.14 把默认运行入口换成了 LifeCycle，所以这里显式调用旧实现，
        验证"旧路径也从不发消息、只写 inner_journal"；新路径的对应断言在
        tests/test_v3_phase6_wiring.py。
        """
        service, bot, _ = self.build(thoughts=[json.dumps({"thoughts": [
            {"type": "curiosity", "content": "他今天在忙什么", "topic": "近况",
             "is_unfinished": True, "evidence": ["今天在忙"]},
        ]}, ensure_ascii=False)])
        service.observations.append(user_message="今天在忙", bot_response_excerpt="好")
        result = service.cycle.run(trigger="test")
        self.assertTrue(result["ran"])
        self.assertEqual(bot.sent, [], "一轮认知不允许产生任何对外消息")
        self.assertEqual(service.journal.count(), 1, "输出只能进 inner_journal")

    def test_only_environment_layer_touches_the_outside_world(self):
        """Phase 6 起：V3 允许行动，但**只有 environments/ 能碰外部世界**。

        Core（life_cycle / checkpoint / cognition / policies / thought / …）一律不许直接
        发消息、不许直接联网；actions/ 只允许调用 Environment 接口。
        这条规则替代 Phase 0-5 的"整个 v3 包都不许出现 telegram/send_message"。
        """
        root = Path(v3_commands.__file__).resolve().parent / "v3"
        forbidden_mechanisms = ("api.telegram.org", "sendMessage", "sendPhoto",
                                "requests.post", "urllib.request", "http.client",
                                "socket.socket", "urlopen")
        env_files = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(root).as_posix()
            if rel.startswith("environments/"):
                env_files.append(rel)
                continue
            for token in forbidden_mechanisms:
                self.assertNotIn(token, text, f"{rel} 不允许直接碰外部世界（{token}）")
            if not rel.startswith("actions/"):
                self.assertNotIn("send_message", text,
                                 f"{rel} 只能通过 ActionProvider 行动，不能直接发消息")
        self.assertIn("environments/telegram.py", env_files, "Telegram 适配器必须存在")

    # ── 铁律：指令不许调用模型来回答 ───────────────────────────────
    def test_every_v3_command_costs_zero_model_calls(self):
        """所有 V3 命令都由 Python 直接回答；空观测下 /cycle 也不该碰模型。"""
        service, bot, _ = self.build()
        for command in v3_commands.V3_COMMANDS:
            self.assertTrue(v3_commands.dispatch(bot, 7, command, ""), command)
        self.assertEqual(service.client.calls, 0, "V3 命令一律 0 次模型调用")
        self.assertEqual(len(bot.sent), len(v3_commands.V3_COMMANDS), "每条命令都要有回复")

    def test_version_command_alone_costs_zero_model_calls(self):
        service, bot, _ = self.build()
        for name in ("/version", "/版本"):
            v3_commands.dispatch(bot, 7, name, "")
        self.assertEqual(service.client.calls, 0)
        self.assertEqual(len(bot.sent), 2)

    def test_commands_never_touch_dialogue_pipeline_or_client(self):
        """命令处理器只能拿 bot 上的只读状态，不许伸手到客户端/对话管线。"""
        sentinel = AssertionError("命令不允许使用对话管线或模型客户端")

        class GuardedBot(FakeBot):
            def __getattr__(self, name):
                if name in ("client", "conversation", "perceive", "context_manager"):
                    raise sentinel
                raise AttributeError(name)

        service, _bot, _ = self.build()
        bot = GuardedBot(service)
        for command in v3_commands.V3_COMMANDS:
            v3_commands.dispatch(bot, 7, command, "")
        self.assertTrue(bot.sent)

    def test_v3_commands_source_has_no_model_call_forms(self):
        text = Path(v3_commands.__file__).read_text(encoding="utf-8")
        for token in (".chat(", "LLMClient", "llm_client", "conversation", "_ask_cheap", "tier="):
            self.assertNotIn(token, text, f"v3_commands.py 不应出现 {token}")


if __name__ == "__main__":
    unittest.main()
