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
        проверка сериализации JSON и сохранения структуры {key: snippet}.
        """
        initial_cache = {
            "peer_100": "Привет, как дела?",
            "peer_200": "Встреча в 18:00",
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
        4. Принципиально новое сообщение -> пропуск (отправка нового пуша).
        """
        seen_dialogs = web_notifier.load_seen_cache()
        last_incoming_push_time = {}
        push_events = []

        def mock_push(author: str, text: str):
            push_events.append((author, text))

        def process_incoming_convo(peer_id: str, author: str, raw_snippet: str, current_time: float):
            snippet = clean_duplicate_author(raw_snippet)
            norm_snippet = normalize_snippet(snippet)
            old_raw = seen_dialogs.get(peer_id, "")
            old_norm = normalize_snippet(old_raw)

            if peer_id not in seen_dialogs:
                # 1. Первое сообщение
                seen_dialogs[peer_id] = snippet
                web_notifier.save_seen_cache(seen_dialogs)
                last_incoming_push_time[peer_id] = current_time
                mock_push(author, snippet)
                return "NEW_DIALOG"
            elif old_norm != norm_snippet:
                time_since_push = current_time - last_incoming_push_time.get(peer_id, 0)
                if time_since_push < EDIT_SUPPRESSION_WINDOW_SEC and is_minor_edit(old_norm, norm_snippet):
                    # 3. Быстрая правка опечатки
                    seen_dialogs[peer_id] = snippet
                    web_notifier.save_seen_cache(seen_dialogs)
                    return "TYPO_SUPPRESSED"
                else:
                    # 4. Принципиально новое сообщение (или время окна истекло)
                    seen_dialogs[peer_id] = snippet
                    web_notifier.save_seen_cache(seen_dialogs)
                    last_incoming_push_time[peer_id] = current_time
                    mock_push(author, snippet)
                    return "NEW_MESSAGE"
            elif old_raw != snippet:
                # Дребезг нормализации ([Файл] <-> Файл)
                seen_dialogs[peer_id] = snippet
                web_notifier.save_seen_cache(seen_dialogs)
                return "NORMALIZATION_UPDATED"
            else:
                # 2. Полный дубль
                return "DUPLICATE_SUPPRESSED"

        # Шаг 1: Первое сообщение
        t0 = 1000.0
        status_1 = process_incoming_convo("peer_42", "Алексей Смирнов", "Привет! Ты сегодня свободен?", t0)
        assert status_1 == "NEW_DIALOG"
        assert len(push_events) == 1
        assert push_events[-1] == ("Алексей Смирнов", "Привет! Ты сегодня свободен?")

        # Проверяем, что состояние сохранилось на диске
        disk_cache_1 = web_notifier.load_seen_cache()
        assert disk_cache_1.get("peer_42") == "Привет! Ты сегодня свободен?"

        # Шаг 2: Повторное сообщение (дубль при том же опросе или через 2 сек)
        t1 = t0 + 2.0
        status_2 = process_incoming_convo("peer_42", "Алексей Смирнов", "Привет! Ты сегодня свободен?", t1)
        assert status_2 == "DUPLICATE_SUPPRESSED"
        assert len(push_events) == 1, "Дубль не должен генерировать пуш"

        # Шаг 3: Правка опечатки в течение окна 25 сек (через 10 сек, схожесть > 0.8)
        # "Привет! Ты севодня свободен?" -> "Привет! Ты сегодня свободен?"
        t2 = t0 + 10.0
        typo_fixed = "Привет! Ты сегодня свободен?!"  # добавился восклицательный знак
        status_3 = process_incoming_convo("peer_42", "Алексей Смирнов", typo_fixed, t2)
        assert status_3 == "TYPO_SUPPRESSED"
        assert len(push_events) == 1, "Правка опечатки должна быть подавлена"

        # Проверяем, что кэш на диске обновился новым текстом
        disk_cache_3 = web_notifier.load_seen_cache()
        assert disk_cache_3.get("peer_42") == typo_fixed

        # Шаг 4: Принципиально новое сообщение (через 30 сек)
        t3 = t0 + 30.0
        new_msg = "Пойдем обедать в столовую на первом этаже"
        status_4 = process_incoming_convo("peer_42", "Алексей Смирнов", new_msg, t3)
        assert status_4 == "NEW_MESSAGE"
        assert len(push_events) == 2, "Принципиально новое сообщение обязано вызвать пуш"
        assert push_events[-1] == ("Алексей Смирнов", new_msg)

        # Проверяем итоговое сохранение на диске
        disk_cache_4 = web_notifier.load_seen_cache()
        assert disk_cache_4.get("peer_42") == new_msg
