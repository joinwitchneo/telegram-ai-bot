"""夕颜 · 图形启动器

一个不依赖第三方库的小窗口：填配置 → 测试连接 → 一键启动/停止机器人。
打包成 exe 后可以自己当"运行器"用（--run-bot），所以用户机器上不需要装 Python。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

APP_TITLE = "夕颜 · 启动器"
FIELDS = [
    # (key, 标签, 是否密码, 说明)
    ("TELEGRAM_BOT_TOKEN", "Telegram Bot Token", True, "找 @BotFather 发 /newbot 获取"),
    ("DEEPSEEK_API_KEY", "DeepSeek API Key", True, "platform.deepseek.com 创建"),
    ("DEEPSEEK_MODEL", "模型名", False, "默认 deepseek-v4-flash"),
    ("PROACTIVE_CHAT_ID", "主动消息发给谁（你的 TG ID）", False, "点右边按钮可自动获取"),
    ("CITY", "天气城市", False, "例如 香港 / Beijing"),
    ("HTTPS_PROXY", "本机代理（可不填）", False, "走路由器代理时留空，例如 http://127.0.0.1:7890"),
    ("SEARCH_API_KEY", "搜索 API Key（可不填）", False, "Tavily，可选功能"),
]


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = base_dir()
CONFIG = BASE / "config.env"
LOG = BASE / "bot.log"


class Config:
    """读写 config.env：保留原有注释和未知键，只更新界面上的那几个。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.values: dict[str, str] = {}
        self.lines: list[str] = []
        self.load()

    def load(self) -> None:
        self.values = {}
        if self.path.is_file():
            self.lines = self.path.read_text(encoding="utf-8", errors="ignore").splitlines()
        else:
            example = BASE / "config.example.env"
            self.lines = (
                example.read_text(encoding="utf-8", errors="ignore").splitlines()
                if example.is_file()
                else []
            )
        for line in self.lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            self.values[key.strip()] = value.strip().strip('"').strip("'")

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def save(self) -> None:
        lines: list[str] = []
        seen: set[str] = set()
        for line in self.lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in self.values:
                    lines.append(f"{key}={self.values[key]}")
                    seen.add(key)
                    continue
            lines.append(line)
        for key, value in self.values.items():
            if key not in seen:
                lines.append(f"{key}={value}")
        self.path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def http_json(url: str, timeout: int = 15, proxy: str = "") -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "xiyan-launcher/1.0"})
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


class Launcher:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config = Config(CONFIG)
        self.process: subprocess.Popen | None = None
        self.entries: dict[str, ttk.Entry] = {}
        root.title(APP_TITLE)
        root.geometry("720x620")
        root.minsize(680, 560)
        self._build()
        self._refresh_status()
        self._tail_log()

    # ── 界面 ────────────────────────────────────────────────────────
    def _build(self) -> None:
        pad = {"padx": 10, "pady": 6}
        title = ttk.Label(self.root, text="夕颜 · 启动器", font=("Microsoft YaHei UI", 16, "bold"))
        title.pack(anchor="w", **pad)
        ttk.Label(
            self.root,
            text="填写下面两项必填内容，点测试，再点启动就行。配置文件保存在同目录的 config.env。",
            foreground="#555",
        ).pack(anchor="w", padx=10)

        form = ttk.Frame(self.root)
        form.pack(fill="x", **pad)
        form.columnconfigure(1, weight=1)
        for row, (key, label, secret, hint) in enumerate(FIELDS):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(form, show="•" if secret else "")
            entry.insert(0, self.config.get(key))
            entry.grid(row=row, column=1, sticky="ew", padx=6)
            self.entries[key] = entry
            if key == "PROACTIVE_CHAT_ID":
                ttk.Button(form, text="自动获取", width=9, command=self.detect_chat_id).grid(
                    row=row, column=2, padx=4
                )
            else:
                ttk.Label(form, text=hint, foreground="#888").grid(row=row, column=2, sticky="w")

        show_secrets = tk.BooleanVar(value=False)

        def toggle() -> None:
            state = "" if show_secrets.get() else "•"
            for key, _label, secret, _hint in FIELDS:
                if secret:
                    self.entries[key].configure(show=state)

        ttk.Checkbutton(self.root, text="显示密钥", variable=show_secrets, command=toggle).pack(
            anchor="w", padx=10
        )

        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", **pad)
        ttk.Button(buttons, text="保存配置", command=self.save_config).pack(side="left", padx=4)
        ttk.Button(buttons, text="测试连接", command=self.test_connection).pack(side="left", padx=4)
        ttk.Button(buttons, text="▶ 启动机器人", command=self.start_bot).pack(side="left", padx=12)
        ttk.Button(buttons, text="■ 停止", command=self.stop_bot).pack(side="left", padx=4)
        ttk.Button(buttons, text="打开日志", command=self.open_log).pack(side="left", padx=12)

        self.status = ttk.Label(self.root, text="状态：未运行", font=("Microsoft YaHei UI", 10, "bold"))
        self.status.pack(anchor="w", padx=10, pady=(0, 4))

        log_frame = ttk.LabelFrame(self.root, text="运行日志")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text = tk.Text(log_frame, height=12, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, side="left")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")

    # ── 配置 ────────────────────────────────────────────────────────
    def collect(self) -> None:
        for key, entry in self.entries.items():
            self.config.set(key, entry.get().strip())

    def save_config(self) -> None:
        self.collect()
        try:
            self.config.save()
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"保存失败：{exc}")
            return
        messagebox.showinfo(APP_TITLE, f"已保存到：\n{CONFIG}")

    # ── 测试 ────────────────────────────────────────────────────────
    def test_connection(self) -> None:
        self.collect()
        proxy = self.config.get("HTTPS_PROXY")
        token = self.config.get("TELEGRAM_BOT_TOKEN")
        key = self.config.get("DEEPSEEK_API_KEY")
        if not token:
            messagebox.showwarning(APP_TITLE, "请先填写 Telegram Bot Token")
            return
        self._append_log("开始测试连接……")

        def worker() -> None:
            try:
                me = http_json(f"https://api.telegram.org/bot{token}/getMe", proxy=proxy)
                name = me.get("result", {}).get("username", "?")
                self._append_log(f"Telegram 连接正常：@{name}")
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"Telegram 连接失败：{exc}")
                return
            if key:
                try:
                    request = urllib.request.Request(
                        "https://api.deepseek.com/v1/models",
                        headers={"Authorization": f"Bearer {key}"},
                    )
                    urllib.request.urlopen(request, timeout=15)
                    self._append_log("DeepSeek 连接正常")
                except urllib.error.HTTPError as exc:
                    self._append_log(f"DeepSeek 返回 {exc.code}（401 表示 Key 不对）")
                except Exception as exc:  # noqa: BLE001
                    self._append_log(f"DeepSeek 连接失败：{exc}")

        threading.Thread(target=worker, daemon=True).start()

    def detect_chat_id(self) -> None:
        """让你先给机器人发一条消息，然后这里自动读出你的 ID。"""
        self.collect()
        token = self.config.get("TELEGRAM_BOT_TOKEN")
        proxy = self.config.get("HTTPS_PROXY")
        if not token:
            messagebox.showwarning(APP_TITLE, "请先填写 Telegram Bot Token")
            return
        self._append_log("请在 20 秒内给机器人发一条消息（比如“你好”）……")

        def worker() -> None:
            deadline = time.time() + 20
            offset = 0
            while time.time() < deadline:
                try:
                    data = http_json(
                        f"https://api.telegram.org/bot{token}/getUpdates?timeout=10&offset={offset}",
                        timeout=20,
                        proxy=proxy,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._append_log(f"获取失败：{exc}")
                    return
                for update in data.get("result", []):
                    offset = max(offset, int(update.get("update_id", 0)) + 1)
                    chat = (update.get("message") or {}).get("chat") or {}
                    if chat.get("id"):
                        chat_id = str(chat["id"])
                        self.entries["PROACTIVE_CHAT_ID"].delete(0, "end")
                        self.entries["PROACTIVE_CHAT_ID"].insert(0, chat_id)
                        self._append_log(f"拿到你的 TG ID：{chat_id}（记得点保存配置）")
                        return
            self._append_log("没收到消息，稍后再试一次。")

        threading.Thread(target=worker, daemon=True).start()

    # ── 启动 / 停止 ────────────────────────────────────────────────
    def _command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--run-bot"]
        return [sys.executable, str(BASE / "bot.py")]

    def start_bot(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showinfo(APP_TITLE, "机器人已经在运行了")
            return
        external = self._external_pids()
        if external:
            if not messagebox.askyesno(
                APP_TITLE,
                "检测到机器人已经在运行（可能是开机自启拉起来的）。\n是否先停掉它再重新启动？",
            ):
                return
            self.stop_bot()
            time.sleep(1.5)
        self.collect()
        try:
            self.config.save()
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"保存配置失败：{exc}")
            return
        creation = 0
        if os.name == "nt":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log_file = open(LOG, "a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                self._command(),
                cwd=str(BASE),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creation,
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"启动失败：{exc}")
            return
        self._append_log(f"机器人已启动（PID {self.process.pid}）。日志会实时显示在下面。")
        self._refresh_status()

    def stop_bot(self) -> None:
        stopped = False
        if self.process and self.process.poll() is None:
            self.process.terminate()
            stopped = True
        # 也可能有上次遗留的进程：按命令行匹配收掉
        try:
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='夕颜启动器.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*--run-bot*' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            stopped = stopped or bool(result.stdout.strip())
        except Exception:  # noqa: BLE001
            pass
        self._append_log("已停止机器人" if stopped else "没有正在运行的机器人")
        self._refresh_status()

    def _refresh_status(self) -> None:
        running = bool(self.process and self.process.poll() is None)
        if running:
            text, color = "状态：运行中（本窗口启动）", "#1a7f37"
        elif self._external_pids():
            text, color = "状态：运行中（已在别处启动）", "#8a6d00"
        else:
            text, color = "状态：未运行", "#a12020"
        self.status.configure(text=text, foreground=color)
        self.root.after(3000, self._refresh_status)

    def _external_pids(self) -> list[int]:
        """找出不是本窗口启动的机器人进程（比如开机自启拉起来的）。"""
        try:
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or "
                    "Name='夕颜启动器.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*--run-bot*' } | "
                    "Select-Object -ExpandProperty ProcessId",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return [int(line) for line in result.stdout.split() if line.strip().isdigit()]
        except Exception:  # noqa: BLE001
            return []

    # ── 日志 ────────────────────────────────────────────────────────
    def _append_log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _tail_log(self) -> None:
        """把 bot.log 的末尾同步到窗口里。"""
        try:
            if LOG.is_file():
                lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-120:]
                text = "\n".join(lines)
                if text != getattr(self, "_log_cache", ""):
                    self._log_cache = text
                    self.log_text.configure(state="normal")
                    self.log_text.delete("1.0", "end")
                    self.log_text.insert("end", text + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass
        self.root.after(2000, self._tail_log)

    def open_log(self) -> None:
        if not LOG.is_file():
            messagebox.showinfo(APP_TITLE, "还没有日志文件")
            return
        try:
            if os.name == "nt":
                os.startfile(LOG)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(LOG)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"打不开日志：{exc}")


def main() -> int:
    # 被启动器以 --run-bot 调起时，这个进程就跑机器人本体
    if "--run-bot" in sys.argv:
        # 打包成 --windowed 之后没有控制台，stdout/stderr 是 None，
        # 日志系统会因此报错，这里先给它们接上一个文件。
        if sys.stdout is None or sys.stderr is None:
            try:
                stream = open(BASE / "launcher-run.log", "a", encoding="utf-8", buffering=1)
            except OSError:
                stream = open(os.devnull, "w", encoding="utf-8")
            sys.stdout = sys.stdout or stream
            sys.stderr = sys.stderr or stream
        # 关键：把启动器自己的参数摘掉，否则会让 bot 的参数解析失败
        sys.argv = [arg for arg in sys.argv if arg != "--run-bot"]
        import bot

        return bot.main()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    Launcher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
