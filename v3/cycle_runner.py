"""一次性认知进程：python -m v3.cycle_runner --wake-id <id>

执行一次 CognitiveCycle 后退出；不常驻、不持有队列锁。
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bot import Config, DATA_DIR  # noqa: E402
from core.llm_client import LLMClient  # noqa: E402
from core.token_budget import TokenBudget  # noqa: E402
from core.usage_logger import UsageLogger  # noqa: E402
from v3.service import V3Service  # noqa: E402


def build_service(config_path: Path, *, with_client: bool = True) -> V3Service:
    config = Config(config_path)
    usage = UsageLogger(DATA_DIR / config.get("USAGE_FILE", "usage.json"))
    budget = TokenBudget(
        max_context_tokens=config.get_int("MAX_CONTEXT_TOKENS", 6000),
        max_output_tokens=config.get_int("MAX_OUTPUT_TOKENS", 400),
        daily_token_budget=config.get_int("DAILY_TOKEN_BUDGET", 300000),
    )
    client = None
    if with_client:
        client = LLMClient(
            backend=config.get("BACKEND", "deepseek").lower(),
            api_key=config.get("DEEPSEEK_API_KEY"),
            base_url=config.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            models={
                "cheap": config.get("V3_LLM_MODEL") or config.get("EXTRACTION_MODEL", "deepseek-flash"),
                "main": config.get("CHAT_MODEL", "deepseek-v4-flash"),
                "strong": config.get("STRONG_MODEL", ""),
            },
            fallback_order=("strong", "main", "cheap"),
            usage_logger=usage,
            token_budget=budget,
        )
    return V3Service(config, base_dir=BASE_DIR, client=client, budget_global=budget)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="夕颜 V3 单次认知周期")
    parser.add_argument("--wake-id", default="", help="来自 wake_queue 的任务 id（仅作标记）")
    parser.add_argument("--config", default=str(BASE_DIR / "config.env"))
    parser.add_argument("--trigger", default="scheduler")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = build_service(Path(args.config))
    if not service.runtime.enabled():
        logging.warning("[v3] V3_ENABLED=false，本次不执行")
        return 0
    started_at = datetime.datetime.now().isoformat(timespec="seconds")
    reason = _wake_reason(service, args.wake_id)
    trigger = f"{args.trigger}:{args.wake_id}" if args.wake_id else args.trigger
    result = service.run_cycle(trigger=trigger, wake_reason=reason)
    try:
        service.record_wake_result(wake_id=args.wake_id or "manual", reason=reason,
                                   result=result, started_at=started_at)
    except Exception as exc:  # noqa: BLE001 - 记录失败不能影响这一轮的结果
        logging.warning("[v3] 写唤醒记录失败：%s", exc)
    print(json.dumps({
        "ran": result.get("ran", False),
        "skip_reason": result.get("skip_reason", ""),
        "reason": reason,
        "threshold_reached": result.get("threshold_reached", False),
        "cognition_ran": result.get("triggered", False),
        "cycle_id": (result.get("cycle") or {}).get("cycle_id", ""),
        "decision": ((result.get("cycle") or {}).get("decision") or {}).get("action", ""),
        "next_wake": (result.get("next_wake") or {}).get("earliest_at", ""),
    }, ensure_ascii=False))
    return 0 if (result.get("ran") or result.get("skip_reason")) else 1


def _wake_reason(service, wake_id: str) -> str:
    """把队列任务的 reason 翻成唤醒模块的 Wake Reason。"""
    if not wake_id:
        return "MANUAL"
    try:
        tasks = service.wake_queue.all()
    except Exception:  # noqa: BLE001
        return "TIME_RECHECK"
    for task in tasks:
        if str(task.get("id")) != str(wake_id):
            continue
        raw = str(task.get("reason", ""))
        if raw.startswith("autonomous:"):
            return raw.split(":", 1)[1].strip() or "TIME_RECHECK"
        return {"life_cycle": "TIME_RECHECK", "initial_wake": "TIME_RECHECK",
                "max_silence_guard": "TIME_RECHECK", "continuity_seed": "CONTINUITY",
                "manual": "MANUAL", "waketest": "WAKE_TEST"}.get(raw, "TIME_RECHECK")
    return "TIME_RECHECK"


if __name__ == "__main__":
    raise SystemExit(main())
