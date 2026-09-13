"""能力层：把外部能力统一成"有/没有"的接口，核心 Bot 不关心实现细节。"""

from capabilities.base import Capability, CapabilityResult, CapabilityState  # noqa: F401
from capabilities.manager import CapabilityManager  # noqa: F401
