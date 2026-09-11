import pymysql
import pymysql.cursors
import config


# ---------------------------------------------------------------------------
# Подключение к asteriskcdrdb — только чтение
# ---------------------------------------------------------------------------

def get_cdr_connection():
    """Открывает новое соединение на каждый запрос — CDR-запросы происходят
    редко относительно, скажем, AMI-нагрузки других панелей проекта,
    держать пул тут избыточно и добавляет сложности без выгоды."""
    return pymysql.connect(
        host=config.CDR_DB_HOST,
        port=config.CDR_DB_PORT,
        user=config.CDR_DB_USER,
        password=config.CDR_DB_PASSWORD,
        database=config.CDR_DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=15,
        charset="utf8mb4",
    )
