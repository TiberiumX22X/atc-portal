"""Управление исключениями fail2ban (ignoreip) из веб-интерфейса.

Полезно, если jail (например 'recidive') банит адрес, который на самом
деле легитимный — например IP другой АТС/шлюза, с которым идёт транк:
повторяющиеся срабатывания смежных jail'ов во время отладки транка
могут привести к бану, и звонки перестают ходить без какой-либо явной
связи с настройками самого FreePBX/Asterisk — recidive стоит в цепочке
INPUT раньше правил FreePBX Firewall и режет пакет ещё до них.

Отдельно от phone_firewall.py (тот управляет Trusted-зоной FreePBX
Firewall через fwconsole) — это два разных, не пересекающихся
защитных механизма на сервере, ignoreip меняется через свой файл.

Изменение здесь также best-effort применяется на СОСЕДНЕМ узле
HA-кластера через SSH (тот же ключ, что уже используют
sync_panels.sh/sync_astdb.sh) — иначе после failover на резервный узел
пришлось бы вспоминать и повторять то же самое там вручную. Если сосед
недоступен — локальное изменение всё равно применяется, просто
возвращается предупреждение.
"""
import os
import re
import shlex
import subprocess

JAIL_LOCAL_PATH = "/etc/fail2ban/jail.local"
HA_CONF_PATH = "/etc/atc-portal-ha.conf"
CIDR_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$")


class ValidationError(Exception):
    pass


def _validate_subnet(subnet):
    if not CIDR_RE.match(subnet):
        raise ValidationError(f"«{subnet}» не похоже на IP-адрес или подсеть в формате CIDR (например 192.168.7.0/24).")


def _read_lines():
    try:
        with open(JAIL_LOCAL_PATH, encoding="utf-8") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def _write_lines(lines):
    with open(JAIL_LOCAL_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def get_ignoreip_list():
    """Текущий список исключений — плоский список адресов/подсетей."""
    lines = _read_lines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ignoreip"):
            _, _, value = stripped.partition("=")
            return value.split(), None
    return [], None


def _reload():
    proc = subprocess.run(
        ["fail2ban-client", "reload"],
        capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0:
        raise ValidationError(f"fail2ban-client reload завершился с ошибкой: {(proc.stderr or proc.stdout).strip()}")


def _read_peer_ip():
    if not os.path.exists(HA_CONF_PATH):
        return None
    with open(HA_CONF_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("PEER_IP="):
                return line.split("=", 1)[1].strip()
    return None


def _apply_remote(action, subnet):
    """Применяет то же изменение на соседнем узле через SSH. Best-effort:
    никогда не бросает исключение — локальное изменение должно остаться в
    силе, даже если сосед сейчас недоступен. Возвращает None при успехе
    (или если HA не настроен — синхронизировать нечего), иначе короткое
    описание проблемы для предупреждения в интерфейсе."""
    peer_ip = _read_peer_ip()
    if not peer_ip:
        return None

    method = "add_ignoreip" if action == "add" else "remove_ignoreip"
    remote_code = (
        "import sys; sys.path.insert(0, '/opt/maintenance-panel'); "
        f"import fail2ban_ignoreip as f; f.{method}({subnet!r}, sync_peer=False)"
    )
    # SSH сам заново склеивает все аргументы команды в ОДНУ строку и
    # отправляет её шеллу удалённого сервера — передача списком (как для
    # обычного subprocess.run) НЕ защищает от разбора спецсимволов на
    # удалённой стороне. Поэтому строим и экранируем команду сами через
    # shlex.quote и передаём уже готовой ОДНОЙ строкой.
    remote_cmd = f"{shlex.quote('/opt/maintenance-panel/venv/bin/python3')} -c {shlex.quote(remote_code)}"
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"root@{peer_ip}", remote_cmd],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"не удалось связаться с соседним узлом ({peer_ip}): {exc}"

    if proc.returncode != 0:
        return f"не применилось на соседнем узле ({peer_ip}): {(proc.stderr or proc.stdout).strip()[:200]}"
    return None


def add_ignoreip(subnet, sync_peer=True):
    subnet = subnet.strip()
    if not subnet:
        raise ValidationError("Укажите подсеть или адрес.")
    _validate_subnet(subnet)

    current, _ = get_ignoreip_list()
    if subnet in current:
        raise ValidationError(f"{subnet} уже в списке исключений.")

    lines = _read_lines()
    found_default, found_ignoreip = False, False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[DEFAULT]":
            found_default = True
        elif stripped.startswith("ignoreip") and found_default:
            lines[i] = line.rstrip("\n") + f" {subnet}\n"
            found_ignoreip = True
            break

    if not found_ignoreip:
        if not found_default:
            lines.append("\n[DEFAULT]\n")
        lines.append(f"ignoreip = 127.0.0.1/8 {subnet}\n")

    _write_lines(lines)
    _reload()

    if sync_peer:
        return _apply_remote("add", subnet)
    return None


def remove_ignoreip(subnet, sync_peer=True):
    subnet = subnet.strip()
    if not subnet:
        raise ValidationError("Укажите подсеть или адрес.")

    lines = _read_lines()
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("ignoreip"):
            _, _, value = stripped.partition("=")
            addrs = [a for a in value.split() if a != subnet]
            if len(addrs) != len(value.split()):
                lines[i] = "ignoreip = " + " ".join(addrs) + "\n"
                changed = True
            break

    if not changed:
        raise ValidationError(f"{subnet} не найден в списке исключений.")

    _write_lines(lines)
    _reload()

    if sync_peer:
        return _apply_remote("remove", subnet)
    return None
