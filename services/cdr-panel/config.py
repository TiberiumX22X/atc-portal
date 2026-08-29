import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# cdrpanel.db (логин/пароль/роли) больше не нужна — с переводом на SSO вход
# и роли живут в центральном sso-auth. Старый файл БД можно спокойно
# оставить на диске (не читается) или удалить вручную.

# --- Подключение к asteriskcdrdb (read-only, отдельный MySQL-пользователь) ---
# Значения ниже — плейсхолдеры по умолчанию. install.sh перезапишет этот файл
# реальными значениями (хост/пользователь/пароль), которые сам же создаст в MySQL.
CDR_DB_HOST = os.environ.get("CDR_DB_HOST", "localhost")
CDR_DB_PORT = int(os.environ.get("CDR_DB_PORT", "3306"))
CDR_DB_NAME = os.environ.get("CDR_DB_NAME", "asteriskcdrdb")
CDR_DB_USER = os.environ.get("CDR_DB_USER", "cdrpanel")
CDR_DB_PASSWORD = os.environ.get("CDR_DB_PASSWORD", "CHANGE_ME")

# --- Каталог с записями разговоров (Asterisk Monitor / MixMonitor) ---
# Стандартный путь FreePBX; если у вас другой — поменяйте после первого
# тестового звонка с включённой записью (Call Recording: Force).
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/var/spool/asterisk/monitor")

# --- Диапазон номеров, который считается "внутренним" добавочным ---
# Используется, чтобы отличить внутренний/исходящий/входящий звонок.
EXTENSION_REGEX = r"^7\d{3}$"

# --- Секрет для подписи flash-сообщений и CSRF-токена формы удаления ---
# Это НЕ секрет входа — логин/пароль панель больше не хранит (см. SSO).
_FLASH_SECRET_PATH = os.path.join(BASE_DIR, ".flash_secret")


def get_or_create_flash_secret() -> str:
    if os.path.exists(_FLASH_SECRET_PATH):
        with open(_FLASH_SECRET_PATH, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(_FLASH_SECRET_PATH, "w") as f:
        f.write(key)
    os.chmod(_FLASH_SECRET_PATH, 0o600)
    return key


# После перевода на SSO панель доступна ТОЛЬКО через nginx (127.0.0.1) —
# nginx проверяет auth_request и подставляет заголовок X-Remote-User.
# Слушать 0.0.0.0 больше нельзя: это позволило бы обойти SSO, обратившись
# на порт панели напрямую.
HOST = "127.0.0.1"
PORT = 8092
PAGE_SIZE = 25
