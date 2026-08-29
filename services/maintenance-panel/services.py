"""Обёртка над systemctl/journalctl — только для заранее известных юнитов
проекта. Ничего не выполняется через shell=True и никакое имя юнита не
подставляется, если оно не входит в белый список — это защита от любой
возможной инъекции команд, даже случайной."""
import subprocess

KNOWN_UNITS = {
    "sso-auth": "Портал / SSO",
    "monitor-panel": "Монитор АТС",
    "cdr-panel": "CDR-панель",
    "alert-panel": "Оповещение",
    "phone-provisioning": "Провижининг телефонов",
    "asterisk-panel": "Конференц-панель",
    "maintenance-panel": "Обслуживание (эта панель)",
    "nginx": "nginx (портал, порт 8888)",
}


def _run(args, timeout=10):
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Команда не ответила вовремя (timeout)"
    except FileNotFoundError:
        return -1, "", "Команда не найдена на сервере"


def get_status(unit):
    if unit not in KNOWN_UNITS:
        return {"unit": unit, "active": False, "error": "неизвестный юнит"}
    code, out, err = _run(["systemctl", "is-active", unit])
    active = out.strip() == "active"
    return {"unit": unit, "label": KNOWN_UNITS[unit], "active": active, "raw": out.strip() or err.strip()}


def get_all_status():
    return [get_status(u) for u in KNOWN_UNITS]


def restart(unit):
    if unit not in KNOWN_UNITS:
        raise ValueError("Неизвестный юнит")
    code, out, err = _run(["systemctl", "restart", unit], timeout=20)
    return code == 0, (err or out or "").strip()


def tail_log(unit, lines=100):
    if unit not in KNOWN_UNITS:
        raise ValueError("Неизвестный юнит")
    lines = max(10, min(int(lines), 1000))  # разумные границы, не даём запросить миллион строк
    code, out, err = _run(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"], timeout=15
    )
    return out or err
