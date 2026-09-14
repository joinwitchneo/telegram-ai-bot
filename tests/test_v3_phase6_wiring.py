"""Phase 6.14：接线验证（运行入口 / 调度 / observe / dry_run / live / 聊天 0 LLM）。"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import v3_commands                                    # noqa: E402
from core.llm_client import LLMResult                 # noqa: E402
from v3.actions.base import JOURNAL, MESSAGE, STATUS_SIMULATED, STATUS_SENT  # noqa: E402
from v3.scheduler.queue import WakeQueue              # noqa: E402
from v3.scheduler.runner import SchedulerRunner        # noqa: E402
from v3.service import V3Service                      # noqa: E402
from v3.thought.models import Thought                 # noqa: E402

BASE_TIME = datetime.datetime(2026, 9, 14, 10, 0, 0)


class FakeConfig:
    """最小 Config 替身：V3Service 只用这几个读取接口。"""

    def __init__(self, values):
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


class ScriptedClient:
    """假模型：按脚本回放 JSON，记录调用次数。"""

    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls += 1
        return LLMResult(ok=True, content=self.payload, model="fake", tier=tier,
                         input_tokens=10, output_tokens=10)


class FakeBot:
    def __init__(self, service=None):
        self.v3_service = service
        self.sent = []
        self._v3_last_user = ""
        self._v3_emotion_delta = {}
        self._v3_relationship_delta = {}
        self.build_info = {}
        self.conversation = None
        self.client = None

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class RecordingSender:
    def __init__(self):
        self.sent = []

    def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


PROPOSAL = json.dumps({
    "thought_update": {"importance": 0.9},
    "new_thoughts": [{"type": "curiosity", "content": "他考完没有",
                      "topic": "考试", "evidence": ["他明天考试"]}],
    "intention": {"type": "message", "reason": "想问问他", "confidence": 0.9,
                  "content": "考完了记得跟我说一声"},
}, ensure_ascii=False)


def build_service(*, mode="observe", payload=PROPOSAL, sender=None, chat_id=1):
    tmp = Path(tempfile.mkdtemp())
    config = FakeConfig({
        "V3_ENABLED": "true", "V3_MODE": mode, "V3_DATA_DIR": str(tmp / "v3"),
        "V3_ACTION_COGNITIVE_COOLDOWN_MINUTES": "0",
    })
    client = ScriptedClient(payload)
    service = V3Service(config, base_dir=tmp, client=client,
                        environment_sender=sender, chat_id=chat_id)
    return service, client, tmp


def strong_thought(service, **overrides):
    data = dict(content="他明天考试，我想问问", topic="考试", is_unfinished=True,
                importance=0.95, urgency=0.9, novelty=0.8, curiosity=0.9,
                activation=0.9, persistence=0.9, information_gain=0.6,
                relationship_relevance=0.8)
    data.update(overrides)
    thought, _ = service.thoughts.add(Thought(id="", **data))
    return thought


class RunningPathTest(unittest.TestCase):
    def test_bootstrap_observation_becomes_thought_without_llm(self):
        """接线缺口回归：空念头库 + 一条聊天观测 → 下一个检查点必须产生念头（0 token）。"""
        service, client, _ = build_service()
        service.observations.append(user_message="我明天要考试了，有点紧张",
                                    bot_response_excerpt="别紧张")
        result = service.run_cycle(trigger="bootstrap", now=BASE_TIME)
        self.assertGreater(service.thoughts.count(), 0, "聊天观测必须能变成念头，否则系统空转")
        self.assertGreaterEqual(result["cycle"]["thoughts_created"], 1)
        self.assertEqual(client.calls, 0, "念头形成是纯 Python，不允许调用模型")
        self.assertGreaterEqual(len(service.checkpoint.recent(limit=1)[0]["candidates"]), 1)

    def test_observation_is_formed_only_once(self):
        service, _client, _ = build_service()
        service.observations.append(user_message="周末想去爬山", bot_response_excerpt="注意安全")
        service.run_cycle(trigger="t1", now=BASE_TIME)
        first = service.thoughts.count()
        service.run_cycle(trigger="t2", now=BASE_TIME + datetime.timedelta(minutes=60))
        self.assertEqual(service.thoughts.count(), first, "同一条观测不允许反复变成新念头")

    def test_A_cycle_command_goes_through_life_cycle(self):
        """Test A：/cycle 必须进 LifeCycle；旧 CognitiveCycle 一旦被碰到就直接炸。"""
        service, client, _ = build_service()

        def boom(*args, **kwargs):
            raise AssertionError("/cycle 不允许再走 Phase 5 的 CognitiveCycle")

        service.cycle.run = boom
        result = service.run_cycle(trigger="manual", now=BASE_TIME)
        self.assertTrue(result["ran"])
        self.assertEqual(len(service.life.recent(limit=5)), 1)
        self.assertEqual(len(service.checkpoint.recent(limit=5)), 1)

        bot = FakeBot(service)
        self.assertTrue(v3_commands.dispatch(bot, 1, "/cycle", ""))
        self.assertIn("跑完一轮", bot.sent[0][1])

    def test_B_low_value_thought_never_calls_cognition(self):
        """Test B：低价值念头 → 检查点 → 不调用 LLM。"""
        service, client, _ = build_service()
        service.thoughts.add(Thought(id="", content="随口一提", topic="闲聊",
                                     importance=0.1, novelty=0.05, activation=0.1))
        for index in range(3):
            result = service.run_cycle(trigger="t", now=BASE_TIME + datetime.timedelta(minutes=index))
            self.assertFalse(result["triggered"])
            self.assertEqual(result["reason_code"], "threshold_not_met")
        self.assertEqual(client.calls, 0)
        self.assertEqual(service.budget.data["llm_calls"], 0)
        self.assertEqual(service.journal.count(), 0)

    def test_C_high_value_thought_triggers_cognition_and_decision(self):
        """Test C：高价值念头 → 认知 → 决策。"""
        service, client, _ = build_service(mode="observe")
        strong_thought(service)
        result = service.run_cycle(trigger="t", now=BASE_TIME)
        self.assertTrue(result["triggered"])
        self.assertEqual(client.calls, 1, "达到阈值才允许调用一次模型")
        cycle = result["cycle"]
        self.assertEqual(cycle["cognitive"]["intention"]["type"], "message")
        self.assertEqual(cycle["decision"]["action"], JOURNAL, "observe 档只能记日志")
        self.assertEqual(cycle["decision"]["would_action"], MESSAGE)
        self.assertEqual(cycle["thoughts_created"], 1, "提案里的新念头要落地")
        self.assertTrue(cycle["cognition_ran"])

    def test_D_observe_never_really_sends(self):
        """Test D：observe 档下，decision=MESSAGE 也只能是 WouldAction。"""
        sender = RecordingSender()
        service, _client, _ = build_service(mode="observe", sender=sender)
        strong_thought(service)
        result = service.run_cycle(trigger="autonomous", now=BASE_TIME)
        self.assertEqual(result["cycle"]["decision"]["action"], JOURNAL)
        self.assertEqual(result["cycle"]["decision"]["would_action"], MESSAGE)
        self.assertEqual(len(sender.sent), 0, "真机发送方一次都不许被调用")
        self.assertEqual(len(service.environment.sent), 0, "observe 档连模拟发送都不该有")
        self.assertGreaterEqual(service.journal.count(), 1, "要把本来会说的话记进 journal")

    def test_E_dry_run_runs_full_chain_without_real_send(self):
        """Test E：dry_run 走完整 Action 链，但真实发送为 0。"""
        sender = RecordingSender()
        service, _client, _ = build_service(mode="dry_run", sender=sender)
        strong_thought(service)
        result = service.run_cycle(trigger="autonomous", now=BASE_TIME)
        self.assertEqual(result["cycle"]["decision"]["action"], MESSAGE)
        self.assertEqual(result["cycle"]["outcome"]["status"], STATUS_SIMULATED)
        self.assertEqual(len(sender.sent), 0, "dry_run 不允许真的发出去")
        self.assertEqual(len(service.environment.sent), 1, "但要走完环境接口")

    def test_F_live_can_reach_environment_with_fake_sender(self):
        """Test F：live 档可以真正进入 ActionRegistry → Environment（用假环境，不发真实消息）。"""
        sender = RecordingSender()
        service, _client, _ = build_service(mode="live", sender=sender)
        strong_thought(service)
        result = service.run_cycle(trigger="autonomous", now=BASE_TIME)
        self.assertEqual(result["cycle"]["decision"]["action"], MESSAGE)
        self.assertEqual(result["cycle"]["outcome"]["status"], STATUS_SENT)
        self.assertEqual(sender.sent, [(1, "考完了记得跟我说一声")])

    def test_observe_chat_path_adds_zero_v3_llm_calls(self):
        """6.14.8：用户聊天只产生 Observation，V3 的 LLM 调用必须是 0。"""
        service, client, _ = build_service()
        bot = FakeBot(service)
        event = type("E", (), {"name": "UserMessageReceived", "payload": {"text": "在忙吗"}})()
        v3_commands.on_user_message(bot, event)
        response = type("E", (), {"payload": {
            "excerpt": "还行，你呢", "response_mode": "CASUAL", "message_count": 1,
            "chat_id": 1}})()
        v3_commands.on_bot_response(bot, response)
        self.assertEqual(service.observations.count(), 1, "聊天要变成观测")
        self.assertEqual(client.calls, 0, "聊天链路不允许新增 V3 模型调用")
        self.assertEqual(service.budget.data["llm_calls"], 0)


class CycleGuardTest(unittest.TestCase):
    """6.14.6：/cycle 不是绕过机制，必须受预算与冷却约束。"""

    def test_budget_exhausted_is_reported_and_traced(self):
        service, client, _ = build_service()
        service.budget.data["cycles"] = 99
        result = service.run_cycle(trigger="manual", now=BASE_TIME)
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason_code"], "budget_exhausted")
        self.assertEqual(client.calls, 0)
        kinds = [item["kind"] for item in service.trace.recent(limit=10)]
        self.assertIn("cognitive_skipped", kinds)

    def test_cognitive_cooldown_blocks_cognition(self):
        service, client, _ = build_service()
        service.action_budget.cognitive_cooldown_minutes = 60
        strong_thought(service)
        first = service.run_cycle(trigger="t1", now=BASE_TIME)
        self.assertTrue(first["triggered"])
        second = service.run_cycle(trigger="t2", now=BASE_TIME + datetime.timedelta(minutes=5))
        self.assertFalse(second["triggered"])
        self.assertTrue(second["threshold_reached"], "阈值够，只是被冷却挡住")
        self.assertEqual(second["reason_code"], "cooldown")
        self.assertEqual(client.calls, 1, "冷却期内不允许再调用模型")

    def test_same_wake_task_is_not_executed_twice(self):
        tmp = Path(tempfile.mkdtemp())
        queue = WakeQueue(tmp / "wake_queue.json")
        now = datetime.datetime.now()
        task = queue.enqueue(reason="t", earliest_at=now.isoformat(timespec="seconds"))
        calls = []

        def spawn(task_id):
            calls.append(task_id)
            return 0, "", ""

        runner = SchedulerRunner(queue=queue, base_dir=tmp, spawn=spawn,
                                 lease_minutes=30, max_silence_hours=48,
                                 min_interval_minutes=60)
        runner.tick(now=now)
        runner.tick(now=now + datetime.timedelta(seconds=1))
        self.assertEqual(calls, [task["id"]], "同一个 wake 只能执行一次")
        self.assertEqual(queue.all()[0]["status"], "COMPLETED")

    def test_stale_lease_returns_to_pending(self):
        tmp = Path(tempfile.mkdtemp())
        queue = WakeQueue(tmp / "wake_queue.json")
        now = datetime.datetime.now()
        task = queue.enqueue(reason="t", earliest_at=now.isoformat(timespec="seconds"))
        queue.claim(task["id"], lease_minutes=1, now=now)
        self.assertEqual(queue.all()[0]["status"], "RUNNING")
        self.assertEqual(queue.recover_stale(now=now + datetime.timedelta(minutes=5)), 1)
        self.assertEqual(queue.all()[0]["status"], "PENDING")


class SchedulerToLifeCycleTest(unittest.TestCase):
    """6.14.10：wake_queue → Scheduler → cycle_runner → LifeCycle → completed。"""

    def test_cycle_runner_process_runs_life_cycle(self):
        tmp = Path(tempfile.mkdtemp())
        config_path = tmp / "config.env"
        data_dir = (tmp / "v3").as_posix()
        config_path.write_text(
            "\n".join([
                "V3_ENABLED=true", "V3_MODE=observe", f"V3_DATA_DIR={data_dir}",
                "V3_CYCLES_PER_DAY=3", "V3_LLM_CALLS_PER_DAY=3",
                "V3_ACTION_COGNITIVE_COOLDOWN_MINUTES=0",
                "BACKEND=deepseek", "DEEPSEEK_API_KEY=sk-not-used",
                "CHAT_MODEL=deepseek-v4-flash",
            ]), encoding="utf-8")
        queue = WakeQueue(tmp / "v3" / "continuity" / "wake_queue.json")
        now = datetime.datetime.now()

        def spawn(task_id):
            proc = subprocess.run(
                [sys.executable, "-m", "v3.cycle_runner", "--config", str(config_path),
                 "--wake-id", task_id],
                cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
            return proc.returncode, (proc.stdout or "")[-200:], (proc.stderr or "")[-200:]

        task = queue.enqueue(reason="smoke", earliest_at=now.isoformat(timespec="seconds"))
        runner = SchedulerRunner(queue=queue, base_dir=tmp, spawn=spawn,
                                 lease_minutes=30, max_silence_hours=48,
                                 min_interval_minutes=60)
        result = runner.tick(now=now)
        self.assertTrue(result["executed"], "调度器应该执行了这个 wake")
        self.assertTrue(result["executed"][0]["ok"], result["executed"][0])
        self.assertEqual(queue.all()[0]["status"], "COMPLETED")

        # cycle_runner 必须留下 LifeCycle 的记录（而不是 Phase 5 的 cycles.json 而已）
        life_log = Path(tmp / "v3" / "life_cycle.jsonl")
        self.assertTrue(life_log.is_file(), "cycle_runner 应该写 life_cycle.jsonl")
        rows = [json.loads(line) for line in
                life_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "observe")
        self.assertFalse(rows[0]["cognition_ran"], "空观测时不该调用认知器官")
        self.assertFalse(rows[0]["threshold_reached"])
        self.assertEqual(rows[0]["llm_calls"], 0)


class ObservationCommandTest(unittest.TestCase):
    """6.14.5：五个新观察命令都要能用，而且是只读的。"""

    def build(self):
        service, _client, _ = build_service(mode="dry_run")
        thought = strong_thought(service)
        service.run_cycle(trigger="t", now=BASE_TIME)
        return service, FakeBot(service), thought

    def test_thought_command_shows_lifecycle_fields(self):
        service, bot, thought = self.build()
        self.assertTrue(v3_commands.dispatch(bot, 1, "/thought", thought.id))
        text = bot.sent[0][1]
        for label in ("内容：", "生命周期：", "trigger_score=", "证据：", "被激活="):
            self.assertIn(label, text)
        for forbidden in ("思维链", "chain of thought", "raw"):
            self.assertNotIn(forbidden, text)

    def test_checkpoints_triggers_actions_why(self):
        service, bot, thought = self.build()
        for command in ("/checkpoints", "/triggers", "/actions", "/why"):
            self.assertTrue(v3_commands.dispatch(bot, 1, command, ""), command)
        texts = [text for _chat, text in bot.sent]
        self.assertIn("检查点", texts[0])
        self.assertIn("触发", texts[1])
        self.assertIn("行动", texts[2])
        self.assertIn("决策", texts[3])

    def test_why_by_action_id(self):
        service, bot, _ = self.build()
        outcomes = service.outcomes.recent(limit=5)
        if not outcomes:
            self.skipTest("这一轮没有产生行动")
        action_id = outcomes[-1]["action_id"]
        self.assertTrue(v3_commands.dispatch(bot, 1, "/why", action_id))
        self.assertIn(action_id, bot.sent[0][1])

    def test_mode_active_is_alias_of_live(self):
        service, bot, _ = self.build()
        self.assertTrue(v3_commands.dispatch(bot, 1, "/mode", "active"))
        self.assertEqual(service.runtime.mode(), "live")
        report = service.runtime.mode_report()
        self.assertEqual(report["effective_mode"], "live")
        self.assertIn("别名", bot.sent[0][1])
        payload = json.loads((Path(service.runtime.data_dir) / "runtime.json").read_text(
            encoding="utf-8"))
        self.assertEqual(payload["mode"], "live", "写盘必须是规范档位")


if __name__ == "__main__":
    unittest.main()
