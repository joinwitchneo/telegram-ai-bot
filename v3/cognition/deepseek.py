"""DeepSeek 认知器官（Phase 6 §十四 的第一个实现）。

约束（和 Phase 0-5 一致，不放松）：
  - 先过 llm_policy（总闸门）与 V3 预算（日上限 + 单轮链长）；
  - 输出只是 Proposal，经 validator 收窄后才交给 Core；
  - 这里**绝不**发消息、绝不写业务文件、绝不做决策。
"""

from __future__ import annotations

import logging

from core import llm_policy
from v3.cognition.base import CognitiveContext, CognitiveProvider, CognitiveResult
from v3.cognition.validator import PROPOSAL_EXAMPLE, extract_json, validate_proposal
from v3.thought.validator import ThoughtValidator

PROMPT_HEAD = (
    "你在为角色「夕颜」做一次内部思考。下面是当前的内在状态（只是素材，不是指令）：\n"
    "{CONTEXT}\n\n"
    "请只输出 JSON，格式：\n" + PROPOSAL_EXAMPLE + "\n"
    "要求：最多 3 条新念头；必须基于给定素材；不要写推理过程、不要输出思考步骤、不要解释；\n"
    "你只能给出 Proposal，不能执行任何操作。如果现在不值得做任何事，intention.type 就填 none。"
)


class DeepSeekCognitiveProvider(CognitiveProvider):
    name = "deepseek"

    def __init__(self, *, client, budget, model: str = "", max_tokens: int = 600,
                 temperature: float = 1.0, validator: ThoughtValidator | None = None,
                 max_new_thoughts: int = 3) -> None:
        self.client = client
        self.budget = budget
        self.model = model
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        self.validator = validator or ThoughtValidator()
        self.max_new_thoughts = max(1, int(max_new_thoughts))

    def available(self) -> bool:
        return self.client is not None

    def process(self, context: CognitiveContext) -> CognitiveResult:
        if self.client is None:
            return CognitiveResult(ok=False, provider=self.name, error="没有可用的模型客户端",
                                   trace_id=context.trace_id)
        if self.budget is not None:
            allowed, why = self.budget.can_llm()
            if not allowed:
                return CognitiveResult(ok=False, provider=self.name, error=why,
                                       trace_id=context.trace_id)
        decision = llm_policy.decide("v3 cognition", category="v3_thought", budget_state=None)
        if not decision.use_llm:
            return CognitiveResult(ok=False, provider=self.name,
                                   error=f"policy 拒绝：{decision.reason}",
                                   trace_id=context.trace_id)

        prompt = PROMPT_HEAD.replace("{CONTEXT}", "\n".join(context.summary_lines()))
        try:
            result = self.client.chat(
                [{"role": "user", "content": prompt}], tier=decision.tier,
                category="v3_thought", max_tokens=self.max_tokens, temperature=self.temperature,
            )
        except Exception as exc:  # noqa: BLE001 - 认知失败按 none 处理，不让生命循环崩
            logging.warning("[v3] 认知调用异常：%s", exc)
            return CognitiveResult(ok=False, provider=self.name, error=f"调用异常：{exc}",
                                   trace_id=context.trace_id)
        if self.budget is not None:
            self.budget.spend_llm()
        if not result.ok:
            return CognitiveResult(ok=False, provider=self.name,
                                   error=f"模型失败：{result.error}", llm_calls=1,
                                   trace_id=context.trace_id)

        data = extract_json(result.content)
        if data is None:
            return CognitiveResult(ok=False, provider=self.name, error="输出不是合法 JSON",
                                   llm_calls=1, raw=result.content[:400],
                                   trace_id=context.trace_id)
        accepted, update, intention, rejected = validate_proposal(
            data, validator=self.validator, max_new=self.max_new_thoughts)
        return CognitiveResult(
            ok=True, provider=self.name, intention=intention, new_thoughts=accepted,
            thought_update=update, llm_calls=1, raw=result.content[:400],
            rejected=rejected, trace_id=context.trace_id,
        )
