"""
Configuration and state persistence for VK Web LongPoll Client.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Каталоги и базовые пути
MODULE_DIR = Path(__file__).resolve().parent
BASE_DIR = MODULE_DIR.parent

ENV_FILE = BASE_DIR / ".env"
SESSION_FILE = BASE_DIR / "session.json"
CREDENTIALS_FILE = MODULE_DIR / "credentials.json"
CACHE_FILE = MODULE_DIR / "seen_cache.json"
ROOT_CACHE_FILE = BASE_DIR / "seen_cache.json"

# Константы VK API и протокола LongPoll
VK_APP_ID = 6287487  # VK Web Client App ID
VK_API_VERSION = "5.199"
LONGPOLL_VERSION = 3
POLL_WAIT_SEC = 25
TOKEN_REFRESH_INTERVAL_MINUTES = 10  # Авто-обновление каждые 10 минут (веб-токен живет 15 минут)

# Константы фильтрации и анти-спама
ENABLE_TYPING_NOTIFICATIONS = False  # Отключено: устраняет задержки и лишние пуши перед сообщением
TYPING_COOLDOWN_SEC = 180  # Максимум 1 пуш о наборе текста раз в 3 минуты на диалог
EDIT_SUPPRESSION_WINDOW_SEC = 25  # Окно подавления быстрых правок опечаток (сек)
EDIT_SIMILARITY_THRESHOLD = 0.80  # Порог сходства текстов для распознавания опечатки

# Фингерпринт: профиль Google Chrome под Windows 10
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


def read_env_dict() -> Dict[str, str]:
    """Считывает переменные из .env файла."""
    values: Dict[str, str] = {}
    if ENV_FILE.is_file():
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass
    return values


def update_env_file(key: str, value: str) -> bool:
    """Обновляет или добавляет ключ в .env файл."""
    try:
        lines = []
        found = False
        if ENV_FILE.is_file():
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

        new_lines = []
        for line in lines:
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                found = True
            else:
                new_lines.append(line)

        if not found:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"{key}={value}\n")

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        return True
    except Exception:
        return False


def get_bark_key() -> str:
    """Возвращает ключ устройства Bark из окружения или .env."""
    key = os.getenv("BARK_KEY", "").strip()
    if not key:
        env = read_env_dict()
        key = env.get("BARK_KEY", "").strip()
    return key


def get_proxy_url() -> Optional[str]:
    """Возвращает адрес прокси (VK_PROXY) при наличии."""
    proxy = os.getenv("VK_PROXY", "").strip()
    if not proxy:
        env = read_env_dict()
        proxy = env.get("VK_PROXY", "").strip()
    return proxy if proxy else None


def get_remixsid() -> Optional[str]:
    """Возвращает куку remixsid из окружения, .env или session.json."""
    sid = os.getenv("REMIXSID", "").strip()
    if sid:
        return sid

    env = read_env_dict()
    sid = env.get("REMIXSID", "").strip()
    if sid:
        return sid

    # Попытка извлечь из credentials.json
    creds = load_credentials()
    if creds.get("remixsid"):
        return creds["remixsid"]

    # Попытка извлечь из session.json
    if SESSION_FILE.is_file():
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for cookie in data.get("cookies", []):
                    if cookie.get("name") == "remixsid":
                        val = cookie.get("value", "").strip()
                        if val:
                            return val
        except Exception:
            pass

    return None


def get_configured_token() -> Optional[str]:
    """Возвращает токен из credentials.json, окружения или .env."""
    # 1. Сначала проверяем credentials.json
    creds = load_credentials()
    if creds.get("access_token"):
        return creds["access_token"]

    # 2. Переменные окружения
    token = os.getenv("VK_TOKEN", "").strip()
    if token:
        return token

    # 3. Файл .env
    env = read_env_dict()
    token = env.get("VK_TOKEN", "").strip()
    if token:
        return token

    return None


def load_credentials() -> Dict[str, Any]:
    """Загружает сохраненные учетные данные из credentials.json."""
    if CREDENTIALS_FILE.is_file():
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_credentials(data: Dict[str, Any]) -> bool:
    """Сохраняет учетные данные в credentials.json с защитой прав."""
    try:
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            CREDENTIALS_FILE.chmod(0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def load_seen_cache() -> Dict[str, str]:
    """
    Загружает кэш виденных сообщений.
    Если локальный кэш модуля отсутствует, мигрирует данные из корневого seen_cache.json.
    """
    if CACHE_FILE.is_file():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    if ROOT_CACHE_FILE.is_file():
        try:
            with open(ROOT_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
                # Копируем в локальный кэш модуля
                save_seen_cache(cache)
                return cache
        except Exception:
            pass

    return {}


def save_seen_cache(seen_dict: Dict[str, str]) -> bool:
    """Сохраняет кэш виденных сообщений на диск."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(seen_dict, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
