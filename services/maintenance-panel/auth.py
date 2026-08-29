import secrets
from functools import wraps

from flask import request, abort, session, g

# ---------------------------------------------------------------------------
# SSO: вход происходит на портале (sso-auth, порт 8080). Панель доверяет
# заголовкам X-Remote-User/X-Remote-Role/X-Remote-Name, которые nginx
# подставляет ПОСЛЕ проверки через auth_request — доверие обосновано тем,
# что панель слушает только 127.0.0.1 (см. config.py).
#
# В отличие от остальных пяти панелей проекта, здесь НЕТ понятия "оператор
# видит хоть что-то" — вся панель целиком доступна только роли admin,
# без исключений: она даёт прямой доступ к перезапуску служб, сырым
# AMI-командам и системным логам.
# ---------------------------------------------------------------------------

from urllib.parse import unquote


def load_remote_user():
    g.remote_user = request.headers.get("X-Remote-User")
    g.remote_role = request.headers.get("X-Remote-Role", "operator")
    remote_name = request.headers.get("X-Remote-Name")
    g.remote_name = unquote(remote_name) if remote_name else g.remote_user


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not request.headers.get("X-Remote-User"):
            abort(401)
        if request.headers.get("X-Remote-Role") != "admin":
            abort(403, description="Панель обслуживания доступна только администратору")
        return view(*args, **kwargs)

    return wrapped


def get_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def check_csrf():
    token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not token or token != session.get("csrf_token"):
        abort(400, description="Неверный CSRF-токен")
