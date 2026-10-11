"""
Lightweight VK Web LongPoll Client (aiohttp-based daemon).
Resource-efficient alternative to Chromium Playwright (~20 MB vs ~450 MB RAM).
"""

from .config import load_credentials, save_credentials, load_seen_cache, save_seen_cache
from .longpoll_transport import VKLongPollTransport, VKAPIError, VKAuthError

def __getattr__(name):
    if name == "VKLongPollDaemon":
        from .notifier_daemon import VKLongPollDaemon
        return VKLongPollDaemon
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

__all__ = [
    "load_credentials",
    "save_credentials",
    "load_seen_cache",
    "save_seen_cache",
    "VKLongPollTransport",
    "VKAPIError",
    "VKAuthError",
    "VKLongPollDaemon",
]
