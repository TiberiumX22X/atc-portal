"""Резервное копирование конфигурации проекта одной кнопкой: базы данных
пользователей/панелей + конфиги nginx и Asterisk AMI + дамп конфигурации
FreePBX (номера, транки, маршруты — база asterisk, БЕЗ asteriskcdrdb:
история звонков может быть гигабайтами, ей место в отдельном процессе
бэкапа на уровне хранилища, не в кнопке в вебе).

Репликация MariaDB (см. HA-кластер) защищает от падения одного сервера,
но НЕ защищает от человеческой ошибки или порчи данных — такая ошибка
реплицируется на второй сервер так же мгновенно, как и любое другое
изменение. Дамп в этом архиве — это точка отката во времени, отдельная
задача от отказоустойчивости.
"""
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime

# (путь на диске, имя внутри архива) — берём только то, что реально нужно
# для восстановления конфигурации, не сами данные звонков.
BACKUP_PATHS = [
    ("/opt/sso-auth/sso.db", "portal/sso-auth-users.db"),
    ("/opt/alert-panel/alert_panel.db", "alert-panel/alert_panel.db"),
    ("/opt/asterisk-panel/panel.db", "confbridge-panel/panel.db"),
    ("/opt/phone-provisioning/devices.db", "phone-provisioning/devices.db"),
    ("/etc/nginx/conf.d/portal.conf", "nginx/portal.conf"),
    ("/etc/asterisk/manager_custom.conf", "asterisk/manager_custom.conf"),
]

# Имя дампа конфигурации FreePBX внутри архива. Генерируется динамически
# (не статический файл на диске, как остальные пути выше) — отдельная
# логика сборки и восстановления, см. ниже.
DB_DUMP_ARCNAME = "mariadb/freepbx-config.sql"

# Какой сервис перезапустить после восстановления файла с данным именем
# внутри архива — чтобы служба реально подхватила восстановленные данные,
# а не продолжала работать со старым состоянием в памяти.
_RESTART_AFTER = {
    "portal/sso-auth-users.db": "sso-auth",
    "alert-panel/alert_panel.db": "alert-panel",
    "confbridge-panel/panel.db": "asterisk-panel",
    "phone-provisioning/devices.db": "phone-provisioning",
    "nginx/portal.conf": "nginx",
    # manager_custom.conf — просто перечитываем через AMI, не перезапуская Asterisk целиком
}

_ALLOWED_ARCNAMES = {arcname for _, arcname in BACKUP_PATHS}
_ARCNAME_TO_REAL_PATH = {arcname: real for real, arcname in BACKUP_PATHS}


def _read_freepbx_db_password():
    try:
        with open("/etc/freepbx.conf", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r"AMPDBPASS[\"']\]\s*=\s*[\"']([^\"']+)[\"']", content)
    return m.group(1) if m else None


def build_backup_archive():
    """Создаёт tar.gz во временном файле и возвращает его путь + имя.
    Вызывающий код (Flask-роут) отвечает за удаление файла после отправки."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_name = f"atc-portal-backup-{timestamp}.tar.gz"
    tmp_path = os.path.join(tempfile.gettempdir(), archive_name)

    included, skipped = [], []
    with tarfile.open(tmp_path, "w:gz") as tar:
        for src_path, arcname in BACKUP_PATHS:
            if os.path.isfile(src_path):
                tar.add(src_path, arcname=arcname)
                included.append(arcname)
            else:
                skipped.append(src_path)

        # Дамп конфигурации FreePBX (номера, транки, маршруты и т.п.) —
        # без asteriskcdrdb (история звонков) и без записей разговоров.
        db_pass = _read_freepbx_db_password()
        if db_pass:
            dump_fd, dump_path = tempfile.mkstemp(suffix=".sql")
            os.close(dump_fd)
            try:
                proc = subprocess.run(
                    ["mysqldump", "-ufreepbxuser", f"-p{db_pass}",
                     "--single-transaction", "--databases", "asterisk"],
                    stdout=open(dump_path, "wb"), stderr=subprocess.PIPE, timeout=120,
                )
                if proc.returncode == 0 and os.path.getsize(dump_path) > 0:
                    tar.add(dump_path, arcname=DB_DUMP_ARCNAME)
                    included.append(DB_DUMP_ARCNAME)
                else:
                    skipped.append("mariadb (mysqldump завершился с ошибкой)")
            finally:
                os.remove(dump_path)
        else:
            skipped.append("mariadb (не удалось прочитать пароль из /etc/freepbx.conf)")

    return tmp_path, archive_name, included, skipped


class RestoreError(Exception):
    pass


def restore_backup_archive(uploaded_file_path):
    """Восстанавливает файлы из архива, сделанного build_backup_archive().

    Дамп MariaDB (DB_DUMP_ARCNAME) намеренно НЕ восстанавливается здесь
    автоматически — это база FreePBX, поверх которой в HA-кластере может
    идти активная репликация; слепой mysql < dump.sql в этом месте рискует
    развалить репликацию или затереть данные, накопленные после снятия
    архива. Дамп извлекается на диск для ручного, осознанного применения
    (см. extract_db_dump ниже), restore_backup_archive восстанавливает
    только файловую часть (базы панелей, конфиги).

    Безопасность — прежде чем что-либо трогать на диске:
    1. Архив должен открыться как валидный tar.gz.
    2. КАЖДОЕ имя внутри архива обязано ровно совпадать с одним из уже
       известных нам имён (_ALLOWED_ARCNAMES) — никаких posix-путей с
       '..' или произвольных мест на диске. Один неизвестный/подозрительный
       элемент — вся операция отклоняется целиком, ничего не восстановится
       частично.
    3. Только после проверки ВСЕГО архива — реальная запись на диск.

    Возвращает (restored: list[str], services_restarted: list[str], db_dump_present: bool).
    """
    try:
        tar = tarfile.open(uploaded_file_path, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise RestoreError(f"Не удалось открыть архив: {exc}")

    with tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        if not members:
            raise RestoreError("В архиве нет файлов")

        db_dump_present = False
        for m in members:
            # tarfile сам по себе уязвим к path traversal через имена вида
            # "../../etc/passwd" при неосторожном extractall — здесь этого
            # не произойдёт в принципе, т.к. мы проверяем ТОЧНОЕ совпадение
            # имени со списком известных, и ничего кроме них не примем.
            if m.name == DB_DUMP_ARCNAME:
                db_dump_present = True
                continue
            if m.name not in _ALLOWED_ARCNAMES:
                raise RestoreError(
                    f"Архив содержит неизвестный/неожиданный файл «{m.name}» — "
                    f"это не похоже на резервную копию, сделанную этой панелью. Отменено."
                )

        # Всё прошло проверку — теперь реально восстанавливаем файловую часть.
        restored = []
        services_to_restart = set()
        for m in members:
            if m.name == DB_DUMP_ARCNAME:
                continue  # см. extract_db_dump — восстанавливается отдельным осознанным шагом
            real_path = _ARCNAME_TO_REAL_PATH[m.name]
            os.makedirs(os.path.dirname(real_path), exist_ok=True)
            extracted = tar.extractfile(m)
            if extracted is None:
                continue
            # Бэкап текущего файла на всякий случай, прежде чем перезаписать.
            if os.path.exists(real_path):
                shutil.copy2(real_path, real_path + ".before-restore")
            with open(real_path, "wb") as f:
                shutil.copyfileobj(extracted, f)
            restored.append(m.name)
            if m.name in _RESTART_AFTER:
                services_to_restart.add(_RESTART_AFTER[m.name])

    manager_conf_restored = "asterisk/manager_custom.conf" in restored
    if manager_conf_restored:
        subprocess.run(["asterisk", "-rx", "manager reload"], capture_output=True, timeout=10)

    restarted = []
    for service in services_to_restart:
        proc = subprocess.run(["systemctl", "restart", service], capture_output=True, timeout=20)
        if proc.returncode == 0:
            restarted.append(service)

    return restored, restarted, db_dump_present


def extract_db_dump(uploaded_file_path, dest_path="/root/atc-portal-restore-freepbx-config.sql"):
    """Достаёт дамп FreePBX (DB_DUMP_ARCNAME) из архива на диск, НЕ применяя
    его к базе — применение осознанное и ручное, см. предупреждение в шаблоне
    страницы восстановления. В HA-кластере применять только на текущем
    MASTER (там, где VIP) — реплика подхватит изменения сама через обычную
    репликацию; применение дампа напрямую на backup при активной
    репликации может её разорвать."""
    try:
        tar = tarfile.open(uploaded_file_path, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise RestoreError(f"Не удалось открыть архив: {exc}")
    with tar:
        try:
            member = tar.getmember(DB_DUMP_ARCNAME)
        except KeyError:
            raise RestoreError("В этом архиве нет дампа базы данных FreePBX.")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise RestoreError("Не удалось прочитать дамп из архива.")
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(extracted, f)
    os.chmod(dest_path, 0o600)
    return dest_path
