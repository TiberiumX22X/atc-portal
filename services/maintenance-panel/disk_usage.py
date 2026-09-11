"""Место, занятое записями звонков (CDR) — /var/spool/asterisk/monitor
(общий для Asterisk путь, его же читает CDR-панель).

Звуки оповещений сюда сознательно НЕ включены: это не архив, который
можно чистить по возрасту — там живут действующие звуковые сообщения для
обзвона, автоматическое удаление по дате их бы уничтожило."""
import os
import shutil
import subprocess
from datetime import datetime

TRACKED_DIRS = {
    "call_recordings": {
        "label": "Записи разговоров (CDR)",
        "path": "/var/spool/asterisk/monitor",
    },
}


def _dir_size_bytes(path):
    """Реальный размер содержимого папки — через `du`, а не ручной обход
    Python (быстрее и надёжнее на директориях с тысячами файлов)."""
    try:
        proc = subprocess.run(["du", "-sb", path], capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return int(proc.stdout.split()[0])
    except (subprocess.TimeoutExpired, ValueError, IndexError, FileNotFoundError):
        pass
    return None


def _file_count_and_oldest(path):
    count = 0
    oldest_ts = None
    try:
        for root, _, files in os.walk(path):
            for name in files:
                count += 1
                try:
                    ts = os.path.getmtime(os.path.join(root, name))
                    if oldest_ts is None or ts < oldest_ts:
                        oldest_ts = ts
                except OSError:
                    continue
    except OSError:
        pass
    oldest = datetime.fromtimestamp(oldest_ts).strftime("%d.%m.%Y") if oldest_ts else None
    return count, oldest


def human_size(num_bytes):
    if num_bytes is None:
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} ПБ"


def get_stats():
    stats = []
    for key, meta in TRACKED_DIRS.items():
        path = meta["path"]
        if not os.path.isdir(path):
            stats.append({"key": key, "label": meta["label"], "path": path, "exists": False})
            continue
        size = _dir_size_bytes(path)
        count, oldest = _file_count_and_oldest(path)
        stats.append({
            "key": key, "label": meta["label"], "path": path, "exists": True,
            "size_bytes": size, "size_human": human_size(size),
            "file_count": count, "oldest_file": oldest,
        })

    # Место на самом разделе диска, где лежат записи — предупреждает, если
    # диск в целом переполняется, а не только конкретная папка растёт.
    disk = None
    try:
        total, used, free = shutil.disk_usage("/var/spool/asterisk")
        disk = {
            "total_human": human_size(total),
            "used_human": human_size(used),
            "free_human": human_size(free),
            "used_percent": round(used / total * 100, 1),
        }
    except OSError:
        pass

    return stats, disk


def delete_older_than(dir_key, days):
    """Удаляет файлы старше N дней в указанной отслеживаемой папке.
    Возвращает (сколько удалено, сколько байт освобождено)."""
    if dir_key not in TRACKED_DIRS:
        raise ValueError("Неизвестная папка")
    path = TRACKED_DIRS[dir_key]["path"]
    if not os.path.isdir(path):
        return 0, 0

    cutoff = datetime.now().timestamp() - (days * 86400)
    deleted_count = 0
    freed_bytes = 0
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
                if st.st_mtime < cutoff:
                    freed_bytes += st.st_size
                    os.remove(full)
                    deleted_count += 1
            except OSError:
                continue
    return deleted_count, freed_bytes
