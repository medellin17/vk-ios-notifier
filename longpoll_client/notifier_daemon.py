"""
Main daemon for VK Web LongPoll Notifier.
Event-driven listener using aiohttp with ~20 MB RAM consumption.
Replaces heavy Playwright Chromium instance in 24/7 runtime.
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
import aiohttp

from .auth import check_token_valid, ensure_valid_credentials
from .config import (
    CACHE_FILE,
    CREDENTIALS_FILE,
    DEFAULT_USER_AGENT,
    EDIT_SIMILARITY_THRESHOLD,
    EDIT_SUPPRESSION_WINDOW_SEC,
    ENABLE_TYPING_NOTIFICATIONS,
    LONGPOLL_VERSION,
    POLL_WAIT_SEC,
    TOKEN_REFRESH_INTERVAL_HOURS,
    TYPING_COOLDOWN_SEC,
    get_bark_key,
    get_configured_token,
    get_proxy_url,
    load_seen_cache,
    save_seen_cache,
)
from .longpoll_transport import VKAPIError, VKAuthError, VKLongPollTransport

logger = logging.getLogger("vk_longpoll.daemon")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")


def get_memory_usage_mb() -> float:
    """Возвращает фактическое потребление оперативной памяти (RSS) в мегабайтах."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def clean_duplicate_author(text: str) -> str:
    """
    Устраняет дублирование авторов реплик в беседах.
    VK рендерит одновременно краткий тег и доступный полный:
    'Анжелика Б: Анжелика Бородина: ...' -> 'Анжелика Бородина: ...'
    'Лина M: Лина M: ...' -> 'Лина M: ...'
    'Вы: Вы: ...' -> 'Вы: ...'
    """
    if not text:
        return ""
    text = text.replace("\u00a0", " ")
    s = re.sub(r"^([^\s:]{1,30})\s+[^\s:]{1,3}\.?:\s*(\1\s+[^:\n]{1,35}):\s*", r"\2: ", text)
    s = re.sub(r"^([^:\n]{1,40}):\s*\1:\s*", r"\1: ", s)
    return s.strip()


def normalize_snippet(text: str) -> str:
    """
    Нормализует сниппет сообщения для устранения дребезга разметки VK.
    Оборачивание типа вложения в квадратные скобки ([Файл] <-> Файл).
    """
    if not text:
        return ""
    clean = clean_duplicate_author(text)
    clean = re.sub(r"\s+", " ", clean.replace("\u00a0", " ")).strip()
    return re.sub(
        r"\[(Файл|Фотография|Стикер|Голосовое сообщение|Видеозапись|Аудиозапись|Запись на стене|Вложение[^\]]*)\]",
        r"\1",
        clean,
        flags=re.IGNORECASE,
    )


def is_minor_edit(old_text: str, new_text: str) -> bool:
    """Определяет, является ли изменение текста исправлением опечатки."""
    if not old_text or not new_text:
        return False
    if old_text == new_text:
        return True
    return SequenceMatcher(None, old_text, new_text).ratio() >= EDIT_SIMILARITY_THRESHOLD


def is_typing_status(text: str) -> bool:
    """Проверяет статус набора текста."""
    if not text:
        return False
    lower = text.strip().lower()
    return lower.startswith("печатает") or lower.startswith("набирает")


def is_ignored_snippet(text: str) -> bool:
    """Фильтрует исходящие реплики пользователя и пустые сниппеты."""
    if not text or not text.strip():
        return True
    clean = text.strip()
    lower = clean.lower()

    if clean.startswith("Вы:"):
        return True
    if clean.startswith("·"):
        return True
    if re.search(r"^\d+\s*(минут|мин|сек|секунд|час|ч|дн|дней)", lower):
        return True
    if lower.endswith("назад") or "только что" in lower:
        return True

    return False


def format_attachment_snippet(extra_data: Dict[str, Any], attachments: Any) -> str:
    """Формирует текстовое описание вложения по атрибутам события LongPoll."""
    attach_type = extra_data.get("attach1_type") or ""
    if not attach_type and isinstance(attachments, dict):
        attach_type = attachments.get("attach1_type") or attachments.get("type", "")

    mapping = {
        "photo": "[Фотография]",
        "sticker": "[Стикер]",
        "doc": "[Файл]",
        "audiomsg": "[Голосовое сообщение]",
        "audio_message": "[Голосовое сообщение]",
        "video": "[Видеозапись]",
        "audio": "[Аудиозапись]",
        "wall": "[Запись на стене]",
        "link": "[Ссылка]",
        "gift": "[Подарок]",
        "market": "[Товар]",
    }
    return mapping.get(str(attach_type).lower(), "[Вложение]")


def parse_longpoll_message(update: list) -> Optional[Dict[str, Any]]:
    """
    Разбирает событие LongPoll v3 кода 4 (новое входящее сообщение).
    Формат: [4, message_id, flags, peer_id, timestamp, text, extra_data, attachments, ...]
    """
    if not isinstance(update, list) or len(update) < 6:
        return None
    if update[0] != 4:
        return None

    msg_id = int(update[1])
    flags = int(update[2])
    peer_id = int(update[3])
    timestamp = int(update[4])
    text = str(update[5])

    # Фильтрация исходящих: flags & 2 == 2 (FLAGS_OUTBOX)
    is_outbox = bool(flags & 2)

    extra_data = {}
    if len(update) > 6 and isinstance(update[6], dict):
        extra_data = update[6]

    attachments = {}
    if len(update) > 7 and isinstance(update[7], (dict, list)):
        attachments = update[7]

    return {
        "message_id": msg_id,
        "flags": flags,
        "is_outbox": is_outbox,
        "peer_id": peer_id,
        "timestamp": timestamp,
        "text": text,
        "extra_data": extra_data,
        "attachments": attachments,
    }


def parse_typing_event(update: list) -> Optional[Dict[str, Any]]:
    """
    Разбирает событие набора текста:
    Код 61: [61, user_id, flags]
    Код 62: [62, user_id, chat_id]
    """
    if not isinstance(update, list) or len(update) < 2:
        return None

    code = update[0]
    if code == 61 and len(update) >= 2:
        user_id = int(update[1])
        return {"peer_id": user_id, "user_id": user_id, "is_chat": False}
    elif code == 62 and len(update) >= 3:
        user_id = int(update[1])
        chat_id = int(update[2])
        peer_id = 2000000000 + chat_id
        return {"peer_id": peer_id, "user_id": user_id, "is_chat": True}

    return None


async def send_bark_push_async(
    session: aiohttp.ClientSession,
    bark_key: str,
    title: str,
    body: str,
    url: str = "https://vk.ru/im",
) -> bool:
    """Асинхронно отправляет push-уведомление в сервис Bark для iOS."""
    if not bark_key:
        logger.warning("BARK_KEY не задан, пуш пропущен.")
        return False

    api_url = f"https://api.day.app/{bark_key}/"
    payload = {
        "title": title,
        "body": body if body else "[Новое сообщение ВКонтакте]",
        "group": "VK",
        "icon": "https://vk.com/images/svg_icons/ic_head_logo.svg",
        "url": url,
        "sound": "calypso",
        "level": "timeSensitive",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with session.post(api_url, json=payload, timeout=timeout) as resp:
            if resp.status == 200:
                logger.info(f"🔔 Bark Push отправлен: {title} | {body}")
                return True
            else:
                logger.warning(f"Ошибка ответа Bark API: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"Сбой отправки Bark Push: {e}")

    return False


class VKLongPollDaemon:
    """
    Основной сервис мониторинга сообщений VK на базе чистого LongPoll.
    Потребление памяти в фоновом режиме: ~20 МБ.
    """

    def __init__(self):
        self.bark_key = get_bark_key()
        self.proxy = get_proxy_url()
        self.token = get_configured_token() or ""
        self.seen_dialogs: Dict[str, str] = load_seen_cache()
        self.last_typing_time: Dict[str, float] = {}
        self.last_incoming_push_time: Dict[str, float] = {}
        self.last_token_refresh_time = time.time()
        self.is_running = True
        self._transport: Optional[VKLongPollTransport] = None
        self._bark_session: Optional[aiohttp.ClientSession] = None

    async def initialize(self):
        """Проверяет токен, при необходимости обновляет его и настраивает транспорт."""
        if not self.bark_key:
            logger.error("BARK_KEY не найден в конфигурации! Задайте его в .env")
            sys.exit(1)

        mem = get_memory_usage_mb()
        logger.info(f"Старт VK LongPoll демона. Потребление RAM: {mem:.1f} МБ")

        # Проверяем токен
        if not self.token or not check_token_valid(self.token, proxy=self.proxy):
            logger.info("Токен недействителен, запуск процедуры обеспечения учетных данных...")
            creds = ensure_valid_credentials()
            self.token = creds.get("access_token", "")

        self._transport = VKLongPollTransport(
            access_token=self.token,
            proxy=self.proxy,
        )
        self._bark_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )

    async def handle_message_event(self, event: Dict[str, Any]):
        """Обработка одного входящего сообщения."""
        # 1. Фильтруем исходящие
        if event["is_outbox"]:
            return

        peer_id = event["peer_id"]
        peer_key = str(peer_id)
        raw_text = event["text"].strip()
        now = time.time()

        # Формируем тело сообщения: если текста нет — определяем тип вложения
        if not raw_text:
            body = format_attachment_snippet(event["extra_data"], event["attachments"])
        else:
            body = raw_text

        # 2. Игнорируем собственные реплики и таймштампы
        if is_ignored_snippet(body):
            return

        # 3. Разрешаем имя отправителя и заголовок
        author_name = "Собеседник"
        is_chat = peer_id > 2000000000

        if is_chat:
            from_id = event["extra_data"].get("from")
            chat_title = await self._transport.resolve_conversation_title(peer_id)
            if from_id:
                try:
                    uid = int(from_id)
                    users = await self._transport.resolve_users([uid])
                    author_name = users.get(uid, f"id{uid}")
                except Exception:
                    author_name = f"id{from_id}"
            title = f"VK: {chat_title}"
            snippet_with_author = f"{author_name}: {body}"
        else:
            if peer_id > 0:
                users = await self._transport.resolve_users([peer_id])
                author_name = users.get(peer_id, f"id{peer_id}")
            elif peer_id < 0:
                author_name = f"Сообщество {-peer_id}"
            title = f"VK: {author_name}"
            snippet_with_author = body

        snippet_clean = clean_duplicate_author(snippet_with_author)
        snippet_norm = normalize_snippet(snippet_clean)

        old_raw = self.seen_dialogs.get(peer_key, "")
        old_norm = normalize_snippet(old_raw)

        # 4. Проверка дедупликации и подавление правок опечаток
        if peer_key not in self.seen_dialogs:
            self.seen_dialogs[peer_key] = snippet_clean
            save_seen_cache(self.seen_dialogs)
            self.last_incoming_push_time[peer_key] = now
            logger.info(f"[НОВОЕ ЛС] Новый диалог: {title} -> {snippet_clean}")
            await send_bark_push_async(
                self._bark_session,
                self.bark_key,
                title,
                snippet_clean,
                url=f"https://vk.ru/im?sel={peer_id}",
            )
        elif old_norm != snippet_norm:
            time_since_push = now - self.last_incoming_push_time.get(peer_key, 0)
            if time_since_push < EDIT_SUPPRESSION_WINDOW_SEC and is_minor_edit(old_norm, snippet_norm):
                self.seen_dialogs[peer_key] = snippet_clean
                save_seen_cache(self.seen_dialogs)
                logger.info(f"[ПРАВКА ОПЕЧАТКИ] {title}: \"{snippet_clean}\" (пуш подавлен)")
            else:
                self.seen_dialogs[peer_key] = snippet_clean
                save_seen_cache(self.seen_dialogs)
                self.last_incoming_push_time[peer_key] = now
                logger.info(f"[НОВОЕ ЛС] {title} -> {snippet_clean}")
                await send_bark_push_async(
                    self._bark_session,
                    self.bark_key,
                    title,
                    snippet_clean,
                    url=f"https://vk.ru/im?sel={peer_id}",
                )
        elif old_raw != snippet_clean:
            # Текст идентичен после нормализации
            self.seen_dialogs[peer_key] = snippet_clean
            save_seen_cache(self.seen_dialogs)

    async def handle_typing_event(self, event: Dict[str, Any]):
        """Обработка статуса 'печатает...' с кулдауном 180 сек."""
        if not ENABLE_TYPING_NOTIFICATIONS:
            return

        peer_id = event["peer_id"]
        peer_key = str(peer_id)
        now = time.time()

        if now - self.last_typing_time.get(peer_key, 0) >= TYPING_COOLDOWN_SEC:
            self.last_typing_time[peer_key] = now
            user_id = event["user_id"]
            users = await self._transport.resolve_users([user_id])
            user_name = users.get(user_id, f"id{user_id}")

            if event["is_chat"]:
                chat_title = await self._transport.resolve_conversation_title(peer_id)
                title = f"VK: {chat_title}"
                text = f"{user_name} печатает..."
            else:
                title = f"VK: {user_name}"
                text = "печатает..."

            logger.info(f"[ПЕЧАТАЕТ] {title}: {text}")
            await send_bark_push_async(
                self._bark_session,
                self.bark_key,
                title,
                text,
                url=f"https://vk.ru/im?sel={peer_id}",
            )

    async def check_periodic_token_refresh(self):
        """Периодическое (раз в 20 часов) фоновое обновление токена по куке remixsid."""
        now = time.time()
        if now - self.last_token_refresh_time >= TOKEN_REFRESH_INTERVAL_HOURS * 3600:
            logger.info("Плановое обновление веб-токена (интервал 20 ч)...")
            try:
                creds = ensure_valid_credentials(force_refresh=True)
                new_token = creds.get("access_token")
                if new_token:
                    self.token = new_token
                    self._transport.update_token(new_token)
                    self.last_token_refresh_time = now
                    logger.info("Веб-токен успешно обновлен по расписанию.")
            except Exception as e:
                logger.error(f"Не удалось обновить токен по расписанию: {e}")

    async def run(self):
        """Основной рабочий цикл прослушивания LongPoll."""
        await self.initialize()

        server_info = None
        while self.is_running:
            try:
                # 1. Получаем параметры сервера событий
                if server_info is None:
                    logger.info("Запрос сервера LongPoll (messages.getLongPollServer)...")
                    server_info = await self._transport.get_long_poll_server()
                    mem = get_memory_usage_mb()
                    logger.info(f"LongPoll сервер подключен: {server_info['server']} (RAM: {mem:.1f} МБ)")

                server = server_info["server"]
                key = server_info["key"]
                ts = server_info["ts"]

                # 2. Опрос событий в цикле
                while self.is_running:
                    await self.check_periodic_token_refresh()

                    poll_res = await self._transport.poll(server, key, ts, wait=POLL_WAIT_SEC)
                    action = poll_res.get("action")

                    if action == "updates":
                        ts = poll_res["ts"]
                        server_info["ts"] = ts
                        updates = poll_res.get("updates", [])

                        for update in updates:
                            if not isinstance(update, list) or not update:
                                continue
                            code = update[0]

                            # Код 4: Новое сообщение
                            if code == 4:
                                msg_data = parse_longpoll_message(update)
                                if msg_data:
                                    await self.handle_message_event(msg_data)

                            # Коды 61, 62: Статус набора текста
                            elif code in (61, 62):
                                typing_data = parse_typing_event(update)
                                if typing_data:
                                    await self.handle_typing_event(typing_data)

                    elif action == "timeout":
                        # Нормальный таймаут при отсутствии сообщений
                        pass

                    elif action == "update_ts":
                        ts = poll_res["ts"]
                        server_info["ts"] = ts

                    elif action in ("refresh_key", "reset_ts"):
                        logger.info(f"Событие LongPoll {action}: переподключение к серверу...")
                        server_info = None
                        break

                    elif action == "bad_version":
                        logger.error("Версия протокола LongPoll устарела (failed: 4)")
                        await asyncio.sleep(5)
                        server_info = None
                        break

                    else:
                        await asyncio.sleep(1)

            except VKAuthError as e:
                logger.error(f"[КРИТИЧНО] Авторизация токена не удалась: {e}")
                await send_bark_push_async(
                    self._bark_session,
                    self.bark_key,
                    "VK: Сессия истекла",
                    "Сессия VK истекла. Требуется повторный вход через QR-код!",
                    url="https://vk.ru/",
                )
                try:
                    logger.info("Попытка экстренного обновления учетных данных...")
                    creds = ensure_valid_credentials(force_refresh=True)
                    self.token = creds.get("access_token", "")
                    self._transport.update_token(self.token)
                    server_info = None
                except Exception as ex:
                    logger.error(f"Не удалось восстановить токен: {ex}. Пауза 60 сек...")
                    await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"Исключение в цикле LongPoll: {e}. Переподключение через 5 сек...")
                await asyncio.sleep(5)
                server_info = None

    async def shutdown(self):
        """Корректное завершение работы демона."""
        logger.info("Остановка VK LongPoll демона...")
        self.is_running = False
        save_seen_cache(self.seen_dialogs)
        if self._transport:
            await self._transport.close()
        if self._bark_session and not self._bark_session.closed:
            await self._bark_session.close()
        logger.info("Демон успешно остановлен.")


def main():
    daemon = VKLongPollDaemon()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler():
        logger.info("Получен сигнал завершения...")
        asyncio.create_task(daemon.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(daemon.run())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        loop.run_until_complete(daemon.shutdown())
        loop.close()


if __name__ == "__main__":
    main()
