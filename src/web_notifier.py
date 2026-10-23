import json
import os
import random
import re
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
import requests
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from src.stealth_browser import create_stealth_browser_and_context
except ModuleNotFoundError:
    from stealth_browser import create_stealth_browser_and_context

try:
    from src.human_activity import perform_in_tab_human_activity
except ModuleNotFoundError:
    from human_activity import perform_in_tab_human_activity
SESSION_FILE = os.path.join(BASE_DIR, os.getenv("VK_SESSION_FILE", "session.json"))
ENV_FILE = os.path.join(BASE_DIR, ".env")
CACHE_FILE = os.path.join(BASE_DIR, os.getenv("VK_CACHE_FILE", "seen_cache.json"))
CHROME_BIN = "/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome"

def load_seen_cache():
    """
    Загружает сохраненное состояние диалогов с диска.
    Поддерживает как новый формат {key: {"snippet": "...", "msg_id": 123}},
    так и устаревший строковый формат {key: "..."}.
    """
    if os.path.isfile(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                normalized = {}
                for k, v in data.items():
                    if isinstance(v, dict):
                        normalized[k] = {
                            "snippet": v.get("snippet", ""),
                            "msg_id": v.get("msg_id")
                        }
                    elif isinstance(v, str):
                        normalized[k] = {
                            "snippet": v,
                            "msg_id": None
                        }
                return normalized
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

def setup_dom_observer(page):
    """Устанавливает в браузере нативный MutationObserver для отслеживания изменений в чатах."""
    try:
        page.evaluate("""() => {
            if (window.__vk_observer_installed) return;
            window.__vk_observer_installed = true;
            window.__vk_dom_dirty = true;
            
            const markDirty = () => { window.__vk_dom_dirty = true; };
            const observer = new MutationObserver(markDirty);
            
            const target = document.body;
            if (target) {
                observer.observe(target, { childList: true, subtree: true, characterData: true });
            }
        }""")
    except Exception:
        pass

TYPING_COOLDOWN_SEC = 180  # Максимум 1 пуш о наборе текста раз в 3 минуты на диалог
EDIT_SUPPRESSION_WINDOW_SEC = 25  # Окно подавления быстрых правок опечаток (сек)
EDIT_SIMILARITY_THRESHOLD = 0.80  # Порог сходства текстов для распознавания опечатки
POLL_INTERVAL_MS = 600  # Интервал проверки мессенджера (мс) — ускоренная субсекундная доставка

# --- Режим сна (Night Sleep Mode) для защиты от антифрода 24/7 ---
SLEEP_MODE_ENABLED = os.getenv("SLEEP_MODE_ENABLED", "true").lower() in ("true", "1", "yes")
SLEEP_TIMEZONE = os.getenv("SLEEP_TIMEZONE", "Europe/Moscow")
SLEEP_START_STR = os.getenv("SLEEP_START", "02:00")  # Базовое начало ночного сна
SLEEP_END_STR = os.getenv("SLEEP_END", "07:30")      # Базовое окончание ночного сна
SLEEP_JITTER_MIN = int(os.getenv("SLEEP_JITTER_MIN", "20"))  # Плавающее смещение сна (+- минут)
SESSION_MAX_LIFETIME_SEC = int(os.getenv("SESSION_MAX_LIFETIME_SEC", str(8 * 3600)))  # Мягкий реконнект каждые 8 часов

# --- Дневной микро-перерыв (Day Break) для разрыва непрерывного онлайна ---
DAY_BREAK_ENABLED = os.getenv("DAY_BREAK_ENABLED", "true").lower() in ("true", "1", "yes")
DAY_BREAK_START_HOUR = int(os.getenv("DAY_BREAK_START_HOUR", "17"))  # Окно: с 17:00
DAY_BREAK_END_HOUR = int(os.getenv("DAY_BREAK_END_HOUR", "19"))      # Окно: до 19:00
DAY_BREAK_DURATION_SEC = int(os.getenv("DAY_BREAK_DURATION_SEC", "900"))  # Длительность перерыва: 15 минут

def parse_time_str(val: str, default_hour: int, default_min: int) -> dtime:
    try:
        parts = val.strip().split(":")
        return dtime(int(parts[0]), int(parts[1]))
    except Exception:
        return dtime(default_hour, default_min)

SLEEP_START_TIME = parse_time_str(SLEEP_START_STR, 2, 0)
SLEEP_END_TIME = parse_time_str(SLEEP_END_STR, 7, 30)

def is_in_sleep_window(dt: datetime = None) -> bool:
    """Проверяет, попадает ли текущее время в интервал ночного сна."""
    if not SLEEP_MODE_ENABLED:
        return False
    try:
        tz = ZoneInfo(SLEEP_TIMEZONE)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    now_dt = dt if dt else datetime.now(tz)
    cur_t = now_dt.time()
    if SLEEP_START_TIME < SLEEP_END_TIME:
        return SLEEP_START_TIME <= cur_t < SLEEP_END_TIME
    else:
        # Переход через полночь (например, 02:00 -> 07:30)
        return cur_t >= SLEEP_START_TIME or cur_t < SLEEP_END_TIME

def seconds_until_wake(dt: datetime = None, apply_jitter: bool = True) -> float:
    """Вычисляет, сколько секунд осталось спать до окончания окна сна с естественным джиттером."""
    try:
        tz = ZoneInfo(SLEEP_TIMEZONE)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    now_dt = dt if dt else datetime.now(tz)
    target = now_dt.replace(hour=SLEEP_END_TIME.hour, minute=SLEEP_END_TIME.minute, second=0, microsecond=0)
    if target <= now_dt:
        target = target.replace(day=target.day + 1)
    diff = (target - now_dt).total_seconds()
    if apply_jitter and SLEEP_JITTER_MIN > 0:
        diff += random.uniform(-SLEEP_JITTER_MIN * 60, SLEEP_JITTER_MIN * 60)
    return max(60.0, diff)

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
            const items = document.querySelectorAll("button.ConvoListItem, .ConvoListItem");
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

                if (textEl) {
                    snippet = getFullTextWithEmoji(textEl).trim();
                }
                
                if (!snippet && msgEl) {
                    const titleAttr = msgEl.getAttribute("title");
                    if (titleAttr) {
                        snippet = titleAttr.trim();
                    }
                }
                
                if (!snippet && item.querySelector("[class*='attach' i], [class*='sticker' i]")) {
                    snippet = "[Вложение/Стикер]";
                }

                if (author) {
                    const rawMsgId = item.getAttribute("data-msg-id") || item.getAttribute("data-last-msg-id");
                    const parsedMsgId = rawMsgId ? parseInt(rawMsgId, 10) : null;
                    convos.push({
                        peerId: peerId,
                        author: author,
                        snippet: snippet,
                        msgId: (!isNaN(parsedMsgId) && parsedMsgId) ? parsedMsgId : null
                    });
                }
            }

            // 3.1 Фоллбэк: если DOM диалогов в SPA-модели срендерен виртуально (скелетон),
            // читаем актуальные диалоги напрямую из памяти браузера (window.cur.apiPrefetchCache)
            if (convos.length === 0 && window.cur && window.cur.apiPrefetchCache) {
                const cache = window.cur.apiPrefetchCache;
                const list = Array.isArray(cache) ? cache : Object.values(cache);
                for (let entry of list) {
                    if (entry && entry.response && entry.response.conversations && entry.response.conversations.items) {
                        const profiles = entry.response.profiles || [];
                        const groups = entry.response.groups || [];
                        const profMap = {};
                        for (let p of profiles) profMap[p.id] = (p.first_name + ' ' + p.last_name).trim();
                        for (let g of groups) profMap[-g.id] = g.name;

                        function getAttachmentType(att) {
                            if (!att) return '[Вложение]';
                            const t = att.type;
                            if (t === 'sticker') return '[Стикер]';
                            if (t === 'doc') return '[Файл]';
                            if (t === 'photo') return '[Фотография]';
                            if (t === 'audio_message') return '[Голосовое сообщение]';
                            if (t === 'video' || t === 'video_message') return '[Видеозапись]';
                            if (t === 'audio') return '[Аудиозапись]';
                            if (t === 'wall') return '[Запись на стене]';
                            return '[Вложение]';
                        }

                        for (let it of entry.response.conversations.items) {
                            const c = it.conversation;
                            const m = it.last_message;
                            if (!c || !c.peer) continue;
                            const peerId = String(c.peer.id);
                            const isChat = c.peer.type === 'chat';
                            const chatTitle = (c.chat_settings && c.chat_settings.title) ? c.chat_settings.title : '';
                            const author = isChat ? (chatTitle || ('Беседа ' + peerId)) : (profMap[c.peer.id] || ('id' + peerId));
                            
                            let text = m ? (m.text || '') : '';
                            if (m && m.action) {
                                const act = m.action;
                                const actUser = profMap[act.member_id] || '';
                                if (act.type === 'chat_invite_user') text = 'пригласил ' + actUser;
                                else if (act.type === 'chat_kick_user') text = 'исключил ' + actUser;
                                else if (act.type === 'chat_title_update') text = 'изменил название чата на «' + (act.text || '') + '»';
                                else text = act.text || 'Действие в чате';
                            } else if (!text && m) {
                                if (m.attachments && m.attachments.length > 0) {
                                    if (m.attachments.length > 1) {
                                        text = m.attachments.length + ' вложения';
                                    } else {
                                        text = getAttachmentType(m.attachments[0]);
                                    }
                                } else if (m.fwd_messages && m.fwd_messages.length > 0) {
                                    const f = m.fwd_messages[0];
                                    if (f.attachments && f.attachments.length > 0) {
                                        text = getAttachmentType(f.attachments[0]);
                                    } else if (f.text) {
                                        text = f.text;
                                    } else {
                                        text = '[Пересланное сообщение]';
                                    }
                                }
                            }

                            if (isChat && m && m.from_id && text) {
                                const sender = profMap[m.from_id] || '';
                                if (sender && !text.startsWith(sender + ':')) {
                                    text = sender + ': ' + text;
                                }
                            }

                            convos.push({
                                peerId: peerId,
                                author: author,
                                snippet: text,
                                msgId: m ? m.id : null,
                                isOut: m ? (m.out === 1) : false
                            });
                        }
                        break;
                    }
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
    session_start_time = time.time()
    print(f"[{time.strftime('%X')}] [1/3] Запуск Google Chrome (Stealth / Windows / NVIDIA)...")
    with sync_playwright() as p:
        browser, context = create_stealth_browser_and_context(p, storage_state=SESSION_FILE)
        
        # Заглушаем шлюзы телеметрии (мягкий 200 OK без сбоев в UI)
        try:
            context.route("**/stats.vk-portal.net/**", lambda r: r.fulfill(status=200, content_type="application/json", body='{"result":true}'))
            context.route("**/sdk-api.apptracer.ru/**", lambda r: r.fulfill(status=200, content_type="application/json", body='{"result":true}'))
            context.route("**/stacks.vk-portal.net/**", lambda r: r.fulfill(status=200, content_type="application/json", body='{"result":true}'))
        except Exception:
            pass

        page = context.new_page()

        print(f"[{time.strftime('%X')}] [2/3] Открытие https://vk.ru/im...")
        page.goto("https://vk.ru/im", wait_until="domcontentloaded", timeout=45000)

        # Проходим challenge если вылез
        handle_challenge(page)
        page.wait_for_timeout(3000)
        handle_challenge(page)

        # Ждем загрузки диалогов (в DOM или в памяти SPA)
        for _ in range(10):
            st = extract_state(page)
            if st["convos"]:
                break
            page.wait_for_timeout(1500)
            handle_challenge(page)

        # Синтетическая смена видимости вкладки (сворачивание) для обнуления скора машинного присутствия
        try:
            page.evaluate("""() => {
                Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
                Object.defineProperty(document, 'hidden', { value: true, configurable: true });
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
        except Exception:
            pass

        print(f"[{time.strftime('%X')}] [3/3] Мессенджер загружен! Инициализация слушателя...")

        # Устанавливаем браузерный MutationObserver
        setup_dom_observer(page)

        # Снимаем исходное состояние
        initial_state = extract_state(page)
        # Если вдруг список еще не отрендерился, ждем еще 3 сек
        if not initial_state["convos"]:
            page.wait_for_timeout(3000)
            initial_state = extract_state(page)
        last_msg_count = initial_state["msgCount"]
        last_bell_count = initial_state["bellCount"]
        
        # Хранилище последних сообщений по ID диалога или автору: {key: {"snippet": ..., "msg_id": ...}}
        seen_dialogs = load_seen_cache()
        for d in initial_state["convos"]:
            key = d["peerId"] if d["peerId"] else d["author"]
            seen_dialogs[key] = {
                "snippet": clean_duplicate_author(d["snippet"]),
                "msg_id": d.get("msgId")
            }
        save_seen_cache(seen_dialogs)

        # Хранилище таймштампов последней отправки статуса 'печатает': {key: timestamp}
        last_typing_time = {}
        # Хранилище времени последнего отправленного пуша по входящему сообщению: {key: timestamp}
        last_incoming_push_time = {}
        # Время последнего снятия состояния для страховочного heartbeat
        last_state_extract_time = time.time()
        FORCE_EXTRACT_INTERVAL_SEC = 2.0  # Страховочный опрос раз в 2 секунды вместо 10

        # Таймер человеческой микро-активности внутри вкладки (раз в 4-7 часов)
        last_human_activity_time = time.time()
        next_human_activity_interval = random.uniform(4 * 3600, 7 * 3600)

        # Выбираем случайное время для дневного 15-минутного перерыва (17:00 - 19:00)
        day_break_scheduled_min = random.randint(0, 105) # от 17:00 до 18:45
        day_break_today_done = False

        print(f"[{time.strftime('%X')}] Текущее состояние: непрочитанных ЛС = {last_msg_count}, уведомлений = {last_bell_count}")
        print(f"[{time.strftime('%X')}] Актуальные верхние диалоги ({len(seen_dialogs)}):")
        for d in initial_state["convos"][:4]:
            print(f"  - {d['author']}: {clean_duplicate_author(d['snippet'])[:50]}")

        # Основной цикл отслеживания
        while True:
            page.wait_for_timeout(POLL_INTERVAL_MS)
            now = time.time()

            # 0. Проверяем окно ночного сна (Night Sleep Mode)
            if is_in_sleep_window():
                print(f"[{time.strftime('%X')}] 🌙 Наступило время ночного сна ({SLEEP_START_STR} - {SLEEP_END_STR} {SLEEP_TIMEZONE}). Закрытие браузера...")
                browser.close()
                return "SLEEP"

            # 0.05 Проверяем дневной 15-минутный перерыв (между 17:00 и 19:00 МСК)
            if DAY_BREAK_ENABLED:
                try:
                    cur_dt = datetime.now(ZoneInfo(SLEEP_TIMEZONE))
                    if cur_dt.hour == DAY_BREAK_START_HOUR and cur_dt.minute >= day_break_scheduled_min and not day_break_today_done:
                        print(f"[{time.strftime('%X')}] ☕ Дневной перерыв ({int(DAY_BREAK_DURATION_SEC/60)} мин, имитация ухода от ПК). Закрытие браузера...")
                        browser.close()
                        return "DAY_BREAK"
                    elif cur_dt.hour > DAY_BREAK_END_HOUR:
                        day_break_today_done = False  # Сброс флага на следующий день
                except Exception:
                    pass

            # 0.08 Периодическая человеческая микро-активность внутри вкладки (движение мыши, скролл диалогов)
            if now - last_human_activity_time >= next_human_activity_interval:
                last_human_activity_time = now
                next_human_activity_interval = random.uniform(4 * 3600, 7 * 3600)
                print(f"[{time.strftime('%X')}] 🖱️ Имитация микро-активности пользователя (плавное движение мыши и скролл диалогов)...")
                perform_in_tab_human_activity(page)

            # 0.1 Проверяем плановую ротацию сессии (каждые N часов)
            if SESSION_MAX_LIFETIME_SEC > 0 and (now - session_start_time >= SESSION_MAX_LIFETIME_SEC):
                print(f"[{time.strftime('%X')}] 🔄 Плановый мягкий рестарт сессии браузера (прошло {int((now - session_start_time)/3600)} ч.)...")
                browser.close()
                return "ROTATE"
            
            # 1. Проверяем, не перенаправило ли на блокировку/разморозку
            current_url = page.url
            if "blocked" in current_url or "id.vk.com/blocked" in current_url:
                print(f"[{time.strftime('%X')}] [КРИТИЧНО] Обнаружена страница блокировки VK: {current_url}")
                _do_send_bark("VK: Внимание!", "Страница VK заморожена или требует проверки!", current_url)
                print(f"[{time.strftime('%X')}] Остановка сервиса для защиты аккаунта (Circuit Breaker).")
                sys.exit(1)

            # Проверяем, не слетела ли сессия (редирект на страницу авторизации)
            try:
                page_title = page.title()
            except Exception:
                page_title = ""
            if "/?u=2" in current_url or "login" in current_url or "ВКонтакте | Добро пожаловать" in page_title:
                print(f"[{time.strftime('%X')}] [КРИТИЧНО] Сессия истекла (URL: {current_url}, title: '{page_title}').")
                _do_send_bark("VK: Сессия истекла", "Сессия VK истекла. Требуется повторный вход через QR-код!", "https://vk.ru/")
                print(f"[{time.strftime('%X')}] Остановка сервиса для предотвращения спама входа (Circuit Breaker).")
                sys.exit(1)

            # 2. Проверяем, не вылетел ли challenge
            if "challenge.html" in current_url or "mincyfry-cert" in current_url:
                handle_challenge(page)
                page.wait_for_timeout(2000)
                continue

            # Поддерживаем активность observer при перерисовках интерфейса
            setup_dom_observer(page)

            # 3. Быстрая проверка изменений через MutationObserver
            try:
                is_dirty = page.evaluate("() => { const d = window.__vk_dom_dirty; window.__vk_dom_dirty = false; return d; }")
            except Exception:
                is_dirty = True

            # Если изменений не было и не пришло время контрольного heartbeat — спим дальше
            if not is_dirty and (now - last_state_extract_time < FORCE_EXTRACT_INTERVAL_SEC):
                continue

            last_state_extract_time = now
            state = extract_state(page)
            current_msg_count = state["msgCount"]
            current_bell_count = state["bellCount"]
            current_convos = state["convos"]

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

                # 1.3 Реальные входящие сообщения с дедупликацией разметки, msg_id и правок
                new_msg_id = c.get("msgId")
                norm_snippet = normalize_snippet(snippet)
                
                cached_entry = seen_dialogs.get(key)
                if isinstance(cached_entry, dict):
                    old_raw = cached_entry.get("snippet", "")
                    old_msg_id = cached_entry.get("msg_id")
                elif isinstance(cached_entry, str):
                    old_raw = cached_entry
                    old_msg_id = None
                else:
                    old_raw = ""
                    old_msg_id = None

                old_norm = normalize_snippet(old_raw)

                if key not in seen_dialogs:
                    seen_dialogs[key] = {"snippet": snippet, "msg_id": new_msg_id}
                    save_seen_cache(seen_dialogs)
                    last_incoming_push_time[key] = now
                    print(f"[{time.strftime('%X')}] [НОВОЕ ЛС] Новый диалог: {author} -> {snippet}")
                    send_bark_push(f"VK: {author}", snippet)
                elif new_msg_id is not None and old_msg_id is not None and new_msg_id == old_msg_id:
                    # Идентификатор сообщения не изменился: изменение сниппета вызвано
                    # прочтением диалога или дребезгом разметки (например, 'Имя: Текст' <-> 'Текст').
                    if old_raw != snippet:
                        seen_dialogs[key] = {"snippet": snippet, "msg_id": new_msg_id}
                        save_seen_cache(seen_dialogs)
                elif old_norm != norm_snippet:
                    time_since_push = now - last_incoming_push_time.get(key, 0)
                    if time_since_push < EDIT_SUPPRESSION_WINDOW_SEC and is_minor_edit(old_norm, norm_snippet):
                        seen_dialogs[key] = {"snippet": snippet, "msg_id": new_msg_id}
                        save_seen_cache(seen_dialogs)
                        print(f"[{time.strftime('%X')}] [ПРАВКА ОПЕЧАТКИ] {author} исправил сообщение: \"{snippet}\" (пуш подавлен)")
                    else:
                        seen_dialogs[key] = {"snippet": snippet, "msg_id": new_msg_id}
                        save_seen_cache(seen_dialogs)
                        last_incoming_push_time[key] = now
                        print(f"[{time.strftime('%X')}] [НОВОЕ ЛС] {author} -> {snippet}")
                        send_bark_push(f"VK: {author}", snippet)
                elif old_raw != snippet or (new_msg_id and new_msg_id != old_msg_id):
                    # Текст нормализован одинаково, обновляем сохраненный сырой сниппет/msg_id
                    seen_dialogs[key] = {"snippet": snippet, "msg_id": new_msg_id}
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
            # 1. Проверяем, не находимся ли мы прямо сейчас в окне сна
            if is_in_sleep_window():
                sec_to_wait = seconds_until_wake()
                hours = round(sec_to_wait / 3600, 1)
                print(f"[{time.strftime('%X')}] 🌙 Режим ночного сна активен ({SLEEP_START_STR} - {SLEEP_END_STR} {SLEEP_TIMEZONE}). Спим ~{hours} ч. (браузер закрыт)...")
                time.sleep(sec_to_wait)
                print(f"[{time.strftime('%X')}] ☀️ Окно сна завершено. Пробуждение и запуск браузера...")
                time.sleep(random.uniform(5.0, 25.0))  # Естественная пауза перед пробуждением

            # 2. Запуск браузерной сессии
            result = run_messenger_listener()
            if result == "SLEEP":
                continue
            elif result == "DAY_BREAK":
                print(f"[{time.strftime('%X')}] ☕ Дневной перерыв на {int(DAY_BREAK_DURATION_SEC/60)} мин. Браузер выключен...")
                time.sleep(DAY_BREAK_DURATION_SEC)
                print(f"[{time.strftime('%X')}] ☀️ Дневной перерыв завершен. Возврат к работе...")
                time.sleep(random.uniform(5.0, 20.0))
                continue
            elif result == "ROTATE":
                pause = random.uniform(15.0, 45.0)
                print(f"[{time.strftime('%X')}] Пауза {int(pause)} сек перед перезапуском сессии...")
                time.sleep(pause)
                continue
        except (KeyboardInterrupt, SystemExit):
            print(f"[{time.strftime('%X')}] Остановка сервиса.")
            sys.exit(0)
        except Exception as e:
            print(f"[{time.strftime('%X')}] Ошибка в слушателе: {e}. Перезапуск через 30 секунд...")
            time.sleep(30)

if __name__ == "__main__":
    main()
