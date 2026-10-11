# vk-ios-notifier — правила для агентов

## Стек
- Язык: Python 3.10+ — `requirements.txt`
- Браузерная автоматизация: Playwright (Google Chrome `--headless=new` + `playwright-stealth`)
- Сеть / Пуши: Requests (HTTP API сервиса Bark)

## Команды
- Установка зависимостей: `pip install -r requirements.txt && playwright install chromium`
- Запуск тестов (E2E и интеграционные): `pytest`
- Первичная авторизация (QR): `python3 src/qr_auth.py`
- Ручной запуск мониторинга (Playwright): `python3 src/web_notifier.py`
- Ручной запуск мониторинга (LongPoll ~20 МБ): `python3 -m longpoll_client.notifier_daemon`
- Управление systemd: `systemctl status|restart|stop|start vk-notifier`
- Просмотр логов сервиса: `journalctl -u vk-notifier -f`

## Структура
- `src/stealth_browser.py` — единый модуль антидетекта: спуфинг Windows 10, WebGL NVIDIA, таймзоны Europe/Moscow и поддержка `VK_PROXY` (HTTP/SOCKS5 с аутентификацией).
- `src/web_notifier.py` — основной сервис мониторинга диалогов VK Web через Playwright, MutationObserver, детект истечения сессии и отправка push в Bark.
- `src/qr_auth.py` — CLI-скрипт для генерации QR-кода и сохранения веб-сессии в `session.json`.
- `longpoll_client/` — альтернативный легковесный модуль Web LongPoll (aiohttp ~20 МБ RAM без постоянного браузера).
- `tests/` — комплекс сквозных E2E-тестов (прокси, гео-чекер IP, Bark шлюз, живая сессия Playwright, дедупликация кэша).
- `vk-notifier.service.example` — шаблон unit-файла systemd для фоновой работы сервиса.
- `.env.example` — шаблон конфигурации окружения (`BARK_KEY`, `VK_PROXY`).

## Правила и ограничения проекта
- Запрещено удалять из `.gitignore` или коммитить `session.json`, `seen_cache.json`, `.env`, `*.png`.
- Любой запуск Chromium (`qr_auth.py`, `web_notifier.py`) обязан использовать `create_stealth_browser_and_context` из `src/stealth_browser.py` для сохранения единого фингерпринта.
- Не переводить ядро проекта на библиотеку `vk_api`: сторонние токены VK блокируются сервером (`Flood Control: code 9`). Подробнее: [SYSTEM_INVARIANTS.md](SYSTEM_INVARIANTS.md).
- Все изменения в логике парсинга сообщений должны сохранять фильтрацию таймштампов, эмодзи и кулдаун статуса «печатает».

## Документация проекта: см. [INDEX.md](INDEX.md)
