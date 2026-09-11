import os
import sqlite3

import config


def get_app_db():
    conn = sqlite3.connect(config.APP_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_app_db():
    conn = get_app_db()
    with open(os.path.join(config.BASE_DIR, "schema.sql"), "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()

    # Миграция для уже существующих установок: добавляем колонку
    # allowed_panels, если её ещё нет (SQLite не умеет "ADD COLUMN IF NOT
    # EXISTS" напрямую — ловим ошибку "duplicate column", это стандартный
    # для SQLite способ сделать миграцию идемпотентной).
    try:
        conn.execute("ALTER TABLE users ADD COLUMN allowed_panels TEXT NOT NULL DEFAULT '*'")
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc):
            raise

    conn.close()
    if os.path.exists(config.APP_DB_PATH):
        os.chmod(config.APP_DB_PATH, 0o600)


def get_active_alerts():
    """Активные (нерешённые) уведомления с обоих узлов (локальные + копия
    соседнего узла, см. peer_alerts.db / sync_peer_alerts.sh), самые
    свежие сверху. Не-HA инсталляции — peer_alerts.db просто не
    существует, тогда возвращаются только локальные."""
    rows = []
    for path in (config.LOCAL_ALERTS_DB_PATH, config.PEER_ALERTS_DB_PATH):
        if not os.path.exists(path):
            continue
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            rows.extend(conn.execute(
                "SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY last_seen DESC"
            ).fetchall())
        except sqlite3.OperationalError:
            # peer_alerts.db может на секунду оказаться пустым файлом
            # посреди rsync-копирования — просто пропускаем этот цикл.
            continue
        finally:
            conn.close()
    rows.sort(key=lambda r: r["last_seen"], reverse=True)
    return rows


def init_local_alerts_db():
    """Локальная (не подменяемая rsync'ом с соседа) БД уведомлений этого
    узла — используется alerts_check.py."""
    conn = sqlite3.connect(config.LOCAL_ALERTS_DB_PATH)
    with open(os.path.join(config.BASE_DIR, "alerts_schema.sql"), "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    os.chmod(config.LOCAL_ALERTS_DB_PATH, 0o644)  # читает и www-data (Flask), и cron (root)


def cleanup_old_alerts():
    """Удаляет РЕШЁННЫЕ уведомления старше config.ALERTS_RETENTION_DAYS —
    иначе alerts_local.db растёт бесконечно без пользы. Активные
    (resolved_at IS NULL) не трогаются никогда, вне зависимости от
    возраста first_seen."""
    if not os.path.exists(config.LOCAL_ALERTS_DB_PATH):
        return
    conn = sqlite3.connect(config.LOCAL_ALERTS_DB_PATH)
    conn.execute(
        "DELETE FROM alerts WHERE resolved_at IS NOT NULL "
        "AND resolved_at < datetime('now', 'localtime', ?)",
        (f"-{config.ALERTS_RETENTION_DAYS} days",),
    )
    conn.commit()
    conn.close()
