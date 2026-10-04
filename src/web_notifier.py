import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_FILE = os.path.join(BASE_DIR, "session.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")
CACHE_FILE = os.path.join(BASE_DIR, "seen_cache.json")
CHROME_BIN = "/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome"

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

bark_session = requests.Session()
bark_executor = ThreadPoolExecutor(max_workers=2)

def _do_send_bark(title: str, text: str, url: str):
    api_url = f"https://api.day.app/{BARK_KEY}/"
    payload = {
        "title": title,
        "body": text if text else "[Новое сообщение ВКонтакте]",
        "group": "VK",
        "icon": "https://vk.com/images/svg_icons/ic_head_logo.svg",
        "url": url,
        "sound": "calypso",
        "level": "timeSensitive"
    }
    try:
        res = bark_session.post(api_url, json=payload, timeout=5)
        res.raise_for_status()
        print(f"[{time.strftime('%X')}] 🔔 Пуш отправлен в Bark: {title} | {text}")
    except Exception as e:
        print(f"[{time.strftime('%X')}] [!] Ошибка отправки в Bark: {e}")

def send_bark_push(title: str, text: str, url: str = "https://vk.ru/im"):
    """Отправляет push-уведомление в Bark на iPhone асинхронно через постоянное keep-alive соединение."""
    bark_executor.submit(_do_send_bark, title, text, url)

def handle_challenge(page):
    """Прокликивает плашку 'Проверяем, что вы не робот' / сертификатов Минцифры."""
    try:
        if "blocked" in page.url:
            print(f"[{time.strftime('%X')}] [ВНИМАНИЕ] Обнаружена страница блокировки/разморозки VK: {page.url}")
        for _ in range(5):
            if "challenge.html" in page.url or "mincyfry-cert" in page.url or "робот" in page.title():
                page.evaluate("""() => {
                    const el = document.getElementsByClassName("start")[0];
                    if (el) el.click();
                }""")
                btn = page.query_selector('button:has-text("Продолжить"), div[role="button"]:has-text("Продолжить"), .start')
                if btn and btn.is_visible():
                    print(f"[{time.strftime('%X')}] Проклик плашки 'Продолжить'...")
                    btn.click()
                page.wait_for_timeout(3000)
            else:
                break
    except Exception:
        pass

def simulate_user_activity(page):
    """Имитирует естественные движения курсора и микро-скролл для обхода поведенческого скоринга."""
    try:
        x = random.randint(250, 750)
        y = random.randint(150, 550)
        page.mouse.move(x, y, steps=random.randint(4, 8))
        if random.random() < 0.25:
            delta = random.choice([30, -30, 50, -50])
            page.mouse.wheel(0, delta)
            page.wait_for_timeout(150)
            page.mouse.wheel(0, -delta)
    except Exception:
        pass

TYPING_COOLDOWN_SEC = 180  # Максимум 1 пуш о наборе текста раз в 3 минуты на диалог
EDIT_SUPPRESSION_WINDOW_SEC = 25  # Окно подавления быстрых правок опечаток (сек)
EDIT_SIMILARITY_THRESHOLD = 0.80  # Порог сходства текстов для распознавания опечатки
POLL_INTERVAL_MS = 600  # Интервал проверки мессенджера (мс) — ускоренная доставка

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
    # 1. 'Имя И: Имя Фамилия: ...' -> 'Имя Фамилия: ...'
    s = re.sub(r"^([^\s:]{1,30})\s+[^\s:]{1,3}\.?:\s*(\1\s+[^:\n]{1,35}):\s*", r"\2: ", text)
    # 2. 'Лина M: Лина M: ...' -> 'Лина M: ...', 'Вы: Вы: ...' -> 'Вы: ...'
    s = re.sub(r"^([^:\n]{1,40}):\s*\1:\s*", r"\1: ", s)
    return s.strip()

def normalize_snippet(text: str) -> str:
    """
    Нормализует сниппет сообщения для устранения дребезга разметки VK.
    VK в веб-клиенте периодически оборачивает тип вложения в квадратные скобки ([Файл] <-> Файл).
    """
    if not text:
        return ""
    clean = clean_duplicate_author(text)
    clean = re.sub(r"\s+", " ", clean.replace("\u00a0", " ")).strip()
    return re.sub(
        r"\[(Файл|Фотография|Стикер|Голосовое сообщение|Видеозапись|Аудиозапись|Запись на стене|Вложение[^\]]*)\]",
        r"\1",
        clean,
        flags=re.IGNORECASE
    )

def is_minor_edit(old_text: str, new_text: str) -> bool:
    """Определяет, является ли изменение текста исправлением опечатки."""
    if not old_text or not new_text:
        return False
    if old_text == new_text:
        return True
    return SequenceMatcher(None, old_text, new_text).ratio() >= EDIT_SIMILARITY_THRESHOLD

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
                
                function getFullTextWithEmoji(root) {
                    if (!root) return "";
                    let out = "";
                    for (let child of root.childNodes) {
                        if (child.nodeType === Node.TEXT_NODE) {
                            out += child.textContent;
                        } else if (child.nodeType === Node.ELEMENT_NODE) {
                            if (child.tagName === "IMG" && child.getAttribute("alt")) {
                                out += child.getAttribute("alt");
                            } else {
                                out += getFullTextWithEmoji(child);
                            }
                        }
                    }
                    return out;
                }

                // Извлекаем полный текст из ConvoListItem__text с сохранением текста и эмодзи
                if (textEl) {
                    snippet = getFullTextWithEmoji(textEl).trim();
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
    print(f"[{time.strftime('%X')}] [1/3] Запуск Google Chrome (Stealth / --headless=new)...")
    with sync_playwright() as p:
        executable_path = CHROME_BIN if os.path.isfile(CHROME_BIN) else None
        browser = p.chromium.launch(
            executable_path=executable_path,
            headless=True,
            args=[
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,800",
                "--ignore-certificate-errors",
            ]
        )
        context = browser.new_context(
            storage_state=SESSION_FILE,
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ru-RU",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)

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
        # При старте актуализируем все текущие диалоги как базовое состояние,
        # чтобы на ребуте ни в коем случае не прилетали старые сообщения
        for d in initial_state["convos"]:
            key = d["peerId"] if d["peerId"] else d["author"]
            seen_dialogs[key] = clean_duplicate_author(d["snippet"])
        save_seen_cache(seen_dialogs)

        # Хранилище таймштампов последней отправки статуса 'печатает': {key: timestamp}
        last_typing_time = {}
        # Хранилище времени последнего отправленного пуша по входящему сообщению: {key: timestamp}
        last_incoming_push_time = {}
        # Хранилище времени последней имитации активности пользователя
        last_activity_time = time.time()
        ACTIVITY_INTERVAL_SEC = 120  # Имитация активности раз в 2 минуты

        print(f"[{time.strftime('%X')}] Текущее состояние: непрочитанных ЛС = {last_msg_count}, уведомлений = {last_bell_count}")
        print(f"[{time.strftime('%X')}] Актуальные верхние диалоги ({len(seen_dialogs)}):")
        for d in initial_state["convos"][:4]:
            print(f"  - {d['author']}: {clean_duplicate_author(d['snippet'])[:50]}")

        # Основной цикл отслеживания
        while True:
            page.wait_for_timeout(POLL_INTERVAL_MS)
            
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

            # Фоновая естественная активность (движение курсора, микро-скролл)
            if now - last_activity_time >= ACTIVITY_INTERVAL_SEC:
                last_activity_time = now
                simulate_user_activity(page)

            # 1. Проверяем новые сообщения и статус печати в диалогах
            for c in current_convos:
                key = c["peerId"] if c["peerId"] else c["author"]
                author = c["author"]
                snippet = clean_duplicate_author(c["snippet"])

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

                # 1.3 Реальные входящие сообщения с дедупликацией разметки и правок
                norm_snippet = normalize_snippet(snippet)
                old_raw = seen_dialogs.get(key, "")
                old_norm = normalize_snippet(old_raw)

                if key not in seen_dialogs:
                    seen_dialogs[key] = snippet
                    save_seen_cache(seen_dialogs)
                    last_incoming_push_time[key] = now
                    print(f"[{time.strftime('%X')}] [НОВОЕ ЛС] Новый диалог: {author} -> {snippet}")
                    send_bark_push(f"VK: {author}", snippet)
                elif old_norm != norm_snippet:
                    time_since_push = now - last_incoming_push_time.get(key, 0)
                    if time_since_push < EDIT_SUPPRESSION_WINDOW_SEC and is_minor_edit(old_norm, norm_snippet):
                        seen_dialogs[key] = snippet
                        save_seen_cache(seen_dialogs)
                        print(f"[{time.strftime('%X')}] [ПРАВКА ОПЕЧАТКИ] {author} исправил сообщение: \"{snippet}\" (пуш подавлен)")
                    else:
                        seen_dialogs[key] = snippet
                        save_seen_cache(seen_dialogs)
                        last_incoming_push_time[key] = now
                        print(f"[{time.strftime('%X')}] [НОВОЕ ЛС] {author} -> {snippet}")
                        send_bark_push(f"VK: {author}", snippet)
                elif old_raw != snippet:
                    # Текст после нормализации идентичен (например, дребезг [Файл] <-> Файл)
                    seen_dialogs[key] = snippet
                    save_seen_cache(seen_dialogs)

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
