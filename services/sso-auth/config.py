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

# --- Уведомления об ошибках (см. alerts_check.py, запускается по cron) ---

# Локальные проверки ЭТОГО узла — отдельный файл, специально исключён из
# sync_panels.sh (lib/ha.sh), иначе push с master затирал бы алерты backup.
LOCAL_ALERTS_DB_PATH = os.path.join(BASE_DIR, "alerts_local.db")
# Копия alerts_local.db соседнего узла — подтягивается по cron
# (sync_peer_alerts.sh, тоже lib/ha.sh) каждые 2 минуты. Не HA-инсталляции
# этот файл просто никогда не появится — тогда учитываются только local.
PEER_ALERTS_DB_PATH = os.path.join(BASE_DIR, "peer_alerts.db")

# systemd-юниты всех панелей портала — проверяются на активность.
# ВАЖНО: конференц-панель называется asterisk-panel.service (НЕ
# confbridge-panel).
PANEL_UNITS = [
    "sso-auth", "monitor-panel", "cdr-panel", "alert-panel",
    "phone-provisioning", "asterisk-panel", "maintenance-panel",
]

# AMI — отдельный read-only пользователь только для проверки живости
# (Action: Ping), не пересекается с AMI-пользователями других панелей.
AMI_HOST = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("AMI_PORT", "5038"))
AMI_USER = os.environ.get("AMI_USER", "portalalerts")
AMI_SECRET = os.environ.get("AMI_SECRET", "")

# Порог свободного места на диске (%), ниже которого — уведомление.
DISK_FREE_PERCENT_THRESHOLD = 10

# HA-кластер: общий конфиг, который пишет lib/ha.sh на каждом узле
# (VIP/IFACE/ROLE).
HA_CONF_PATH = "/etc/atc-portal-ha.conf"
# Порог отставания репликации MariaDB (сек) на резервном узле.
HA_REPLICATION_LAG_THRESHOLD = 30

# Сколько дней хранить РЕШЁННЫЕ уведомления (и локальные, и снятые
# вручную) в alerts_local.db, прежде чем удалить — иначе таблица растёт
# бесконечно без какой-либо пользы (см. db.cleanup_old_alerts).
ALERTS_RETENTION_DAYS = 30
