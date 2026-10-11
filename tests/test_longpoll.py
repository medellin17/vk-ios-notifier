import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from longpoll_client.notifier_daemon import (
    clean_duplicate_author,
    format_attachment_snippet,
    is_ignored_snippet,
    is_minor_edit,
    is_typing_status,
    normalize_snippet,
    parse_longpoll_message,
    parse_typing_event,
)


class TestMessageCleaning(unittest.TestCase):
    def test_clean_duplicate_author(self):
        # 1. 'Имя И: Имя Фамилия: ...' -> 'Имя Фамилия: ...'
        raw = "Анжелика Б: Анжелика Бородина: Привет всем"
        self.assertEqual(clean_duplicate_author(raw), "Анжелика Бородина: Привет всем")

        # 2. 'Лина M: Лина M: ...' -> 'Лина M: ...'
        raw2 = "Лина M: Лина M: Привет"
        self.assertEqual(clean_duplicate_author(raw2), "Лина M: Привет")

        # 3. 'Вы: Вы: ...' -> 'Вы: ...'
        raw3 = "Вы: Вы: Мое сообщение"
        self.assertEqual(clean_duplicate_author(raw3), "Вы: Мое сообщение")

        # 4. Обычный автор без дублирования
        raw4 = "Иван Иванов: Тестовое сообщение"
        self.assertEqual(clean_duplicate_author(raw4), "Иван Иванов: Тестовое сообщение")

    def test_normalize_snippet(self):
        self.assertEqual(normalize_snippet("[Файл]"), "Файл")
        self.assertEqual(normalize_snippet("[Фотография]"), "Фотография")
        self.assertEqual(normalize_snippet("[Стикер]"), "Стикер")
        self.assertEqual(normalize_snippet("[Голосовое сообщение]"), "Голосовое сообщение")
        self.assertEqual(normalize_snippet("[Видеозапись]"), "Видеозапись")

        # Проверка устранения неразрывных пробелов
        self.assertEqual(normalize_snippet("Текст\u00a0с\u00a0пробелами"), "Текст с пробелами")

    def test_is_minor_edit(self):
        old = "Привет, как твои дела?"
        new_typo_fix = "Привет, как твои дела?!"
        self.assertTrue(is_minor_edit(old, new_typo_fix))

        completely_different = "Пойдем сегодня в кино в восемь вечера"
        self.assertFalse(is_minor_edit(old, completely_different))

    def test_is_typing_status(self):
        self.assertTrue(is_typing_status("печатает..."))
        self.assertTrue(is_typing_status("Печатает"))
        self.assertTrue(is_typing_status("набирает сообщение..."))
        self.assertFalse(is_typing_status("Привет, как дела?"))
        self.assertFalse(is_typing_status(""))

    def test_is_ignored_snippet(self):
        # Исходящие
        self.assertTrue(is_ignored_snippet("Вы: Привет"))
        self.assertTrue(is_ignored_snippet("Вы: Вы: тест"))

        # Относительные таймштампы
        self.assertTrue(is_ignored_snippet("· 5м"))
        self.assertTrue(is_ignored_snippet("5 минут назад"))
        self.assertTrue(is_ignored_snippet("только что"))
        self.assertTrue(is_ignored_snippet(""))

        # Валидное входящее
        self.assertFalse(is_ignored_snippet("Привет!"))
        self.assertFalse(is_ignored_snippet("Встречаемся завтра в 10:00"))


class TestLongPollEventParsing(unittest.TestCase):
    def test_parse_incoming_message(self):
        # [4, msg_id, flags, peer_id, timestamp, text, extra, attachments]
        # flags: 1 (UNREAD) -> incoming
        raw_event = [
            4,
            12345,
            1,
            447977046,
            1710000000,
            "Привет, как дела?",
            {"from": "447977046"},
            {},
        ]
        res = parse_longpoll_message(raw_event)
        self.assertIsNotNone(res)
        self.assertEqual(res["message_id"], 12345)
        self.assertEqual(res["peer_id"], 447977046)
        self.assertEqual(res["text"], "Привет, как дела?")
        self.assertFalse(res["is_outbox"])

    def test_parse_outgoing_message_filtered(self):
        # flags: 2 (OUTBOX) -> flags & 2 != 0
        raw_event = [
            4,
            12346,
            2,
            447977046,
            1710000005,
            "Мой ответ",
            {},
            {},
        ]
        res = parse_longpoll_message(raw_event)
        self.assertIsNotNone(res)
        self.assertTrue(res["is_outbox"])

    def test_parse_group_chat_message(self):
        # peer_id > 2000000000 -> беседа
        raw_event = [
            4,
            12347,
            1,
            2000000251,
            1710000010,
            "Ребята, домашку задали?",
            {"from": "54321"},
            {},
        ]
        res = parse_longpoll_message(raw_event)
        self.assertIsNotNone(res)
        self.assertEqual(res["peer_id"], 2000000251)
        self.assertEqual(res["extra_data"].get("from"), "54321")

    def test_format_attachment_snippet(self):
        extra_photo = {"attach1_type": "photo"}
        self.assertEqual(format_attachment_snippet(extra_photo, {}), "[Фотография]")

        extra_sticker = {"attach1_type": "sticker"}
        self.assertEqual(format_attachment_snippet(extra_sticker, {}), "[Стикер]")

        extra_audiomsg = {"attach1_type": "audiomsg"}
        self.assertEqual(format_attachment_snippet(extra_audiomsg, {}), "[Голосовое сообщение]")

        extra_doc = {"attach1_type": "doc"}
        self.assertEqual(format_attachment_snippet(extra_doc, {}), "[Файл]")

        extra_unknown = {"attach1_type": "something_else"}
        self.assertEqual(format_attachment_snippet(extra_unknown, {}), "[Вложение]")

    def test_parse_typing_events(self):
        # Код 61: диалог
        ev_61 = [61, 447977046, 0]
        res_61 = parse_typing_event(ev_61)
        self.assertIsNotNone(res_61)
        self.assertEqual(res_61["peer_id"], 447977046)
        self.assertFalse(res_61["is_chat"])

        # Код 62: беседа
        ev_62 = [62, 54321, 251]
        res_62 = parse_typing_event(ev_62)
        self.assertIsNotNone(res_62)
        self.assertEqual(res_62["peer_id"], 2000000251)
        self.assertEqual(res_62["user_id"], 54321)
        self.assertTrue(res_62["is_chat"])


class TestSessionExpirationDetection(unittest.TestCase):
    def test_session_redirect_patterns(self):
        # Паттерны из требования 2
        expired_urls = [
            "https://vk.ru/?u=2&to=L2FsX2ltLnBocA--",
            "https://vk.com/login.php?op=logout",
            "https://id.vk.com/login?app_id=6287487",
        ]
        expired_titles = [
            "ВКонтакте | Добро пожаловать",
            "ВКонтакте | Добро пожаловать!",
        ]

        def check_expired(url, title):
            return "/?u=2" in url or "login" in url or "ВКонтакте | Добро пожаловать" in title

        for u in expired_urls:
            self.assertTrue(check_expired(u, "Загрузка..."))

        for t in expired_titles:
            self.assertTrue(check_expired("https://vk.ru/", t))

        # Обычный активный мессенджер не должен детектироваться как expired
        self.assertFalse(check_expired("https://vk.ru/im", "Мессенджер"))
        self.assertFalse(check_expired("https://vk.com/im?sel=123", "Диалог с Иваном"))


class TestTransportExceptions(unittest.TestCase):
    def test_exception_hierarchy(self):
        from longpoll_client.longpoll_transport import VKAPIError, VKAuthError, VKFloodError, VKError

        err5 = VKAuthError(5, "User authorization failed: invalid access_token")
        self.assertIsInstance(err5, VKAPIError)
        self.assertIsInstance(err5, VKError)
        self.assertEqual(err5.error_code, 5)

        err9 = VKFloodError(9, "Flood control")
        self.assertIsInstance(err9, VKAPIError)
        self.assertEqual(err9.error_code, 9)


if __name__ == "__main__":
    unittest.main()

