import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# monitorpanel.db (логин/пароль администратора) больше не нужна — с переводом
# на SSO вход и пароли живут в центральном sso-auth. Старый файл БД можно
# спокойно оставить на диске (не читается) или удалить вручную.

# --- AMI (отдельное подключение только для этой панели) ---
AMI_HOST = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("AMI_PORT", "5038"))
AMI_USER = os.environ.get("AMI_USER", "monitorpanel")
AMI_SECRET = os.environ.get("AMI_SECRET", "CHANGE_ME")

# --- MySQL FreePBX (только чтение имён добавочных из таблицы users) ---
# Креды берутся из /etc/freepbx.conf при установке (тот же путь, что уже
# использует сервис провижининга) — отдельного read-only пользователя здесь
# не заводим, т.к. нужен только SELECT по одной таблице, а не по всей CDR-базе.
FREEPBX_DB_HOST = os.environ.get("FREEPBX_DB_HOST", "localhost")
FREEPBX_DB_NAME = os.environ.get("FREEPBX_DB_NAME", "asterisk")
FREEPBX_DB_USER = os.environ.get("FREEPBX_DB_USER", "freepbxuser")
FREEPBX_DB_PASSWORD = os.environ.get("FREEPBX_DB_PASSWORD", "")

EXTENSION_REGEX = r"^7\d{3}$"

POLL_INTERVAL_SECONDS = 3
PAGE_SIZE = 25

# После перевода на SSO панель доступна ТОЛЬКО через nginx (127.0.0.1) —
# nginx проверяет auth_request и подставляет заголовок X-Remote-User.
# Слушать 0.0.0.0 больше нельзя: это позволило бы обойти SSO, обратившись
# на порт панели напрямую.
HOST = "127.0.0.1"
PORT = 8093
