# 夕颜 V3 —— 与 V2 / 旧项目的关系与同步规则

## 一、总原则

- `telegram-bot`（旧版）与 `telegram-bot-v2` **只读**，任何时候都不因为 V3 的需求去改它们。
- `telegram-bot-v3` 是 V2 代码基线的一份**复制 + 叠加**，**不会自动跟随 V2 的后续修改**。
- 因此两边会出现分叉：这是刻意的，不是故障。

## 二、同一时刻只能跑一个

V2 与 V3 共用同一个 `TELEGRAM_BOT_TOKEN`。
Telegram 的 `getUpdates` 是**独占**的：两个进程同时轮询会互相踢掉，表现为
`Conflict: terminated by other getUpdates request` 或直接收不到消息。

所以：

1. 启动 V3 前，先把 V2 的 `bot.py` 停掉（`启动夕颜V3.cmd` 已自动处理）。
2. 反过来，回滚到 V2 前，也要停掉 V3 的 `bot.py` 与 `scheduler.py`。
3. 想临时"只看不接客"，把 V3 的 `V3_ENABLED` 改成 `false` 即可——V2 的聊天链路仍在，V3 不订阅任何事件、不写任何 V3 文件。

## 三、数据不共享

| 数据 | V2 | V3 |
| --- | --- | --- |
| Memory / Emotion / Relationship / Self Model | `telegram-bot-v2/data/` | 不读、不写 |
| V3 认知状态 | 无 | `telegram-bot-v3/data/v3/` |

`telegram-bot-v3/data/` 由 `.gitignore` 排除（只保留一个 `data/.gitkeep`），
所以 V3 的运行痕迹不会被推到 GitHub，也不会把 V2 的记忆带过去。

V3 自己的数据布局：

```
data/v3/
  runtime.json               # /mode 写的运行时档位覆盖
  budget.json                # 日计数：llm_calls / cycles / actions
  observations.jsonl         # 聊天观测（只存已有数据，0 token）
  observations_archive.jsonl # 超过 V3_OBSERVATION_KEEP 后轮转
  thoughts/thoughts.json
  interests/interests.json
  continuity/{seed.json,cycles.json,wake_queue.json,queue.lock}
  inner_journal/inner_journal.jsonl
```

## 四、要把 V2 的某个修复搬到 V3 时

1. 先确认那个修复**不碰 V3 的铁律**（见 `V3_BASE.md` 第四节）。
2. 用 diff 看 V2 到底改了哪几行，只搬那几行，不要整文件覆盖——
   `bot.py`、`core/event_bus.py`、`core/conversation.py`、`config*.env` 在 V3 里**已经被刻意改过**，
   整文件覆盖会把 V3 的接线冲掉。
3. 搬完必须跑：

```powershell
cd telegram-bot-v3
python -m unittest discover -s tests -t .
python tests/verify_v3_mvp.py
```

4. 全绿之后再看一眼 `telegram-bot-v2/bot.py` 的 SHA256 有没有变
   （不该变；变了说明误改了回滚点）。

## 五、当前分叉点一览（V2 vs V3）

- `bot.py`：V3 多了 `V3Service` 装配、4 个事件订阅、命令前置分发、`/version`、`[build] v3=...` 一行、
  `_pid_alive()` 的安全判定（加查退出码）。
- `core/event_bus.py`：V3 的事件白名单多了 `BotResponseSent` 与 4 个 `v3:*`。
- `core/conversation.py`：V3 在回复发送后多发布一个 `BotResponseSent`。
- `tools/reminders.py`：V3 重写了口语时间解析（V2 的旧解析器仍是原样）。
- `config.env` / `config.example.env`：V3 多了 15 个 `V3_*` 键。
- `tests/test_event_bus.py`：跟随事件白名单更新断言。
- `tests/test_proactive_engine.py`：测试基准时间改为跟随真实时钟（修 V2 里"过了 20:00 必红"）。
- `.gitignore`：`data/*` + `!data/.gitkeep`。

另外**只在 V2 侧改了一个脚本**：`telegram-bot-v2/launch.ps1`（启动器加固：能认出并替换掉带 V3 的旧进程），
原文件备份为 `telegram-bot-v2/launch.ps1.orig`。V2 的 `bot.py` 等业务代码未改动。

除此之外，V3 的其余 V2 代码与复制时**逐字节一致**。
