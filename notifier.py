import os
import sys
import time
import requests
import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "vk_config.v2.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")

def load_credentials():
    """Загружает учетные данные из окружения или .env."""
    vk_token = os.getenv("VK_TOKEN", "").strip()
    bark_key = os.getenv("BARK_KEY", "").strip()
    remixsid = os.getenv("REMIXSID", "").strip()

    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "VK_TOKEN" and not vk_token and v:
                        vk_token = v
                    elif k == "BARK_KEY" and not bark_key and v:
                        bark_key = v
                    elif k == "REMIXSID" and not remixsid and v:
                        remixsid = v

    return vk_token, bark_key, remixsid

VK_TOKEN, BARK_KEY, REMIXSID = load_credentials()

if not BARK_KEY:
    print("[ОШИБКА] Не задан BARK_KEY в .env!")
    sys.exit(1)

def get_vk_session():
    """Инициализирует сессию VK (из vk_config.v2.json, токена или remixsid)."""
    # 1. Если есть сохраненная сессия после auth.py
    if os.path.isfile(CONFIG_FILE):
        print("[INFO] Используется сохраненная сессия из vk_config.v2.json")
        session = vk_api.VkApi(
            config_filename=CONFIG_FILE,
            app_id=6222115,
            scope=1073737727
        )
        session.auth(token_only=True)
        return session

    # 2. Если задан токен
    if VK_TOKEN:
        print("[INFO] Используется токен VK_TOKEN")
        return vk_api.VkApi(token=VK_TOKEN)

    print("[ОШИБКА] Нет активной авторизации VK.")
    print("Запустите сначала: python3 auth.py")
    sys.exit(1)

def send_bark_notification(sender_name: str, message_text: str, peer_id: int):
    url = f"https://api.day.app/{BARK_KEY}/"
    preview = message_text if message_text.strip() else "[Медиа / Стикер / Вложение]"
    payload = {
        "title": f"VK: {sender_name}",
        "body": preview,
        "group": "VK",
        "icon": "https://vk.com/images/svg_icons/ic_head_logo.svg",
        "url": f"https://vk.com/im?sel={peer_id}"
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        res.raise_for_status()
    except Exception as e:
        print(f"[{time.strftime('%X')}] Ошибка отправки в Bark: {e}")

def get_user_name(vk_session, user_id: int) -> str:
    try:
        users = vk_session.method("users.get", {"user_ids": user_id})
        if users:
            return f"{users[0].get('first_name', '')} {users[0].get('last_name', '')}".strip()
    except Exception:
        pass
    return f"Пользователь {user_id}"

def run_listener():
    session = get_vk_session()
    user = session.method("users.get")[0]
    print(f"[{time.strftime('%X')}] Авторизован как: {user.get('first_name')} {user.get('last_name')} (ID: {user.get('id')})")
    
    longpoll = VkLongPoll(session)
    print(f"[{time.strftime('%X')}] LongPoll слушатель активен. Ожидание сообщений...")

    for event in longpoll.listen():
        if event.type == VkEventType.MESSAGE_NEW and not event.from_me:
            sender = get_user_name(session, event.user_id)
            print(f"[{time.strftime('%X')}] Сообщение от {sender}: {event.text[:40] if event.text else '[вложение]'}")
            send_bark_notification(sender, event.text, event.peer_id)

def main():
    while True:
        try:
            run_listener()
        except Exception as err:
            print(f"[{time.strftime('%X')}] Соединение разорвано ({err}). Переподключение через 5 сек...")
            time.sleep(5)

if __name__ == "__main__":
    main()
