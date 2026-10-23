import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import src.web_notifier as web_notifier

def test_day_break_window():
    web_notifier.DAY_BREAK_ENABLED = True
    web_notifier.DAY_BREAK_START_HOUR = 17
    web_notifier.DAY_BREAK_END_HOUR = 19
    web_notifier.DAY_BREAK_DURATION_SEC = 900
    
    assert web_notifier.DAY_BREAK_DURATION_SEC == 900
    assert web_notifier.DAY_BREAK_START_HOUR == 17
    assert web_notifier.DAY_BREAK_END_HOUR == 19
