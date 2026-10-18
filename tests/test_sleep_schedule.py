import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import src.web_notifier as web_notifier

def test_sleep_window_crossing_midnight():
    tz = ZoneInfo("Europe/Moscow")
    web_notifier.SLEEP_MODE_ENABLED = True
    web_notifier.SLEEP_TIMEZONE = "Europe/Moscow"
    web_notifier.SLEEP_START_TIME = dtime(2, 0)
    web_notifier.SLEEP_END_TIME = dtime(7, 30)

    # Дневное время (бодрствование)
    assert not web_notifier.is_in_sleep_window(datetime(2026, 10, 18, 12, 0, tzinfo=tz))
    assert not web_notifier.is_in_sleep_window(datetime(2026, 10, 18, 1, 59, tzinfo=tz))
    assert not web_notifier.is_in_sleep_window(datetime(2026, 10, 18, 7, 30, tzinfo=tz))

    # Ночное время (сон)
    assert web_notifier.is_in_sleep_window(datetime(2026, 10, 18, 2, 0, tzinfo=tz))
    assert web_notifier.is_in_sleep_window(datetime(2026, 10, 18, 4, 15, tzinfo=tz))
    assert web_notifier.is_in_sleep_window(datetime(2026, 10, 18, 7, 29, tzinfo=tz))

def test_sleep_mode_disabled():
    tz = ZoneInfo("Europe/Moscow")
    web_notifier.SLEEP_MODE_ENABLED = False
    assert not web_notifier.is_in_sleep_window(datetime(2026, 10, 18, 3, 0, tzinfo=tz))
    web_notifier.SLEEP_MODE_ENABLED = True

def test_seconds_until_wake():
    tz = ZoneInfo("Europe/Moscow")
    web_notifier.SLEEP_START_TIME = dtime(2, 0)
    web_notifier.SLEEP_END_TIME = dtime(7, 30)

    # В 05:30 до пробуждения в 07:30 ровно 2 часа (7200 сек)
    dt = datetime(2026, 10, 18, 5, 30, tzinfo=tz)
    wait_sec = web_notifier.seconds_until_wake(dt)
    assert abs(wait_sec - 7200.0) < 1.0
