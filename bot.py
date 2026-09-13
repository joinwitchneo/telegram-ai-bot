"""夕颜 V2 · Phase 1 主程序。

本阶段只搭基础设施：配置 → LLM Client（模型分级/降级/usage）→ LLM Policy（总闸门）
→ Token Budget → Conversation（debounce + 调用 + 渲染）→ Telegram。
命令与工具走 Python 直连，不调用模型。
Memory / Emotion / Relationship / Proactive 留到后续 Phase。
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from core import llm_policy
from core.conversation import Conversation
from core.context_debugger import ContextDebugger
from core.context_manager import ContextManager
from core.event_bus import EventBus
from core.llm_client import LLMClient
from core.response_planner import ResponsePlanner
from core.response_stats import ResponseStats
from core.response_validator import ResponseValidator
from core.token_budget import TokenBudget, estimate_tokens
from core.usage_logger import UsageLogger
from personality.consistency_checker import ConsistencyChecker
from proactive.character_life import CharacterLife
from proactive.proactive_engine import ProactiveEngine, ProactiveHistory
from proactive.proactive_scheduler import ProactiveScheduler, ProactiveState
from style import style_adapter
from style.message_renderer import MessageRenderer, RenderLimits
from style.user_style import UserStyle
from self_model.events import SelfModelBridge
from self_model.store import load_self_model
from capabilities.manager import CapabilityManager
from capabilities.services import (
    OcrCapability,
    SearchCapability,
    VideoCapability,
    VisionCapability,
    WeatherCapability,
    WhisperCapability,
)
from tools.bootstrap import build_registry
from tools.core.cache import TTLCache
from tools.core.cost import CostTracker
from tools.core.executor import ToolExecutor
from tools.core.permissions import Permissions
from tools.core.policy import ToolPolicy
from tools.core.schema import register_bin_dir
from tools.interaction.sticker import StickerBook
from tools.interaction.voice_reply import LocalTTS
from tools.perception.router import PerceptionRequest, PerceptionRouter
from tools.perception.vision import OllamaVision
from memory.memory_decay import MemoryDecay
from memory.memory_retriever import MemoryRetriever
from memory.memory_store import MemoryStore
from memory.memory_summary import MemoryExtractor, MemorySummarizer
from memory.topic_memory import TopicMemory
from emotion.emotion_engine import EmotionEngine
from relationship.relationship_engine import RelationshipEngine
from tools import weather
from tools.memos import MemoStore
from tools.reminders import (
    ReminderStore,
    ReminderThread,
    next_water_time,
    parse_reminder,
    parse_reminder_with_llm,
)

TG_API = "https://api.telegram.org/bot{token}/{method}"
MAX_REPLY_LEN = 4096


def _resolve_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def build_fingerprint() -> dict:
    """这次运行的到底是哪份代码——启动第一件事就把它钉下来。"""
    import hashlib

    source = Path(__file__).resolve()
    digest = "?"
    mtime = "?"
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        mtime = datetime.datetime.fromtimestamp(source.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        pass
    return {"path": str(source), "sha256": digest, "mtime": mtime,
            "pid": os.getpid(), "started_at": datetime.datetime.now().isoformat(timespec="seconds")}


def log_build(*, handlers: bool = False) -> dict:
    info = build_fingerprint()
    logging.info("[build] path=%s", info["path"])
    logging.info("[build] sha256=%s", info["sha256"])
    logging.info("[build] mtime=%s", info["mtime"])
    logging.info("[build] pid=%d", info["pid"])
    logging.info("[build] started_at=%s", info["started_at"])
    if handlers:
        logging.info(
            "[build] handlers: /vision=%s /cap=%s /tools=%s",
            "/vision" in Bot.COMMANDS, "/cap" in Bot.COMMANDS, "/tools" in Bot.COMMANDS,
        )
    return info


def _pid_alive(pid: int) -> bool:
    """判断进程是否还活着（不用 psutil，也不做危险操作）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:  # noqa: BLE001
        return False


class SingleInstance:
    """锁文件 + PID 存活检查：不允许多开，也把"另一个实例还在跑"说清楚。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def acquire(self) -> tuple[bool, dict]:
        existing: dict = {}
        if self.path.is_file():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except (OSError, json.JSONDecodeError):
                existing = {}
            other = int(existing.get("pid", 0) or 0)
            if other and other != os.getpid() and _pid_alive(other):
                return False, existing
        info = build_fingerprint()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            logging.warning("[instance] 写锁文件失败：%s", exc)
        self.held = True
        return True, info

    def release(self) -> None:
        if not self.held:
            return
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                if int(data.get("pid", 0) or 0) == os.getpid():
                    self.path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError, ValueError):
            pass


BASE_DIR = _resolve_base_dir()
DATA_DIR = BASE_DIR / "data"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Config:
    def __init__(self, path: Path) -> None:
        self.values = load_env_file(path)

    def get(self, name: str, default: str = "") -> str:
        return os.environ.get(name, "").strip() or self.values.get(name, "").strip() or default

    def get_int(self, name: str, default: int) -> int:
        try:
            return int(self.get(name, str(default)))
        except ValueError:
            return default

    def get_float(self, name: str, default: float) -> float:
        try:
            return float(self.get(name, str(default)))
        except ValueError:
            return default

    def get_bool(self, name: str, default: bool = False) -> bool:
        return self.get(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


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
    request = urllib.request.Request(url, data=data, headers=headers)
    last_error = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {body[:200]}"
            if exc.code in (400, 401, 403, 404, 409, 422):
                raise RuntimeError(last_error) from exc
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"request failed after {retries} tries: {last_error}")


class Bot:
    """Phase 1：Telegram 层 + 命令 + 对话管线。"""

    COMMANDS = (
        "/remind", "/reminders", "/todo", "/done", "/delremind", "/memo", "/memos", "/delmemo",
        "/water", "/search", "/autostart", "/clear", "/status", "/proactive", "/主动",
        "/tools", "/工具", "/cap", "/能力", "/vision", "/视觉", "/start", "/help",
    )

    HELP_TEXT = (
        "这个版本是 V2 的基础架构（Phase 1）。\n"
        "命令：\n"
        "· /todo 看待办 ｜ /remind 内容 记一件 ｜ /done 编号 完成 ｜ /delremind 编号 删除\n"
        "· /memo 内容 ｜ /memos ｜ /memo 编号 ｜ /delmemo 编号 —— 备忘录\n"
        "· /water on|off —— 循环喝水提醒\n"
        "· /search 关键词 —— 联网搜索\n"
        "· /autostart on|off —— 开机自启（Windows）\n"
        "· /proactive —— 看主动消息状态（/proactive now 试一次）\n"
        "· /tools —— 看工具层（/tools cache 缓存 ｜ /tools cost 成本）\n"
        "· /vision —— 视觉链路体检（图片识别每一级的真实结果）\n"
        "· /cap —— 看能力层现状（哪些能力可用）\n"
        "· /clear 清空本会话上下文 ｜ /status 后端状态 ｜ /help 帮助\n"
        "直接发消息就是聊天。"
        "图片、语音、视频、文件、链接都可以直接发。"
    )

    def __init__(self, config: Config) -> None:
        self.config = config
        self.started_at = datetime.datetime.now().isoformat(timespec="seconds")
        self.token = config.get("TELEGRAM_BOT_TOKEN")
        self.city = config.get("CITY", "香港")
        self.search_api_key = config.get("SEARCH_API_KEY")
        self.search_api_url = config.get("SEARCH_API_URL", "https://api.tavily.com/search")
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 用量与预算
        self.usage = UsageLogger(
            DATA_DIR / config.get("USAGE_FILE", "usage.json"),
            enabled=config.get_bool("USAGE_LOG_ENABLED", True),
        )
        self.budget = TokenBudget(
            max_context_tokens=config.get_int("MAX_CONTEXT_TOKENS", 6000),
            max_output_tokens=config.get_int("MAX_OUTPUT_TOKENS", 400),
            daily_token_budget=config.get_int("DAILY_TOKEN_BUDGET", 300000),
            soft_ratio=config.get_float("BUDGET_SOFT_RATIO", 0.8),
            hard_ratio=config.get_float("BUDGET_HARD_RATIO", 0.95),
        )

        # 模型（全部来自配置）
        backend = config.get("BACKEND", "deepseek").lower()
        self.client = LLMClient(
            backend=backend,
            api_key=config.get("DEEPSEEK_API_KEY"),
            base_url=config.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
            if backend == "deepseek"
            else config.get("OLLAMA_URL", "http://127.0.0.1:11434"),
            models={
                "cheap": config.get("SUMMARY_MODEL", "deepseek-flash"),
                "main": config.get("CHAT_MODEL", "deepseek-v4-flash"),
                "strong": config.get("STRONG_MODEL", ""),
            },
            fallback_order=tuple(
                part.strip() for part in config.get("MODEL_FALLBACK_ORDER", "strong,main,cheap").split(",")
                if part.strip()
            ),
            usage_logger=self.usage,
            token_budget=self.budget,
        )
        self.model_report = self.client.validate_models()

        # 事件总线（Phase 4 起各模块会订阅）
        self.bus = EventBus()
        self.bus.subscribe("UserMessageReceived", self._on_user_message)

        # 渲染器与对话管线
        cps = config.get("TYPING_CPS", "2.5-4.5")
        try:
            cps_min, cps_max = (float(x) for x in cps.split("-"))
        except ValueError:
            cps_min, cps_max = 2.5, 4.5
        self.renderer = MessageRenderer(
            send=self.send_message,
            typing=self.send_typing,
            limits=RenderLimits(
                max_messages=config.get_int("MESSAGE_MAX_COUNT", 4),
                min_pause=config.get_float("MESSAGE_MIN_PAUSE", 0.5),
                max_pause=config.get_float("MESSAGE_MAX_PAUSE", 8.0),
                max_chars=config.get_int("MESSAGE_MAX_CHARS", 800),
                max_sticker_probability=config.get_float("STICKER_MAX_PROBABILITY", 0.35),
                cps_min=cps_min,
                cps_max=cps_max,
                max_chars_by_length={
                    "ULTRA_SHORT": config.get_int("MESSAGE_MAX_CHARS_ULTRA_SHORT", 30),
                    "SHORT": config.get_int("MESSAGE_MAX_CHARS_SHORT", 60),
                    "NORMAL": config.get_int("MESSAGE_MAX_CHARS_NORMAL", 200),
                    "LONG": config.get_int("MESSAGE_MAX_CHARS_LONG", 800),
                },
            ),
        )
        # 上下文管理（Phase 2：五层结构 + 模式窗口 + 预算裁剪）
        self.context_manager = ContextManager(
            contract=self._build_contract(),
            persona=self._load_persona(),
            token_budget=self.budget,
            max_context_tokens=config.get_int("MAX_CONTEXT_TOKENS", 6000),
            windows={
                "CASUAL": config.get_int("HISTORY_CASUAL", config.get_int("HISTORY_MESSAGES", 12)),
                "DEEP": config.get_int("HISTORY_DEEP", 25),
                "EMOTIONAL": config.get_int("HISTORY_EMOTIONAL", 25),
                "TASK": config.get_int("HISTORY_TASK", 12),
            },
        )
        # 调试旁路（默认只记日志，不写快照）
        self.debugger = ContextDebugger(
            debug_dir=DATA_DIR / "debug",
            enabled=config.get_bool("CONTEXT_DEBUG", False),
            snapshot_enabled=config.get_bool("CONTEXT_SNAPSHOT", False),
        )
        # ── Phase 3：结构化记忆 + 话题生命周期 ──
        self.memory_store = MemoryStore(
            DATA_DIR / config.get("MEMORIES_FILE", "memories.json"),
            dedup_threshold=config.get_float("MEMORY_DEDUP_SIMILARITY", 0.55),
        )
        self.topics = TopicMemory(
            DATA_DIR / config.get("TOPICS_FILE", "topics.json"),
            cooldown_days=config.get_int("TOPIC_COOLDOWN_DAYS", 3),
            dormant_days=config.get_int("TOPIC_DORMANT_DAYS", 14),
            event_bus=self.bus,
        )
        self.retriever = MemoryRetriever(
            self.memory_store,
            weights={
                "keyword": config.get_float("MEMORY_WEIGHT_KEYWORD", 0.45),
                "importance": config.get_float("MEMORY_WEIGHT_IMPORTANCE", 0.25),
                "recency": config.get_float("MEMORY_WEIGHT_RECENCY", 0.15),
                "frequency": config.get_float("MEMORY_WEIGHT_FREQUENCY", 0.10),
                "emotion": config.get_float("MEMORY_WEIGHT_EMOTION", 0.05),
            },
            top_k=config.get_int("MEMORY_TOP_K", config.get_int("MEMORY_TOPK", 5)),
            cold_keyword_min=config.get_float("MEMORY_COLD_KEYWORD_MIN", 0.3),
        )
        self.decay = MemoryDecay(
            self.memory_store,
            hot_days=config.get_int("MEMORY_HOT_DAYS", 3),
            warm_days=config.get_int("MEMORY_WARM_DAYS", 30),
            archive_days=config.get_int("MEMORY_ARCHIVE_DAYS", 90),
        )
        self.extractor = MemoryExtractor(
            self.memory_store,
            topics=self.topics,
            client=self.client,
            budget=self.budget,
            bus=self.bus,
            model_enabled=config.get_bool("MEMORY_EXTRACT_ENABLED", True),
        )
        self.summarizer = MemorySummarizer(
            self.memory_store,
            topics=self.topics,
            client=self.client,
            budget=self.budget,
            bus=self.bus,
            min_messages=config.get_int("MEMORY_SUMMARY_MIN_MESSAGES", 30),
            max_messages=config.get_int("MEMORY_SUMMARY_MAX_MESSAGES", 50),
            token_threshold=config.get_int("MEMORY_SUMMARY_TOKEN_THRESHOLD", 1500),
        )
        # ── Phase 4：情绪 + 关系（事件驱动，0 token）──
        self.emotion = EmotionEngine(
            DATA_DIR / config.get("EMOTION_STATE_FILE", "emotion_state.json"),
            bus=self.bus,
            inertia=config.get_float("EMOTION_INERTIA", 0.6),
            rates=self._parse_overrides(config.get("EMOTION_DECAY_OVERRIDES", "")),
        )
        self.relationship = RelationshipEngine(
            DATA_DIR / config.get("RELATIONSHIP_STATE_FILE", "relationship_state.json"),
            bus=self.bus,
            inertia=config.get_float("RELATIONSHIP_INERTIA", 0.35),
            rates=self._parse_overrides(config.get("RELATIONSHIP_DECAY_OVERRIDES", "")),
        )
        self._event_memory_at = 0.0
        self._event_memory_min_delta = config.get_float("STATE_EVENT_MEMORY_DELTA", 0.15)
        self._event_memory_cooldown = config.get_int("STATE_EVENT_MEMORY_COOLDOWN_MINUTES", 360) * 60
        self.bus.subscribe("EmotionChanged", self._on_state_changed)
        self.bus.subscribe("RelationshipChanged", self._on_state_changed)

        # ── Phase 5：回复计划 + 用户风格 + AI 味检查（全部 0 token）──
        self.user_style = UserStyle(
            DATA_DIR / config.get("STYLE_FILE", "user_style.json"),
            alpha=config.get_float("STYLE_EMA_ALPHA", 0.10),
            alpha_slow=config.get_float("STYLE_EMA_ALPHA_SLOW", 0.03),
            max_common_words=config.get_int("STYLE_COMMON_WORDS", 12),
            min_messages=config.get_int("STYLE_MIN_MESSAGES", 5),
        )
        self.response_stats = ResponseStats(
            DATA_DIR / config.get("RESPONSE_STATS_FILE", "response_stats.json"),
            enabled=config.get_bool("USAGE_LOG_ENABLED", True),
        )
        self.planner = ResponsePlanner(
            score_normal=config.get_float(
                "PLANNER_SCORE_NORMAL", config.get_float("RESPONSE_SCORE_NORMAL", 0.75)
            ),
            score_short=config.get_float(
                "PLANNER_SCORE_SHORT", config.get_float("RESPONSE_SCORE_SHORT", 0.50)
            ),
            score_low=config.get_float("PLANNER_SCORE_LOW", 0.30),
            allow_silence=config.get_bool("PLANNER_ALLOW_SILENCE", True),
            silence_cooldown_minutes=config.get_int("PLANNER_SILENCE_COOLDOWN_MINUTES", 10),
            max_consecutive_replies=config.get_int("PLANNER_MAX_CONSECUTIVE", 3),
            max_messages=config.get_int("MESSAGE_MAX_COUNT", 4),
            sticker_enabled=config.get_bool("STICKER_ENABLED", True),
        )
        self.validator = ResponseValidator(
            limits=self.renderer.limits,
            fallback_texts=[
                part.strip()
                for part in config.get("FALLBACK_REPLY_TEXTS", "").split("|")
                if part.strip()
            ],
        )
        self.checker = ConsistencyChecker(
            max_severity=config.get_float("AI_FLAVOR_MAX_SEVERITY", 0.5),
            enabled=config.get_bool("AI_FLAVOR_ENABLED", True),
        )

        # ── Phase 6：Character Life + 主动消息（候选生成 0 token）──
        self.life = CharacterLife(
            DATA_DIR / config.get("CHARACTER_LIFE_FILE", "character_life.json"),
            max_entries=config.get_int("CHARACTER_LIFE_MAX_ENTRIES", 80),
        )
        self.proactive_history = ProactiveHistory(
            DATA_DIR / config.get("PROACTIVE_HISTORY_FILE", "proactive_history.json")
        )
        self.proactive_engine = ProactiveEngine(
            life=self.life,
            memory_store=self.memory_store,
            topics=self.topics,
            history=self.proactive_history,
            topic_cooldown_hours=config.get_float("PROACTIVE_TOPIC_COOLDOWN_HOURS", 72.0),
            memory_min_age_hours=config.get_float("PROACTIVE_MEMORY_MIN_AGE_HOURS", 2.0),
            gap_full_hours=config.get_float("PROACTIVE_GAP_FULL_HOURS", 48.0),
            recent_chat_window_minutes=config.get_float("PROACTIVE_RECENT_CHAT_MINUTES", 45.0),
        )
        self.proactive_state = ProactiveState(
            DATA_DIR / config.get("PROACTIVE_STATE_FILE", "proactive_state.json")
        )
        self.conversation = Conversation(
            llm_client=self.client,
            renderer=self.renderer,
            context_manager=self.context_manager,
            token_budget=self.budget,
            usage_logger=self.usage,
            event_bus=self.bus,
            debugger=self.debugger,
            memory_provider=self._recall_memory,
            after_reply=self._after_reply,
            state_provider=self._state_for_context,
            planner=self.planner,
            validator=self.validator,
            checker=self.checker,
            style_provider=self._style_for_context,
            signal_provider=self._signals_for_planner,
            memory_by_id=self._memory_by_id,
            response_stats=self.response_stats,
            max_retry=config.get_int("REPLY_MAX_RETRY", 1),
            history_limit=config.get_int("HISTORY_MESSAGES", 12),
            debounce_single=config.get_float("DEBOUNCE_SINGLE_SECONDS", 1.0),
            debounce_multi=config.get_float("DEBOUNCE_MULTI_SECONDS", 2.0),
        )
        self.proactive_chat_id = config.get_int("PROACTIVE_CHAT_ID", 0) or 0
        self.proactive = ProactiveScheduler(
            engine=self.proactive_engine,
            state=self.proactive_state,
            history=self.proactive_history,
            runner=self._send_proactive,
            signals_provider=lambda: self.proactive_engine and {
                "emotion": self.emotion.emotions(),
                "relationship": self.relationship.dimensions(),
            },
            quiet_start=config.get("PROACTIVE_QUIET_START", "23:30"),
            quiet_end=config.get("PROACTIVE_QUIET_END", "08:00"),
            daily_limit=config.get_int("PROACTIVE_DAILY_LIMIT", 3),
            min_interval_minutes=config.get_float("PROACTIVE_MIN_INTERVAL_MINUTES", 90.0),
            recent_chat_minutes=config.get_float("PROACTIVE_RECENT_CHAT_MINUTES", 45.0),
            nonresponse_hours=config.get_float("PROACTIVE_NONRESPONSE_HOURS", 3.0),
            nonresponse_limit=config.get_int("PROACTIVE_NONRESPONSE_LIMIT", 2),
            cooldown_hours=config.get_float("PROACTIVE_COOLDOWN_HOURS", 6.0),
            threshold_full=config.get_float("PROACTIVE_THRESHOLD_FULL", 0.75),
            threshold_short=config.get_float("PROACTIVE_THRESHOLD_SHORT", 0.35),
            allow_special_override=config.get_bool("PROACTIVE_SPECIAL_OVERRIDE", True),
            enabled=config.get_bool("PROACTIVE_ENABLED", True),
            tick_minutes=config.get_float("PROACTIVE_TICK_MINUTES", 15.0),
            budget=self.budget,
            usage=self.usage,
            stats=self.response_stats,
        )

        # ── Phase 7：Tool Layer（本地优先，联网次之，付费最后）──
        self.bin_dir = self._register_tool_bins(config)
        self.tool_cache = TTLCache(
            DATA_DIR / config.get("TOOL_CACHE_FILE", "tool_cache.json"),
            enabled=config.get_bool("TOOL_CACHE_ENABLED", True),
        )
        self.tool_cost = CostTracker(
            DATA_DIR / config.get("TOOL_COST_FILE", "tool_cost.json"),
            enabled=config.get_bool("USAGE_LOG_ENABLED", True),
        )
        self.vision = OllamaVision(
            config.get("OLLAMA_URL", "http://127.0.0.1:11434"),
            config.get("VISION_MODEL", ""),
            enabled=config.get_bool("VISION_ENABLED", True),
            exe_path=config.get("OLLAMA_EXE", ""),
            autostart=config.get_bool("OLLAMA_AUTOSTART", True),
            start_timeout=config.get_float("OLLAMA_START_TIMEOUT", 90.0),
        )
        self.sticker_book = StickerBook(
            DATA_DIR / config.get("STICKER_FILE", "stickers.json")
            if (DATA_DIR / config.get("STICKER_FILE", "stickers.json")).is_file()
            else BASE_DIR / config.get("STICKER_FILE", "stickers.json")
        )
        self.tts = LocalTTS(
            piper_model=config.get("TTS_PIPER_MODEL", ""),
            enabled=config.get_bool("VOICE_REPLY_ENABLED", False),
        )
        self.registry = build_registry(
            city=self.city,
            vision=self.vision,
            sticker_book=self.sticker_book,
            tts=self.tts,
            whisper_model_path=config.get("WHISPER_MODEL_PATH", ""),
            search_api_key=self.search_api_key,
            search_api_url=self.search_api_url,
        )
        self.permissions = Permissions(
            allow_network=config.get_bool("ALLOW_NETWORK_TOOLS", True),
            allow_local_model=config.get_bool("ALLOW_LOCAL_MODEL", True),
            allow_paid_api=config.get_bool("ALLOW_PAID_TOOLS", True),
            allow_file_write=True,
        )
        self.tools = ToolExecutor(
            self.registry,
            cache=self.tool_cache,
            cost=self.tool_cost,
            permissions=self.permissions,
            timeout=config.get_float("TOOL_HTTP_TIMEOUT", 12.0),
            enabled=config.get_bool("TOOL_LAYER_ENABLED", True),
        )
        self.tool_policy = ToolPolicy(self.registry, enabled=config.get_bool("TOOL_LAYER_ENABLED", True))
        self.perception = PerceptionRouter(
            vision=self.vision,
            use_ocr=config.get_bool("OCR_ENABLED", True),
            whisper_model_path=config.get("WHISPER_MODEL_PATH", ""),
            whisper_prompt=config.get("WHISPER_PROMPT", ""),
            language=config.get("WHISPER_LANGUAGE", "zh"),
            max_frames=config.get_int("VIDEO_MAX_FRAMES", 4),
            vision_prompt=config.get("VISION_PROMPT", ""),
        )
        self.media_dir = DATA_DIR / config.get("MEDIA_TMP_DIR", "tmp")

        # ── Phase 8：Self Model（她怎么理解"我自己"）──
        self.self_model = load_self_model(
            DATA_DIR / config.get("SELF_MODEL_FILE", "self_model.json"),
            enabled=config.get_bool("SELF_MODEL_ENABLED", True),
        )
        self.self_bridge = SelfModelBridge(
            self.self_model,
            bus=self.bus,
            enabled=config.get_bool("SELF_MODEL_ENABLED", True),
            cooldown_minutes=config.get_float("SELF_MODEL_THOUGHT_COOLDOWN_MINUTES", 30.0),
        )
        self._self_model_context_enabled = config.get_bool("SELF_MODEL_IN_CONTEXT", True)

        # ── 能力层：核心 Bot 只问"我现在有没有这个能力" ──
        self.caps = CapabilityManager(enabled=True)
        self.caps.register(VisionCapability(self.vision, enabled=config.get_bool("VISION_ENABLED", True)))
        self.caps.register(OcrCapability(enabled=config.get_bool("OCR_ENABLED", True)))
        self.caps.register(WhisperCapability(
            model_path=config.get("WHISPER_MODEL_PATH", ""),
            language=config.get("WHISPER_LANGUAGE", "zh"),
            prompt=config.get("WHISPER_PROMPT", ""),
        ))
        self.caps.register(VideoCapability())
        self.caps.register(WeatherCapability())
        self.caps.register(SearchCapability(
            api_key=self.search_api_key, api_url=self.search_api_url
        ))

        # 本地存储（提醒 / 备忘录）
        self.reminders = ReminderStore(DATA_DIR / config.get("REMINDERS_FILE", "reminders.json"))
        self.memos = MemoStore(DATA_DIR / config.get("MEMOS_FILE", "memos.json"))
        self._last_activity = time.time()
        self._last_user_text = ""

    # ── L0 / L1（构建一次，之后字节不变，利于 prompt cache）────────
    @staticmethod
    def _build_contract() -> str:
        """L0 System Contract：身份 + 输出协议 + 硬性边界。"""
        return (
            "【系统合同】\n"
            "你是「夕颜」，住在 Telegram 里的 AI。你不是 AI 助理、不是客服、不是答题工具；"
            "第一职责是聊天，不是解决问题。\n"
            "输出协议：直接输出你要发的话；要发多条就用换行分开；"
            "禁止 Markdown 表格、框线字符与代码块；不要把内部状态、数值、设定说给对方听。\n"
            "行为边界：不编造自己没有的经历（没有身体、没有现实生活）；不知道就说不知道；"
            "不假装能看到对方的设备、位置或现实处境；不读心，猜测要用“我猜/你是不是”。\n"
        )

    @staticmethod
    def _load_persona() -> str:
        """L1 Persona：Phase 2 继续沿用现有静态人格文件。"""
        persona_file = BASE_DIR / "persona_core.txt"
        if persona_file.is_file():
            try:
                return persona_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logging.warning("读取 persona_core.txt 失败：%s", exc)
        return "你是夕颜，说话短、嘴硬心软，像真人聊天。"

    # ── Telegram 收发 ──────────────────────────────────────────────
    def tg_call(self, method: str, payload: dict | None = None, timeout: int = 60, retries: int = 3) -> dict:
        return api_request(
            TG_API.format(token=self.token, method=method), payload, timeout=timeout, retries=retries
        )

    def send_message(self, chat_id: int, text: str) -> None:
        text = (text or "").strip() or "…"
        for chunk in (text[i : i + MAX_REPLY_LEN] for i in range(0, len(text), MAX_REPLY_LEN)):
            self.tg_call("sendMessage", {"chat_id": chat_id, "text": chunk}, retries=5)

    def send_typing(self, chat_id: int) -> None:
        try:
            self.tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=30, retries=2)
        except Exception:  # noqa: BLE001
            pass

    # ── 事件 ───────────────────────────────────────────────────────
    def _on_user_message(self, event) -> None:
        gap_hours = (time.time() - self._last_activity) / 3600
        self._last_activity = time.time()
        if gap_hours >= 6:
            # 很久没说话又回来了：让情绪/关系系统知道（走 Event Bus）
            self.bus.publish("UserReturned", gap_hours=round(gap_hours, 2))

    @staticmethod
    def _parse_overrides(text: str) -> dict:
        """解析 "joy:0.12,anger:0.1" 这类覆盖配置。"""
        overrides: dict = {}
        for chunk in (text or "").split(","):
            if ":" not in chunk:
                continue
            key, _, value = chunk.partition(":")
            try:
                overrides[key.strip()] = float(value.strip())
            except ValueError:
                continue
        return overrides

    # ── Phase 4：状态 → L2 ─────────────────────────────────────────
    def _signals_for_planner(self) -> dict:
        """给 Response Planner 的数值信号（只进程序，不进 Prompt）。"""
        try:
            return {
                "emotion": self.emotion.emotions(),
                "relationship": self.relationship.dimensions(),
            }
        except Exception as exc:  # noqa: BLE001 - 状态失败不能阻断聊天
            logging.warning("读取状态信号失败：%s", exc)
            return {}

    def _style_for_context(self, chat_id: int) -> dict:
        """给 L2 的用户风格参考（自然语言，不含数字）。"""
        try:
            text = style_adapter.render(self.user_style.profile(chat_id))
        except Exception as exc:  # noqa: BLE001
            logging.warning("生成用户风格参考失败：%s", exc)
            return {}
        return {"style": text[:120]} if text else {}

    def _memory_by_id(self, ids: list[str]) -> list[dict]:
        records = []
        for memory_id in ids or []:
            try:
                record = self.memory_store.get(memory_id)
            except Exception:  # noqa: BLE001
                record = None
            if record:
                records.append(record)
        return records

    # ── Phase 6：Character Life 维护 + 主动消息 ────────────────────
    UNFINISHED_MARKERS = (
        "考虑一下", "我想想", "再想想", "再说吧", "回头说", "以后再说", "先这样",
        "还没决定", "不确定", "看看再说", "过几天再说", "等我消息",
        "考虑", "再考虑", "还没想好", "到时候再说",
    )
    EVENT_WORDS = (
        "考试", "面试", "开会", "体检", "出差", "答辩", "复试", "笔试",
        "报名", "交材料", "截止", "生日", "复查", "搬家",
    )
    TIME_HINTS = (
        ("明天", 36), ("后天", 60), ("下周", 24 * 8), ("下个月", 24 * 35),
        ("这周", 24 * 5), ("周末", 24 * 4), ("今天", 24),
    )

    def _update_character_life(self, text: str) -> None:
        """从用户这句话里提炼"她值得记住的念头/事件"（0 token，纯规则）。"""
        raw = (text or "").strip()
        if not raw or len(raw) > 200:
            return
        try:
            existing = {item["content"] for item in self.life.active()}
            if any(word in raw for word in self.EVENT_WORDS) and any(
                word in raw for word, _ in self.TIME_HINTS
            ):
                ttl = 72
                for word, hours in self.TIME_HINTS:
                    if word in raw:
                        ttl = hours
                        break
                content = raw[:60]
                if content not in existing:
                    self.life.add(
                        "important_event", content, source="CONFIRMED",
                        importance=0.85, ttl_hours=ttl, reason="user_event",
                    )
                return
            if any(word in raw for word in self.UNFINISHED_MARKERS):
                content = f"还想知道他后来怎么决定的：{raw[:50]}"
                if content not in existing:
                    self.life.add(
                        "unfinished_thought", content, source="INTERNAL",
                        importance=0.7, ttl_hours=72, reason="unfinished",
                    )
        except Exception as exc:  # noqa: BLE001 - 角色生活失败不能影响聊天
            logging.warning("更新角色生活失败：%s", exc)

    def _send_proactive(self, candidate) -> dict:
        """主动消息的出口：交给 Conversation 走完整管线。"""
        if not self.proactive_chat_id:
            return {"sent": [], "cancelled": True, "reason": "no_chat_id"}
        return self.conversation.proactive(self.proactive_chat_id, candidate=candidate)

    # ── Phase 7：感知层（图片 / 语音 / 视频 / 文件 / 网页）─────────
    def _register_tool_bins(self, config: "Config") -> str:
        """把便携版二进制目录（TOOL_BIN_DIR）加进查找范围：ffmpeg / whisper / ollama。"""
        raw = config.get("TOOL_BIN_DIR", "")
        if not raw:
            return ""
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = (BASE_DIR / raw).resolve()
        if not target.is_dir():
            logging.warning("TOOL_BIN_DIR 不存在：%s", target)
            return ""
        register_bin_dir(str(target))
        # 便携包常见的两级结构（whisper\Release\whisper-cli.exe）也要能找到
        for name in target.iterdir():
            if name.is_dir():
                register_bin_dir(str(name))
                for nested in name.iterdir():
                    if nested.is_dir():
                        register_bin_dir(str(nested))
            elif name.suffix.lower() in (".exe", ".cmd", ".bat"):
                register_bin_dir(str(target))
        logging.info("工具便携目录：%s", target)
        return str(target)

    def download_telegram_file(self, file_id: str, suffix: str = "") -> str:
        """把 Telegram 上的文件下载到本机（本地处理的前提，不上传第三方）。"""
        logging.info("[telegram][photo] downloading file_id=%s", str(file_id)[:24])
        info = self.tg_call("getFile", {"file_id": file_id}, timeout=30)
        file_path = ((info.get("result") or {}).get("file_path") or "").strip()
        if not file_path:
            raise RuntimeError("Telegram 没有返回可下载的文件路径")
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with urllib.request.urlopen(url, timeout=90) as response:
            payload = response.read()
        self.media_dir.mkdir(parents=True, exist_ok=True)
        name = Path(file_path).name
        if suffix and not name.lower().endswith(suffix.lower()):
            name = f"{name}{suffix}"
        target = self.media_dir / name
        target.write_bytes(payload)
        logging.info("[telegram][photo] downloaded path=%s size=%d bytes", target, len(payload))
        return str(target)

    def _perceive_and_reply(self, chat_id: int, *, source_type: str, file_id: str = "",
                            suffix: str = "", meta: dict | None = None, summary: str = "",
                            user_text: str = "") -> dict:
        """下载 → 本地感知 → 交给 Conversation 用角色语气回应。"""
        decision = self.tool_policy.decide_perception(source_type)
        path = ""
        if file_id:
            try:
                path = self.download_telegram_file(file_id, suffix)
            except Exception as exc:  # noqa: BLE001 - 下载失败要如实说
                logging.warning("下载 Telegram 文件失败：%s", exc)
                self.send_message(chat_id, "这个文件我没接住，你重发一次试试。")
                return {"sent": [], "error": str(exc)}
        result = None
        if decision.tool in ("image", "audio", "video"):
            # 媒体必须走感知路由器：真实调用本地视觉 / OCR / Whisper / 抽帧
            logging.info("[perception] %s 进入感知路由器 path=%s", source_type, path or "-")
            result = self.perception.perceive(
                PerceptionRequest(source_type=source_type, path=path, meta=meta or {})
            )
            self.tool_cost.record(
                f"perception.{source_type}", mode="LOCAL",
                error=not result.ok, cache_hit=False,
            )
        elif decision.tool:
            result = self.tools.run(decision.tool, path=path, meta=meta or {})
        if result is not None:
            # 感知结果必须留下痕迹，否则出问题只能靠猜
            logging.info(
                "[perception] %s ok=%s error=%s text=%s",
                source_type, result.ok, result.error or "-", (result.text or "")[:80],
            )
        if result is None or not summary:
            summary = (
                PerceptionRouter.describe_for_context(result)
                if result is not None
                else "这条内容本地没能读到"
            )
        return self.conversation.perceive(
            chat_id, source_type=source_type, summary=summary, user_text=user_text,
            perception_ok=bool(result.ok) if result is not None else False,
        )

    def handle_photo(self, chat_id: int, message: dict) -> None:
        photos = message.get("photo") or []
        biggest = photos[-1] if photos else {}
        logging.info(
            "[telegram][photo] received sizes=%d file_id=%s",
            len(photos), str(biggest.get("file_id", ""))[:24],
        )
        self._perceive_and_reply(
            chat_id, source_type="image", file_id=biggest.get("file_id", ""), suffix=".jpg"
        )

    def handle_voice(self, chat_id: int, message: dict) -> None:
        voice = message.get("voice") or message.get("audio") or {}
        self._perceive_and_reply(
            chat_id, source_type="voice", file_id=voice.get("file_id", ""), suffix=".ogg",
            meta={"duration": voice.get("duration", 0), "mime": voice.get("mime_type", "")},
        )

    def handle_video(self, chat_id: int, message: dict) -> None:
        video = message.get("video") or message.get("video_note") or message.get("animation") or {}
        self._perceive_and_reply(
            chat_id, source_type="video", file_id=video.get("file_id", ""), suffix=".mp4",
            meta={
                "duration": video.get("duration", 0),
                "width": video.get("width", 0),
                "height": video.get("height", 0),
            },
        )

    def handle_document(self, chat_id: int, message: dict) -> None:
        document = message.get("document") or {}
        name = str(document.get("file_name") or "")
        self._perceive_and_reply(
            chat_id, source_type="document", file_id=document.get("file_id", ""),
            suffix=Path(name).suffix, meta={"file_name": name},
        )

    def handle_sticker(self, chat_id: int, message: dict) -> None:
        sticker = message.get("sticker") or {}
        emoji = sticker.get("emoji") or ""
        summary = f"用户发来一个表情包（{emoji}）"
        local = self.sticker_book.pick(emoji) if self.sticker_book.available() else None
        if local is not None and local.ok:
            self.send_sticker(chat_id, local.data.get("path", ""))
        self.conversation.perceive(chat_id, source_type="sticker", summary=summary)

    def handle_url(self, chat_id: int, text: str, url: str) -> None:
        """用户发链接：本地读正文 → 交给角色回应。"""
        result = self.tools.run("web_reader", url=url)
        summary = result.text if result.ok else f"这个链接我没读到正文（{result.error}）"
        self._perceive_and_reply(
            chat_id, source_type="url", summary=summary, user_text=text
        )

    def send_sticker(self, chat_id: int, path: str) -> None:
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                payload = handle.read()
            boundary = "----xiyan" + str(int(time.time() * 1000))
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"sticker\"; "
                f"filename=\"{Path(path).name}\"\r\nContent-Type: image/webp\r\n\r\n"
            ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
            request = urllib.request.Request(
                TG_API.format(token=self.token, method="sendSticker"),
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            urllib.request.urlopen(request, timeout=60).read()
        except Exception as exc:  # noqa: BLE001 - 发不出去也不能炸
            logging.warning("发送表情包失败：%s", exc)

    def answer_with_tool(self, chat_id: int, text: str) -> bool:
        """Tool Policy 的 L0 分流：能 Python 直接答的，绝不叫模型。"""
        decision = self.tool_policy.decide(text)
        if not decision.is_direct or not decision.tool:
            return False
        result = self.tools.run(decision.tool, text=text, chat_id=chat_id, tag=text)
        if not result.ok:
            return False
        self.send_message(chat_id, result.text)
        self.usage.record_local("rule", 0, note=f"tool:{decision.tool}")
        return True

    def enrich_with_tool(self, chat_id: int, text: str) -> bool:
        """需要联网/本地感知但不需要额外模型的工具：跑完把结果交给角色说。"""
        decision = self.tool_policy.decide(text)
        if not decision.tool or decision.level == "L2":
            return False
        if decision.tool == "weather":
            city = self._weather_city()
            result = self.tools.run("weather", city=city)
        elif decision.tool == "web_search":
            result = self.tools.run("web_search", query=text)
        elif decision.tool == "location":
            result = self.tools.run("location", name=text)
        elif decision.tool == "web_reader":
            result = self.tools.run("web_reader", url=decision.args.get("url", ""))
        else:
            return False
        if not result.ok:
            return False
        self.conversation.perceive(
            chat_id, source_type="tool_result",
            summary=f"{decision.tool} 的查询结果：{result.text}",
            user_text=text,
        )
        return True

    def _state_for_context(self) -> dict:
        """给 Context Manager 的 L2 内容（自然语言，不含数字）。"""
        try:
            hints = self.emotion.behavior_hints() + self.relationship.behavior_hints()
            state = {
                "emotion": self.emotion.describe(),
                "relationship": self.relationship.describe(),
                "behavior": "；".join(hints)[:160],
            }
            view = self.self_model_view(self._last_user_text or "")
            if view:
                state["self_model"] = view
            return state
        except Exception as exc:  # noqa: BLE001 - 状态失败不能阻断聊天
            logging.warning("读取状态失败：%s", exc)
            return {}

    def self_model_view(self, text: str) -> str:
        """只有相关话题才召回 Self Model（0 token 的规则判断），且只给最相关的一两句。"""
        if not self._self_model_context_enabled:
            return ""
        try:
            from core.context_manager import wants_self_model

            if not wants_self_model(text):
                return ""
            identity = self.self_model.identity()
            parts = [f"我是{identity.get('name', '夕颜')}，是 AI，不是人类；我的连续性靠记忆和经历，不是靠某次模型调用。"]
            questions = self.self_model.questions()
            if questions:
                parts.append(f"我最近在想：{questions[-1]}")
            beliefs = self.self_model.beliefs()
            if beliefs:
                last = beliefs[-1]
                parts.append(f"我现在的看法：{last['belief']}（确信度 {last.get('confidence', 0.5)}）")
            return "；".join(parts)
        except Exception as exc:  # noqa: BLE001
            logging.warning("生成自我认知失败：%s", exc)
            return ""

    def _on_state_changed(self, event) -> None:
        """只有"长期有意义"的状态变化才写进 Memory，且带冷却。"""
        deltas = event.payload.get("deltas", {}) or {}
        if not deltas:
            return
        biggest = max(abs(float(value)) for value in deltas.values())
        if biggest < self._event_memory_min_delta:
            return
        if (time.time() - self._event_memory_at) < self._event_memory_cooldown:
            return
        self._event_memory_at = time.time()
        try:
            if event.name == "RelationshipChanged":
                content = f"关系变化：{self.relationship.describe()}"
                kind = "relationship_event"
            else:
                content = f"情绪变化：{self.emotion.describe()}"
                kind = "emotion_event"
            record, action = self.memory_store.create(
                type=kind, content=content[:60], importance=0.6, protected=kind == "relationship_event"
            )
            if action in ("created", "superseded"):
                self.bus.publish("MemoryCreated", memory_id=record.get("id"), type=kind)
        except Exception as exc:  # noqa: BLE001
            logging.warning("状态事件写记忆失败：%s", exc)

    # ── Phase 3：记忆召回与写入 ────────────────────────────────────
    def _recall_memory(self, text: str, history: list[dict]) -> list[dict]:
        """给 Context Manager 的 L3：只返回 Top-K，并记 use_count。"""
        try:
            return self.retriever.retrieve_blocks(text, mark_used=True)
        except Exception as exc:  # noqa: BLE001 - 记忆失败不能影响聊天
            logging.warning("记忆召回失败：%s", exc)
            return []

    def _after_reply(self, chat_id: int, user_text: str, history: list[dict]) -> None:
        """回复之后：规则优先写入记忆；达到阈值再跑结构化摘要。"""
        try:
            written = self.extractor.process(user_text)
            if written:
                logging.info("写入记忆 %d 条：%s", len(written),
                             [item["memory"].get("content") for item in written])
        except Exception as exc:  # noqa: BLE001
            logging.warning("记忆写入失败：%s", exc)
        estimated = estimate_tokens("\n".join(str(item.get("content", "")) for item in history))
        if self.summarizer.should_run(message_count=len(history), token_count=estimated):
            threading.Thread(
                target=self._summarize_worker, args=(chat_id, list(history)), daemon=True
            ).start()

    def _summarize_worker(self, chat_id: int, history: list[dict]) -> None:
        try:
            report = self.summarizer.run(history)
            if report.get("ran"):
                logging.info("结构化摘要完成：写入 %d 条记忆", len(report.get("written", [])))
        except Exception as exc:  # noqa: BLE001
            logging.warning("结构化摘要失败：%s", exc)

    # ── 消息入口 ───────────────────────────────────────────────────
    def handle(self, update: dict) -> None:
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if not chat_id:
            return
        try:
            self.proactive.on_user_message()
        except Exception as exc:  # noqa: BLE001 - 主动状态失败不能影响聊天
            logging.warning("主动状态更新失败：%s", exc)
        text = (message.get("text") or "").strip()
        if text:
            self._last_user_text = text
        if message.get("location"):
            self.handle_location(chat_id, message["location"])
            return
        if message.get("photo"):
            self.handle_photo(chat_id, message)
            return
        if message.get("voice") or message.get("audio"):
            self.handle_voice(chat_id, message)
            return
        if message.get("video") or message.get("video_note") or message.get("animation"):
            self.handle_video(chat_id, message)
            return
        if message.get("document"):
            self.handle_document(chat_id, message)
            return
        if message.get("sticker"):
            self.handle_sticker(chat_id, message)
            return
        if not text:
            return
        if text.startswith("/"):
            self.handle_command(chat_id, text)
            return
        # 天气这类"简单内容"由 Python 直接答，不调模型
        if self._is_weather_question(text):
            self.reply_weather(chat_id)
            return
        # Phase 7：先看 Tool Policy —— L0 直接答，联网工具喂给角色说
        try:
            if self.answer_with_tool(chat_id, text):
                return
        except Exception as exc:  # noqa: BLE001 - 工具挂了照常聊天
            logging.warning("L0 工具处理失败：%s", exc)
        url_match = re.search(r"https?://\S+", text)
        if url_match:
            try:
                self.handle_url(chat_id, text, url_match.group(0))
                return
            except Exception as exc:  # noqa: BLE001
                logging.warning("网页读取失败：%s", exc)
        try:
            if self.enrich_with_tool(chat_id, text):
                return
        except Exception as exc:  # noqa: BLE001
            logging.warning("联网工具处理失败：%s", exc)
        try:
            self.user_style.observe(chat_id, text)
        except Exception as exc:  # noqa: BLE001 - 风格统计失败不能阻断聊天
            logging.warning("记录用户风格失败：%s", exc)
        self._update_character_life(text)
        self.usage.record_local("tool", estimate_tokens(text), note="进入对话管线前的判定")
        self.conversation.handle_text(chat_id, text)

    def _is_weather_question(self, text: str) -> bool:
        if "天气" not in text:
            return False
        return len(text) <= 30 and any(word in text for word in ("怎么样", "如何", "查", "看", "多少度", "冷不冷", "热不热"))

    def reply_weather(self, chat_id: int) -> None:
        city = self._weather_city()
        info = weather.get_weather(city)
        if not info:
            self.send_message(chat_id, "天气没查到，等下再试。")
            return
        self.send_message(chat_id, f"{info}\n自己看着穿。")

    def _weather_city(self) -> str:
        location_file = DATA_DIR / "location.json"
        try:
            data = json.loads(location_file.read_text(encoding="utf-8"))
            if data.get("lat") is not None and data.get("lon") is not None:
                return f"{data['lat']},{data['lon']}"
        except (OSError, json.JSONDecodeError):
            pass
        return self.city

    def handle_location(self, chat_id: int, location: dict) -> None:
        lat, lon = location.get("latitude"), location.get("longitude")
        if lat is None or lon is None:
            return
        (DATA_DIR / "location.json").write_text(
            json.dumps({"lat": lat, "lon": lon}), encoding="utf-8"
        )
        self.send_message(chat_id, "位置记下了，以后天气按这个报。")

    # ── 命令 ───────────────────────────────────────────────────────
    def handle_command(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        handlers = {
            "/remind": self.cmd_remind,
            "/reminders": self.cmd_todo,
            "/todo": self.cmd_todo,
            "/done": self.cmd_done,
            "/delremind": self.cmd_delremind,
            "/memo": self.cmd_memo,
            "/memos": self.cmd_memos,
            "/delmemo": self.cmd_delmemo,
            "/water": self.cmd_water,
            "/search": self.cmd_search,
            "/autostart": self.cmd_autostart,
            "/clear": self.cmd_clear,
            "/status": self.cmd_status,
            "/proactive": self.cmd_proactive,
            "/主动": self.cmd_proactive,
            "/tools": self.cmd_tools,
            "/工具": self.cmd_tools,
            "/cap": self.cmd_caps,
            "/能力": self.cmd_caps,
            "/vision": self.cmd_vision,
            "/视觉": self.cmd_vision,
            "/start": self.cmd_help,
            "/help": self.cmd_help,
        }
        handler = handlers.get(command)
        if handler is None:
            self.send_message(chat_id, "没这个命令，发 /help 看我认哪些。")
            return
        handler(chat_id, rest)
        self.usage.record_local("rule", estimate_tokens(text), note=f"命令 {command}")

    def cmd_help(self, chat_id: int, _rest: str = "") -> None:
        self.send_message(chat_id, self.HELP_TEXT)

    def cmd_clear(self, chat_id: int, _rest: str = "") -> None:
        self.conversation.clear_history(chat_id)
        self.send_message(chat_id, "这个会话的上下文清空了。")

    def cmd_proactive(self, chat_id: int, rest: str = "") -> None:
        """查看（或手动触发一次）主动消息：为什么她会主动找我。"""
        if (rest or "").strip().lower() in ("cap", "caps", "能力"):
            self.cmd_caps(chat_id, "")
            return
        action = (rest or "").strip().lower()
        if action in ("now", "试一下", "test"):
            result = self.proactive.tick(force=True)
            if result.get("sent"):
                return
            self.send_message(
                chat_id,
                f"这次没主动找你。原因：{result.get('reason')}"
                + (f"（候选分数 {result['candidate']['score']}）" if result.get("candidate") else ""),
            )
            return
        status = self.proactive.status()
        life = self.life.stats()
        history = self.proactive_history.stats()
        lines = [
            f"主动开关：{'开' if status['enabled'] else '关'}　静默时段：{'是' if status['quiet'] else '否'}",
            f"今天已主动：{status['sent_today']}/{status['daily_limit']}　上次主动：{status['last_sent_at'] or '还没有'}",
            f"连续未回复：{status['nonresponse_streak']}　冷却到：{status['cooldown_until'] or '无'}",
            f"主动记录：发过 {history.get('sent', 0)} 次，被回过 {history.get('replied', 0)} 次",
            f"角色生活：共 {life.get('total', 0)} 条，进行中 {life.get('by_status', {}).get('ACTIVE', 0)} 条",
        ]
        active = [item for item in self.life.active()][-3:]
        for item in active:
            lines.append(f"· [{item.get('source')}] {item.get('content')}")
        self.send_message(chat_id, "\n".join(lines))

    def cmd_tools(self, chat_id: int, rest: str = "") -> None:
        """看一眼工具与能力的现状。"""
        if (rest or "").strip().lower() in ("cap", "caps", "能力"):
            self.cmd_caps(chat_id, "")
            return
        self._cmd_tools_body(chat_id, rest)

    def cmd_caps(self, chat_id: int, _rest: str = "") -> None:
        """能力层状态：她"现在拥有/没有"哪些能力（只给诊断看，不进 Context）。"""
        lines = ["能力现状（核心 Bot 只问有没有，不关心怎么实现）："]
        for name, item in sorted(self.caps.report()["capabilities"].items()):
            mark = "可用" if item["available"] else "不可用"
            desc = item["description"].split("：")[0]
            lines.append(f"· {name}：{mark}（{desc}）")
            state = item["state"]
            if state.get("last_error"):
                lines.append(f"    最近错误：{state['last_error'][:60]}")
        lines.append("状态只用于诊断与日志，不会进入她的对话上下文。")
        self.send_message(chat_id, "\n".join(lines))

    def cmd_vision(self, chat_id: int, _rest: str = "") -> None:
        """一键体检整条视觉链路，每一步的真实结果都发到 Telegram。"""
        from tools.perception import vision as vision_mod

        source = Path(__file__).resolve()
        try:
            import hashlib

            digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
        except OSError:
            digest = "?"
        started = getattr(self, "started_at", None)
        lines = [
            "视觉链路体检（每一步都实测）：",
            f"[运行代码] {source}",
            f"[运行代码] sha256={digest} mtime={datetime.datetime.fromtimestamp(source.stat().st_mtime):%Y-%m-%d %H:%M:%S}",
            f"[进程] pid={os.getpid()} 启动于 {started or '未知'}",
            f"[命令] /vision 已注册={hasattr(self, 'cmd_vision')} ／ /cap={hasattr(self, 'cmd_caps')}",
        ]
        last = vision_mod.last_call()
        lines.append(
            "[最近一次 Vision] " + (
                f"成功 ok={last.get('ok')} model={last.get('model')} 耗时={last.get('latency_ms')}ms 文本={str(last.get('text'))[:80]}"
                if last else "（本次进程内还没有调用过）"
            )
        )
        if last.get("error"):
            lines.append(f"[最近一次错误] {str(last.get('error'))[:150]}")
        # 1. 生成一张已知测试图：白底 + 红色圆圈
        try:
            test_image = self._make_test_image()
            size = test_image.stat().st_size
            lines.append(f"1) 测试图生成：OK（{test_image}，{size} 字节）")
        except Exception as exc:  # noqa: BLE001
            self.send_message(chat_id, "\n".join(lines + [f"1) 测试图生成失败：{exc}"]))
            return
        # 2. 本地文件是否真的存在
        lines.append(f"2) 本地文件存在：{test_image.is_file()}")
        # 3. OCR
        try:
            ocr_result = self.tools.run("image", path=str(test_image))
            lines.append(f"3) 图片元数据：{ocr_result.text}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"3) 图片元数据读取异常：{exc}")
        # 4. Ollama 健康 + 模型
        lines.append(f"4) Ollama 可用：{self.vision.available()}")
        lines.append(f"5) 使用模型：{self.vision.resolve_model() or '（无）'}")
        lines.append(f"5b) 模型缓存状态：{self.vision._model_cache!r}")
        # 5. 真实推理
        vision_result = self.caps.execute(
            "vision", path=str(test_image),
            prompt="用中文简短描述这张图片里有什么，有文字就照抄。40字以内。",
        )
        lines.append(
            f"6) VisionResult(success={vision_result.success}, capability={vision_result.capability}, "
            f"degraded={vision_result.degraded}, latency={vision_result.latency_ms}ms, "
            f"error={vision_result.error or 'None'})"
        )
        lines.append(f"6b) data 非空：{bool(vision_result.data)} data={str(vision_result.data)[:120]}")
        if vision_result.success:
            lines.append(f"7) Vision 返回：{vision_result.summary[:160]}")
        else:
            lines.append(f"7) Vision 失败原因（原始错误）：{vision_result.error[:200]}")
        # 6. 端到端：让它真的走一遍 Context
        try:
            endtoend = self.perception.perceive(
                PerceptionRequest(source_type="image", path=str(test_image))
            )
            lines.append(f"8) 路由器结果：ok={endtoend.ok}")
            lines.append(f"9) 进入 Context 的文本：{(endtoend.text or endtoend.error)[:200]}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"8) 路由器异常：{exc}")
        # 7. 硬校验：最终发给 LLM 的 messages 里到底有没有视觉结果（只拼上下文，不调模型）
        try:
            instruction = self.conversation.perception_instruction(
                "image", PerceptionRouter.describe_for_context(endtoend) if endtoend else "",
                ok=bool(endtoend and endtoend.ok),
            )
            built = self.context_manager.build(
                user_text="", history=[], state=None, memory_blocks=[],
                mode="CASUAL", extra_system=instruction,
            )
            joined = "\n".join(str(m.get("content", "")) for m in built.messages)
            injected = (endtoend.text[:12] in joined) if (endtoend and endtoend.ok) else ("没有获得任何视觉" in joined)
            lines.append(f"9b) 最终 LLM messages 注入校验：{injected}")
            lines.append(f"9c) messages 内容片段：{joined.split('【本地感知结果】')[-1][:120]}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"9b) messages 注入校验异常：{exc}")
        lines.append("10) 能力层状态：" + "；".join(
            f"{name}={'可用' if item['available'] else '不可用'}"
            for name, item in sorted(self.caps.report()["capabilities"].items())
        ))
        self.send_message(chat_id, "\n".join(lines))

    def _make_test_image(self, size: int = 240) -> Path:
        """生成一张可验证的测试图（白底 + 红色圆圈），纯标准库，不依赖 Pillow。"""
        import struct
        import zlib

        rows = []
        centre = size / 2
        radius = size * 0.3
        for y in range(size):
            row = bytearray([0])
            for x in range(size):
                inside = (x - centre) ** 2 + (y - centre) ** 2 <= radius ** 2
                row += bytes((214, 40, 40) if inside else (255, 255, 255))
            rows.append(bytes(row))
        raw = b"".join(rows)

        def chunk(tag: bytes, data: bytes) -> bytes:
            payload = tag + data
            return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(raw, 6))
        png += chunk(b"IEND", b"")
        self.media_dir.mkdir(parents=True, exist_ok=True)
        target = self.media_dir / "vision_selftest.png"
        target.write_bytes(png)
        return target

    def _cmd_tools_body(self, chat_id: int, rest: str = "") -> None:
        """（能力层状态见 /cap）"""
        """看一眼工具账本：谁在本地、谁要联网、谁还用不了。"""
        action = (rest or "").strip().lower()
        if action in ("cache", "缓存"):
            stats = self.tool_cache.stats()
            self.send_message(
                chat_id,
                f"工具缓存：{stats['entries']} 条 ｜ 命中 {stats['hits']} ｜ "
                f"未命中 {stats['misses']} ｜ 命中率 {stats['hit_rate']}",
            )
            return
        if action in ("cost", "成本"):
            day = self.tool_cost.day()
            self.send_message(
                chat_id,
                f"工具调用：{day['calls']} 次 ｜ 其中付费 {day['paid_calls']} 次 ｜ "
                f"HTTP {day['http_requests']} 次 ｜ 缓存命中 {day['cache_hits']} ｜ 出错 {day['errors']}",
            )
            return
        report = self.registry.cost_report()
        lines = ["工具层现状（本地优先）："]
        labels = {"LOCAL": "本地 0 API", "FREE_NETWORK": "联网免费", "PAID_API": "付费"}
        for mode, names in report["by_mode"].items():
            if names:
                lines.append(f"· {labels.get(mode, mode)}：{', '.join(names)}")
        if report["unavailable"]:
            lines.append(f"暂时用不了：{', '.join(report['unavailable'])}")
            lines.append("（缺本机依赖：ffmpeg / whisper.cpp / 本地视觉模型 / TTS / 表情包目录）")
        vision = "可用" if self.vision.available() else "不可用（Ollama 没启动或没装视觉模型）"
        lines.append(f"本地视觉：{vision}")
        lines.append("用法：/tools cache 看缓存 ｜ /tools cost 看成本")
        self.send_message(chat_id, "\n".join(lines))

    def cmd_status(self, chat_id: int, _rest: str = "") -> None:
        report = self.model_report or {}
        models = self.client.models
        lines = [
            f"后端：{self.config.get('BACKEND', 'deepseek')}",
            f"模型：cheap={models.get('cheap') or '-'} / main={models.get('main') or '-'} / strong={models.get('strong') or '-'}",
        ]
        if report.get("checked"):
            lines.append("模型校验：已连接 API")
        else:
            lines.append("模型校验：未连上 API（暂不校验）")
        if report.get("fallback"):
            lines.append("降级生效：" + "；".join(f"{k}→{v['using']}" for k, v in report["fallback"].items()))
        lines.append(f"上下文上限：{self.budget.max_context_tokens} tokens ｜ 日预算：{self.budget.daily_token_budget}")
        lines.append(f"对话缓冲：{self.conversation.pending_count(chat_id)} 条待合并")
        stats = self.response_stats.summary()
        profile = self.user_style.profile(chat_id)
        lines.append(
            f"回复统计：已回 {stats['replies']} 次 ｜ 平均 {stats['avg_messages_per_reply']} 条/次"
            f" ｜ 不回 {stats['silences']} 次 ｜ 重试 {stats['retries']} ｜ 兜底 {stats['fallbacks']}"
        )
        lines.append(
            f"你的风格：平均 {profile['average_message_length']} 字"
            f" ｜ 连发 {profile['consecutive_message_count']} 条"
            f" ｜ {profile['punctuation_style']}"
        )
        pro = self.proactive.status()
        lines.append(
            f"主动消息：今天 {pro['sent_today']}/{pro['daily_limit']} ｜ "
            f"静默 {'是' if pro['quiet'] else '否'} ｜ 冷却 {pro['cooldown_until'] or '无'}"
        )
        self.send_message(chat_id, "\n".join(lines))

    # ── 待办 / 提醒 ────────────────────────────────────────────────
    def _todo_groups(self, chat_id: int) -> tuple[list[dict], list[dict]]:
        items = [r for r in self.reminders.all() if r.get("chat_id") == chat_id]
        dated = sorted([r for r in items if r.get("due")], key=lambda r: r.get("due", ""))
        undated = sorted([r for r in items if not r.get("due")], key=lambda r: r.get("id", 0))
        return dated, undated

    def todo_lines(self, chat_id: int, limit: int = 30) -> list[str]:
        dated, undated = self._todo_groups(chat_id)
        lines: list[str] = []
        for item in dated[:limit]:
            repeat = "（每2小时）" if item.get("repeat") == "water" else ""
            lines.append(f"{item.get('id')}. {item.get('due', '')[:16].replace('T', ' ')} {item.get('content', '')}{repeat}")
        room = limit - len(lines)
        if room > 0:
            for item in undated[:room]:
                lines.append(f"{item.get('id')}. {item.get('content', '')}（未定时间）")
        total = len(dated) + len(undated)
        if total > len(lines):
            lines.append(f"……还有 {total - len(lines)} 条")
        return lines

    def cmd_todo(self, chat_id: int, _rest: str = "") -> None:
        lines = self.todo_lines(chat_id)
        self.send_message(chat_id, "待办：\n" + "\n".join(lines) if lines else "清单是空的。")

    def cmd_remind(self, chat_id: int, rest: str) -> None:
        if not rest:
            self.send_message(chat_id, "直接说要记的事，比如「周六晚上买牛奶」。")
            return
        result = parse_reminder(rest)
        if not result or not result.get("when"):
            result = parse_reminder_with_llm(
                rest, lambda prompt: self._ask_cheap(prompt), datetime.datetime.now()
            )
        if not result or not result.get("when"):
            content = (result or {}).get("content") or rest
            item_id = self.reminders.add({"chat_id": chat_id, "due": "", "content": content, "repeat": ""})
            self.send_message(chat_id, f"记下了（{item_id}）：{content}\n没定时间，先放着。")
            return
        when = result["when"]
        item_id = self.reminders.add(
            {
                "chat_id": chat_id,
                "due": when.isoformat(),
                "content": result["content"],
                "repeat": result.get("repeat", ""),
            }
        )
        self.send_message(chat_id, f"记下了（{item_id}）：{when.strftime('%m-%d %H:%M')} {result['content']}")

    def _ask_cheap(self, prompt: str) -> str:
        """用便宜模型做抽取类工作（记忆/时间解析）。"""
        result = self.client.chat(
            [{"role": "user", "content": prompt}], tier="cheap", category="extraction", max_tokens=200
        )
        return result.content if result.ok else ""

    def cmd_done(self, chat_id: int, rest: str) -> None:
        try:
            item_id = int(rest.strip())
        except ValueError:
            self.send_message(chat_id, "用法：/done 编号")
            return
        target = next((r for r in self.reminders.all() if r.get("chat_id") == chat_id and r.get("id") == item_id), None)
        if not target:
            self.send_message(chat_id, f"没找到编号 {item_id}。")
            return
        self.reminders.remove(item_id)
        dated, undated = self._todo_groups(chat_id)
        self.send_message(chat_id, f"{item_id}. {target.get('content', '')} —— 干掉一件，还剩 {len(dated) + len(undated)} 件。")

    def cmd_delremind(self, chat_id: int, rest: str) -> None:
        try:
            item_id = int(rest.strip())
        except ValueError:
            self.send_message(chat_id, "用法：/delremind 编号")
            return
        self.send_message(chat_id, f"删了 {item_id}。" if self.reminders.remove(item_id) else f"没找到编号 {item_id}。")

    def cmd_water(self, chat_id: int, rest: str) -> None:
        action = rest.strip().lower()
        if action not in ("on", "off", "开", "关"):
            self.send_message(chat_id, "用法：/water on 开启循环喝水提醒｜/water off 关闭")
            return
        for item in [r for r in self.reminders.all() if r.get("repeat") == "water" and r.get("chat_id") == chat_id]:
            self.reminders.remove(item.get("id"))
        if action in ("on", "开"):
            self.reminders.add(
                {"chat_id": chat_id, "due": next_water_time().isoformat(), "content": "喝水", "repeat": "water"}
            )
            self.send_message(chat_id, "行，每 2 小时提醒你喝水。")
        else:
            self.send_message(chat_id, "喝水提醒关了。")

    # ── 备忘录 ─────────────────────────────────────────────────────
    def cmd_memo(self, chat_id: int, rest: str) -> None:
        arg = rest.strip()
        if not arg:
            self.send_message(chat_id, "用法：/memo 内容 ｜ /memos 看全部 ｜ /memo 编号 看全文。")
            return
        if arg.isdigit():
            item = self.memos.get(chat_id, int(arg))
            if not item:
                self.send_message(chat_id, f"没找到编号 {arg} 的备忘。")
                return
            self.send_message(chat_id, f"{item['id']}. {item['text']}")
            return
        item = self.memos.add(chat_id, arg)
        self.send_message(chat_id, f"记进备忘录了（{item['id']}）：{item['text']}")

    def cmd_memos(self, chat_id: int, _rest: str = "") -> None:
        items = sorted(self.memos.for_chat(chat_id), key=lambda m: m.get("id", 0))
        if not items:
            self.send_message(chat_id, "备忘录是空的。")
            return
        lines = [f"{m['id']}. {str(m.get('text', '')).replace(chr(10), ' ')[:40]}" for m in items[:30]]
        self.send_message(chat_id, "备忘录：\n" + "\n".join(lines))

    def cmd_delmemo(self, chat_id: int, rest: str) -> None:
        try:
            memo_id = int(rest.strip())
        except ValueError:
            self.send_message(chat_id, "用法：/delmemo 编号")
            return
        target = self.memos.remove(memo_id)
        self.send_message(chat_id, f"删了：{str(target.get('text', ''))[:40]}" if target else f"没找到编号 {memo_id}。")

    # ── 搜索 / 自启 ────────────────────────────────────────────────
    def cmd_search(self, chat_id: int, rest: str) -> None:
        from tools import search

        if not self.search_api_key:
            self.send_message(chat_id, "还没配搜索 API。")
            return
        if not rest:
            self.send_message(chat_id, "用法：/search 关键词")
            return
        try:
            results = search.web_search(rest, self.search_api_key, self.search_api_url)
        except Exception as exc:  # noqa: BLE001
            self.send_message(chat_id, f"搜索失败了……{exc}")
            return
        if not results:
            self.send_message(chat_id, "没查到什么有用的。")
            return
        self.send_message(chat_id, results[:1500])

    def cmd_autostart(self, chat_id: int, rest: str) -> None:
        action = rest.strip().lower()
        vbs = self._autostart_path()
        if action in ("on", "开", "1", "true"):
            if os.name != "nt":
                self.send_message(chat_id, "开机自启仅支持 Windows。")
                return
            vbs.parent.mkdir(parents=True, exist_ok=True)
            inner = f'"{sys.executable}" "{BASE_DIR / "bot.py"}"'
            vbs.write_text(
                f'CreateObject("WScript.Shell").Run "{inner.replace(chr(34), chr(34) * 2)}", 0, False\n',
                encoding="ascii",
            )
            self.send_message(chat_id, "开机自启开好了，以后一开机我就在。")
        elif action in ("off", "关", "0", "false"):
            if vbs.exists():
                vbs.unlink()
                self.send_message(chat_id, "开机自启已关闭。")
            else:
                self.send_message(chat_id, "本来就没开。")
        else:
            self.send_message(chat_id, "用法：/autostart on｜/autostart off")

    def _autostart_path(self) -> Path:
        return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "xiyan_v2_bot.vbs"

    # ── 提醒到点（模板直发，不调用模型）──────────────────────────
    def send_reminder(self, reminder: dict) -> None:
        chat_id = reminder.get("chat_id")
        content = reminder.get("content", "提醒")
        if not chat_id:
            return
        prefix = "喝水时间到。" if reminder.get("repeat") == "water" else "提醒你："
        self.send_message(chat_id, f"{prefix}{content}")
        self.usage.record_local("rule", estimate_tokens(content), note="提醒模板直发")

    # ── 轮询 ───────────────────────────────────────────────────────
    def wait_for_network(self, timeout: float = 180.0, interval: float = 5.0) -> bool:
        """开机后网络/代理往往还没起来：先等到能连上 Telegram 再开始轮询。

        不这样做的话，开机那几分钟会一直报"目标计算机积极拒绝"，用户以为机器人没启动。
        """
        deadline = time.time() + max(0.0, float(timeout))
        attempt = 0
        while True:
            attempt += 1
            try:
                self.tg_call("getMe", {}, timeout=15, retries=1)
                if attempt > 1:
                    logging.info("网络已恢复，第 %d 次尝试连上 Telegram", attempt)
                return True
            except Exception as exc:  # noqa: BLE001
                if time.time() >= deadline:
                    logging.warning("等了 %.0f 秒还是连不上 Telegram，先照常启动：%s", timeout, exc)
                    return False
                logging.info("网络还没就绪（第 %d 次）：%s，%.0f 秒后重试", attempt, str(exc)[:80], interval)
                time.sleep(interval)

    def poll(self) -> None:
        offset = 0
        logging.info(
            "夕颜 V2 Phase1 启动：backend=%s models=%s", self.config.get("BACKEND", "deepseek"), self.client.models
        )
        while True:
            try:
                updates = self.tg_call(
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
                    except Exception as exc:  # noqa: BLE001
                        logging.error("handle failed: %s", exc)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                logging.error("poll error: %s", exc)
                time.sleep(3)


def main() -> int:
    parser = argparse.ArgumentParser(description="夕颜 V2 Phase1")
    parser.add_argument("--config", default=str(BASE_DIR / "config.env"))
    parser.add_argument("--self-test", action="store_true", help="只验证配置与模型可用性")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8")],
    )
    # 第一时间留下代码指纹（早于任何可能失败的步骤）
    log_build()
    config = Config(Path(args.config))
    proxy = config.get("HTTPS_PROXY")
    if proxy:
        os.environ.setdefault("HTTPS_PROXY", proxy)
        os.environ.setdefault("HTTP_PROXY", proxy)
    if not config.get("TELEGRAM_BOT_TOKEN"):
        logging.error("缺少 TELEGRAM_BOT_TOKEN（config.env 或环境变量）")
        return 1
    bot = Bot(config)
    log_build(handlers=True)
    # 唯一实例保护：绝不允许悄悄跑起第二个进程
    lock = SingleInstance(DATA_DIR / "bot.lock")
    ok, existing = lock.acquire()
    if not ok:
        logging.error(
            "[instance] 已经有一个夕颜在运行：pid=%s started_at=%s path=%s",
            existing.get("pid"), existing.get("started_at"), existing.get("path"),
        )
        logging.error("[instance] 本次启动被拒绝（不会开第二个实例）。要重启请先停掉旧进程，或用 启动夕颜.cmd。")
        print(f"[instance] 已有实例在运行 pid={existing.get('pid')}，本次启动退出。")
        return 2
    # 启动健康检查：只查路径在不在，不强行拉起感知服务
    try:
        report = bot.registry.cost_report()
        logging.info("[startup] 工具可用性：共 %d 个，缺少依赖的：%s",
                     report["total"], "、".join(report["unavailable"]) or "无")
        logging.info("[startup] 本地视觉：%s（按需启动，不预热）",
                     "已就绪" if bot.vision.health_check() else "待首次使用时启动")
    except Exception as exc:  # noqa: BLE001
        logging.warning("[startup] 健康检查失败：%s", exc)
    for tier, model in bot.client.models.items():
        logging.info("模型档位 %s = %s", tier, model or "（未配置）")
    if bot.model_report.get("fallback"):
        logging.warning("模型降级：%s", bot.model_report["fallback"])
    if args.self_test:
        print(json.dumps({"models": bot.client.models, "report": bot.model_report}, ensure_ascii=False, indent=2))
        return 0
    reminder_thread = ReminderThread(bot.reminders, bot)
    reminder_thread.start()
    bot.wait_for_network(timeout=float(config.get_int("STARTUP_NETWORK_WAIT_SECONDS", 180)))
    # 后台预热本地视觉服务：开机后第一张图不用等 Ollama 冷启动
    # 默认不预热：只有显式打开 PERCEPTION_WARMUP=true 才会在开机时拉起本地视觉
    if config.get_bool("VISION_ENABLED", True) and config.get_bool("PERCEPTION_WARMUP", False):
        threading.Thread(
            target=lambda: (logging.info("预热本地视觉：%s", bot.vision.ensure_running() and "就绪" or "不可用"),
                            logging.info("视觉模型：%s", bot.vision.resolve_model() or "（没有可用视觉模型）")),
            name="vision-prewarm", daemon=True,
        ).start()
    if bot.proactive.enabled:
        bot.proactive.start()
        logging.info(
            "主动消息已启动：每 %.0f 分钟检查一次，静默 %s~%s，每天最多 %d 次",
            bot.proactive.tick_minutes, bot.config.get("PROACTIVE_QUIET_START", "23:30"),
            bot.config.get("PROACTIVE_QUIET_END", "08:00"), bot.proactive.daily_limit,
        )
    try:
        bot.poll()
    except KeyboardInterrupt:
        logging.info("stopped by user")
    finally:
        bot.proactive.stop()
        try:
            lock.release()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
