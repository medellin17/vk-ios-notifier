import json
import os
import sys
import tempfile
import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import src.web_notifier as web_notifier
from src.web_notifier import (
    clean_duplicate_author,
    normalize_snippet,
    is_minor_edit,
    EDIT_SUPPRESSION_WINDOW_SEC,
    EDIT_SIMILARITY_THRESHOLD,
)


class TestE2ECacheDedup:
    """E2E тесты жизненного цикла кэша и алгоритма дедупликации сообщений."""

    @pytest.fixture
    def isolated_cache_file(self):
        """Создает временный файл кэша и подменяет CACHE_FILE в модуле web_notifier."""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp_path = tmp.name
        tmp.close()
        original_cache_file = web_notifier.CACHE_FILE
        web_notifier.CACHE_FILE = tmp_path
        try:
            yield tmp_path
        finally:
            web_notifier.CACHE_FILE = original_cache_file
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)

    def test_cache_save_and_load_cycle(self, isolated_cache_file):
        """
        Проверка сохранения и повторной загрузки кэша с диска:
        проверка сериализации JSON и сохранения структуры {key: {'snippet': ..., 'msg_id': ...}}.
        """
        initial_cache = {
            "peer_100": {"snippet": "Привет, как дела?", "msg_id": 1001},
            "peer_200": {"snippet": "Встреча в 18:00", "msg_id": 1002},
        }
        web_notifier.save_seen_cache(initial_cache)

        assert os.path.isfile(isolated_cache_file), "Файл кэша не был создан на диске"

        loaded_cache = web_notifier.load_seen_cache()
        assert loaded_cache == initial_cache, (
            f"Загруженный кэш {loaded_cache} не совпадает с исходным {initial_cache}"
        )

    def test_dedup_full_lifecycle_pipeline(self, isolated_cache_file):
        """
        Сквозная проверка пайплайна дедупликации:
        1. Первое сообщение -> фиксация в seen_cache.json -> отправка пуша.
        2. Повторное сообщение (дубль) -> подавление.
        3. Быстрая правка опечатки (<25 сек, similarity >= 0.8) -> подавление.
        4. Прочтение диалога (смена сниппета при том же msg_id) -> подавление (без повторного пуша!).
        5. Принципиально новое сообщение -> пропуск (отправка нового пуша).
        """
        seen_dialogs = web_notifier.load_seen_cache()
        last_incoming_push_time = {}
        push_events = []

        def mock_push(author: str, text: str):
            push_events.append((author, text))

        def process_incoming_convo(peer_id: str, author: str, raw_snippet: str, msg_id: int, current_time: float):
            snippet = clean_duplicate_author(raw_snippet)
            norm_snippet = normalize_snippet(snippet)
            cached_entry = seen_dialogs.get(peer_id)
            if isinstance(cached_entry, dict):
                old_raw = cached_entry.get("snippet", "")
                old_msg_id = cached_entry.get("msg_id")
            else:
                old_raw = cached_entry or ""
                old_msg_id = None
            old_norm = normalize_snippet(old_raw)

            if peer_id not in seen_dialogs:
                # 1. Первое сообщение
                seen_dialogs[peer_id] = {"snippet": snippet, "msg_id": msg_id}
                web_notifier.save_seen_cache(seen_dialogs)
                last_incoming_push_time[peer_id] = current_time
                mock_push(author, snippet)
                return "NEW_DIALOG"
            elif msg_id is not None and old_msg_id is not None and msg_id == old_msg_id:
                # 2. Прочтение диалога / дребезг разметки при неизменном msg_id
                if old_raw != snippet:
                    seen_dialogs[peer_id] = {"snippet": snippet, "msg_id": msg_id}
                    web_notifier.save_seen_cache(seen_dialogs)
                    return "READ_STATUS_UPDATED"
                return "DUPLICATE_SUPPRESSED"
            elif old_norm != norm_snippet:
                time_since_push = current_time - last_incoming_push_time.get(peer_id, 0)
                if time_since_push < EDIT_SUPPRESSION_WINDOW_SEC and is_minor_edit(old_norm, norm_snippet):
                    # 3. Быстрая правка опечатки
                    seen_dialogs[peer_id] = {"snippet": snippet, "msg_id": msg_id}
                    web_notifier.save_seen_cache(seen_dialogs)
                    return "TYPO_SUPPRESSED"
                else:
                    # 4. Принципиально новое сообщение
                    seen_dialogs[peer_id] = {"snippet": snippet, "msg_id": msg_id}
                    web_notifier.save_seen_cache(seen_dialogs)
                    last_incoming_push_time[peer_id] = current_time
                    mock_push(author, snippet)
                    return "NEW_MESSAGE"
            elif old_raw != snippet or (msg_id and msg_id != old_msg_id):
                seen_dialogs[peer_id] = {"snippet": snippet, "msg_id": msg_id}
                web_notifier.save_seen_cache(seen_dialogs)
                return "NORMALIZATION_UPDATED"
            else:
                return "DUPLICATE_SUPPRESSED"

        # Шаг 1: Первое входящее сообщение
        t0 = 1000.0
        status_1 = process_incoming_convo("peer_42", "Алексей Смирнов", "Привет! Ты сегодня свободен?", 501, t0)
        assert status_1 == "NEW_DIALOG"
        assert len(push_events) == 1
        assert push_events[-1] == ("Алексей Смирнов", "Привет! Ты сегодня свободен?")

        # Проверяем сохранение на диске
        disk_cache_1 = web_notifier.load_seen_cache()
        assert disk_cache_1.get("peer_42") == {"snippet": "Привет! Ты сегодня свободен?", "msg_id": 501}

        # Шаг 2: Повторный опрос (полный дубль)
        t1 = t0 + 2.0
        status_2 = process_incoming_convo("peer_42", "Алексей Смирнов", "Привет! Ты сегодня свободен?", 501, t1)
        assert status_2 == "DUPLICATE_SUPPRESSED"
        assert len(push_events) == 1

        # Шаг 3: Прочтение диалога пользователем!
        # В DOM/кеше может измениться сниппет (например, дребезг разметки или вложений [Файл] <-> Файл),
        # но msg_id остался 501. Должно быть строго подавлено без пуша!
        t2 = t0 + 8.0
        status_read = process_incoming_convo("peer_42", "Алексей Смирнов", "Привет! Ты сегодня свободен?", 501, t2)
        assert status_read == "DUPLICATE_SUPPRESSED"
        assert len(push_events) == 1, "При прочтении диалога пуш ни в коем случае не должен дублироваться!"

        # Кейс 3.1: Изменение сниппета при прочтении (тот же msg_id 501, но другой текст/разметка)
        status_read_change = process_incoming_convo("peer_42", "Алексей Смирнов", "Алексей: Привет! Ты сегодня свободен?", 501, t2 + 1.0)
        assert status_read_change == "READ_STATUS_UPDATED"
        assert len(push_events) == 1, "Смена сниппета при неизменном msg_id не должна вызывать пуш!"

        # Шаг 4: Быстрая правка опечатки с новым msg_id в пределах окна (<25 сек, схожесть > 0.8)
        t3 = t0 + 15.0
        typo_fixed = "Алексей: Привет! Ты сегодня свободен?!"
        status_3 = process_incoming_convo("peer_42", "Алексей Смирнов", typo_fixed, 502, t3)
        assert status_3 == "TYPO_SUPPRESSED"
        assert len(push_events) == 1

        # Шаг 5: Принципиально новое сообщение (новое входящее ЛС)
        t4 = t0 + 35.0
        new_msg = "Пойдем обедать в столовую на первом этаже"
        status_4 = process_incoming_convo("peer_42", "Алексей Смирнов", new_msg, 503, t4)
        assert status_4 == "NEW_MESSAGE"
        assert len(push_events) == 2
        assert push_events[-1] == ("Алексей Смирнов", new_msg)

        # Проверяем финальное состояние на диске
        disk_cache_final = web_notifier.load_seen_cache()
        assert disk_cache_final.get("peer_42") == {"snippet": new_msg, "msg_id": 503}
