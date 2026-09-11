#!/usr/bin/env python3
"""
Перенос пользователей из старых БД пяти панелей в центральную БД портала.

По умолчанию ищет БД по стандартным путям установки (/opt/...). Если у вас
другие пути — передайте их через --panel-db.

Правила переноса:
  - пароли (хеши werkzeug) переносятся как есть — пересоздавать не нужно,
    формат совместим (generate_password_hash/check_password_hash);
  - при совпадении username в нескольких панелях — первое найденное
    побеждает, остальные пропускаются с предупреждением (нужно решить
    вручную, если это разные люди с одинаковым логином);
  - панели без ролей (monitor-panel: admin, phone-provisioning: panel_users)
    — все их пользователи переносятся с ролью admin (это соответствует их
    нынешней модели "один администратор панели");
  - is_active по умолчанию 1, если в исходной таблице нет такого поля.

Запуск (из каталога sso-auth, после install.sh --install, ДО первого /setup):
    ./venv/bin/python migrate_users.py --dry-run     # посмотреть, что перенесётся
    ./venv/bin/python migrate_users.py                # выполнить перенос
"""
import argparse
import os
import sqlite3
import sys

DEFAULT_SOURCES = [
    # (человекочитаемое имя, путь к БД, имя таблицы, есть ли колонка role, есть ли full_name)
    ("confbridge-panel",   "/opt/asterisk-panel/panel.db",        "users",       True,  True),
    ("cdr-panel",          "/opt/cdr-panel/cdrpanel.db",          "users",       True,  False),
    ("alert-panel",        "/opt/alert-panel/alert_panel.db",     "users",       True,  False),
    ("monitor-panel",      "/opt/monitor-panel/monitorpanel.db",  "admin",       False, False),
    ("phone-provisioning", "/opt/phone-provisioning/devices.db",  "panel_users", False, False),
]


def read_source(path, table, has_role, has_full_name):
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return None
    conn.close()
    result = []
    for r in rows:
        result.append({
            "username": r["username"],
            "password_hash": r["password_hash"],
            "role": (r["role"] if has_role and "role" in r.keys() else "admin"),
            "full_name": (r["full_name"] if has_full_name and "full_name" in r.keys() else ""),
        })
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="только показать, ничего не писать")
    parser.add_argument("--panel-db", action="append", default=[],
                         help="переопределить путь: NAME=/путь/к.db (можно несколько раз)")
    args = parser.parse_args()

    overrides = {}
    for item in args.panel_db:
        name, _, path = item.partition("=")
        overrides[name] = path

    sso_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sso.db")
    if not os.path.exists(sso_db_path) and not args.dry_run:
        print("sso.db не найден — сначала запустите приложение хотя бы раз "
              "(или install.sh --install), чтобы схема была создана.")
        sys.exit(1)

    seen_usernames = set()
    to_insert = []

    for name, default_path, table, has_role, has_full_name in DEFAULT_SOURCES:
        path = overrides.get(name, default_path)
        users = read_source(path, table, has_role, has_full_name)
        if users is None:
            print(f"[{name}] БД не найдена или таблица {table} отсутствует ({path}) — пропуск")
            continue
        print(f"[{name}] найдено пользователей: {len(users)} ({path})")
        for u in users:
            if u["username"] in seen_usernames:
                print(f"    ⚠ '{u['username']}' уже перенесён из другой панели — пропускаю дубликат")
                continue
            seen_usernames.add(u["username"])
            to_insert.append((name, u))

    print()
    print(f"Итого к переносу: {len(to_insert)} пользователей")
    for source_name, u in to_insert:
        print(f"  {u['username']:20s} роль={u['role']:8s} источник={source_name}")

    if args.dry_run:
        print("\n(--dry-run, ничего не записано)")
        return

    conn = sqlite3.connect(sso_db_path)
    conn.row_factory = sqlite3.Row
    inserted, skipped = 0, 0
    for source_name, u in to_insert:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (u["username"],)
        ).fetchone()
        if exists:
            print(f"  '{u['username']}' уже есть в sso.db — пропуск")
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO users (username, password_hash, role, full_name) VALUES (?, ?, ?, ?)",
            (u["username"], u["password_hash"], u["role"], u["full_name"]),
        )
        inserted += 1
    conn.commit()
    conn.close()
    print(f"\nГотово: добавлено {inserted}, пропущено (уже были) {skipped}.")
    print("Пароли перенесены как есть — пользователи заходят со старыми паролями.")


if __name__ == "__main__":
    main()
