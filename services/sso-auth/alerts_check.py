#!/usr/bin/env python3
"""
Собирает уведомления об ошибках системы для главной страницы портала:
падение systemd-сервисов панелей, обрыв AMI, нехватка места на диске,
проблемы HA-кластера. Запускается по cron (см. install_sso_auth в
lib/services.sh) каждые пару минут, ничего не выводит в норме.

Каждая проверка вызывает upsert_alert(key, ...) при обнаруженной
проблеме и resolve_alert(key) когда проблема ушла — так активные
уведомления на портале всегда отражают текущее состояние, а не
накапливаются бесконечно.
"""
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import db  # noqa: E402

NODE = socket.gethostname()


def _local_conn():
    conn = sqlite3.connect(config.LOCAL_ALERTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def upsert_alert(key, source, severity, message):
    conn = _local_conn()
    existing = conn.execute(
        "SELECT id FROM alerts WHERE alert_key = ? AND resolved_at IS NULL", (key,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE alerts SET last_seen = datetime('now','localtime'), message = ?, severity = ? WHERE id = ?",
            (message, severity, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO alerts (alert_key, node, source, severity, message) VALUES (?, ?, ?, ?, ?)",
            (key, NODE, source, severity, message),
        )
    conn.commit()
    conn.close()


def resolve_alert(key):
    conn = _local_conn()
    conn.execute(
        "UPDATE alerts SET resolved_at = datetime('now','localtime') WHERE alert_key = ? AND resolved_at IS NULL",
        (key,),
    )
    conn.commit()
    conn.close()


def resolve_missing(source, current_keys):
    """Снимает все активные уведомления данного source, которых нет
    среди current_keys — проверка либо больше не находит проблему у
    этого объекта, либо сам объект пропал из списка (например юнит
    переименовали)."""
    conn = _local_conn()
    rows = conn.execute(
        "SELECT alert_key FROM alerts WHERE source = ? AND resolved_at IS NULL", (source,)
    ).fetchall()
    conn.close()
    for row in rows:
        if row["alert_key"] not in current_keys:
            resolve_alert(row["alert_key"])


# ---------------------------------------------------------------------------
# 1. systemd-сервисы панелей
# ---------------------------------------------------------------------------

def check_services():
    active_keys = set()
    for unit in config.PANEL_UNITS:
        key = f"service:{unit}"
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=10,
        )
        status = proc.stdout.strip()
        if status != "active":
            upsert_alert(key, "service", "error", f"Сервис {unit} не запущен (статус: {status or 'unknown'})")
            active_keys.add(key)
        else:
            resolve_alert(key)
    resolve_missing("service", active_keys)


# ---------------------------------------------------------------------------
# 2. AMI — живость соединения (отдельный read-only пользователь)
# ---------------------------------------------------------------------------

def check_ami():
    key = "ami:connection"
    if not config.AMI_SECRET:
        # .env ещё не заполнен (например при первой установке до
        # применения install.sh) — не шумим уведомлением, просто пропускаем.
        return
    try:
        sock = socket.create_connection((config.AMI_HOST, config.AMI_PORT), timeout=5)
        sock.settimeout(5)
        sock.recv(1024)  # приветственная строка
        login = (
            f"Action: Login\r\nUsername: {config.AMI_USER}\r\n"
            f"Secret: {config.AMI_SECRET}\r\nEvents: off\r\n\r\n"
        )
        sock.sendall(login.encode("utf-8"))
        resp = sock.recv(4096).decode("utf-8", errors="replace")
        sock.close()
        if "Success" not in resp:
            upsert_alert(key, "ami", "error", f"AMI: логин не прошёл ({resp.strip().splitlines()[:1]})")
            return
        resolve_alert(key)
    except OSError as exc:
        upsert_alert(key, "ami", "error", f"AMI недоступен на {config.AMI_HOST}:{config.AMI_PORT}: {exc}")


# ---------------------------------------------------------------------------
# 3. Место на диске
# ---------------------------------------------------------------------------

def check_disk():
    key = "disk:/"
    total, used, free = shutil.disk_usage("/")
    free_percent = free / total * 100
    if free_percent < config.DISK_FREE_PERCENT_THRESHOLD:
        upsert_alert(
            key, "disk", "warning",
            f"Свободно на диске: {free_percent:.1f}% ({free // (1024**3)} ГБ) — ниже порога {config.DISK_FREE_PERCENT_THRESHOLD}%",
        )
    else:
        resolve_alert(key)


# ---------------------------------------------------------------------------
# 4. HA-кластер
# ---------------------------------------------------------------------------

def _read_ha_conf():
    if not os.path.exists(config.HA_CONF_PATH):
        return None
    result = {}
    with open(config.HA_CONF_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    return result


def _get_freepbx_db_config():
    conf_path = "/etc/freepbx.conf"
    if not os.path.exists(conf_path):
        return None
    with open(conf_path, encoding="utf-8") as f:
        content = f.read()
    result = {}
    mapping = {"AMPDBHOST": "host", "AMPDBNAME": "database", "AMPDBUSER": "user", "AMPDBPASS": "password"}
    for k, field in mapping.items():
        m = re.search(r'\$amp_conf\[["\']' + k + r'["\']\]\s*=\s*["\']([^"\']*)["\']', content)
        if m:
            result[field] = m.group(1)
    result.setdefault("host", "localhost")
    result.setdefault("database", "asterisk")
    if "user" not in result or "password" not in result:
        return None
    return result


def check_ha():
    ha_conf = _read_ha_conf()
    if ha_conf is None:
        # HA не настроен на этом сервере вообще — нечего проверять.
        return

    active_keys = set()

    # keepalived должен быть запущен на обоих узлах, независимо от роли.
    key = "ha:keepalived"
    proc = subprocess.run(["systemctl", "is-active", "keepalived"], capture_output=True, text=True, timeout=10)
    if proc.stdout.strip() != "active":
        upsert_alert(key, "ha", "error", "keepalived не запущен — узел не участвует в HA-кластере")
        active_keys.add(key)
    else:
        resolve_alert(key)

    # VIP должен отвечать откуда-то (неважно, с какого узла сейчас активна роль).
    vip = ha_conf.get("VIP")
    if vip:
        key = "ha:vip-unreachable"
        proc = subprocess.run(["ping", "-c", "1", "-W", "1", vip], capture_output=True, timeout=5)
        if proc.returncode != 0:
            upsert_alert(key, "ha", "error", f"VIP {vip} не отвечает ни с одного узла кластера")
            active_keys.add(key)
        else:
            resolve_alert(key)

    # Отставание репликации MariaDB — проверяем только на backup-узле
    # (на master эта метрика не имеет смысла).
    if ha_conf.get("ROLE", "").lower() == "backup":
        key = "ha:replication-lag"
        db_conf = _get_freepbx_db_config()
        if db_conf:
            try:
                import pymysql
                conn = pymysql.connect(
                    host=db_conf["host"], user=db_conf["user"], password=db_conf["password"],
                    database=db_conf["database"], cursorclass=pymysql.cursors.DictCursor,
                    connect_timeout=5,
                )
                try:
                    with conn.cursor() as cur:
                        cur.execute("SHOW SLAVE STATUS")
                        row = cur.fetchone()
                finally:
                    conn.close()
                lag = row.get("Seconds_Behind_Master") if row else None
                if row is None:
                    upsert_alert(key, "ha", "error", "MariaDB: репликация не настроена или остановлена (SHOW SLAVE STATUS пуст)")
                    active_keys.add(key)
                elif lag is None:
                    upsert_alert(key, "ha", "error", "MariaDB: репликация остановлена (Seconds_Behind_Master = NULL)")
                    active_keys.add(key)
                elif lag > config.HA_REPLICATION_LAG_THRESHOLD:
                    upsert_alert(key, "ha", "warning", f"MariaDB: отставание репликации {lag} сек (порог {config.HA_REPLICATION_LAG_THRESHOLD})")
                    active_keys.add(key)
                else:
                    resolve_alert(key)
            except Exception as exc:  # pymysql.err.*, любые сетевые ошибки
                upsert_alert(key, "ha", "error", f"MariaDB: не удалось проверить репликацию: {exc}")
                active_keys.add(key)

    resolve_missing("ha", active_keys)


if __name__ == "__main__":
    db.init_local_alerts_db()
    check_services()
    check_ami()
    check_disk()
    check_ha()
    db.cleanup_old_alerts()
