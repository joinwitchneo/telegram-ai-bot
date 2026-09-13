"""图片：本地读取尺寸与格式（标准库解析文件头），不需要任何外部依赖。"""

from __future__ import annotations

import struct
from pathlib import Path

from tools.core.result import ToolResult


def _png_size(head: bytes) -> tuple[int, int] | None:
    if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", head[16:24])
    return None


def _gif_size(head: bytes) -> tuple[int, int] | None:
    if len(head) >= 10 and head[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", head[6:10])
    return None


def _bmp_size(head: bytes) -> tuple[int, int] | None:
    if len(head) >= 26 and head[:2] == b"BM":
        return struct.unpack("<ii", head[18:26])
    return None


def _jpeg_size(head: bytes) -> tuple[int, int] | None:
    if len(head) < 4 or head[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(head):
        if head[index] != 0xFF:
            index += 1
            continue
        marker = head[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            height, width = struct.unpack(">HH", head[index + 5 : index + 9])
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > len(head):
            break
        length = struct.unpack(">H", head[index + 2 : index + 4])[0]
        index += 2 + max(2, length)
    return None


def inspect(path: str | Path) -> ToolResult:
    file_path = Path(path)
    if not file_path.is_file():
        return ToolResult.failure("image", "图片文件不存在")
    head = file_path.read_bytes()[:65536]
    size = _png_size(head) or _jpeg_size(head) or _gif_size(head) or _bmp_size(head)
    kind = "unknown"
    for name, magic in (("png", b"\x89PNG"), ("jpeg", b"\xff\xd8"), ("gif", b"GIF8"), ("bmp", b"BM"), ("webp", b"RIFF")):
        if head.startswith(magic):
            kind = name
            break
    metadata = {
        "format": kind,
        "bytes": file_path.stat().st_size,
        "width": size[0] if size else 0,
        "height": size[1] if size else 0,
    }
    if size:
        width, height = size
        describe = f"一张 {width}×{height} 的{kind}图片"
    else:
        describe = f"一张{kind}图片（{metadata['bytes']} 字节）"
    return ToolResult(
        name="image", ok=True, text=describe, data={"metadata": metadata, "path": str(file_path)},
        source_type="image", confidence=1.0,
    )
