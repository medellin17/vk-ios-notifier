"""
Скрипт глубокого перехвата сетевого трафика и протокола VK Web.
Используется для реверс-инжиниринга:
1. Авторизация по QR-коду в изолированный файл session_test.json
2. Перехват WebSocket фреймов (FastPush / Queue)
3. Перехват внутренних HTTP API (al_im.php, al_stat.php, web.api.vk.ru)
4. Дамп глобальных JS-объектов (window.vk, window.cur)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from playwright.sync_api import sync_playwright
from src.stealth_browser import create_stealth_browser_and_context

SESSION_TEST_FILE = os.path.join(BASE_DIR, "session_test.json")
QR_TEST_FILE = os.path.join(BASE_DIR, "research", "qr_test.png")
TRAFFIC_LOG_FILE = os.path.join(BASE_DIR, "research", "traffic_log.jsonl")


def log_event(category: str, data: dict):
    entry = {
        "ts": datetime.now().isoformat(),
        "category": category,
        "data": data,
    }
    with open(TRAFFIC_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def handle_challenge(page):
    for attempt in range(1, 10):
        try:
            if "challenge" in page.url or "mincyfry-cert" in page.url or "робот" in page.title():
                btn = page.query_selector('.start, button:has-text("Продолжить"), div[role="button"]:has-text("Продолжить")')
                if btn:
                    print(f"[{time.strftime('%X')}] [Challenge {attempt}] Клик по 'Продолжить'...")
                    btn.click()
                    page.wait_for_timeout(3000)
            else:
                break
        except Exception:
            time.sleep(1)
            continue


def do_auth():
    print(f"[{time.strftime('%X')}] Запуск авторизации для тестового аккаунта...")
    artifact_dir = os.environ.get("ANTIGRAVITY_ARTIFACT_DIR", "/root/.gemini/antigravity-cli/brain/af0a733a-19bb-4731-a76a-086e95ca82ca")

    with sync_playwright() as p:
        browser, context = create_stealth_browser_and_context(p)
        page = context.new_page()

        print(f"[{time.strftime('%X')}] Переход на vk.com...")
        try:
            page.goto("https://vk.com/", wait_until="domcontentloaded")
        except Exception:
            pass
        time.sleep(2)

        handle_challenge(page)
        time.sleep(2)
        handle_challenge(page)

        def take_qr_screenshot():
            try:
                ifr = page.query_selector("iframe[src*='qr_auth'], iframe")
                if ifr:
                    ifr.screenshot(path=QR_TEST_FILE)
                else:
                    page.screenshot(path=QR_TEST_FILE)
                if os.path.isdir(artifact_dir):
                    import shutil
                    shutil.copy2(QR_TEST_FILE, os.path.join(artifact_dir, "qr_test.png"))
            except Exception:
                pass

        print(f"[{time.strftime('%X')}] Ожидание появления QR-кода...")
        for _ in range(30):
            handle_challenge(page)
            if page.query_selector("iframe[src*='qr_auth'], iframe"):
                time.sleep(2)
                take_qr_screenshot()
                break
            time.sleep(1)

        take_qr_screenshot()
        print(f"[{time.strftime('%X')}] Свежий QR-код сохранен: {QR_TEST_FILE}")
        print(f"[{time.strftime('%X')}] Ожидание сканирования с мобильного приложения (до 180 сек)...")

        start_wait = time.time()
        last_shot = time.time()
        code_file = os.path.join(BASE_DIR, "code.txt")
        while time.time() - start_wait < 180:
            time.sleep(1.5)
            handle_challenge(page)
            if time.time() - last_shot > 3.0:
                take_qr_screenshot()
                last_shot = time.time()

            # Ввод 2FA кода из code.txt
            if os.path.exists(code_file):
                try:
                    with open(code_file, "r", encoding="utf-8") as f:
                        code = f.read().strip()
                    if code:
                        print(f"[{time.strftime('%X')}] Получен 2FA код: {code}, вводим...")
                        page.keyboard.type(code, delay=100)
                        page.keyboard.press("Enter")
                        inputs = page.query_selector_all("input")
                        for inp in inputs:
                            try:
                                inp.fill(code)
                                inp.press("Enter")
                                break
                            except Exception:
                                pass
                        os.remove(code_file)
                        time.sleep(3)
                except Exception as e:
                    print(f"[{time.strftime('%X')}] Ошибка ввода кода: {e}")

            # Проверка авторизации через URL или сессионную куку
            cookies = context.cookies()
            remixsid = next((c['value'] for c in cookies if c['name'] == 'remixsid'), None)
            url = page.url
            if remixsid or "feed" in url or "im" in url or "id.vk.com/account" in url:
                print(f"[{time.strftime('%X')}] Успешная авторизация! (remixsid: {bool(remixsid)}, URL: {url})")
                try:
                    page.goto("https://vk.ru/im", wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                context.storage_state(path=SESSION_TEST_FILE)
                print(f"[{time.strftime('%X')}] Тестовая сессия сохранена в {SESSION_TEST_FILE}")
                browser.close()
                return True

        print(f"[{time.strftime('%X')}] Время ожидания авторизации истекло.")
        browser.close()
        return False


def do_sniff(duration_sec: int = 300):
    if not os.path.exists(SESSION_TEST_FILE):
        print(f"[ОШИБКА] Файл сессии {SESSION_TEST_FILE} не найден. Сначала выполните авторизацию (--auth).")
        return

    print(f"[{time.strftime('%X')}] Запуск сниффера трафика (длительность: {duration_sec} с)...")
    print(f"[{time.strftime('%X')}] Логи будут записываться в {TRAFFIC_LOG_FILE}")

    with sync_playwright() as p:
        browser, context = create_stealth_browser_and_context(p, storage_state=SESSION_TEST_FILE)
        page = context.new_page()

        # 1. Перехват WebSocket
        def on_websocket(ws):
            ws_url = ws.url
            print(f"[{time.strftime('%X')}] [WS CONNECT] {ws_url}")
            log_event("ws_connect", {"url": ws_url})

            def on_frame_received(payload):
                text = str(payload)
                short_text = text[:150] + "..." if len(text) > 150 else text
                print(f"[{time.strftime('%X')}] [WS RECV] {short_text}")
                log_event("ws_recv", {"url": ws_url, "payload": text})

            def on_frame_sent(payload):
                text = str(payload)
                short_text = text[:150] + "..." if len(text) > 150 else text
                print(f"[{time.strftime('%X')}] [WS SENT] {short_text}")
                log_event("ws_sent", {"url": ws_url, "payload": text})

            ws.on("framereceived", on_frame_received)
            ws.on("framesent", on_frame_sent)
            ws.on("close", lambda: log_event("ws_close", {"url": ws_url}))

        page.on("websocket", on_websocket)

        # 2. Перехват HTTP API и телеметрии
        def on_response(response):
            url = response.url
            # Фильтруем интересующие эндпоинты: API, очереди, телеметрию, авторизацию
            keywords = ["al_im.php", "queue", "api.vk.ru", "al_stat.php", "fast_push", "get_token"]
            if any(k in url for k in keywords):
                try:
                    status = response.status
                    content_type = response.headers.get("content-type", "")
                    body_snippet = ""
                    if "json" in content_type or "text" in content_type:
                        try:
                            body = response.text()
                            body_snippet = body[:300]
                        except Exception:
                            body_snippet = "[binary]"
                    print(f"[{time.strftime('%X')}] [HTTP {status}] {url[:80]} -> {body_snippet[:100]}")
                    log_event("http_response", {
                        "url": url,
                        "status": status,
                        "headers": dict(response.headers),
                        "snippet": body_snippet
                    })
                except Exception:
                    pass

        page.on("response", on_response)

        # Переход в мессенджер
        print(f"[{time.strftime('%X')}] Открытие https://vk.ru/im...")
        page.goto("https://vk.ru/im", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        handle_challenge(page)
        page.wait_for_timeout(3000)

        # Сбор внутренних глобальных переменных VK
        try:
            vk_info = page.evaluate("""() => {
                return {
                    vk_id: window.vk ? window.vk.id : null,
                    app_id: window.vk ? window.vk.app_id : null,
                    cur_keys: window.cur ? Object.keys(window.cur) : [],
                    has_prefetch: !!(window.cur && window.cur.apiPrefetchCache),
                    ls_keys: Object.keys(localStorage)
                };
            }""")
            print(f"[{time.strftime('%X')}] JS Environment: {json.dumps(vk_info, ensure_ascii=False)}")
            log_event("js_env", vk_info)
        except Exception as e:
            print(f"[{time.strftime('%X')}] Ошибка чтения JS объекта: {e}")

        # Снимок экрана мессенджера
        im_shot_path = os.path.join(BASE_DIR, "research", "im_sniff_state.png")
        page.screenshot(path=im_shot_path)
        artifact_dir = os.environ.get("ANTIGRAVITY_ARTIFACT_DIR", "/root/.gemini/antigravity-cli/brain/af0a733a-19bb-4731-a76a-086e95ca82ca")
        if os.path.isdir(artifact_dir):
            import shutil
            shutil.copy2(im_shot_path, os.path.join(artifact_dir, "im_sniff_state.png"))

        print(f"[{time.strftime('%X')}] Сниффер слушает трафик. Отправляй сообщения в тестовый аккаунт! Нажми Ctrl+C для выхода.")
        start_sniff = time.time()
        while time.time() - start_sniff < duration_sec:
            page.wait_for_timeout(1000)

        browser.close()
        print(f"[{time.strftime('%X')}] Сниффинг завершен. Все события сохранены в {TRAFFIC_LOG_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VK Web Traffic Sniffer & Protocol Reverse")
    parser.add_argument("--auth", action="store_true", help="Авторизоваться через QR в отдельную тестовую сессию")
    parser.add_argument("--sniff", action="store_true", help="Запустить перехват трафика и WebSocket")
    parser.add_argument("--duration", type=int, default=300, help="Длительность сниффинга в секундах")
    args = parser.parse_args()

    if args.auth:
        do_auth()
    elif args.sniff:
        do_sniff(args.duration)
    else:
        parser.print_help()
