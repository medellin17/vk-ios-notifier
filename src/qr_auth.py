import os
import sys
import time
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QR_IMG_LOCAL = os.path.join(BASE_DIR, "qr.png")
SESSION_FILE = os.path.join(BASE_DIR, "session.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")
CODE_FILE = os.path.join(BASE_DIR, "code.txt")

def handle_challenge(page):
    """Безопасно прокликивает 'Продолжить', обрабатывая навигации страниц."""
    for attempt in range(1, 20):
        try:
            btn = page.query_selector('button:has-text("Продолжить"), div[role="button"]:has-text("Продолжить")')
            if btn and btn.is_visible():
                print(f"[Challenge {attempt}] Клик по 'Продолжить'...")
                btn.click()
                time.sleep(2.5)
            else:
                break
        except Exception:
            time.sleep(1)
            continue

def main():
    print("[1/4] Запуск браузера...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ru-RU",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        print("[2/4] Загрузка страницы входа VK...")
        try:
            page.goto("https://vk.com/", wait_until="domcontentloaded")
        except Exception:
            pass
        time.sleep(2)

        # Прокликиваем капчи при входе
        handle_challenge(page)
        time.sleep(2)
        handle_challenge(page)

        print("[3/4] Ожидание отрисовки QR-кода...")
        for _ in range(20):
            handle_challenge(page)
            try:
                has_qr = page.evaluate("""() => {
                    const media = document.querySelectorAll('svg, canvas, img');
                    return media.length > 5;
                }""")
                if has_qr:
                    break
            except Exception:
                pass
            time.sleep(1)

        time.sleep(2)
        try:
            page.screenshot(path=QR_IMG_LOCAL)
        except Exception:
            pass

        print(f"\n" + "="*60)
        print(f"СВЕЖИЙ QR-КОД ГОТОВ: {QR_IMG_LOCAL}")
        print("="*60 + "\n")
        print("[4/4] Ожидание сканирования с телефона (таймаут 120 сек)...")

        start = time.time()
        success = False
        last_screenshot = time.time()

        while time.time() - start < 120:
            time.sleep(1.5)
            handle_challenge(page)

            if time.time() - last_screenshot > 2.5:
                try:
                    page.screenshot(path=QR_IMG_LOCAL)
                    last_screenshot = time.time()
                except Exception:
                    pass

            # Ввод кода из code.txt если появился input
            if os.path.exists(CODE_FILE):
                try:
                    with open(CODE_FILE, "r", encoding="utf-8") as f:
                        code = f.read().strip()
                    if code:
                        inputs = page.query_selector_all("input")
                        for inp in inputs:
                            inp.fill(code)
                            page.keyboard.press("Enter")
                            print(f"[INFO] Введен код {code}")
                            break
                        os.remove(CODE_FILE)
                        time.sleep(3)
                except Exception as e:
                    print(f"Ошибка ввода кода: {e}")

            try:
                cookies = context.cookies()
                remixsid = next((c['value'] for c in cookies if c['name'] == 'remixsid'), None)

                if remixsid or "feed" in page.url or "id.vk.com/account" in page.url or "im" in page.url:
                    print("\n ВХОД УСПЕШНО ВЫПОЛНЕН!")
                    context.storage_state(path=SESSION_FILE)
                    print(f"Сессия сохранена в {SESSION_FILE}")

                    if remixsid and os.path.exists(ENV_FILE):
                        with open(ENV_FILE, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                        with open(ENV_FILE, "w", encoding="utf-8") as f:
                            has_remixsid = False
                            for line in lines:
                                if line.startswith("REMIXSID="):
                                    f.write(f"REMIXSID={remixsid}\n")
                                    has_remixsid = True
                                else:
                                    f.write(line)
                            if not has_remixsid:
                                f.write(f"REMIXSID={remixsid}\n")
                        print(f"Кука remixsid сохранена в .env")

                    success = True
                    break
            except Exception:
                pass

        if not success:
            print("\n[!] Время ожидания истекло.")
            browser.close()
            sys.exit(1)

        browser.close()

if __name__ == "__main__":
    main()
