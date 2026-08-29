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
