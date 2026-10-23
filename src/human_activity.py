import math
import random
import time
from playwright.sync_api import Page

def bezier_point(p0, p1, p2, p3, t):
    """Вычисляет координаты кубической кривой Безье."""
    x = (1-t)**3 * p0[0] + 3*(1-t)**2 * t * p1[0] + 3*(1-t) * t**2 * p2[0] + t**3 * p3[0]
    y = (1-t)**3 * p0[1] + 3*(1-t)**2 * t * p1[1] + 3*(1-t) * t**2 * p2[1] + t**3 * p3[1]
    return x, y

def human_mouse_move(page: Page, start_pos, target_pos, steps=15):
    """
    Плавное перемещение мыши по кривой Безье с естественным ускорением/замедлением.
    Генерирует реальные pointermove события в DOM VK.
    """
    try:
        dx = target_pos[0] - start_pos[0]
        dy = target_pos[1] - start_pos[1]
        ctrl1 = (start_pos[0] + dx * 0.25 + random.uniform(-40, 40), start_pos[1] + dy * 0.25 + random.uniform(-40, 40))
        ctrl2 = (start_pos[0] + dx * 0.75 + random.uniform(-40, 40), start_pos[1] + dy * 0.75 + random.uniform(-40, 40))

        for i in range(1, steps + 1):
            t = i / steps
            t_smooth = 0.5 * (1 - math.cos(math.pi * t))
            x, y = bezier_point(start_pos, ctrl1, ctrl2, target_pos, t_smooth)
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.005, 0.02))
    except Exception:
        pass

def human_scroll(page: Page, delta_y: int, steps=6):
    """Плавный скролл страницы колесом мыши с человеческими микро-задержками."""
    try:
        step_delta = delta_y / steps
        for _ in range(steps):
            page.mouse.wheel(0, step_delta + random.uniform(-5, 5))
            time.sleep(random.uniform(0.04, 0.12))
    except Exception:
        pass

def perform_in_tab_human_activity(page: Page):
    """
    Легкая активность внутри мессенджера (без ухода со страницы vk.ru/im):
    - Несколько плавных движений мыши
    - Легкий скролл списка диалогов вниз и обратно наверх
    Занимает ~3-5 секунд, сокеты не рвутся, фокус/активность имитируются.
    """
    try:
        start_x, start_y = random.uniform(50, 150), random.uniform(200, 400)
        target_x, target_y = random.uniform(250, 450), random.uniform(300, 600)
        human_mouse_move(page, (start_x, start_y), (target_x, target_y), steps=random.randint(12, 18))
        
        human_scroll(page, delta_y=random.randint(150, 300), steps=5)
        time.sleep(random.uniform(0.5, 1.2))
        human_scroll(page, delta_y=-random.randint(150, 300), steps=5)
        time.sleep(random.uniform(0.3, 0.8))
        
        end_x, end_y = random.uniform(600, 900), random.uniform(100, 300)
        human_mouse_move(page, (target_x, target_y), (end_x, end_y), steps=10)
    except Exception:
        pass
