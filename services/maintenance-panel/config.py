import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

HOST = "127.0.0.1"
PORT = 8094

# AMI — секрет читается из окружения (.env + EnvironmentFile в systemd),
# по тому же принципу, что и монитор-панель. НЕ зашиваем секрет в текст
# файла литералом — это ровно та грабля, на которую мы уже наступали с
# конференц-панелью (там секрет живёт в api.py и теряется при полной
# замене файла).
AMI_HOST = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("AMI_PORT", "5038"))
AMI_USER = os.environ.get("AMI_USER", "maintpanel")
AMI_SECRET = os.environ.get("AMI_SECRET", "CHANGE_ME")

# Секрет для подписи Flask-сессии (CSRF-токен, flash-сообщения) — НЕ вход,
# логин целиком приходит от SSO-портала.
_SECRET_FILE = os.path.join(BASE_DIR, ".secret_key")


def get_or_create_secret_key() -> str:
    if os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE, "r") as f:
            return f.read().strip()
    import secrets
    key = secrets.token_hex(32)
    with open(_SECRET_FILE, "w") as f:
        f.write(key)
    os.chmod(_SECRET_FILE, 0o600)
    return key
