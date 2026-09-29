import json
import os
import re
import sys
import time
import requests
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_FILE = os.path.join(BASE_DIR, "session.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")
CACHE_FILE = os.path.join(BASE_DIR, "seen_cache.json")

def load_seen_cache():
    """Загружает сохраненное состояние диалогов с диска."""
    if os.path.isfile(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_seen_cache(seen_dict):
    """Сохраняет состояние диалогов на диск для защиты от дублей при перезапуске."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(seen_dict, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_bark_key():
    bark_key = os.getenv("BARK_KEY", "")
    if not bark_key and os.path.isfile(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("BARK_KEY="):
                    bark_key = line.split("=", 1)[1].strip().strip("'\"")
    return bark_key

BARK_KEY = load_bark_key()

if not BARK_KEY:
    print("[ОШИБКА] BARK_KEY не найден в .env")
    sys.exit(1)

def send_bark_push(title: str, text: str, url: str = "https://vk.ru/im"):
    """Отправляет push-уведомление в Bark на iPhone."""
    api_url = f"https://api.day.app/{BARK_KEY}/"
    payload = {
        "title": title,
        "body": text if text else "[Новое сообщение ВКонтакте]",
        "group": "VK",
        "icon": "https://vk.com/images/svg_icons/ic_head_logo.svg",
        "url": url,
        "sound": "calypso"
    }
    try:
        res = requests.post(api_url, json=payload, timeout=5)
        res.raise_for_status()
        print(f"[{time.strftime('%X')}] 🔔 Пуш отправлен в Bark: {title} | {text}")
    except Exception as e:
        print(f"[{time.strftime('%X')}] [!] Ошибка отправки в Bark: {e}")

def handle_challenge(page):
    """Прокликивает плашку 'Проверяем, что вы не робот' / сертификатов Минцифры."""
    try:
        for _ in range(5):
            if "challenge.html" in page.url or "mincyfry-cert" in page.url or "робот" in page.title():
                btn = page.query_selector('button:has-text("Продолжить"), div[role="button"]:has-text("Продолжить")')
                if btn and btn.is_visible():
                    print(f"[{time.strftime('%X')}] Проклик плашки 'Продолжить'...")
                    btn.click()
                    page.wait_for_timeout(3000)
                else:
                    page.wait_for_timeout(1000)
            else:
                break
    except Exception:
        pass

TYPING_COOLDOWN_SEC = 180  # Максимум 1 пуш о наборе текста раз в 3 минуты на диалог

def is_typing_status(text: str) -> bool:
    """Проверяет, показывает ли диалог статус набора текста."""
    if not text:
        return False
    lower = text.strip().lower()
    return lower.startswith("печатает") or lower.startswith("набирает")

def is_ignored_snippet(text: str) -> bool:
    """Фильтрует таймштампы и исходящие сообщения."""
    if not text or not text.strip():
        return True
    clean = text.strip()
    lower = clean.lower()
    
    # 1. Исходящие сообщения
    if clean.startswith("Вы:"):
        return True
        
    # 2. Относительное время / таймштампы в дате
    if clean.startswith("·"):
        return True
    if re.search(r"^\d+\s*(минут|мин|сек|секунд|час|ч|дн|дней)", lower):
        return True
    if lower.endswith("назад") or "только что" in lower:
        return True
        
    return False

def extract_state(page):
    """
    Извлекает из DOM:
    1. Общий счетчик непрочитанных сообщений в левом меню (data-testid='leftmenuitem-counter')
    2. Счетчик колокольчика уведомлений в шапке
    3. Список актуальных диалогов с корректным распознаванием эмодзи и вложений
    """
    try:
        return page.evaluate("""() => {
            // 1. Счетчик сообщений в левом меню
            const msgCounterEl = document.querySelector("#l_msg [data-testid='leftmenuitem-counter']");
            let msgCount = 0;
            if (msgCounterEl) {
                const num = parseInt(msgCounterEl.innerText.trim(), 10);
                if (!isNaN(num)) msgCount = num;
            }

            // 2. Счетчик уведомлений (колокольчик) в шапке
            let bellCount = 0;
            const topBadges = document.querySelectorAll("header [class*='badge' i], header [class*='counter' i]");
            for (let b of topBadges) {
                const num = parseInt(b.innerText.trim(), 10);
                if (!isNaN(num) && num > 0) {
                    bellCount = Math.max(bellCount, num);
                }
            }

            // 3. Список диалогов
            const convos = [];
            const items = document.querySelectorAll("button.ConvoListItem");
            for (let item of items) {
                const peerId = item.getAttribute("data-peer-id") || "";
                const titleEl = item.querySelector(".ConvoTitle__title, .ConvoTitle__author, h3");
                const author = titleEl ? titleEl.innerText.trim() : "";
                
                const msgEl = item.querySelector(".ConvoListItem__message");
                const textEl = item.querySelector(".ConvoListItem__text");
                
                let snippet = "";
                
                // Извлекаем текст из ConvoListItem__text с подстановкой alt для картинок-эмодзи
                if (textEl) {
                    let content = "";
                    for (let node of textEl.childNodes) {
                        if (node.nodeType === Node.TEXT_NODE) {
                            content += node.textContent;
                        } else if (node.nodeType === Node.ELEMENT_NODE) {
                            if (node.tagName === "IMG" && node.getAttribute("alt")) {
                                content += node.getAttribute("alt");
                            } else {
                                const innerImgs = node.querySelectorAll("img[alt]");
                                if (innerImgs.length > 0) {
                                    innerImgs.forEach(im => { content += (im.getAttribute("alt") || ""); });
                                } else {
                                    content += node.innerText || "";
                                }
                            }
                        }
                    }
                    snippet = content.trim();
                }
                
                // Если текст пуст (например, одиночный эмодзи), проверяем title у ConvoListItem__message (title="👌🏻")
                if (!snippet && msgEl) {
                    const titleAttr = msgEl.getAttribute("title");
                    if (titleAttr) {
                        snippet = titleAttr.trim();
                    }
                }
                
                // Проверяем вложения
                if (!snippet && item.querySelector("[class*='attach' i], [class*='sticker' i]")) {
                    snippet = "[Вложение/Стикер]";
                }

                if (author) {
                    convos.push({
                        peerId: peerId,
                        author: author,
                        snippet: snippet
                    });
                }
            }

            return {
                msgCount: msgCount,
                bellCount: bellCount,
                convos: convos.slice(0, 10)
            };
        }""")
    except Exception:
        return {"msgCount": 0, "bellCount": 0, "convos": []}

def run_messenger_listener():
    print(f"[{time.strftime('%X')}] [1/3] Запуск Chromium Playwright...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--ignore-certificate-errors",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--no-zygote",
                "--renderer-process-limit=1",
                "--disable-shared-workers",
                "--disable-audio",
                "--mute-audio"
            ]
        )
        context = browser.new_context(
            storage_state=SESSION_FILE,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ru-RU",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        # Блокируем загрузку тяжелых медиа, шрифтов и трекеров для экономии RAM и CPU
        def block_heavy_media(route):
            url = route.request.url.lower()
            blocked_trackers = ["mail.ru/tracker", "counter", "target.my.com", "google-analytics", "mc.yandex"]
            if any(t in url for t in blocked_trackers):
                route.abort()
                return
            if any(ext in url for ext in [".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".ogg"]):
                if "data:" not in url:
                    route.abort()
                    return
            route.continue_()

        page.route("**/*", block_heavy_media)

        print(f"[{time.strftime('%X')}] [2/3] Открытие https://vk.ru/im...")
        page.goto("https://vk.ru/im")

        # Проходим challenge если вылез
        handle_challenge(page)
        page.wait_for_timeout(3000)
        handle_challenge(page)

        # Ждем загрузки элементов диалогов
        try:
            page.wait_for_selector("button.ConvoListItem", timeout=20000)
            page.wait_for_timeout(3000)
        except Exception:
            handle_challenge(page)
            page.wait_for_selector("button.ConvoListItem", timeout=15000)
            page.wait_for_timeout(3000)

        print(f"[{time.strftime('%X')}] [3/3] Мессенджер загружен! Инициализация слушателя...")

        # Снимаем исходное состояние
        initial_state = extract_state(page)
        # Если вдруг список еще не отрендерился, ждем еще 3 сек
        if not initial_state["convos"]:
            page.wait_for_timeout(3000)
            initial_state = extract_state(page)
        last_msg_count = initial_state["msgCount"]
        last_bell_count = initial_state["bellCount"]
        
        # Хранилище последних сообщений по ID диалога или автору: {key: snippet}
        # Загружаем постоянный кэш с диска для защиты от ложных пушей при рестартах
        seen_dialogs = load_seen_cache()
        for d in initial_state["convos"]:
            key = d["peerId"] if d["peerId"] else d["author"]
            if key not in seen_dialogs:
                seen_dialogs[key] = d["snippet"]
        save_seen_cache(seen_dialogs)

        # Хранилище таймштампов последней отправки статуса 'печатает': {key: timestamp}
        last_typing_time = {}

        print(f"[{time.strftime('%X')}] Текущее состояние: непрочитанных ЛС = {last_msg_count}, уведомлений = {last_bell_count}")
        print(f"[{time.strftime('%X')}] Актуальные верхние диалоги ({len(seen_dialogs)}):")
        for d in initial_state["convos"][:4]:
            print(f"  - {d['author']}: {d['snippet'][:50]}")

        # Основной цикл отслеживания
        while True:
            page.wait_for_timeout(2500)
            
            # Проверяем, не вылетел ли challenge
            if "challenge.html" in page.url or "mincyfry-cert" in page.url:
                handle_challenge(page)
                page.wait_for_timeout(2000)
                continue

            state = extract_state(page)
            current_msg_count = state["msgCount"]
            current_bell_count = state["bellCount"]
            current_convos = state["convos"]

            now = time.time()

            # 1. Проверяем новые сообщения и статус печати в диалогах
            for c in current_convos:
                key = c["peerId"] if c["peerId"] else c["author"]
                author = c["author"]
                snippet = c["snippet"]

                # 1.1 Обработка статуса 'печатает' с ограничением по частоте (кулдаун)
                if is_typing_status(snippet):
                    if now - last_typing_time.get(key, 0) >= TYPING_COOLDOWN_SEC:
                        last_typing_time[key] = now
                        print(f"[{time.strftime('%X')}] [ПЕЧАТАЕТ] {author} набирает сообщение...")
                        send_bark_push(f"VK: {author}", "печатает...")
                    continue

                # 1.2 Игнорируем таймштампы '· 5м' и исходящие 'Вы: ...'
                if is_ignored_snippet(snippet):
                    continue

                # 1.3 Реальные входящие сообщения
                if key not in seen_dialogs:
                    seen_dialogs[key] = snippet
                    save_seen_cache(seen_dialogs)
                    print(f"[{time.strftime('%X')}] [НОВОЕ ЛС] Новый диалог: {author} -> {snippet}")
                    send_bark_push(f"VK: {author}", snippet)
                elif seen_dialogs[key] != snippet:
                    seen_dialogs[key] = snippet
                    save_seen_cache(seen_dialogs)
                    print(f"[{time.strftime('%X')}] [НОВОЕ ЛС] {author} -> {snippet}")
                    send_bark_push(f"VK: {author}", snippet)

            # 2. Проверяем рост общего счетчика непрочитанных ЛС (страховка)
            if current_msg_count > last_msg_count:
                print(f"[{time.strftime('%X')}] Счетчик непрочитанных вырос: {last_msg_count} -> {current_msg_count}")
                last_msg_count = current_msg_count
            elif current_msg_count < last_msg_count:
                last_msg_count = current_msg_count

            # 3. Проверяем колокольчик уведомлений
            if current_bell_count > last_bell_count:
                print(f"[{time.strftime('%X')}] Новое уведомление (колокольчик)! Всего: {current_bell_count}")
                send_bark_push("VK: Уведомление", f"Новое уведомление в ВКонтакте (всего: {current_bell_count})", "https://vk.ru/feed?section=notifications")
            last_bell_count = current_bell_count

def main():
    while True:
        try:
            run_messenger_listener()
        except Exception as e:
            print(f"[{time.strftime('%X')}] Исключение в слушателе: {e}. Перезапуск через 5 сек...")
            time.sleep(5)

if __name__ == "__main__":
    main()
