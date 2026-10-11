# VK Web LongPoll Notifier (Легковесный клиент)

Альтернативный асинхронный демон для мониторинга личных сообщений и бесед ВКонтакте с мгновенной отправкой push-уведомлений в [Bark](https://github.com/Finb/Bark) для iOS.

Реализован согласно архитектурному предложению [LIGHTWEIGHT_LONGPOLL_RFC.md](../.context/LIGHTWEIGHT_LONGPOLL_RFC.md).

---

## 1. Сравнение потребления ресурсов

В отличие от стандартного демона на Chromium Playwright, данный модуль **не держит браузер в постоянном рантайме**. Браузер запускается только один раз (на 10–20 секунд) для генерации или извлечения сессии, после чего полностью выгружается из памяти.

| Параметр | Playwright Chromium (`src/web_notifier.py`) | Web LongPoll (`longpoll_client/`) |
|---|---|---|
| **Потребление RAM** | ~450–536 МБ | **~18–25 МБ** (сокращение в 25 раз) |
| **Потребление CPU** | ~1.0–2.5% (MutationObserver + тики Chrome) | **~0.01%** (event-driven socket) |
| **Фоновые процессы** | Python + Node.js + 8–12 процессов Chrome | **1 процесс Python** |
| **Сетевой протокол** | Рендеринг DOM-дерева в headless-вкладке | Чистый HTTP LongPoll (`a_check` сокет) |
| **Время доставки пуша** | 1.0–2.0 сек | **0.1–0.3 сек** (мгновенно по сокету) |

---

## 2. Архитектура и компоненты

```
/root/vk-ios-notifier/longpoll_client/
├── __init__.py           # Экспорт интерфейсов модуля
├── config.py             # Загрузка .env, путей, фингерпринта и кэшей
├── auth.py               # Автономный скрипт получения/обновления учетных данных (токен app_id 6287487)
├── longpoll_transport.py # Асинхронный сетевой транспорт на aiohttp (с поддержкой aiohttp-socks и прокси)
├── notifier_daemon.py    # Основной цикл демона: опрос LongPoll v3, фильтрация исходящих, отправка в Bark
├── README.md             # Документация модуля
├── credentials.json      # Защищенный файл учетных данных (в .gitignore)
└── seen_cache.json       # Локальный кэш виденных сообщений для защиты от дубликатов
```

### Принцип работы:
1. **Токен веб-клиента**: Используется нативный токен официального веб-интерфейса VK (app_id `6287487`).
2. **Бесшовное авто-обновление (Token Refresh Loop)**:
   - Сессионный токен живет ровно 24 часа.
   - Раз в 20 часов демон автоматически делает один HTTPS-запрос к веб-шлюзу VK (`/al_im.php?act=get_token`) с долгоживущей кукой `remixsid`, обновляя токен без разлогина и без запуска браузера.
3. **Разбор пакетов LongPoll v3**:
   - Код `4` — новое сообщение. Фильтруется исходящий трафик пользователя (`flags & 2 == 0`).
   - Коды `61` и `62` — статус набора текста («печатает...») с настраиваемым кулдауном (180 сек).
   - Защита от дублей и подавление быстрых правок опечаток (<25 сек, сходство >= 80%).

---

## 3. Требования и окружение

Модуль использует существующие зависимости проекта:
- Python 3.10+
- `aiohttp >= 3.9`
- `aiohttp_socks` (опционально, для SOCKS5 прокси)
- `requests >= 2.28`
- `playwright` (только для первичного разового извлечения сессии)

Файл `.env` в корне проекта:
```ini
BARK_KEY=your_bark_device_key
# Опционально:
VK_PROXY=socks5://user:pass@ip:port
REMIXSID=your_remixsid_cookie
VK_TOKEN=your_web_access_token
```

---

## 4. Запуск и управление

### Шаг 1. Первичное получение / проверка учетных данных
```bash
# Проверить статус текущего токена
python3 -m longpoll_client.auth --check

# Извлечь учетные данные через разовый Playwright (10-15 сек)
python3 -m longpoll_client.auth --browser

# Принудительно обновить токен через HTTP-шлюз
python3 -m longpoll_client.auth --refresh
```

### Шаг 2. Запуск основного демона
```bash
# Прямой запуск в терминале
python3 -m longpoll_client.notifier_daemon
```

### Шаг 3. Запуск как systemd-сервис
Для перевода проекта на легковесный режим отредактируйте сервис:
```ini
[Unit]
Description=VK to iOS Push Notifier (Lightweight LongPoll)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/vk-ios-notifier
ExecStart=/usr/bin/python3 -m longpoll_client.notifier_daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Перезапуск службы:
```bash
systemctl daemon-reload
systemctl restart vk-notifier
```

---

## 5. Безопасность и защита от антифрода

- **Windows/Chrome Headers**: Все сетевые запросы транспорта содержат идентичный пул HTTP-заголовков (`Sec-Ch-Ua`, `User-Agent: Chrome/126 on Windows 10`, `Accept-Language: ru-RU`).
- **Секреты**: Файлы `credentials.json`, `session.json`, `seen_cache.json` добавлены в `.gitignore` и имеют строгие файловые права (`0600`).
- **Мониторинг истечения сессии**: При получении ошибки `VKAuthError` (код 5) демон отправляет пуш в Bark с предупреждением о необходимости обновления сессии и пытается обновить токен через `remixsid`.
