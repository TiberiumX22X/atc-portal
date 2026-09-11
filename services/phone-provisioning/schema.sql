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


-- ============================================================
-- Мульти-вендорные шаблоны (Fanvil, Yealink и т.п.)
--
-- Grandstream (devices.manufacturer='Grandstream') НЕ переносится
-- сюда и продолжает идти старым путём: MODEL_FAMILIES в app.py +
-- файлы .xml.j2 в templates/. Эти таблицы обслуживают только новые
-- вендоры, добавленные поверх — существующий код их не трогает.
-- ============================================================

CREATE TABLE IF NOT EXISTS phone_vendors (
    id TEXT PRIMARY KEY,                   -- 'fanvil', 'yealink' — слаг, используется в URL/коде
    name TEXT NOT NULL,                    -- 'Fanvil', 'Yealink' — для отображения в интерфейсе
    template_format TEXT NOT NULL,         -- 'xml' | 'cfg' — определяет расширение выдаваемого файла и Content-Type
    template_content TEXT NOT NULL,        -- единый Jinja2-шаблон на вендора; модели внутри различаются через {% if model == ... %}
    original_content TEXT,                 -- пристина копия template_content на момент built_in — для кнопки «Восстановить оригинал» после правок
    source TEXT NOT NULL DEFAULT 'built_in', -- 'built_in' (штатный, из проекта) | 'custom' (правки пользователя)
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS phone_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id TEXT NOT NULL REFERENCES phone_vendors(id) ON DELETE CASCADE,
    model_key TEXT NOT NULL,               -- как модель значится в User-Agent/выпадающем списке, напр. 'X4U', 'SIP-T46S'
    display_name TEXT NOT NULL,            -- 'Fanvil X4U' — для интерфейса
    status TEXT NOT NULL DEFAULT 'unverified', -- 'verified' | 'unverified' | 'broken'
    status_note TEXT,                      -- ссылка на GitHub issue или комментарий (обязателен при status='broken')
    verified_at TEXT,                      -- когда подтверждено; NULL пока unverified/broken
    UNIQUE(vendor_id, model_key)
);

-- Настройки по вендору — та же идея, что settings-таблица с префиксами
-- (1610_/2170_/grp_) у Grandstream, но нормализовано в пары ключ-значение,
-- чтобы не переписывать схему при добавлении следующего вендора.
CREATE TABLE IF NOT EXISTS phone_vendor_settings (
    vendor_id TEXT NOT NULL REFERENCES phone_vendors(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (vendor_id, key)
);

INSERT OR IGNORE INTO phone_vendors (id, name, template_format, template_content, original_content, source) VALUES
    ('fanvil', 'Fanvil', 'xml', '<?xml version="1.0" encoding="UTF-8"?>
<VOIP_CONFIG_FILE>
<Version>2.0003</Version>
<GLOBAL_CONFIG_MODULE>
<CFG_Update_Realtime>1</CFG_Update_Realtime>
<Host_Name>{{ mac }}</Host_Name>
<RTP_Initial_Port>10000</RTP_Initial_Port>
<RTP_Port_Quantity>200</RTP_Port_Quantity>
<SNTP_Server>{{ s.ntp_server }}</SNTP_Server>
<Second_SNTP_Server>{{ s.ntp_server }}</Second_SNTP_Server>
<Time_Zone>{{ s.timezone }}</Time_Zone>
<Language>{{ s.language }}</Language>
</GLOBAL_CONFIG_MODULE>

<SIP_CONFIG_MODULE>
<Notify_Reboot>0</Notify_Reboot>
<SIP__Port>5060</SIP__Port>
<SIP_Line_List>
<SIP_Line_List_Entry>
<ID>SIP1</ID>
<Enable>{{ ''1'' if device.active else ''0'' }}</Enable>
<Phone_Number>{{ device.extension }}</Phone_Number>
<Display_Name>{{ device.label }}</Display_Name>
<Register_Addr>{{ s.sip_server }}</Register_Addr>
<Register_Port>{{ s.sip_port }}</Register_Port>
<Local_Domain>{{ s.sip_server }}:{{ s.sip_port }}</Local_Domain>
<Register_User>{{ device.extension }}</Register_User>
<Register_Pswd>{{ device.sip_password }}</Register_Pswd>
<Register_TTL>120</Register_TTL>
<Enable_Reg>1</Enable_Reg>
<Proxy_User>{{ device.extension }}</Proxy_User>
<Proxy_Pswd>{{ device.sip_password }}</Proxy_Pswd>
</SIP_Line_List_Entry>
</SIP_Line_List>
</SIP_CONFIG_MODULE>

<MMI_CONFIG_MODULE>
<Web_Authentication>1</Web_Authentication>
<MMI_Account>
<MMI_Account_Entry>
<ID>Account1</ID>
<Name>admin</Name>
<Password>{{ s.admin_password }}</Password>
<Level>10</Level>
</MMI_Account_Entry>
</MMI_Account>
</MMI_CONFIG_MODULE>

{% if firmware_file %}
<AUTOUPDATE_CONFIG_MODULE>
<Auto_Image_Url>{{ s.firmware_server }}/{{ firmware_file }}</Auto_Image_Url>
</AUTOUPDATE_CONFIG_MODULE>
{% endif %}
</VOIP_CONFIG_FILE>
', '<?xml version="1.0" encoding="UTF-8"?>
<VOIP_CONFIG_FILE>
<Version>2.0003</Version>
<GLOBAL_CONFIG_MODULE>
<CFG_Update_Realtime>1</CFG_Update_Realtime>
<Host_Name>{{ mac }}</Host_Name>
<RTP_Initial_Port>10000</RTP_Initial_Port>
<RTP_Port_Quantity>200</RTP_Port_Quantity>
<SNTP_Server>{{ s.ntp_server }}</SNTP_Server>
<Second_SNTP_Server>{{ s.ntp_server }}</Second_SNTP_Server>
<Time_Zone>{{ s.timezone }}</Time_Zone>
<Language>{{ s.language }}</Language>
</GLOBAL_CONFIG_MODULE>

<SIP_CONFIG_MODULE>
<Notify_Reboot>0</Notify_Reboot>
<SIP__Port>5060</SIP__Port>
<SIP_Line_List>
<SIP_Line_List_Entry>
<ID>SIP1</ID>
<Enable>{{ ''1'' if device.active else ''0'' }}</Enable>
<Phone_Number>{{ device.extension }}</Phone_Number>
<Display_Name>{{ device.label }}</Display_Name>
<Register_Addr>{{ s.sip_server }}</Register_Addr>
<Register_Port>{{ s.sip_port }}</Register_Port>
<Local_Domain>{{ s.sip_server }}:{{ s.sip_port }}</Local_Domain>
<Register_User>{{ device.extension }}</Register_User>
<Register_Pswd>{{ device.sip_password }}</Register_Pswd>
<Register_TTL>120</Register_TTL>
<Enable_Reg>1</Enable_Reg>
<Proxy_User>{{ device.extension }}</Proxy_User>
<Proxy_Pswd>{{ device.sip_password }}</Proxy_Pswd>
</SIP_Line_List_Entry>
</SIP_Line_List>
</SIP_CONFIG_MODULE>

<MMI_CONFIG_MODULE>
<Web_Authentication>1</Web_Authentication>
<MMI_Account>
<MMI_Account_Entry>
<ID>Account1</ID>
<Name>admin</Name>
<Password>{{ s.admin_password }}</Password>
<Level>10</Level>
</MMI_Account_Entry>
</MMI_Account>
</MMI_CONFIG_MODULE>

{% if firmware_file %}
<AUTOUPDATE_CONFIG_MODULE>
<Auto_Image_Url>{{ s.firmware_server }}/{{ firmware_file }}</Auto_Image_Url>
</AUTOUPDATE_CONFIG_MODULE>
{% endif %}
</VOIP_CONFIG_FILE>
', 'built_in'),
    ('yealink', 'Yealink', 'cfg', '#!version:1.0.0.1

## Время / NTP
local_time.time_zone = {{ s.timezone }}
local_time.ntp_server1 = {{ s.ntp_server }}
local_time.ntp_server2 = {{ s.ntp_server }}

## Язык
lang.wui = {{ s.language }}
lang.gui = {{ s.language }}

## Веб-интерфейс телефона
security.user_name.admin = admin
security.user_password = admin:{{ s.admin_password }}

## Аккаунт 1
account.1.enable = {{ ''1'' if device.active else ''0'' }}
account.1.label = {{ device.extension }} | {{ device.label }}
account.1.display_name = {{ device.label }}
account.1.auth_name = {{ device.extension }}
account.1.password = {{ device.sip_password }}
account.1.user_name = {{ device.extension }}
account.1.sip_server.1.address = {{ s.sip_server }}
account.1.sip_server.1.port = {{ s.sip_port }}

## Прошивка
{% if firmware_file %}firmware.url = {{ s.firmware_server }}/{{ firmware_file }}{% endif %}
', '#!version:1.0.0.1

## Время / NTP
local_time.time_zone = {{ s.timezone }}
local_time.ntp_server1 = {{ s.ntp_server }}
local_time.ntp_server2 = {{ s.ntp_server }}

## Язык
lang.wui = {{ s.language }}
lang.gui = {{ s.language }}

## Веб-интерфейс телефона
security.user_name.admin = admin
security.user_password = admin:{{ s.admin_password }}

## Аккаунт 1
account.1.enable = {{ ''1'' if device.active else ''0'' }}
account.1.label = {{ device.extension }} | {{ device.label }}
account.1.display_name = {{ device.label }}
account.1.auth_name = {{ device.extension }}
account.1.password = {{ device.sip_password }}
account.1.user_name = {{ device.extension }}
account.1.sip_server.1.address = {{ s.sip_server }}
account.1.sip_server.1.port = {{ s.sip_port }}

## Прошивка
{% if firmware_file %}firmware.url = {{ s.firmware_server }}/{{ firmware_file }}{% endif %}
', 'built_in');
-- template_content заполнен на Этапе 2 (сконвертировано из 3CX-файлов
-- fanvil_ph.xml/yealink_ph.xml — lean-версия: SIP-регистрация, NTP,
-- часовой пояс, язык, пароль администратора; без DECT/мультикаста/
-- логотипов/расширенных функций — их можно добавить позже через
-- вкладку «Шаблоны», режим advanced).

INSERT OR IGNORE INTO phone_vendor_settings (vendor_id, key, value) VALUES
    ('fanvil', 'sip_server', '192.168.1.100'),
    ('fanvil', 'sip_port', '5060'),
    ('fanvil', 'config_server', '192.168.1.100:8090'),
    ('fanvil', 'firmware_server', '192.168.1.100:8090/firmware'),
    ('fanvil', 'ntp_server', 'pool.ntp.org'),
    ('fanvil', 'timezone', 'auto'),
    ('fanvil', 'language', 'ru'),
    ('fanvil', 'admin_password', 'CHANGE_ME_ADMIN_PASSWORD'),

    ('yealink', 'sip_server', '192.168.1.100'),
    ('yealink', 'sip_port', '5060'),
    ('yealink', 'config_server', '192.168.1.100:8090'),
    ('yealink', 'firmware_server', '192.168.1.100:8090/firmware'),
    ('yealink', 'ntp_server', 'pool.ntp.org'),
    ('yealink', 'timezone', 'auto'),
    ('yealink', 'language', 'ru'),
    ('yealink', 'admin_password', 'CHANGE_ME_ADMIN_PASSWORD');

-- Модели загружены на Этапе 3 (54 Fanvil + 34 Yealink = 88 моделей),
-- все status='unverified' — ни одна не проверена на реальном железе.
-- Статус меняется вручную администратором панели по итогам
-- подтверждений в GitHub Issues (см. README, раздел «Обратная связь»).
-- Единственные verified-модели в проекте — 3 модели Grandstream,
-- которые в эту таблицу не входят (свой путь через MODEL_FAMILIES).

INSERT OR IGNORE INTO phone_models (vendor_id, model_key, display_name, status) VALUES
    ('fanvil', 'V50P', 'Fanvil V50P', 'unverified'),
    ('fanvil', 'V60P', 'Fanvil V60P', 'unverified'),
    ('fanvil', 'V60W', 'Fanvil V60W', 'unverified'),
    ('fanvil', 'V61G', 'Fanvil V61G', 'unverified'),
    ('fanvil', 'V61W', 'Fanvil V61W', 'unverified'),
    ('fanvil', 'V62', 'Fanvil V62', 'unverified'),
    ('fanvil', 'V62G', 'Fanvil V62G', 'unverified'),
    ('fanvil', 'V62Pro', 'Fanvil V62Pro', 'unverified'),
    ('fanvil', 'V62W', 'Fanvil V62W', 'unverified'),
    ('fanvil', 'V63', 'Fanvil V63', 'unverified'),
    ('fanvil', 'V64', 'Fanvil V64', 'unverified'),
    ('fanvil', 'V65', 'Fanvil V65', 'unverified'),
    ('fanvil', 'V66', 'Fanvil V66', 'unverified'),
    ('fanvil', 'V66Pro', 'Fanvil V66Pro', 'unverified'),
    ('fanvil', 'W610W', 'Fanvil W610W', 'unverified'),
    ('fanvil', 'W611W', 'Fanvil W611W', 'unverified'),
    ('fanvil', 'W620W', 'Fanvil W620W', 'unverified'),
    ('fanvil', 'X1S', 'Fanvil X1S', 'unverified'),
    ('fanvil', 'X1SG', 'Fanvil X1SG', 'unverified'),
    ('fanvil', 'X2', 'Fanvil X2', 'unverified'),
    ('fanvil', 'X210', 'Fanvil X210', 'unverified'),
    ('fanvil', 'X210-V2', 'Fanvil X210-V2', 'unverified'),
    ('fanvil', 'X210i-V2', 'Fanvil X210i-V2', 'unverified'),
    ('fanvil', 'X2C', 'Fanvil X2C', 'unverified'),
    ('fanvil', 'X301', 'Fanvil X301/X301P', 'unverified'),
    ('fanvil', 'X301G', 'Fanvil X301G', 'unverified'),
    ('fanvil', 'X301W', 'Fanvil X301W', 'unverified'),
    ('fanvil', 'X303', 'Fanvil X303/X303P', 'unverified'),
    ('fanvil', 'X303G', 'Fanvil X303G', 'unverified'),
    ('fanvil', 'X303W', 'Fanvil X303W', 'unverified'),
    ('fanvil', 'X305', 'Fanvil X305', 'unverified'),
    ('fanvil', 'X3S', 'Fanvil X3S', 'unverified'),
    ('fanvil', 'X3SG', 'Fanvil X3SG', 'unverified'),
    ('fanvil', 'X3SG_Lite', 'Fanvil X3SG Lite', 'unverified'),
    ('fanvil', 'X3SG_Pro', 'Fanvil X3SG Pro', 'unverified'),
    ('fanvil', 'X3SP_Lite', 'Fanvil X3S(P) Lite', 'unverified'),
    ('fanvil', 'X3SP_Pro', 'Fanvil X3S(P) Pro', 'unverified'),
    ('fanvil', 'X3U', 'Fanvil X3U', 'unverified'),
    ('fanvil', 'X3U_Pro', 'Fanvil X3U Pro', 'unverified'),
    ('fanvil', 'X4', 'Fanvil X4', 'unverified'),
    ('fanvil', 'X4U', 'Fanvil X4U', 'unverified'),
    ('fanvil', 'X4U-V2', 'Fanvil X4U-V2', 'unverified'),
    ('fanvil', 'X5S', 'Fanvil X5S', 'unverified'),
    ('fanvil', 'X5U', 'Fanvil X5U', 'unverified'),
    ('fanvil', 'X5U-V2', 'Fanvil X5U-V2', 'unverified'),
    ('fanvil', 'X6', 'Fanvil X6', 'unverified'),
    ('fanvil', 'X6U', 'Fanvil X6U', 'unverified'),
    ('fanvil', 'X6U-V2', 'Fanvil X6U-V2', 'unverified'),
    ('fanvil', 'X7', 'Fanvil X7', 'unverified'),
    ('fanvil', 'X7-V2', 'Fanvil X7-V2', 'unverified'),
    ('fanvil', 'X7A', 'Fanvil X7A', 'unverified'),
    ('fanvil', 'X7C', 'Fanvil X7C', 'unverified'),
    ('fanvil', 'X7C-V2', 'Fanvil X7C-V2', 'unverified'),
    ('fanvil', 'i56A', 'Fanvil i56A', 'unverified'),
    ('yealink', 'SIP-T19_E2', 'Yealink T19 E2', 'unverified'),
    ('yealink', 'SIP-T19P_E2', 'Yealink T19P E2', 'unverified'),
    ('yealink', 'SIP-T21_E2', 'Yealink T21 E2', 'unverified'),
    ('yealink', 'SIP-T21P_E2', 'Yealink T21P E2', 'unverified'),
    ('yealink', 'SIP-T23G', 'Yealink T23G', 'unverified'),
    ('yealink', 'SIP-T23P', 'Yealink T23P', 'unverified'),
    ('yealink', 'SIP-T27G', 'Yealink T27G', 'unverified'),
    ('yealink', 'SIP-T30P', 'Yealink T30P', 'unverified'),
    ('yealink', 'SIP-T31G', 'Yealink T31G', 'unverified'),
    ('yealink', 'SIP-T31P', 'Yealink T31P', 'unverified'),
    ('yealink', 'SIP-T31W', 'Yealink T31W', 'unverified'),
    ('yealink', 'SIP-T33G', 'Yealink T33G', 'unverified'),
    ('yealink', 'SIP-T33P', 'Yealink T33P', 'unverified'),
    ('yealink', 'SIP-T34W', 'Yealink T34W', 'unverified'),
    ('yealink', 'SIP-T40G', 'Yealink T40G', 'unverified'),
    ('yealink', 'SIP-T40P', 'Yealink T40P', 'unverified'),
    ('yealink', 'SIP-T41S', 'Yealink T41S', 'unverified'),
    ('yealink', 'SIP-T41U', 'Yealink T41U', 'unverified'),
    ('yealink', 'SIP-T42S', 'Yealink T42S', 'unverified'),
    ('yealink', 'SIP-T42U', 'Yealink T42U', 'unverified'),
    ('yealink', 'SIP-T43U', 'Yealink T43U', 'unverified'),
    ('yealink', 'SIP-T44U', 'Yealink T44U', 'unverified'),
    ('yealink', 'SIP-T44W', 'Yealink T44W', 'unverified'),
    ('yealink', 'SIP-T46S', 'Yealink T46S', 'unverified'),
    ('yealink', 'SIP-T46U', 'Yealink T46U', 'unverified'),
    ('yealink', 'SIP-T48S', 'Yealink T48S', 'unverified'),
    ('yealink', 'SIP-T48U', 'Yealink T48U', 'unverified'),
    ('yealink', 'SIP-T52S', 'Yealink T52S', 'unverified'),
    ('yealink', 'SIP-T53', 'Yealink T53', 'unverified'),
    ('yealink', 'SIP-T53C', 'Yealink T53C', 'unverified'),
    ('yealink', 'SIP-T53W', 'Yealink T53W', 'unverified'),
    ('yealink', 'SIP-T54S', 'Yealink T54S', 'unverified'),
    ('yealink', 'SIP-T54W', 'Yealink T54W', 'unverified'),
    ('yealink', 'SIP-T57W', 'Yealink T57W', 'unverified');
