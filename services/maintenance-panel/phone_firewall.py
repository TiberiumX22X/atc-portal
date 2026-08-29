"""Управление доверенными подсетями Firewall FreePBX из веб-интерфейса.

Изначально сделано под конкретную проблему: порт 8090 (провижининг
телефонов) не входит ни в один штатный сервис Firewall FreePBX и молча
блокируется для недоверенных сетей — телефоны из LAN не могли скачать
конфигурацию, хотя сам сервер отвечал на запросы с localhost. Решение —
дать администратору управлять доверенными подсетями без консоли и без
переустановки панелей.
"""
import re
import subprocess

CIDR_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$")


class ValidationError(Exception):
    pass


def _validate_subnet(subnet):
    if not CIDR_RE.match(subnet):
        raise ValidationError(f"«{subnet}» не похоже на IP-адрес или подсеть в формате CIDR (например 192.168.0.0/24).")


def get_trusted_list():
    """Список текущих доверенных сетей — как есть, для отображения."""
    try:
        proc = subprocess.run(
            ["fwconsole", "firewall", "list", "trusted"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return [], str(exc)
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout).strip()
    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if CIDR_RE.match(line):
            entries.append(line)
    return entries, None


def add_trusted_subnet(subnet):
    subnet = subnet.strip()
    if not subnet:
        raise ValidationError("Укажите подсеть или адрес.")
    _validate_subnet(subnet)

    proc = subprocess.run(
        ["fwconsole", "firewall", "add", "trusted", subnet],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise ValidationError(f"fwconsole не смог добавить {subnet}: {(proc.stderr or proc.stdout).strip()}")

    restart = subprocess.run(
        ["fwconsole", "firewall", "restart"],
        capture_output=True, text=True, timeout=30,
    )
    if restart.returncode != 0:
        raise ValidationError(f"Подсеть добавлена, но firewall не перезапустился: {(restart.stderr or restart.stdout).strip()} — примените вручную: fwconsole firewall restart")


def remove_trusted_subnet(subnet):
    subnet = subnet.strip()
    if not subnet:
        raise ValidationError("Укажите подсеть или адрес.")

    proc = subprocess.run(
        ["fwconsole", "firewall", "del", "trusted", subnet],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise ValidationError(f"fwconsole не смог удалить {subnet}: {(proc.stderr or proc.stdout).strip()}")

    restart = subprocess.run(
        ["fwconsole", "firewall", "restart"],
        capture_output=True, text=True, timeout=30,
    )
    if restart.returncode != 0:
        raise ValidationError(f"Подсеть удалена, но firewall не перезапустился: {(restart.stderr or restart.stdout).strip()} — примените вручную: fwconsole firewall restart")
