"""
Asynchronous transport for VK Web LongPoll via aiohttp.
Supports HTTP/HTTPS and SOCKS5 proxies, error handling and event polling.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional
import aiohttp

try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None  # type: ignore

from .config import (
    DEFAULT_HEADERS,
    DEFAULT_USER_AGENT,
    LONGPOLL_VERSION,
    POLL_WAIT_SEC,
    VK_API_VERSION,
)

logger = logging.getLogger("vk_longpoll.transport")


class VKError(Exception):
    """Базовое исключение сетевого транспорта VK."""
    pass


class VKAPIError(VKError):
    """Исключение при ошибке вызова метода VK API."""

    def __init__(self, error_code: int, error_msg: str, extra: Optional[dict] = None):
        super().__init__(f"VK API error {error_code}: {error_msg}")
        self.error_code = error_code
        self.error_msg = error_msg
        self.extra = extra or {}


class VKAuthError(VKAPIError):
    """Ошибка 5: Токен истек или недействителен."""
    pass


class VKFloodError(VKAPIError):
    """Ошибка 9: Превышен лимит запросов (Flood control)."""
    pass


class VKLongPollTransport:
    """
    Легковесный асинхронный транспорт на базе aiohttp.
    Обеспечивает постоянное соединение к VK LongPoll без запуска браузера.
    """

    def __init__(
        self,
        access_token: str,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        self.access_token = access_token
        self.proxy = proxy
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self._session: Optional[aiohttp.ClientSession] = None
        self._user_cache: Dict[int, str] = {}
        self._chat_cache: Dict[int, str] = {}

    def update_token(self, new_token: str):
        """Обновляет токен доступа на лету."""
        self.access_token = new_token

    async def _get_session(self) -> aiohttp.ClientSession:
        """Создает или возвращает существующую aiohttp.ClientSession с настройкой прокси."""
        if self._session is None or self._session.closed:
            connector = None
            if self.proxy:
                proxy_lower = self.proxy.lower()
                if proxy_lower.startswith(("socks5://", "socks4://", "socks5h://")):
                    if ProxyConnector is not None:
                        connector = ProxyConnector.from_url(self.proxy)
                    else:
                        logger.warning("aiohttp-socks не установлен, SOCKS прокси может работать некорректно")

            headers = dict(DEFAULT_HEADERS)
            headers["User-Agent"] = self.user_agent

            timeout = aiohttp.ClientTimeout(total=45, connect=15)
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers=headers,
                timeout=timeout,
            )
        return self._session

    async def close(self):
        """Закрывает клиентскую сессию."""
        if self._session and not self._session.closed:
            await self._session.close()
            # Даем циклу событий время закрыть внутренние сокеты
            await asyncio.sleep(0.1)

    async def __aenter__(self):
        await self._get_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def call_method(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Выполняет вызов метода VK API https://api.vk.ru/method/{method}.
        Автоматически передает токен, версию API и парсит ответ.
        """
        session = await self._get_session()
        url = f"https://api.vk.ru/method/{method}"

        payload: Dict[str, Any] = {
            "access_token": self.access_token,
            "v": VK_API_VERSION,
        }
        if params:
            payload.update(params)

        request_kwargs: Dict[str, Any] = {"params": payload}
        # Для HTTP/HTTPS прокси (не SOCKS) aiohttp поддерживает параметр proxy в get()
        if self.proxy and self.proxy.lower().startswith(("http://", "https://")):
            request_kwargs["proxy"] = self.proxy

        try:
            async with session.get(url, **request_kwargs) as resp:
                data = await resp.json(content_type=None)
        except Exception as e:
            raise VKError(f"Сетевая ошибка при вызове {method}: {e}") from e

        if "error" in data:
            err = data["error"]
            code = err.get("error_code", 0)
            msg = err.get("error_msg", "Неизвестная ошибка")
            if code == 5:
                raise VKAuthError(code, msg, err)
            if code == 9:
                raise VKFloodError(code, msg, err)
            raise VKAPIError(code, msg, err)

        return data.get("response")

    async def get_long_poll_server(self, need_pts: int = 0) -> Dict[str, Any]:
        """
        Запрашивает параметры сервера LongPoll:
        GET https://api.vk.ru/method/messages.getLongPollServer
        Возвращает: {"server": str, "key": str, "ts": int}
        """
        params = {
            "need_pts": need_pts,
            "lp_version": LONGPOLL_VERSION,
        }
        resp = await self.call_method("messages.getLongPollServer", params)
        if not isinstance(resp, dict) or "server" not in resp:
            raise VKError(f"Некорректный ответ getLongPollServer: {resp}")
        return {
            "server": resp["server"],
            "key": resp["key"],
            "ts": int(resp["ts"]),
        }

    async def poll(
        self,
        server: str,
        key: str,
        ts: int,
        wait: int = POLL_WAIT_SEC,
        mode: int = 2,
    ) -> Dict[str, Any]:
        """
        Выполняет один такт LongPoll запроса к серверу очередей сообщений.
        GET https://{server}?act=a_check&key={key}&ts={ts}&wait={wait}&mode={mode}&version=3
        """
        session = await self._get_session()

        # Нормализация URL сервера событий
        clean_server = server.strip()
        if not clean_server.startswith("http://") and not clean_server.startswith("https://"):
            url = f"https://{clean_server}"
        else:
            url = clean_server

        params = {
            "act": "a_check",
            "key": key,
            "ts": ts,
            "wait": wait,
            "mode": mode,
            "version": LONGPOLL_VERSION,
        }

        # Таймаут запроса: время ожидания плюс запас на сетевой лаг
        poll_timeout = aiohttp.ClientTimeout(total=wait + 15, connect=10)
        request_kwargs: Dict[str, Any] = {
            "params": params,
            "timeout": poll_timeout,
        }
        if self.proxy and self.proxy.lower().startswith(("http://", "https://")):
            request_kwargs["proxy"] = self.proxy

        try:
            async with session.get(url, **request_kwargs) as resp:
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            # Обычный таймаут при отсутствии новых сообщений — возвращаем прежний ts
            return {"action": "timeout", "ts": ts}
        except Exception as e:
            raise VKError(f"Ошибка LongPoll сокета: {e}") from e

        if "failed" in data:
            code = data["failed"]
            # failed: 1 — устарел ts, обновить ts
            if code == 1:
                return {"action": "update_ts", "ts": data.get("ts", ts)}
            # failed: 2 — истек key, требуется перезапрос getLongPollServer
            if code == 2:
                return {"action": "refresh_key"}
            # failed: 3 — потеря информации, заново запросить сервер и ts
            if code == 3:
                return {"action": "reset_ts"}
            # failed: 4 — неверная версия протокола
            if code == 4:
                return {"action": "bad_version"}
            return {"action": "error", "code": code}

        new_ts = data.get("ts", ts)
        updates = data.get("updates", [])
        return {
            "action": "updates",
            "ts": new_ts,
            "updates": updates,
        }

    async def resolve_users(self, user_ids: List[int]) -> Dict[int, str]:
        """
        Разрешает список идентификаторов пользователей в удобочитаемые имена.
        Использует локальный кэш для экономии лимитов VK API.
        """
        unresolved = [uid for uid in user_ids if uid not in self._user_cache and uid > 0]
        if not unresolved:
            return {uid: self._user_cache.get(uid, f"id{uid}") for uid in user_ids}

        try:
            # VK API users.get поддерживает до 1000 ID за один запрос
            uids_str = ",".join(str(uid) for uid in unresolved[:100])
            res = await self.call_method("users.get", {"user_ids": uids_str, "fields": "first_name,last_name"})
            if isinstance(res, list):
                for u in res:
                    uid = u.get("id")
                    first_name = u.get("first_name", "")
                    last_name = u.get("last_name", "")
                    name = f"{first_name} {last_name}".strip()
                    if uid and name:
                        self._user_cache[uid] = name
        except Exception as e:
            logger.debug(f"Не удалось разрешить имена пользователей: {e}")

        return {uid: self._user_cache.get(uid, f"id{uid}") for uid in user_ids}

    async def resolve_conversation_title(self, peer_id: int) -> str:
        """
        Разрешает заголовок беседы (peer_id > 2000000000).
        Использует локальный кэш.
        """
        if peer_id in self._chat_cache:
            return self._chat_cache[peer_id]

        title = f"Беседа {peer_id - 2000000000}"
        try:
            res = await self.call_method("messages.getConversationsById", {"peer_ids": str(peer_id)})
            if isinstance(res, dict) and "items" in res and res["items"]:
                chat_info = res["items"][0]
                chat_settings = chat_info.get("chat_settings", {})
                chat_title = chat_settings.get("title", "")
                if chat_title:
                    title = chat_title
                    self._chat_cache[peer_id] = title
        except Exception as e:
            logger.debug(f"Не удалось разрешить название беседы {peer_id}: {e}")

        return title
