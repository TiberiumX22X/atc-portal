"""Лёгкий AMI-клиент для панели «Обслуживание».

В отличие от монитор-панели (держит один долгоживущий фоновый поток опроса),
эта панель используется от случая к случаю — оператор открыл страницу,
отправил команду, посмотрел ответ. Поэтому здесь проще и безопаснее держать
разовое подключение на каждый вызов: открыли сокет → залогинились →
отправили экшен → прочитали ответ → закрыли. Ничего не висит в фоне между
запросами.
"""
import socket
from datetime import datetime
import config


class AMIError(Exception):
    pass


def _read_until_terminator(sock, timeout=8):
    """Читает из сокета до пустой строки (\\r\\n\\r\\n) — стандартный
    терминатор ПОЛНОГО ответа AMI (Response/Output-блоков). НЕ подходит
    для самого первого приветствия при подключении — см. _read_banner."""
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", errors="replace")


def _read_banner(sock, timeout=8):
    """AMI при подключении присылает ОДНУ строку-приветствие
    ("Asterisk Call Manager/X.X.X\\r\\n") — она заканчивается ОДИНАРНЫМ
    \\r\\n, не двойным. Ждать здесь двойной терминатор (как для обычных
    ответов) означает зависнуть до таймаута — банер его никогда не
    пришлёт. Читаем ровно до первого одинарного \\r\\n."""
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", errors="replace")


def send_action(action_lines, timeout=8):
    """Отправляет один AMI-экшен (уже полностью сформированный, включая
    завершающую пустую строку) и возвращает текст ответа. action_lines —
    строка вида "Action: Command\\r\\nCommand: core show channels\\r\\n\\r\\n"
    (без строк Login/Username/Secret — те добавляются автоматически)."""
    try:
        sock = socket.create_connection((config.AMI_HOST, config.AMI_PORT), timeout=timeout)
    except OSError as exc:
        raise AMIError(f"Не удалось подключиться к {config.AMI_HOST}:{config.AMI_PORT}: {exc}")

    try:
        banner = _read_banner(sock, timeout)  # приветствие AMI, не используется

        login = (
            f"Action: Login\r\n"
            f"Username: {config.AMI_USER}\r\n"
            f"Secret: {config.AMI_SECRET}\r\n"
            f"Events: off\r\n\r\n"
        )
        sock.sendall(login.encode())
        login_resp = _read_until_terminator(sock, timeout)
        if "Response: Success" not in login_resp:
            raise AMIError(f"AMI не подключён (ошибка логина): {login_resp.strip()}")

        sock.sendall(action_lines.encode())
        # У некоторых экшенов (Command) ответ приходит несколькими блоками
        # "Output: ..." подряд — дочитываем чуть дольше одного терминатора,
        # пока сокет не замолчит на паузу.
        sock.settimeout(1.5)
        full = ""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                full += chunk.decode("utf-8", errors="replace")
        except socket.timeout:
            pass
        return full
    finally:
        try:
            sock.sendall(b"Action: Logoff\r\n\r\n")
        except OSError:
            pass
        sock.close()


def _format_uptime(delta):
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours or days:
        parts.append(f"{hours} ч.")
    parts.append(f"{minutes} мин.")
    return " ".join(parts)


def core_status():
    """Быстрая сводка для дашборда: аптайм и число активных каналов.
    Возвращает dict; при недоступности AMI — dict с ключом 'error'."""
    try:
        uptime_resp = send_action("Action: CoreStatus\r\n\r\n")
        channels_resp = send_action(
            "Action: Command\r\nCommand: core show channels count\r\n\r\n"
        )
    except AMIError as exc:
        return {"error": str(exc)}

    result = {"raw_uptime": uptime_resp, "raw_channels": channels_resp}

    startup_date, startup_time = None, None
    for line in uptime_resp.splitlines():
        if line.startswith("CoreStartupDate:"):
            startup_date = line.split(":", 1)[1].strip()
        if line.startswith("CoreStartupTime:"):
            startup_time = line.split(":", 1)[1].strip()
        if line.startswith("CoreReloadTime:"):
            result["reload_time"] = line.split(":", 1)[1].strip()

    # CoreStartupTime — это ТОЛЬКО время суток (например "08:00:00"), без
    # даты. Раз Asterisk не перезапускался с момента старта, это значение
    # никогда не меняется — при показе "как есть" выглядит как зависшая,
    # неживая строка. Считаем реальный аптайм (сколько прошло с момента
    # старта) — это значение видимо тикает при каждой проверке и наглядно
    # подтверждает, что данные живые, а не заморожены.
    if startup_date and startup_time:
        try:
            started_at = datetime.strptime(f"{startup_date} {startup_time}", "%Y-%m-%d %H:%M:%S")
            result["startup_at"] = started_at.strftime("%d.%m.%Y %H:%M:%S")
            result["uptime_human"] = _format_uptime(datetime.now() - started_at)
        except ValueError:
            result["startup_at"] = f"{startup_date} {startup_time}"

    for line in channels_resp.splitlines():
        line = line.replace("Output: ", "").strip()
        if "active channel" in line:
            result["active_channels"] = line.split()[0]
        if "active call" in line:
            result["active_calls"] = line.split()[0]

    return result
