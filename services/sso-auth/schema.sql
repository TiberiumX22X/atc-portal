-- Центральная БД пользователей единого портала.
-- Заменяет собой отдельные таблицы users/admin/panel_users в каждой панели.

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    role TEXT NOT NULL DEFAULT 'operator' CHECK(role IN ('admin', 'operator')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_login TEXT
);

-- Журнал входов — полезно при переносе с 5 разных логинов на 1,
-- чтобы видеть, кто и когда реально заходил через портал.
CREATE TABLE IF NOT EXISTS login_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    ip TEXT,
    success INTEGER NOT NULL,
    ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
