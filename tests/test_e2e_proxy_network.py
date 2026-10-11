import os
import sys
from urllib.parse import urlparse
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


class TestE2EProxyNetwork:
    """E2E интеграционные тесты сетевого контура и прокси-соединения."""

    @pytest.fixture(scope="class")
    def proxy_config(self):
        proxy_url = load_env_var("VK_PROXY")
        assert proxy_url, "VK_PROXY не задан в окружении или .env"
        parsed = urlparse(proxy_url)
        assert parsed.hostname, f"Не удалось распарсить хост прокси из {proxy_url}"
        assert parsed.port, f"Не удалось распарсить порт прокси из {proxy_url}"
        return {
            "raw_url": proxy_url,
            "expected_ip": parsed.hostname,
            "proxies": {
                "http": proxy_url,
                "https": proxy_url,
            },
        }

    def test_proxy_egress_ip_and_geo(self, proxy_config):
        """
        Проверяет egress IP через внешний чекер (ip-api.com).
        IP должен строго совпадать с хостом прокси (79.143.19.248), страна — RU.
        """
        resp = requests.get(
            "http://ip-api.com/json",
            proxies=proxy_config["proxies"],
            timeout=15,
        )
        assert resp.status_code == 200, f"ip-api.com вернул HTTP {resp.status_code}"
        data = resp.json()
        assert data.get("status") == "success", f"ip-api status failure: {data}"

        actual_ip = data.get("query")
        country_code = data.get("countryCode")

        assert actual_ip == proxy_config["expected_ip"], (
            f"Egress IP {actual_ip} не совпадает с ожидаемым прокси IP {proxy_config['expected_ip']}"
        )
        assert country_code == "RU", f"Egress страна {country_code} не RU (ожидалась Россия)"

    def test_vk_gateways_reachability(self, proxy_config):
        """
        Проверяет сетевой доступ к шлюзам vk.ru и api.vk.ru через прокси:
        отсутствие кодов 418, 429 или блокировок.
        """
        # 1. Проверка портала vk.ru
        vk_resp = requests.get(
            "https://vk.ru",
            proxies=proxy_config["proxies"],
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            },
        )
        assert vk_resp.status_code not in (418, 429, 403), (
            f"vk.ru заблокировал запрос через прокси с кодом {vk_resp.status_code}"
        )
        assert vk_resp.status_code == 200, f"vk.ru вернул статус {vk_resp.status_code}"

        # 2. Проверка шлюза api.vk.ru (шлюз доступен, возвращает 404/200 без 418/429/WAF блокировок)
        api_resp = requests.get(
            "https://api.vk.ru",
            proxies=proxy_config["proxies"],
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            },
        )
        assert api_resp.status_code not in (418, 429, 403), (
            f"api.vk.ru вернул код блокировки/rate-limit {api_resp.status_code}"
        )
