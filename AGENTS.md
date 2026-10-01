# vk-ios-notifier — правила для агентов

## Стек
- Язык: Python 3.10+ — `requirements.txt`
- Браузерная автоматизация: Playwright (Google Chrome `--headless=new` + `playwright-stealth`)
- Сеть / Пуши: Requests (HTTP API сервиса Bark)

## Команды
- Установка зависимостей: `pip install -r requirements.txt && playwright install chromium`
- Первичная авторизация (QR): `python3 src/qr_auth.py`
- Ручной запуск мониторинга: `python3 src/web_notifier.py`
- Управление systemd: `systemctl status|restart|stop|start vk-notifier`
- Просмотр логов сервиса: `journalctl -u vk-notifier -f`

## Структура
- `src/web_notifier.py` — основной сервис мониторинга диалогов VK Web через Playwright и отправки push в Bark.
- `src/qr_auth.py` — CLI-скрипт для генерации QR-кода и сохранения веб-сессии в `session.json`.
- `src/inspect_chat.py` — вспомогательная утилита для проверки селекторов и DOM-элементов страницы сообщений.
- `vk-notifier.service.example` — шаблон unit-файла systemd для фоновой работы сервиса.
- `.env.example` — шаблон конфигурации окружения (`BARK_KEY`).

## Правила и ограничения проекта
- Запрещено удалять из `.gitignore` или коммитить `session.json`, `seen_cache.json`, `.env`, `*.png`.
- Не переводить ядро проекта на библиотеку `vk_api`: сторонние токены VK блокируются сервером (`Flood Control: code 9`). Подробнее: [SYSTEM_INVARIANTS.md](SYSTEM_INVARIANTS.md).
- Все изменения в логике парсинга сообщений должны сохранять фильтрацию таймштампов, эмодзи и кулдаун статуса «печатает».

## Документация проекта: см. [INDEX.md](INDEX.md)
