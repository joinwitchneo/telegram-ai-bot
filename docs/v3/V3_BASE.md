# 夕颜 V3 —— 基线与边界（Phase 0 产出）

> 本文件记录 V3 是从哪个版本的 V2 复制来的、复制之后改了什么、以及怎么回滚。
> 采集时间：2026-09-13

## 一、三个目录的分工

| 目录 | 角色 | 本轮是否改动 |
| --- | --- | --- |
| `telegram-bot` | 最早的线上旧版 | **零修改**（SHA256 由测试锁定） |
| `telegram-bot-v2` | V2 正式版，**回滚点** | 本轮不动 |
| `telegram-bot-v3` | 本轮新建：V2 代码基线 + `v3/` 自主生命模块 | 本轮的唯一施工区 |

V2、V3 **共用同一个 bot token**，因此**同一时刻只能有一个在跑**。
`启动夕颜V3.cmd` 启动前会自动停掉 V2/V3 残留的 bot 与 scheduler 进程。

## 二、V2 基线指纹（用于证明"回滚点没被动过"）

| 文件 | SHA256 | 记录时间 |
| --- | --- | --- |
| `telegram-bot-v2/bot.py` | `806A2FC52D1DD3B34C5BA6D14F2730A89F2810D1160D7C02E17C3EFECAB5F08A` | 2026-09-13T17:10:28 |
| `telegram-bot-v2/core/event_bus.py` | `968CD5F19C40D8E99747E295C5AED0A6797A75B9C3799640C707910E93C07884` | 2026-09-13T00:40:29 |
| `telegram-bot-v2/core/conversation.py` | `0AA1028E0A51CCE8D99E7CDC59368C027CE67B873421312B2547FCAC3F864D98` | 2026-09-13T13:52:32 |
| `telegram-bot-v2/config.example.env` | `878AF301231116F858881C054294B92E2F6B13A83B9F4A16F689CA087A97E65E` | 2026-09-13T16:57:40 |

旧项目（`telegram-bot/bot.py`）基线：`0AC38276BEC1EE324E284197203684E165F8AA69576D4C61408CE6B93A9FA7EB`
（V3 项目里的 `tests/test_regression_old_project.py` 每次跑测试都会重新校验它）。

V2 目录共 182 个文件，V3 复制时**只跳过 `data/`（运行数据）**，
另外有 3 个非代码文件没有复制，均不影响运行：

- `!LOGFILE!`（V2 目录里的历史遗留文件）
- `personality_observations/README.md`、`personality_observations/observations.md`（人工笔记，无代码引用）

## 三、V3 相对 V2 的全部差异

### 1) 新增（42 个文件，都不在 V2 里）

- `v3/`：`config.py`、`budget.py`、`journal.py`、`service.py`、`cycle_runner.py`
  - `v3/thought/`：`models.py` `store.py` `validator.py` `lifecycle.py` `novelty.py` `generator.py`
  - `v3/interest/`：`models.py` `store.py` `engine.py`
  - `v3/motivation/engine.py`、`v3/decision/engine.py`、`v3/reward/engine.py`
  - `v3/continuity/`：`models.py` `store.py` `rules.py` `cycle.py`
  - `v3/observation/recorder.py`
  - `v3/scheduler/`：`queue.py` `runner.py`
- `v3_commands.py`：Telegram 观察/控制命令
- `scheduler.py`：常驻"闹钟"进程入口
- `启动夕颜V3.cmd` + `launch_v3.ps1`：V3 专用启动器
- `VERSION`：版本标签（`/version` 与启动器都会读它）
- `tests/test_v3_core.py`、`tests/test_v3_runtime.py`、`tests/test_v3_scheduler.py`、
  `tests/test_v3_commands.py`、`tests/verify_v3_mvp.py`
- `data/.gitkeep`（V3 运行数据根目录，内容不入库）

### 2) 修改（7 个文件，都是最小必要改动）

| 文件 | 改了什么 | 为什么 |
| --- | --- | --- |
| `bot.py` | `V3_ENABLED=true` 时装配 `V3Service`、订阅 4 个事件、命令前置分发、`/version`、启动时多打一行 `[build] v3=...`；`/remind` 的模型兜底改为配置开关（默认关）；`_pid_alive()` 加查退出码 | 让 V3 挂在同一条聊天链路上；指令一律 0 token；被强杀的旧进程不能被误判成"还活着"，否则新实例永远起不来 |
| `core/event_bus.py` | 事件白名单加 `BotResponseSent` 与 4 个 `v3:*` | V3 只通过事件总线观察，不直接互相调用 |
| `core/conversation.py` | 回复发送完成后发布 `BotResponseSent`（只带已存在的文本/模式/条数） | 让"聊天＝免费环境感知"成立且 **0 token** |
| `tools/reminders.py` | 重写口语时间解析（点半/一刻、时段词、中文数字小时、时段默认小时、频次词与内容分离） | 修 `周五下午3点半交表` 与 `每晚11点提醒睡觉` 两个旧 bug |
| `tests/test_event_bus.py` | 白名单断言同步新增事件 | 跟随上面两处 |
| `tests/test_proactive_engine.py` | 测试基准时间改为跟随真实时钟 | 修 V2 里"过了 20:00 必红"的时间窗假失败（V2 目录保持原样） |
| `.gitignore` | `data/*` + `!data/.gitkeep` | 保留运行数据目录结构，但不把数据入库 |
| `config.env` / `config.example.env` | 新增 15 个 `V3_*` 键、`REMIND_LLM_PARSE`；`PROACTIVE_ENABLED` 默认关 | 配置化，不写死；V3 不主动发消息 |

### 3) 没有动的东西

Persona、Self Model、Memory、Emotion、Relationship、Character Life、Proactive、
Response Planner、Context Manager、Vision、Sticker 核心逻辑**一律未改**。
V3 也不读 V2 的 `data/`：V3 从**空白状态**开始。

## 四、V3 的铁律（改代码前先看这里）

1. **任何档位都不向用户发消息**：V3 的唯一输出是 `data/v3/inner_journal/inner_journal.jsonl`。
   唯一例外是用户主动敲的 `/version /v3help /wakestatus /wakequeue /wake /whyawake /wakereasons /waketest /thoughts /thought <id> /checkpoints /triggers /actions /interests /journal /why [action_id] /cycle /mode`。
   V2 继承来的后台主动消息也已在 V3 默认关掉（`PROACTIVE_ENABLED=false`，不启动后台线程）；
   手动 `/proactive now` 仍可用（它本就会用模型）。
   Phase 6 起 V3 **有能力**行动（`observe`/`dry_run` 下都不真的发出去），
   只有 `live` 档 + 校验通过 + 预算允许，才会通过 Environment 适配器真正发送。
2. **聊天主链路新增 0 次 LLM 调用**：观测只存"已经存在的数据"，不做摘要。
   同理，**指令一律由 Python 直接回答**：`handle()` 见到 `/` 开头就交给 `handle_command`，
   永远不进对话管线。唯二的例外是 `/cycle`（它本身就是跑一轮认知）和 `/proactive now`
   （它的目的就是让她现在说一句）——这两条的**回复文字仍是模板**，不经过 LLM 生成。
3. LLM 只出现在 V3 认知周期内，且先过 `llm_policy` + V3 预算（≤6 次/日、链长 ≤3）。
4. `scheduler / decision / interest / reward` 规则层**禁止调 LLM**。
   Phase 6 的运行链路是：
   `V3Service → LifeCycle → (Observation → ThoughtFormer) → Checkpoint → ThoughtDynamics
   → CognitiveProvider(LLM, 仅达阈值时) → Validator → ActionDecisionEngine
   → ActionValidator → ActionProvider → Environment → Outcome → Feedback → Continuity`。
   Phase 5 的 `CognitiveCycle` 保留在 `service.cycle`，默认路径不再调用（仅回滚/兼容用）。
   **自主唤醒**（`v3/wake/`）：Scheduler 空闲时问 `WakeManager`"要不要自己醒来"（纯 Python），
   醒来只意味着"看看有没有值得处理的"，绝不等于发消息；所有唤醒都排进 `wake_queue` 再执行。
   唤醒分 ≥ `V3_WAKE_THRESHOLD`（默认 0.55）且未超每日上限/间隔/念头冷却，才会入队。
5. 不新增后台线程：Scheduler 是独立进程，Cycle 是 `python -m v3.cycle_runner` 一次性进程。
6. 不引入 DB / MQ / Embedding / Vector DB / MTProto；时间沿用本地 naive `datetime`。

## 五、运行与回滚

**启动 V3**

```
双击 启动夕颜V3.cmd
```

它会：校验 Python 与 `config.env` → 停掉 V2/V3 旧进程 → 启动 `bot.py` → 启动 `scheduler.py`
（只有 `V3_ENABLED=true` 才启动 Scheduler）→ 打印 `bot.log` 末尾的 `[build]` 指纹。

### 启动器怎么"用新进程替换旧进程"

点一下就是"替换"，靠三条互相兜底的识别途径（只认路径是不够的——
老启动器用 `-ArgumentList "bot.py"` 启动，命令行里根本没有目录）：

1. **锁文件**：`data/bot.lock`（bot 自己写）与 `data/v3/scheduler.lock`（scheduler 自己写）里的 pid；
2. **命令行带本项目路径**：启动器现在用完整路径启动，命令行里能直接看出来；
3. **裸脚本名**：`"...python.exe" bot.py` 这种没有任何目录的写法，认作同一套（V2/V3 共用 token，都要让位）。

停掉之后会等旧进程真的退出，再启动新的；万一还有漏网的实例（表现为
`[instance] 已经有一个夕颜在运行`），启动器会自动解析出那个 pid、清掉、重启一次。

自检（不需要真的启动机器人）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File launch_v3.ps1 -SelfTest
powershell -NoProfile -ExecutionPolicy Bypass -File launch_v3.ps1 -DryRun
```

> 注意：`telegram-bot-v2/launch.ps1` 也做了同样的加固（否则回滚时它认不出带 V3 的进程），
> 旧版本备份在 `telegram-bot-v2/launch.ps1.orig`。V2 的业务代码仍未改动。

确认跑的是新代码：看 `bot.log` 里的
`[build] sha256=...`、`[build] pid=...`、`[build] handlers: ...`、`[build] v3=on mode=...`。

**回滚到 V2**（V3 停了就断干净，V3 不写 V2 的任何文件）

```
taskkill /IM python.exe /F          # 或者只杀 V3 的 bot/scheduler
cd ..\telegram-bot-v2
双击 启动夕颜.cmd
```

**临时只关 V3 的功能、保留 V2 聊天链路**：把 `config.env` 的 `V3_ENABLED=false`，
重启 bot。此时 V3 **不注册订阅、不写任何 V3 文件**，命令会回"V3 未启用"。
