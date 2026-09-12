# Telegram AI 聊天机器人

一个有情绪、有关系、有长期记忆的 Telegram 聊天机器人。后端支持 **DeepSeek API**（云端）和 **Ollama**（本地模型），纯 Python 标准库实现，**零第三方依赖**，开箱即用。

> 本仓库不包含任何第三方作品（游戏、动漫、小说等）的角色、剧情或台词。仓库自带的 `persona.txt` / `worldbook.txt` 是**原创示例角色「夕颜」**，你可以直接改，也可以整个替换成自己的设定。

## 功能

- **人格与人设**：`persona.txt`（角色卡）+ `worldbook.txt`（世界书），性格强度由 `personality.json` 调节
- **情绪引擎**：12 种情绪 + 强度 + 心情/精力/社交欲/烦躁/委屈/亲近六个连续值；由事件驱动、有惯性、会随时间衰减
- **关系等级**：每个用户独立，从陌生到亲近慢慢变化，直接影响说话方式
- **消息策略**：每轮自动选择 SHORT / NORMAL / LONG / BURST / COLD / PLAYFUL / ANGRY / HURT / AFFECTIONATE，决定说几句、多快回
- **长期记忆**：对话每 N 轮自动浓缩成记忆，并定期检查、压缩、重建；重要内容用结构化方式存储（带类型与重要性，可被新信息推翻）
- **真人聊天感**：连发合并成一次回复、回复速度三档（秒回/正常/慢回）、生成期间持续显示"输入中"、分条发送按字数估算时间、偶尔先发一句缓冲话、深夜更慢更短、你插话它会停下
- **主动开口**：待办跟进、早晚清单汇总（无待办不发）、饭点与熬夜关心、偶尔发牢骚；有冷却时间，不会变成骚扰
- **待办与备忘录**：`/todo` 看待办、`/done` 标完成；`/memo`、`/memos` 记和翻资料
- **记忆查看**：`/memory` 看它记得你什么
- **表情包**：按情绪随机发贴纸，支持斗图（识别对方表情包 emoji 回怼），尽量不重复
- **提醒与天气**：自然语言记提醒（"周六买牛奶"）、循环喝水提醒、定位/城市名天气
- **联网搜索**：可选 Tavily API

## 一键部署

### 方式一：Render（免费，推荐）

1. 把本仓库推送到你的 GitHub；
2. 到 [Render](https://render.com) 注册；
3. 点击下方按钮：

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/joinwitchneo/telegram-ai-bot)

4. 部署时按提示填写环境变量：

| 变量 | 说明 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | 找 [@BotFather](https://t.me/BotFather) 创建机器人获取 |
| `DEEPSEEK_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com) 创建 |
| `PROACTIVE_CHAT_ID` | 你的 Telegram 私聊 ID（给机器人发条消息后用 [@userinfobot](https://t.me/userinfobot) 查） |

`render.yaml` 已配好 `BACKEND=deepseek`、`TZ=Asia/Shanghai`，其余用默认即可。

### 方式二：Docker

```bash
cp config.example.env config.env
# 编辑 config.env，填入 TELEGRAM_BOT_TOKEN、DEEPSEEK_API_KEY、PROACTIVE_CHAT_ID
docker compose up -d --build
```

### 方式三：本机直接运行

需要 Python 3.10+（无需安装任何第三方库）。

Windows：

```bat
copy config.example.env config.env
:: 编辑 config.env 填入配置
start_bot.bat
```

Linux / macOS：

```bash
cp config.example.env config.env
# 编辑 config.env 填入配置
./start_bot.sh
```

## 配置说明

复制 `config.example.env` 为 `config.env`（密钥文件已被 `.gitignore` 忽略，不会提交）。也可以不改文件，直接用同名环境变量。

常用配置项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BACKEND` | `deepseek` | `deepseek` 或 `ollama` |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | DeepSeek 模型名，以官网为准 |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://127.0.0.1:11434` / `qwen3` | 本地模型配置 |
| `PROACTIVE_CHAT_ID` | 空 | 主动消息发到哪个会话；留空自动从历史记录取 |
| `REPLY_DEBOUNCE_SECONDS` | `3.0` | 连发合并窗口：连着发几条时等你说完再一起回 |
| `REPLY_SPEED_TIERS` | `20,60,20` | 秒回/正常/慢回 的权重 |
| `REPLY_SLOW_RANGE` | `10-25` | 慢回档的等待秒数区间 |
| `TYPING_CPS` | `2.5-4.5` | 打字速度（字/秒），用于估算每条消息的行动时间 |
| `MAX_MESSAGES_PER_REPLY` | `6` | 一次回复最多发几条（超出会合并） |
| `FILLER_PROB` | `0.25` | 先发一句缓冲话（"等下""我看看"）的概率 |
| `DAILY_SUMMARY_MORNING` / `_NIGHT` | `08:30` / `22:00` | 早晚清单汇总（没有待办就不发） |
| `RANT_PER_WEEK` | `3` | 每周主动发牢骚次数上限（0 = 关闭） |
| `PROACTIVE_COOLDOWN_MINUTES` | `45` | 主动消息冷却，避免变成骚扰 |
| `MEMORY_SUMMARY_EVERY` | `6` | 每几轮对话总结一次记忆 |
| `MEMORY_EXTRACT_EVERY` | `3` | 每几轮做一次结构化记忆抽取 |
| `MEMORY_CHECK_MINUTES` | `60` | 每隔多久检查一次记忆书 |
| `MEAL_*` | 早 7-8 / 午 11:30-12:30 / 晚 18-19 | 饭点问候窗口（北京时间） |
| `PROACTIVE_ACTIVE_WINDOW` | `07:00-22:00` | 主动消息只在这个时段发 |
| `IDLE_FOLLOWUP_PROB` | `0.25` | 10/15 分钟没回消息时的跟进触发概率 |
| `STICKER_SEND_PROB` | `0.35` | 回复带表情包的概率 |
| `CITY` | `香港` | 天气城市（也可让对方发定位） |
| `SEARCH_API_KEY` | 空 | Tavily 搜索 API Key |

## 自定义你的角色

### 1. 角色卡（persona.txt）

写清楚：身份背景、性格、说话风格（称呼、语气、口头禅）、行为习惯、与对话者的关系。仓库自带一份原创示例角色（夕颜），包含 39 条行为规则，可以直接改用。

### 2. 世界书（worldbook.txt）

写角色的世界观和记忆：世界舞台、共同经历、人物关系、使用规则。会追加在角色卡之后一起注入模型。

### 3. 性格与情绪参数（personality.json）

调整 `sarcasm`（毒舌）、`tsundere`（傲娇）、`playfulness`、`warmth`、`assertiveness`、`vulgarity`（脏话倾向，设 0 即禁用）、`teasing`、`patience`、`expressiveness` 就能改变性格，不用动代码。情绪衰减速度和事件影响也可以在这里覆盖。

### 4. 特殊日子（special_events.py）

在文件顶部添加节日/角色生日/对方生日（支持公历、农历、除夕）。默认只启用通用节日，生日默认关闭；到日子会发一条真心话式的短消息。

### 5. 表情包（stickers.json）

用 [@BotFather](https://t.me/BotFather) 创建表情包后，把每个贴纸的 `file_id` 按情绪分类填进去：

```json
{
  "enabled": true,
  "categories": {
    "happy": [{"label": "开心1", "file_id": "CAAC..."}],
    "shy": [{"label": "害羞1", "file_id": "CAAC..."}]
  }
}
```

分类可选：`happy` 开心、`pout` 傲娇、`sad` 委屈、`surprised` 惊讶、`lovey` 撒娇、`shy` 害羞、`sleepy` 犯困、`flat` 无语、`gag` 搞怪。

## 目录结构

```
bot.py              主程序（消息收发、真人节奏、主动消息、命令）
emotion.py          情绪引擎（12 种情绪 + 连续数值 + 衰减）
state_store.py      每个用户的情绪/关系/回复记录
analyzer.py         消息分析（把一句话翻译成情绪事件）
strategy.py         消息策略（说几句、多快回、要不要连发）
prompting.py        分层 Prompt 组装 + 回复校验
memory_book.py      第二本世界书（记忆的总结 / 压缩 / 重建）
memos.py            备忘录存储
events.py           主动开口的素材（牢骚 / 短聊 / 收尾）
scheduler.py        主动消息调度 + 记忆维护
reminders.py        提醒与喝水服务
special_events.py   节日/生日短消息
tools.py            天气与搜索
lunar.py            农历转换
config.example.env  配置模板（复制为 config.env 使用）
persona.txt         角色卡（自带原创示例角色）
worldbook.txt       世界书（自带原创示例）
personality.json    性格与情绪参数（可调）
stickers.json       表情包配置
Dockerfile / docker-compose.yml / render.yaml  部署配置
start_bot.bat / start_bot.sh  本机启动脚本
```

## 常见问题

**需要代理吗？** 本地部署在国内访问 Telegram API 需要代理。设置环境变量即可（urllib 会自动使用）：

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
```

部署在 Render 等海外平台则不需要。

**时区不对怎么办？** 确保运行环境 `TZ=Asia/Shanghai`（Docker 和 render.yaml 已配好；本机 Windows 直接使用系统时区）。

**开机自启**：机器人里发 `/autostart on`（仅 Windows 本机可用）。

**模型报错？** 先在配置里确认 `DEEPSEEK_MODEL` 是官网当前有效的模型名；也可以用 `python bot.py --self-test` 检查 token 是否有效。

## 版权与免责

- 本项目的**代码**采用 MIT 许可证（见 LICENSE，可改为你自己的署名）。
- 仓库中的 `persona.txt`、`worldbook.txt` 是本项目**原创**的示例角色（夕颜），不包含任何第三方作品的受版权保护内容；表情包配置默认为空。
- 请勿将任何第三方作品的角色、台词、剧情或图片打包进你的公开仓库，以免侵权；如需使用请自行获得授权或改用原创设定。
