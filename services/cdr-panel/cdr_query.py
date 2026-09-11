import re
from db import get_cdr_connection
from recordings import resolve_recording_path
import config

EXT_RE = re.compile(config.EXTENSION_REGEX)

# Служебные номера панелей проекта — их звонки не показываем в журнале CDR,
# т.к. эта информация уже отображается в собственных панелях этих сервисов
# (конференц-панель, панель оповещения).
SERVICE_NUMBERS = {"8500", "6000"}  # 8500 = панель оповещения, 6000 = конференция

DISPOSITION_LABELS = {
    "ANSWERED": "Ответил",
    "NO ANSWER": "Нет ответа",
    "BUSY": "Занято",
    "FAILED": "Ошибка",
    "CONGESTION": "Перегрузка",
}

CALL_TYPE_LABELS = {
    "internal": "Внутренний",
    "outgoing_city": "Город",
    "outgoing_intercity": "Межгород",
    "incoming": "Входящий",
    "other": "Прочее",
}

# Номера, начинающиеся с этих символов, считаются межгородом/мобильной связью
# (GSM), согласно реальным Dial Patterns маршрута GorodMezgorod: 8[2348]...,
# 8XXXXXXXXXX, 810., 858., 89., +79XXXXXXXXX — все они начинаются с "8" или "+".
_INTERCITY_PREFIX_RE = re.compile(r"^(8|\+)")


def _endpoint_name(channel: str) -> str:
    """Из 'PJSIP/7002-00000011' достаёт '7002'. Из 'PJSIP/ToGateway-0000000a'
    достаёт 'ToGateway'. Пустая строка, если канал не задан."""
    if not channel:
        return ""
    m = re.match(r"^[A-Za-z]+/([^-]+)-", channel)
    if m:
        return m.group(1)
    if "/" in channel:
        return channel.split("/", 1)[1]
    return channel


def classify_call(src: str, dst: str) -> str:
    src_is_ext = bool(EXT_RE.match(src or ""))
    dst_is_ext = bool(EXT_RE.match(dst or ""))
    if src_is_ext and dst_is_ext:
        return "internal"
    if src_is_ext and not dst_is_ext:
        return "outgoing_intercity" if _INTERCITY_PREFIX_RE.match(dst or "") else "outgoing_city"
    if dst_is_ext and not src_is_ext:
        return "incoming"
    return "other"


def line_name(row: dict) -> str:
    for chan in (row.get("dstchannel"), row.get("channel")):
        name = _endpoint_name(chan)
        if name and not EXT_RE.match(name):
            return name
    return ""


def list_available_lines() -> list:
    """Собирает уникальные имена транков из последних записей — для
    выпадающего списка фильтра 'Линия'. Пересчитывается заново при каждом
    открытии страницы (не кешируется) — специальной синхронизации не
    требуется, новый транк появится сам после первого реального звонка
    через него."""
    conn = get_cdr_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT channel, dstchannel
                FROM cdr
                WHERE calldate >= NOW() - INTERVAL 90 DAY
                LIMIT 5000
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    names = set()
    for row in rows:
        for chan in (row.get("channel"), row.get("dstchannel")):
            name = _endpoint_name(chan)
            if name and not EXT_RE.match(name):
                names.add(name)
    return sorted(names)


def search_cdr(filters: dict, page: int = 1, page_size: int = None):
    """
    filters — словарь:
        date_from, date_to (строки 'YYYY-MM-DD HH:MM' или пусто)
        src, dst (подстрока)
        min_duration (int или None)
        disposition ('ANSWERED'/'NO ANSWER'/'BUSY'/'FAILED'/'' — все)
        line (имя транка или '' — все)
        call_type ('internal'/'outgoing_city'/'outgoing_intercity'/'incoming'/'other'/'' — все)
    Возвращает (rows, total_count).
    """
    page_size = page_size or config.PAGE_SIZE
    offset = max(page - 1, 0) * page_size

    where = []
    params = []

    # Единственные безусловные исключения — не зависят от фильтров формы:
    # 1) служебные номера панелей (оповещение/конференция)
    if SERVICE_NUMBERS:
        placeholders = ",".join(["%s"] * len(SERVICE_NUMBERS))
        where.append(f"src NOT IN ({placeholders}) AND dst NOT IN ({placeholders})")
        params.extend(SERVICE_NUMBERS)
        params.extend(SERVICE_NUMBERS)
    # 2) технические записи с нечисловым dst (например 's' — служебный вход
    # dialplan для системных событий, не реальный звонок на номер).
    # Регулярка передаётся параметром, а не вставляется в текст SQL — иначе
    # MySQL съедает одиночный backslash при разборе строкового литерала.
    where.append("dst REGEXP %s")
    params.append(r"^\+?[0-9]+$")

    if filters.get("date_from"):
        where.append("calldate >= %s")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        where.append("calldate <= %s")
        params.append(filters["date_to"])
    if filters.get("src"):
        where.append("src LIKE %s")
        params.append(f"%{filters['src']}%")
    if filters.get("dst"):
        where.append("dst LIKE %s")
        params.append(f"%{filters['dst']}%")
    if filters.get("min_duration"):
        where.append("billsec >= %s")
        params.append(int(filters["min_duration"]))
    if filters.get("disposition"):
        where.append("disposition = %s")
        params.append(filters["disposition"])
    if filters.get("line"):
        where.append("(channel LIKE %s OR dstchannel LIKE %s)")
        like = f"%/{filters['line']}-%"
        params.extend([like, like])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_cdr_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM cdr {where_sql}", params)
            total = cur.fetchone()["cnt"]

            sql = f"""
                SELECT calldate, clid, src, dst, duration, billsec,
                       disposition, channel, dstchannel, recordingfile, uniqueid
                FROM cdr
                {where_sql}
                ORDER BY calldate DESC
                LIMIT %s OFFSET %s
            """
            cur.execute(sql, params + [page_size, offset])
            rows = cur.fetchall()
    finally:
        conn.close()

    call_type_filter = filters.get("call_type") or ""
    result = []
    for row in rows:
        ctype = classify_call(row["src"], row["dst"])
        if call_type_filter and ctype != call_type_filter:
            continue
        row["call_type"] = ctype
        row["call_type_label"] = CALL_TYPE_LABELS.get(ctype, ctype)
        row["disposition_label"] = DISPOSITION_LABELS.get(
            row["disposition"], row["disposition"]
        )
        row["line"] = line_name(row)
        # Проверяем реальное наличие файла на диске, а не только заполненность
        # поля recordingfile в CDR — панель может удалить сам файл (см. кнопку
        # 🗑 у администратора), но не имеет прав менять строку в asteriskcdrdb
        # (подключение read-only), поэтому поле в базе остаётся как было.
        row["recording_exists"] = bool(row["recordingfile"]) and bool(
            resolve_recording_path(row["recordingfile"])
        )
        result.append(row)

    # Примечание: фильтр call_type применяется постфактум к уже выбранной
    # странице (кроме исключения служебных номеров, которое всегда в SQL).
    # При активном фильтре по типу вызова это может дать неполную страницу —
    # приемлемо для объёма данных этого проекта.
    return result, total
