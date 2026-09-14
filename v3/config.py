"""V3 运行时配置：唯一事实源。

优先级链（任何模块都不许绕过这里去读 env 或 json）：
    V3_ENABLED（总开关，config.env）
        ↓
    config.env: V3_MODE（基础档）
        ↓
    data/v3/runtime.json: mode（运行时覆盖，/mode 只写这里）
        ↓
    RuntimeConfig（生效值）
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

from core.atomic_io import atomic_write_text

# 规范档位只有三个；active 是 live 的别名（6.14.4），内部一律归一到 live
MODES = ("observe", "dry_run", "live")
MODE_ALIASES = {"active": "live"}


def normalize_mode(value) -> str:
    """把用户写的档位归一成规范档位；不认识的一律返回空串。"""
    text = str(value or "").strip().lower()
    text = MODE_ALIASES.get(text, text)
    return text if text in MODES else ""


class RuntimeConfig:
    def __init__(self, config, *, base_dir: Path) -> None:
        self._config = config
        self.base_dir = Path(base_dir)
        data_dir = config.get("V3_DATA_DIR", "data/v3").strip() or "data/v3"
        self.data_dir = Path(data_dir)
        if not self.data_dir.is_absolute():
            self.data_dir = self.base_dir / self.data_dir
        self.runtime_path = self.data_dir / "runtime.json"
        self._lock = threading.RLock()

    # ── 开关与档位 ──────────────────────────────────────────────────
    def enabled(self) -> bool:
        return bool(self._config.get_bool("V3_ENABLED", False))

    def base_mode(self) -> str:
        return normalize_mode(self._config.get("V3_MODE", "observe")) or "observe"

    def runtime_override(self) -> str:
        try:
            data = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return normalize_mode((data or {}).get("mode", ""))

    def mode(self) -> str:
        """生效档位：运行时覆盖 > 基础档。"""
        if not self.enabled():
            return "observe"
        return self.runtime_override() or self.base_mode()

    def set_runtime_mode(self, mode: str, *, actor: str = "") -> bool:
        """只允许写 runtime.json；写完立即生效（下次 mode() 调用即可读到）。"""
        value = normalize_mode(mode)
        if not value:
            return False
        payload = {
            "mode": value,
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "updated_by": actor[:40],
        }
        with self._lock:
            try:
                atomic_write_text(self.runtime_path, json.dumps(payload, ensure_ascii=False, indent=1))
            except OSError as exc:
                logging.warning("[v3] 写 runtime.json 失败：%s", exc)
                return False
        logging.info("[v3] 运行时档位切换为 %s（by %s）", value, actor or "unknown")
        return True

    def mode_report(self) -> dict:
        return {
            "enabled": self.enabled(),
            "base_mode": self.base_mode(),
            "runtime_override": self.runtime_override(),
            "effective_mode": self.mode(),
        }

    def actions_allowed(self) -> bool:
        """MVP：只有 live 档才允许真实执行 Capability/Action（本轮无 Action）。"""
        return self.mode() == "live"

    # ── 数值上限（LLM 不可改）───────────────────────────────────────
    def limit(self, name: str, default) -> float:
        return self._config.get_float(name, float(default))

    def limit_int(self, name: str, default: int) -> int:
        return self._config.get_int(name, int(default))

    def paths(self) -> dict:
        return {
            "data": self.data_dir,
            "continuity": self.data_dir / "continuity",
            "thoughts": self.data_dir / "thoughts",
            "interests": self.data_dir / "interests",
            "motivation": self.data_dir / "motivation",
            "experiences": self.data_dir / "experiences",
            "journal": self.data_dir / "inner_journal",
            "budget": self.data_dir / "budget.json",
            "observations": self.data_dir / "observations.jsonl",
        }

    def ensure_dirs(self) -> None:
        for path in self.paths().values():
            if path.suffix:
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                path.mkdir(parents=True, exist_ok=True)
