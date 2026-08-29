"""Запуск настройки HA-кластера (ha_apply.sh) из веб-интерфейса, без входа
по SSH. В отличие от ha_status.py (только просмотр) — здесь единственное
действие, которое можно запустить, это ПЕРВОНАЧАЛЬНАЯ настройка узла
(keepalived + репликация + синхронизация). Переключение ролей во время
работы по-прежнему полностью на keepalived, вручную это не трогается.

Скрипт ha_apply.sh лежит рядом с этим файлом (устанавливается вместе с
панелью через deploy_source) — путь известен заранее, не зависит от того,
где на сервере лежит клонированный репозиторий установщика.
"""
import ipaddress
import os
import re
import signal
import subprocess
import time

HA_APPLY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ha_apply.sh")
HA_FAILBACK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ha_safe_failback.sh")
LOG_PATH = "/var/log/atc-ha-setup.log"
PID_PATH = "/var/run/atc-ha-setup.pid"
FAILBACK_LOG_PATH = "/var/log/atc-ha-failback.log"
FAILBACK_PID_PATH = "/var/run/atc-ha-failback.pid"
HA_CONF_PATH = "/etc/atc-portal-ha.conf"


class ValidationError(Exception):
    pass


def get_local_interfaces():
    """Список сетевых интерфейсов этого сервера с их IPv4/CIDR — чтобы в
    форме можно было выбрать из списка, а не набирать вручную и не
    ошибиться в адресе собственного сервера."""
    try:
        proc = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=5
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    result = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1]
        if iface == "lo":
            continue
        addr_cidr = parts[3]  # вида 192.168.3.162/21
        ip_str, _, cidr = addr_cidr.partition("/")
        result.append({"iface": iface, "ip": ip_str, "cidr": cidr})
    return result


def is_setup_running():
    """Идёт ли прямо сейчас настройка (запущенная ранее и ещё не завершившаяся)."""
    if not os.path.exists(PID_PATH):
        return False
    try:
        with open(PID_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _validate_ip(value, field_name):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ValidationError(f"«{field_name}» не похоже на корректный IP-адрес: {value}")


def start_setup(role, local_ip, peer_ip, vip, cidr, iface, vrrp_pass, repl_pass,
                 peer_user="", peer_password=""):
    """Проверяет параметры и запускает ha_apply.sh в фоне. Возвращает
    сразу, не дожидаясь завершения — процесс может идти несколько минут
    (снятие дампа БД, передача по сети). Прогресс — через /ha-setup/log.

    peer_user/peer_password — необязательные, логин и пароль ОБЫЧНОГО
    (не root) администратора второго сервера. Если заполнены, ha_apply.sh
    сам, один раз, через sudo на этой учётке разложит SSH-ключ и настроит
    firewall/fail2ban на соседе — вместо того чтобы просить сделать это
    руками. Пароль никуда не сохраняется, используется только для этого
    одного запуска и передаётся дочернему процессу, не в лог."""
    if is_setup_running():
        raise ValidationError("Настройка уже выполняется на этом сервере — дождитесь её завершения.")

    if role not in ("master", "backup"):
        raise ValidationError("Роль должна быть master или backup.")
    for value, name in ((local_ip, "локальный IP"), (peer_ip, "IP второго сервера"), (vip, "VIP")):
        _validate_ip(value, name)
    if not re.fullmatch(r"\d{1,2}", cidr) or not (1 <= int(cidr) <= 32):
        raise ValidationError("Маска сети (CIDR) должна быть числом от 1 до 32.")
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]+", iface):
        raise ValidationError("Некорректное имя сетевого интерфейса.")
    if not vrrp_pass or len(vrrp_pass) > 8:
        raise ValidationError("Пароль VRRP-аутентификации — от 1 до 8 символов (ограничение протокола).")
    if not repl_pass:
        raise ValidationError("Пароль пользователя репликации MySQL обязателен.")
    if local_ip == peer_ip:
        raise ValidationError("Локальный IP и IP второго сервера не могут совпадать.")

    with open(LOG_PATH, "w", encoding="utf-8") as log_file:
        log_file.write(f"=== Запуск настройки HA ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===\n")

    log_file = open(LOG_PATH, "a", encoding="utf-8")
    args = [
        "bash", HA_APPLY_SCRIPT,
        "--role", role,
        "--local-ip", local_ip,
        "--peer-ip", peer_ip,
        "--vip", vip,
        "--cidr", cidr,
        "--iface", iface,
        "--vrrp-pass", vrrp_pass,
        "--repl-pass", repl_pass,
    ]
    if peer_user and peer_password:
        args += ["--peer-user", peer_user, "--peer-password", peer_password]
    proc = subprocess.Popen(
        args,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with open(PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))


def get_log_tail(lines=200):
    if not os.path.exists(LOG_PATH):
        return ""
    try:
        proc = subprocess.run(["tail", "-n", str(lines), LOG_PATH], capture_output=True, text=True, timeout=5)
        return proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def get_log_state():
    """Для страницы прогресса: идёт ли ещё процесс, и если завершился — с
    каким результатом (по маркеру HA_SETUP_RESULT в последней строке лога)."""
    running = is_setup_running()
    log_text = get_log_tail()
    result = None
    if not running:
        m = re.search(r"HA_SETUP_RESULT:\s*(OK|FAILED.*)", log_text)
        if m:
            result = m.group(1).strip()
    return {"running": running, "log": log_text, "result": result}


# ---------------------------------------------------------------------------
# Безопасный возврат в кластер после простоя узла (не путать с
# первоначальной настройкой выше) — см. ha_safe_failback.sh.
# ---------------------------------------------------------------------------

def has_existing_ha_config():
    """Узел уже когда-либо был настроен в HA — тогда для него имеет смысл
    предлагать «безопасный возврат», а не только первоначальную настройку."""
    return os.path.exists(HA_CONF_PATH)


def is_failback_running():
    if not os.path.exists(FAILBACK_PID_PATH):
        return False
    try:
        with open(FAILBACK_PID_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def start_failback(repl_pass, peer_user="", peer_password=""):
    """Запускает ha_safe_failback.sh в фоне. Параметры узла (свой IP, IP
    соседа, VIP) скрипт сам берёт из /etc/atc-portal-ha.conf — здесь нужен
    только пароль репликации (тот же, что задавался при первой настройке)
    и, опционально, логин/пароль администратора соседа для автоматического
    SSH-бутстрапа, если ключ ещё не настроен (например, после переустановки
    именно этого узла)."""
    if is_failback_running():
        raise ValidationError("Безопасный возврат уже выполняется на этом сервере — дождитесь завершения.")
    if is_setup_running():
        raise ValidationError("Сейчас выполняется первоначальная настройка HA — дождитесь её завершения.")
    if not has_existing_ha_config():
        raise ValidationError("Этот узел ещё ни разу не был частью HA-кластера — сначала обычная настройка, а не безопасный возврат.")
    if not repl_pass:
        raise ValidationError("Пароль пользователя репликации обязателен.")

    with open(FAILBACK_LOG_PATH, "w", encoding="utf-8") as log_file:
        log_file.write(f"=== Безопасный возврат в кластер ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===\n")

    log_file = open(FAILBACK_LOG_PATH, "a", encoding="utf-8")
    args = ["bash", HA_FAILBACK_SCRIPT, "--repl-pass", repl_pass]
    if peer_user and peer_password:
        args += ["--peer-user", peer_user, "--peer-password", peer_password]
    proc = subprocess.Popen(
        args,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with open(FAILBACK_PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))


def get_failback_log_tail(lines=200):
    if not os.path.exists(FAILBACK_LOG_PATH):
        return ""
    try:
        proc = subprocess.run(["tail", "-n", str(lines), FAILBACK_LOG_PATH], capture_output=True, text=True, timeout=5)
        return proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def get_failback_log_state():
    running = is_failback_running()
    log_text = get_failback_log_tail()
    result = None
    if not running:
        m = re.search(r"FAILBACK_RESULT:\s*(OK|FAILED.*)", log_text)
        if m:
            result = m.group(1).strip()
    return {"running": running, "log": log_text, "result": result}
