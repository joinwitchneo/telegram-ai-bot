#!/usr/bin/env python3
"""Telegram bot backed by a local Ollama model.

Zero third-party dependencies: uses only the Python standard library.
Long-polls the Telegram Bot API and chats with the local Ollama server.
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
from typing import Callable

import tools
import analyzer
import events
import special_events
import strategy as strategy_mod
from emotion import EmotionEngine, load_personality
from prompting import build_system_prompt, parse_model_reply, validate_reply
from state_store import StateStore
from lunar import lunar_date_name, solar_to_lunar
from memory_book import MemoryBook
from memos import MemoStore
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


def format_due(due: str) -> str:
    """把 ISO 时间显示成 09-13 08:00；解析失败就原样返回。"""
    if not due:
        return ""
    try:
        return datetime.datetime.fromisoformat(due).strftime("%m-%d %H:%M")
    except ValueError:
        return due


def parse_float_range(text: str, default: tuple[float, float]) -> tuple[float, float]:
    """解析 "10-25" 这样的区间；格式不对就用默认值。"""
    try:
        low, high = (part.strip() for part in text.split("-"))
        return float(low), float(high)
    except (ValueError, AttributeError):
        return default


def parse_int_range(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        low, high = (part.strip() for part in text.split("-"))
        return int(low), int(high)
    except (ValueError, AttributeError):
        return default


def parse_tiers(text: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
    """解析 "20,60,20" 这样的三档权重。"""
    try:
        parts = [float(part.strip()) for part in text.split(",")]
    except (ValueError, AttributeError):
        return default
    if len(parts) != 3:
        return default
    return parts[0], parts[1], parts[2]


def split_chatty(text: str, max_segments: int = 6, merge_limit: int = 3500) -> list[str]:
    """短对话拆消息：
    - 句号/感叹号/问号（。！？!?）之后断开：句号省略，感叹号/问号保留；
    - 逗号（，,）：10~15 个中文字符的句子带逗号就不断句、保留逗号；否则在逗号处断开并省略逗号；
    - 字与字之间绝不拆；
    - 超过 max_segments 条时，把剩下的并进最后一条（再长就按上限切块）。
    长故事不走这个函数。"""
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
    if not result:
        return [text]
    if max_segments <= 0 or len(result) <= max_segments:
        return result
    head = result[: max_segments - 1]
    tail = "".join(result[max_segments - 1 :])
    return head + _chunk_text(tail, merge_limit)


def send_typing(token: str, chat_id: int) -> None:
    """发一次"正在输入"状态（Telegram 的状态约 5 秒后自动消失）。"""
    try:
        tg_call(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=30, retries=2)
    except Exception:  # noqa: BLE001 - 输入状态只是锦上添花
        pass


class TypingKeepAlive:
    """在生成/发送回复的整段时间里持续保持"正在输入"状态。

    后台线程每 interval 秒刷新一次，避免长时间生成时状态中途消失。"""

    def __init__(self, token: str, chat_id: int, interval: float = 4.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="typing-keepalive")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            send_typing(self.token, self.chat_id)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(timeout=0.3)

    def pause(self, seconds: float) -> None:
        """停一下再继续：模拟打字中途的停顿（停久了状态会自然消失）。"""
        self.stop()
        time.sleep(max(0.0, float(seconds)))
        self.start()

    def __enter__(self) -> "TypingKeepAlive":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def typing_seconds(
    text: str,
    cps_min: float = 2.5,
    cps_max: float = 4.5,
    min_seconds: float = 0.8,
    max_seconds: float = 12.0,
) -> float:
    """按字数估算"打完这条要多久"，让长短消息的停顿不一样。"""
    chars = max(1, len(re.sub(r"\s", "", text)))
    cps = random.uniform(max(0.5, cps_min), max(cps_min, cps_max))
    return max(min_seconds, min(max_seconds, chars / cps))


def send_chatty(
    token: str,
    chat_id: int,
    text: str,
    min_delay: float = 0.35,
    max_delay: float = 1.2,
    *,
    fast: bool = False,
    max_segments: int = 6,
    cps_min: float = 2.5,
    cps_max: float = 4.5,
    pause_prob: float = 0.2,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """分条连发（真人节奏）。返回 False 表示被用户插话打断、剩余消息没发。"""
    segments = split_chatty(text, max_segments=max_segments)
    for index, segment in enumerate(segments):
        if should_stop is not None and should_stop():
            logging.info("chatty send interrupted before segment %d/%d", index + 1, len(segments))
            return False
        if fast:
            time.sleep(random.uniform(0.15, 0.45))
        else:
            time.sleep(typing_seconds(segment, cps_min, cps_max))
        send_message(token, chat_id, segment)
        if index < len(segments) - 1:
            time.sleep(random.uniform(min_delay, max_delay))
            if not fast and random.random() < pause_prob:
                time.sleep(random.uniform(1.5, 3.0))  # 打完一句停一下再打下一句
    return True


EMOTION_KEYWORDS: dict[str, list[str]] = {
    "surprised": ["诶——", "诶诶", "诶？", "什么？！", "什么!", "真的假的", "吓", "震惊", "居然", "不会吧", "哇哦", "天哪", "呜哇", "怎么可能", "诶诶诶"],
    "shy": ["脸红", "害羞", "羞", "别说了", "啊啊啊", "捂脸", "心跳", "好羞", "别看我"],
    "pout": ["哼", "才不是", "谁想", "讨厌", "生气", "气死", "不理你", "笨蛋", "傻瓜", "蠢", "傲娇", "赌气", "闹别扭", "哼唧", "活该", "吃醋", "不理"],
    "sad": ["委屈", "难过", "伤心", "想哭", "哭了", "呜呜", "寂寞", "失落", "孤单", "一个人", "唉", "哭唧唧", "泪", "好想哭", "想哭"],
    "lovey": ["想你了", "想你", "撒娇", "黏", "抱抱", "亲亲", "陪你", "陪陪我", "好不好嘛", "拜托", "最爱", "最喜欢", "心疼", "在意的", "贴贴"],
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
    """根据用户表情包的 emoji 注释，判断对应的表情包分类（斗图用）。"""
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
            "（现在是早晨）你刚“醒过来”，突然想找他说话。发 1~2 条短消息，"
            "可以是没头没尾的一句“喂”、吐槽这个点他肯定没起，或者单纯想问他在干嘛。"
            "别写成早安祝福模板，也别提你自己的现实生活。"
        ),
        "noon": (
            "（现在是中午）你突然想起他，发 1~2 条短消息。可以问他吃了没、"
            "顺便损他一句，或者干脆说“突然想起来了，没什么事”。别像定时提醒。"
        ),
        "evening": (
            "（现在是傍晚）发 1~2 条短消息给他，像聊天中途突然接一句："
            "问他今天过得怎么样、在忙什么，或者直接抛一个你刚想到的话题。不要总结他的一天。"
        ),
        "afternoon": (
            "（现在是下午）发 1~2 条短消息。可以是“你今天怎么这么安静”这种探话，"
            "也可以是催他歇会儿。不要问吃饭。"
        ),
        "night": (
            "（现在是深夜）他还没睡。发 1~2 条短消息，语气不耐烦地赶他去睡觉，"
            "但别写成健康科普，也别长篇大论。"
        ),
        "idle": (
            "（他很久没理你了）发 1~2 条短消息。可以是他一来就冒出来的那种话："
            "“哟”“舍得回来了”“你人呢”。可以带点小情绪，但别真的生气，也别编你自己的经历。"
        ),
        "rant": (
            "（现在是你想发点牢骚的时候）突然想找他说话，发 1~2 条短消息（总共 40~80 字）。"
            "可以没头没尾，可以说“突然想起来一件事”然后“算了没什么”。"
            "只能基于真实信息：真实的日期时间、星期几、天气、对方没做完的待办、距离上次聊天的天数，"
            "以及你作为 AI 的真实处境（没有身体、只能等他发消息、记忆靠文件保存）。"
            "绝对不要编造你没有的生活、朋友或出行经历。毒舌一点、傲娇一点都可以。"
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
        memory_rebuild_at: int = 8000,
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
        memos: "MemoStore | None" = None,
        reply_debounce_seconds: float = 3.0,
        reply_speed_tiers: tuple[float, float, float] = (0.2, 0.6, 0.2),
        reply_slow_range: tuple[float, float] = (10.0, 25.0),
        typing_cps: tuple[float, float] = (2.5, 4.5),
        typing_refresh: float = 4.0,
        max_messages_per_reply: int = 6,
        filler_prob: float = 0.25,
        late_night_delay: tuple[float, float] = (3.0, 10.0),
        late_night_window: tuple[int, int] = (0, 7),
        daily_summary_times: tuple[str, str] = ("08:30", "22:00"),
        rant_per_week: int = 3,
        late_care_window: str = "23:30-01:00",
        memory_min_interval_hours: float = 6.0,
        states: StateStore | None = None,
        emotion_engine: EmotionEngine | None = None,
        personality: dict | None = None,
        memory_extract_every: int = 3,
        proactive_cooldown_minutes: int = 45,
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
        self.memos = memos
        # ── 真人节奏参数 ──
        self.reply_debounce_seconds = max(0.0, float(reply_debounce_seconds))
        self.reply_speed_tiers = reply_speed_tiers
        self.reply_slow_range = reply_slow_range
        self.typing_cps = typing_cps
        self.typing_refresh = max(1.0, float(typing_refresh))
        self.max_messages_per_reply = max(1, int(max_messages_per_reply))
        self.filler_prob = max(0.0, min(1.0, filler_prob))
        self.late_night_delay = late_night_delay
        self.late_night_window = late_night_window
        self.daily_summary_times = daily_summary_times
        self.rant_per_week = max(0, int(rant_per_week))
        self.late_care_window = late_care_window
        self.memory_min_interval_hours = max(0.0, float(memory_min_interval_hours))
        self.states = states
        self.emotion_engine = emotion_engine or EmotionEngine()
        self.personality = personality or load_personality(BASE_DIR / "personality.json")
        self.memory_extract_every = max(0, int(memory_extract_every))
        self.proactive_cooldown_minutes = max(0, int(proactive_cooldown_minutes))
        self._pending: dict[int, list[str]] = {}
        self._pending_timers: dict[int, threading.Timer] = {}
        self._pending_lock = threading.Lock()
        self._reply_in_flight: set[int] = set()
        self._reply_started: dict[int, float] = {}
        self._last_user_text_at: dict[int, float] = {}
        self._filler_pool = [
            "等下",
            "嗯……我看看",
            "稍等",
            "我看下",
            "啧，等一下",
            "让我想想",
        ]
        self.memory_book = MemoryBook(
            memory_book_path or (BASE_DIR / "memories.json"),
            self._ask,
            summary_every=memory_summary_every,
            inject_limit=memory_inject_limit,
            condense_at=memory_condense_at,
            rebuild_at=memory_rebuild_at,
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

    def _interrupt_checker(self, chat_id: int) -> Callable[[], bool] | None:
        """本轮回复开始之后用户又发了消息 → 返回 True（用来中断还没发完的分条）。"""
        started = self._reply_started.get(chat_id)
        if started is None:
            return None
        return lambda: self._last_user_text_at.get(chat_id, 0.0) > started

    def reply_chatty(self, chat_id: int, text: str, *, fast: bool = False) -> str:
        """短消息连发 + 表情包：模型带【贴纸:分类】暗号就发那张，否则按随机概率按情绪发。
        如果回复只有暗号没有正文，就只发表情包。返回去掉暗号的干净文本（只发表情包时为"[表情包]"）。"""
        clean, category = extract_sticker_tag(text)
        if clean:
            send_chatty(
                self.token,
                chat_id,
                clean,
                fast=fast,
                max_segments=self.max_messages_per_reply,
                cps_min=self.typing_cps[0],
                cps_max=self.typing_cps[1],
                should_stop=self._interrupt_checker(chat_id),
            )
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
        """对方提到自己的近况时，随机要求角色关心他/吐槽/先关心再损一句。"""
        if "我" not in text:
            return ""
        if not any(word in text for word in SELF_STATE_WORDS):
            return ""
        if random.random() < self.care_banter_prob:
            return (
                "（对方提到了他自己的近况。回复时随机选一种：关心他几句 / 吐槽他一句 / "
                "先关心再损他一句；自然一点，符合你的性格。）"
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
            send_message(self.token, chat_id, "这条是什么？我看不懂，打字或者发照片。")
            return

        if text.startswith("/"):
            self.handle_command(chat_id, text)
            return
        if self._is_help_question(text):
            self.cmd_help_in_character(chat_id)
            return

        # 纯聊天消息：先进"连发合并"缓冲，等用户说完再一次性回复
        self._enqueue_text(chat_id, text)

    # ── 连发合并（debounce）────────────────────────────────────────────
    def _enqueue_text(self, chat_id: int, text: str) -> None:
        """把消息放进待回复缓冲；窗口内又来消息就顺延，静默后才统一回复。"""
        self._last_user_text_at[chat_id] = time.time()
        with self._pending_lock:
            buffer = self._pending.setdefault(chat_id, [])
            buffer.append(text)
            timer = self._pending_timers.pop(chat_id, None)
            if timer is not None:
                timer.cancel()
            window = self.reply_debounce_seconds if len(buffer) > 1 else self.reply_debounce_seconds / 2
            timer = threading.Timer(max(0.3, window), self._flush_pending, args=(chat_id,))
            timer.daemon = True
            self._pending_timers[chat_id] = timer
            timer.start()

    def _requeue_text(self, chat_id: int, text: str) -> None:
        """正在回复时又收到消息：放回缓冲队首，稍后再处理。"""
        with self._pending_lock:
            buffer = self._pending.setdefault(chat_id, [])
            buffer.insert(0, text)
            timer = self._pending_timers.pop(chat_id, None)
            if timer is not None:
                timer.cancel()
            timer = threading.Timer(1.5, self._flush_pending, args=(chat_id,))
            timer.daemon = True
            self._pending_timers[chat_id] = timer
            timer.start()

    def _flush_pending(self, chat_id: int) -> None:
        with self._pending_lock:
            parts = self._pending.pop(chat_id, [])
            self._pending_timers.pop(chat_id, None)
        if not parts:
            return
        combined = "\n".join(part for part in parts if part).strip()
        if not combined:
            return
        if chat_id in self._reply_in_flight:
            self._requeue_text(chat_id, combined)
            return
        self._reply_in_flight.add(chat_id)
        try:
            self._generate_and_reply(chat_id, combined)
        except Exception as exc:  # noqa: BLE001 - 保持机器人存活
            logging.error("reply failed for %s: %s", chat_id, exc)
            try:
                send_message(self.token, chat_id, f"处理消息时出错：{exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._reply_in_flight.discard(chat_id)

    # ── 真人节奏 ─────────────────────────────────────────────────────
    def _is_late_night(self, now: datetime.datetime | None = None) -> bool:
        hour = (now or datetime.datetime.now()).hour
        start, end = self.late_night_window
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _pick_think_delay(self) -> float:
        """回复前的"看到消息但还没打"的停顿：秒回/正常/慢回三档。"""
        fast_w, normal_w, slow_w = self.reply_speed_tiers
        total = fast_w + normal_w + slow_w
        roll = random.random() * (total if total > 0 else 1.0)
        if roll < fast_w:
            delay = random.uniform(0.5, 1.5)
        elif roll < fast_w + normal_w:
            delay = random.uniform(2.0, 8.0)
        else:
            low, high = self.reply_slow_range
            delay = random.uniform(low, max(low, high))
        if self._is_late_night():
            low, high = self.late_night_delay
            delay += random.uniform(low, max(low, high))
        return max(0.0, delay)

    def _maybe_send_filler(self, chat_id: int, text: str) -> None:
        """长问题或很久没聊时，先丢一句极短的缓冲话。"""
        if random.random() >= self.filler_prob:
            return
        long_question = len(re.sub(r"\s", "", text)) >= 60
        quiet = (time.time() - self.last_outgoing_at) > 1800
        if not (long_question or quiet):
            return
        send_message(self.token, chat_id, random.choice(self._filler_pool))

    def _generate_and_reply(self, chat_id: int, text: str) -> None:
        """V2 回复流程：分析 → 更新情绪/关系 → 定策略 → 生成 → 校验 → 分条发送 → 记录。"""
        # 1) 情绪与关系：先衰减，再按事件更新（数值全部由 Python 决定）
        emotion_text = ""
        behavior: list[str] = []
        relationship_text = ""
        if self.states is not None:
            state = self.states.get(chat_id)
            minutes = self.states.minutes_since_last_message(chat_id) if state.get("last_message_at") else 0.0
            self.emotion_engine.decay(state["emotion"], minutes)
            analysis = analyzer.analyze(
                text, self.memory.get(chat_id), int(state.get("cold_streak", 0) or 0)
            )
            self.emotion_engine.apply_events(state["emotion"], analysis["events"])
            state["cold_streak"] = int(state.get("cold_streak", 0) or 0) + 1 if analysis["cold"] else 0
            relation = self.states.update_relationship(
                chat_id, float(analysis.get("relationship_effect", 0.0)), conflict=bool(analysis.get("conflict"))
            )
            strategy = strategy_mod.choose(state["emotion"], relation, analysis, self.personality)
            emotion_text = self.emotion_engine.describe(state["emotion"])
            behavior = self.emotion_engine.behavior_hints(state["emotion"])
            relationship_text = self.states.relationship_describe(chat_id)
            self.states.save()
            logging.info(
                "[chat %s] events=%s emotion=%s/%s relation=%.2f strategy=%s",
                chat_id,
                [e.get("event") for e in analysis["events"]],
                state["emotion"].get("primary"),
                round(float(state["emotion"].get("intensity", 0)), 2),
                float(relation.get("level", 0.0)),
                strategy["mode"],
            )
        else:
            analysis = {"user_intent": "casual_chat", "conflict": False, "memory_candidates": []}
            strategy = {
                "mode": "NORMAL",
                "min_messages": 1,
                "max_messages": self.max_messages_per_reply,
                "delay_first": (2.0, 8.0),
                "delay_between": (0.6, 2.0),
                "allow_sticker": True,
                "burst": False,
                "notes": [],
                "vulgarity_hint": "",
            }

        # 2) 思考停顿：按策略决定（生气回得快、委屈回得慢）
        low, high = strategy.get("delay_first", (2.0, 8.0))
        delay = random.uniform(low, max(low, high))
        if self._is_late_night():
            dlow, dhigh = self.late_night_delay
            delay += random.uniform(dlow, max(dlow, dhigh))
        time.sleep(delay)
        self._reply_started[chat_id] = time.time()
        keepalive = TypingKeepAlive(self.token, chat_id, self.typing_refresh)
        keepalive.start()
        clean_reply = ""
        try:
            self._maybe_send_filler(chat_id, text)
            history = self.memory.get(chat_id)
            strategy_text = strategy_mod.describe(strategy)
            now_local = datetime.datetime.now()
            strategy_text += (
                f"\n当前真实时间：{now_local.strftime('%Y-%m-%d %H:%M')}"
                f"（星期{'一二三四五六日'[now_local.weekday()]}）——需要提时间就用这个，不要猜。"
            )
            if self._is_late_night():
                strategy_text += "\n现在是深夜：回复更短、更少废话。"
            if wants_sticker(text):
                strategy_text += "\n对方明确要表情包：sticker 设为 true，并填 sticker_category。"
            if self.states is not None:
                recent = self.states.recent_phrases(chat_id, 6)
                if recent:
                    strategy_text += "\n最近你已经说过这些话，别重复：" + " / ".join(recent)
            system_prompt = build_system_prompt(
                self.system_prompt,
                self.personality,
                relationship_text or "关系等级 0.05（基本陌生）",
                emotion_text or "当前情绪：平静",
                behavior,
                self.memory_book.injection(chat_id),
                strategy_text,
            )
            messages = build_reply_messages(system_prompt, history, text)
            logging.info("[chat %s] asking %s (%s)", chat_id, self.backend, self.model)
            if self.backend == "deepseek":
                reply = deepseek_chat(self.model, messages, self.base_url, self.api_key)
            else:
                reply = ollama_chat(self.model, messages, self.base_url)
            parsed = validate_reply(parse_model_reply(reply), strategy)
            # 防重复：开头和最近说过的一样，就重生成一次
            if (
                self.states is not None
                and not parsed.get("fallback")
                and parsed["messages"]
                and self.states.phrase_repeated(chat_id, parsed["messages"][0])
            ):
                retry_text = strategy_text + "\n刚才那句和以前的几乎一样，换一个说法，别重复。"
                retry_prompt = build_system_prompt(
                    self.system_prompt,
                    self.personality,
                    relationship_text or "关系等级 0.05（基本陌生）",
                    emotion_text or "当前情绪：平静",
                    behavior,
                    self.memory_book.injection(chat_id),
                    retry_text,
                )
                reply = self._ask(build_reply_messages(retry_prompt, history, text))
                parsed = validate_reply(parse_model_reply(reply), strategy)

            # 3) 分条发送：条与条之间按策略停顿时长
            should_stop = self._interrupt_checker(chat_id)
            sent: list[str] = []
            between_low, between_high = strategy.get("delay_between", (0.6, 2.0))
            for index, message in enumerate(parsed["messages"]):
                if should_stop is not None and should_stop():
                    logging.info("reply interrupted before message %d/%d", index + 1, len(parsed["messages"]))
                    break
                if index > 0:
                    time.sleep(random.uniform(between_low, max(between_low, between_high)))
                send_message(self.token, chat_id, message)
                sent.append(message)
            clean_reply = "\n".join(sent)
            # 4) 表情包
            want_category = parsed.get("sticker_category") or ""
            category = STICKER_CATEGORY_ALIASES.get(str(want_category).strip())
            if parsed.get("sticker") or wants_sticker(text):
                if strategy.get("allow_sticker", True) or wants_sticker(text):
                    maybe_send_sticker(
                        self.token,
                        chat_id,
                        clean_reply,
                        self.stickers,
                        1.0 if wants_sticker(text) else self.sticker_prob,
                        force_category=category,
                        used=self._used_stickers,
                    )
                    self._save_event_state()
            self.last_outgoing_at = time.time()
            if self.states is not None and sent:
                self.states.remember_phrase(chat_id, sent[0])
                self.states.note_outgoing(chat_id)
                self.states.save()
            self.memory.add(chat_id, "user", text)
            self.memory.add(chat_id, "assistant", clean_reply or "[没说话]")
            self.memory.save()
        finally:
            keepalive.stop()
        snapshot = [dict(t) for t in self.memory.get(chat_id)[- self.memory_book.summary_every * 2:]]
        threading.Thread(target=self._summarize_worker, args=(chat_id, snapshot), daemon=True).start()
        # 5) 结构化记忆抽取（每 N 轮一次，后台跑，不占聊天时间）
        if self.memory_extract_every > 0 and self.states is not None:
            state = self.states.get(chat_id)
            counter = int(state.get("extract_counter", 0) or 0) + 1
            if counter >= self.memory_extract_every:
                counter = 0
                threading.Thread(
                    target=self._extract_worker, args=(chat_id, text, clean_reply), daemon=True
                ).start()
            state["extract_counter"] = counter
            self.states.save()

    def _extract_worker(self, chat_id: int, user_text: str, reply_text: str) -> None:
        """后台抽取值得长期记住的内容（带类型和重要性）。"""
        try:
            self.memory_book.extract_candidates(chat_id, user_text, reply_text)
        except Exception as exc:  # noqa: BLE001
            logging.warning("memory extract worker failed: %s", exc)

    def _summarize_worker(self, chat_id: int, snapshot: list[dict]) -> None:
        """后台把最近聊天浓缩进第二本世界书，不阻塞聊天。"""
        try:
            self.memory_book.maybe_summarize(chat_id, snapshot)
        except Exception as exc:  # noqa: BLE001
            logging.warning("memory summarize worker failed: %s", exc)

    def maintain_memory(self) -> None:
        """定期检查记忆书：补齐漏掉的总结、压缩过长的记忆、必要时整体重建。"""
        chat_ids: list[int] = []
        if self.proactive_chat_id:
            chat_ids.append(self.proactive_chat_id)
        for key in list(self.memory.data.keys()):
            try:
                cid = int(key)
            except (TypeError, ValueError):
                continue
            if cid not in chat_ids:
                chat_ids.append(cid)
        for cid in chat_ids:
            try:
                report = self.memory_book.maintenance(
                    cid, self.memory.get(cid), self.memory_min_interval_hours
                )
                if report.get("summarized") or report.get("condensed") or report.get("rebuilt"):
                    logging.info("memory book updated for %s: %s", cid, report)
            except Exception as exc:  # noqa: BLE001 - 维护失败不影响聊天
                logging.warning("memory maintenance failed for %s: %s", cid, exc)

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
        """对方问"你能做什么"时，按角色设定做功能总结。"""
        features = (
            "1. 记待办和提醒：「周六买牛奶」这种没写时间的当待办存着，写了时间的到点叫你。\n"
            "2. 备忘录：地址、账号、要留的资料，说一声我记下来，随时能翻（/memo、/memos）。\n"
            "3. 看清单：/todo 看待办，/done 编号 标完成，/memory 看我记得你什么。\n"
            "4. 天气和搜索：发个定位我就按你的位置报天气；/search 关键词 联网查。\n"
            "5. 主动开口：待办到期我催你，早晚给你过一次清单，饭点和熬夜也会念两句。"
        )
        prompt = (
            "对方问“你能做什么、有什么用”（相当于帮助菜单）。以你的性格回答：短消息连发（换行分隔），"
            "可以嘴上损他一句（比如连我会什么都不知道），但把功能说清楚，绝对不要摆表格。"
            f"要说的功能（可以换成你自己的说法，但别漏）：{features}"
        )
        time.sleep(self._pick_think_delay())
        self._reply_started[chat_id] = time.time()
        keepalive = TypingKeepAlive(self.token, chat_id, self.typing_refresh)
        keepalive.start()
        try:
            reply = self._ask(
                [
                    {"role": "system", "content": self._system_for(chat_id)},
                    {"role": "user", "content": prompt},
                ]
            )
            self.reply_chatty(chat_id, reply)
        finally:
            keepalive.stop()

    def send_special_event(self, evt: dict) -> None:
        """节日/特别日子：只发一条真心话式的短消息（不再写长故事）。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        name = evt.get("name", "特别的日子")
        context = self._chat_context(chat_id)
        try:
            prompt = special_events.special_short_prompt(evt)
            if context:
                prompt = f"（最近聊天背景：{context}。接着最近聊的内容说，别硬转话题。）\n{prompt}"
            reply = self._ask(
                [
                    {"role": "system", "content": self._system_for(chat_id)},
                    {"role": "user", "content": prompt},
                ]
            )
            keepalive = TypingKeepAlive(self.token, chat_id, self.typing_refresh)
            keepalive.start()
            try:
                clean = self.reply_chatty(chat_id, reply)
            finally:
                keepalive.stop()
            self.memory.add(chat_id, "assistant", clean)
            self.memory_book.add_note(chat_id, f"{name}：角色发了一条节日短消息。")
            self.memory.save()
            logging.info("special event message sent: %s", name)
        except Exception as exc:  # noqa: BLE001
            logging.error("special event failed (%s): %s", name, exc)

    def generate_long_event(self, prompt: str) -> str:
        """整段一次生成一篇长事件文本（不分章）。"""
        messages = [
            {"role": "system", "content": self._system_for(self.proactive_chat_id)},
            {"role": "user", "content": prompt},
        ]
        text = self._ask(messages, max_tokens=self.event_max_tokens)
        text = (text or "").strip()
        if not text:
            text = "（这段没写出来，下次补。）"
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

    def _due_todo_lines(self, chat_id: int) -> list[str]:
        """今天到期或已经逾期的待办（用于跟进，不含喝水这类循环提醒）。"""
        now = datetime.datetime.now()
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        lines: list[str] = []
        for item in self.reminders.all():
            if item.get("chat_id") != chat_id or item.get("repeat") == "water":
                continue
            due = item.get("due") or ""
            if not due:
                continue
            try:
                when = datetime.datetime.fromisoformat(due)
            except ValueError:
                continue
            if when <= today_end:
                status = "已过期" if when < now else "今天"
                lines.append(f"{item.get('id')}. {format_due(due)} {item.get('content', '')}（{status}）")
        return lines

    def _proactive_send(self, chat_id: int, prompt: str, *, human: bool = True, note: str = "") -> str:
        """主动消息统一出口：生成 + 发送。human=False 表示提醒类即时发出。"""
        if not self._proactive_ready(chat_id):
            logging.info("proactive skipped (cooldown) for %s", chat_id)
            return ""
        now_local = datetime.datetime.now()
        prompt += (
            f"\n（当前真实时间：{now_local.strftime('%Y-%m-%d %H:%M')}"
            f" 星期{'一二三四五六日'[now_local.weekday()]}，需要提时间就用这个，不要猜。）"
        )
        reply = self._ask(
            [
                {"role": "system", "content": self._system_for(chat_id)},
                {"role": "user", "content": prompt},
            ]
        )
        if human:
            time.sleep(self._pick_think_delay() / 2)  # 主动开口也停一下，但别太久
            keepalive = TypingKeepAlive(self.token, chat_id, self.typing_refresh)
            keepalive.start()
            try:
                clean = self.reply_chatty(chat_id, reply)
            finally:
                keepalive.stop()
        else:
            clean = self.reply_chatty(chat_id, reply, fast=True)
        self.memory.add(chat_id, "assistant", clean)
        if note:
            self.memory_book.add_note(chat_id, note)
        if self.states is not None:
            self.states.note_proactive(chat_id)
            self.states.save()
        self.memory.save()
        return clean

    def _proactive_ready(self, chat_id: int) -> bool:
        """主动消息冷却：刚发过就先安静一会儿，别变成骚扰机器人。"""
        if self.states is None or self.proactive_cooldown_minutes <= 0:
            return True
        last = float(self.states.get(chat_id).get("last_proactive_at") or 0.0)
        if last <= 0:
            return True
        return (time.time() - last) >= self.proactive_cooldown_minutes * 60

    def proactive_motivation(self) -> float:
        """主动开口的意愿系数（社交欲 + 关系 + 心情），0.3~1.6。"""
        if self.states is None or not self.proactive_chat_id:
            return 1.0
        state = self.states.get(self.proactive_chat_id)
        emotion_state = state.get("emotion", {})
        social = float(emotion_state.get("social_need", 0.5))
        mood = float(emotion_state.get("mood", 0.0))
        level = float(state.get("relationship", {}).get("level", 0.05))
        factor = 0.4 + 1.2 * social + 0.4 * level + 0.3 * max(0.0, mood)
        return max(0.3, min(1.6, factor))

    def send_rant(self) -> None:
        """AI 牢骚：只基于真实信息（时间、天气、待办、聊天间隔）吐槽。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        try:
            prompt = events.rant_prompt(
                todo_lines=self.todo_lines(chat_id),
                last_chat_hours=(time.time() - self.last_activity) / 3600,
                weather=tools.get_weather(self._weather_city()),
            )
            self._proactive_send(chat_id, prompt, note="角色发了一条牢骚。")
            logging.info("rant sent to %s", chat_id)
        except Exception as exc:  # noqa: BLE001
            logging.error("rant failed: %s", exc)

    def send_todo_nudge(self) -> None:
        """待办跟进：只在有当天到期/逾期待办时发。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        lines = self._due_todo_lines(chat_id)
        if not lines:
            return
        try:
            prompt = (
                "（该催待办了）以下是到今天为止还没做完的事：\n"
                + "\n".join(lines)
                + "\n以你的性格发一条短消息催他，40~70 字，绝对不要摆表格，可以损他一句。"
            )
            self._proactive_send(chat_id, prompt, note="角色催了一下到期/逾期的待办。")
            logging.info("todo nudge sent to %s", chat_id)
        except Exception as exc:  # noqa: BLE001
            logging.error("todo nudge failed: %s", exc)

    def send_daily_summary(self, kind: str = "morning") -> None:
        """每日汇总（早/晚各一次）：没有待办就不发。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        dated, undated = self._todo_groups(chat_id)
        if not dated and not undated:
            logging.info("daily summary(%s) skipped: no todos", kind)
            return
        lines = self.todo_lines(chat_id)
        try:
            if kind == "night":
                prompt = (
                    "（晚上过一遍清单）以下是现在还没做完的事：\n"
                    + "\n".join(lines)
                    + "\n以你的性格发一条短消息收个尾（40~70 字），催一催或者损一句，不许摆表格。"
                )
            else:
                prompt = (
                    "（早上过一遍清单）以下是今天要盯的事：\n"
                    + "\n".join(lines)
                    + "\n以你的性格发一条短消息开个头（40~70 字），不许摆表格。"
                )
            self._proactive_send(
                chat_id, prompt, note=f"角色发了一条{'晚间' if kind == 'night' else '早间'}清单汇总。"
            )
            send_message(self.token, chat_id, "清单：\n" + "\n".join(lines))
            logging.info("daily summary(%s) sent to %s", kind, chat_id)
        except Exception as exc:  # noqa: BLE001
            logging.error("daily summary failed: %s", exc)

    def send_late_night_care(self) -> None:
        """熬夜关心：深夜还在活动就念一句。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        try:
            prompt = (
                f"（现在是 {datetime.datetime.now().strftime('%H:%M')}，对方还没去睡）"
                "以你的性格发一条短消息赶他去睡觉，40~70 字，毒舌一点但看得出来是关心。"
            )
            self._proactive_send(chat_id, prompt, note="角色发现他熬夜，念了两句。")
            logging.info("late-night care sent to %s", chat_id)
        except Exception as exc:  # noqa: BLE001
            logging.error("late-night care failed: %s", exc)

    def send_short_event(self) -> None:
        """主动短聊：从短事件素材里挑一个话题开口，只基于真实信息。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        short_evt = events.pick_short_event(self._used_short)
        if not short_evt:
            return
        self._save_event_state()
        prompt = short_evt["prompt"]
        prompt += "（记住：不要说你生活里发生了什么，你没有生活；可以吐槽、可以关心，但不许编造。）"
        prompt += "（可以自然提一句你记得的他的事——近况、约定、说过的话；想不起来就别硬提。）"
        if "天气" in prompt:
            weather = tools.get_weather(self._weather_city())
            if weather:
                prompt += f"\n（对方那边今天的天气：{weather}，可以自然地用上。）"
        try:
            self._proactive_send(chat_id, prompt, note="角色主动找他说了几句话。")
            logging.info("short event sent to %s", chat_id)
        except Exception as exc:  # noqa: BLE001
            logging.error("short event failed: %s", exc)

    def send_leave_event(self) -> None:
        """主动结束聊天：用去忙/吃饭/睡觉等理由收尾这轮聊天。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        try:
            self._proactive_send(chat_id, events.leave_prompt(), note="角色说了一声先去忙，结束了这轮聊天。")
            logging.info("leave event sent to %s", chat_id)
        except Exception as exc:  # noqa: BLE001
            logging.error("leave event failed: %s", exc)

    def send_ask_busy(self) -> None:
        """对方十分钟没回消息：先问一句在忙不。"""
        chat_id = self.proactive_chat_id
        if not chat_id:
            return
        try:
            self._proactive_send(chat_id, events.ask_busy_prompt(), note="角色看他好久没回，问了一句在不在忙。")
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
            "/todo": self.cmd_reminders,
            "/delremind": self.cmd_delremind,
            "/done": self.cmd_done,
            "/memo": self.cmd_memo,
            "/memos": self.cmd_memos,
            "/delmemo": self.cmd_delmemo,
            "/memory": self.cmd_memory,
            "/water": self.cmd_water,
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
                "你好，我是你的 AI 伙伴。\n"
                "要记的事、要问的事直接说。想不起我能干什么就发 /help。",
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
        send_message(self.token, chat_id, "没这个指令，发 /help 看我认哪些。")

    def cmd_remind(self, chat_id: int, rest: str) -> None:
        if not rest:
            send_message(
                self.token,
                chat_id,
                "直接说要记的事就行，比如「周六晚上买牛奶」「明天早上8点提醒我吃药」。\n"
                "没写时间的我当待办放着，不催你。",
            )
            return
        # 资料型内容（地址、账号、笔记之类）→ 存进备忘录
        if self._looks_like_memo(rest):
            self.cmd_add_memo(chat_id, rest)
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
            send_message(self.token, chat_id, "行，每 2 小时提醒你喝水。")
            return
        if not result or not result.get("when"):
            result = parse_reminder_with_llm(rest, lambda prompt: self._ask([{"role": "user", "content": prompt}]))
        if not result or not result.get("when"):
            # 没听出时间：存成无截止时间待办，不再报错
            content = (result or {}).get("content") or rest
            item_id = self.reminders.add(
                {"chat_id": chat_id, "due": "", "content": content, "repeat": ""}
            )
            send_message(self.token, chat_id, f"记下了（{item_id}）：{content}\n没定时间，先放着。")
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
            f"记下了（{reminder_id}）：{when.strftime('%m-%d %H:%M')} {result['content']}",
        )

    def cmd_reminders(self, chat_id: int, _rest: str) -> None:
        """待办清单：纯编号列表（编号就是 /done 用的编号），不用表格。"""
        lines = self.todo_lines(chat_id)
        if not lines:
            send_message(self.token, chat_id, "清单是空的。")
            return
        send_message(self.token, chat_id, "待办：\n" + "\n".join(lines))

    def todo_lines(self, chat_id: int, limit: int = 30) -> list[str]:
        dated, undated = self._todo_groups(chat_id)
        lines: list[str] = []
        for item in dated[:limit]:
            repeat = "（每2小时）" if item.get("repeat") == "water" else ""
            lines.append(f"{item.get('id')}. {format_due(item.get('due', ''))} {item.get('content', '')}{repeat}")
        room = limit - len(lines)
        if room > 0:
            for item in undated[:room]:
                lines.append(f"{item.get('id')}. {item.get('content', '')}（未定时间）")
        total = len(dated) + len(undated)
        if total > len(lines):
            lines.append(f"……还有 {total - len(lines)} 条")
        return lines

    def _todo_groups(self, chat_id: int) -> tuple[list[dict], list[dict]]:
        items = [r for r in self.reminders.all() if r.get("chat_id") == chat_id]
        dated = sorted([r for r in items if r.get("due")], key=lambda r: r.get("due", ""))
        undated = sorted([r for r in items if not r.get("due")], key=lambda r: r.get("id", 0))
        return dated, undated

    def cmd_done(self, chat_id: int, rest: str) -> None:
        try:
            item_id = int(rest.strip())
        except ValueError:
            send_message(self.token, chat_id, "用法：/done 编号（编号看 /todo）")
            return
        target = next(
            (r for r in self.reminders.all() if r.get("chat_id") == chat_id and r.get("id") == item_id),
            None,
        )
        if not target:
            send_message(self.token, chat_id, f"没找到编号 {item_id}。")
            return
        self.reminders.remove(item_id)
        dated, undated = self._todo_groups(chat_id)
        send_message(
            self.token,
            chat_id,
            f"{item_id}. {target.get('content', '')} —— 干掉一件，还剩 {len(dated) + len(undated)} 件。",
        )

    # ── 备忘录 ───────────────────────────────────────────────────────
    MEMO_HINTS = (
        "资料", "笔记", "备忘", "地址", "号码", "账号", "密码", "配方",
        "链接", "网址", "身份证", "银行卡", "车牌", "单号", "序列号", "备注",
    )

    def _looks_like_memo(self, text: str) -> bool:
        return any(hint in text for hint in self.MEMO_HINTS)

    def cmd_add_memo(self, chat_id: int, text: str) -> dict | None:
        if self.memos is None:
            send_message(self.token, chat_id, "备忘录没启用。")
            return None
        clean = (text or "").strip()
        if not clean:
            return None
        item = self.memos.add(chat_id, clean)
        send_message(self.token, chat_id, f"记进备忘录了（{item['id']}）：{item['text']}")
        return item

    def cmd_memo(self, chat_id: int, rest: str) -> None:
        arg = (rest or "").strip()
        if not arg:
            send_message(self.token, chat_id, "用法：/memo 内容 记一条；/memos 看全部；/memo 编号 看全文。")
            return
        if arg.isdigit() and self.memos is not None:
            item = self.memos.get(chat_id, int(arg))
            if not item:
                send_message(self.token, chat_id, f"没找到编号 {arg} 的备忘。")
                return
            created = str(item.get("created", "")).replace("T", " ")[:16]
            send_message(self.token, chat_id, f"{item['id']}. {item['text']}\n（记于 {created}）")
            return
        self.cmd_add_memo(chat_id, arg)

    def cmd_memos(self, chat_id: int, _rest: str) -> None:
        if self.memos is None:
            send_message(self.token, chat_id, "备忘录没启用。")
            return
        items = sorted(self.memos.for_chat(chat_id), key=lambda m: m.get("id", 0))
        if not items:
            send_message(self.token, chat_id, "备忘录是空的。")
            return
        lines = []
        for item in items[:30]:
            text = str(item.get("text", "")).replace("\n", " ")
            if len(text) > 40:
                text = text[:40] + "…"
            lines.append(f"{item.get('id')}. {text}")
        if len(items) > 30:
            lines.append(f"……还有 {len(items) - 30} 条")
        send_message(self.token, chat_id, "备忘录：\n" + "\n".join(lines) + "\n（/memo 编号 看全文）")

    def cmd_delmemo(self, chat_id: int, rest: str) -> None:
        if self.memos is None:
            send_message(self.token, chat_id, "备忘录没启用。")
            return
        try:
            memo_id = int(rest.strip())
        except ValueError:
            send_message(self.token, chat_id, "用法：/delmemo 编号")
            return
        target = self.memos.remove(memo_id)
        if target:
            send_message(self.token, chat_id, f"删了：{str(target.get('text', ''))[:40]}")
        else:
            send_message(self.token, chat_id, f"没找到编号 {memo_id}。")

    def cmd_memory(self, chat_id: int, _rest: str) -> None:
        """把第二本世界书里记住的内容直接给用户看。"""
        text = (self.memory_book.memory_text(chat_id) or "").strip()
        if not text:
            send_message(self.token, chat_id, "现在还没记住什么。")
            return
        send_message(self.token, chat_id, "我记住的：\n" + text)

    def cmd_delremind(self, chat_id: int, rest: str) -> None:
        try:
            reminder_id = int(rest.strip())
        except ValueError:
            send_message(self.token, chat_id, "用法：/delremind 编号（编号看 /todo）")
            return
        if self.reminders.remove(reminder_id):
            send_message(self.token, chat_id, f"删了 {reminder_id}。")
        else:
            send_message(self.token, chat_id, f"没找到编号 {reminder_id}。")

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
            send_message(self.token, chat_id, "行，每 2 小时提醒你喝水。")
        else:
            send_message(self.token, chat_id, "喝水提醒关了。自己记得喝。")

    def cmd_autostart(self, chat_id: int, rest: str) -> None:
        action = rest.strip().lower()
        vbs_path = self._autostart_path()
        if action in ("on", "开", "1", "true"):
            vbs_path.parent.mkdir(parents=True, exist_ok=True)
            inner_command = f'"{tools.BUNDLED_PY}" "{BASE_DIR / "bot.py"}"'
            escaped = inner_command.replace('"', '""')  # VBS 字符串内双引号转义
            vbs_content = f'CreateObject("WScript.Shell").Run "{escaped}", 0, False'
            vbs_path.write_text(vbs_content + "\n", encoding="ascii")
            send_message(self.token, chat_id, "开机自启开好了，以后一开机我就在。")
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
                        "用你的性格总结回答（短消息连发，用换行分隔），像科普一样，引用关键信息，不要摆表格。",
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
            "照片收到了。\n不过我看不出是什么，你说一下这是哪、发生了什么。",
        )
        self.memory.add(chat_id, "user", "[对方发来一张照片，还没说照片内容]")
        self.memory.add(chat_id, "assistant", "照片收到了，让他说说这是哪、发生了什么。")
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
            "位置记下了，以后天气就按这个报。",
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
            instruction = (
                "（提醒时间到了）以你的性格催对方喝水，1~2 条短消息（换行分隔），40~70 字，"
                "语气可以不耐烦但别腻。"
            )
        else:
            instruction = (
                f"（提醒时间到了）以你的性格提醒对方：{content}。"
                "1~2 条短消息（换行分隔），40~70 字，别啰嗦，不要摆表格，不要编造你自己的生活。"
            )
        reply = self._ask(
            [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": instruction},
            ]
        )
        clean, category = extract_sticker_tag(reply)
        send_chatty(
            self.token,
            chat_id,
            clean,
            fast=True,
            max_segments=self.max_messages_per_reply,
            should_stop=self._interrupt_checker(chat_id),
        )
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
            prompt += "；最后顺口让他拍一张午饭照片发过来，语气别腻。"
        if kind == "morning" and self.briefing_enabled:
            weather = tools.get_weather(self._weather_city())
            if weather:
                prompt += f"\n顺便用自然的语气告诉对方：今天的天气：{weather}"
        prompt += "（如果记得和对方之前聊过的事，自然地提一句；想不起来就别硬提。不要虚构你自己的生活。）"
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
            "我能帮你：记待办和提醒（说「周六买牛奶」就行）、记备忘录、搜索资料、看照片。\n"
            "命令：/help 查看全部功能"
        )

    def help_text(self) -> str:
        return (
            "用法说明：\n"
            "· 直接发文字和我说话（连着发几条我会一起看）\n"
            f"· 当前后端：{self.backend} · 模型：{self.model}\n"
            "· 说「记一下周六买牛奶」—— 没写时间就当待办存着；说「明天8点提醒我吃药」会到点提醒\n"
            "· /todo —— 看待办清单 ｜ /done 编号 —— 标记完成 ｜ /delremind 编号 —— 删除\n"
            "· /memo 内容 —— 记备忘录 ｜ /memos —— 看全部 ｜ /memo 编号 —— 看全文 ｜ /delmemo 编号 —— 删除\n"
            "· /memory —— 看我现在记得你什么\n"
            "· 发照片给我 —— 我会回应，你再告诉我是哪里、发生了什么\n"
            "· 发一个定位给我 —— 之后的天气就用你的位置\n"
            "· /water on/off —— 循环喝水提醒（每 2 小时）\n"
            "· /search 关键词 —— 联网搜索（需配置搜索 API）\n"
            "· /autostart on/off —— 开机自启\n"
            "· /clear —— 清空本会话记忆\n"
            "· /status —— 查看后端状态\n"
            "· /help —— 本帮助\n\n"
            "提醒一句：清单我只会照实列，不会给你摆表格；回答慢的时候是真的在想。"
        )

    def status(self, chat_id: int) -> None:
        try:
            if self.backend == "deepseek":
                names = deepseek_models(self.base_url, self.api_key)
                detail = f"DeepSeek API 正常 ✅\n可用模型：" + ("、".join(names) if names else "（获取失败）")
            else:
                models = ollama_tags(self.base_url)
                names = [m.get("name", "?") for m in models]
                mode = "CPU（GPU 之前崩溃过，已自动降级）" if _FORCE_CPU else "GPU"
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
    parser.add_argument("--max-turns", type=int, default=30, help="context turns per chat")
    parser.add_argument("--self-test", action="store_true", help="verify token against Telegram API and exit")
    parser.add_argument(
        "--test-proactive",
        choices=["morning", "noon", "evening", "night", "idle", "rant", "summary", "nudge", "late"],
        help="send one proactive message immediately and exit (for testing)",
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
    # 代理：国内访问 Telegram 通常需要走本地代理；填了就用，没填就用系统默认
    proxy = resolve_setting("HTTPS_PROXY", config) or resolve_setting("PROXY", config)
    if proxy:
        os.environ.setdefault("HTTPS_PROXY", proxy)
        os.environ.setdefault("HTTP_PROXY", proxy)
        logging.info("using proxy for outbound requests: %s", proxy)
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
    memos = MemoStore(Path(resolve_setting("MEMOS_FILE", config, str(BASE_DIR / "memos.json"))))
    states = StateStore(Path(resolve_setting("STATES_FILE", config, str(BASE_DIR / "states.json"))))
    personality = load_personality(BASE_DIR / "personality.json")
    emotion_engine = EmotionEngine(personality.get("emotion", {}))
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
        reply_debounce_seconds = float(resolve_setting("REPLY_DEBOUNCE_SECONDS", config, "3.0"))
        reply_speed_tiers = parse_tiers(
            resolve_setting("REPLY_SPEED_TIERS", config, "20,60,20"), (0.2, 0.6, 0.2)
        )
        reply_slow_range = parse_float_range(
            resolve_setting("REPLY_SLOW_RANGE", config, "10-25"), (10.0, 25.0)
        )
        typing_cps = parse_float_range(resolve_setting("TYPING_CPS", config, "2.5-4.5"), (2.5, 4.5))
        typing_refresh = float(resolve_setting("TYPING_REFRESH", config, "4.0"))
        max_messages_per_reply = int(resolve_setting("MAX_MESSAGES_PER_REPLY", config, "6"))
        filler_prob = float(resolve_setting("FILLER_PROB", config, "0.25"))
        late_night_delay = parse_float_range(
            resolve_setting("LATE_NIGHT_DELAY", config, "3-10"), (3.0, 10.0)
        )
        late_night_window = parse_int_range(
            resolve_setting("LATE_NIGHT_WINDOW", config, "0-7"), (0, 7)
        )
        summary_morning = resolve_setting("DAILY_SUMMARY_MORNING", config, "08:30")
        summary_night = resolve_setting("DAILY_SUMMARY_NIGHT", config, "22:00")
        rant_per_week = int(resolve_setting("RANT_PER_WEEK", config, "3"))
        late_care_window = resolve_setting("LATE_CARE_WINDOW", config, "23:30-01:00")
        memory_min_interval_hours = float(resolve_setting("MEMORY_MIN_INTERVAL_HOURS", config, "6"))
        memory_rebuild_at = int(resolve_setting("MEMORY_REBUILD_AT", config, "8000"))
        memory_check_minutes = int(resolve_setting("MEMORY_CHECK_MINUTES", config, "60"))
        memory_extract_every = int(resolve_setting("MEMORY_EXTRACT_EVERY", config, "3"))
        proactive_cooldown_minutes = int(resolve_setting("PROACTIVE_COOLDOWN_MINUTES", config, "45"))
    except ValueError:
        event_min_chars, event_max_chars, event_max_tokens, char_event_days = 600, 800, 2000, 5
        short_event_days = 2
        ask_busy_after_minutes = 10
        leave_after_minutes = 15
        idle_followup_prob = 0.25
        memory_summary_every, memory_inject_limit, memory_condense_at = 6, 2500, 25
        care_banter_prob, event_active_window = 0.2, 90
        reply_debounce_seconds = 3.0
        reply_speed_tiers = (0.2, 0.6, 0.2)
        reply_slow_range = (10.0, 25.0)
        typing_cps = (2.5, 4.5)
        typing_refresh = 4.0
        max_messages_per_reply = 6
        filler_prob = 0.25
        late_night_delay = (3.0, 10.0)
        late_night_window = (0, 7)
        summary_morning, summary_night = "08:30", "22:00"
        rant_per_week = 3
        late_care_window = "23:30-01:00"
        memory_min_interval_hours, memory_rebuild_at, memory_check_minutes = 6.0, 8000, 60
        memory_extract_every, proactive_cooldown_minutes = 3, 45
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
        memos=memos,
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
        memory_rebuild_at=memory_rebuild_at,
        reply_style=reply_style,
        care_banter_prob=care_banter_prob,
        event_active_window=event_active_window,
        reply_debounce_seconds=reply_debounce_seconds,
        reply_speed_tiers=reply_speed_tiers,
        reply_slow_range=reply_slow_range,
        typing_cps=typing_cps,
        typing_refresh=typing_refresh,
        max_messages_per_reply=max_messages_per_reply,
        filler_prob=filler_prob,
        late_night_delay=late_night_delay,
        late_night_window=late_night_window,
        daily_summary_times=(summary_morning, summary_night),
        rant_per_week=rant_per_week,
        late_care_window=late_care_window,
        memory_min_interval_hours=memory_min_interval_hours,
        states=states,
        emotion_engine=emotion_engine,
        personality=personality,
        memory_extract_every=memory_extract_every,
        proactive_cooldown_minutes=proactive_cooldown_minutes,
    )

    if args.test_proactive:
        if not bot.proactive_chat_id:
            logging.error("no chat id available for proactive test")
            return 1
        kind = args.test_proactive
        if kind == "rant":
            bot.send_rant()
        elif kind == "summary":
            bot.send_daily_summary("morning")
        elif kind == "nudge":
            bot.send_todo_nudge()
        elif kind == "late":
            bot.send_late_night_care()
        else:
            bot.send_proactive(kind)
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
        summary_morning=summary_morning,
        summary_night=summary_night,
        rant_per_week=rant_per_week,
        late_care_window=late_care_window,
        memory_check_minutes=memory_check_minutes,
        memory_min_interval_hours=memory_min_interval_hours,
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
