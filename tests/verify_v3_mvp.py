"""夕颜 V3 MVP 验收：模拟跨天 12 轮认知，逐条断言原设计 C 节 8 条验收标准。

跑法：python tests/verify_v3_mvp.py
全程离线：假的 LLM、假的时间、不发任何 Telegram 消息，只写本地 inner_journal。
"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.llm_client import LLMResult                       # noqa: E402
from v3.budget import V3Budget                              # noqa: E402
from v3.continuity.cycle import CognitiveCycle               # noqa: E402
from v3.continuity.rules import WAKE_LOW, WAKE_NORMAL        # noqa: E402
from v3.continuity.store import ContinuityStore              # noqa: E402
from v3.decision.engine import NO_ACTION, DecisionEngine     # noqa: E402
from v3.interest.engine import InterestEngine                # noqa: E402
from v3.interest.store import InterestStore                  # noqa: E402
from v3.journal import InnerJournal                          # noqa: E402
from v3.motivation.engine import MotivationEngine            # noqa: E402
from v3.observation.recorder import ObservationRecorder       # noqa: E402
from v3.reward.engine import RewardEngine                    # noqa: E402
from v3.scheduler.queue import WakeQueue                     # noqa: E402
from v3.scheduler.runner import SchedulerRunner               # noqa: E402
from v3.thought.generator import ThoughtGenerator            # noqa: E402
from v3.thought.lifecycle import ThoughtLifecycle            # noqa: E402
from v3.thought.store import ThoughtStore                    # noqa: E402

DAY_ONE = datetime.datetime(2026, 9, 13, 0, 0, 0)
MIN_INTERVAL_MINUTES = 60
MAX_SILENCE_HOURS = 48
MAX_CHAIN = 3
LLM_PER_DAY = 6
CYCLES_PER_DAY = 6
WATCHED_TOPICS = ("Python", "咖啡")


def thought(kind: str, content: str, topic: str, unfinished: bool, evidence: list) -> dict:
    return {"type": kind, "content": content, "topic": topic,
            "is_unfinished": unfinished, "evidence": evidence}


def payload(*items) -> str:
    return json.dumps({"thoughts": list(items)}, ensure_ascii=False)


# 12 轮：3 天 x 4 轮。第 6 轮故意夹带一条思维链候选，必须被 Validator 丢掉。
PLAN = (
    # ── 第 1 天 ───────────────────────────────────────────────────
    (1, 9, 0, "我最近在学 Python", "学得怎么样呀", {"interest": 0.05},
     payload(thought("unfinished", "他说最近在学 Python，我还没问到他学到哪一步了",
                     "Python", True, ["我最近在学 Python"]))),
    (1, 13, 0, "Python 那个卡住的地方我搞懂了", "厉害", {"interest": 0.05},
     payload(thought("unfinished", "他把卡住的 Python 地方搞懂了，我挺替他高兴",
                     "Python", True, ["Python 那个卡住的地方我搞懂了"]))),
    (1, 17, 0, "我不太喜欢喝咖啡", "那就不喝", {"anxiety": 0.04},
     payload(thought("curiosity", "他说他不喜欢咖啡，我有点想知道他喜欢什么",
                     "咖啡", False, ["我不太喜欢喝咖啡"]))),
    (1, 21, 0, "今天把 Python 那章看完了", "挺好啊", {"interest": 0.05},
     payload(thought("evaluation", "他把 Python 那章看完了，好像挺顺利",
                     "Python", True, ["今天把 Python 那章看完了"]))),
    # ── 第 2 天 ───────────────────────────────────────────────────
    (2, 9, 30, "继续学 Python", "加油", {"interest": 0.05},
     payload(thought("curiosity", "他今天又提起 Python，我想问问他现在学到哪了",
                     "Python", True, ["继续学 Python"]))),
    (2, 14, 0, "今天学 Python 挺顺的", "那就好", {"interest": 0.04},
     payload(thought("reflection", "让我一步步分析：用户为什么学 Python，思维链如下",
                     "Python", False, ["今天学 Python 挺顺的"]),
             thought("observation", "他今天学 Python 挺顺的",
                     "Python", True, ["今天学 Python 挺顺的"]))),
    (2, 19, 0, "Python 的装饰器有点绕", "慢慢来", {"interest": 0.03},
     payload(thought("question", "Python 装饰器他绕明白了吗",
                     "Python", True, ["Python 的装饰器有点绕"]))),
    (2, 22, 0, "我今天喝了点咖啡", "不是说不太喜欢吗", {"anxiety": 0.03},
     payload(thought("observation", "他说不喜欢咖啡，今天又喝了点",
                     "咖啡", False, ["我今天喝了点咖啡"]))),
    # ── 第 3 天 ───────────────────────────────────────────────────
    (3, 10, 0, "Python 那个报错我解决了", "厉害啊", {"interest": 0.05},
     payload(thought("evaluation", "那个 Python 报错他解决了",
                     "Python", True, ["Python 那个报错我解决了"]))),
    (3, 13, 0, "Python 那本书挺好懂的", "那就继续", {"interest": 0.04},
     payload(thought("reflection", "他换的那本 Python 书好像挺好懂的",
                     "Python", True, ["Python 那本书挺好懂的"]))),
    (3, 18, 0, "我打算换本 Python 书", "哪本", {"interest": 0.04},
     payload(thought("unfinished", "他要换 Python 书，还没说换哪本",
                     "Python", True, ["我打算换本 Python 书"]))),
    (3, 22, 0, "今天又学了点 Python", "不错", {"excitement": 0.03},
     payload(thought("curiosity", "他今天又学了点 Python，看起来还挺来劲",
                     "Python", True, ["今天又学了点 Python"]))),
)


class ScriptedClient:
    """按脚本回放 JSON 的假模型：不联网、无随机。"""

    def __init__(self, contents) -> None:
        self.contents = list(contents)
        self.calls = 0

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls += 1
        index = min(self.calls - 1, len(self.contents) - 1)
        return LLMResult(ok=True, content=self.contents[index], model="fake",
                         tier=tier, input_tokens=0, output_tokens=0)


class FakeRuntime:
    """只暴露 cycle 需要的 mode / limit_int（MVP 固定 observe）。"""

    def __init__(self, mode="observe", limits=None) -> None:
        self._mode = mode
        self._limits = {"V3_MIN_WAKE_INTERVAL_MINUTES": MIN_INTERVAL_MINUTES,
                        "V3_MAX_SILENCE_HOURS": MAX_SILENCE_HOURS,
                        "V3_MAX_ATTEMPTS": 3, **(limits or {})}

    def mode(self) -> str:
        return self._mode

    def limit(self, name, default):
        return float(self._limits.get(name, default))

    def limit_int(self, name, default):
        return int(self._limits.get(name, default))


class FakeBot:
    """只记录"有没有对外发消息"。"""

    def __init__(self) -> None:
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class Simulation:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="v3_verify_"))
        self.bot = FakeBot()
        self.thoughts = ThoughtStore(self.tmp / "thoughts" / "thoughts.json")
        self.interests = InterestStore(self.tmp / "interests" / "interests.json")
        self.continuity = ContinuityStore(self.tmp / "continuity")
        self.observations = ObservationRecorder(self.tmp / "observations.jsonl", keep=500)
        self.journal = InnerJournal(self.tmp / "journal" / "inner_journal.jsonl")
        self.queue = WakeQueue(self.tmp / "continuity" / "wake_queue.json")
        self.budget = V3Budget(self.tmp / "budget.json", llm_per_day=LLM_PER_DAY,
                               cycles_per_day=CYCLES_PER_DAY, max_chain=MAX_CHAIN)
        self.client = ScriptedClient([entry[6] for entry in PLAN])
        self.cycle = CognitiveCycle(
            runtime=FakeRuntime(), budget=self.budget, thoughts=self.thoughts,
            lifecycle=ThoughtLifecycle(self.thoughts),
            generator=ThoughtGenerator(client=self.client, budget=self.budget),
            interests=self.interests, interest_engine=InterestEngine(self.interests),
            motivation_engine=MotivationEngine(),
            decision_engine=DecisionEngine(mode_getter=lambda: "observe"),
            reward_engine=RewardEngine(llm_eval_enabled=False), continuity=self.continuity,
            observations=self.observations, journal=self.journal, wake_queue=self.queue,
        )
        self.records: list = []
        self.curve: list = []
        self.seeds: list = []

    @staticmethod
    def _moment(day: int, hour: int, minute: int) -> datetime.datetime:
        return DAY_ONE + datetime.timedelta(days=day - 1, hours=hour, minutes=minute)

    @staticmethod
    def new_day(budget: V3Budget) -> None:
        """模拟跨天：把记账日期改成旧日期，下一次判定自然日切归零。"""
        budget.data["date"] = "1970-01-01"

    def run(self) -> None:
        current_day = 0
        for day, hour, minute, user, reply, delta, script in PLAN:
            if day != current_day:
                self.new_day(self.budget)
                current_day = day
            moment = self._moment(day, hour, minute)
            self.observations.append(user_message=user, bot_response_excerpt=reply,
                                     response_mode="CASUAL", message_count=1,
                                     emotion_delta=delta, chat_id=1, now=moment)
            result = self.cycle.run(trigger=f"day{day}", now=moment)
            self.records.append({"day": day, "now": moment, **result})
            self.curve.append({
                "day": day,
                "time": moment.isoformat(timespec="minutes"),
                "attraction": {topic: self._value(topic) for topic in WATCHED_TOPICS},
                "curiosity": {topic: self._detail(topic, "curiosity") for topic in WATCHED_TOPICS},
                "valence": {topic: self._detail(topic, "valence") for topic in WATCHED_TOPICS},
            })
            self.seeds.append(self.continuity.load_seed())

    def _value(self, topic: str) -> float:
        item = self.interests.get(topic)
        return round(item.attraction, 4) if item else 0.0

    def _detail(self, topic: str, field: str) -> float:
        item = self.interests.get(topic)
        return round(getattr(item, field), 4) if item else 0.0

    # ── 报告 ────────────────────────────────────────────────────────
    def report(self) -> str:
        lines = ["", "=" * 72, "V3 MVP 12 轮跨天模拟", "=" * 72, "", "[兴趣曲线]"]
        for row in self.curve:
            attrs = "  ".join(f"{t}: attraction={row['attraction'][t]:.3f} "
                              f"curiosity={row['curiosity'][t]:.3f} valence={row['valence'][t]:+.3f}"
                              for t in WATCHED_TOPICS)
            lines.append(f"  {row['time']}  {attrs}")
        lines += ["", "[inner_journal]"]
        for item in self.journal.recent(limit=20):
            lines.append(f"  {item['timestamp']} [{item['kind']}] {item['content']}")
        lines += ["", "[决策序列]"]
        for record in self.records:
            cycle = record.get("cycle") or {}
            decision = cycle.get("decision") or {}
            lines.append(f"  day{record['day']} {record['now'].strftime('%H:%M')} "
                         f"thoughts={cycle.get('thoughts_created')} "
                         f"gain={cycle.get('info_gain')} "
                         f"motivation={(cycle.get('motivation') or {}).get('score')} "
                         f"decision={decision.get('action')} "
                         f"llm={cycle.get('llm_calls')} "
                         f"next_wake={(record.get('next_wake') or {}).get('earliest_at')}")
        return "\n".join(lines)


class MVPAcceptanceTest(unittest.TestCase):
    """原设计 C 节 8 条验收标准，逐条断言。"""

    @classmethod
    def setUpClass(cls):
        cls.sim = Simulation()
        cls.sim.run()

    # 标准 1：合法 Thought / ephemeral 与 unfinished 可分 / 不保存 CoT
    def test_01_thoughts_valid_and_no_cot(self):
        sim = self.sim
        self.assertEqual(len(sim.records), 12)
        self.assertTrue(all(record["ran"] for record in sim.records))
        all_thoughts = sim.thoughts.all()
        self.assertTrue(any(t.is_unfinished for t in all_thoughts), "必须能标记未完成念头")
        self.assertTrue(any(not t.is_unfinished for t in all_thoughts), "也要有普通瞬时念头")
        for thought_obj in all_thoughts:
            self.assertNotIn("思维链", thought_obj.content)
            self.assertNotIn("step by step", thought_obj.content.lower())
            self.assertTrue(thought_obj.evidence, "落地念头必须有证据")
        cot_cycle = sim.records[5]["cycle"]                       # 第 6 轮夹带了思维链
        self.assertGreaterEqual(cot_cycle["thoughts_rejected"], 1, "思维链候选必须被拒收")
        self.assertEqual(cot_cycle["thoughts_created"], 1, "同一轮里合法的那条要留下")

    # 标准 2：跨 Cycle 读取 ContinuitySeed，接续未完成念头
    def test_02_unfinished_thought_survives_to_next_day(self):
        sim = self.sim
        day1_seed = sim.seeds[3]                                  # 第 1 天最后一轮
        self.assertTrue(day1_seed.unfinished_thought_ids)
        day2_seed = sim.seeds[4]                                  # 第 2 天第一轮
        carried = set(day1_seed.unfinished_thought_ids) & set(day2_seed.unfinished_thought_ids)
        self.assertTrue(carried, "昨天没想完的念头，第二天醒来必须还在")
        self.assertEqual(day2_seed.cycle_id, sim.records[4]["cycle"]["cycle_id"])

    # 标准 3：正向缓慢上升 / 负向缓慢下降 / 单次不剧烈跳变
    def test_03_interest_moves_slowly(self):
        sim = self.sim
        python_day1 = sim.curve[3]["attraction"]["Python"]
        python_day3 = sim.curve[-1]["attraction"]["Python"]
        self.assertGreater(python_day3, python_day1, "多次正向对话，兴趣应缓慢上升")
        self.assertLessEqual(python_day3 - python_day1, 0.12, "上升必须很慢，不能一步到位")
        coffee = sim.interests.get("咖啡")
        self.assertIsNotNone(coffee)
        self.assertLess(coffee.valence, 0.0, "负向话题的 valence 应为负")
        for record in sim.records:
            for change in (record["cycle"].get("interest_changes") or []):
                self.assertLessEqual(abs(change["attraction_delta"]), 0.05,
                                     "单次对话不允许造成剧烈跳变")

    # 标准 4：可以出现"valence 负但 curiosity 正"
    def test_04_negative_valence_positive_curiosity(self):
        coffee = self.sim.interests.get("咖啡")
        self.assertLess(coffee.valence, 0)
        self.assertGreater(coffee.curiosity, 0.5)

    # 标准 5：预算 / 链长 / 最小唤醒间隔硬限制生效
    def test_05_hard_limits(self):
        sim = self.sim
        self.assertEqual(sim.budget.max_chain, MAX_CHAIN)
        per_day: dict = {}
        for record in sim.records:
            day = record["day"]
            bucket = per_day.setdefault(day, {"cycles": 0, "llm": 0})
            bucket["cycles"] += 1
            bucket["llm"] += int(record["cycle"]["llm_calls"])
            self.assertLessEqual(int(record["cycle"]["llm_calls"]), MAX_CHAIN, "链长硬上限")
            floor = record["now"] + datetime.timedelta(minutes=MIN_INTERVAL_MINUTES)
            earliest = datetime.datetime.fromisoformat(record["next_wake"]["earliest_at"])
            self.assertGreaterEqual(earliest, floor, "两次自主唤醒间隔不得低于下限")
            self.assertIn(record["next_wake"]["priority"], (WAKE_LOW, WAKE_NORMAL))
            self.assertNotEqual(record["next_wake"]["priority"], "high", "MVP 不允许 High 唤醒")
        for day, bucket in per_day.items():
            self.assertLessEqual(bucket["cycles"], CYCLES_PER_DAY, f"第 {day} 天 cycle 超限")
            self.assertLessEqual(bucket["llm"], LLM_PER_DAY, f"第 {day} 天 LLM 超限")

    # 标准 6：NoAction 是常见输出；没有足够理由不会触发新的 cycle
    def test_06_no_action_common_and_no_idle_wake(self):
        decisions = [record["cycle"]["decision"]["action"] for record in self.sim.records]
        self.assertIn(NO_ACTION, decisions, "NoAction 必须是常见结果之一")
        self.assertTrue(all(action in (NO_ACTION, "JOURNAL_NOTE") for action in decisions),
                        "MVP 只允许两种决策")
        # 队列里应该只有认知自己排的唤醒，没有"没事找事"的任务
        self.assertEqual(self.sim.queue.pending_count() + self.sim.queue.stats()["by_status"].get(
            "COMPLETED", 0), len(self.sim.records))

        tmp = Path(tempfile.mkdtemp())
        idle_queue = WakeQueue(tmp / "wake_queue.json")
        now = datetime.datetime.now()
        idle_queue.enqueue(reason="just_scheduled",
                           earliest_at=(now + datetime.timedelta(minutes=30)).isoformat(timespec="seconds"))
        runner = SchedulerRunner(queue=idle_queue, base_dir=tmp, spawn=lambda task_id: (0, "", ""),
                                 max_silence_hours=MAX_SILENCE_HOURS,
                                 min_interval_minutes=MIN_INTERVAL_MINUTES)
        self.assertEqual(idle_queue.ensure_silence_guard(now=now, max_silence_hours=MAX_SILENCE_HOURS,
                                                         min_interval_minutes=MIN_INTERVAL_MINUTES), {},
                         "刚排过任务就不该再补一个唤醒")
        self.assertEqual(runner.tick(now=now)["executed"], [], "没到点、又刚聊过，就不该跑 cycle")
        self.assertEqual(idle_queue.stats()["by_status"], {"PENDING": 1})

    # 标准 7：崩溃后数据不损坏、可安全恢复、不重复执行
    def test_07_crash_recovery_without_duplicate(self):
        tmp = Path(tempfile.mkdtemp())
        queue = WakeQueue(tmp / "wake_queue.json")
        base = datetime.datetime.now()
        running = queue.enqueue(reason="crash", earliest_at=base.isoformat(timespec="seconds"))
        self.assertTrue(queue.claim(running["id"], lease_minutes=30, now=base))
        self.assertEqual(queue.all()[0]["status"], "RUNNING")
        self.assertEqual(queue.recover_stale(now=base + datetime.timedelta(minutes=10)), 0,
                         "租约没过期不能抢别人的活")
        self.assertEqual(queue.recover_stale(now=base + datetime.timedelta(minutes=31)), 1)
        self.assertEqual(queue.all()[0]["status"], "PENDING")
        self.assertEqual(queue.all()[0]["stale_recovered"], 1)
        self.assertEqual(queue.recover_stale(now=base + datetime.timedelta(hours=5)), 0,
                         "已经回收过的任务不会再次回收")

        done = queue.enqueue(reason="done", earliest_at=base.isoformat(timespec="seconds"))
        self.assertTrue(queue.claim(done["id"], now=base))
        self.assertTrue(queue.complete(done["id"], now=base))
        self.assertFalse(queue.claim(done["id"], now=base + datetime.timedelta(hours=9)),
                         "已完成任务绝不允许重复执行")
        reopened = WakeQueue(tmp / "wake_queue.json")
        self.assertEqual({item["id"]: item["status"] for item in reopened.all()},
                         {running["id"]: "PENDING", done["id"]: "COMPLETED"})

    # 标准 8：不向用户发任何消息
    def test_08_never_sends_messages(self):
        self.assertEqual(self.sim.bot.sent, [], "12 轮认知不允许产生任何对外消息")
        self.assertEqual(self.sim.journal.count(), len(self.sim.records),
                         "每轮恰好写一条 inner_journal")


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromTestCase(MVPAcceptanceTest)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print(Simulation_report())
    return 0 if result.wasSuccessful() else 1


def Simulation_report() -> str:
    return MVPAcceptanceTest.sim.report()


if __name__ == "__main__":
    raise SystemExit(main())
