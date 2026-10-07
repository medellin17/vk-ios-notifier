import os
from typing import Optional, Tuple
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(BASE_DIR, ".env")
CHROME_BIN = "/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

EXTRA_STEALTH_JS = """
(() => {
    try {
        // 1. Аппаратные характеристики десктопного ПК на Windows
        Object.defineProperty(Object.getPrototypeOf(navigator), 'hardwareConcurrency', { get: () => 8 });
        Object.defineProperty(Object.getPrototypeOf(navigator), 'deviceMemory', { get: () => 8 });
        Object.defineProperty(Object.getPrototypeOf(navigator), 'platform', { get: () => 'Win32' });

        // 2. Параметры экрана Windows 10/11 с таскбаром (1040px)
        Object.defineProperty(window.screen, 'width', { get: () => 1920 });
        Object.defineProperty(window.screen, 'height', { get: () => 1080 });
        Object.defineProperty(window.screen, 'availWidth', { get: () => 1920 });
        Object.defineProperty(window.screen, 'availHeight', { get: () => 1040 });
        Object.defineProperty(window.screen, 'colorDepth', { get: () => 24 });
        Object.defineProperty(window.screen, 'pixelDepth', { get: () => 24 });

        // 3. Валидный объект window.chrome
        if (!window.chrome) {
            window.chrome = {};
        }
        if (!window.chrome.runtime) {
            window.chrome.runtime = {
                OnInstalledReason: {},
                OnRestartRequiredReason: {},
                PlatformArch: {},
                PlatformNaclArch: {},
                PlatformOs: {},
                RequestUpdateCheckStatus: {}
            };
        }
        if (!window.chrome.app) {
            window.chrome.app = {
                isInstalled: false,
                InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
            };
        }
        if (!window.chrome.csi) {
            window.chrome.csi = function() {};
        }
        if (!window.chrome.loadTimes) {
            window.chrome.loadTimes = function() {};
        }
    } catch(e) {}
})();
"""

def get_proxy_config() -> Optional[dict]:
    """Загружает настройки прокси из .env при наличии (VK_PROXY=http://user:pass@ip:port)."""
    proxy_url = os.getenv("VK_PROXY", "").strip()
    if not proxy_url and os.path.isfile(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VK_PROXY="):
                        proxy_url = line.split("=", 1)[1].strip().strip("'\"")
                        break
        except Exception:
            pass
    if proxy_url:
        return {"server": proxy_url}
    return None

def create_stealth_browser_and_context(
    playwright_instance,
    storage_state: Optional[str] = None
) -> Tuple[Browser, BrowserContext]:
    """
    Создает изолированный браузерный контекст с глубоким антидетектом:
    - Спуфинг платформы Windows 10 (Win32, 1920x1080, Moscow TZ)
    - Подмена WebGL рендерера с SwiftShader/Linux на NVIDIA GeForce GTX 1660
    - Единый фингерпринт для авторизации и рантайма
    - Опциональная маршрутизация через VK_PROXY
    """
    executable_path = CHROME_BIN if os.path.isfile(CHROME_BIN) else None
    proxy_config = get_proxy_config()

    browser = playwright_instance.chromium.launch(
        executable_path=executable_path,
        headless=True,
        proxy=proxy_config,
        args=[
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1920,1080",
            "--ignore-certificate-errors",
            "--lang=ru-RU,ru",
        ]
    )

    stealth = Stealth(
        navigator_platform_override="Win32",
        navigator_user_agent_override=DEFAULT_USER_AGENT,
        navigator_languages_override=("ru-RU", "ru", "en-US", "en"),
        webgl_vendor_override="Google Inc. (NVIDIA)",
        webgl_renderer_override="ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        chrome_runtime=True
    )

    context_kwargs = {
        "user_agent": DEFAULT_USER_AGENT,
        "locale": "ru-RU",
        "timezone_id": "Europe/Moscow",
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 1,
    }
    if storage_state and os.path.isfile(storage_state):
        context_kwargs["storage_state"] = storage_state

    context = browser.new_context(**context_kwargs)
    stealth.apply_stealth_sync(context)
    context.add_init_script(EXTRA_STEALTH_JS)

    return browser, context
