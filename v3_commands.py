"""V3 的 Telegram 观察/控制命令 + 观测钩子（只读或请求，不直接改文件）。

所有命令都通过 V3Service 的接口；写操作只有两种：
  /mode  -> 写 data/v3/runtime.json（经 RuntimeConfig）
  /cycle -> 提交一次认知请求（同样过预算与 Validator）
"""

from __future__ import annotations

import logging
from pathlib import Path

V3_HELP = (
    "V3 观察命令（默认档位 observe：只思考 + 写 inner_journal，绝不主动发消息）：\n"
    "这些命令全部由 Python 直接回答，不调用模型、不花 token（只有 /cycle 例外：\n"
    "它本身就是「跑一轮认知」，那一轮按预算用模型；但命令的回复文字仍是模板）。\n"
    "· /version —— 现在聊的是 V3 还是 V2（含代码指纹与进程号）\n"
    "· /wakestatus —— 档位、下次唤醒、队列、今日 cycle 与 LLM 计数\n"
    "· /wakequeue —— 队列明细（含 FAILED_PERMANENT 与 attempts）\n"
    "· /wakequeue requeue <id> —— 显式复活一个永久失败任务\n"
    "· /thoughts —— 最近的念头（含 id、触发分、激活度）\n"
    "· /thought <id> —— 单个念头的完整快照（生命周期/触发分/证据/经历）\n"
    "· /checkpoints —— 最近检查点（为什么这一刻没调用 LLM）\n"
    "· /triggers —— 当前触发候选（candidate / eligible / blocked）\n"
    "· /actions —— 最近的行动（observe 档显示 WOULD_ACTION）\n"
    "· /interests —— 兴趣状态（好感/好奇/倾向/证据）\n"
    "· /journal [n] —— 最近的内在日志\n"
    "· /why —— 最近一次认知的可解释链\n"
    "· /why <action_id> —— 解释某一次行动是怎么来的\n"
    "· /wake —— 现在会不会自己醒来（候选 + 唤醒分 + 卡在哪）\n"
    "· /whyawake —— 最近一次唤醒：为什么醒、想到了什么、决定了什么\n"
    "· /wakereasons —— 唤醒原因统计（醒来 ≠ 发消息）\n"
    "· /waketest unfinished|curiosity|intention|relationship [now]\n"
    "     受控构造一个内部场景来验证唤醒链路（照样过预算/阈值/校验，没有后门）\n"
    "· /cycle —— 手动跑一轮认知（同样受预算与校验限制）\n"
    "· /mode observe|dry_run|live —— 只改运行时覆盖，立即生效\n"
    "· /v3help —— 这份说明"
)

V3_COMMANDS = (
    "/v3help", "/version", "/版本", "/wakestatus", "/wakequeue", "/thoughts",
    "/thought", "/checkpoints", "/triggers", "/actions", "/interests", "/journal",
    "/why", "/wake", "/whyawake", "/wakereasons", "/waketest", "/cycle", "/mode",
)

VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
VERSION_FALLBACK = "V3.0-mvp"


def version_label() -> str:
    """版本标签：优先读 VERSION 文件的第一行，读不到就用内置兜底值。"""
    try:
        lines = VERSION_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return VERSION_FALLBACK
    return next((line.strip() for line in lines if line.strip()), "") or VERSION_FALLBACK


def version_report(bot) -> str:
    """回答"我现在聊的是 V3 还是 V2"。不需要 V3Service —— V3 关掉时也要能答。"""
    service = getattr(bot, "v3_service", None)
    info = dict(getattr(bot, "build_info", {}) or {})
    lines = ["夕颜 · 当前进程自证", "────────────"]
    if service is None:
        lines.append("在跑的是：V2（V3 未启用）")
        lines.append("说明：只有 V2 的聊天链路在工作，V3 不订阅事件、不写任何 V3 文件。")
    else:
        report = service.runtime.mode_report()
        lines.append(f"在跑的是：V3（版本 {version_label()}）")
        lines.append(
            "档位：基础={0} 覆盖={1} 生效={2}".format(
                report["base_mode"], report["runtime_override"] or "-", report["effective_mode"]
            )
        )
        lines.append(f"V3 数据目录：{service.runtime.data_dir}")
    if info:
        lines.append(f"入口：{info.get('path', '?')}")
        lines.append("指纹：sha256={0} mtime={1}".format(
            str(info.get("sha256", "?"))[:12], info.get("mtime", "?")))
        lines.append(f"进程：pid={info.get('pid', '?')} 启动于 {info.get('started_at', '?')}")
    else:
        lines.append("指纹：这次没拿到（进程可能是手工起的）")
    if service is None:
        lines.append("提示：V3 关掉时这条命令依然会答（用来确认你到底在跟谁说话）。")
    else:
        lines.append("提示：V2 目录里的机器人没有 /version，发过去只会回「没这个命令」。")
    return "\n".join(lines)


def dispatch(bot, chat_id: int, command: str, rest: str) -> bool:
    """处理 V3 命令；返回 True 表示已处理（不再走普通聊天）。"""
    name = (command or "").strip().lower()
    if name not in V3_COMMANDS:
        return False
    service = getattr(bot, "v3_service", None)
    if name == "/v3help":
        bot.send_message(chat_id, V3_HELP)
        return True
    if name in ("/version", "/版本"):
        # 故意放在"未启用"判断之前：V3 关掉时这条命令才有诊断价值
        bot.send_message(chat_id, version_report(bot))
        return True
    if service is None:
        bot.send_message(
            chat_id,
            "V3 未启用。要开启：在 config.env 里把 V3_ENABLED 改成 true，然后重启机器人。\n"
            "（开启后仍不会主动给你发消息，只会在本地写 inner_journal）",
        )
        return True

    if name == "/wakestatus":
        status = service.status()
        runtime = status["runtime"]
        budget = status["budget"]
        queue = status["queue"]
        lines = [
            "夕颜 V3 状态",
            "────────────",
            f"启用：{runtime['enabled']}",
            f"档位：基础={runtime['base_mode']} 覆盖={runtime['runtime_override'] or '-'} "
            f"生效={runtime['effective_mode']}",
            f"下次唤醒：{queue.get('next_at') or '（无排队任务）'}",
            f"队列：{queue.get('total', 0)} 条（{queue.get('by_status')}）",
            f"今日：cycle {budget['cycles']}/{budget['limits']['cycles']}，"
            f"LLM {budget['llm_calls']}/{budget['limits']['llm_calls']}，"
            f"全局预算={budget['global_level']}",
            f"念头：{status['thoughts']['total']}（未完成 {status['thoughts']['unfinished']}）"
            f" 兴趣：{status['interests']['topics']} 个话题",
            f"journal {status['journal']} 条 ｜ observations {status['observations']} 条",
        ]
        phase6 = status.get("phase6") or {}
        if phase6:
            env = phase6.get("environment") or {}
            actions = phase6.get("actions") or {}
            lines += [
                "Phase 6（自主闭环）",
                f"检查点：{phase6.get('checkpoints', 0)} 次（触发深思 {phase6.get('triggered', 0)} 次）"
                f"　最近档位={phase6.get('band') or '-'}",
                f"阈值：{phase6.get('thresholds')}",
                f"行动：今日消息 {actions.get('messages_sent', 0)}/"
                f"{actions.get('limits', {}).get('daily_message_limit', 0)}"
                f"　深思 {actions.get('cognitive_calls', 0)}/"
                f"{actions.get('limits', {}).get('daily_cognitive_limit', 0)}",
                f"环境：{env.get('name')}（可用={env.get('available')}，模拟={env.get('dry_run')}）"
                f"　现在可执行={phase6.get('actions_executable')}"
                f"（登记在册={phase6.get('actions_available')}）",
            ]
        bot.send_message(chat_id, "\n".join(lines))
        return True

    if name == "/wakequeue":
        action = (rest or "").strip().split()
        if action and action[0] == "requeue" and len(action) > 1:
            ok = service.wake_queue.requeue(action[1])
            bot.send_message(chat_id, f"复活 {action[1]}：{'成功' if ok else '没找到这个任务'}")
            return True
        rows = service.wakequeue_lines()
        bot.send_message(chat_id, "唤醒队列：\n" + ("\n".join(rows) if rows else "（空）"))
        return True

    if name == "/thoughts":
        rows = service.thought_lines()
        bot.send_message(chat_id, "最近的念头：\n" + ("\n".join(rows) if rows else "（还没有念头）"))
        return True

    if name == "/thought":
        thought_id = (rest or "").strip().split()[0] if (rest or "").strip() else ""
        if not thought_id:
            bot.send_message(chat_id, "用法：/thought <念头id>（id 在 /thoughts 里能看到）")
            return True
        rows = service.thought_detail(thought_id)
        bot.send_message(chat_id, "\n".join(rows) if rows else f"没找到念头 {thought_id}")
        return True

    if name == "/checkpoints":
        try:
            limit = int((rest or "5").strip() or 5)
        except ValueError:
            limit = 5
        rows = service.checkpoint_lines(limit=max(1, min(20, limit)))
        bot.send_message(chat_id, "最近检查点：\n" + ("\n".join(rows) if rows else "（还没有检查点）"))
        return True

    if name == "/triggers":
        bot.send_message(chat_id, "触发候选：\n" + "\n".join(service.trigger_lines()))
        return True

    if name == "/actions":
        try:
            limit = int((rest or "8").strip() or 8)
        except ValueError:
            limit = 8
        bot.send_message(chat_id, "最近的行动：\n" + "\n".join(
            service.action_lines(limit=max(1, min(20, limit)))))
        return True

    if name == "/interests":
        rows = service.interest_lines()
        bot.send_message(chat_id, "当前兴趣：\n" + ("\n".join(rows) if rows else "（还没有兴趣数据）"))
        return True

    if name == "/wake":
        preview = service.wake_preview()
        lines = [
            "自主唤醒：现在会不会自己醒？",
            "────────────",
            f"判定：{preview.get('decision')}"
            + (f"（{preview.get('blocked_by')}）" if preview.get("blocked_by") else ""),
            f"说明：{preview.get('reason')}",
        ]
        for item in (preview.get("candidates") or [])[:5]:
            flag = "eligible" if item.get("eligible") else ("blocked" if item.get("blocked_by") else "candidate")
            lines.append(f"· [{flag}] {item.get('reason')} 分={item.get('wake_score')} "
                         f"{str(item.get('content', ''))[:36]}"
                         + (f"　({item.get('blocked_by')})" if item.get("blocked_by") else ""))
        if not preview.get("candidates"):
            lines.append("（还没有可评估的念头）")
        bot.send_message(chat_id, "\n".join(lines))
        return True

    if name == "/whyawake":
        row = service.whyawake()
        if not row:
            bot.send_message(chat_id, "还没有唤醒记录。用 /waketest unfinished now 试一次。")
            return True
        lines = [
            f"最近一次唤醒（{row.get('wake_id') or '?'}）",
            "────────────",
            f"原因：{row.get('reason')}　模式：{row.get('mode')}",
            f"念头：{('、'.join(row.get('thought_ids') or [])) or '-'}",
            f"唤醒分/动机：{row.get('wake_score')} / {row.get('motivation')}",
            f"达到阈值：{'是' if row.get('threshold_reached') else '否'}"
            f"　调用了认知：{'是' if row.get('cognition_ran') else '否'}",
            f"决定：{row.get('decision') or '-'}（{row.get('decision_reason') or '-'}）",
        ]
        if row.get("would_action"):
            lines.append(f"本来会做：{row['would_action']}（实际未执行）")
        if row.get("action_id"):
            lines.append(f"行动：{row.get('action_id')} → {row.get('action_status')}"
                         + ("（模拟）" if row.get("simulated") else ""))
        lines.append(f"链路：{row.get('trace_id') or '-'}")
        bot.send_message(chat_id, "\n".join(lines))
        return True

    if name == "/wakereasons":
        summary = service.wake_reasons()
        lines = ["唤醒原因统计（醒来 ≠ 发消息）", "────────────",
                 f"共 {summary.get('total', 0)} 次唤醒"]
        for reason, item in sorted((summary.get("by_reason") or {}).items()):
            lines.append(f"· {reason}：唤醒 {item.get('wakes')} 次，其中深思 {item.get('cognition')} 次，"
                         f"消息 Action {item.get('messages')} 次")
        if not summary.get("by_reason"):
            lines.append("（还没有唤醒记录）")
        bot.send_message(chat_id, "\n".join(lines))
        return True

    if name == "/waketest":
        parts = (rest or "").strip().split()
        kind = parts[0].lower() if parts else ""
        immediate = len(parts) > 1 and parts[1].lower() in ("now", "立即", "马上")
        if not kind:
            bot.send_message(chat_id, "用法：/waketest unfinished|curiosity|intention|relationship [now]")
            return True
        outcome = service.run_waketest(kind, immediate=immediate)
        if not outcome.get("ok"):
            bot.send_message(chat_id, outcome.get("error", "构造失败") + "\n" + outcome.get("usage", ""))
            return True
        lines = [f"已构造内部场景：{kind}（念头 {outcome.get('thought_id')}）",
                 f"唤醒原因：{outcome.get('reason')}"]
        if outcome.get("immediate"):
            result = outcome.get("result") or {}
            cycle = result.get("cycle") or {}
            decision = cycle.get("decision") or {}
            lines += [
                f"达到阈值：{'是' if result.get('threshold_reached') else '否'}"
                f"　调用了认知：{'是' if result.get('triggered') else '否'}",
                f"决定：{decision.get('action', '-')}（{decision.get('reason', '-')}）",
            ]
            if decision.get("would_action"):
                lines.append(f"本来会做：{decision['would_action']}（不会真的发出去）")
            outcome = cycle.get("outcome") or {}
            if outcome.get("simulated"):
                lines.append("（模拟发送：真实 Telegram 没有被调用）")
            if not decision.get("would_action") and not outcome.get("simulated"):
                lines.append("（没有产生对外行动）")
            lines.append("（受预算/阈值/校验/档位约束，没有后门）")
        else:
            lines.append("已排入唤醒队列，Scheduler 下一轮巡检会执行它。")
        bot.send_message(chat_id, "\n".join(lines))
        return True

    if name == "/journal":
        try:
            limit = int((rest or "5").strip() or 5)
        except ValueError:
            limit = 5
        rows = service.journal_lines(limit=max(1, min(20, limit)))
        bot.send_message(chat_id, "内在日志：\n" + ("\n".join(rows) if rows else "（还没有记录）"))
        return True

    if name == "/why":
        action_id = (rest or "").strip().split()[0] if (rest or "").strip() else ""
        data = service.why(action_id)
        cycle = data.get("cycle") or {}
        if not cycle:
            bot.send_message(chat_id, "还没有跑过认知周期。用 /cycle 手动跑一轮看看。")
            return True
        bot.send_message(chat_id, "\n".join(_why_lines(data, action_id=action_id)))
        return True

    if name == "/cycle":
        result = service.run_cycle(trigger="manual")
        if not result.get("ran"):
            bot.send_message(chat_id, f"这一轮没有执行：{result.get('skip_reason')}")
            return True
        cycle = result.get("cycle") or {}
        decision = cycle.get("decision") or {}
        bot.send_message(
            chat_id,
            f"跑完一轮：{cycle.get('cycle_id')}\n"
            f"念头 {cycle.get('thoughts_created')} 条 ｜ 信息增益 {cycle.get('info_gain')}\n"
            f"决策 {decision.get('action')} —— {decision.get('reason')}\n"
            f"下次唤醒 {result.get('next_wake', {}).get('earliest_at')}\n"
            "（只写入 inner_journal，没有给你发任何消息）",
        )
        return True

    if name == "/mode":
        value = (rest or "").strip().lower()
        if value not in ("observe", "dry_run", "live", "active"):
            bot.send_message(chat_id, "用法：/mode observe｜/mode dry_run｜/mode live"
                                      "（live 也可以写成 active）")
            return True
        ok = service.set_mode(value, actor=str(chat_id))
        effective = service.runtime.mode()
        alias = "（active 是 live 的别名，内部规范档位仍是 live）" if value == "active" else ""
        bot.send_message(
            chat_id,
            f"运行时档位已切换为 {effective}{alias}（{'成功' if ok else '写入失败'}）。"
            "基础档写在 config.env，这里只改运行时覆盖；两者都会显示在 /wakestatus。",
        )
        return True
    return False


def _why_lines(data: dict, *, action_id: str = "") -> list:
    """把一条生命循环记录 + trace 事件整理成可解释摘要（不含模型思维链）。"""
    cycle = data.get("cycle") or {}
    checkpoint = cycle.get("checkpoint") or {}
    top = checkpoint.get("top") or {}
    cognitive = cycle.get("cognitive") or {}
    intention = cognitive.get("intention") or {}
    decision = cycle.get("decision") or {}
    action = cycle.get("action") or {}
    outcome = cycle.get("outcome") or {}
    feedback = cycle.get("feedback") or {}
    changes = (feedback.get("changes") or {})
    thought_change = changes.get("thought") or {}
    reward = feedback.get("reward") or {}

    lines = [
        f"为什么这么做（{cycle.get('cycle_id', '?')}）" if action_id else "最近一次认知",
        "────────────",
        f"唤醒：{cycle.get('trigger') or '-'}　模式：{cycle.get('mode') or '-'}",
        f"检查点：{checkpoint.get('checkpoint_id', '-')}　档位 {checkpoint.get('band', '-')}"
        f"　最高触发分 {top.get('trigger_score', '-')}",
        f"念头：{top.get('thought_id', '-')} {str(top.get('content', ''))[:60]}",
        f"动机/触发：motivation={top.get('motivation')}　"
        f"urgency={top.get('components', {}).get('urgency')}　"
        f"novelty={top.get('components', {}).get('novelty')}",
    ]
    if cognitive:
        lines.append(
            f"认知：{cognitive.get('provider')} ok={cognitive.get('ok')}"
            f" 意图={intention.get('type')}({intention.get('confidence')})"
            f" LLM={cognitive.get('llm_calls')} 次"
        )
        if cognitive.get("error"):
            lines.append(f"认知失败原因：{cognitive.get('error')}")
        if cognitive.get("rejected"):
            lines.append(f"被拒收的提案：{'；'.join(str(x) for x in cognitive['rejected'][:3])}")
    else:
        lines.append(f"认知：未调用（{cycle.get('skip_reason') or '未达到阈值'}）")
    lines.append(f"决策：{decision.get('action', '-')} —— {decision.get('reason', '-')}")
    if decision.get("would_action"):
        lines.append(f"（本来会做：{decision['would_action']}，实际未执行）")
    if action:
        lines.append(f"行动：{action.get('action_id')} 类型={action.get('kind')}")
    if outcome:
        lines.append(f"结果：{outcome.get('status')}"
                     + ("（模拟发送）" if outcome.get("simulated") else "")
                     + (f"　错误：{outcome.get('error')}" if outcome.get("error") else ""))
    if thought_change:
        lines.append(f"反馈：activation={thought_change.get('activation')} "
                     f"已行动={thought_change.get('times_acted')} "
                     f"被忽略={thought_change.get('times_ignored')}")
    if reward:
        lines.append(f"奖励：{reward.get('reward')}（预测误差 {reward.get('prediction_error')}）")
    kinds = [str(item.get("kind")) for item in (data.get("trace") or [])]
    if kinds:
        lines.append("链路：" + "→".join(kinds))
    return lines


# ── 观测钩子（只读数据，0 token）───────────────────────────────────
def on_user_message(bot, event) -> None:
    try:
        bot._v3_last_user = str(event.payload.get("text", ""))[:240]
    except Exception as exc:  # noqa: BLE001
        logging.debug("[v3] 记录用户消息失败：%s", exc)


def on_state_changed(bot, event) -> None:
    try:
        deltas = event.payload.get("deltas") or {}
        if event.name == "EmotionChanged":
            bot._v3_emotion_delta = dict(deltas)
        else:
            bot._v3_relationship_delta = dict(deltas)
    except Exception as exc:  # noqa: BLE001
        logging.debug("[v3] 记录状态变化失败：%s", exc)


def on_bot_response(bot, event) -> None:
    """把"已经存在的数据"写进 observations（不调模型、不做摘要）。"""
    service = getattr(bot, "v3_service", None)
    if service is None:
        return
    try:
        service.observations.append(
            user_message=getattr(bot, "_v3_last_user", ""),
            bot_response_excerpt=str(event.payload.get("excerpt", "")),
            response_mode=str(event.payload.get("response_mode", "")),
            message_count=int(event.payload.get("message_count", 0) or 0),
            emotion_delta=dict(getattr(bot, "_v3_emotion_delta", {}) or {}),
            relationship_delta=dict(getattr(bot, "_v3_relationship_delta", {}) or {}),
            chat_id=event.payload.get("chat_id", ""),
        )
        bot._v3_emotion_delta = {}
        bot._v3_relationship_delta = {}
    except Exception as exc:  # noqa: BLE001
        logging.warning("[v3] 写观测失败（不影响聊天）：%s", exc)
