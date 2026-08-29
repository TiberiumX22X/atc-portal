import os
from flask import Response, abort
import config

CHUNK_SIZE = 64 * 1024


def _within_recordings_dir(path: str) -> bool:
    """Проверяет, что путь физически лежит внутри RECORDINGS_DIR (с учётом
    симлинков — сравнение идёт по realpath). Защита от path traversal:
    без этой проверки recordingfile из URL мог бы указывать на любой файл
    сервера (например /etc/passwd) — сервис работает от root в systemd,
    поэтому это было бы не только чтением произвольных файлов, но и
    возможностью их удаления через кнопку 🗑."""
    base = os.path.realpath(config.RECORDINGS_DIR)
    real = os.path.realpath(path)
    return real == base or real.startswith(base + os.sep)


def resolve_recording_path(recordingfile: str) -> str:
    """recordingfile в CDR обычно содержит либо полный путь, либо только
    имя файла (тогда реальный путь — RECORDINGS_DIR/YYYY/MM/DD/имя). В любом
    случае результат обязан лежать внутри RECORDINGS_DIR — см.
    _within_recordings_dir."""
    if not recordingfile:
        return ""

    # 1) значение уже абсолютный путь (так Asterisk обычно и хранит его в
    # CDR) — разрешаем ТОЛЬКО если он физически внутри RECORDINGS_DIR
    if os.path.isabs(recordingfile):
        if _within_recordings_dir(recordingfile) and os.path.isfile(recordingfile):
            return os.path.realpath(recordingfile)
        return ""

    # 2) относительный путь/имя файла — собираем внутри RECORDINGS_DIR и
    # снова проверяем итоговый путь (на случай "../../etc/passwd" в имени)
    candidate = os.path.join(config.RECORDINGS_DIR, recordingfile)
    if _within_recordings_dir(candidate) and os.path.isfile(candidate):
        return os.path.realpath(candidate)

    # 3) обход дерева как запасной вариант — сравниваем только basename,
    # найденный файл всё равно гарантированно внутри RECORDINGS_DIR, т.к.
    # os.walk начинается от него
    safe_name = os.path.basename(recordingfile)
    for root, _, files in os.walk(config.RECORDINGS_DIR):
        if safe_name in files:
            found = os.path.join(root, safe_name)
            if _within_recordings_dir(found):
                return os.path.realpath(found)

    return ""


def _stream_file_response(path: str, download_name: str, as_attachment: bool):
    file_size = os.path.getsize(path)

    def generate():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    ext = os.path.splitext(path)[1].lower()
    content_type = "audio/wav" if ext == ".wav" else "audio/gsm" if ext == ".gsm" else "application/octet-stream"

    headers = {
        "Content-Length": str(file_size),
        "Content-Type": content_type,
    }
    disposition = "attachment" if as_attachment else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="{download_name}"'

    return Response(generate(), headers=headers)


def serve_recording(recordingfile: str, as_attachment: bool):
    path = resolve_recording_path(recordingfile)
    if not path:
        abort(404)
    download_name = os.path.basename(path)
    return _stream_file_response(path, download_name, as_attachment)


def delete_recording(recordingfile: str) -> bool:
    """Удаляет файл записи с диска. Строку в asteriskcdrdb.cdr панель не
    трогает — подключение к этой БД read-only по дизайну (см. cdr_query.py),
    после удаления файла колонка "Запись" в журнале просто перестанет
    находить файл и покажет прочерк."""
    path = resolve_recording_path(recordingfile)
    if not path:
        return False
    try:
        os.remove(path)
    except PermissionError as exc:
        # Файлы записей обычно принадлежат пользователю asterisk, панель
        # работает от www-data — без включения www-data в группу asterisk
        # (это делает установщик) удаление упадёт именно так. Поднимаем
        # понятную ошибку вместо необработанного исключения → 500 без
        # единого слова о причине.
        raise RuntimeError(
            "Нет прав на удаление файла записи (обычно бывает, если "
            "www-data не входит в группу владельца файлов записи — "
            "проверьте `groups www-data` на сервере)"
        ) from exc
    return True
