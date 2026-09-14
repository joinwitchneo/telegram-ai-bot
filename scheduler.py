"""夕颜 V3 Scheduler：常驻闹钟进程（不含业务逻辑、不调 LLM）。

跑法：python scheduler.py
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bot import Config, _pid_alive, load_env_file  # noqa: E402
from v3.continuity.rules import plan_next_wake  # noqa: E402
from v3.scheduler.queue import WakeQueue  # noqa: E402
from v3.scheduler.runner import SchedulerRunner  # noqa: E402
from v3.service import V3Service  # noqa: E402


def _acquire_lock(path: Path) -> bool:
    """调度器唯一实例锁：写 {pid, path, started_at}，顺便给启动器定位用。"""
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            existing = {}
        other = int(existing.get("pid", 0) or 0)
        if other and other != os.getpid() and _pid_alive(other):
            logging.error("[scheduler] 已经有一个调度器在跑：pid=%s started_at=%s",
                          other, existing.get("started_at", ""))
            return False
    info = {
        "pid": os.getpid(),
        "path": str(Path(__file__).resolve()),
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        logging.warning("[scheduler] 写锁文件失败：%s", exc)
    return True


def _release_lock(path: Path) -> None:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if int(data.get("pid", 0) or 0) == os.getpid():
                path.unlink()
    except (OSError, json.JSONDecodeError):
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="夕颜 V3 唤醒调度器")
    parser.add_argument("--config", default=str(BASE_DIR / "config.env"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(BASE_DIR / "scheduler.log", encoding="utf-8")],
    )
    values = load_env_file(Path(args.config))
    if str(values.get("V3_ENABLED", "")).strip().lower() not in ("1", "true", "yes", "on"):
        logging.warning("[v3] V3_ENABLED 未开启，调度器退出")
        return 0

    config = Config(Path(args.config))
    data_dir = Path(config.get("V3_DATA_DIR", "data/v3"))
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir
    lock_path = data_dir / "scheduler.lock"
    if not _acquire_lock(lock_path):
        return 2
    queue = WakeQueue(data_dir / "continuity" / "wake_queue.json",
                      default_max_attempts=config.get_int("V3_MAX_ATTEMPTS", 3))
    # 只为了拿接线好的唤醒管理器（不调模型、不发消息）
    service = V3Service(config, base_dir=BASE_DIR)
    min_interval = config.get_int("V3_MIN_WAKE_INTERVAL_MINUTES", 60)
    max_silence = config.get_int("V3_MAX_SILENCE_HOURS", 48)
    if not queue.all():
        plan = plan_next_wake(
            now=datetime.datetime.now(), min_interval_minutes=min_interval,
            max_silence_hours=max_silence, priority="normal",
            has_unfinished=False, has_gain=False, no_action=False,
        )
        queue.enqueue(reason="initial_wake", earliest_at=plan["earliest_at"], priority=plan["priority"])
    runner = SchedulerRunner(
        queue=queue, base_dir=BASE_DIR,
        lease_minutes=config.get_int("V3_LEASE_MINUTES", 30),
        max_silence_hours=max_silence, min_interval_minutes=min_interval,
        # 唤醒判断（纯 Python）：Scheduler 只问"要不要醒"，不碰业务状态
        wake_manager=service.wake_manager,
    )
    try:
        runner.loop(poll_seconds=config.get_int("V3_SCHEDULER_POLL_SECONDS", 300))
    finally:
        _release_lock(lock_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
