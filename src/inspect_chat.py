import os
import sys
import time
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_FILE = os.path.join(BASE_DIR, "session.json")

if not os.path.isfile(SESSION_FILE):
    print(f"Session file not found at {SESSION_FILE}. Run qr_auth.py first.")
    sys.exit(1)

search_target = sys.argv[1] if len(sys.argv) > 1 else ""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=['--ignore-certificate-errors', '--no-sandbox'])
    c = b.new_context(storage_state=SESSION_FILE, viewport={'width': 1280, 'height': 800})
    page = c.new_page()
    page.goto('https://vk.ru/im')
    time.sleep(3)
    
    for attempt in range(1, 10):
        btn = page.query_selector('button:has-text("Продолжить"), div[role="button"]:has-text("Продолжить")')
        if btn and btn.is_visible():
            btn.click()
            time.sleep(3)
        else:
            break
            
    time.sleep(5)
    print('Title:', page.title())
    print('URL:', page.url)
    
    elements = page.query_selector_all('*')
    found = []
    for el in elements:
        try:
            txt = el.inner_text().strip()
            if search_target and search_target in txt and len(txt) < 80:
                cls = el.get_attribute('class') or ''
                tag = el.evaluate('e => e.tagName')
                found.append((tag, cls, txt.replace('\n', ' -- ')))
        except Exception:
            pass

    print(f'Найдено совпадений: {len(found)}')
    for f in found:
        print(f)
    b.close()
