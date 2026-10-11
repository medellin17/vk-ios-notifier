import os
import sys
import time
import pytest
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

ENV_FILE = os.path.join(BASE_DIR, ".env")


def load_env_var(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val and os.path.isfile(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    break
    return val


class TestE2EBarkGateway:
    """E2E интеграционные тесты шлюза Bark Push на реальном iOS устройстве."""

    @pytest.fixture(scope="class")
    def bark_key(self):
        key = load_env_var("BARK_KEY")
        assert key, "BARK_KEY не найден в окружении или в .env"
        return key

    def test_bark_push_delivery(self, bark_key):
        """
        Проверка отправки реального тестового пуша в Bark API через ключ из .env.
        Валидация HTTP-статуса 200 и ответа {'code': 200, 'message': 'success'}.
        """
        api_url = f"https://api.day.app/{bark_key}/"
        payload = {
            "title": "E2E Test: vk-ios-notifier",
            "body": f"Тестовый пуш интеграционного контура [{time.strftime('%X')}]",
            "group": "VK-E2E",
            "icon": "https://vk.com/images/svg_icons/ic_head_logo.svg",
            "url": "https://vk.ru/im",
            "sound": "calypso",
        }

        resp = requests.post(api_url, json=payload, timeout=10)
        assert resp.status_code == 200, f"Bark API вернул не 200: {resp.status_code} ({resp.text})"

        data = resp.json()
        assert isinstance(data, dict), f"Ожидался JSON-словарь, получено {type(data)}"
        assert data.get("code") == 200, f"Код в ответе Bark: {data.get('code')}, ожидался 200"
        assert data.get("message") == "success", (
            f"Сообщение от Bark API: {data.get('message')}, ожидалось 'success'"
        )
