import config
from poller import get_snapshot


SORTABLE_COLUMNS = {"number", "name", "status", "ip", "type"}


def build_extensions_list(query: str = "", sort: str = "number", direction: str = "asc"):
    """Сливает полный список добавочных (имена из FreePBX) со свежим
    снапшотом AMI (статус/IP:порт). Добавочные без активного контакта
    показываются как офлайн, без IP."""
    snapshot = get_snapshot()
    names = snapshot["names"]
    contacts = snapshot["extensions"]
    call_states = snapshot["call_states"]

    numbers = set(names.keys()) | set(contacts.keys())

    rows = []
    for number in numbers:
        contact = contacts.get(number)
        rows.append(
            {
                "number": number,
                "name": names.get(number, ""),
                "status": contact["status"] if contact else "Offline",
                "ip": contact["ip"] if contact else "",
                "port": contact["port"] if contact else "",
                "type": contact["type"] if contact else "",
                "call_state": call_states.get(number),  # None / "ringing" / "in_call"
            }
        )

    if query:
        q = query.strip().lower()
        rows = [
            r
            for r in rows
            if q in r["number"].lower()
            or q in (r["name"] or "").lower()
            or q in (r["ip"] or "")
            or q in (r["port"] or "")
        ]

    if sort not in SORTABLE_COLUMNS:
        sort = "number"
    reverse = direction == "desc"
    # Числовые added добавочные сортируем численно, если это возможно —
    # иначе "10" оказывался бы раньше "9" при обычной строковой сортировке.
    if sort == "number":
        rows.sort(key=lambda r: (len(r["number"]), r["number"]), reverse=reverse)
    else:
        rows.sort(key=lambda r: (r[sort] or "").lower(), reverse=reverse)
    return rows, snapshot["last_update"], snapshot["last_error"]


def paginate(rows: list, page: int, page_size: int = None):
    page_size = page_size or config.PAGE_SIZE
    total = len(rows)
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return rows[start:start + page_size], total, page, total_pages
