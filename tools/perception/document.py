"""文档解析：全部本地，0 API。

标准库就能搞定的：txt / md / csv / json / 代码
用 zip+xml 解析：docx（docx 本质是个 zip）
尽力而为：pdf（用 zlib 解 FlateDecode 抽文本，复杂的扫描版直接说读不了）
"""

from __future__ import annotations

import json
import re
import zipfile
import zlib
from pathlib import Path

from tools.core.result import ToolResult

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh",
    ".html", ".htm", ".xml", ".sql",
}
MAX_CHARS = 4000

TAG_RE = re.compile(r"<[^>]+>")
PDF_TEXT_RE = re.compile(rb"\((?:\\.|[^\\()])*\)")


def _read_text(path: Path) -> str:
    raw = path.read_bytes()[: MAX_CHARS * 4]
    for encoding in ("utf-8", "utf-16", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"docx 解析失败：{exc}") from exc
    xml = xml.replace("</w:p>", "\n")
    text = TAG_RE.sub("", xml)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_pdf(path: Path) -> str:
    """只处理文本型 PDF：解压内容流后抓括号里的字符串。扫描版会返回空。"""
    raw = path.read_bytes()
    chunks: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        blob = match.group(1)
        try:
            blob = zlib.decompress(blob)
        except zlib.error:
            continue
        for piece in PDF_TEXT_RE.findall(blob):
            try:
                chunks.append(piece[1:-1].decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
    text = " ".join(chunks).strip()
    text = text.replace("\\(", "(").replace("\\)", ")")
    return re.sub(r"\s{2,}", " ", text)


def parse(path: str | Path) -> ToolResult:
    file_path = Path(path)
    if not file_path.is_file():
        return ToolResult.failure("document", "文件不存在")
    suffix = file_path.suffix.lower()
    try:
        if suffix in TEXT_EXTENSIONS:
            text = _read_text(file_path)
        elif suffix == ".docx":
            text = _read_docx(file_path)
        elif suffix == ".pdf":
            text = _read_pdf(file_path)
            if len(text) < 20:
                return ToolResult.failure("document", "这个 PDF 读不出文字（可能是扫描件），需要 OCR")
        else:
            return ToolResult.failure("document", f"暂时不支持这种文件：{suffix or '未知类型'}")
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure("document", f"解析失败：{exc}")
    text = text.strip()
    if not text:
        return ToolResult.failure("document", "文件里没有可读的文字")
    return ToolResult(
        name="document",
        ok=True,
        text=f"文件《{file_path.name}》的内容：\n{text[:MAX_CHARS]}",
        data={
            "text": text[:MAX_CHARS],
            "filename": file_path.name,
            "suffix": suffix,
            "chars": len(text),
            "truncated": len(text) > MAX_CHARS,
            "source_type": "document",
        },
        source_type="document",
        confidence=0.9,
    )
