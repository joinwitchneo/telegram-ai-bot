# Telegram AI Bot（夕颜 V3）

一个**跑在自己电脑上**的 Telegram 角色聊天机器人：不是客服、不是问答工具，而是一个有稳定人格、长期记忆、情绪与关系状态、自我理解、会挑时候主动找你说话，并且**尽量把能力留在本地**的角色系统。

主聊天模型可接任意 OpenAI 兼容接口（默认 DeepSeek）；图片理解、语音识别、OCR、天气、网页阅读全部走**本地程序或免费 HTTP**，不需要额外的付费 AI 接口。

> 本仓库当前是 **V3**：在 V2 基础上加了「数字生命内核（Digital Life Core）」——
> 念头会随时间衰减/激活、检查点会便宜地反复评估、只有值得想的时候才调用模型、
> 醒来不等于说话、行动能力通过接口接进来（Telegram 只是第一个环境实现）。
> V2 的代码仍完整保留在本仓库的历史提交里，并可随时回滚。

---

## 它和普通"AI 助手"的区别

| 模块 | 作用 |
| --- | --- |
| `persona_core.txt` | 人格内核（说话方式、脾气、边界、反规则） |
| `worldbook.txt` | 世界观与关系设定（可替换成你自己的角色） |
| `memory/` | 结构化长期记忆 + 话题生命周期 + 冷热分层 |
| `emotion/` `relationship/` | 情绪状态（12 维、有惯性、有时间衰减）与关系五维 |
| `self_model/` | 她"怎么看自己"：从经历里慢慢形成观点，允许矛盾、允许改变 |
| `proactive/` | 主动消息：必须有理由（未完成话题/重要日期/久未联系），不是随机骚扰 |
| `core/` | LLM Policy、Context（L0–L4）、Response Planner、Validator、Token 预算 |
| `style/` | 消息拆分与真人节奏（中文标点规则、连发、停顿） |
| `capabilities/` `tools/` | 能力层：视觉/OCR/语音/视频/天气/搜索/提醒，统一"有或没有" |
| `v3/` | **V3 数字生命内核**：Thought 动力学、检查点/阈值、认知器官接口、行动接口、自主唤醒、反馈 |

### V3 的自主闭环（Phase 6）

```
聊天/时间/内部状态 → 观测 → 念头(可衰减/激活) → 检查点(纯 Python，0 token)
  → 达到阈值才调用认知器官(LLM) → 结构化 Proposal → Python 决策
  → 行动校验(预算/冷却/档位) → 行动接口 → 环境适配器 → 结果 → 反馈 → 连续性 → 下一次唤醒
```

三条铁律：

1. **LLM 只是提议者**：它的输出必须过校验器，最后由 Python 决定做什么（可以什么都不做）。
2. **检查点便宜、LLM 昂贵**：检查点每轮只做算术，只有达到认知阈值才可能调用一次模型。
3. **行动接口化**：核心不认识 Telegram，只产生 ActionRequest；Telegram 只是 `v3/environments/` 的第一个实现。

默认档位 `observe`：**会思考、会记录，但绝不主动给你发消息**（`dry_run` 走模拟发送，`live` 才允许真正发送）。

观察命令：`/wakestatus` `/wake` `/whyawake` `/wakereasons` `/thoughts` `/thought <id>`
`/checkpoints` `/triggers` `/actions` `/why [action_id]` `/interests` `/journal` `/cycle` `/mode`

设计原则只有一句：**程序负责状态和规则，模型负责理解与表达。**

---

## 快速开始

### 启动脚本说明（只有一个命令行入口）

| 文件 | 作用 |
| --- | --- |
| `启动夕颜.cmd` | **唯一推荐入口**：校验 Python 与 `config.env` → 按需拉起本地 Ollama → 停掉本目录旧实例 → 启动 `bot.py` → 打印 `[build]` 指纹自证 → 失败时保留窗口并显示原因 |
| `启动器.bat` | 图形界面入口（`gui.py`）：适合填 token、看日志；机器人日志同样写进 `bot.log` |
| `build_exe.bat` | 打包成 exe（开发用） |

两个入口的检查逻辑一致：找不到 Python、缺 `config.env`、启动失败都会**明确报错并保留窗口**，不会静默退出、也不会吞掉 traceback。

启动后日志里必须出现这几行——这就是"跑的是最新代码"的证据：

```
[build] path=…\bot.py
[build] sha256=…
[build] mtime=…
[build] pid=…
[build] started_at=…
[build] handlers: /vision=True /cap=True /tools=True
```

同一时间只允许一个实例：若已有实例在跑，新进程会打印对方 PID 并拒绝启动（不会偷偷开第二个）。

```bash
git clone https://github.com/joinwitchneo/telegram-ai-bot.git
cd telegram-ai-bot
cp config.example.env config.env     # Windows: copy config.example.env config.env
```

编辑 `config.env`，至少填两项：

```
TELEGRAM_BOT_TOKEN=从 @BotFather 拿的 token
DEEPSEEK_API_KEY=你的模型 API Key
```

然后启动：

```bash
python bot.py
```

Windows 用户可以直接双击 `启动器.bat`（图形启动器，可填 token、一键保存并启动）。

---

## 可选：本地感知能力（不配也能用，配了更完整）

不装这些，机器人照样聊天；只是对应能力会**诚实地说"我这边看不到/听不到"**，绝不会编内容。

| 能力 | 需要 | 说明 |
| --- | --- | --- |
| 图片理解 | [Ollama](https://ollama.com) + 视觉模型（如 `qwen2.5vl:3b`、`llava`、`gemma3`） | 图片不出本机；`VISION_MODEL` 留空则自动探测 |
| 语音转文字 | `whisper.cpp` + `ggml-base.bin` | 完全离线；配 `WHISPER_MODEL_PATH` |
| 视频抽帧 | `ffmpeg` | 短视频抽帧后交给视觉模型；>5 分钟默认不自动分析 |
| OCR | Windows 自带 OCR 或 `tesseract` | 读截图/图片里的文字 |
| 天气 / 地名 | 无需申请：Open-Meteo 免费接口 | 带缓存，只有 HTTP 成本 |
| 网页阅读 | 无需申请 | 本地提取正文；搜索质量不足时会**拒绝回答**而不是编 |

装了便携版但不在 PATH 里？把目录填到 `TOOL_BIN_DIR` 即可（支持 `whisper\Release\whisper-cli.exe` 这种两级结构）。

---

## Telegram 里可用的命令

```
/help            查看帮助
/todo            看待办（纯编号清单）
/remind 内容      记一件待办
/done 编号        完成待办
/memo /memos     备忘录
/water on|off    喝水提醒
/search 关键词    联网搜索
/clear           清空当前会话上下文
/status          后端与用量状态
/tools           工具层（缓存/成本）
/cap             能力层现状（哪些能力可用）
/vision          视觉链路体检（逐级输出真实结果）
/proactive       主动消息状态（/proactive now 试发一次）
/version         确认现在跑的是 V3 还是 V2（含代码指纹）
/autostart on|off 开机自启（指向 V3 启动器）

# ── V3 自主生命内核（默认 observe：只思考，不主动发消息）──
/wakestatus      档位 / 队列 / 今日 cycle·LLM·Action / Phase 6 状态
/wake            现在会不会自己醒来（候选 + 唤醒分 + 卡在哪）
/whyawake        最近一次唤醒：为什么醒、想到了什么、决定了什么
/wakereasons     唤醒原因统计（醒来 ≠ 发消息）
/waketest unfinished|curiosity|intention|relationship [now]
/thoughts        最近的念头（含 id、触发分、激活度）
/thought <id>    单个念头的完整生命周期
/checkpoints     最近检查点（为什么这一刻没有调用 LLM）
/triggers        当前触发候选（candidate / eligible / blocked）
/actions         最近的行动（observe 档显示「本来会做」）
/why [action_id] 可解释链：为什么这么做
/interests       兴趣状态
/journal [n]     内在日志
/cycle           手动跑一轮（同样受预算/阈值/校验约束）
/mode observe|dry_run|live   切换运行时档位（active 是 live 的别名）
/v3help          V3 命令说明
```

直接发消息就是聊天；图片、语音、视频、文件、链接都可以直接发。

---

## 隐私

- 本仓库**不包含**任何人的聊天记录、记忆数据、API Key 或运行日志；`data/` 与 `config.env` 已被 `.gitignore` 排除，请勿提交。
- 图片/语音/文件默认**只在本机处理**，不上传第三方；只有主聊天文本会发给你配置的模型服务。

---

## 测试

纯标准库 + `unittest`，不需要额外依赖：

```bash
python -m unittest discover -s tests -t .
```

真实模型/真实本机能力的验证脚本在 `tests/verify_*.py`（例如 `verify_phase7e.py` 会真的调用本地视觉与语音，可用 `XIYAN_FIXTURES` 指定素材目录）。

V3 相关的设计与边界写在 `docs/v3/V3_BASE.md`（基线、差异、铁律、回滚）与
`docs/v3/SYNC.md`（与 V2 的关系、怎么搬运修复）。

---

## 说明

- `persona_core.txt` / `worldbook.txt` 是原创角色设定，欢迎替换成你自己的角色；请勿直接用于有版权争议的角色复刻。
- 这是一个**私人自用**项目，没有多用户隔离、没有云部署方案，适合一个人跑在自己的电脑上。
