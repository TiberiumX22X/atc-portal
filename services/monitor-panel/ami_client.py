import socket
import time
import config

_TERMINATOR = b"\r\n\r\n"


class AmiError(Exception):
    pass


class AmiConnection:
    """Одно синхронное AMI-соединение с Events: off — только request/response,
    без подписки на события. По уроку проекта: отдельное соединение под свой
    тип нагрузки, не смешиваем с другими панелями и их AMI-пользователями."""

    def __init__(self):
        self.sock = None

    def connect(self):
        self.close()
        self.sock = socket.create_connection((config.AMI_HOST, config.AMI_PORT), timeout=8)
        self.sock.settimeout(8)
        # приветственная строка от Asterisk
        self._read_line()
        self._login()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _read_line(self) -> bytes:
        buf = b""
        while not buf.endswith(b"\r\n"):
            chunk = self.sock.recv(1)
            if not chunk:
                break
            buf += chunk
        return buf

    def _read_block(self) -> str:
        """Читает один блок (Response или Event) до пустой строки. Пропускает
        несвязанные Event-блоки, которые Asterisk может прислать вперемешку
        (например FullyBooted сразу после логина) — ждёт именно то, что
        просили через send_action()."""
        buf = b""
        while _TERMINATOR not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        raw, _, rest = buf.partition(_TERMINATOR)
        self._pending = rest
        return raw.decode("utf-8", errors="replace")

    def _login(self):
        action = (
            f"Action: Login\r\n"
            f"Username: {config.AMI_USER}\r\n"
            f"Secret: {config.AMI_SECRET}\r\n"
            f"Events: off\r\n\r\n"
        )
        self.sock.sendall(action.encode("utf-8"))
        resp = self._read_block()
        if "Success" not in resp:
            raise AmiError(f"AMI login failed: {resp}")

    def send_action(self, action_text: str) -> str:
        """Отправляет action, возвращает сырой текст первого блока-ответа."""
        self.sock.sendall(action_text.encode("utf-8"))
        return self._read_block()

    def send_command(self, command: str) -> str:
        """Action: Command — используется для 'pjsip show contacts' и
        аналогичных CLI-команд, доступных через AMI."""
        action = f"Action: Command\r\nCommand: {command}\r\n\r\n"
        return self.send_action(action)


def connect_with_retry(max_attempts: int = 3, delay: float = 2.0) -> AmiConnection:
    last_exc = None
    for attempt in range(max_attempts):
        try:
            conn = AmiConnection()
            conn.connect()
            return conn
        except (OSError, AmiError) as exc:
            last_exc = exc
            time.sleep(delay)
    raise AmiError(f"Не удалось подключиться к AMI после {max_attempts} попыток: {last_exc}")
