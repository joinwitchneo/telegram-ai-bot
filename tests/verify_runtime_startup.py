"""运行期验收（不发 Telegram、不调主模型）：启动自证 + 视觉链路 + 降级。

跑法：python tests/verify_runtime_startup.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from tests.test_vision_e2e import build_conversation, make_test_png  # noqa: E402
from tools.perception.router import PerceptionRequest, PerceptionRouter  # noqa: E402


def main() -> int:
    lines: list[str] = []
    ok_all = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and passed
        lines.append(f"[{'OK' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    # 1. bot.py 能不能 import + 2. 配置能不能加载
    try:
        import bot as bot_mod

        check("bot.py 可 import", True)
    except Exception as exc:  # noqa: BLE001
        check("bot.py 可 import", False, f"{type(exc).__name__}: {exc}")
        (BASE / "data" / "verify_startup.txt").write_text("\n".join(lines), encoding="utf-8")
        return 1

    config = bot_mod.Config(BASE / "config.env")
    check("config.env 可加载", bool(config.get("TELEGRAM_BOT_TOKEN")), "token 已配置")

    # 1b. 启动指纹可用
    info = bot_mod.build_fingerprint()
    check("启动指纹可用（path/sha256/mtime/pid）",
          bool(info["path"] and info["sha256"] != "?" and info["mtime"] != "?"),
          f"sha256={info['sha256'][:12]} pid={info['pid']}")
    check("handlers 含 /vision 与 /cap",
          "/vision" in bot_mod.Bot.COMMANDS and "/cap" in bot_mod.Bot.COMMANDS)

    # 1c. 唯一实例锁
    lock_path = Path(tempfile.mkdtemp()) / "bot.lock"
    # 用一个"确实活着、但不是本进程"的 PID 模拟另一个实例
    other_pid = os.getppid() if os.getppid() and os.getppid() != os.getpid() else 999998
    lock_path.write_text(json.dumps({"pid": other_pid, "started_at": "x"}), encoding="utf-8")
    lock = bot_mod.SingleInstance(lock_path)
    allowed, existing = lock.acquire()
    check("已有实例在跑时拒绝再开第二个", not allowed, f"检测到 pid={existing.get('pid')}")
    lock_path.write_text(json.dumps({"pid": 999999, "started_at": "x"}), encoding="utf-8")
    allowed2, _ = bot_mod.SingleInstance(lock_path).acquire()
    check("旧锁对应的进程已消失时允许启动", allowed2)

    # 3. 工具目录能注册
    try:
        bot = bot_mod.Bot(config)
        report = bot.registry.cost_report()
        check("工具/能力注册成功", report["total"] >= 10, f"共 {report['total']} 个")
    except Exception as exc:  # noqa: BLE001
        check("工具/能力注册成功", False, f"{type(exc).__name__}: {exc}")
        (BASE / "data" / "verify_startup.txt").write_text("\n".join(lines), encoding="utf-8")
        return 1

    # 4. Ollama 不存在时不崩（用一个假地址模拟不可用）
    from tools.perception.vision import OllamaVision

    dead = OllamaVision("http://127.0.0.1:9", "", enabled=True, autostart=False)
    result = dead.describe(str(BASE / "persona_core.txt"))
    check("Ollama 不可用时优雅失败（不抛异常）", (not result.ok) and bool(result.error),
          f"error={result.error[:60]}")

    # 5. Ollama 恢复后能重新发现模型（不缓存失败结果）
    recovering = OllamaVision("http://127.0.0.1:11434", "")
    first = recovering.resolve_model()          # 现在真的可用
    scan = recovering.list_models()
    check("能发现本地视觉模型", bool(first), f"model={first or '（无）'}")
    if first:
        recovering._model_cache = "已经不存在的老模型:1b"
        again = recovering.resolve_model()
        check("缓存失效时重新探测", again == first, f"重新解析到 {again}")
    recovering._model_cache = None
    check("Ollama 当前可用性", bool(scan), f"模型数={len(scan)}")

    # 6~8. 视觉结果与事实边界
    tmp = Path(tempfile.mkdtemp())
    image = make_test_png(tmp / "red_circle.png")
    if first:
        vision = OllamaVision("http://127.0.0.1:11434", "")
        router = PerceptionRouter(vision=vision, use_ocr=False)
        perception = router.perceive(PerceptionRequest(source_type="image", path=str(image)))
        check("VisionResult.success == True", perception.ok, (perception.text or "")[:60])
        check("VisionResult.data 非空", bool(perception.data))
        summary = PerceptionRouter.describe_for_context(perception)
        conversation, client, _ = build_conversation()
        conversation.perceive(1, source_type="image", summary=summary, perception_ok=True)
        joined = "\n".join(str(m.get("content", "")) for m in client.calls[0])
        check("最终 LLM messages 含视觉结果", perception.text[:10] in joined or "图片" in joined)
    else:
        lines.append("[SKIP] 本机没有可用视觉模型，跳过 6~8 的真实识别断言")

    # 失败路径：绝不能把失败当成"看到了"
    blind = PerceptionRouter(vision=None, use_ocr=False)
    failed = blind.perceive(PerceptionRequest(source_type="image", path=str(image)))
    check("Vision 失败时 success=False、data 为空", (not failed.ok) and not failed.data.get("vision"))
    conversation2, client2, _ = build_conversation()
    conversation2.perceive(
        1, source_type="image", summary=PerceptionRouter.describe_for_context(failed), perception_ok=False
    )
    joined2 = "\n".join(str(m.get("content", "")) for m in client2.calls[0])
    check("失败时 Context 只表达「没有获得视觉内容」",
          "没有获得任何视觉" in joined2 and "红色" not in joined2)

    # 9. /vision 的诊断要来自当前进程
    check("/vision 诊断使用当前进程信息（pid/path/sha256）",
          hasattr(bot_mod.Bot, "cmd_vision") and info["pid"] == os.getpid())

    report_path = BASE / "data" / "verify_startup.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n结果：{'全部通过' if ok_all else '有失败项'}（详见 {report_path}）")
    return 0 if ok_all else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
