import secrets
from functools import wraps

from flask import session, redirect, url_for, request, abort
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_app_db

ROLES = ("admin", "operator")


def create_user(username: str, password: str, role: str = "operator", full_name: str = "", allowed_panels: str = "*"):
    if role not in ROLES:
        raise ValueError("Неизвестная роль")
    conn = get_app_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, role, full_name, allowed_panels) VALUES (?, ?, ?, ?, ?)",
        (username, generate_password_hash(password), role, full_name, allowed_panels),
    )
    conn.commit()
    conn.close()


def verify_user(username: str, password: str):
    """Возвращает Row пользователя при успехе (и только если активен), иначе None."""
    conn = get_app_db()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)
    ).fetchone()
    conn.close()
    if not row or not check_password_hash(row["password_hash"], password):
        return None
    return row


def touch_last_login(username: str):
    conn = get_app_db()
    conn.execute(
        "UPDATE users SET last_login = datetime('now', 'localtime') WHERE username = ?",
        (username,),
    )
    conn.commit()
    conn.close()


def log_login_attempt(username: str, ip: str, success: bool):
    conn = get_app_db()
    conn.execute(
        "INSERT INTO login_log (username, ip, success) VALUES (?, ?, ?)",
        (username, ip, 1 if success else 0),
    )
    conn.commit()
    conn.close()


def list_users():
    conn = get_app_db()
    rows = conn.execute(
        "SELECT id, username, full_name, role, is_active, created_at, last_login, allowed_panels "
        "FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def username_exists(username: str) -> bool:
    conn = get_app_db()
    row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row is not None


def count_admins() -> int:
    conn = get_app_db()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin' AND is_active = 1"
    ).fetchone()
    conn.close()
    return row["cnt"]


def set_password(username: str, new_password: str):
    conn = get_app_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE username = ?",
        (generate_password_hash(new_password), username),
    )
    conn.commit()
    conn.close()


def set_role(username: str, role: str):
    if role not in ROLES:
        raise ValueError("Неизвестная роль")
    conn = get_app_db()
    conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
    conn.commit()
    conn.close()


def set_allowed_panels(username: str, panel_keys: list):
    """panel_keys — список ключей панелей (см. config.PANELS), либо
    пустой список ('нет доступа ни к одной'). Роль admin это ограничение
    игнорирует и всегда видит всё — этот список значим только для
    операторов."""
    value = ",".join(panel_keys) if panel_keys else ""
    conn = get_app_db()
    conn.execute("UPDATE users SET allowed_panels = ? WHERE username = ?", (value, username))
    conn.commit()
    conn.close()


def get_allowed_panels(username: str) -> str:
    conn = get_app_db()
    row = conn.execute("SELECT allowed_panels FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row["allowed_panels"] if row else "*"


def set_active(username: str, is_active: bool):
    conn = get_app_db()
    conn.execute(
        "UPDATE users SET is_active = ? WHERE username = ?", (1 if is_active else 0, username)
    )
    conn.commit()
    conn.close()


def delete_user(username: str):
    conn = get_app_db()
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def any_user_exists() -> bool:
    conn = get_app_db()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
    conn.close()
    return row["cnt"] > 0


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            abort(403)
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
