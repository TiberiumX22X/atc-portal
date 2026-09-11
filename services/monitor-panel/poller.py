import re
import threading
import time
import logging

import config
from ami_client import connect_with_retry, AmiError
from freepbx_names import fetch_extension_names

logger = logging.getLogger("monitor-panel.poller")

_lock = threading.Lock()
_state = {
    "extensions": {},       # {number: {"status": "Avail"/"Unavail", "ip": "", "port": "", "type": "PJSIP"}}
    "names": {},            # {number: "Имя"}
    "call_states": {},      # {number: "in_call"/"ringing"} — отсутствие ключа = не в разговоре
    "concurrent_calls": 0,
    "last_update": None,
    "last_error": None,
}

_EXT_RE = re.compile(config.EXTENSION_REGEX)

# "  Contact:  7000/sip:7000@192.168.0.5:52777    9a4db60a36 Avail   3.542"
# Реальный вывод Asterisk имеет отступ перед "Contact:" — учитываем его.
_CONTACT_RE = re.compile(
    r"^\s*Contact:\s+(?P<aor>[^/\s]+)/\S*@(?P<ip>[\d.]+):(?P<port>\d+)\S*\s+\S+\s+(?P<status>\S+)",
    re.MULTILINE,
)

_ACTIVE_CALLS_RE = re.compile(r"(\d+)\s+active call")
_ACTIVE_CHANNELS_RE = re.compile(r"(\d+)\s+active channel")

# "core show channels concise" — построчный, машинно-читаемый формат:
# Channel!Context!Exten!Priority!State!Application!Data!CallerID!...
# Нам нужны только первые два поля: Channel (чтобы вытащить номер
# добавочного из "PJSIP/7001-00000123") и State.
_CONCISE_CHANNEL_RE = re.compile(
    r"^(?P<channel>[^!\r\n]+)!(?P<context>[^!\r\n]*)!(?P<exten>[^!\r\n]*)!"
    r"(?P<priority>[^!\r\n]*)!(?P<state>[^!\r\n]*)!",
    re.MULTILINE,
)
_CHANNEL_EXT_RE = re.compile(r"^PJSIP/(\d+)-")

# Состояния канала Asterisk -> что реально показать в мониторе. Порядок
# важен: если у номера сразу два канала (звонок + разговор одновременно,
# редко, но бывает при переводе/конференции) — берём более "активное".
_CALL_STATE_PRIORITY = {"up": 2, "ring": 1, "ringing": 1, "dialing": 1, "pre-ring": 1}
_CALL_STATE_LABELS = {"up": "in_call", "ring": "ringing", "ringing": "ringing",
                       "dialing": "ringing", "pre-ring": "ringing"}


def _strip_output_prefix(raw: str) -> str:
    """AMI Action: Command оборачивает КАЖДУЮ строку CLI-вывода префиксом
    'Output: ' — это формат самого протокола менеджера Asterisk, не наша
    команда его так не просила. Без снятия этого префикса ни одна строка
    вида 'Output: Contact: 7000/...' не совпадёт с regex, ожидающим
    'Contact:' в начале строки. Если префикса нет (на случай другого
    формата ответа) — regex просто ничего не меняет, безопасно."""
    return re.sub(r"(?m)^Output:\s?", "", raw)


def _parse_contacts(raw: str) -> dict:
    raw = _strip_output_prefix(raw)
    result = {}
    for m in _CONTACT_RE.finditer(raw):
        aor = m.group("aor")
        if not _EXT_RE.match(aor):
            continue
        status = m.group("status")
        result[aor] = {
            "status": "Avail" if status.lower().startswith("avail") else "Unavail",
            "ip": m.group("ip"),
            "port": m.group("port"),
            "type": "PJSIP",
        }
    return result


def _parse_concurrent_calls(raw: str) -> int:
    """Считаем именно людей/устройств в разговоре (каналы), а не пары —
    2 человека на линии должны показывать "2", не "1". 'N active channels'
    и есть это число: каждый активный канал — одна из сторон разговора."""
    raw = _strip_output_prefix(raw)
    m = _ACTIVE_CHANNELS_RE.search(raw)
    if m:
        return int(m.group(1))
    m = _ACTIVE_CALLS_RE.search(raw)
    if m:
        # запасной вариант, если в выводе почему-то нет строки про каналы —
        # переводим число звонков обратно в число участников (× 2)
        return int(m.group(1)) * 2
    return 0


def _parse_call_states(raw: str) -> dict:
    """По 'core show channels concise' определяет для каждого добавочного,
    разговаривает ли он сейчас или у него идёт звонок (набор/дозвон).
    Номер добавочного берём из имени канала (PJSIP/7001-xxx) — это
    надёжнее, чем поле Exten, которое показывает НАБИРАЕМЫЙ номер, а не
    владельца телефона."""
    raw = _strip_output_prefix(raw)
    result = {}  # {number: "in_call"/"ringing"}
    best_priority = {}
    for m in _CONCISE_CHANNEL_RE.finditer(raw):
        chan_match = _CHANNEL_EXT_RE.match(m.group("channel"))
        if not chan_match:
            continue
        number = chan_match.group(1)
        if not _EXT_RE.match(number):
            continue
        state = m.group("state").strip().lower()
        priority = _CALL_STATE_PRIORITY.get(state)
        if priority is None:
            continue
        if priority > best_priority.get(number, -1):
            best_priority[number] = priority
            result[number] = _CALL_STATE_LABELS[state]
    return result


def _poll_once(conn):
    contacts_raw = conn.send_command("pjsip show contacts")
    contacts = _parse_contacts(contacts_raw)

    channels_raw = conn.send_command("core show channels")
    concurrent = _parse_concurrent_calls(channels_raw)

    concise_raw = conn.send_command("core show channels concise")
    call_states = _parse_call_states(concise_raw)

    names = fetch_extension_names()

    with _lock:
        _state["extensions"] = contacts
        _state["names"] = names
        _state["call_states"] = call_states
        _state["concurrent_calls"] = concurrent
        _state["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _state["last_error"] = None


def _poll_loop():
    conn = None
    while True:
        try:
            if conn is None:
                conn = connect_with_retry()
            _poll_once(conn)
        except (AmiError, OSError, ConnectionError) as exc:
            logger.warning("Ошибка опроса AMI: %s", exc)
            with _lock:
                _state["last_error"] = str(exc)
            if conn:
                conn.close()
            conn = None
        except Exception as exc:  # защита от неожиданных ошибок парсинга и т.п.
            logger.exception("Неожиданная ошибка в цикле опроса")
            with _lock:
                _state["last_error"] = str(exc)
        time.sleep(config.POLL_INTERVAL_SECONDS)


def start_background_poller():
    """Запускать РОВНО ОДИН раз на процесс — при нескольких gunicorn workers
    каждый завёл бы свой поток опроса AMI. Поэтому install.sh поднимает этот
    сервис с --workers 1 (см. комментарий в install.sh)."""
    thread = threading.Thread(target=_poll_loop, daemon=True)
    thread.start()


def get_snapshot() -> dict:
    with _lock:
        return {
            "extensions": dict(_state["extensions"]),
            "names": dict(_state["names"]),
            "call_states": dict(_state["call_states"]),
            "concurrent_calls": _state["concurrent_calls"],
            "last_update": _state["last_update"],
            "last_error": _state["last_error"],
        }
