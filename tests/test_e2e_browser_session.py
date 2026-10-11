import os
import sys
import pytest
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.stealth_browser import create_stealth_browser_and_context
from src.web_notifier import handle_challenge, extract_state, setup_dom_observer

SESSION_FILE = os.path.join(BASE_DIR, "session.json")


class TestE2EBrowserSession:
    """E2E тесты реальной браузерной сессии Playwright с активным аккаунтом VK."""

    @pytest.fixture(scope="class")
    def browser_session(self):
        assert os.path.isfile(SESSION_FILE), f"Файл сессии {SESSION_FILE} не найден"
        with sync_playwright() as p:
            browser, context = create_stealth_browser_and_context(
                p,
                storage_state=SESSION_FILE,
            )
            page = context.new_page()
            try:
                page.goto("https://vk.ru/im", wait_until="domcontentloaded", timeout=45000)
                handle_challenge(page)
                yield page
            finally:
                context.close()
                browser.close()

    def test_session_navigation_and_auth_integrity(self, browser_session):
        """
        Верификация навигации: URL строго в https://vk.ru/im,
        отсутствие редиректов на логин (/?u=2) и страниц блокировки/капчи.
        """
        page = browser_session
        current_url = page.url

        assert "vk.ru/im" in current_url, f"Неожиданный URL: {current_url}"
        assert "/?u=2" not in current_url, f"Редирект на логин (/?u=2): {current_url}"
        assert "login" not in current_url.lower(), f"Редирект на страницу авторизации: {current_url}"
        assert "blocked" not in current_url.lower(), f"Обнаружена блокировка: {current_url}"
        assert "challenge.html" not in current_url.lower(), f"Неразрешенный challenge: {current_url}"

        title = page.title()
        assert "ВКонтакте | Добро пожаловать" not in title, (
            f"Заголовок страницы указывает на форму входа: {title}"
        )

    def test_dom_elements_and_dialog_extraction(self, browser_session):
        """
        Поиск реальных элементов диалогов в DOM (button.ConvoListItem)
        и проверка работы extract_state (непустой список convos с авторами и сниппетами).
        """
        page = browser_session
        # Ждем рендера списка диалогов (с повторной проверкой challenge при задержке сети/прокси)
        try:
            page.wait_for_selector("button.ConvoListItem", timeout=25000)
        except Exception:
            handle_challenge(page)
            page.wait_for_timeout(2000)
            page.wait_for_selector("button.ConvoListItem", timeout=25000)

        items = page.query_selector_all("button.ConvoListItem")
        assert len(items) > 0, "Не найдены элементы диалогов button.ConvoListItem"

        state = extract_state(page)
        assert isinstance(state, dict), f"extract_state вернул не dict: {type(state)}"
        assert "convos" in state, "Ключ 'convos' отсутствует в ответе extract_state"

        convos = state["convos"]
        assert len(convos) > 0, f"extract_state вернул пустой список диалогов: {state}"

        first = convos[0]
        assert "author" in first and first["author"].strip(), f"Диалог без автора: {first}"
        assert "snippet" in first, f"Диалог без сниппета: {first}"

    def test_mutation_observer_triggers_on_dom_change(self, browser_session):
        """
        Проверка MutationObserver: создание тестового DOM-элемента
        в контейнере диалогов и проверка сработки флага window.__vk_dom_dirty == True.
        """
        page = browser_session
        setup_dom_observer(page)

        # Выполняем сброс флага и вставку тестового узла в одной атомарной транзакции evaluate
        result = page.evaluate("""() => {
            window.__vk_dom_dirty = false;
            const wasDirtyBefore = window.__vk_dom_dirty;

            const target = document.querySelector("#im_dialogs") ||
                           document.querySelector("main") ||
                           document.body;
            const probe = document.createElement("div");
            probe.className = "test-e2e-observer-probe";
            probe.innerText = "e2e-probe";
            target.appendChild(probe);

            return { wasDirtyBefore: wasDirtyBefore };
        }""")

        assert result["wasDirtyBefore"] is False, "Флаг __vk_dom_dirty не был сброшен в false"

        # Даем MutationObserver микротаск на вызов callback
        page.wait_for_timeout(200)

        is_dirty_after = page.evaluate("() => window.__vk_dom_dirty")
        assert is_dirty_after is True, (
            "MutationObserver не взвел флаг window.__vk_dom_dirty при модификации DOM"
        )

