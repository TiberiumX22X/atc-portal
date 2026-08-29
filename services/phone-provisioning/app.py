#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сервис автопровижининга телефонов Grandstream.
Заменяет функционал вкладки "Автоматическая настройка" Yeastar K2.
"""
import os
import re
import sqlite3
import secrets
import string
import socket
import struct
import threading
import random
import time
import requests
from functools import wraps
from datetime import datetime
from urllib.parse import unquote
from flask import Flask, request, Response, render_template, redirect, url_for, flash, send_from_directory, jsonify, session, g, abort
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "devices.db")
FIRMWARE_DIR = os.path.join(BASE_DIR, "static", "firmware")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder="admin_templates")
# Панель работает за nginx под префиксом /provision/ (см. nginx/portal.conf).
# nginx передаёт заголовок X-Forwarded-Prefix, а ProxyFix заставляет
# url_for() автоматически подставлять этот префикс во все внутренние
# ссылки (иначе они указывали бы на корень портала, а не на саму панель).
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)
def _get_or_create_secret_key():
    key_path = os.path.join(BASE_DIR, ".secret_key")
    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(key_path, "w", encoding="utf-8") as f:
        f.write(key)
    os.chmod(key_path, 0o600)
    return key


app.secret_key = _get_or_create_secret_key()
# Уникальное имя cookie — по умолчанию Flask называет её "session", и на
# одном домене с cdr-panel/alert-panel (тоже держат Flask-сессию ради CSRF)
# это привело бы к коллизии: посещение одной панели затирало бы cookie
# другой, ломая CSRF-проверку без явной ошибки авторизации.
app.config["SESSION_COOKIE_NAME"] = "provision_session"
# Только /admin/* формы затрагиваются (POST) — раздача cfg<MAC>.xml/прошивок
# телефонам всегда идёт через GET и CSRFProtect её не трогает.
csrf = CSRFProtect(app)
app.jinja_loader.searchpath.append(TEMPLATES_DIR)  # доступ к XML-шаблонам через render_template

# Flask по умолчанию включает автоэкранирование только для файлов, чьё имя
# заканчивается на .html/.htm/.xml/.xhtml — наши файлы шаблонов провижининга
# называются "*.xml.j2" (заканчиваются на .j2), поэтому под это правило не
# попадают, и автоэкранирование для них было ВЫКЛЮЧЕНО. Из-за этого символы
# вроде "&", "<", ">" в имени/метке/пароле устройства ломали XML целиком —
# телефон успевал применить всё до места разрыва и молча останавливался,
# остальные настройки просто не доходили. Явно включаем автоэкранирование
# для всех *.xml.j2 файлов.
def _select_autoescape(template_name):
    return bool(template_name) and template_name.endswith((".html", ".htm", ".xml", ".xhtml", ".xml.j2"))


app.jinja_env.autoescape = _select_autoescape

# ---------- Модели устройств ----------
# Соответствие модели -> (семейство шаблона, файл шаблона)
MODEL_FAMILIES = {
    "GXP1610": ("p_value", "gxp1610_1620.xml.j2"),
    "GXP1615": ("p_value", "gxp1610_1620.xml.j2"),
    "GXP1620": ("p_value", "gxp1610_1620.xml.j2"),
    "GXP1625": ("p_value", "gxp1610_1620.xml.j2"),
    "GXP2170": ("p_value", "gxp2170.xml.j2"),
    "GRP2613": ("item_part", "grp2613_2615_2636.xml.j2"),
    "GRP2613W": ("item_part", "grp2613_2615_2636.xml.j2"),
    "GRP2615": ("item_part", "grp2613_2615_2636.xml.j2"),
    "GRP2636": ("item_part", "grp2613_2615_2636.xml.j2"),
    "GRP2650": ("item_part", "grp2613_2615_2636.xml.j2"),
    # старые названия тех же телефонов (пользователь может вводить их привычно)
    "GXP2613": ("item_part", "grp2613_2615_2636.xml.j2"),
    "GXP2615": ("item_part", "grp2613_2615_2636.xml.j2"),
    "GXP2636": ("item_part", "grp2613_2615_2636.xml.j2"),
    "GXP2650": ("item_part", "grp2613_2615_2636.xml.j2"),
}

# GXP26xx и GRP26xx — старое и новое название одних и тех же телефонов
# (см. миграцию: Grandstream переименовал линейку). Модель на устройстве и
# модель, под которой загружена прошивка, могут не совпасть текстово —
# приводим обе стороны к одному, "каноничному" имени при поиске.
MODEL_ALIASES = {
    "GXP2613": "GRP2613",
    "GXP2615": "GRP2615",
    "GXP2636": "GRP2636",
    "GXP2650": "GRP2650",
}


def canonical_model(model):
    """Приводит старое имя модели (GXP26xx) к новому (GRP26xx), если это одна
    из переименованных моделей; остальные модели возвращает как есть."""
    return MODEL_ALIASES.get(model.upper(), model.upper()) if model else model


def model_variants(model):
    """Все равнозначные написания модели (GXP2613 и GRP2613 — один телефон).
    Нужно, чтобы найти прошивку/шаблон вне зависимости от того, под каким
    именно именем модель была сохранена (старым или новым)."""
    if not model:
        return []
    model = model.upper()
    variants = {model}
    if model in MODEL_ALIASES:
        variants.add(MODEL_ALIASES[model])
    for old, new in MODEL_ALIASES.items():
        if new == model:
            variants.add(old)
    return list(variants)


# Список моделей для выпадающих списков в интерфейсе — без дублей вида
# GXP2613/GRP2613 (показываем только актуальное название), чтобы не плодить
# повторную путаницу при выборе модели в форме или на странице прошивок.
DROPDOWN_MODELS = sorted({canonical_model(m) if m in MODEL_ALIASES else m for m in MODEL_FAMILIES.keys()})

MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def get_db():
    # check_same_thread=False: с threaded=True Flask-сервер обрабатывает запросы
    # в разных потоках; каждый запрос всё равно открывает и закрывает своё
    # соединение (никакого общего сокета/состояния между запросами, в отличие
    # от AMI-панели конференций — там и была причина зависаний)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # WAL: чтения не блокируют друг друга
    # даже при одновременном провижининге сотен телефонов после массовой перезагрузки
    return conn


def init_db():
    conn = get_db()
    with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()

    # Миграция: добавляем новые колонки в уже существующую (старую) таблицу devices
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)").fetchall()}
    migrations = {
        "extension2": "ALTER TABLE devices ADD COLUMN extension2 TEXT",
        "name2": "ALTER TABLE devices ADD COLUMN name2 TEXT",
        "label2": "ALTER TABLE devices ADD COLUMN label2 TEXT",
        "sip_password2": "ALTER TABLE devices ADD COLUMN sip_password2 TEXT",
        "active2": "ALTER TABLE devices ADD COLUMN active2 INTEGER DEFAULT 0",
        "last_seen_ip": "ALTER TABLE devices ADD COLUMN last_seen_ip TEXT",
        "last_seen_at": "ALTER TABLE devices ADD COLUMN last_seen_at TEXT",
    }
    for col, ddl in migrations.items():
        if col not in existing_cols:
            conn.execute(ddl)
    conn.commit()

    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('config_enabled', '1')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('firmware_enabled', '1')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('color_bluetooth_enabled', '0')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('color_screensaver_enabled', '0')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('color_brightness', '100')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('color_wifi_enabled', '0')")

    # Миграция на настройки по семействам телефонов: если раньше уже были
    # общие значения (sip_server, ntp_server и т.п. без префикса) — переносим
    # их как стартовые для ВСЕХ трёх семейств, чтобы уже подтверждённые тестами
    # значения (часовой пояс, язык и т.д.) не потерялись при обновлении.
    for _fam in ("1610", "2170", "grp"):
        for _field in SETTINGS_FIELDS:
            _old = conn.execute("SELECT value FROM settings WHERE key = ?", (_field,)).fetchone()
            if _old:
                # старое общее значение реально было (обновление с предыдущей версии) —
                # используем его, перезаписывая дефолт, который schema.sql уже вставил
                conn.execute(
                    "UPDATE settings SET value = ? WHERE key = ?", (_old["value"], f"{_fam}_{_field}")
                )
            # если старого значения не было (свежая установка) — не трогаем,
            # оставляем дефолт из schema.sql как есть
    for _fam, _fields in COLOR_FIELDS.items():
        for _field in _fields:
            _old = conn.execute("SELECT value FROM settings WHERE key = ?", (_field,)).fetchone()
            if _old:
                conn.execute(
                    "UPDATE settings SET value = ? WHERE key = ?", (_old["value"], f"{_fam}_{_field}")
                )
    conn.commit()

    # Таблица panel_users в схеме остаётся, но больше не используется для
    # входа — логин/пароль теперь на портале (sso-auth). Локального admin
    # с автогенерацией пароля при первом запуске больше не создаём —
    # заходить сюда с ним уже некуда (страницы /login у панели нет).
    conn.close()


def get_settings(conn):
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# Разделение настроек по семействам телефонов — у каждого свои SIP/NTP/язык/
# пароль и (где применимо) Bluetooth/WiFi/яркость/заставка, независимо друг
# от друга. Ключи в БД хранятся с префиксом семейства (например "1610_sip_server"),
# а шаблонам *.xml.j2 отдаётся обычный "плоский" словарь без префикса (s.sip_server
# и т.д.) — сами шаблоны переписывать не пришлось.
FAMILY_PREFIX = {
    "gxp1610_1620.xml.j2": "1610",
    "gxp2170.xml.j2": "2170",
    "grp2613_2615_2636.xml.j2": "grp",
}
FAMILY_LABELS = {
    "1610": "GXP1610 / GXP1620",
    "2170": "GXP2170",
    "grp": "GRP2613 / GRP2615 / GRP2636",
}
SETTINGS_FIELDS = [
    "sip_server", "sip_port", "config_server", "firmware_server",
    "ntp_server", "timezone", "language", "admin_password",
    "date_format", "time_format",
]
# action_uri_ip намеренно убран отсюда и зафиксирован как "any" прямо в
# шаблонах (см. комментарий там) — редактируемым это поле уже дважды
# приводило к отказу массовой перезагрузки на практике.
COLOR_FIELDS = {
    "2170": ["color_bluetooth_enabled", "color_screensaver_enabled", "color_brightness"],
    "grp": ["color_bluetooth_enabled", "color_screensaver_enabled", "color_brightness", "color_wifi_enabled"],
}


def get_family_settings(conn, prefix):
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE ?", (f"{prefix}_%",)
    ).fetchall()
    return {r["key"][len(prefix) + 1:]: r["value"] for r in rows}


def normalize_mac(raw):
    return re.sub(r"[^0-9a-fA-F]", "", raw).lower()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _trusted_remote_user():
            abort(401)
        return view(*args, **kwargs)
    return wrapped


# ---------- Интеграция с FreePBX: автоподстановка данных добавочного ----------

def get_freepbx_db_config():
    """Читает креды БД из /etc/freepbx.conf. Понимает и одинарные, и двойные
    кавычки вокруг ключа/значения (на боевом сервере ключи в двойных)."""
    conf_path = "/etc/freepbx.conf"
    if not os.path.exists(conf_path):
        return None
    with open(conf_path, encoding="utf-8") as f:
        content = f.read()
    result = {}
    mapping = {
        "AMPDBHOST": "host",
        "AMPDBNAME": "database",
        "AMPDBUSER": "user",
        "AMPDBPASS": "password",
    }
    for key, field in mapping.items():
        m = re.search(
            r'\$amp_conf\[["\']' + key + r'["\']\]\s*=\s*["\']([^"\']*)["\']', content
        )
        if m:
            result[field] = m.group(1)
    if "host" not in result:
        result["host"] = "localhost"
    if "database" not in result:
        result["database"] = "asterisk"
    if "user" not in result or "password" not in result:
        return None
    return result


def lookup_freepbx_extension(extension):
    """Возвращает {name, sip_password} по номеру добавочного из БД FreePBX,
    либо None если не найден / БД недоступна."""
    cfg = get_freepbx_db_config()
    if not cfg:
        return None
    try:
        import pymysql
        conn = pymysql.connect(
            host=cfg["host"], user=cfg["user"], password=cfg["password"],
            database=cfg["database"], connect_timeout=3,
        )
        try:
            with conn.cursor() as cur:
                name = ""
                cur.execute(
                    "SELECT COLUMN_NAME FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'users'",
                    (cfg["database"],),
                )
                available_cols = {row[0] for row in cur.fetchall()}
                for candidate in ("description", "name", "displayname", "fullname", "extension"):
                    if candidate in available_cols:
                        cur.execute(
                            f"SELECT `{candidate}` FROM users WHERE extension = %s", (extension,)
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            name = row[0]
                        break

                password = ""
                # PJSIP (новая нормализованная схема): пароль в ps_auths, id обычно = номеру.
                # Таблицы может не быть вовсе (старая/гибридная схема хранения) — не считаем это фатальной ошибкой.
                try:
                    cur.execute("SELECT password FROM ps_auths WHERE id = %s", (extension,))
                    row = cur.fetchone()
                    if row:
                        password = row[0]
                except Exception:
                    pass

                if not password:
                    # Старая схема id/keyword/data (таблица sip) — используется и для chan_pjsip
                    # на некоторых конфигурациях FreePBX, не только для chan_sip
                    try:
                        cur.execute(
                            "SELECT data FROM sip WHERE id = %s AND keyword = 'secret'", (extension,)
                        )
                        row = cur.fetchone()
                        if row:
                            password = row[0]
                    except Exception:
                        pass

            if not name and not password:
                return None
            return {"name": name, "sip_password": password}
        finally:
            conn.close()
    except Exception as e:
        app.logger.warning(f"Не удалось получить данные добавочного {extension} из FreePBX: {e}")
        return None


@app.route("/admin/api/extension/<extension>")
@login_required
def api_extension_lookup(extension):
    data = lookup_freepbx_extension(extension)
    if not data:
        return jsonify({"found": False}), 404
    return jsonify({"found": True, **data})




# ---------- SSO ----------
# Вход/пароли теперь в центральном sso-auth (порт 8080). Панель доверяет
# заголовкам X-Remote-User/X-Remote-Role от nginx — НО, в отличие от
# остальных панелей проекта, эта не может слушать только 127.0.0.1: часть
# её маршрутов (/cfg<mac>.xml, /firmware/..., корневой fallback) обязаны
# оставаться доступны телефонам по всей локальной сети напрямую, без SSO.
#
# Поэтому здесь заголовку доверяем ТОЛЬКО если сам запрос физически пришёл
# с loopback (т.е. от нашего же nginx, который проксирует локально) — иначе
# кто угодно в локальной сети мог бы подделать X-Remote-User прямым
# запросом на порт панели в обход всякой авторизации.

def _trusted_remote_user():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return None
    return request.headers.get("X-Remote-User")


@app.before_request
def load_remote_user():
    g.remote_user = _trusted_remote_user()
    g.remote_role = request.headers.get("X-Remote-Role", "operator") if g.remote_user else None
    # Имя доверяем ровно на тех же условиях, что и логин (только с loopback);
    # закодировано процентно на стороне портала — кириллица небезопасна в
    # сыром виде для HTTP-заголовков.
    if g.remote_user:
        remote_name = request.headers.get("X-Remote-Name")
        g.remote_name = unquote(remote_name) if remote_name else g.remote_user
    else:
        g.remote_name = None


@app.after_request
def _no_cache_admin_pages(response):
    """Страницы /admin/* содержат CSRF-токен, привязанный к текущей сессии.
    Если браузер закэширует такую страницу (например, после навигации назад
    или между перезапусками сервиса/сменой cookie), пользователь отправит
    форму со СТАРЫМ токеном — и получит невнятную "CSRF token missing" вместо
    явной причины. Запрещаем кэширование этих страниц вовсе."""
    if request.path.startswith("/admin/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ---------- Провижининг: то, что реально запрашивают телефоны ----------

@app.route("/cfg<mac>.xml")
def provision_config(mac):
    """Основной эндпоинт провижининга. Телефон запрашивает cfg<MAC>.xml."""
    mac = normalize_mac(mac)
    if not MAC_RE.match(mac):
        return Response("Bad MAC", status=400)

    conn = get_db()

    # Глобальный рубильник: если раздача конфигов выключена в Настройках —
    # никому ничего не отдаём, независимо от реестра устройств. Нужен как
    # аварийная кнопка, если что-то на новой сети идёт не по плану, а также
    # чтобы старые телефоны с Yeastar случайно не "утекли" на новую систему
    # раньше времени, пока раздаётся широкая DHCP-опция 66.
    config_enabled = conn.execute(
        "SELECT value FROM settings WHERE key = 'config_enabled'"
    ).fetchone()
    if config_enabled and config_enabled["value"] != "1":
        conn.close()
        return Response(status=404)

    device = conn.execute("SELECT * FROM devices WHERE mac = ? AND active = 1", (mac,)).fetchone()
    if not device:
        conn.close()
        return Response(status=404)  # телефон не найден в реестре — не провижинится, как у Yeastar

    model = device["model"]
    family = MODEL_FAMILIES.get(model.upper())
    if not family:
        conn.close()
        return Response(f"Unknown model {model}", status=404)

    _, template_file = family
    s = get_family_settings(conn, FAMILY_PREFIX[template_file])
    blf_keys = conn.execute(
        "SELECT * FROM blf_keys WHERE device_id = ? ORDER BY key_index", (device["id"],)
    ).fetchall()
    variants = model_variants(model)
    placeholders = ",".join("?" * len(variants))
    fw_row = conn.execute(
        f"SELECT filename FROM firmware WHERE model IN ({placeholders}) AND enabled = 1 LIMIT 1", variants
    ).fetchone()
    firmware_file = fw_row["filename"] if fw_row else ""

    # Телефон сам сообщает нам свой IP, обращаясь за конфигом — сохраняем,
    # чтобы видеть в списке устройств (аналогично колонке IP-адрес в Yeastar)
    phone_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if phone_ip:
        phone_ip = phone_ip.split(",")[0].strip()
    conn.execute(
        "UPDATE devices SET last_seen_ip = ?, last_seen_at = ? WHERE id = ?",
        (phone_ip, datetime.now().isoformat(timespec="seconds"), device["id"]),
    )
    conn.commit()
    conn.close()

    xml = render_template(
        template_file,
        mac=mac,
        device=device,
        s=s,
        blf_keys=blf_keys,
        firmware_file=firmware_file,
    )
    return Response(xml, mimetype="text/xml")


def _requester_is_active_device(remote_addr):
    """Разрешаем раздачу прошивки только тем, чей IP реально принадлежит
    активному устройству из реестра (сверяем с last_seen_ip — он обновляется
    только для активных, так что деактивированное устройство "выпадает" отсюда,
    даже если знает точное имя файла прошивки)."""
    if not remote_addr:
        return False
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM devices WHERE active = 1 AND last_seen_ip = ?", (remote_addr,)
    ).fetchone()
    conn.close()
    return bool(row)


def _log_firmware_request(filename):
    # Временная диагностика: некоторые телефоны присылали 200 с пустым телом
    # при отдаче прошивки. Заголовки запроса оказались обычными (Host,
    # User-Agent, Accept — без Range/If-*), файл на диске присутствует
    # правильного размера — то есть дело не в данных и не в условных
    # заголовках, а в самом механизме отдачи (см. _stream_file_response).
    print(
        f"[firmware-debug] {filename}, "
        f"headers={dict(request.headers)}",
        flush=True,
    )
    fw_path = os.path.join(FIRMWARE_DIR, filename)
    print(
        f"[firmware-debug] on-disk path={fw_path} exists={os.path.exists(fw_path)} "
        f"size={os.path.getsize(fw_path) if os.path.exists(fw_path) else 'n/a'}",
        flush=True,
    )


def _stream_file_response(path):
    """Отдаёт файл вручную через генератор, кусками по 64 КБ, вместо
    send_from_directory/send_file. send_from_directory пытается использовать
    эффективную нулевую копию через wsgi.file_wrapper/sendfile() — на этом
    сервере с gunicorn (gthread worker) для большого файла (~107 МБ) это
    приводило к ответу 200 с пустым телом вместо самого файла, хотя файл на
    диске присутствовал корректного размера и запрос не содержал Range/
    If-Modified-Since/If-None-Match. Ручной генератор проходит через обычный
    путь WSGI-итерации и не использует этот механизм вовсе."""
    size = os.path.getsize(path)

    def generate():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk

    resp = Response(generate(), mimetype="application/octet-stream")
    resp.headers["Content-Length"] = str(size)
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{os.path.basename(path)}"'
    )
    return resp


@app.route("/firmware/<path:filename>")
def firmware_file(filename):
    conn = get_db()
    firmware_enabled = conn.execute(
        "SELECT value FROM settings WHERE key = 'firmware_enabled'"
    ).fetchone()
    conn.close()
    if firmware_enabled and firmware_enabled["value"] != "1":
        return Response(status=404)
    if not _requester_is_active_device(request.remote_addr):
        return Response(status=404)
    _log_firmware_request(filename)
    fw_path = os.path.join(FIRMWARE_DIR, filename)
    if not os.path.exists(fw_path):
        return Response(status=404)
    return _stream_file_response(fw_path)


@app.route("/<string:filename>")
def firmware_file_root_fallback(filename):
    """Некоторые старые прошивки GXP16xx игнорируют папку в Firmware Server Path
    и всегда запрашивают файл прямо в корне сервера, а не по заданному пути
    (подтверждено: GXP1610 со старой прошивкой запрашивает /gxp1600fw.bin вместо
    /firmware/gxp1600fw.bin даже после полного сброса к заводским). Подстраховка —
    отдаём известные файлы прошивок ещё и с корня. Отдаём только то, что реально
    зарегистрировано в таблице firmware — не открываем произвольные файлы с диска."""
    conn = get_db()
    firmware_enabled = conn.execute(
        "SELECT value FROM settings WHERE key = 'firmware_enabled'"
    ).fetchone()
    known = conn.execute("SELECT 1 FROM firmware WHERE filename = ?", (filename,)).fetchone()
    conn.close()
    if not known or (firmware_enabled and firmware_enabled["value"] != "1"):
        return Response(status=404)
    if not _requester_is_active_device(request.remote_addr):
        return Response(status=404)
    _log_firmware_request(filename)
    fw_path = os.path.join(FIRMWARE_DIR, filename)
    if not os.path.exists(fw_path):
        return Response(status=404)
    return _stream_file_response(fw_path)


# ---------- Веб-интерфейс администратора ----------

@app.route("/")
def index():
    return redirect(url_for("devices_list"))


SORTABLE_COLUMNS = {
    "mac": "mac",
    "extension": "extension",
    "name": "name",
    "model": "model",
    "ip": "last_seen_ip",
    "active": "active",
}


@app.route("/admin/devices")
@login_required
def devices_list():
    q = request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    sort = request.args.get("sort", "mac")
    direction = request.args.get("dir", "asc")
    if sort not in SORTABLE_COLUMNS:
        sort = "mac"
    if direction not in ("asc", "desc"):
        direction = "asc"
    order_column = SORTABLE_COLUMNS[sort]

    conn = get_db()
    if q:
        like = f"%{q}%"
        where = """WHERE mac LIKE ? OR extension LIKE ? OR name LIKE ? OR label LIKE ?
                   OR extension2 LIKE ? OR name2 LIKE ? OR label2 LIKE ?"""
        params = (like, like, like, like, like, like, like)
    else:
        where = ""
        params = ()
    total = conn.execute(f"SELECT COUNT(*) AS n FROM devices {where}", params).fetchone()["n"]
    total_pages = max(1, (total + DEVICES_PER_PAGE - 1) // DEVICES_PER_PAGE)
    page = min(page, total_pages)
    offset = (page - 1) * DEVICES_PER_PAGE
    # order_column берётся строго из белого списка SORTABLE_COLUMNS выше — не
    # из request.args напрямую, так что подстановка в SQL здесь безопасна.
    rows = conn.execute(
        f"SELECT * FROM devices {where} ORDER BY {order_column} COLLATE NOCASE {direction.upper()}, id DESC LIMIT ? OFFSET ?",
        params + (DEVICES_PER_PAGE, offset),
    ).fetchall()
    conn.close()
    return render_template(
        "devices_list.html", devices=rows, q=q, models=DROPDOWN_MODELS,
        page=page, total_pages=total_pages, total=total, page_size=DEVICES_PER_PAGE,
        sort=sort, direction=direction,
    )


@app.route("/admin/devices/new", methods=["GET", "POST"])
@login_required
def device_new():
    conn = get_db()
    if request.method == "POST":
        mac = normalize_mac(request.form["mac"])
        if not MAC_RE.match(mac):
            flash("Некорректный MAC-адрес", "error")
            conn.close()
            return redirect(url_for("device_new"))
        try:
            conn.execute(
                """INSERT INTO devices (mac, extension, name, manufacturer, model, label, sip_password, active,
                                         extension2, name2, label2, sip_password2, active2, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mac,
                    request.form.get("extension", ""),
                    request.form.get("name", ""),
                    request.form.get("manufacturer", "Grandstream"),
                    request.form["model"],
                    request.form.get("label", request.form.get("name", "")),
                    request.form.get("sip_password", ""),
                    1 if request.form.get("active") else 0,
                    request.form.get("extension2", ""),
                    request.form.get("name2", ""),
                    request.form.get("label2", ""),
                    request.form.get("sip_password2", ""),
                    1 if request.form.get("active2") else 0,
                    request.form.get("notes", ""),
                ),
            )
            conn.commit()
            flash("Устройство добавлено", "ok")
        except sqlite3.IntegrityError:
            flash("Устройство с таким MAC уже существует", "error")
        conn.close()
        return redirect(url_for("devices_list"))
    conn.close()
    return render_template("device_form.html", device=None, models=DROPDOWN_MODELS)


@app.route("/admin/devices/<int:device_id>/edit", methods=["GET", "POST"])
@login_required
def device_edit(device_id):
    conn = get_db()
    if request.method == "POST":
        conn.execute(
            """UPDATE devices SET extension=?, name=?, manufacturer=?, model=?, label=?,
               sip_password=?, active=?, extension2=?, name2=?, label2=?, sip_password2=?, active2=?,
               notes=?, updated_at=? WHERE id=?""",
            (
                request.form.get("extension", ""),
                request.form.get("name", ""),
                request.form.get("manufacturer", "Grandstream"),
                request.form["model"],
                request.form.get("label", ""),
                request.form.get("sip_password", ""),
                1 if request.form.get("active") else 0,
                request.form.get("extension2", ""),
                request.form.get("name2", ""),
                request.form.get("label2", ""),
                request.form.get("sip_password2", ""),
                1 if request.form.get("active2") else 0,
                request.form.get("notes", ""),
                datetime.now().isoformat(),
                device_id,
            ),
        )
        conn.commit()
        conn.close()
        flash("Сохранено", "ok")
        return redirect(url_for("devices_list"))
    device = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    blf_keys = conn.execute("SELECT * FROM blf_keys WHERE device_id = ? ORDER BY key_index", (device_id,)).fetchall()
    conn.close()
    return render_template("device_form.html", device=device, blf_keys=blf_keys, models=DROPDOWN_MODELS)


@app.route("/admin/devices/<int:device_id>/delete", methods=["POST"])
@login_required
def device_delete(device_id):
    conn = get_db()
    conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    conn.commit()
    conn.close()
    flash("Устройство удалено", "ok")
    return redirect(url_for("devices_list"))


@app.route("/admin/devices/<int:device_id>/toggle-active", methods=["POST"])
@login_required
def device_toggle_active(device_id):
    conn = get_db()
    conn.execute("UPDATE devices SET active = 1 - active WHERE id = ?", (device_id,))
    conn.commit()
    conn.close()
    # Возвращаемся туда же, откуда пришли (сохраняем поиск/фильтр в списке)
    return redirect(request.referrer or url_for("devices_list"))


@app.route("/admin/devices/deactivate-all", methods=["POST"])
@login_required
def devices_deactivate_all():
    conn = get_db()
    cur = conn.execute("UPDATE devices SET active = 0 WHERE active = 1")
    conn.commit()
    conn.close()
    flash(f"Деактивировано устройств: {cur.rowcount}. Включайте вручную только те MAC, что реально тестируете.", "ok")
    return redirect(url_for("devices_list"))


@app.route("/admin/devices/activate-all", methods=["POST"])
@login_required
def devices_activate_all():
    conn = get_db()
    cur = conn.execute("UPDATE devices SET active = 1 WHERE active = 0")
    conn.commit()
    conn.close()
    flash(f"Активировано устройств: {cur.rowcount}.", "ok")
    return redirect(url_for("devices_list"))


# ---------- Массовая перезагрузка ----------
# Grandstream reboot API: GET http://<IP-телефона>/cgi-bin/api-sys_operation?
# passcode=<admin-пароль>&request=REBOOT. Требует на телефоне включённый
# "Enable Action URI Support" (Network -> Remote Control) — иначе телефон
# просто не ответит на запрос. Пока не завезено в шаблоны провижининга
# автоматически (нет подтверждённых P-value номеров для этой настройки и для
# "Remote Control Pop-up Window Support" — до их подтверждения диффом
# реального конфига перезагрузка по одиночным телефонам, где Action URI ещё
# не включён вручную, будет требовать физического подтверждения на экране).

REBOOT_STAGGER_SECONDS = 10  # пауза между отдельными командами reboot внутри пачки
DEVICES_PER_PAGE = 25       # он же размер пачки для массовой перезагрузки


def _reboot_device(ip, admin_password, timeout=5):
    """Отправляет команду перезагрузки на один телефон. Возвращает (ok, error)."""
    try:
        resp = requests.get(
            f"http://{ip}/cgi-bin/api-sys_operation",
            params={"passcode": admin_password, "request": "REBOOT"},
            timeout=timeout,
        )
        return resp.ok, None if resp.ok else f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        return False, str(e)


def _admin_password_for_model(conn, model):
    """Пароль администратора того семейства настроек, к которому относится
    модель устройства — у каждой вкладки (1610/1620, 2170, GRP) он свой."""
    family = MODEL_FAMILIES.get((model or "").upper())
    if not family:
        return None
    _, template_file = family
    s = get_family_settings(conn, FAMILY_PREFIX[template_file])
    return s.get("admin_password")


def _mass_reboot_worker(device_ids):
    """Работает в фоновом потоке: перезагружает устройства по одному с паузой
    между ними, чтобы не поднимать полную пачку DHCP/SIP REGISTER одним
    залпом. Каждое устройство — отдельное соединение с БД (SQLite +
    check_same_thread=False, как и везде в проекте)."""
    conn = get_db()
    for device_id in device_ids:
        device = conn.execute(
            "SELECT * FROM devices WHERE id = ? AND active = 1", (device_id,)
        ).fetchone()
        if not device or not device["last_seen_ip"]:
            continue
        admin_password = _admin_password_for_model(conn, device["model"])
        if not admin_password:
            continue
        ok, err = _reboot_device(device["last_seen_ip"], admin_password)
        print(
            f"[mass-reboot] device_id={device_id} mac={device['mac']} "
            f"ip={device['last_seen_ip']} ok={ok} err={err}",
            flush=True,
        )
        time.sleep(REBOOT_STAGGER_SECONDS)
    conn.close()


@app.route("/admin/devices/mass-reboot", methods=["POST"])
@login_required
def devices_mass_reboot():
    device_ids = [int(x) for x in request.form.getlist("device_ids")]
    if not device_ids:
        flash("Не выбрано ни одного устройства для перезагрузки.", "error")
        return redirect(request.referrer or url_for("devices_list"))
    conn = get_db()
    placeholders = ",".join("?" * len(device_ids))
    known = conn.execute(
        f"SELECT COUNT(*) AS n FROM devices WHERE id IN ({placeholders}) "
        f"AND active = 1 AND last_seen_ip IS NOT NULL AND last_seen_ip != ''",
        device_ids,
    ).fetchone()["n"]
    conn.close()
    if known == 0:
        flash("У выбранных устройств нет известного IP-адреса (ещё не выходили на связь) — перезагрузка невозможна.", "error")
        return redirect(request.referrer or url_for("devices_list"))
    threading.Thread(target=_mass_reboot_worker, args=(device_ids,), daemon=True).start()
    eta = known * REBOOT_STAGGER_SECONDS
    flash(
        f"Команда перезагрузки отправляется на {known} устройств (~{eta} сек, "
        f"по {REBOOT_STAGGER_SECONDS} сек между телефонами). Если на телефоне не включён "
        f"Action URI Support — потребуется подтвердить перезагрузку физически на экране.",
        "ok",
    )
    return redirect(request.referrer or url_for("devices_list"))


@app.route("/admin/devices/<int:device_id>/reboot", methods=["POST"])
@login_required
def device_reboot(device_id):
    conn = get_db()
    device = conn.execute("SELECT * FROM devices WHERE id = ? AND active = 1", (device_id,)).fetchone()
    if not device or not device["last_seen_ip"]:
        conn.close()
        flash("У устройства нет известного IP-адреса — перезагрузка невозможна.", "error")
        return redirect(request.referrer or url_for("devices_list"))
    admin_password = _admin_password_for_model(conn, device["model"])
    conn.close()
    if not admin_password:
        flash("Не удалось определить пароль администратора для этой модели.", "error")
        return redirect(request.referrer or url_for("devices_list"))
    ok, err = _reboot_device(device["last_seen_ip"], admin_password)
    if ok:
        flash(f"Команда перезагрузки отправлена на {device['mac']}.", "ok")
    else:
        flash(f"Не удалось отправить команду перезагрузки на {device['mac']}: {err}", "error")
    return redirect(request.referrer or url_for("devices_list"))


@app.route("/admin/devices/<int:device_id>/blf", methods=["POST"])
@login_required
def blf_add(device_id):
    conn = get_db()
    max_idx = conn.execute(
        "SELECT COALESCE(MAX(key_index), 0) AS m FROM blf_keys WHERE device_id = ?", (device_id,)
    ).fetchone()["m"]
    conn.execute(
        "INSERT INTO blf_keys (device_id, key_index, description, value, key_mode) VALUES (?, ?, ?, ?, ?)",
        (device_id, max_idx + 1, request.form["description"], request.form["value"], request.form.get("key_mode", "BLF")),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("device_edit", device_id=device_id))


@app.route("/admin/blf/<int:blf_id>/delete", methods=["POST"])
@login_required
def blf_delete(blf_id):
    conn = get_db()
    row = conn.execute("SELECT device_id FROM blf_keys WHERE id = ?", (blf_id,)).fetchone()
    conn.execute("DELETE FROM blf_keys WHERE id = ?", (blf_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("device_edit", device_id=row["device_id"]))


# ---------- Массовый импорт из xlsx (Yeastar_device_inventory.xlsx) ----------

@app.route("/admin/export-yeastar-disable")
@login_required
def export_yeastar_disable():
    """CSV в формате импорта устройств Yeastar (Manufacturer,Phonetype,Mac Address,
    Template,Extension,Label,Active) с Active=No — чтобы одним импортом в Yeastar
    отключить там все уже мигрированные к нам устройства и защититься от того,
    что Yeastar случайно перезапишет их конфиг/прошивку обратно.
    Актуально только для 1610/1620/2170 — под GRP2613/2636/2615 у Yeastar шаблонов
    нет вовсе, там нечего отключать."""
    import csv
    from io import StringIO

    # Соответствие модели и имени шаблона в самой Yeastar (как было настроено там)
    YEASTAR_TEMPLATE = {
        "GXP1610": "Grand1610",
        "GXP1620": "Grand1620",
        "GXP2170": "grand2170",
    }

    conn = get_db()
    rows = conn.execute(
        f"""SELECT * FROM devices WHERE active = 1 AND model IN
            ({",".join("?" * len(YEASTAR_TEMPLATE))}) ORDER BY mac""",
        list(YEASTAR_TEMPLATE.keys()),
    ).fetchall()
    conn.close()

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Manufacturer", "Phonetype", "Mac Address", "Template", "Extension", "Label", "Active"])
    for d in rows:
        mac_colons = ":".join(d["mac"][i:i + 2] for i in range(0, 12, 2))
        writer.writerow([
            d["manufacturer"] or "Grandstream",
            d["model"],
            mac_colons,
            YEASTAR_TEMPLATE.get(d["model"], ""),
            d["extension"] or "",
            d["label"] or d["name"] or "",
            "No",
        ])

    filename = f"yeastar_disable_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),  # BOM для корректного открытия кириллицы в Excel
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/admin/export")
@login_required
def export_devices():
    import openpyxl
    from io import BytesIO

    conn = get_db()
    rows = conn.execute("SELECT * FROM devices ORDER BY mac").fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Устройства"
    ws.append([
        "MAC-адрес", "Номер (линия 1)", "Имя", "Производитель", "Модель", "Метка (линия 1)",
        "Пароль SIP (линия 1)", "Активна (линия 1)",
        "Номер (линия 2)", "Имя (линия 2)", "Метка (линия 2)", "Пароль SIP (линия 2)", "Активна (линия 2)",
        "IP-адрес", "Последний раз на связи", "Заметки",
    ])
    for d in rows:
        ws.append([
            d["mac"], d["extension"], d["name"], d["manufacturer"], d["model"], d["label"],
            d["sip_password"], "да" if d["active"] else "нет",
            d["extension2"], d["name2"], d["label2"], d["sip_password2"], "да" if d["active2"] else "нет",
            d["last_seen_ip"], d["last_seen_at"], d["notes"],
        ])
    for col in ws.columns:
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"provisioning_devices_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/admin/import", methods=["GET", "POST"])
@login_required
def import_devices():
    if request.method == "POST":
        f = request.files.get("file")
        if not f:
            flash("Файл Yeastar (xlsx) не выбран", "error")
            return redirect(url_for("import_devices"))

        # Необязательный CSV с реальными данными из FreePBX (extension,name,tech,secret)
        freepbx_data = {}
        csv_file = request.files.get("freepbx_csv")
        if csv_file and csv_file.filename:
            import csv, io
            text = csv_file.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                ext = str(row.get("extension", "")).strip()
                if ext:
                    freepbx_data[ext] = {
                        "name": (row.get("name") or "").strip(),
                        "secret": (row.get("secret") or "").strip(),
                    }

        import openpyxl
        wb = openpyxl.load_workbook(f, data_only=True)
        ws = wb.active
        conn = get_db()
        added, updated, skipped = 0, 0, 0

        for row in ws.iter_rows(min_row=2, values_only=True):
            mac_raw, ext_raw, name_raw, manuf, model, template, source = (list(row) + [None] * 7)[:7]
            if not mac_raw or not model:
                skipped += 1
                continue
            mac = normalize_mac(str(mac_raw))
            if not MAC_RE.match(mac):
                skipped += 1
                continue

            # У части устройств Yeastar в одном MAC настроено 2 линии —
            # номер/имя записаны через запятую. Раскладываем на линию 1 / линию 2,
            # а реальные имя и пароль подставляем из FreePBX CSV, если он дан
            # (это авторитетный источник — в самой Yeastar-выгрузке пароля не было).
            exts = [e.strip() for e in str(ext_raw or "").split(",") if e.strip()]
            names = [n.strip() for n in str(name_raw or "").split(",") if n.strip()]

            def resolve(idx):
                ext = exts[idx] if idx < len(exts) else ""
                fallback_name = names[idx] if idx < len(names) else (names[0] if names else "")
                fp = freepbx_data.get(ext, {})
                name = fp.get("name") or fallback_name
                secret = fp.get("secret", "")
                return ext, name, secret

            ext1, name1, secret1 = resolve(0)
            ext2, name2, secret2 = resolve(1) if len(exts) > 1 else ("", "", "")

            manuf_val = str(manuf or "Grandstream")
            model_val = str(model)

            existing = conn.execute("SELECT id FROM devices WHERE mac = ?", (mac,)).fetchone()
            if existing:
                conn.execute(
                    """UPDATE devices SET extension=?, name=?, manufacturer=?, model=?, label=?,
                       sip_password=?, extension2=?, name2=?, label2=?, sip_password2=?, active2=?,
                       updated_at=? WHERE mac=?""",
                    (
                        ext1, name1, manuf_val, model_val, name1,
                        secret1, ext2, name2, name2, secret2, 1 if ext2 else 0,
                        datetime.now().isoformat(), mac,
                    ),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO devices (mac, extension, name, manufacturer, model, label,
                                             sip_password, extension2, name2, label2, sip_password2, active2)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (mac, ext1, name1, manuf_val, model_val, name1,
                     secret1, ext2, name2, name2, secret2, 1 if ext2 else 0),
                )
                added += 1

        conn.commit()
        conn.close()
        note = "" if freepbx_data else " (пароли не заполнены — CSV из FreePBX не был загружен)"
        flash(f"Добавлено: {added}, обновлено: {updated}, пропущено: {skipped}{note}", "ok")
        return redirect(url_for("devices_list"))
    return render_template("import.html")


# ---------- Настройки и прошивки ----------

@app.route("/admin/settings/<family>", methods=["GET", "POST"])
@login_required
def settings_page(family):
    if family not in FAMILY_LABELS:
        return Response("Unknown family", status=404)
    conn = get_db()
    if request.method == "POST":
        for field in SETTINGS_FIELDS:
            if field in request.form:
                value = request.form[field]
                if field == "timezone" and value == "__custom__":
                    value = request.form.get("timezone_custom", "").strip() or "auto"
                conn.execute(
                    "UPDATE settings SET value = ? WHERE key = ?",
                    (value, f"{family}_{field}"),
                )
        # Аварийные рубильники — общие для всех семейств, ключи без префикса
        for global_key in ("config_enabled", "firmware_enabled"):
            if global_key in request.form:
                conn.execute(
                    "UPDATE settings SET value = ? WHERE key = ?",
                    (request.form[global_key], global_key),
                )
        conn.commit()
        flash("Настройки сохранены", "ok")
    s = get_family_settings(conn, family)
    global_switches = get_settings(conn)
    conn.close()
    return render_template(
        "settings.html", s=s, family=family, families=FAMILY_LABELS, label=FAMILY_LABELS[family],
        g=global_switches,
    )


@app.route("/admin/settings")
@login_required
def settings_page_default():
    return redirect(url_for("settings_page", family="1610"))


@app.route("/admin/color-settings/<family>", methods=["GET", "POST"])
@login_required
def color_settings_page(family):
    """Отдельные настройки для цветных моделей (GXP2170, GRP2615/2636) —
    Bluetooth, заставка, яркость (и WiFi только у GRP). У простых 1610/1620
    их просто нет — своей вкладки для них тут не будет."""
    if family not in COLOR_FIELDS:
        return Response("Unknown family", status=404)
    conn = get_db()
    if request.method == "POST":
        for field in COLOR_FIELDS[family]:
            if field == "color_brightness":
                continue
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                ("1" if request.form.get(field) == "1" else "0", f"{family}_{field}"),
            )
        brightness = request.form.get("color_brightness", "100")
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?", (brightness, f"{family}_color_brightness")
        )
        conn.commit()
        flash("Настройки сохранены", "ok")
    s = get_family_settings(conn, family)
    conn.close()
    color_families = {k: v for k, v in FAMILY_LABELS.items() if k in COLOR_FIELDS}
    return render_template(
        "color_settings.html", s=s, family=family, families=color_families,
        label=FAMILY_LABELS[family], has_wifi="color_wifi_enabled" in COLOR_FIELDS[family],
    )


@app.route("/admin/color-settings")
@login_required
def color_settings_page_default():
    return redirect(url_for("color_settings_page", family="2170"))


@app.route("/admin/firmware", methods=["GET", "POST"])
@login_required
def firmware_page():
    MIN_FIRMWARE_SIZE = 500 * 1024  # 500 КБ — меньше этого почти наверняка обрыв/битый файл
    conn = get_db()
    if request.method == "POST":
        model = request.form["model"]
        f = request.files.get("file")
        if f and f.filename:
            filename = f.filename
            filepath = os.path.join(FIRMWARE_DIR, filename)
            f.save(filepath)
            actual_size = os.path.getsize(filepath)
            conn.execute(
                "INSERT INTO firmware (model, filename, enabled) VALUES (?, ?, 1) "
                "ON CONFLICT(model) DO UPDATE SET filename=excluded.filename, enabled=1",
                (model, filename),
            )
            conn.commit()
            if actual_size < MIN_FIRMWARE_SIZE:
                flash(
                    f"Файл загружен, но подозрительно маленький ({actual_size // 1024} КБ) — "
                    f"похоже на обрыв при загрузке или повреждённый файл. Проверьте и перезалейте, "
                    f"прежде чем раздавать телефонам.",
                    "error",
                )
            else:
                size_mb = actual_size / (1024 * 1024)
                flash(f"Прошивка для {model} обновлена ({size_mb:.1f} МБ)", "ok")
    rows = conn.execute("SELECT * FROM firmware").fetchall()
    conn.close()
    firmware_with_size = []
    for row in rows:
        d = dict(row)
        fpath = os.path.join(FIRMWARE_DIR, row["filename"]) if row["filename"] else None
        d["size_mb"] = round(os.path.getsize(fpath) / (1024 * 1024), 1) if fpath and os.path.exists(fpath) else None
        firmware_with_size.append(d)
    return render_template("firmware.html", firmware=firmware_with_size, models=DROPDOWN_MODELS)


@app.route("/admin/firmware/<model>/delete", methods=["POST"])
@login_required
def firmware_delete(model):
    conn = get_db()
    row = conn.execute("SELECT filename FROM firmware WHERE model = ?", (model,)).fetchone()
    if row and row["filename"]:
        fpath = os.path.join(FIRMWARE_DIR, row["filename"])
        # Файл может отсутствовать физически (уже удалён вручную, обрыв
        # загрузки и т.п.) — не считаем это ошибкой, всё равно чистим запись в БД
        if os.path.exists(fpath):
            os.remove(fpath)
    conn.execute("DELETE FROM firmware WHERE model = ?", (model,))
    conn.commit()
    conn.close()
    flash(f"Прошивка для {model} удалена", "ok")
    return redirect(url_for("firmware_page"))


# Вызывается на уровне модуля (а не только внутри if __name__), чтобы база и
# миграции инициализировались и при запуске через gunicorn, который импортирует
# этот файл как модуль ("app:app"), а не выполняет его как __main__.
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PROVISIONING_PORT", 8090))
    # Только для локальной отладки (python3 app.py). В продакшне сервис
    # запускается через gunicorn (см. systemd-юнит) — встроенный dev-сервер
    # Werkzeug использует очередь приёма соединений (request_queue_size) по
    # умолчанию всего 5 и не годится для всплесков параллельных запросов
    # (например, когда несколько телефонов одновременно докачивают файлы
    # прошивки/рингтонов) даже с threaded=True.
    app.run(host="0.0.0.0", port=port, threaded=True)
