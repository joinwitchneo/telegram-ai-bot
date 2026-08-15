#!/usr/bin/env python3
"""Telegram bot backed by a DeepSeek / Ollama chat model.

Zero third-party dependencies: uses only the Python standard library.
Long-polls the Telegram Bot API and chats with the configured backend.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pcstatus
import tools
import events
import special_events
from lunar import lunar_date_name, solar_to_lunar
from memory_book import MemoryBook
from reminders import (
    ReminderStore,
    ReminderThread,
    next_water_time,
    parse_reminder,
    parse_reminder_with_llm,
)
from scheduler import ProactiveScheduler

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.env"
DEFAULT_HISTORY = BASE_DIR / "history.json"
TG_API = "https://api.telegram.org/bot{token}/{method}"
MAX_REPLY_LEN = 4096
MAX_MESSAGE_LEN = 2000


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def resolve_setting(name: str, config: dict[str, str], default: str = "") -> str:
    return os.environ.get(name, "").strip() or config.get(name, "").strip() or default


def api_request(
    url: str,
    payload: dict | None = None,
    timeout: int = 60,
    retries: int = 3,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = dict(extra_headers or {})
    if data:
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers)
    last_error = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {body[:300]}"
            if exc.code in (400, 401, 403, 404, 409, 422):
                raise RuntimeError(last_error) from exc
        except Exception as exc:  # noqa: BLE001 - network layer
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"request failed after {retries} tries: {last_error}")


def tg_call(token: str, method: str, payload: dict | None = None, timeout: int = 60, retries: int = 3) -> dict:
    return api_request(TG_API.format(token=token, method=method), payload, timeout=timeout, retries=retries)


_FORCE_CPU = False


def _chat_once(model: str, messages: list[dict], base_url: str, options: dict, timeout: int) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": options,
    }
    result = api_request(base_url.rstrip("/") + "/api/chat", payload, timeout=timeout, retries=1)
    content = result.get("message", {}).get("content", "")
    if not content:
        raise RuntimeError(f"Ollama returned no content: {json.dumps(result, ensure_ascii=False)[:200]}")
    return content


def _looks_like_gpu_error(exc: Exception) -> bool:
    lowered = str(exc).lower()
    return "cuda" in lowered or "illegal instruction" in lowered


def deepseek_chat(
    model: str,
    messages: list[dict],
    base_url: str,
    api_key: str,
    timeout: int = 300,
    max_attempts: int = 3,
    max_tokens: int | None = None,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    last_error = "empty content"
    for attempt in range(1, max_attempts + 1):
        result = api_request(
            base_url.rstrip("/") + "/chat/completions",
            payload,
            timeout=timeout,
            retries=1,
            extra_headers={"Authorization": f"Bearer {api_key}"},
        )
        choices = result.get("choices") or []
        content = ""
        if choices:
            content = choices[0].get("message", {}).get("content")
            if isinstance(content, list):
                content = "\n".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            content = (content or "").strip()
        if content:
            return content
        last_error = json.dumps(result, ensure_ascii=False)[:200]
        if attempt < max_attempts:
            time.sleep(2 * attempt)
    raise RuntimeError(f"DeepSeek returned empty content after {max_attempts} attempts: {last_error}")


def deepseek_models(base_url: str, api_key: str) -> list[str]:
    result = api_request(
        base_url.rstrip("/") + "/models",
        timeout=30,
        retries=1,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    return [item.get("id", "?") for item in result.get("data", [])]


def ollama_chat(model: str, messages: list[dict], base_url: str, timeout: int = 300) -> str:
    global _FORCE_CPU
    options = {"num_ctx": 8192}
    if _FORCE_CPU:
        options["num_gpu"] = 0
    try:
        return _chat_once(model, messages, base_url, options, timeout)
    except RuntimeError as exc:
        if _FORCE_CPU or not _looks_like_gpu_error(exc):
            raise
        logging.warning("GPU inference failed (%s); retrying on CPU", exc)
        _FORCE_CPU = True
        return _chat_once(model, messages, base_url, {"num_ctx": 4096, "num_gpu": 0}, timeout)


def ollama_tags(base_url: str) -> list[dict]:
    result = api_request(base_url.rstrip("/") + "/api/tags", timeout=30, retries=1)
    return result.get("models", [])


class Memory:
    """Per-chat conversation history persisted to a JSON file."""

    def __init__(self, path: Path, max_turns: int) -> None:
        self.path = path
        self.max_turns = max_turns
        self._lock = threading.Lock()
        self.data: dict[str, list[dict]] = {}
        if path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, chat_id: int) -> list[dict]:
        with self._lock:
            return self.data.setdefault(str(chat_id), [])

    def add(self, chat_id: int, role: str, content: str) -> None:
        with self._lock:
            chat = self.data.setdefault(str(chat_id), [])
            chat.append({"role": role, "content": content[:MAX_MESSAGE_LEN]})
            if len(chat) > self.max_turns * 2:
                del chat[: len(chat) - self.max_turns * 2]

    def clear(self, chat_id: int) -> None:
        with self._lock:
            self.data.pop(str(chat_id), None)
        self.save()

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=2)
        try:
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("could not save history: %s", exc)


def send_message(token: str, chat_id: int, text: str) -> None:
    text = text.strip() or "…"
    for chunk in (text[i : i + MAX_REPLY_LEN] for i in range(0, len(text), MAX_REPLY_LEN)):
        tg_call(token, "sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=60, retries=5)


def _chunk_text(text: str, limit: int) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [text]


def split_chatty(text: str, max_segments: int = 40) -> list[str]:
    """短对话拆消息：
    - 句号/感叹号/问号（。！？!?）之后断开：句号省略，感叹号/问号保留；
    - 逗号（，,）：10~15 个中文字符的句子带逗号就不断句、保留逗号；否则在逗号处断开并省略逗号；
    - 字与字之间绝不拆。长故事不走这个函数。"""
    text = text.strip()
    if not text:
        return [text]
    sentences: list[str] = []
    for seg in re.split(r"(?<=[。！？!?])", text):
        seg = re.sub(r"[。]+$", "", seg).strip()  # 句号是断开标记，省略
        if seg:
            sentences.append(seg)
    result: list[str] = []
    for sentence in sentences:
        cjk_len = len(re.findall(r"[\u4e00-\u9fff]", sentence))
        has_comma = "，" in sentence or "," in sentence
        if has_comma and 10 <= cjk_len <= 15:
            result.append(sentence)  # 10~15 字带逗号：不断句、保留逗号
        elif has_comma:
            result.extend(part.strip() for part in re.split(r"[，,]", sentence) if part.strip())
        else:
            result.append(sentence)
    return result[:max_segments] or [text]


def send_chatty(token: str, chat_id: int, text: str, min_delay: float = 0.4, max_delay: float = 1.0) -> None:
    segments = split_chatty(text)
    for index, segment in enumerate(segments):
        try:
            tg_call(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=30, retries=2)
        except Exception:  # noqa: BLE001 - typing indicator is cosmetic
            pass
        time.sleep(random.uniform(1.2, 2.8))  # 假装在输入
        send_message(token, chat_id, segment)
        if index < len(segments) - 1:
            time.sleep(random.uniform(min_delay, max_delay))


EMOTION_KEYWORDS: dict[str, list[str]] = {
    "surprised": ["诶——", "诶诶", "诶？", "什么？！", "什么!", "真的假的", "吓", "震惊", "居然", "不会吧", "哇哦", "天哪", "呜哇", "怎么可能", "诶诶诶"],
    "shy": ["脸红", "害羞", "羞", "别说了", "啊啊啊", "捂脸", "心跳", "好羞", "别看我"],
    "pout": ["哼", "才不是", "谁想", "讨厌", "生气", "气死", "不理你", "笨蛋", "傻瓜", "蠢", "傲娇", "赌气", "闹别扭", "哼唧", "活该", "吃醋", "不理"],
    "sad": ["委屈", "难过", "伤心", "想哭", "哭了", "呜呜", "寂寞", "失落", "孤单", "一个人", "唉", "哭唧唧", "泪", "好想哭", "想哭"],
    "lovey": ["想你了", "想你", "撒娇", "黏", "抱抱", "亲亲", "陪你", "陪陪我", "好不好嘛", "拜托", "最爱", "最喜欢", "爱你", "甜甜", "贴贴"],
    "happy": ["嘿嘿", "嘻嘻", "哈哈", "开心", "高兴", "得意", "太好", "耶！", "耶~", "万岁", "赢", "翘尾巴", "骄傲", "♪", "好耶", "顺利"],
    "sleepy": ["困", "睡了", "睡觉", "晚安", "熬夜", "眼皮", "哈欠", "梦", "好累", "累死"],
    "gag": ["笑死", "整活", "恶作剧", "偷偷", "藏起来", "拆家", "搞怪", "闯祸", "小坏事", "干坏事", "手滑", "口误", "翻车", "丢人", "糗"],
    "flat": ["无语", "懒得", "随便", "呵呵", "算了", "好吧", "没意思", "嗯嗯", "行吧"],
}
EMOTION_ORDER = ["surprised", "shy", "pout", "sad", "lovey", "happy", "sleepy", "gag", "flat"]

STICKER_TAG_RE = re.compile(r"[【（(]\s*(?:贴纸|表情包)\s*[:：]\s*([^】）)\s]+?)\s*[】）)]")
STICKER_TAG_TAIL_RE = re.compile(r"(?:贴纸|表情包)\s*[:：]\s*([^，。！？\s]+?)\s*$")
STICKER_CATEGORY_ALIASES = {
    "开心": "happy", "高兴": "happy", "得意": "happy",
    "撒娇": "lovey", "黏人": "lovey",
    "傲娇": "pout", "生气": "pout", "吃醋": "pout",
    "委屈": "sad", "难过": "sad", "伤心": "sad", "哭": "sad",
    "惊讶": "surprised", "震惊": "surprised", "吓": "surprised",
    "害羞": "shy", "脸红": "shy",
    "犯困": "sleepy", "困": "sleepy", "睡": "sleepy",
    "无语": "flat", "平静": "flat", "冷淡": "flat",
    "搞怪": "gag", "搞笑": "gag", "整活": "gag",
    "happy": "happy", "lovey": "lovey", "pout": "pout", "sad": "sad",
    "surprised": "surprised", "shy": "shy", "sleepy": "sleepy", "flat": "flat", "gag": "gag",
}
EMOJI_CATEGORY_MAP = {
    "happy": ["😂", "🤣", "😄", "😁", "😆", "😊", "☺️", "☺", "😃", "😎", "😜", "😝", "😋", "😉", "🥳", "😺"],
    "pout": ["😡", "😠", "😤", "🤬", "💢", "😾", "😒", "🙄"],
    "sad": ["😭", "😢", "😥", "😞", "😔", "🙁", "😿", "😣", "😫", "😩", "🥺"],
    "surprised": ["😱", "😧", "😯", "😮", "😲", "🤯", "🙀"],
    "lovey": ["🥰", "😍", "😘", "😚", "😙", "💕", "💗", "💞", "❤️", "❤", "💘", "😻"],
    "shy": ["😳", "😖", "😰", "😅"],
    "sleepy": ["😴", "😪", "🥱", "😌", "💤"],
    "flat": ["😐", "😑", "😶", "🤔", "😕", "🤨", "😬"],
    "gag": ["💵", "👀", "🎵", "🤪", "😹", "👍", "🙈", "🙉", "🙊", "💀", "👻"],
}


def emoji_to_category(emoji: str) -> str | None:
    """根据用户表情包的 emoji 注释，判断对应的角色表情包分类（斗图用）。"""
    if not emoji:
        return None
    for category, emojis in EMOJI_CATEGORY_MAP.items():
        if any(e in emoji for e in emojis):
            return category
    return None


def extract_sticker_tag(text: str) -> tuple[str, str | None]:
    """解析模型回复里的表情包暗号【贴纸:分类】（位置不限），返回（去掉暗号的正文，分类key或None）。"""
    for pattern in (STICKER_TAG_RE, STICKER_TAG_TAIL_RE):
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).strip()
        category = STICKER_CATEGORY_ALIASES.get(raw) or STICKER_CATEGORY_ALIASES.get(raw.lower())
        clean = (text[: match.start()] + text[match.end() :]).strip()
        return clean, category
    return text, None


def wants_sticker(text: str) -> bool:
    """用户明确要表情包/贴纸。"""
    lowered = text.lower()
    return any(keyword in lowered for keyword in ("表情包", "贴纸", "sticker", "发张表情", "来个表情"))

SELF_STATE_WORDS = [
    "累", "困", "饿", "冷", "热", "感冒", "发烧", "头疼", "头痛", "难受", "不舒服",
    "加班", "工作", "出差", "心情", "难过", "伤心", "开心", "忙", "吃", "睡", "失眠",
    "想家", "孤单", "寂寞", "今天", "昨天", "最近", "这边", "这里", "刚", "现在",
    "有点", "下雨", "天气", "被", "又",
]


def detect_emotion(text: str) -> str:
    """根据回复文本里的语气词判断情绪分类（同分时按 EMOTION_ORDER 优先级取）。"""
    best, best_score = "flat", 0
    for cat in EMOTION_ORDER:
        score = sum(text.count(kw) for kw in EMOTION_KEYWORDS[cat])
        if score > best_score:
            best, best_score = cat, score
    return best if best_score else "neutral"


def maybe_send_sticker(
    token: str,
    chat_id: int,
    text: str,
    stickers: dict,
    probability: float = 0.35,
    force_category: str | None = None,
    used: set[str] | None = None,
) -> None:
    """表情包：模型指定分类（force_category）就发那张；否则按文本情绪随机补一张，概率内不发。
    发过的贴纸（used）先跳过，一轮发完再重置，尽量不重复。"""
    if not stickers:
        return
    if force_category:
        base = stickers.get(force_category) or [st for group in stickers.values() for st in group]
        cat = force_category
    else:
        if probability <= 0 or random.random() > probability:
            return
        cat = detect_emotion(text)
        if cat == "neutral":
            base = [st for group in stickers.values() for st in group]
        else:
            base = stickers.get(cat) or stickers.get("happy") or []
    if used is not None:
        pool = [st for st in base if st.get("label") not in used]
        if not pool:
            pool = list(base)
            used.clear()
    else:
        pool = base
    if not pool:
        return
    time.sleep(random.uniform(0.4, 1.2))  # 像真人补一张贴纸的停顿
    pick = random.choice(pool)
    if used is not None:
        used.add(pick.get("label", ""))
    try:
        tg_call(token, "sendSticker", {"chat_id": chat_id, "sticker": pick["file_id"]}, timeout=60, retries=3)
        logging.info("[sticker] %s (%s) -> chat %s", pick.get("label", "?"), cat, chat_id)
    except Exception as exc:  # noqa: BLE001
        logging.warning("sticker send failed (%s): %s", pick.get("label", "?"), exc)


def build_reply_messages(system_prompt: str, history: list[dict], user_text: str) -> list[dict]:
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    return messages


class Bot:
    PROACTIVE_PROMPTS = {
        "morning": (
            "（现在是早晨）以你的角色设定，主动给对方发一条早安问候，提醒他吃早饭、注意休息，"
            "40~70 字，自然亲近，符合你的人设。"
        ),
        "noon": (
            "（现在是中午）以你的角色设定，主动问对方午饭吃了什么，顺便说说自己今天在忙什么，"
            "40~70 字，符合你的人设。"
        ),
        "evening": (
            "（现在是傍晚）以你的角色设定，主动关心对方今天过得怎么样，问他晚饭想吃什么，"
            "40~70 字，符合你的人设。"
        ),
        "afternoon": (
            "（现在是下午/傍晚）以你的角色设定，主动关心对方今天在忙什么、提醒他注意休息，"
            "自然亲近，40~70 字，符合你的人设。不要问吃饭。"
        ),
        "night": (
            "（现在是深夜）以你的角色设定，催对方早点睡、别熬夜，语气又关心又带点管束，"
            "40~70 字，符合你的人设。"
        ),
        "idle": (
            "（对方很久没理你了）以你的角色设定，给对方发一条带点小委屈的亲近消息，"
            "40~70 字，符合你的人设。"
        ),
        "event": (
            "（现在是特别的日子）以你的角色设定，主动和对方分享一件最近发生的趣事或囧事："
            "生活里的小插曲、小失误、被认出来的尴尬、偷偷干的小坏事都可以。"
            "拆成 2~4 条短消息（用换行分隔），语气自然亲近，像在说悄悄话；不要邀约出门。"
        ),
    }

    def __init__(
        self,
        token: str,
        backend: str,
        model: str,
        base_url: str,
        api_key: str,
        system_prompt: str,
        memory: Memory,
        proactive_chat_id: int | None = None,
        city: str = "香港",
        briefing_enabled: bool = True,
        search_api_key: str = "",
        search_api_url: str = "https://api.tavily.com/search",
        reminders: ReminderStore | None = None,
        photo_dir: Path | None = None,
        see_output_dir: Path | None = None,
        stickers: dict | None = None,
        sticker_prob: float = 0.35,
        memory_book_path: Path | None = None,
        memory_summary_every: int = 6,
        memory_inject_limit: int = 2500,
        memory_condense_at: int = 25,
        event_min_chars: int = 600,
        event_max_chars: int = 800,
        event_max_tokens: int = 2000,
        char_event_days: int = 5,
        short_event_days: int = 2,
        ask_busy_after_minutes: int = 10,
        leave_after_minutes: int = 15,
        idle_followup_prob: float = 0.25,
        reply_style: str = "mixed",
        care_banter_prob: float = 0.2,
        event_active_window: int = 90,
    ) -> None:
        self.token = token
        self.backend = backend
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.memory = memory
        self.proactive_chat_id = proactive_chat_id
        self.last_activity = time.time()
        self.city = city
        self.briefing_enabled = briefing_enabled
        self.search_api_key = search_api_key
        self.search_api_url = search_api_url
        self.reminders = reminders
        self.photo_dir = photo_dir or (BASE_DIR / "media")
        self.see_output_dir = see_output_dir or (BASE_DIR / "see_outputs")
        self.stickers = stickers or {}
        self.sticker_prob = sticker_prob
        self.event_min_chars = event_min_chars
        self.event_max_chars = event_max_chars
        self.event_max_tokens = event_max_tokens
        self.char_event_days = char_event_days
        self.short_event_days = short_event_days
        self.ask_busy_after_minutes = ask_busy_after_minutes
        self.leave_after_minutes = leave_after_minutes
        self.idle_followup_prob = max(0.0, min(1.0, idle_followup_prob))
        self.last_outgoing_at = time.time()
        self.reply_style = reply_style
        self.care_banter_prob = max(0.0, min(1.0, care_banter_prob))
        self.event_active_window = max(0, event_active_window)
        self.memory_book = MemoryBook(
            memory_book_path or (BASE_DIR / "memories.json"),
            self._ask,
            summary_every=memory_summary_every,
            inject_limit=memory_inject_limit,
            condense_at=memory_condense_at,
        )
        # 事件去重状态（长事件/短事件各自轮换，用过的先跳过）
        self._state_file = BASE_DIR / "events_state.json"
        self._used_long: set[str] = set()
        self._used_short: set[str] = set()
        self._used_stickers: set[str] = set()
        try:
            state = json.loads(self._state_file.read_text(encoding="utf-8"))
            self._used_long = set(state.get("daily_long_used", []))
            self._used_short = set(state.get("short_used", []))
            self._used_stickers = set(state.get("stickers_used", []))
        except (OSError, json.JSONDecodeError):
            pass

    def _ask(self, messages: list[dict], max_tokens: int | None = None) -> str:
        if self.backend == "deepseek":
            return deepseek_chat(self.model, messages, self.base_url, self.api_key, max_tokens=max_tokens)
        return ollama_chat(self.model, messages, self.base_url)

    def reply_chatty(self, chat_id: int, text: str) -> str:
        """短消息连发 + 表情包：模型带【贴纸:分类】暗号就发那张，否则按随机概率按情绪发。
        如果回复只有暗号没有正文，就只发表情包。返回去掉暗号的干净文本（只发表情包时为"[表情包]"）。"""
        clean, category = extract_sticker_tag(text)
        if clean:
            send_chatty(self.token, chat_id, clean)
        if clean or category:
            maybe_send_sticker(
                self.token,
                chat_id,
                clean,
                self.stickers,
                self.sticker_prob,
                force_category=category,
                used=self._used_stickers,
            )
            self._save_event_state()
        self.last_outgoing_at = time.time()
        if not clean and category:
            return "[表情包]"
        return clean

    def _system_for(self, chat_id: int) -> str:
        """系统提示词 + 角色记忆书（第二世界书），保持记忆且省 token。"""
        memory_text = self.memory_book.injection(chat_id)
        if memory_text:
            return f"{self.system_prompt}\n\n## 角色记忆（第二世界书）\n{memory_text}"
        return self.system_prompt

    def _style_instruction(self) -> str:
        """按概率给本次回复定风格：短对话（≤15字）/中对话（~75字）/默认。"""
        mode = self.reply_style
        if mode == "short":
            return "（本条回复用短对话风格：整条回复只发 1 条消息，15 个汉字以内，特别简短自然，像真人随手回一条，可以只回两三个字。）"
        if mode == "medium":
            return "（本条回复用中对话风格：发 1~2 条消息，每条 75 个汉字左右，自然一点，可以带一个语气词或小动作。）"
        if mode == "normal":
            return ""
        roll = random.random()
        if roll < 0.45:
            return "（本条回复用短对话风格：整条回复只发 1 条消息，15 个汉字以内，特别简短自然，像真人随手回一条，可以只回两三个字。）"
        if roll < 0.85:
            return "（本条回复用中对话风格：发 1~2 条消息，每条 75 个汉字左右，自然一点，可以带一个语气词或小动作。）"
        return ""

    def _care_banter_hint(self, text: str) -> str:
        """对方提到自己的近况时，随机要求角色关心对方/拌嘴/先关心再拌嘴。"""
        if "我" not in text:
            return ""
        if not any(word in text for word in SELF_STATE_WORDS):
            return ""
        if random.random() < self.care_banter_prob:
            return (
                "（对方提到了自己的近况。回复时随机选一种：关心他几句 / 跟他开玩笑拌嘴 / "
                "先关心再拌一句嘴；自然一点，符合人设。）"
            )
        return ""

    def _chat_context(self, chat_id: int, turns: int = 4) -> str:
        """取最近几轮对话的浓缩背景，让主动事件能自然地接上聊天场景。"""
        history = self.memory.get(chat_id)
        lines = []
        for item in history[-turns:]:
            role = "对方" if item.get("role") == "user" else "角色"
            content = (item.get("content") or "").strip()
            if not content or content.startswith("["):
                continue
            lines.append(f"{role}：{content[:80]}")
        return " | ".join(lines[-4:])

    def _save_event_state(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps(
                    {
                        "daily_long_used": sorted(self._used_long),
                        "short_used": sorted(self._used_short),
                        "stickers_used": sorted(self._used_stickers),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logging.warning("could not save event state: %s", exc)

    def handle(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id:
            return
        self.last_activity = time.time()
        self.memory_book.touch_user(chat_id, message.get("from") or {})
        if message.get("location"):
            self.handle_location(chat_id, message["location"])
            return
        if message.get("photo"):
            self.handle_photo(chat_id, message)
            return
        if message.get("sticker"):
            self.handle_sticker(chat_id, message)
            return
        if not text:
            send_message(self.token, chat_id, "这条消息我看不懂，发文字或者照片给我嘛！")
            return

        if text.startswith("/"):
            self.handle_command(chat_id, text)
            return
        if self._is_help_question(text):
            self.cmd_help_in_character(chat_id)
            return
        if "电脑" in text:
            self.cmd_pcstatus(chat_id, "")
            return

        history = self.memory.get(chat_id)
        style = self._style_instruction()
        care = self._care_banter_hint(text)
        hints = " ".join(hint for hint in (style, care) if hint)
        if wants_sticker(text):
            hints += (
                "（对方要表情包：回复末尾一定要加【贴纸:分类】暗号，"
                "分类可选：开心、撒娇、傲娇、委屈、惊讶、害羞、犯困、无语、搞怪）"
            )
        user_content = f"{hints}\n{text}" if hints else text
        messages = build_reply_messages(self._system_for(chat_id), history, user_content)
        logging.info("[chat %s] asking %s (%s)", chat_id, self.backend, self.model)
        if self.backend == "deepseek":
            reply = deepseek_chat(self.model, messages, self.base_url, self.api_key)
        else:
            reply = ollama_chat(self.model, messages, self.base_url)
        clean_reply = self.reply_chatty(chat_id, reply)
        if wants_sticker(text) and not extract_sticker_tag(reply)[1]:
            # 兜底：对方明确要表情包但模型没带暗号 → 必发一张
            maybe_send_sticker(
                self.token,
                chat_id,
                clean_reply,
                self.stickers,
                1.0,
                used=self._used_stickers,
            )
            self._save_event_state()
        self.memory.add(chat_id, "user", text)
        self.memory.add(chat_id, "assistant", clean_reply)
        self.memory.save()
        snapshot = [dict(t) for t in self.memory.get(chat_id)[- self.memory_book.summary_every * 2:]]
        threading.Thread(target=self._summarize_worker, args=(chat_id, snapshot), daemon=True).start()

    def _summarize_worker(self, chat_id: int, snapshot: list[dict]) -> None:
        """后台把最近聊天浓缩进第二本世界书，不阻塞聊天。"""
        try:
            self.memory_book.maybe_summarize(chat_id, snapshot)
        except Exception as exc:  # noqa: BLE001
            logging.warning("memory summarize worker failed: %s", exc)

    HELP_KEYWORDS = [
        "你能做什么", "你能干什么", "你能干嘛", "你会什么",
        "有什么功能", "有什么用", "能做什么", "能干什么", "能干嘛",
        "会什么", "怎么用",
    ]

    def _is_help_question(self, text: str) -> bool:
        lowered = text.lower().strip()
        if lowered in ("help", "/help", "帮助"):
            return True
        return any(keyword in text for keyword in self.HELP_KEYWORDS)

    def cmd_help_in_character(self, chat_id: int) -> None:
        """对方问"你能做什么"时，按角色设定的功能总结。"""
        features = (
            "1. 记提醒/待办：直接说「周六买牛奶」「明天8点提醒我吃药」，到点我提醒你，还有喝水提醒。\n"
            "2. 查电脑：说「电脑」或发 /pcstatus，我能看 CPU/内存/磁盘。\n"
            "3. 定位天气：发个定位给我，早上的天气简报就用你的位置。\n"
            "4. 记性超好：聊过的事、你说过的话、约定，我都记得（第二世界书）。\n"
            "5. 主动关心：饭点提醒你吃饭、日常主动找你聊天、节日和生日给你讲长故事。\n"
            "6. 表情包：聊天时我会随机给你发表情包~"
        )
        prompt = (
            "对方问“你能做什么、有什么用”（相当于帮助菜单）。以你的角色设定回答："
            "短消息连发（换行分隔），先自然回应一句，"
            f"再列出这些功能（可以简化成自己的说法，但别漏）：{features}。"
            "结尾带一句符合角色设定的总结语。保持人设，不要跳出角色。"
        )
        reply = self._ask(
            [
                {"role": "system", "content": self._system_for(chat_id)},
                {"role": "user", "content": prompt},
            ]
        )
        self.reply_chatty(chat_id, reply)

    def send_special_event(self, evt: dict) -> None:
        """节日/生日特殊事件：先短祝福，再整段生成一篇长事件发给对方。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        name = evt.get("name", "特别的日子")
        context = self._chat_context(chat_id)
        try:
            prompt = special_events.special_short_prompt(evt)
            if context:
                prompt = f"（最近聊天背景：{context}。让铺垫自然地接上最近聊的内容。）\n{prompt}"
            blessing = self._ask(
                [
                    {"role": "system", "content": self._system_for(chat_id)},
                    {"role": "user", "content": prompt},
                ]
            )
            self.reply_chatty(chat_id, blessing)
            logging.info("special event blessing sent: %s", name)
        except Exception as exc:  # noqa: BLE001
            logging.error("special event blessing failed (%s): %s", name, exc)
        try:
            prompt = special_events.special_long_prompt(evt, self.event_min_chars, self.event_max_chars)
            if context:
                prompt = f"（最近聊天背景：{context}。故事开头要先自然接上对方最近聊的内容和关心的事，再展开。）\n{prompt}"
            long_text = self.generate_long_event(prompt)
            self.send_long_text(chat_id, long_text)
            logging.info("special event long text sent: %s (%d chars)", name, len(long_text))
        except Exception as exc:  # noqa: BLE001
            logging.error("special event long text failed (%s): %s", name, exc)
        note = f"{name}：角色给对方发了祝福和一整篇长长的故事，对方可以接着这个话题聊。"
        self.memory_book.add_note(chat_id, note)
        self.memory.add(chat_id, "assistant", f"[特殊事件] {name} 祝福+长故事已发送")
        self.memory.save()

    def generate_long_event(self, prompt: str) -> str:
        """整段一次生成一篇长事件文本（不分章）。"""
        messages = [
            {"role": "system", "content": self._system_for(self.proactive_chat_id)},
            {"role": "user", "content": prompt},
        ]
        text = self._ask(messages, max_tokens=self.event_max_tokens)
        text = (text or "").strip()
        if not text:
            text = "（今天的故事没编出来……下次一定！）"
        return text

    def send_long_text(self, chat_id: int, text: str) -> None:
        """把长文本按段落切成 <=3800 字的若干条消息连发（带输入状态和停顿）。"""
        text, category = extract_sticker_tag(text)
        text = text.strip()
        if not text:
            return
        paragraphs = [p.strip() for p in re.split(r"[\n]+", text) if p.strip()]
        chunks: list[str] = []
        buffer = ""
        for para in paragraphs:
            if buffer and len(buffer) + len(para) + 1 > 3800:
                chunks.append(buffer)
                buffer = para
            else:
                buffer = f"{buffer}\n{para}" if buffer else para
        if buffer:
            chunks.append(buffer)
        for index, chunk in enumerate(chunks[:60]):
            try:
                tg_call(self.token, "sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=30, retries=2)
            except Exception:  # noqa: BLE001 - 输入状态是锦上添花
                pass
            time.sleep(random.uniform(1.5, 3.0))  # 假装在输入长文
            send_message(self.token, chat_id, chunk)
            if index < len(chunks) - 1:
                time.sleep(random.uniform(0.4, 1.0))
        self.last_outgoing_at = time.time()
        if category:
            maybe_send_sticker(
                self.token,
                chat_id,
                text,
                self.stickers,
                1.0,
                force_category=category,
                used=self._used_stickers,
            )
            self._save_event_state()

    def send_daily_event(self) -> None:
        """日常事件（每月 5~6 次）：像节日/生日事件一样，先短铺垫，再整段长故事。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        evt = events.pick_daily_long(self._used_long)
        if not evt:
            return
        self._save_event_state()
        title = evt.get("title", "日常事件")
        context = self._chat_context(chat_id)
        try:
            prompt = events.daily_short_prompt(evt)
            if context:
                prompt = f"（最近聊天背景：{context}。让铺垫自然地接上最近聊的内容。）\n{prompt}"
            short = self._ask(
                [
                    {"role": "system", "content": self._system_for(chat_id)},
                    {"role": "user", "content": prompt},
                ]
            )
            self.reply_chatty(chat_id, short)
            logging.info("daily event short sent: %s", title)
        except Exception as exc:  # noqa: BLE001
            logging.error("daily event short failed (%s): %s", title, exc)
        try:
            prompt = events.daily_long_prompt(evt, self.event_min_chars, self.event_max_chars)
            if context:
                prompt = f"（最近聊天背景：{context}。故事开头要先自然接上对方最近聊的内容和关心的事，再展开。）\n{prompt}"
            long_text = self.generate_long_event(prompt)
            self.send_long_text(chat_id, long_text)
            logging.info("daily event long text sent: %s (%d chars)", title, len(long_text))
        except Exception as exc:  # noqa: BLE001
            logging.error("daily event long text failed (%s): %s", title, exc)
        self.memory_book.add_note(chat_id, f"日常事件：{title}——角色先铺垫、又讲了一整篇长故事，对方可以接着聊。")
        self.memory.add(chat_id, "assistant", f"[日常事件] {title} 短铺垫+长故事已发送")
        self.memory.save()

    def send_short_event(self) -> None:
        """短事件（每月 10~15 次）：角色主动找对方短聊——求打气/想说的话/天气闲聊/小知识等。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        short_evt = events.pick_short_event(self._used_short)
        if not short_evt:
            return
        self._save_event_state()
        prompt = short_evt["prompt"]
        prompt += "（记住：保持角色设定和人设一致，不聊越界内容。）"
        prompt += "（记得翻一下角色记忆：如果之前和对方聊过什么——他的近况、约定、说过的话——自然地提一句，增加代入感；想不起来就别硬提。）"
        if "天气" in prompt:
            weather = tools.get_weather(self._weather_city())
            if weather:
                prompt += f"\n（对方那边今天的天气：{weather}，可以自然地用上。）"
        try:
            reply = self._ask(
                [
                    {"role": "system", "content": self._system_for(chat_id)},
                    {"role": "user", "content": prompt},
                ]
            )
            clean = self.reply_chatty(chat_id, reply)
            self.memory.add(chat_id, "assistant", clean)
            self.memory_book.add_note(chat_id, "角色主动找对方短聊了几句（求打气/想说的话/闲聊）")
            self.memory.save()
            logging.info("short event sent to %s", chat_id)
        except Exception as exc:  # noqa: BLE001
            logging.error("short event failed: %s", exc)

    def send_leave_event(self) -> None:
        """主动结束聊天：角色用去工作/吃饭/睡觉等理由收尾这轮聊天。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        try:
            reply = self._ask(
                [
                    {"role": "system", "content": self._system_for(chat_id)},
                    {"role": "user", "content": events.leave_prompt()},
                ]
            )
            clean = self.reply_chatty(chat_id, reply)
            self.memory.add(chat_id, "assistant", clean)
            self.memory_book.add_note(chat_id, "角色主动跟对方说要先去忙了，结束了这轮聊天")
            self.memory.save()
            logging.info("leave event sent to %s", chat_id)
        except Exception as exc:  # noqa: BLE001
            logging.error("leave event failed: %s", exc)

    def send_ask_busy(self) -> None:
        """对方十分钟没回消息：角色先问一句"在忙吗"。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        try:
            reply = self._ask(
                [
                    {"role": "system", "content": self._system_for(chat_id)},
                    {"role": "user", "content": events.ask_busy_prompt()},
                ]
            )
            clean = self.reply_chatty(chat_id, reply)
            self.memory.add(chat_id, "assistant", clean)
            self.memory_book.add_note(chat_id, "角色看对方好久没回消息，问他在不在忙")
            self.memory.save()
            logging.info("ask-busy sent to %s", chat_id)
        except Exception as exc:  # noqa: BLE001
            logging.error("ask-busy failed: %s", exc)

    def handle_command(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        handlers = {
            "/remind": self.cmd_remind,
            "/reminders": self.cmd_reminders,
            "/delremind": self.cmd_delremind,
            "/water": self.cmd_water,
            "/pcstatus": self.cmd_pcstatus,
            "/autostart": self.cmd_autostart,
            "/search": self.cmd_search,
        }
        handler = handlers.get(command)
        if handler:
            handler(chat_id, rest)
            return
        if command == "/start":
            send_message(
                self.token,
                chat_id,
                "你好！我是你的 AI 助手～\n"
                "想聊什么直接说，想让我记事情、查电脑都可以；问一句“你能做什么”我就告诉你！",
            )
            return
        if command == "/help":
            self.cmd_help_in_character(chat_id)
            return
        if command == "/clear":
            self.memory.clear(chat_id)
            send_message(self.token, chat_id, "已清空本会话的历史记忆 ✅")
            return
        if command == "/status":
            self.status(chat_id)
            return
        send_message(self.token, chat_id, "这个指令我还不认识哦，试试 /help 看看我会什么！")

    def cmd_remind(self, chat_id: int, rest: str) -> None:
        if not rest:
            send_message(
                self.token,
                chat_id,
                "用法：直接说想提醒的事，比如「周六晚上买牛奶」「明天早上8点提醒我吃药」；\n"
                "我听得懂自然语言，记好会告诉你编号。",
            )
            return
        result = parse_reminder(rest)
        if result and result.get("repeat") == "water" and not result.get("when"):
            self.reminders.add(
                {
                    "chat_id": chat_id,
                    "due": next_water_time().isoformat(),
                    "content": result["content"],
                    "repeat": "water",
                }
            )
            send_message(self.token, chat_id, "好啦，我会每 2 小时提醒你喝水的~")
            return
        if not result or not result.get("when"):
            result = parse_reminder_with_llm(rest, lambda prompt: self._ask([{"role": "user", "content": prompt}]))
        if not result or not result.get("when"):
            send_message(self.token, chat_id, "呜哇，我没听懂时间……再说具体一点嘛，比如「明天早上8点」")
            return
        when = result["when"]
        item = {
            "chat_id": chat_id,
            "due": when.isoformat(),
            "content": result["content"],
            "repeat": result.get("repeat", ""),
        }
        if item["repeat"] == "water":
            item["due"] = next_water_time().isoformat()
        reminder_id = self.reminders.add(item)
        send_message(
            self.token,
            chat_id,
            f"记下啦~ {when.strftime('%m月%d日 %H:%M')} 我会提醒你：{result['content']}（编号 {reminder_id}）",
        )

    def cmd_reminders(self, chat_id: int, _rest: str) -> None:
        items = [r for r in self.reminders.all() if r.get("chat_id") == chat_id]
        if not items:
            send_message(self.token, chat_id, "现在没有待办的提醒哦~")
            return
        lines = []
        for item in sorted(items, key=lambda r: r.get("due", "")):
            due = item.get("due", "")
            try:
                due_text = datetime.datetime.fromisoformat(due).strftime("%m月%d日 %H:%M")
            except ValueError:
                due_text = due
            repeat = "（循环喝水）" if item.get("repeat") == "water" else ""
            lines.append(f"· [{item.get('id')}] {due_text} {item.get('content', '')}{repeat}")
        send_message(self.token, chat_id, "你的提醒清单：\n" + "\n".join(lines))

    def cmd_delremind(self, chat_id: int, rest: str) -> None:
        try:
            reminder_id = int(rest.strip())
        except ValueError:
            send_message(self.token, chat_id, "用法：/delremind 编号（用 /reminders 查看编号）")
            return
        if self.reminders.remove(reminder_id):
            send_message(self.token, chat_id, f"已删除提醒 {reminder_id}~")
        else:
            send_message(self.token, chat_id, f"没找到编号 {reminder_id} 的提醒哦")

    def cmd_water(self, chat_id: int, rest: str) -> None:
        action = rest.strip().lower()
        if action not in ("on", "off", "开", "关"):
            send_message(self.token, chat_id, "用法：/water on 开启循环喝水提醒（每 2 小时）｜/water off 关闭")
            return
        water_ids = [r.get("id") for r in self.reminders.all() if r.get("repeat") == "water" and r.get("chat_id") == chat_id]
        for reminder_id in water_ids:
            self.reminders.remove(reminder_id)
        if action in ("on", "开"):
            self.reminders.add(
                {
                    "chat_id": chat_id,
                    "due": next_water_time().isoformat(),
                    "content": "喝水",
                    "repeat": "water",
                }
            )
            send_message(self.token, chat_id, "好啦，每 2 小时我会催你喝水的~")
        else:
            send_message(self.token, chat_id, "喝水提醒关掉了。不过还是要记得喝水哦！")

    def cmd_pcstatus(self, chat_id: int, _rest: str) -> None:
        try:
            report = pcstatus.report()
            info = (
                f"CPU {report['cpu']}% | 内存 {report['memory']['percent']}% "
                f"（{report['memory']['used_gb']}/{report['memory']['total_gb']} GB）"
                f" | C盘剩 {report['disk']['free_gb']}GB"
            )
            reply = self._ask(
                [
                    {"role": "system", "content": self.system_prompt},
                    {
                        "role": "user",
                        "content": f"对方让你看一下他电脑的状态：{info}。用你的角色语气简短汇报（短消息连发，用换行分隔），可以顺带吐槽一句。",
                    },
                ]
            )
            self.reply_chatty(chat_id, reply)
        except Exception as exc:  # noqa: BLE001
            logging.error("pcstatus failed: %s", exc)
            send_message(self.token, chat_id, f"呜哇，电脑状态查不到了……（{exc}）")

    def cmd_autostart(self, chat_id: int, rest: str) -> None:
        action = rest.strip().lower()
        vbs_path = self._autostart_path()
        if action in ("on", "开", "1", "true"):
            if os.name != "nt":
                send_message(self.token, chat_id, "开机自启仅支持 Windows。")
                return
            vbs_path.parent.mkdir(parents=True, exist_ok=True)
            inner_command = f'"{sys.executable}" "{BASE_DIR / "bot.py"}"'
            escaped = inner_command.replace('"', '""')  # VBS 字符串内双引号转义
            vbs_content = f'CreateObject("WScript.Shell").Run "{escaped}", 0, False'
            vbs_path.write_text(vbs_content + "\n", encoding="ascii")
            send_message(self.token, chat_id, "已开启开机自启~以后电脑一开机，我就自动运行啦！")
        elif action in ("off", "关", "0", "false"):
            if vbs_path.exists():
                vbs_path.unlink()
                send_message(self.token, chat_id, "开机自启已关闭。")
            else:
                send_message(self.token, chat_id, "本来就没开自启哦。")
        else:
            send_message(self.token, chat_id, "用法：/autostart on 开启开机自启｜/autostart off 关闭")

    def _autostart_path(self) -> Path:
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "see_telegram_bot.vbs"

    def cmd_search(self, chat_id: int, rest: str) -> None:
        if not self.search_api_key:
            send_message(self.token, chat_id, "还没配搜索 API 哦~给我一个 Tavily API Key 我就能帮你查资料了")
            return
        if not rest:
            send_message(self.token, chat_id, "用法：/search 你想查的东西，比如「/search 香港今天的天气」")
            return
        try:
            results = tools.web_search(rest, self.search_api_key, self.search_api_url)
            if not results:
                send_message(self.token, chat_id, "没查到什么有用的……换个说法试试？")
                return
            reply = self._ask(
                [
                    {"role": "system", "content": self.system_prompt},
                    {
                        "role": "user",
                        "content": f"对方让你帮忙查：{rest}\n以下是搜到的资料：\n{results[:2000]}\n"
                        "用你的角色语气总结回答对方（短消息连发，用换行分隔），像科普一样，引用关键信息。",
                    },
                ]
            )
            self.reply_chatty(chat_id, reply)
        except Exception as exc:  # noqa: BLE001
            logging.error("search failed: %s", exc)
            send_message(self.token, chat_id, f"搜索失败了……{exc}")

    def handle_photo(self, chat_id: int, message: dict) -> None:
        self.reply_chatty(
            chat_id,
            "收到照片啦~！\n让我看看……嗯嗯！（认真看）\n"
            "快跟我说说，这是哪呀？发生什么了？",
        )
        self.memory.add(chat_id, "user", "[对方发来一张照片，还没说照片内容]")
        self.memory.add(chat_id, "assistant", "收到照片啦~快跟我说说，这是哪呀？")
        self.memory.save()

    def handle_sticker(self, chat_id: int, message: dict) -> None:
        """斗图：识别对方发来的表情包注释（emoji），回一张同情绪、没发过的表情包。"""
        sticker = message.get("sticker") or {}
        emoji = sticker.get("emoji", "")
        category = emoji_to_category(emoji)
        if category and random.random() < 0.5:
            # 斗图不一定要同情绪：一半概率故意回一张别的情绪（反制/随性）
            category = None
        maybe_send_sticker(
            self.token,
            chat_id,
            emoji,
            self.stickers,
            1.0,
            force_category=category,
            used=self._used_stickers,
        )
        self._save_event_state()
        self.memory.add(chat_id, "user", f"[对方发来一张表情包{emoji}]")
        self.memory.add(chat_id, "assistant", "[角色回了一张表情包]")
        self.memory.save()
        logging.info("sticker battle: user emoji=%s -> category=%s", emoji, category)

    def handle_location(self, chat_id: int, location: dict) -> None:
        lat = location.get("latitude")
        lon = location.get("longitude")
        if lat is None or lon is None:
            return
        self._save_location(lat, lon)
        self.reply_chatty(
            chat_id,
            "记住你的位置啦~！\n以后报天气就用这里，\n要照顾好自己哦！",
        )

    def _location_file(self) -> Path:
        return BASE_DIR / "location.json"

    def _save_location(self, lat: float, lon: float) -> None:
        try:
            self._location_file().write_text(json.dumps({"lat": lat, "lon": lon}), encoding="utf-8")
        except OSError as exc:
            logging.warning("could not save location: %s", exc)

    def _weather_city(self) -> str:
        try:
            location = json.loads(self._location_file().read_text(encoding="utf-8"))
            if location.get("lat") is not None and location.get("lon") is not None:
                return f"{location['lat']},{location['lon']}"
        except (OSError, json.JSONDecodeError):
            pass
        return self.city

    def send_reminder(self, reminder: dict) -> None:
        chat_id = reminder.get("chat_id")
        content = reminder.get("content", "提醒")
        repeat = reminder.get("repeat")
        if repeat == "water":
            instruction = "（提醒时间到了）以你的角色设定，用又关心又带点管束的语气催对方喝水，短消息连发（换行分隔），40~70 字。"
        else:
            instruction = (
                f"（提醒时间到了）以你的角色设定提醒对方：{content}。"
                "短消息连发（换行分隔），40~70 字，别啰嗦，自然一点。"
            )
        reply = self._ask(
            [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": instruction},
            ]
        )
        clean, category = extract_sticker_tag(reply)
        send_chatty(self.token, chat_id, clean)
        maybe_send_sticker(
            self.token,
            chat_id,
            clean,
            self.stickers,
            self.sticker_prob,
            force_category=category,
            used=self._used_stickers,
        )
        self._save_event_state()
        logging.info("reminder delivered to %s: %s", chat_id, content)

    def send_proactive(self, kind: str) -> None:
        prompt = self.PROACTIVE_PROMPTS.get(kind)
        if not prompt:
            logging.warning("unknown proactive kind: %s", kind)
            return
        if not self.proactive_chat_id:
            logging.warning("no proactive chat id, skip %s greeting", kind)
            return
        if kind == "noon" and random.random() < 0.5:
            prompt += "；如果合适，可以顺便请对方拍一张午饭照片发给你看看。"
        if kind == "morning" and self.briefing_enabled:
            weather = tools.get_weather(self._weather_city())
            if weather:
                prompt += f"\n顺便用自然的语气告诉对方：今天的天气：{weather}"
        prompt += "（如果记得和对方之前聊过的事，自然地提一句，增加代入感；想不起来就别硬提。）"
        messages = [
            {"role": "system", "content": self._system_for(self.proactive_chat_id)},
            {"role": "user", "content": prompt},
        ]
        reply = self._ask(messages)
        clean = self.reply_chatty(self.proactive_chat_id, reply)
        self.memory.add(self.proactive_chat_id, "assistant", clean)
        self.memory.save()
        logging.info("proactive %s sent to %s", kind, self.proactive_chat_id)

    def welcome(self) -> str:
        return (
            "你好！我是你的 AI 助手 🤖\n\n"
            f"当前后端：`{self.backend}` · 模型：`{self.model}`\n"
            "直接发消息就能聊，支持多轮上下文记忆。\n"
            "我能帮你：记提醒（说「周六买牛奶」就行）、查电脑状态、搜索资料、看照片。\n"
            "命令：/help 查看全部功能"
        )

    def help_text(self) -> str:
        return (
            "用法说明：\n"
            "· 直接发文字和我聊天（自动带上下文）\n"
            f"· 当前后端：{self.backend} · 模型：{self.model}\n"
            "· 说「记一下周六买牛奶 / 明天8点提醒我吃药」—— 自然语言记提醒\n"
            "· 发照片给我 —— 我会回应，你再告诉我是哪里、发生了什么\n"
            "· 发一个定位给我 —— 之后的天气就用你的位置\n"
            "· 提到「电脑」或 /pcstatus —— 查看电脑 CPU/内存/磁盘\n"
            "· /remind —— 记提醒 ｜ /reminders —— 查看提醒 ｜ /delremind 编号 —— 删除提醒\n"
            "· /water on/off —— 循环喝水提醒（每 2 小时）\n"
            "· /search 关键词 —— 联网搜索（需配置搜索 API）\n"
            "· /autostart on/off —— 开机自启\n"
            "· /clear —— 清空本会话记忆\n"
            "· /status —— 查看后端状态\n"
            "· /help —— 本帮助\n\n"
            "注意：模型跑在云端/本地取决于后端配置，长回复可能需要一些时间。"
        )

    def status(self, chat_id: int) -> None:
        try:
            if self.backend == "deepseek":
                names = deepseek_models(self.base_url, self.api_key)
                detail = f"DeepSeek API 正常 ✅\n可用模型：" + ("、".join(names) if names else "（获取失败）")
            else:
                models = ollama_tags(self.base_url)
                names = [m.get("name", "?") for m in models]
                mode = "CPU（自动降级）" if _FORCE_CPU else "GPU"
                detail = f"Ollama 运行正常 ✅（推理模式：{mode}）\n可用模型：" + ("、".join(names) if names else "（无）")
            reply = (
                f"后端：{self.backend} · 模型：{self.model}\n"
                + detail
            )
        except Exception as exc:  # noqa: BLE001
            reply = f"{self.backend} 连接失败 ❌\n{exc}"
        send_message(self.token, chat_id, reply)

    def poll(self) -> None:
        offset = 0
        logging.info("bot started: backend=%s model=%s endpoint=%s", self.backend, self.model, self.base_url)
        while True:
            try:
                updates = tg_call(
                    self.token,
                    "getUpdates",
                    {"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                    timeout=70,
                    retries=2,
                )
                for update in updates.get("result", []):
                    update_id = update.get("update_id")
                    if update_id is not None:
                        offset = update_id + 1
                    try:
                        self.handle(update)
                    except Exception as exc:  # noqa: BLE001 - keep polling alive
                        logging.error("handle failed: %s", exc)
                        chat = (update.get("message") or {}).get("chat") or {}
                        if chat.get("id"):
                            try:
                                send_message(self.token, chat["id"], f"处理消息时出错：{exc}")
                            except Exception:  # noqa: BLE001
                                pass
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - transient network errors
                logging.error("poll error: %s", exc)
                time.sleep(3)


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram bot backed by local Ollama")
    parser.add_argument("--token", help="Telegram bot token (or set TELEGRAM_BOT_TOKEN / config.env)")
    parser.add_argument("--model", help="Ollama model name")
    parser.add_argument("--backend", help="deepseek or ollama")
    parser.add_argument("--ollama-url", help="Ollama base URL")
    parser.add_argument("--persona", help="character card file (overrides SYSTEM_PROMPT)")
    parser.add_argument("--worldbook", help="worldbook/lore file (appended to SYSTEM_PROMPT)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="config file path")
    parser.add_argument("--history", default=str(DEFAULT_HISTORY), help="history JSON path")
    parser.add_argument("--max-turns", type=int, default=20, help="context turns per chat")
    parser.add_argument("--self-test", action="store_true", help="verify token against Telegram API and exit")
    parser.add_argument(
        "--test-proactive",
        choices=["morning", "noon", "evening", "night", "idle", "daily"],
        help="send one proactive greeting (or a daily event) immediately and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
        ],
    )

    config = load_env_file(Path(args.config))
    token = args.token or resolve_setting("TELEGRAM_BOT_TOKEN", config)
    backend = (args.backend or resolve_setting("BACKEND", config, "deepseek")).lower()
    system_prompt = resolve_setting(
        "SYSTEM_PROMPT",
        config,
        "你是一个友好、乐于助人的中文AI助手。回答要自然、简洁、准确；不确定时直接说明。",
    )
    persona_file = Path(args.persona) if args.persona else BASE_DIR / "persona.txt"
    if persona_file.is_file():
        try:
            persona_text = persona_file.read_text(encoding="utf-8").strip()
            if persona_text:
                system_prompt = persona_text
                logging.info("using persona: %s", persona_file)
        except OSError as exc:
            logging.warning("could not read persona file %s: %s", persona_file, exc)

    worldbook_file = Path(args.worldbook) if args.worldbook else BASE_DIR / "worldbook.txt"
    if worldbook_file.is_file():
        try:
            worldbook_text = worldbook_file.read_text(encoding="utf-8").strip()
            if worldbook_text:
                system_prompt = f"{system_prompt}\n\n{worldbook_text}"
                logging.info("using worldbook: %s", worldbook_file)
        except OSError as exc:
            logging.warning("could not read worldbook file %s: %s", worldbook_file, exc)

    if not token:
        logging.error(
            "缺少 Telegram Bot Token。请用 @BotFather 创建机器人并获取 token，"
            "然后填入 %s 或设置环境变量 TELEGRAM_BOT_TOKEN。",
            args.config,
        )
        return 1

    if backend == "deepseek":
        api_key = resolve_setting("DEEPSEEK_API_KEY", config)
        model = args.model or resolve_setting("DEEPSEEK_MODEL", config, "deepseek-v4-flash")
        base_url = args.ollama_url or resolve_setting("DEEPSEEK_BASE_URL", config, "https://api.deepseek.com/v1")
        if not api_key and not args.self_test:
            logging.error(
                "缺少 DeepSeek API Key。请到 https://platform.deepseek.com 获取，"
                "然后填入 %s 的 DEEPSEEK_API_KEY 或设置环境变量。",
                args.config,
            )
            return 1
    elif backend == "ollama":
        api_key = ""
        model = args.model or resolve_setting("OLLAMA_MODEL", config, "qwen3.6")
        base_url = args.ollama_url or resolve_setting("OLLAMA_URL", config, "http://127.0.0.1:11434")
    else:
        logging.error("未知 BACKEND: %s（可选 deepseek / ollama）", backend)
        return 1

    if args.self_test:
        try:
            me = tg_call(token, "getMe", timeout=30, retries=1)
            print(f"OK: bot @{me.get('result', {}).get('username', '?')} token 有效")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {exc}")
            return 1

    memory = Memory(Path(args.history), max_turns=args.max_turns)
    reminders = ReminderStore(Path(resolve_setting("REMINDERS_FILE", config, str(BASE_DIR / "reminders.json"))))
    city = resolve_setting("CITY", config, "香港")
    briefing_enabled = resolve_setting("BRIEFING_ENABLED", config, "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    search_api_key = resolve_setting("SEARCH_API_KEY", config)
    search_api_url = resolve_setting("SEARCH_API_URL", config, "https://api.tavily.com/search")
    stickers: dict = {}
    sticker_file = BASE_DIR / "stickers.json"
    if sticker_file.is_file():
        try:
            sticker_data = json.loads(sticker_file.read_text(encoding="utf-8"))
            if sticker_data.get("enabled", True):
                stickers = sticker_data.get("categories", {})
                logging.info(
                    "using stickers: %s (%d categories, %d stickers)",
                    sticker_file,
                    len(stickers),
                    sum(len(v) for v in stickers.values()),
                )
        except (OSError, ValueError) as exc:
            logging.warning("could not read stickers file %s: %s", sticker_file, exc)
    try:
        sticker_prob = float(resolve_setting("STICKER_SEND_PROB", config, "0.35"))
    except ValueError:
        sticker_prob = 0.35
    try:
        event_min_chars = int(resolve_setting("EVENT_MIN_CHARS", config, "600"))
        event_max_chars = int(resolve_setting("EVENT_MAX_CHARS", config, "800"))
        event_max_tokens = int(resolve_setting("EVENT_MAX_TOKENS", config, "2000"))
        char_event_days = int(resolve_setting("CHAR_EVENT_DAYS", config, "5"))
        short_event_days = int(resolve_setting("SHORT_EVENT_DAYS", config, "2"))
        ask_busy_after_minutes = int(resolve_setting("ASK_BUSY_AFTER_MINUTES", config, "10"))
        leave_after_minutes = int(resolve_setting("LEAVE_AFTER_MINUTES", config, "15"))
        idle_followup_prob = float(resolve_setting("IDLE_FOLLOWUP_PROB", config, "0.25"))
        memory_summary_every = int(resolve_setting("MEMORY_SUMMARY_EVERY", config, "6"))
        memory_inject_limit = int(resolve_setting("MEMORY_INJECT_LIMIT", config, "2500"))
        memory_condense_at = int(resolve_setting("MEMORY_CONDENSE_AT", config, "25"))
        care_banter_prob = float(resolve_setting("CARE_BANTER_PROB", config, "0.2"))
        event_active_window = int(resolve_setting("EVENT_ACTIVE_WINDOW", config, "90"))
    except ValueError:
        event_min_chars, event_max_chars, event_max_tokens, char_event_days = 600, 800, 2000, 5
        short_event_days = 2
        ask_busy_after_minutes = 10
        leave_after_minutes = 15
        idle_followup_prob = 0.25
        memory_summary_every, memory_inject_limit, memory_condense_at = 6, 2500, 25
        care_banter_prob, event_active_window = 0.2, 90
    reply_style = resolve_setting("REPLY_STYLE", config, "mixed").lower()
    if reply_style not in ("short", "medium", "normal", "mixed"):
        reply_style = "mixed"
    proactive_enabled = resolve_setting("PROACTIVE_ENABLED", config, "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    proactive_chat_id = resolve_setting("PROACTIVE_CHAT_ID", config, "").strip()
    if not proactive_chat_id:
        keys = sorted(memory.data, key=lambda k: len(memory.data[k]), reverse=True)
        if keys:
            proactive_chat_id = keys[0]
            logging.info("derived proactive chat id from history: %s", proactive_chat_id)
    bot = Bot(
        token,
        backend,
        model,
        base_url,
        api_key,
        system_prompt,
        memory,
        proactive_chat_id=int(proactive_chat_id) if proactive_chat_id else None,
        city=city,
        briefing_enabled=briefing_enabled,
        search_api_key=search_api_key,
        search_api_url=search_api_url,
        reminders=reminders,
        stickers=stickers,
        sticker_prob=sticker_prob,
        event_min_chars=event_min_chars,
        event_max_chars=event_max_chars,
        event_max_tokens=event_max_tokens,
        char_event_days=char_event_days,
        short_event_days=short_event_days,
        ask_busy_after_minutes=ask_busy_after_minutes,
        leave_after_minutes=leave_after_minutes,
        idle_followup_prob=idle_followup_prob,
        memory_summary_every=memory_summary_every,
        memory_inject_limit=memory_inject_limit,
        memory_condense_at=memory_condense_at,
        reply_style=reply_style,
        care_banter_prob=care_banter_prob,
        event_active_window=event_active_window,
    )

    if args.test_proactive:
        if not bot.proactive_chat_id:
            logging.error("no chat id available for proactive test")
            return 1
        if args.test_proactive == "daily":
            bot.send_daily_event()
        else:
            bot.send_proactive(args.test_proactive)
        return 0

    try:
        fixed_times = [
            slot.strip()
            for slot in resolve_setting("PROACTIVE_TIMES", config, "").split(",")
            if slot.strip()
        ]
        daily_count = int(resolve_setting("PROACTIVE_COUNT", config, "4"))
        window = resolve_setting("PROACTIVE_WINDOW", config, "08:00-23:30")
        idle_hours = float(resolve_setting("PROACTIVE_IDLE_HOURS", config, "6"))
        jitter = int(resolve_setting("PROACTIVE_JITTER_MINUTES", config, "15"))
        event_days = int(resolve_setting("PROACTIVE_EVENT_DAYS", config, "7"))
        meal_enabled = resolve_setting("MEAL_ENABLED", config, "true").lower() in ("1", "true", "yes", "on")
        meal_windows = {
            "morning": resolve_setting("MEAL_BREAKFAST", config, "07:00-08:00"),
            "noon": resolve_setting("MEAL_LUNCH", config, "12:00-12:30"),
            "evening": resolve_setting("MEAL_DINNER", config, "18:00-19:00"),
        }
    except ValueError as exc:
        logging.error("invalid proactive config: %s", exc)
        return 1

    scheduler = ProactiveScheduler(
        bot,
        fixed_times,
        daily_count,
        window,
        idle_hours,
        jitter,
        event_days,
        proactive_enabled,
        meal_enabled=meal_enabled,
        meal_windows=meal_windows,
        active_window=resolve_setting("PROACTIVE_ACTIVE_WINDOW", config, "07:00-22:00"),
    )
    scheduler.start()
    reminder_thread = ReminderThread(reminders, bot)
    reminder_thread.start()
    try:
        bot.poll()
    except KeyboardInterrupt:
        memory.save()
        logging.info("stopped by user")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
