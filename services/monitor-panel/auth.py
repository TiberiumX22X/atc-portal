from functools import wraps

from flask import request, abort

# ---------------------------------------------------------------------------
# SSO: вход, логины и пароли теперь живут в центральном sso-auth (порт 8080).
# Эта панель больше не хранит и не проверяет пароли сама — она доверяет
# заголовкам X-Remote-User/X-Remote-Role, которые nginx подставляет ПОСЛЕ
# успешной проверки через auth_request (см. nginx/portal.conf, блок /monitor/
# в sso-auth). Доверие обосновано тем, что панель слушает только 127.0.0.1
# (см. config.py) — обратиться к ней в обход nginx снаружи невозможно.
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
