# Telegram AI 聊天机器人

一个可以扮演任意角色、带主动消息和记忆功能的 Telegram 机器人。后端支持 **DeepSeek API**（云端）和 **Ollama**（本地模型），纯 Python 标准库实现，**零第三方依赖**，开箱即用。

> 本仓库不包含任何第三方作品（游戏、动漫、小说等）的角色、剧情或台词。`persona.txt`、`worldbook.txt` 及素材书均为**空白模板**，请只填入你自己的原创内容后再公开使用。

## 功能

- **角色扮演**：通过 `persona.txt`（角色卡）+ `worldbook.txt`（世界书）定制人设和世界观
- **主动消息**：饭点问候（早 7-8 点 / 午 11:30-12:30 / 晚 18-19 点，北京时间）、日常长/短事件、节日生日长故事
- **角色记忆**：每 N 轮自动把聊天浓缩成"第二本世界书"，定期整理，省 token
- **真人聊天感**：短消息连发、输入状态、随机停顿、主动结束聊天（15 分钟没回）、10 分钟没回先问一句
- **表情包**：按文本情绪随机发表情包，支持斗图（识别对方表情包 emoji 回怼），尽量不重复
- **提醒服务**：自然语言记提醒（"周六买牛奶"）、循环喝水提醒
- **天气**：定位或城市名，早上问候附带天气
- **电脑状态**：查 CPU / 内存 / 磁盘（仅本地部署可用）
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
| `MEAL_*` | 早 7-8 / 午 11:30-12:30 / 晚 18-19 | 饭点问候窗口（北京时间） |
| `PROACTIVE_ACTIVE_WINDOW` | `07:00-22:00` | 主动消息只在这个时段发 |
| `IDLE_FOLLOWUP_PROB` | `0.25` | 10/15 分钟没回消息时的跟进触发概率 |
| `STICKER_SEND_PROB` | `0.35` | 回复带表情包的概率 |
| `CITY` | `香港` | 天气城市（也可让对方发定位） |
| `SEARCH_API_KEY` | 空 | Tavily 搜索 API Key |

## 自定义你的角色

### 1. 角色卡（persona.txt）

写清楚：身份背景、性格、说话风格（称呼、语气、口头禅）、行为习惯、与对话者的关系。示例格式见文件内注释。

### 2. 世界书（worldbook.txt）

写角色的世界观和记忆：世界舞台、共同经历、人物关系、特别的日子、使用规则。会追加在角色卡之后一起注入模型。

### 3. 日常事件素材书

- `long_story_book.json`：日常长事件素材（`key` / `title` / `short` / `long` 四个字段）
- `short_story_book.json`：日常短事件素材（`key` / `prompt` 两个字段）

直接替换成你自己的内容即可；文件里已放了通用示例。

### 4. 特殊日子（special_events.py）

在文件顶部的列表里添加你的节日/角色生日/对方生日（支持公历、农历、除夕），每个日子可写一段专属剧情角度。默认只启用通用节日，生日为空。

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
bot.py              主程序（消息处理、主动事件、表情包、记忆注入）
config.example.env  配置模板（复制为 config.env 使用）
persona.txt         角色卡模板
worldbook.txt       世界书模板
special_events.py   节日/生日特殊事件（可编辑）
events.py           日常长/短事件逻辑
long_story_book.json  日常长事件素材书
short_story_book.json 日常短事件素材书
stickers.json       表情包配置
memory_book.py      第二本世界书（对话记忆）
reminders.py        提醒与喝水服务
scheduler.py        主动消息调度
pcstatus.py         电脑状态查询
tools.py            天气、搜索工具
lunar.py            农历转换
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
- 仓库中的 `persona.txt`、`worldbook.txt`、素材书、表情包均为空白/通用模板，**不包含任何第三方作品的受版权保护内容**。
- 请勿将任何第三方作品的角色、台词、剧情或图片打包进你的公开仓库，以免侵权；如需使用请自行获得授权或改用原创设定。
