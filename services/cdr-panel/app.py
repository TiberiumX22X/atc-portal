import math
from urllib.parse import unquote
from flask import Flask, render_template, request, redirect, url_for, flash, g
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import auth
import cdr_query
import recordings

app = Flask(__name__)

# Панель работает за nginx под префиксом /cdr/ (см. nginx/portal.conf).
# nginx передаёт заголовок X-Forwarded-Prefix, а ProxyFix заставляет
# url_for() автоматически подставлять этот префикс во все внутренние
# ссылки (иначе они генерировались бы как "/", "/recording/..." и т.д. —
# то есть указывали бы на корень портала, а не на саму панель).
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)

# flash() всё ещё используется (удаление записи), поэтому секрет сессии
# нужен — но он больше не хранит логин/пароль, только подписывает
# flash-сообщения между запросами.
app.secret_key = config.get_or_create_flash_secret()
# Уникальное имя cookie — во избежание коллизии с alert-panel/phone-
# provisioning на одном домене портала (все трое держат Flask-сессию
# ради CSRF; одинаковое имя "session" по умолчанию у Flask привело бы
# к тому, что посещение одной панели тихо ломает CSRF в другой).
app.config["SESSION_COOKIE_NAME"] = "cdr_session"


# ---------------------------------------------------------------------------
# SSO: логин/пароль/выход/смена пароля/управление пользователями теперь на
# портале (sso-auth, порт 8080) — эта панель их больше не реализует. nginx
# подставляет заголовки X-Remote-User/X-Remote-Role ПОСЛЕ проверки через
# auth_request; кладём их в g, чтобы шаблоны могли показать имя пользователя
# и скрыть кнопку удаления записи от не-администраторов.
# ---------------------------------------------------------------------------

@app.before_request
def load_remote_user():
    g.remote_user = request.headers.get("X-Remote-User")
    g.remote_role = request.headers.get("X-Remote-Role", "operator")
    remote_name = request.headers.get("X-Remote-Name")
    g.remote_name = unquote(remote_name) if remote_name else g.remote_user


@app.context_processor
def inject_csrf():
    return {"csrf_token": auth.get_csrf_token()}


# ---------------------------------------------------------------------------
# CDR
# ---------------------------------------------------------------------------

@app.route("/")
@auth.login_required
def cdr_list():
    min_duration_raw = request.args.get("min_duration", "").strip()
    filters = {
        "date_from": request.args.get("date_from", "").strip(),
        "date_to": request.args.get("date_to", "").strip(),
        "src": request.args.get("src", "").strip(),
        "dst": request.args.get("dst", "").strip(),
        "min_duration": min_duration_raw if min_duration_raw.isdigit() else None,
        "disposition": request.args.get("disposition", "").strip(),
        "line": request.args.get("line", "").strip(),
        "call_type": request.args.get("call_type", "").strip(),
    }
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1

    try:
        rows, total = cdr_query.search_cdr(filters, page=page)
        db_error = None
    except Exception as exc:
        rows, total = [], 0
        db_error = str(exc)

    try:
        lines = cdr_query.list_available_lines()
    except Exception:
        lines = []

    total_pages = max(math.ceil(total / config.PAGE_SIZE), 1)

    return render_template(
        "cdr_list.html",
        rows=rows,
        total=total,
        page=page,
        total_pages=total_pages,
        filters=filters,
        lines=lines,
        disposition_labels=cdr_query.DISPOSITION_LABELS,
        call_type_labels=cdr_query.CALL_TYPE_LABELS,
        db_error=db_error,
        is_admin=(g.remote_role == "admin"),
    )


@app.route("/recording/<path:recordingfile>/play")
@auth.login_required
def play_recording(recordingfile):
    return recordings.serve_recording(recordingfile, as_attachment=False)


@app.route("/recording/<path:recordingfile>/download")
@auth.login_required
def download_recording(recordingfile):
    return recordings.serve_recording(recordingfile, as_attachment=True)


@app.route("/recording/<path:recordingfile>/delete", methods=["POST"])
@auth.admin_required
def delete_recording(recordingfile):
    auth.check_csrf()
    try:
        deleted = recordings.delete_recording(recordingfile)
    except RuntimeError as exc:
        flash(str(exc), "error")
        return redirect(request.referrer or url_for("cdr_list"))
    if deleted:
        flash("Запись удалена", "success")
    else:
        flash("Файл записи не найден", "error")
    # Возвращаемся на ту же страницу списка (со всеми фильтрами/страницей)
    return redirect(request.referrer or url_for("cdr_list"))


if __name__ == "__main__":
    # Только для локальной отладки — в бою всегда через gunicorn (см. install.sh)
    app.run(host=config.HOST, port=config.PORT, debug=False)
