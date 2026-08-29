import os
import re
import secrets
import socket
import sqlite3
import subprocess
import threading
import time
import traceback
from datetime import datetime
from functools import wraps
from urllib.parse import unquote

from flask import (
    Flask, request, session, redirect, url_for, render_template,
    flash, g, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "alert_panel.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
ALLOWED_RECORDING_EXT = {"wav", "mp3", "gsm", "ulaw", "alaw", "sln"}

# Имя БД FreePBX и таблица с номерами (стандартная для всех версий FreePBX)
FREEPBX_DB = "asterisk"

# Dialplan-контекст, в который заходит каждый набираемый номер (см. install.sh)
PAGE_CONTEXT = "alert-panel-room"
PAGE_EXTEN = "join"

app = Flask(__name__)

# Панель работает за nginx под префиксом /alert/ (см. nginx/portal.conf).
# nginx передаёт заголовок X-Forwarded-Prefix, а ProxyFix заставляет
# url_for() автоматически подставлять этот префикс во все внутренние
# ссылки (иначе они указывали бы на корень портала, а не на саму панель).
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)

# CSRF-токен всё ещё подписывается через Flask-сессию (не через SSO) —
# секрет нужен только для этого, не для логина/пароля.
_SECRET_FILE = os.path.join(BASE_DIR, ".flask_secret")
if os.environ.get("ALERT_PANEL_SECRET"):
    app.secret_key = os.environ["ALERT_PANEL_SECRET"]
else:
    if not os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE, "w") as f:
            f.write(os.urandom(32).hex())
        os.chmod(_SECRET_FILE, 0o600)
    with open(_SECRET_FILE, "r") as f:
        app.secret_key = f.read().strip()

# Уникальное имя cookie — во избежание коллизии с cdr-panel/phone-
# provisioning на одном домене портала (все трое держат Flask-сессию
# ради CSRF; одинаковое имя "session" по умолчанию у Flask привело бы
# к тому, что посещение одной панели тихо ломает CSRF в другой).
app.config["SESSION_COOKIE_NAME"] = "alert_session"

os.makedirs(RECORDINGS_DIR, exist_ok=True)


# ---------- CSRF-защита ----------
# До этой правки её не было вообще — POST-формы (создание/удаление
# пользователей, смена пароля, запуск оповещения) были уязвимы к
# межсайтовой подделке запроса. Простой session-based токен без
# дополнительных зависимостей (Flask-WTF и т.п.).

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = get_csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST":
        form_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not form_token or not secrets.compare_digest(form_token, session_token):
            abort(403)


# ---------- БД ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Таблица users в схеме остаётся (используется где-то ещё не будет —
    просто не трогаем структуру БД), но больше не используется для входа:
    логин и роли теперь приходят от sso-auth через заголовки nginx (см.
    login_required/admin_required ниже). Локального admin с автогенерацией
    пароля при первом запуске больше не создаём — заходить сюда с ним
    было бы уже некуда (страницы /login у панели нет)."""
    db = sqlite3.connect(DB_PATH)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        db.executescript(f.read())
    db.commit()
    db.close()


def local_now():
    """
    Локальное время станции для записи в БД. SQLite-функция datetime('now')
    по умолчанию отдаёт UTC, а не время сервера — из-за этого время в
    журнале панели расходилось с временем в /var/log/asterisk/full
    (там местное время, +05:00) на 5 часов. Пишем время явно из Python,
    оно берёт системный часовой пояс сервера напрямую.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _read_freepbx_db_creds():
    """Читает реальные реквизиты подключения к БД FreePBX из
    /etc/freepbx.conf — тот же способ, что уже используется в остальных
    панелях проекта. Раньше здесь было захардкожено 'mysql -u root' без
    пароля — работает только если процесс выполняется от имени ОС-пользователя
    root (unix_socket-аутентификация MySQL сверяет имя ОС-пользователя с
    именем MySQL-пользователя), а эта панель работает от www-data — отсюда
    и "Access denied for user 'root'@'localhost'" (ошибка 1698, классический
    признак именно несовпадения unix_socket, а не неверного пароля)."""
    try:
        with open("/etc/freepbx.conf", "r", encoding="utf-8", errors="ignore") as f:
            conf = f.read()
    except OSError:
        return "freepbxuser", ""

    def _extract(key, default=""):
        m = re.search(r"\$amp_conf\[['\"]" + key + r"['\"]\]\s*=\s*['\"]([^'\"]*)['\"]", conf)
        return m.group(1) if m else default

    return _extract("AMPDBUSER", "freepbxuser"), _extract("AMPDBPASS", "")


def fetch_freepbx_extensions():
    """
    Тянет список номеров напрямую из БД FreePBX (таблица `users`).
    Берём только номера с заполненным именем, отличным от самого номера —
    так отсеиваются служебные записи (очереди, парковки, заготовки без
    привязки к реальному человеку/устройству).
    Возвращает список (extension, name).
    """
    query = (
        "SELECT extension, name FROM users "
        "WHERE name IS NOT NULL AND TRIM(name) != '' AND TRIM(name) != extension "
        "ORDER BY CAST(extension AS UNSIGNED)"
    )
    db_user, db_pass = _read_freepbx_db_creds()
    cmd = ["mysql", "-u", db_user]
    if db_pass:
        cmd.append(f"-p{db_pass}")
    cmd += [FREEPBX_DB, "-N", "-B", "-e", query]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "mysql вернул ошибку")

    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        ext = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else ""
        if ext.isdigit():
            rows.append((ext, name))
    return rows


init_db()


# ---------- Авторизация ----------
# SSO: вход/пароли/роли теперь в центральном sso-auth (порт 8080). Панель
# доверяет заголовкам X-Remote-User/X-Remote-Role, которые nginx подставляет
# ПОСЛЕ проверки через auth_request — доверие обосновано тем, что панель
# слушает только 127.0.0.1 (см. install.sh), обратиться в обход nginx
# снаружи невозможно.

@app.before_request
def load_remote_user():
    g.remote_user = request.headers.get("X-Remote-User")
    g.remote_role = request.headers.get("X-Remote-Role", "operator")
    remote_name = request.headers.get("X-Remote-Name")
    g.remote_name = unquote(remote_name) if remote_name else g.remote_user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not request.headers.get("X-Remote-User"):
            abort(401)
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not request.headers.get("X-Remote-User"):
            abort(401)
        if request.headers.get("X-Remote-Role") != "admin":
            flash("Доступно только администратору", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


# ---------- AMI: запуск оповещения ----------

class AMIError(Exception):
    pass


def ami_originate(channel_tech, extensions, ami_host, ami_port, ami_username, ami_secret,
                   alert_name="", recording_path=None):
    """
    Отдельная короткая AMI Originate на КАЖДЫЙ номер — не подвержена
    никакому лимиту длины ни при каком N (в отличие от одного общего
    Page() со всем списком через переменную — тот на 400+ номерах молча
    обрезался внутренним лимитом Asterisk на размер dialplan-переменной).
    Все номера заходят в одну и ту же именованную ConfBridge-конференцию.

    Требует dialplan-контекст alert-panel-room (создаётся install.sh).
    """
    if not extensions:
        raise AMIError("Пустой список номеров")

    recording_noext = ""
    if recording_path:
        recording_noext = os.path.splitext(recording_path)[0]

    room_id = f"alert{int(time.time())}"
    alert_name_safe = (alert_name or "Оповещение").replace("\r", " ").replace("\n", " ")

    host = ami_host
    port = int(ami_port)
    username = ami_username
    secret = ami_secret

    sock = socket.create_connection((host, port), timeout=30)
    sock_file = sock.makefile("rwb")

    def send_raw(lines):
        payload = "\r\n".join(lines) + "\r\n\r\n"
        sock_file.write(payload.encode("utf-8"))
        sock_file.flush()

    def read_banner():
        sock_file.readline()

    def _read_one_block():
        lines = []
        while True:
            line = sock_file.readline().decode("utf-8", errors="replace")
            if line in ("\r\n", "\n", ""):
                break
            lines.append(line.strip())
        return lines

    def read_block():
        # Asterisk шлёт неожиданные Event-блоки асинхронно, независимо от
        # того, что мы спрашиваем (например "Event: FullyBooted" сразу
        # после логина) — если такой блок попадётся между отправкой нашей
        # команды и чтением её ответа, мы читаем чужой блок вместо своего,
        # и весь дальнейший обмен рассинхронизируется. Пропускаем всё, что
        # не начинается с "Response:", пока не найдём настоящий ответ.
        for _ in range(20):  # разумный предел, чтобы не зациклиться навечно
            block = _read_one_block()
            if not block:
                return block
            if block[0].startswith("Response:"):
                return block
            # иначе — постороннее асинхронное событие, читаем следующий блок
        return block

    sent = 0
    skipped_offline = []
    try:
        read_banner()
        send_raw([
            "Action: Login",
            f"Username: {username}",
            f"Secret: {secret}",
        ])
        login_resp = read_block()
        if not any("Success" in l for l in login_resp):
            raise AMIError(f"AMI login failed: {login_resp}")

        # Перед рассылкой узнаём, кто реально СЕЙЧАС зарегистрирован —
        # сотрудник может быть в отпуске, телефон выключен и т.п. Слать
        # Originate на заведомо офлайн-номера бессмысленно (никто не
        # ответит) и, судя по наблюдениям, при массе таких "пустых" вызовов
        # среди них иногда теряются и реально живые номера. Фильтруем
        # заранее — звоним только тем, у кого есть хотя бы один контакт.
        #
        # Формат ответа на Action: Command в этой версии AMI (11.0.0) —
        # обычный блок (Response: Success / Message: ... / Output: <строка>
        # ... / пустая строка), БЕЗ маркера "--END COMMAND--" из старых
        # версий протокола — читаем как read_block(), просто снимаем
        # префикс "Output: " при разборе содержимого.
        send_raw(["Action: Command", "Command: pjsip show contacts"])
        contacts_output = read_block()
        if any(l.startswith("Response: Error") for l in contacts_output):
            raise AMIError(
                f"AMI отказал в 'pjsip show contacts' (не хватает прав у пользователя?): {contacts_output}"
            )
        registered = set()
        contact_re = re.compile(r"Contact:\s+(\d+)/\S+")
        for line in contacts_output:
            content = line[len("Output: "):] if line.startswith("Output: ") else line
            m = contact_re.search(content)
            if m:
                registered.add(m.group(1))

        skipped_offline = [e for e in extensions if e not in registered]
        extensions = [e for e in extensions if e in registered]

        if not extensions:
            raise AMIError(
                f"Ни один из {len(skipped_offline)} номеров сейчас не зарегистрирован — некому звонить. "
                f"Сырой ответ pjsip show contacts ({len(contacts_output)} строк): {contacts_output!r}"
            )

        action_to_ext = {}
        failures = []

        def process_response(lines):
            resp = {}
            for l in lines:
                if ": " in l:
                    k, v = l.split(": ", 1)
                    resp[k] = v
            aid = resp.get("ActionID", "")
            ext_for_this = action_to_ext.get(aid, "?")
            if resp.get("Response") == "Error":
                failures.append(f"{ext_for_this}: {resp.get('Message', lines)}")

        for i, ext in enumerate(extensions):
            action_id = f"alertpanel-{int(time.time() * 1000)}-{i}"
            action_to_ext[action_id] = ext
            send_raw([
                "Action: Originate",
                f"Channel: {channel_tech}/{ext}",
                f"Context: {PAGE_CONTEXT}",
                f"Exten: {PAGE_EXTEN}",
                "Priority: 1",
                "Async: true",
                "Timeout: 20000",
                f'CallerID: "{alert_name_safe}" <8500>',
                "Variable: PJSIP_HEADER(add,Call-Info)=answer-after=0",
                "Variable: PJSIP_HEADER(add,Alert-Info)=answer-after=0",
                f"Variable: ALERT_NAME={alert_name_safe}",
                f"Variable: RECORDING_FILE={recording_noext}",
                f"Variable: ROOM_ID={room_id}",
                f"ActionID: {action_id}",
            ])
            sent += 1

        # Без пачек и без пауз — все команды уходят одна за другой без
        # задержки, ответы вычитываются разом в конце. Раньше это грозило
        # переполнением буфера сокета на очень больших списках, но при
        # реалистичном числе номеров (сотни, не тысячи) и расширенном пуле
        # PJSIP-потоков риск невелик, а звоним теперь только реально
        # зарегистрированным — список для рассылки короче, чем весь парк.
        for _ in range(sent):
            process_response(read_block())

        send_raw(["Action: Logoff"])
    finally:
        try:
            sock_file.close()
        finally:
            sock.close()

    return {
        "sent": sent,
        "room_id": room_id,
        "failures": failures,
        "skipped_offline": skipped_offline,
    }


# ---------- Страницы ----------

@app.route("/")
@login_required
def dashboard():
    db = get_db()
    recordings = db.execute(
        "SELECT * FROM recordings ORDER BY sort_order, id"
    ).fetchall()
    ext_count = db.execute(
        "SELECT COUNT(*) c FROM extensions WHERE active = 1"
    ).fetchone()["c"]
    recent_logs = db.execute(
        "SELECT * FROM events_log ORDER BY id DESC LIMIT 15"
    ).fetchall()
    return render_template(
        "dashboard.html",
        recordings=recordings,
        ext_count=ext_count,
        recent_logs=recent_logs,
    )


def _run_alert_in_background(recording_id, alert_name, recording_path, exts, channel_tech, username, ami_settings):
    """
    Выполняется в отдельном потоке — своё соединение с БД (Flask-овский
    get_db()/g привязан к контексту конкретного HTTP-запроса, здесь его
    уже нет). Настройки AMI тоже переданы параметрами по той же причине —
    get_setting() внутри потока падает с "Working outside of application
    context". Пишет результат в events_log по завершении.
    """
    db = sqlite3.connect(DB_PATH)
    try:
        result = ami_originate(
            channel_tech,
            exts,
            ami_settings["host"], ami_settings["port"], ami_settings["username"], ami_settings["secret"],
            alert_name=alert_name,
            recording_path=recording_path,
        )
        note = f"звонили на {result['sent']} из {len(exts)}"
        if result["skipped_offline"]:
            note += f"; не в сети: {len(result['skipped_offline'])}"
        if result["failures"]:
            note += f"; отказов: {len(result['failures'])}"
        db.execute(
            "INSERT INTO events_log (event_type, name, target_count, triggered_by, note, triggered_at) VALUES (?,?,?,?,?,?)",
            ("alert", alert_name, result["sent"], username, note, local_now()),
        )
        db.commit()
    except Exception as e:
        # Раньше ловились только AMIError/OSError — любая другая ошибка
        # (баг в разборе ответов, кодировка и т.п.) падала молча: поток
        # умирал без единой записи в журнале и без следа в браузере.
        tb = traceback.format_exc()
        db.execute(
            "INSERT INTO events_log (event_type, name, target_count, triggered_by, note, triggered_at) VALUES (?,?,?,?,?,?)",
            ("alert", alert_name, len(exts), username, f"ОШИБКА ПОТОКА: {type(e).__name__}: {e}\n{tb[-1500:]}", local_now()),
        )
        db.commit()
    finally:
        db.close()


@app.route("/trigger/alert/<int:recording_id>", methods=["POST"])
@login_required
def trigger_alert(recording_id):
    db = get_db()
    rec = db.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    if not rec:
        flash("Оповещение не найдено", "error")
        return redirect(url_for("dashboard"))

    exts = [r["extension"] for r in db.execute(
        "SELECT extension FROM extensions WHERE active = 1 ORDER BY extension"
    ).fetchall()]

    if not exts:
        flash("Список номеров пуст — синхронизируйте его в разделе «Номера»", "error")
        return redirect(url_for("dashboard"))

    recording_path = None
    if rec["filename"]:
        recording_path = os.path.join(RECORDINGS_DIR, rec["filename"])

    ami_settings = {
        "host": get_setting("ami_host", "127.0.0.1"),
        "port": get_setting("ami_port", "5038"),
        "username": get_setting("ami_username", "alertpanel"),
        "secret": get_setting("ami_secret", ""),
    }

    thread = threading.Thread(
        target=_run_alert_in_background,
        args=(
            recording_id, rec["alert_name"], recording_path, exts,
            get_setting("channel_tech", "PJSIP"), g.remote_user, ami_settings,
        ),
        daemon=True,
    )
    thread.start()

    flash(f"Оповещение «{rec['alert_name']}» запускается на {len(exts)} номеров (в фоне)", "success")
    return redirect(url_for("dashboard"))


@app.route("/logs")
@login_required
def logs():
    db = get_db()
    rows = db.execute("SELECT * FROM events_log ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("logs.html", rows=rows)


@app.route("/logs/clear", methods=["POST"])
@admin_required
def logs_clear():
    db = get_db()
    db.execute("DELETE FROM events_log")
    db.commit()
    flash("Журнал очищен", "success")
    return redirect(url_for("logs"))


# ---------- Администрирование: пользователи ----------
# Управление пользователями и ролями теперь на портале (sso-auth, страница
# «Пользователи») — единый список для всех панелей. Здесь эта страница
# больше не нужна; ссылка в base.html ведёт на портал.


# ---------- Администрирование: записи оповещений ----------

def allowed_recording(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_RECORDING_EXT


@app.route("/admin/recordings", methods=["GET", "POST"])
@admin_required
def admin_recordings():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            alert_name = request.form.get("alert_name", "").strip()
            file = request.files.get("file")
            filename = None
            if not alert_name:
                flash("Укажите название оповещения", "error")
                return redirect(url_for("admin_recordings"))
            if file and file.filename:
                if not allowed_recording(file.filename):
                    flash("Недопустимый формат файла", "error")
                    return redirect(url_for("admin_recordings"))
                filename = secure_filename(f"{int(time.time())}_{file.filename}")
                filepath = os.path.join(RECORDINGS_DIR, filename)
                file.save(filepath)
                os.chmod(filepath, 0o644)
            db.execute(
                "INSERT INTO recordings (alert_name, filename, created_by, created_at) VALUES (?,?,?,?)",
                (alert_name, filename, g.remote_user, local_now()),
            )
            db.commit()
            flash(f"Оповещение «{alert_name}» добавлено", "success")
        elif action == "delete":
            rec_id = request.form.get("recording_id")
            row = db.execute("SELECT filename FROM recordings WHERE id = ?", (rec_id,)).fetchone()
            if row and row["filename"]:
                path = os.path.join(RECORDINGS_DIR, row["filename"])
                if os.path.exists(path):
                    os.remove(path)
            db.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
            db.commit()
            flash("Оповещение удалено", "success")
        return redirect(url_for("admin_recordings"))

    recordings = db.execute("SELECT * FROM recordings ORDER BY sort_order, id").fetchall()
    return render_template("admin_recordings.html", recordings=recordings)


# ---------- Администрирование: номера-цели ----------

@app.route("/admin/extensions", methods=["GET", "POST"])
@admin_required
def admin_extensions():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")

        if action == "sync":
            try:
                rows = fetch_freepbx_extensions()
            except (subprocess.SubprocessError, RuntimeError, OSError) as e:
                flash(f"Не удалось получить номера из FreePBX: {e}", "error")
                return redirect(url_for("admin_extensions"))

            added = 0
            updated = 0
            seen = []
            for ext, name in rows:
                seen.append(ext)
                existing = db.execute(
                    "SELECT id FROM extensions WHERE extension = ?", (ext,)
                ).fetchone()
                if existing:
                    db.execute(
                        "UPDATE extensions SET label = ?, active = 1 WHERE extension = ?",
                        (name, ext),
                    )
                    updated += 1
                else:
                    db.execute(
                        "INSERT INTO extensions (extension, label, active, imported_at) VALUES (?,?,1,?)",
                        (ext, name, local_now()),
                    )
                    added += 1

            if seen:
                placeholders = ",".join("?" for _ in seen)
                db.execute(
                    f"UPDATE extensions SET active = 0 WHERE extension NOT IN ({placeholders})",
                    tuple(seen),
                )
            db.commit()
            flash(
                f"Синхронизировано из FreePBX: добавлено {added}, обновлено {updated} "
                f"(всего в выборке: {len(rows)})",
                "success",
            )
        elif action == "import":
            raw = request.form.get("extensions_raw", "")
            added = 0
            skipped_lines = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                token = re.split(r"[;,\s]+", line)[0]
                if token.isdigit():
                    try:
                        db.execute(
                            "INSERT INTO extensions (extension, imported_at) VALUES (?, ?)", (token, local_now())
                        )
                        added += 1
                    except sqlite3.IntegrityError:
                        pass
                else:
                    skipped_lines.append(line[:40])
            db.commit()
            msg = f"Добавлено новых номеров: {added}"
            if skipped_lines:
                shown = ", ".join(skipped_lines[:5])
                more = f" и ещё {len(skipped_lines)-5}" if len(skipped_lines) > 5 else ""
                msg += f". Не распознано как номер: {shown}{more}"
            flash(msg, "success" if added else "error")
        elif action == "toggle":
            ext_id = request.form.get("ext_id")
            db.execute(
                "UPDATE extensions SET active = 1 - active WHERE id = ?", (ext_id,)
            )
            db.commit()
        elif action == "delete_all":
            db.execute("DELETE FROM extensions")
            db.commit()
            flash("Список номеров очищен", "success")
        return redirect(url_for("admin_extensions"))

    extensions = db.execute("SELECT * FROM extensions ORDER BY extension").fetchall()
    return render_template("admin_extensions.html", extensions=extensions)


# ---------- Настройки AMI ----------

@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    db = get_db()
    if request.method == "POST":
        for key in ("ami_host", "ami_port", "ami_username", "ami_secret", "channel_tech"):
            value = request.form.get(key)
            if value is not None:
                db.execute(
                    "INSERT INTO settings (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        db.commit()
        flash("Настройки сохранены", "success")
        return redirect(url_for("admin_settings"))

    settings = {row["key"]: row["value"] for row in db.execute("SELECT * FROM settings")}
    return render_template("admin_settings.html", settings=settings)


if __name__ == "__main__":
    # debug=True ЗАПРЕЩЕНО: встроенный отладчик Werkzeug при включённом
    # debug позволяет выполнение произвольного Python-кода через браузер
    # у любого, кто достучится до порта. В проде всегда через gunicorn
    # (см. install.sh), этот блок — только для локальной разработки.
    app.run(host="127.0.0.1", port=8091, debug=False)
