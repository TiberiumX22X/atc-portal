import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_DB_PATH = os.environ.get("SSO_DB_PATH", os.path.join(BASE_DIR, "sso.db"))
SECRET_KEY_PATH = os.path.join(BASE_DIR, ".secret_key")

# Сервис слушает только localhost — наружу торчит исключительно через nginx.
HOST = "127.0.0.1"
PORT = 8080

# Список панелей портала: используется на главной странице и нигде
# в логике авторизации (пути nginx настраиваются отдельно, см. nginx/).
PANELS = [
    {"key": "confbridge",  "name": "Конференц-панель", "path": "/confbridge/", "icon": "👥"},
    {"key": "provision",   "name": "Провижининг",      "path": "/provision/",  "icon": "☎️"},
    {"key": "alert",       "name": "Оповещение",       "path": "/alert/",      "icon": "📢"},
    {"key": "cdr",         "name": "CDR / записи",     "path": "/cdr/",        "icon": "🗂️"},
    {"key": "monitor",     "name": "Монитор АТС",      "path": "/monitor/",    "icon": "📊"},
    {"key": "maintenance", "name": "Обслуживание",     "path": "/maintenance/", "icon": "🛠️", "admin_only": True},
]

SESSION_COOKIE_NAME = "sso_session"
