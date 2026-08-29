from functools import wraps

from flask import request, abort, session
import secrets

# ---------------------------------------------------------------------------
# SSO: вход, логины, пароли и роли теперь живут в центральном sso-auth
# (порт 8080). Эта панель больше не хранит и не проверяет пароли сама — она
# доверяет заголовкам X-Remote-User/X-Remote-Role, которые nginx подставляет
# ПОСЛЕ успешной проверки через auth_request (см. nginx/portal.conf, блок
# /cdr/ в sso-auth). Доверие обосновано тем, что панель слушает только
# 127.0.0.1 (см. config.py) — обратиться к ней в обход nginx снаружи
# невозможно.
# ---------------------------------------------------------------------------


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not request.headers.get("X-Remote-User"):
            # В норме сюда не попасть — nginx уже отсеял неавторизованных
            # через auth_request. Если заголовка нет, значит запрос пришёл
            # мимо nginx (или тот неправильно настроен) — блокируем.
            abort(401)
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not request.headers.get("X-Remote-User"):
            abort(401)
        if request.headers.get("X-Remote-Role") != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


# --- CSRF: простой session-token для формы удаления записи. Сессия у панели
# осталась только ради этого (и ради flash-сообщений) — логин/пароль в ней
# больше не хранится, это не аутентификация. ---

def get_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def check_csrf():
    token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not token or token != session.get("csrf_token"):
        abort(400, description="Неверный CSRF-токен")
