"""PC status via ctypes only (no third-party dependencies)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import socket
import time


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.wintypes.DWORD),
        ("dwMemoryLoad", ctypes.wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _filetime_to_int(value: ctypes.wintypes.FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def memory_status() -> dict[str, float]:
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    total_gb = status.ullTotalPhys / 1024**3
    used_gb = (status.ullTotalPhys - status.ullAvailPhys) / 1024**3
    return {"total_gb": round(total_gb, 1), "used_gb": round(used_gb, 1), "percent": int(status.dwMemoryLoad)}


def cpu_percent(sample_seconds: float = 0.6) -> float:
    def times() -> tuple[int, int, int]:
        idle = ctypes.wintypes.FILETIME()
        kernel = ctypes.wintypes.FILETIME()
        user = ctypes.wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        return _filetime_to_int(idle), _filetime_to_int(kernel), _filetime_to_int(user)

    idle0, kernel0, user0 = times()
    time.sleep(sample_seconds)
    idle1, kernel1, user1 = times()
    idle_delta = idle1 - idle0
    kernel_delta = kernel1 - kernel0
    user_delta = user1 - user0
    total = kernel_delta + user_delta
    if total <= 0:
        return 0.0
    return round(100.0 * (1.0 - idle_delta / total), 1)


def disk_usage(path: str = "C:\\") -> dict[str, float]:
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(path),
        None,
        ctypes.byref(total),
        ctypes.byref(free),
    )
    total_gb = total.value / 1024**3
    free_gb = free.value / 1024**3
    return {
        "total_gb": round(total_gb, 1),
        "free_gb": round(free_gb, 1),
        "used_gb": round(total_gb - free_gb, 1),
        "percent": round(100.0 * (1.0 - free.value / total.value), 1) if total.value else 0.0,
    }


def report() -> dict:
    return {
        "hostname": socket.gethostname(),
        "cpu": cpu_percent(),
        "memory": memory_status(),
        "disk": disk_usage(),
    }
