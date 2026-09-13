"""OCR：优先本机免费方案，绝不上云。

两条本地路线：
1. tesseract（装了就用，支持中文最好）
2. Windows 自带 OCR（Windows.Media.Ocr，Win10/11 自带，不需要装东西）
两个都没有 -> 明确返回不可用，由上层决定怎么跟用户说（通常是让本地视觉模型去读）。
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

from tools.core.result import ToolResult
from tools.core.schema import which

WINDOWS_OCR_SCRIPT = r"""
param([string]$Path)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language 'en-US')) }
if ($null -eq $engine) { Write-Output ""; exit 0 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) { Write-Output $line.Text }
"""


def tesseract_available() -> str:
    return which("tesseract")


def windows_ocr_available() -> bool:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null; 'ok'"],
            capture_output=True, timeout=25, text=True,
        )
        return "ok" in (result.stdout or "")
    except Exception as exc:  # noqa: BLE001
        logging.info("Windows OCR 不可用：%s", exc)
        return False


def _run_tesseract(path: Path, lang: str = "chi_sim+eng") -> ToolResult:
    try:
        result = subprocess.run(
            [tesseract_available(), str(path), "stdout", "-l", lang],
            capture_output=True, timeout=60, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure("ocr", f"tesseract 调用失败：{exc}")
    text = (result.stdout or "").strip()
    if not text:
        return ToolResult.failure("ocr", "tesseract 没识别出文字")
    return ToolResult(
        name="ocr", ok=True, text=text, data={"engine": "tesseract", "source_type": "image", "text": text},
        source_type="image", confidence=0.75,
    )


def _run_windows_ocr(path: Path) -> ToolResult:
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
        handle.write(WINDOWS_OCR_SCRIPT)
        script = handle.name
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, str(path)],
            capture_output=True, timeout=90, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure("ocr", f"Windows OCR 调用失败：{exc}")
    finally:
        try:
            Path(script).unlink(missing_ok=True)
        except OSError:
            pass
    text = (result.stdout or "").strip()
    if not text:
        return ToolResult.failure("ocr", "Windows OCR 没识别出文字")
    # Windows OCR 会把中文按字拆开（"夕 颜 测 试"），这里合回去
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return ToolResult(
        name="ocr", ok=True, text=text, data={"engine": "windows_ocr", "source_type": "image", "text": text},
        source_type="image", confidence=0.7,
    )


def recognize(path: str | Path, *, lang: str = "chi_sim+eng", prefer: str = "") -> ToolResult:
    file_path = Path(path)
    if not file_path.is_file():
        return ToolResult.failure("ocr", "文件不存在")
    order = [prefer] if prefer else []
    order += ["tesseract", "windows"]
    for engine in order:
        if engine == "tesseract" and tesseract_available():
            result = _run_tesseract(file_path, lang=lang)
            if result.ok:
                return result
        elif engine == "windows" and windows_ocr_available():
            result = _run_windows_ocr(file_path)
            if result.ok:
                return result
    return ToolResult.failure("ocr", "本机没有可用的 OCR（没装 tesseract，Windows OCR 也不可用）")
