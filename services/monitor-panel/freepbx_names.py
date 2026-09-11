import pymysql
import pymysql.cursors
import config


def fetch_extension_names() -> dict:
    """Возвращает {extension: name} для всех добавочных, известных FreePBX.
    На этом сервере имя хранится в колонке users.name (то же самое уже
    подтверждено и используется в сервисе автопровижининга) — обёрнуто в
    try/except на случай отличий на других инсталляциях."""
    conn = pymysql.connect(
        host=config.FREEPBX_DB_HOST,
        user=config.FREEPBX_DB_USER,
        password=config.FREEPBX_DB_PASSWORD,
        database=config.FREEPBX_DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=10,
        charset="utf8mb4",
    )
    names = {}
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT extension, name FROM users")
            except pymysql.err.OperationalError:
                # запасной вариант, если колонка называется иначе
                cur.execute("SELECT extension, description AS name FROM users")
            for row in cur.fetchall():
                names[str(row["extension"])] = row.get("name") or ""
    finally:
        conn.close()
    return names
