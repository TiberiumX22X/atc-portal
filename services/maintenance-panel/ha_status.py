"""Статус HA-кластера — только для просмотра, никаких действий отсюда не
выполняется (переключение ролей полностью на keepalived, вручную ничего
дёргать не нужно и не стоит). Читает общий конфиг /etc/atc-portal-ha.conf,
который создаёт установщик (lib/ha.sh, пункт меню «Настроить HA-кластер»)
при настройке этого узла — если файла нет, значит HA на этом сервере ещё
не настраивался."""
import os
import re
import subprocess

HA_CONFIG_PATH = "/etc/atc-portal-ha.conf"
KEEPALIVED_NOTIFY_LOG = "/var/log/keepalived-notify.log"
SYNC_PANELS_LOG = "/var/log/sync_panels.log"
SYNC_ASTDB_LOG = "/var/log/sync_astdb.log"


def _run(args, timeout=8):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return -1, "", ""


def read_ha_config():
    """Роль/IP-адреса/VIP этого узла — как заданы при установке HA.
    Возвращает None, если HA-кластер вообще не настроен на этом сервере."""
    if not os.path.exists(HA_CONFIG_PATH):
        return None
    config = {}
    with open(HA_CONFIG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
    return config or None


def get_keepalived_status():
    code, out, _ = _run(["systemctl", "is-active", "keepalived"])
    return out.strip() == "active"


def get_vrrp_state(vip):
    """Определяет текущее состояние VRRP этого узла по самому надёжному
    признаку — поднят ли реально VIP на локальном интерфейсе прямо сейчас
    (не по журналу, который может быть неактуален, а по факту)."""
    if not vip:
        return "неизвестно"
    code, out, _ = _run(["ip", "addr", "show"])
    if vip in out:
        return "MASTER (VIP поднят на этом узле)"
    return "BACKUP (VIP на другом узле)"


def get_peer_reachable(peer_ip):
    if not peer_ip:
        return None
    code, _, _ = _run(["ping", "-c", "1", "-W", "2", peer_ip])
    return code == 0


def get_replication_status(role):
    """SHOW SLAVE STATUS имеет смысл смотреть только на backup-узле (это
    он тянет данные с master). На master-узле реплика не запущена в
    принципе — не ошибка, так и должно быть."""
    if role != "backup":
        return {"applicable": False}
    code, out, err = _run(["mysql", "-N", "-e", "SHOW SLAVE STATUS\\G"])
    if code != 0:
        return {"applicable": True, "error": err.strip() or "Не удалось подключиться к MySQL"}
    io_running = re.search(r"Slave_IO_Running:\s*(\S+)", out)
    sql_running = re.search(r"Slave_SQL_Running:\s*(\S+)", out)
    lag = re.search(r"Seconds_Behind_Master:\s*(\S+)", out)
    return {
        "applicable": True,
        "io_running": io_running.group(1) if io_running else "?",
        "sql_running": sql_running.group(1) if sql_running else "?",
        "seconds_behind": lag.group(1) if lag else "?",
    }


def _tail_log(path, lines=5):
    if not os.path.exists(path):
        return None
    code, out, _ = _run(["tail", "-n", str(lines), path])
    return out.strip() or None


def get_sync_status():
    """Периодическая (не мгновенная — раз в 3-5 минут по cron) синхронизация
    панелей и astdb.sqlite3 настраивается только на master. На backup эти
    логи попросту не существуют — это ожидаемо, не ошибка."""
    return {
        "panels_log": _tail_log(SYNC_PANELS_LOG),
        "astdb_log": _tail_log(SYNC_ASTDB_LOG),
    }


def get_full_status():
    """Собирает всё разом для страницы «HA-кластер» в одном вызове."""
    config = read_ha_config()
    if config is None:
        return {"configured": False}

    role = config.get("ROLE", "?")
    vip = config.get("VIP", "")
    peer_ip = config.get("PEER_IP", "")

    return {
        "configured": True,
        "config": config,
        "keepalived_active": get_keepalived_status(),
        "vrrp_state": get_vrrp_state(vip),
        "peer_reachable": get_peer_reachable(peer_ip),
        "replication": get_replication_status(role),
        "sync": get_sync_status(),
        "notify_log": _tail_log(KEEPALIVED_NOTIFY_LOG, lines=10),
    }
