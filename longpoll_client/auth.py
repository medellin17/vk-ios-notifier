"""
Authentication and credential management for VK Web LongPoll.
Supports lightweight HTTP refresh (remixsid) and one-off Playwright extraction (app_id 6287487).
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
import requests

from .config import (
    BASE_DIR,
    CREDENTIALS_FILE,
    DEFAULT_HEADERS,
    DEFAULT_USER_AGENT,
    ENV_FILE,
    SESSION_FILE,
    VK_APP_ID,
    VK_API_VERSION,
    get_bark_key,
    get_configured_token,
    get_proxy_url,
    get_remixsid,
    load_credentials,
    save_credentials,
    update_env_file,
)

logger = logging.getLogger("vk_longpoll.auth")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")


def check_token_valid(token: str, proxy: Optional[str] = None) -> bool:
    """
    Проверяет валидность токена вызовом легковесного метода VK API.
    Возвращает True если токен активен.
    """
    if not token:
        return False

    url = f"https://api.vk.ru/method/utils.getServerTime?v={VK_API_VERSION}&access_token={token}"
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            proxies=proxies,
            timeout=10,
        )
        data = resp.json()
        if "response" in data and isinstance(data["response"], int):
            return True
        if "error" in data:
            code = data["error"].get("error_code")
            logger.debug(f"Токен не прошел проверку (код ошибки {code})")
    except Exception as e:
        logger.debug(f"Сетевая ошибка при проверке токена: {e}")

    return False


def refresh_token_via_http(remixsid: str, proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Этап 2 RFC: Легковесное обновление веб-токена по сессионной куке remixsid без запуска Chromium.
    Делает один HTTPS-запрос к внутреннему веб-шлюзу VK.
    """
    if not remixsid:
        return None

    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = "https://vk.ru/im"
    headers["Origin"] = "https://vk.ru"

    cookies = {"remixsid": remixsid}
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    endpoints = [
        "https://vk.ru/al_im.php?act=get_token",
        "https://vk.com/al_im.php?act=get_token",
    ]

    for url in endpoints:
        try:
            logger.info(f"Попытка обновления веб-токена через HTTP: {url}")
            resp = requests.post(
                url,
                cookies=cookies,
                headers=headers,
                proxies=proxies,
                timeout=12,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    token = None
                    if isinstance(data, dict):
                        token = data.get("access_token") or data.get("token")
                    elif isinstance(data, list) and len(data) > 0:
                        # В некоторых ответах al_im формат payload [..., {access_token: ...}]
                        for item in data:
                            if isinstance(item, dict) and "access_token" in item:
                                token = item["access_token"]
                                break

                    if token:
                        creds = {
                            "access_token": token,
                            "remixsid": remixsid,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                            "app_id": VK_APP_ID,
                            "method": "http_refresh",
                        }
                        save_credentials(creds)
                        update_env_file("VK_TOKEN", token)
                        logger.info("Веб-токен успешно обновлен через HTTP!")
                        return creds
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Не удалось обновить токен через {url}: {e}")

    return None


def extract_credentials_playwright(
    session_file: Optional[Path] = None,
    proxy: Optional[str] = None,
    wait_qr_if_needed: bool = False,
) -> Dict[str, Any]:
    """
    Этап 1 RFC: Разовый запуск Playwright на 10-20 секунд для извлечения веб-токена и кук.
    После извлечения браузер полностью закрывается, освобождая всю память (~500 МБ).
    """
    logger.info("Запуск однократной сессии Playwright для извлечения веб-токена...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright не установлен в системе.")

    # Используем проверенный stealth-профиль проекта
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    try:
        from src.stealth_browser import create_stealth_browser_and_context
    except ImportError:
        create_stealth_browser_and_context = None  # type: ignore

    session_path = str(session_file or SESSION_FILE)
    if not os.path.isfile(session_path):
        session_path = None

    captured_token: Optional[str] = None
    captured_cookies: Dict[str, str] = {}
    user_id: Optional[int] = None

    with sync_playwright() as p:
        if create_stealth_browser_and_context:
            browser, context = create_stealth_browser_and_context(p, storage_state=session_path)
        else:
            browser = p.chromium.launch(
                headless=True,
                args=["--headless=new", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                storage_state=session_path,
            )

        page = context.new_page()

        # 1. Перехват сетевых ответов для поиска access_token веб-клиента
        def handle_response(response):
            nonlocal captured_token, user_id
            url = response.url
            if "token" in url or "api.vk" in url or "al_im.php" in url:
                try:
                    ctype = response.headers.get("content-type", "")
                    if "application/json" in ctype or "text/javascript" in ctype:
                        body = response.json()
                        if isinstance(body, dict):
                            t = body.get("access_token") or body.get("token")
                            if t and not captured_token:
                                captured_token = t
                                logger.info("Токен обнаружен в сетевом ответе!")
                            uid = body.get("user_id") or body.get("uid")
                            if uid and not user_id:
                                user_id = int(uid)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            logger.info("Открытие https://vk.ru/im...")
            page.goto("https://vk.ru/im", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)

            # Проклик плашки безопасности если вылезла
            for _ in range(3):
                if "challenge" in page.url or "cert" in page.url or "робот" in page.title():
                    btn = page.query_selector('.start, button:has-text("Продолжить"), div[role="button"]:has-text("Продолжить")')
                    if btn:
                        btn.click()
                        page.wait_for_timeout(3000)
                else:
                    break

            # 2. Проверка localStorage на сохраненные токены
            if not captured_token:
                try:
                    storage_data = page.evaluate("""() => {
                        const out = {};
                        for (let i = 0; i < localStorage.length; i++) {
                            const k = localStorage.key(i);
                            out[k] = localStorage.getItem(k);
                        }
                        return out;
                    }""")
                    for k, val in storage_data.items():
                        if "token" in k or "6287487" in k:
                            try:
                                parsed = json.loads(val)
                                if isinstance(parsed, dict) and "access_token" in parsed:
                                    captured_token = parsed["access_token"]
                                    logger.info(f"Токен найден в localStorage ({k})")
                                    break
                            except Exception:
                                pass
                except Exception:
                    pass

            # 3. Вызов внутреннего JS-метода vk для выпуска токена внутри активной страницы
            if not captured_token:
                try:
                    token_res = page.evaluate("""async () => {
                        try {
                            const r = await fetch('/al_im.php?act=get_token', { method: 'POST' });
                            return await r.json();
                        } catch(e) {
                            return null;
                        }
                    }""")
                    if isinstance(token_res, dict) and "access_token" in token_res:
                        captured_token = token_res["access_token"]
                        logger.info("Токен получен через fetch внутри страницы VK")
                except Exception:
                    pass

            # 4. Сбор сессионных кук
            cookies_list = context.cookies()
            for c in cookies_list:
                name = c.get("name")
                val = c.get("value")
                if name and val:
                    captured_cookies[name] = val

            # Сохраняем обновленный storage_state
            if captured_cookies.get("remixsid"):
                try:
                    context.storage_state(path=str(SESSION_FILE))
                except Exception:
                    pass

        finally:
            logger.info("Закрытие Playwright браузера (освобождение памяти)...")
            browser.close()

    remixsid = captured_cookies.get("remixsid") or get_remixsid() or ""
    remixnsid = captured_cookies.get("remixnsid", "")

    creds: Dict[str, Any] = {
        "access_token": captured_token or "",
        "remixsid": remixsid,
        "remixnsid": remixnsid,
        "user_id": user_id,
        "app_id": VK_APP_ID,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "method": "playwright_one_off",
    }

    if captured_token:
        save_credentials(creds)
        update_env_file("VK_TOKEN", captured_token)
        if remixsid:
            update_env_file("REMIXSID", remixsid)
        logger.info("Учетные данные успешно извлечены и сохранены в credentials.json")
    else:
        logger.warning("Playwright завершил работу, но access_token не был найден.")

    return creds


def ensure_valid_credentials(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Гарантирует наличие валидного веб-токена:
    1. Проверяет текущий токен (если не force).
    2. При устаревании пробует легкий HTTP refresh через remixsid.
    3. При неудаче вызывает разовый Playwright.
    """
    proxy = get_proxy_url()
    creds = load_credentials()
    token = creds.get("access_token") or get_configured_token() or ""

    if not force_refresh and token and check_token_valid(token, proxy=proxy):
        logger.info("Текущий токен VK валиден и готов к работе.")
        return creds

    logger.info("Токен отсутствует или устарел. Запуск процедуры обновления...")
    remixsid = creds.get("remixsid") or get_remixsid()

    # Попытка 1: Легковесный HTTP refresh
    if remixsid:
        refreshed = refresh_token_via_http(remixsid, proxy=proxy)
        if refreshed and check_token_valid(refreshed.get("access_token", ""), proxy=proxy):
            return refreshed

    # Попытка 2: Разовый Playwright
    extracted = extract_credentials_playwright(proxy=proxy)
    if extracted.get("access_token") and check_token_valid(extracted["access_token"], proxy=proxy):
        return extracted

    raise RuntimeError(
        "Не удалось получить валидный токен VK. "
        "Сессия устарела. Запустите 'python3 src/qr_auth.py' для повторного входа через QR-код."
    )


def main():
    parser = argparse.ArgumentParser(description="Автономное управление учетными данными VK Web LongPoll")
    parser.add_argument("--check", action="store_true", help="Проверить валидность текущего токена")
    parser.add_argument("--refresh", action="store_true", help="Принудительно обновить токен")
    parser.add_argument("--browser", action="store_true", help="Запустить разовый Playwright для извлечения")
    args = parser.parse_args()

    proxy = get_proxy_url()

    if args.check:
        token = get_configured_token() or ""
        valid = check_token_valid(token, proxy=proxy)
        if valid:
            print("[OK] Токен валиден.")
            sys.exit(0)
        else:
            print("[FAIL] Токен недействителен или истек.")
            sys.exit(1)

    if args.browser:
        creds = extract_credentials_playwright(proxy=proxy)
        token = creds.get("access_token", "")
        if token and check_token_valid(token, proxy=proxy):
            print("[OK] Токен успешно извлечен через Playwright.")
            sys.exit(0)
        else:
            print("[FAIL] Не удалось получить токен через Playwright.")
            sys.exit(1)

    try:
        creds = ensure_valid_credentials(force_refresh=args.refresh)
        print(f"[OK] Учетные данные готовы! Метод: {creds.get('method', 'cached')}")
    except Exception as e:
        print(f"[ОШИБКА] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
