"""Единое место, которое знает про все AMI-учётки проекта и про то, КАК
именно каждая панель хранит свой секрет — а хранят они его по-разному,
и это ровно то, из-за чего мы весь проект спотыкались (см. историю с
конференц-панелью, где секрет живёт литералом в api.py и терялся при
каждой полной замене файла). Ротация здесь обновляет секрет everywhere
одним действием — в Asterisk, в хранилище самой панели, и перезапускает
её, чтобы новый секрет реально подхватился.
"""
import os
import re
import sqlite3
import subprocess

MANAGER_CONF = "/etc/asterisk/manager_custom.conf"

# storage:
#   "env"     — секрет в файле .env как AMI_SECRET=...
#   "sqlite"  — секрет в таблице settings(key, value) sqlite-базы панели
#   "literal" — секрет зашит текстом в самом .py-файле (самый хрупкий вариант)
AMI_USERS = {
    "monitorpanel": {
        "label": "Монитор АТС",
        "service": "monitor-panel",
        "storage": "env",
        "path": "/opt/monitor-panel/.env",
    },
    "alertpanel": {
        "label": "Оповещение",
        "service": "alert-panel",
        "storage": "sqlite",
        "path": "/opt/alert-panel/alert_panel.db",
    },
    "dashboard": {
        "label": "Конференц-панель",
        "service": "asterisk-panel",
        "storage": "literal",
        "path": "/opt/asterisk-panel/api.py",
    },
    "maintpanel": {
        "label": "Обслуживание (эта панель)",
        "service": "maintenance-panel",
        "storage": "env",
        "path": "/opt/maintenance-panel/.env",
    },
    "portalalerts": {
        "label": "Уведомления портала (проверка AMI)",
        "service": "sso-auth",
        "storage": "env",
        "path": "/opt/sso-auth/.env",
    },
}


def _random_secret(length=20):
    import secrets as _secrets
    return _secrets.token_urlsafe(length)


def read_manager_secret(username):
    """Текущий секрет пользователя из manager_custom.conf, либо None,
    если стойки такого пользователя там ещё нет."""
    if not os.path.exists(MANAGER_CONF):
        return None
    with open(MANAGER_CONF, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(rf"^\[{re.escape(username)}\]\s*\n(?:.*\n)*?secret\s*=\s*(\S+)", content, re.MULTILINE)
    return m.group(1) if m else None


def list_ami_users():
    """Сводка по всем известным AMI-пользователям: есть ли стойка в
    Asterisk, совпадает ли секрет там с тем, что хранит сама панель —
    последнее показывает точно ту рассинхронизацию, что мы чинили вручную
    несколько раз за этот проект."""
    result = []
    for username, meta in AMI_USERS.items():
        manager_secret = read_manager_secret(username)
        panel_secret = _read_panel_secret(meta)
        result.append({
            "username": username,
            "label": meta["label"],
            "service": meta["service"],
            "configured_in_asterisk": manager_secret is not None,
            "in_sync": manager_secret is not None and panel_secret is not None and manager_secret == panel_secret,
            "panel_secret_found": panel_secret is not None,
        })
    return result


def _read_panel_secret(meta):
    path = meta["path"]
    if not os.path.exists(path):
        return None
    try:
        if meta["storage"] == "env":
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("AMI_SECRET="):
                        return line.split("=", 1)[1].strip()
            return None
        if meta["storage"] == "sqlite":
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT value FROM settings WHERE key='ami_secret'").fetchone()
            conn.close()
            return row[0] if row else None
        if meta["storage"] == "literal":
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"'ami_secret':\s*'([^']*)'", content)
            return m.group(1) if m else None
    except (OSError, sqlite3.Error):
        return None
    return None


def _write_manager_secret(username, new_secret):
    meta = AMI_USERS[username]
    if not os.path.exists(MANAGER_CONF):
        raise RuntimeError(f"{MANAGER_CONF} не найден")
    with open(MANAGER_CONF, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = re.compile(rf"(^\[{re.escape(username)}\]\s*\n(?:.*\n)*?secret\s*=\s*)\S+", re.MULTILINE)
    if not pattern.search(content):
        raise RuntimeError(f"Стойка [{username}] не найдена в {MANAGER_CONF} — создайте её вручную сначала")
    content = pattern.sub(rf"\g<1>{new_secret}", content, count=1)

    with open(MANAGER_CONF, "w", encoding="utf-8") as f:
        f.write(content)

    subprocess.run(["asterisk", "-rx", "manager reload"], capture_output=True, timeout=10)


def _write_panel_secret(meta, new_secret):
    path = meta["path"]
    if meta["storage"] == "env":
        lines = []
        found = False
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("AMI_SECRET="):
                        lines.append(f"AMI_SECRET={new_secret}\n")
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f"AMI_SECRET={new_secret}\n")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.chmod(path, 0o600)

    elif meta["storage"] == "sqlite":
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('ami_secret', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (new_secret,),
        )
        conn.commit()
        conn.close()

    elif meta["storage"] == "literal":
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        new_content, n = re.subn(r"('ami_secret':\s*)'[^']*'", rf"\g<1>'{new_secret}'", content, count=1)
        if n == 0:
            raise RuntimeError("Не нашёл строку 'ami_secret' в файле панели — измените вручную")
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)


def rotate_secret(username):
    """Полная ротация: новый секрет → Asterisk → хранилище панели →
    перезапуск панели, чтобы новый секрет реально применился. Если что-то
    падает на середине — секрет в Asterisk и в панели может временно
    разойтись; сообщение об ошибке явно это укажет, чтобы не гадать
    (ровно то, что нам сегодня стоило часа отладки с конференц-панелью)."""
    if username not in AMI_USERS:
        raise ValueError("Неизвестный AMI-пользователь")
    meta = AMI_USERS[username]
    new_secret = _random_secret()

    _write_manager_secret(username, new_secret)
    _write_panel_secret(meta, new_secret)

    service = meta["service"]
    self_restart = service == "maintenance-panel"
    if self_restart:
        # Перезапускаем себя же — отправляем команду в фоне с небольшой
        # задержкой, чтобы этот HTTP-ответ успел уйти пользователю раньше,
        # чем сервис реально остановится.
        subprocess.Popen(
            ["bash", "-c", "sleep 1 && systemctl restart maintenance-panel"],
            start_new_session=True,
        )
    else:
        subprocess.run(["systemctl", "restart", service], capture_output=True, timeout=20)

    return new_secret, self_restart
