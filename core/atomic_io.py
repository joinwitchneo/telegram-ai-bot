"""原子写入：先写临时文件再 replace，避免进程被杀时 JSON 半写导致下次启动损坏。

只做这一件事，不改任何业务逻辑。所有状态存储（记忆/情绪/关系/自我模型/主动状态/
角色生活/用户风格/统计）都通过它落盘。
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)      # Windows/POSIX 上都是原子的替换
        tmp_name = ""
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
