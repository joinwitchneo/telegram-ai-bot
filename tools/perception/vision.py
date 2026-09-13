"""本地视觉：走 Ollama 的 HTTP API（0 AI API 成本，图片不出本机）。

Ollama 没启动 / 没装视觉模型时，明确返回"不可用"，由上层决定怎么跟用户说。
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import subprocess
import time
from pathlib import Path

from tools.core.result import ToolResult
from tools.world import http

# 名字写法太乱（qwen2.5vl / qwen2.5-vl / llava / moondream …），统一归一化后再比
VISION_HINTS = ("llava", "vision", "moondream", "minicpmv", "vl")


def normalize_model_name(name: str) -> str:
    """去掉 tag 与分隔符，只留字母数字，方便匹配（qwen2.5vl:3b -> qwen25vl）。"""
    base = str(name or "").split(":")[0].lower()
    return "".join(char for char in base if char.isalnum())

DEFAULT_PROMPT = (
    "用中文简短描述这张图片：主体是什么、在做什么、画面里有没有文字。"
    "只描述你确定看到的内容，不要猜。控制在 80 字内。"
)


CREATE_NO_WINDOW = 0x08000000

# 最近一次调用的遥测，供 /vision 诊断直接展示（不进 LLM Context）
LAST_CALL: dict = {}


def record_call(**fields) -> None:
    LAST_CALL.update(fields)


def last_call() -> dict:
    return dict(LAST_CALL)


class OllamaVision:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "",
        *,
        enabled: bool = True,
        exe_path: str = "",
        autostart: bool = True,
        start_timeout: float = 90.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.enabled = bool(enabled)
        self.exe_path = str(exe_path or "")
        self.autostart = bool(autostart)
        self.start_timeout = max(5.0, float(start_timeout))
        self._model_cache: str | None = None
        self._start_attempted_at = 0.0

    # ── 探测 ────────────────────────────────────────────────────────
    def list_models(self) -> list[str]:
        if not self.base_url:
            return []
        try:
            data = http.get_json(f"{self.base_url}/api/tags", timeout=3.0)
        except Exception as exc:  # noqa: BLE001 - 没启动就是没启动
            logging.info("本地视觉不可用（Ollama 未响应）：%s", exc)
            logging.info("[vision] ollama health=False error=%s", str(exc)[:120])
            return []
        names = [str(item.get("name", "")) for item in (data.get("models") or [])]
        logging.info("[vision] ollama health=True models=%d", len(names))
        return names

    def resolve_model(self) -> str:
        models = self.list_models()
        # 只缓存"找到了"的结果；缓存失效（模型被删/换）或为空时都重新探测
        if self._model_cache:
            if self._model_cache in models:
                return self._model_cache
            logging.info("[vision] 缓存的模型已不在列表里（%s），重新探测", self._model_cache)
            self._model_cache = None
        if self.model and any(item.startswith(self.model) for item in models):
            self._model_cache = next(item for item in models if item.startswith(self.model))
            return self._model_cache
        if self.model:
            logging.warning("[vision] 配置的 VISION_MODEL=%s 不在本机模型列表里", self.model)
        for item in models:
            normalized = normalize_model_name(item)
            if any(hint in normalized for hint in VISION_HINTS):
                self._model_cache = item
                return item
        # 故意不缓存失败结果：Ollama 后来起来了必须能重新发现
        if models:
            logging.warning("[vision] 本机模型里没有视觉模型：%s", ", ".join(models)[:120])
        return ""

    def ensure_running(self) -> bool:
        """Ollama 没起来就自己拉起来（开机后它常常不在，视觉就全废了）。"""
        if not self.enabled:
            return False
        if self.list_models():
            return True
        if not self.autostart or not self.exe_path or not Path(self.exe_path).is_file():
            return False
        # 60 秒内只尝试拉起一次，避免每条消息都去 spawn
        now = time.time()
        if now - self._start_attempted_at < 60:
            return False
        self._start_attempted_at = now
        try:
            subprocess.Popen(
                [self.exe_path, "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            logging.info("尝试拉起本地视觉服务：%s serve", self.exe_path)
        except Exception as exc:  # noqa: BLE001
            logging.warning("[vision] ensure_running failed: %s: %s", type(exc).__name__, exc)
            return False
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            if self.list_models():
                logging.info("本地视觉服务已就绪")
                return True
            time.sleep(2.0)
        logging.warning("等了 %.0f 秒，本地视觉服务仍未就绪", self.start_timeout)
        logging.warning("[vision] ensure_running failed: 服务未在 %.0f 秒内就绪", self.start_timeout)
        return False

    def available(self) -> bool:
        if not self.enabled:
            return False
        if not self.list_models():
            logging.info("[vision] ensure_running 尝试拉起本地服务")
            self.ensure_running()
        resolved = bool(self.resolve_model())
        logging.info("[vision] available=%s model=%s cache=%r", resolved, self.resolve_model() or "-",
                     self._model_cache)
        return resolved

    def health_check(self) -> bool:
        """被动健康检查：只问"现在能不能用"，绝不拉起服务（启动时用这个）。"""
        if not self.enabled:
            return False
        models = self.list_models()
        if not models:
            return False
        return bool(self.resolve_model())

    # ── 推理 ────────────────────────────────────────────────────────
    def describe(self, image_path: str | Path, *, prompt: str = "") -> ToolResult:
        if not self.enabled:
            return ToolResult.failure("vision", "本地视觉被关闭")
        model = self.resolve_model()
        path = Path(image_path)
        logging.info("[vision] execute start model=%s path=%s", model or "-", path)
        if not model:
            logging.warning("[vision] request failed error=没有可用的视觉模型")
            record_call(ok=False, error="没有可用的视觉模型", model="", path=str(path))
            return ToolResult.failure("vision", "本机没有可用的视觉模型（Ollama 未启动或没装）")
        if not path.is_file():
            logging.warning("[vision] request failed error=图片文件不存在 path=%s", path)
            record_call(ok=False, error="图片文件不存在", model=model, path=str(path))
            return ToolResult.failure("vision", "图片文件不存在")
        logging.info("[vision] request start bytes=%d", path.stat().st_size)
        payload = {
            "model": model,
            "prompt": prompt or DEFAULT_PROMPT,
            "images": [base64.b64encode(path.read_bytes()).decode("ascii")],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        started = time.time()
        try:
            raw = http.post_bytes(
                f"{self.base_url}/api/generate",
                json.dumps(payload).encode("utf-8"),
                timeout=180.0,
            )
            data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception as exc:  # noqa: BLE001 - 原始错误必须保留
            logging.warning("[vision] request failed error=%s: %s", type(exc).__name__, exc)
            record_call(ok=False, error=f"{type(exc).__name__}: {exc}", model=model, path=str(path))
            return ToolResult.failure("vision", f"{type(exc).__name__}: {exc}")
        elapsed = int((time.time() - started) * 1000)
        text = str(data.get("response", "")).strip()
        if not text:
            logging.warning("[vision] request failed error=模型返回空内容 raw=%s", str(data)[:120])
            record_call(ok=False, error="模型返回空内容", model=model, path=str(path))
            return ToolResult.failure("vision", "本地视觉没有返回内容")
        logging.info("[vision] request success latency_ms=%d", elapsed)
        logging.info("[vision] result=%s", text[:160])
        record_call(
            ok=True, error="", model=model, path=str(path), text=text,
            latency_ms=elapsed, at=datetime.datetime.now().isoformat(timespec="seconds"),
        )
        return ToolResult(
            name="vision", ok=True, text=text,
            data={"model": model, "source_type": "image", "raw": text},
            source_type="image", confidence=0.7,
        )
