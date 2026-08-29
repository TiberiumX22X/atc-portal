import os
from urllib.parse import quote

from flask import Flask, request, redirect, url_for, render_template, session, flash, make_response

import auth
import config
from db import init_app_db

app = Flask(__name__)
app.config["SESSION_COOKIE_NAME"] = config.SESSION_COOKIE_NAME
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Единая cookie на весь домен/путь портала — это и есть SSO-сессия,
# которую nginx проверяет через auth_request для КАЖДОЙ панели.
app.config["SESSION_COOKIE_PATH"] = "/"

if os.path.exists(config.SECRET_KEY_PATH):
    with open(config.SECRET_KEY_PATH, "r") as f:
        app.secret_key = f.read().strip()
else:
    key = os.urandom(32).hex()
    with open(config.SECRET_KEY_PATH, "w") as f:
        f.write(key)
    os.chmod(config.SECRET_KEY_PATH, 0o600)
    app.secret_key = key

init_app_db()


@app.context_processor
def inject_csrf():
    return {"csrf_token": auth.get_csrf_token()}


# ---------------------------------------------------------------------------
# Первичная настройка / вход / выход
# ---------------------------------------------------------------------------

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if auth.any_user_exists():
        return redirect(url_for("login"))
    if request.method == "POST":
        auth.check_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or len(password) < 6:
            flash("Логин обязателен, пароль — минимум 6 символов", "error")
            return render_template("setup.html")
        auth.create_user(username, password, role="admin", full_name="Администратор")
        flash("Администратор создан, теперь войдите", "success")
        return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.any_user_exists():
        return redirect(url_for("setup"))
    if request.method == "POST":
        auth.check_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth.verify_user(username, password)
        auth.log_login_attempt(username, request.remote_addr, success=bool(user))
        if not user:
            flash("Неверный логин или пароль", "error")
            return render_template("login.html")
        session.clear()
        session["username"] = user["username"]
        session["role"] = user["role"]
        session["full_name"] = user["full_name"] or user["username"]
        session["allowed_panels"] = user["allowed_panels"] if "allowed_panels" in user.keys() else "*"
        auth.touch_last_login(username)
        return redirect(request.args.get("next") or url_for("portal_home"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Внутренний эндпоинт для nginx auth_request.
# Доступен только из локальной сети сервера (nginx проксирует сюда со
# служебной локацией "internal;") — сам по себе наружу не торчит.
# Возвращает 200 + заголовки X-Remote-User/X-Remote-Role при валидной
# сессии, иначе 401 (nginx после этого сам уводит пользователя на /login).
# ---------------------------------------------------------------------------

@app.route("/_verify")
def verify():
    if not session.get("username"):
        return ("", 401)

    # nginx передаёт, к какой именно панели идёт запрос (см. блок
    # location каждой панели в portal.conf — там `set $panel_key ...`
    # перед auth_request). Администратор видит всё всегда; у оператора
    # может быть ограниченный список — если панель не входит в список
    # разрешённых, отдаём 403 (а не просто прячем карточку в интерфейсе —
    # иначе прямой переход по URL в обход портала снял бы это ограничение).
    panel_key = request.headers.get("X-Panel-Key", "")
    if panel_key and session.get("role") != "admin":
        allowed = session.get("allowed_panels", "*")
        if allowed != "*" and panel_key not in allowed.split(","):
            return ("", 403)

    resp = make_response("", 200)
    resp.headers["X-Remote-User"] = session["username"]
    resp.headers["X-Remote-Role"] = session.get("role", "operator")
    # Отображаемое имя — процентно-закодировано (urllib.quote), т.к. полное
    # имя может быть кириллицей, а HTTP-заголовки допускают только ASCII.
    # Раньше здесь была сырая кириллица напрямую — gunicorn корректно
    # отклонял такой ответ с 400, что рушило всю проверку auth_request в
    # nginx (502 -> 500 у клиента на любой панели). Панели декодируют этот
    # заголовок сами (urllib.parse.unquote) перед показом.
    display_name = session.get("full_name") or session["username"]
    resp.headers["X-Remote-Name"] = quote(display_name)
    return resp


# ---------------------------------------------------------------------------
# Главная страница портала
# ---------------------------------------------------------------------------

@app.route("/")
@auth.login_required
def portal_home():
    return render_template("portal.html", panels=config.PANELS)


# ---------------------------------------------------------------------------
# Управление пользователями (только admin)
# ---------------------------------------------------------------------------

@app.route("/users")
@auth.admin_required
def users_list():
    return render_template("users.html", users=auth.list_users(), panels=[p for p in config.PANELS if not p.get("admin_only")])


@app.route("/users/add", methods=["POST"])
@auth.admin_required
def users_add():
    auth.check_csrf()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "operator")
    full_name = request.form.get("full_name", "").strip()
    selected_panels = request.form.getlist("panels")
    non_admin_panel_keys = [p["key"] for p in config.PANELS if not p.get("admin_only")]
    if set(selected_panels) == set(non_admin_panel_keys):
        allowed_panels = "*"  # отметили всё — храним как "*", чтобы новые панели в будущем тоже стали доступны автоматически
    else:
        allowed_panels = ",".join(selected_panels)
    if not username or len(password) < 6:
        flash("Логин обязателен, пароль — минимум 6 символов", "error")
    elif auth.username_exists(username):
        flash("Такой логин уже существует", "error")
    else:
        auth.create_user(username, password, role=role, full_name=full_name, allowed_panels=allowed_panels)
        flash(f"Пользователь {username} создан", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<username>/panels", methods=["POST"])
@auth.admin_required
def users_set_panels(username):
    auth.check_csrf()
    selected_panels = request.form.getlist("panels")
    auth.set_allowed_panels(username, selected_panels)
    flash(f"Доступные панели для {username} обновлены", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<username>/role", methods=["POST"])
@auth.admin_required
def users_set_role(username):
    auth.check_csrf()
    new_role = request.form.get("role", "operator")
    if new_role == "operator" and username == session["username"] and auth.count_admins() <= 1:
        flash("Нельзя понизить последнего администратора", "error")
        return redirect(url_for("users_list"))
    auth.set_role(username, new_role)
    flash(f"Роль {username} изменена на {new_role}", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<username>/toggle_active", methods=["POST"])
@auth.admin_required
def users_toggle_active(username):
    auth.check_csrf()
    if username == session["username"]:
        flash("Нельзя деактивировать самого себя", "error")
        return redirect(url_for("users_list"))
    rows = {u["username"]: u for u in auth.list_users()}
    row = rows.get(username)
    if row:
        auth.set_active(username, not row["is_active"])
    return redirect(url_for("users_list"))


@app.route("/users/<username>/reset_password", methods=["POST"])
@auth.admin_required
def users_reset_password(username):
    auth.check_csrf()
    new_password = request.form.get("password", "")
    if len(new_password) < 6:
        flash("Пароль — минимум 6 символов", "error")
    else:
        auth.set_password(username, new_password)
        flash(f"Пароль {username} обновлён", "success")
    return redirect(url_for("users_list"))


@app.route("/users/<username>/delete", methods=["POST"])
@auth.admin_required
def users_delete(username):
    auth.check_csrf()
    if username == session["username"]:
        flash("Нельзя удалить самого себя", "error")
    elif rows_is_last_admin(username):
        flash("Нельзя удалить последнего администратора", "error")
    else:
        auth.delete_user(username)
        flash(f"Пользователь {username} удалён", "success")
    return redirect(url_for("users_list"))


def rows_is_last_admin(username: str) -> bool:
    rows = {u["username"]: u for u in auth.list_users()}
    row = rows.get(username)
    return bool(row) and row["role"] == "admin" and auth.count_admins() <= 1


# ---------------------------------------------------------------------------
# Смена собственного пароля
# ---------------------------------------------------------------------------

@app.route("/change_password", methods=["GET", "POST"])
@auth.login_required
def change_password():
    if request.method == "POST":
        auth.check_csrf()
        current = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        user = auth.verify_user(session["username"], current)
        if not user:
            flash("Текущий пароль неверен", "error")
        elif len(new_password) < 6:
            flash("Новый пароль — минимум 6 символов", "error")
        else:
            auth.set_password(session["username"], new_password)
            flash("Пароль изменён", "success")
            return redirect(url_for("portal_home"))
    return render_template("change_password.html")


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=False)
