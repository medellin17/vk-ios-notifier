import os
import sys
import vk_api

def auth_handler():
    """Обработчик двухфакторной аутентификации (2FA)."""
    key = input("Введите код подтверждения (SMS или Push из приложения VK): ").strip()
    remember_device = True
    return key, remember_device

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(base_dir, ".env")
    
    login = os.getenv("VK_LOGIN", "")
    password = os.getenv("VK_PASSWORD", "")

    if os.path.isfile(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("VK_LOGIN="):
                    login = line.split("=", 1)[1].strip().strip("'\"")
                elif line.startswith("VK_PASSWORD="):
                    password = line.split("=", 1)[1].strip().strip("'\"")

    if not login or not password:
        print("Введите учетные данные VK для авторизации:")
        if not login:
            login = input("Телефон или Email: ").strip()
        if not password:
            import getpass
            password = getpass.getpass("Пароль: ").strip()

    print(f"Попытка авторизации для {login}...")
    vk_session = vk_api.VkApi(
        login=login,
        password=password,
        auth_handler=auth_handler,
        config_filename=os.path.join(base_dir, "vk_config.v2.json"),
        app_id=6222115,  # Официальное приложение VK
        scope=1073737727
    )

    try:
        vk_session.auth(token_only=True)
        print(" Авторизация успешна! Сессия сохранена в vk_config.v2.json.")
        user = vk_session.method("users.get")[0]
        print(f"Пользователь: {user.get('first_name')} {user.get('last_name')} (ID: {user.get('id')})")
    except Exception as e:
        print(f" Ошибка авторизации: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
