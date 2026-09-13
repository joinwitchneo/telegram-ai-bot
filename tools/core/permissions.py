"""权限：私人 Bot 也要有硬边界——联网、调用外部模型、写文件都得先被允许。"""

from __future__ import annotations


class Permissions:
    def __init__(
        self,
        *,
        allow_network: bool = True,
        allow_local_model: bool = True,
        allow_paid_api: bool = True,
        allow_file_write: bool = True,
        allowed_tools: list[str] | None = None,
        blocked_tools: list[str] | None = None,
    ) -> None:
        self.allow_network = bool(allow_network)
        self.allow_local_model = bool(allow_local_model)
        self.allow_paid_api = bool(allow_paid_api)
        self.allow_file_write = bool(allow_file_write)
        self.allowed_tools = {str(name) for name in (allowed_tools or []) if str(name)}
        self.blocked_tools = {str(name) for name in (blocked_tools or []) if str(name)}

    def check(self, name: str, mode: str = "LOCAL") -> tuple[bool, str]:
        if name in self.blocked_tools:
            return False, "这个工具被关闭了"
        if self.allowed_tools and name not in self.allowed_tools:
            return False, "这个工具不在白名单里"
        if mode == "FREE_NETWORK" and not self.allow_network:
            return False, "联网被关闭"
        if mode == "PAID_API" and not self.allow_paid_api:
            return False, "付费接口被关闭"
        if mode == "LOCAL" and not self.allow_file_write:
            return False, "本地文件操作被关闭"
        return True, ""

    def to_dict(self) -> dict:
        return {
            "allow_network": self.allow_network,
            "allow_local_model": self.allow_local_model,
            "allow_paid_api": self.allow_paid_api,
            "allow_file_write": self.allow_file_write,
            "allowed_tools": sorted(self.allowed_tools),
            "blocked_tools": sorted(self.blocked_tools),
        }
