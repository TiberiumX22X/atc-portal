from flask import Flask, render_template, request, redirect, url_for, flash, g, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
import os

import config
import auth
import services
import ami_client
import ami_secrets
import disk_usage
import backup
import ha_status
import ha_setup
import phone_firewall

app = Flask(__name__)

# Панель работает за nginx под префиксом /maintenance/ (см. nginx/portal.conf).
app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)
app.secret_key = config.get_or_create_secret_key()
app.config["SESSION_COOKIE_NAME"] = "maintenance_session"


@app.before_request
def _load_remote_user():
    auth.load_remote_user()


@app.context_processor
def inject_csrf():
    return {"csrf_token": auth.get_csrf_token()}


@app.after_request
def _no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Дашборд — статус всех сервисов + краткая сводка по Asterisk
# ---------------------------------------------------------------------------

@app.route("/")
@auth.admin_required
def dashboard():
    statuses = services.get_all_status()
    core = ami_client.core_status()
    return render_template("dashboard.html", statuses=statuses, core=core)


@app.route("/services/<unit>/restart", methods=["POST"])
@auth.admin_required
def restart_service(unit):
    auth.check_csrf()
    if unit not in services.KNOWN_UNITS:
        flash("Неизвестный сервис", "error")
        return redirect(url_for("dashboard"))
    if unit == "maintenance-panel":
        flash("Нельзя перезапустить панель обслуживания саму себя из её же интерфейса — сделайте это через SSH.", "error")
        return redirect(url_for("dashboard"))
    ok, message = services.restart(unit)
    if ok:
        flash(f"Сервис «{services.KNOWN_UNITS[unit]}» перезапущен", "success")
    else:
        flash(f"Не удалось перезапустить «{services.KNOWN_UNITS[unit]}»: {message}", "error")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Логи
# ---------------------------------------------------------------------------

@app.route("/logs")
@auth.admin_required
def logs():
    unit = request.args.get("unit", "sso-auth")
    lines = request.args.get("lines", "100")
    if unit not in services.KNOWN_UNITS:
        unit = "sso-auth"
    log_text = services.tail_log(unit, lines)
    return render_template(
        "logs.html", units=services.KNOWN_UNITS, selected_unit=unit, log_text=log_text, lines=lines
    )


# ---------------------------------------------------------------------------
# AMI-консоль
# ---------------------------------------------------------------------------

# Небольшой набор быстрых команд — чтобы не печатать вручную самое частое.
QUICK_COMMANDS = [
    "core show channels",
    "core show channels verbose",
    "pjsip show endpoints",
    "pjsip show registrations",
    "core show version",
    "sip show peers",
    "core show uptime",
]


@app.route("/ami", methods=["GET", "POST"])
@auth.admin_required
def ami_console():
    result = None
    command = ""
    if request.method == "POST":
        auth.check_csrf()
        command = request.form.get("command", "").strip()
        if not command:
            flash("Введите команду", "error")
        else:
            try:
                action = f"Action: Command\r\nCommand: {command}\r\n\r\n"
                result = ami_client.send_action(action)
            except ami_client.AMIError as exc:
                flash(str(exc), "error")
    return render_template(
        "ami_console.html", result=result, command=command, quick_commands=QUICK_COMMANDS
    )


# ---------------------------------------------------------------------------
# AMI-секреты — просмотр и ротация
# ---------------------------------------------------------------------------

@app.route("/ami-secrets")
@auth.admin_required
def ami_secrets_page():
    users = ami_secrets.list_ami_users()
    return render_template("ami_secrets.html", users=users)


@app.route("/ami-secrets/<username>/rotate", methods=["POST"])
@auth.admin_required
def rotate_ami_secret(username):
    auth.check_csrf()
    try:
        new_secret, self_restart = ami_secrets.rotate_secret(username)
        if self_restart:
            flash(
                "Секрет обновлён. Эта панель сейчас перезапустится сама — "
                "страница может на секунду стать недоступна, обновите её вручную.",
                "success",
            )
        else:
            flash(f"Секрет пользователя «{username}» обновлён и распространён, служба перезапущена", "success")
    except (RuntimeError, ValueError) as exc:
        flash(f"Не удалось выполнить ротацию: {exc}", "error")
    return redirect(url_for("ami_secrets_page"))


# ---------------------------------------------------------------------------
# Место на диске: записи разговоров и звуки оповещений
# ---------------------------------------------------------------------------

@app.route("/disk-usage")
@auth.admin_required
def disk_usage_page():
    stats, disk = disk_usage.get_stats()
    return render_template("disk_usage.html", stats=stats, disk=disk)


@app.route("/disk-usage/<dir_key>/cleanup", methods=["POST"])
@auth.admin_required
def disk_cleanup(dir_key):
    auth.check_csrf()
    try:
        days = int(request.form.get("days", "90"))
    except ValueError:
        days = 90
    days = max(7, days)  # не даём случайно снести всё за последнюю неделю одним кликом
    try:
        deleted, freed = disk_usage.delete_older_than(dir_key, days)
        flash(f"Удалено файлов: {deleted}, освобождено: {disk_usage.human_size(freed)}", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("disk_usage_page"))


# ---------------------------------------------------------------------------
# Резервное копирование
# ---------------------------------------------------------------------------

@app.route("/backup")
@auth.admin_required
def backup_download():
    tmp_path, archive_name, included, skipped = backup.build_backup_archive()
    if skipped:
        flash(
            "В архив не попали (файл не найден на сервере): " + ", ".join(skipped),
            "error",
        )
    response = send_file(tmp_path, as_attachment=True, download_name=archive_name)
    response.call_on_close(lambda: os.path.exists(tmp_path) and os.remove(tmp_path))
    return response


@app.route("/backup/restore", methods=["POST"])
@auth.admin_required
def backup_restore():
    auth.check_csrf()
    uploaded = request.files.get("backup_file")
    if not uploaded or not uploaded.filename:
        flash("Выберите файл резервной копии (.tar.gz)", "error")
        return redirect(url_for("dashboard"))

    tmp_path = None
    try:
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
        os.close(fd)
        uploaded.save(tmp_path)

        restored, restarted, db_dump_present = backup.restore_backup_archive(tmp_path)
        msg = (
            f"Восстановлено файлов: {len(restored)} ({', '.join(restored)}). "
            f"Перезапущены службы: {', '.join(restarted) if restarted else 'нет'}. "
            f"Старые версии файлов сохранены рядом с суффиксом .before-restore."
        )
        if db_dump_present:
            msg += (
                " В архиве есть также дамп конфигурации FreePBX — он НЕ применён "
                "автоматически (риск для активной репликации в HA). Чтобы применить, "
                "загрузите тот же архив ещё раз на странице «Дамп FreePBX из бэкапа»."
            )
        flash(msg, "success")
    except backup.RestoreError as exc:
        flash(f"Восстановление отменено: {exc}", "error")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return redirect(url_for("dashboard"))


@app.route("/backup/db-dump", methods=["GET", "POST"])
@auth.admin_required
def backup_db_dump():
    """Отдельный, намеренно более трудоёмкий путь применения дампа FreePBX
    из резервной копии — не автоматом при обычном восстановлении (см.
    backup.py: риск для активной репликации в HA, если применить не на
    master). Здесь только извлекаем файл на диск и показываем точную
    команду — применяет её администратор сам, осознанно."""
    if request.method == "POST":
        auth.check_csrf()
        uploaded = request.files.get("backup_file")
        if not uploaded or not uploaded.filename:
            flash("Выберите файл резервной копии (.tar.gz)", "error")
            return redirect(url_for("backup_db_dump"))
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
        os.close(fd)
        try:
            uploaded.save(tmp_path)
            dest = backup.extract_db_dump(tmp_path)
            flash(
                f"Дамп извлечён в {dest}. Примените вручную ТОЛЬКО на текущем master "
                f"(там, где сейчас VIP): mysql -ufreepbxuser -p<пароль> < {dest} — "
                f"реплика подхватит изменения сама через обычную репликацию.",
                "success",
            )
        except backup.RestoreError as exc:
            flash(f"Не удалось извлечь дамп: {exc}", "error")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return redirect(url_for("backup_db_dump"))

    return render_template("backup_db_dump.html")


# ---------------------------------------------------------------------------
# HA-кластер — просмотр статуса
# ---------------------------------------------------------------------------

@app.route("/ha-status")
@auth.admin_required
def ha_status_page():
    status = ha_status.get_full_status()
    return render_template("ha_status.html", status=status)


# ---------------------------------------------------------------------------
# HA-кластер — настройка из веба (первоначальная установка узла; переключение
# ролей во время работы по-прежнему только через keepalived, здесь не трогается)
# ---------------------------------------------------------------------------

@app.route("/ha-setup", methods=["GET", "POST"])
@auth.admin_required
def ha_setup_form():
    if request.method == "GET" and ha_setup.is_setup_running():
        return redirect(url_for("ha_setup_progress"))

    interfaces = ha_setup.get_local_interfaces()

    if request.method == "POST":
        auth.check_csrf()
        try:
            ha_setup.start_setup(
                role=request.form.get("role", ""),
                local_ip=request.form.get("local_ip", ""),
                peer_ip=request.form.get("peer_ip", "").strip(),
                vip=request.form.get("vip", "").strip(),
                cidr=request.form.get("cidr", "").strip(),
                iface=request.form.get("iface", "").strip(),
                vrrp_pass=request.form.get("vrrp_pass", ""),
                repl_pass=request.form.get("repl_pass", ""),
                peer_user=request.form.get("peer_user", "").strip(),
                peer_password=request.form.get("peer_password", ""),
            )
        except ha_setup.ValidationError as exc:
            flash(str(exc), "error")
            return render_template("ha_setup_form.html", interfaces=interfaces)
        return redirect(url_for("ha_setup_progress"))

    return render_template("ha_setup_form.html", interfaces=interfaces)


@app.route("/ha-setup/progress")
@auth.admin_required
def ha_setup_progress():
    return render_template("ha_setup_progress.html")


@app.route("/ha-setup/log.json")
@auth.admin_required
def ha_setup_log_json():
    return ha_setup.get_log_state()


# ---------------------------------------------------------------------------
# Безопасный возврат в кластер после простоя узла — см. ha_safe_failback.sh.
# Отдельно от первоначальной настройки: узел сначала забирает актуальные
# данные с того, кто реально работал, и только потом присоединяется к VRRP.
# ---------------------------------------------------------------------------

@app.route("/ha-setup/failback", methods=["GET", "POST"])
@auth.admin_required
def ha_failback_form():
    if request.method == "GET" and ha_setup.is_failback_running():
        return redirect(url_for("ha_failback_progress"))

    if request.method == "POST":
        auth.check_csrf()
        try:
            ha_setup.start_failback(
                repl_pass=request.form.get("repl_pass", ""),
                peer_user=request.form.get("peer_user", "").strip(),
                peer_password=request.form.get("peer_password", ""),
            )
        except ha_setup.ValidationError as exc:
            flash(str(exc), "error")
            return render_template("ha_failback_form.html")
        return redirect(url_for("ha_failback_progress"))

    return render_template("ha_failback_form.html")


@app.route("/ha-setup/failback/progress")
@auth.admin_required
def ha_failback_progress():
    return render_template("ha_failback_progress.html")


@app.route("/ha-setup/failback/log.json")
@auth.admin_required
def ha_failback_log_json():
    return ha_setup.get_failback_log_state()


# ---------------------------------------------------------------------------
# Сеть телефонов — доверенные подсети Firewall FreePBX. Изначально сделано
# под конкретную проблему: порт 8090 (провижининг) молча блокировался для
# телефонов из недоверенной сети — см. phone_firewall.py.
# ---------------------------------------------------------------------------

@app.route("/phone-firewall", methods=["GET", "POST"])
@auth.admin_required
def phone_firewall_page():
    if request.method == "POST":
        auth.check_csrf()
        action = request.form.get("action")
        subnet = request.form.get("subnet", "")
        try:
            if action == "add":
                phone_firewall.add_trusted_subnet(subnet)
                flash(f"Подсеть {subnet} добавлена в доверенную зону.", "success")
            elif action == "remove":
                phone_firewall.remove_trusted_subnet(subnet)
                flash(f"Подсеть {subnet} удалена из доверенной зоны.", "success")
        except phone_firewall.ValidationError as exc:
            flash(str(exc), "error")
        return redirect(url_for("phone_firewall_page"))

    entries, error = phone_firewall.get_trusted_list()
    return render_template("phone_firewall.html", entries=entries, error=error)


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=False)
