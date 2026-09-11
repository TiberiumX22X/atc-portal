from flask import Flask, render_template, request, g
from urllib.parse import unquote
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import auth
import poller
from extensions_view import build_extensions_list, paginate

app = Flask(__name__)

# Панель работает за nginx под префиксом /monitor/ (см. nginx/portal.conf).
# nginx передаёт заголовок X-Forwarded-Prefix, а ProxyFix заставляет
# url_for() автоматически подставлять этот префикс во все внутренние
# ссылки (иначе они генерировались бы как "/", "/calls" и т.д. — то есть
# указывали бы на корень портала, а не на саму панель).
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)

poller.start_background_poller()


# ---------------------------------------------------------------------------
# SSO: логин/пароль/выход/смена пароля теперь на портале (sso-auth, порт
# 8080) — эта панель их больше не реализует. nginx подставляет заголовки
# X-Remote-User/X-Remote-Role ПОСЛЕ проверки через auth_request; кладём их
# в g, чтобы шаблоны могли показать имя пользователя.
# ---------------------------------------------------------------------------

@app.before_request
def load_remote_user():
    g.remote_user = request.headers.get("X-Remote-User")
    g.remote_role = request.headers.get("X-Remote-Role", "operator")
    # Отображаемое имя приходит от портала процентно-закодированным (там
    # может быть кириллица, а сырые HTTP-заголовки такого не допускают) —
    # декодируем перед показом. Если портал ещё не обновлён (заголовка
    # нет) — используем логин, как было раньше.
    remote_name = request.headers.get("X-Remote-Name")
    g.remote_name = unquote(remote_name) if remote_name else g.remote_user


# ---------------------------------------------------------------------------
# Номера
# ---------------------------------------------------------------------------

def _numbers_context():
    query = request.args.get("q", "").strip()
    sort = request.args.get("sort", "number")
    direction = request.args.get("dir", "asc")
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1

    rows, last_update, last_error = build_extensions_list(query, sort, direction)
    page_rows, total, page, total_pages = paginate(rows, page)

    return {
        "rows": page_rows,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "query": query,
        "sort": sort,
        "direction": direction,
        "last_update": last_update,
        "last_error": last_error,
        "poll_interval": config.POLL_INTERVAL_SECONDS,
    }


@app.route("/")
@auth.login_required
def numbers_page():
    return render_template("numbers.html", **_numbers_context())


@app.route("/partial/numbers")
@auth.login_required
def numbers_partial():
    return render_template("_numbers_table.html", **_numbers_context())


# ---------------------------------------------------------------------------
# Одновременные вызовы
# ---------------------------------------------------------------------------

def _calls_context():
    snapshot = poller.get_snapshot()
    return {
        "concurrent_calls": snapshot["concurrent_calls"],
        "last_update": snapshot["last_update"],
        "last_error": snapshot["last_error"],
        "poll_interval": config.POLL_INTERVAL_SECONDS,
    }


@app.route("/calls")
@auth.login_required
def calls_page():
    return render_template("calls.html", **_calls_context())


@app.route("/partial/calls")
@auth.login_required
def calls_partial():
    return render_template("_calls_content.html", **_calls_context())


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=False)
