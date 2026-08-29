-- Схема БД панели оповещения (SQLite)

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'operator')),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_name TEXT NOT NULL,
    filename TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    created_by TEXT
);

CREATE TABLE IF NOT EXISTS extensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extension TEXT UNIQUE NOT NULL,
    label TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    imported_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS events_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK(event_type IN ('alert', 'test')),
    name TEXT NOT NULL,
    target_count INTEGER NOT NULL,
    triggered_by TEXT,
    triggered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    note TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

INSERT OR IGNORE INTO settings (key, value) VALUES
    ('ami_host', '127.0.0.1'),
    ('ami_port', '5038'),
    ('ami_username', 'alertpanel'),
    ('ami_secret', 'CHANGE_ME'),
    ('channel_tech', 'PJSIP');
