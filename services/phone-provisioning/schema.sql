-- Схема БД сервиса автопровижининга телефонов
-- SQLite

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac TEXT UNIQUE NOT NULL,              -- без разделителей, нижний регистр, напр. c074ad1a0c40
    extension TEXT,                        -- номер линии 1 (может быть пустым для "заготовок")
    name TEXT,                             -- ФИО / подразделение (для справки в интерфейсе)
    manufacturer TEXT DEFAULT 'Grandstream',
    model TEXT NOT NULL,                   -- GXP1610 / GXP1620 / GXP2170 / GRP2613 / GRP2636 / GRP2650
    label TEXT,                            -- отображаемое имя линии 1 на экране телефона
    sip_password TEXT,                     -- пароль SIP-аккаунта линии 1
    active INTEGER DEFAULT 1,              -- активна ли линия 1
    extension2 TEXT,                       -- номер линии 2 (опционально)
    name2 TEXT,                            -- имя / подразделение линии 2
    label2 TEXT,                           -- отображаемое имя линии 2 на экране телефона
    sip_password2 TEXT,                    -- пароль SIP-аккаунта линии 2
    active2 INTEGER DEFAULT 0,             -- активна ли линия 2 (по умолчанию выключена, пока не задан номер)
    last_seen_ip TEXT,                     -- IP-адрес, с которого телефон последний раз запрашивал конфиг
    last_seen_at TEXT,                     -- когда это было
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blf_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    key_index INTEGER NOT NULL,            -- номер клавиши (1,2,3...)
    description TEXT,                      -- подпись на клавише
    value TEXT,                            -- отслеживаемый номер
    key_mode TEXT DEFAULT 'BLF',           -- BLF / Line / SpeedDial и т.п.
    FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS firmware (
    model TEXT PRIMARY KEY,                -- модель, для которой действует файл
    filename TEXT,                         -- имя файла в /static/firmware/
    enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS panel_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Настройки разнесены по семействам телефонов (1610_, 2170_, grp_) —
-- у каждого свои SIP/NTP/язык/пароль и, где применимо, Bluetooth/WiFi/
-- яркость/заставка, независимо друг от друга.
--
-- ВНИМАНИЕ: значения ниже — примеры-заглушки (192.168.1.100, CHANGE_ME_...),
-- не настоящие боевые адреса/пароли. install.sh при первой установке
-- подставляет вместо CHANGE_ME_ADMIN_PASSWORD случайно сгенерированный
-- пароль (уникальный на каждое семейство) — открыть /admin/settings/<family>
-- и указать реальные SIP/NTP-сервер для вашей сети до раздачи конфигов
-- телефонам.
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('config_enabled', '1'),
    ('firmware_enabled', '1'),

    ('1610_sip_server', '192.168.1.100'),
    ('1610_sip_port', '5060'),
    ('1610_config_server', '192.168.1.100:8090'),
    ('1610_firmware_server', '192.168.1.100:8090/firmware'),
    ('1610_ntp_server', 'pool.ntp.org'),
    ('1610_timezone', 'auto'),
    ('1610_language', 'ru'),
    ('1610_admin_password', 'CHANGE_ME_ADMIN_PASSWORD'),
    ('1610_date_format', '2'),
    ('1610_time_format', '1'),

    ('2170_sip_server', '192.168.1.100'),
    ('2170_sip_port', '5060'),
    ('2170_config_server', '192.168.1.100:8090'),
    ('2170_firmware_server', '192.168.1.100:8090/firmware'),
    ('2170_ntp_server', 'pool.ntp.org'),
    ('2170_timezone', 'auto'),
    ('2170_language', 'ru'),
    ('2170_admin_password', 'CHANGE_ME_ADMIN_PASSWORD'),
    ('2170_date_format', '2'),
    ('2170_time_format', '1'),
    ('2170_color_bluetooth_enabled', '0'),
    ('2170_color_screensaver_enabled', '0'),
    ('2170_color_brightness', '100'),

    ('grp_sip_server', '192.168.1.100'),
    ('grp_sip_port', '5060'),
    ('grp_config_server', '192.168.1.100:8090'),
    ('grp_firmware_server', '192.168.1.100:8090/firmware'),
    ('grp_ntp_server', 'pool.ntp.org'),
    ('grp_timezone', 'auto'),
    ('grp_language', 'ru'),
    ('grp_admin_password', 'CHANGE_ME_ADMIN_PASSWORD'),
    ('grp_date_format', '2'),
    ('grp_time_format', '1'),
    ('grp_color_bluetooth_enabled', '0'),
    ('grp_color_screensaver_enabled', '0'),
    ('grp_color_brightness', '100'),
    ('grp_color_wifi_enabled', '0');
