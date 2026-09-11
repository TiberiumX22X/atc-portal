"""Ручное снятие уведомлений с колокольчика портала.

Не путать с автоматическим resolve в alerts_check.py — тот срабатывает,
когда проверка перестаёт находить проблему. Здесь — админ руками
скрывает уведомление, даже если проблема ещё не устранена. Если она
реально не устранена, alerts_check.py на следующем цикле (~2 мин)
снова его создаст — это ожидаемо, не баг: разовое "скрыть шум" не
равно "починили".

Уведомления с локального узла снимаются напрямую в alerts_local.db.
Уведомления с соседнего узла (node != текущий hostname) — через SSH
тем же способом, каким уже пользуется sync_panels.sh/sync_astdb.sh
(root@PEER_IP по ключу, без пароля).
"""
import os
import re
import socket
import sqlite3
import subprocess

import config
import db

NODE = socket.gethostname()
# alert_key всегда генерируется самим alerts_check.py (не из пользовательского
# ввода) — но валидируем формат перед подстановкой в SSH-команду, для защиты
# от инъекции на всякий случай.
KEY_RE = re.compile(r"^[a-zA-Z0-9:_./-]+$")


class DismissError(Exception):
    pass


def _read_peer_ip():
    if not os.path.exists(config.HA_CONF_PATH):
        return None
    with open(config.HA_CONF_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("PEER_IP="):
                return line.split("=", 1)[1].strip()
    return None


def _dismiss_in_file(path, key):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "UPDATE alerts SET resolved_at = datetime('now','localtime') WHERE alert_key = ? AND resolved_at IS NULL",
        (key,),
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0


def dismiss(key, node):
    if not KEY_RE.match(key or ""):
        raise DismissError("Некорректный идентификатор уведомления.")

    if not node or node == NODE:
        if not _dismiss_in_file(config.LOCAL_ALERTS_DB_PATH, key):
            raise DismissError("Уведомление уже неактивно или не найдено.")
        return

    peer_ip = _read_peer_ip()
    if not peer_ip:
        raise DismissError(f"Уведомление с узла {node}, но адрес соседа неизвестен (HA не настроен на этом сервере?).")

    sql = (
        "UPDATE alerts SET resolved_at=datetime('now','localtime') "
        f"WHERE alert_key='{key}' AND resolved_at IS NULL;"
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"root@{peer_ip}",
         "sqlite3", "/opt/sso-auth/alerts_local.db", sql],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise DismissError(f"Не удалось снять уведомление на узле {node} ({peer_ip}): {(proc.stderr or proc.stdout).strip()}")

    # Обновляем и свою локальную копию peer_alerts.db — иначе строка
    # провисит на экране до ближайшего цикла sync_peer_alerts.sh (~2 мин).
    if os.path.exists(config.PEER_ALERTS_DB_PATH):
        try:
            _dismiss_in_file(config.PEER_ALERTS_DB_PATH, key)
        except sqlite3.OperationalError:
            pass


def dismiss_all():
    """Снять все текущие активные уведомления (локальные — сразу,
    с соседа — по одному через SSH). Возвращает (успешно, с ошибкой)."""
    ok, failed = 0, 0
    for row in db.get_active_alerts():
        try:
            dismiss(row["alert_key"], row["node"])
            ok += 1
        except DismissError:
            failed += 1
    return ok, failed
