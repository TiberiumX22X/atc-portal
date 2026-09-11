#!/usr/bin/env bash
# По одной функции install_<service>() на каждый сервис.
# Каждая: копирует код (deploy_source сохраняет уже накопленные данные при
# повторном запуске), ставит venv+зависимости, донастраивает то, что
# специфично именно для этого сервиса (AMI-пользователь, читатель БД CDR
# и т.п.), пишет systemd-юнит, запускает.

NGINX_PREFIX_MONITOR="/monitor/"
NGINX_PREFIX_CDR="/cdr/"
NGINX_PREFIX_ALERT="/alert/"
NGINX_PREFIX_PROVISION="/provision/"
NGINX_PREFIX_CONFBRIDGE="/confbridge/"

install_sso_auth() {
    header "Установка: Единый портал / SSO"
    local dir="/opt/sso-auth"
    deploy_source "$PAYLOAD_DIR/services/sso-auth" "$dir"
    create_venv "$dir"

    # AMI — отдельный read-only пользователь только для проверки живости
    # (Action: Ping), см. alerts_check.py.
    local ami_secret
    ami_secret=$(setup_ami_user "portalalerts" "system" "system")

    cat > "$dir/.env" <<EOF
AMI_HOST=127.0.0.1
AMI_PORT=5038
AMI_USER=portalalerts
AMI_SECRET=$ami_secret
EOF
    chmod 600 "$dir/.env"

    write_gunicorn_unit "sso-auth" "$dir" "127.0.0.1" 8080 "app:app" \
        "--workers 2" "SSO Auth (единый вход для панелей АТС)" "$dir/.env"
    set_ownership "$dir"
    enable_and_start "sso-auth"
    if wait_for_port 8080; then
        log_ok "Портал поднят на 127.0.0.1:8080"
    else
        log_err "Портал не ответил на порту 8080 — проверьте: journalctl -u sso-auth -n 50"
    fi

    setup_alerts_check_cron "$dir"
}

# Уведомления об ошибках на главной странице портала (см. alerts_check.py):
# проверки сервисов/AMI/диска/HA по cron каждые 2 минуты. Обёртка нужна,
# чтобы подтянуть AMI_* из .env перед запуском — сам alerts_check.py их
# читает через os.environ, как и остальные панели.
setup_alerts_check_cron() {
    local dir="$1"
    cat > /usr/local/bin/run_alerts_check.sh <<EOF
#!/usr/bin/env bash
set -a
[[ -f "$dir/.env" ]] && source "$dir/.env"
set +a
"$dir/venv/bin/python3" "$dir/alerts_check.py"
EOF
    chmod +x /usr/local/bin/run_alerts_check.sh

    (crontab -l 2>/dev/null | grep -v run_alerts_check.sh; \
        echo "*/2 * * * * /usr/local/bin/run_alerts_check.sh >> /var/log/alerts_check.log 2>&1") | crontab -
    log_ok "Уведомления об ошибках: проверка по cron каждые 2 минуты (run_alerts_check.sh)"

    setup_ha_failover_test_cron "$dir"
}

# Еженедельный тест failover (см. ha_failover_test.py) — только на узле,
# где ROLE=backup в /etc/atc-portal-ha.conf (сам скрипт тоже это
# проверяет, но нет смысла даже пытаться запускать на не-HA/master
# инсталляциях). Воскресенье, 4 утра — вне рабочего времени.
setup_ha_failover_test_cron() {
    local dir="$1"
    cat > /usr/local/bin/run_ha_failover_test.sh <<EOF
#!/usr/bin/env bash
"$dir/venv/bin/python3" "$dir/ha_failover_test.py"
EOF
    chmod +x /usr/local/bin/run_ha_failover_test.sh

    (crontab -l 2>/dev/null | grep -v run_ha_failover_test.sh; \
        echo "0 4 * * 0 /usr/local/bin/run_ha_failover_test.sh >> /var/log/ha_failover_test.log 2>&1") | crontab -
    log_ok "Еженедельный тест HA-failover настроен (воскресенье 4:00, run_ha_failover_test.sh)"
}

install_monitor_panel() {
    header "Установка: Монитор АТС"
    local dir="/opt/monitor-panel"
    deploy_source "$PAYLOAD_DIR/services/monitor-panel" "$dir"
    create_venv "$dir"

    local ami_secret
    ami_secret=$(setup_ami_user "monitorpanel" "system,call,command" "system,call,command")

    local fpbx_user fpbx_pass
    fpbx_user=$(read_freepbx_conf "AMPDBUSER" "freepbxuser")
    fpbx_pass=$(read_freepbx_conf "AMPDBPASS" "")

    cat > "$dir/.env" <<EOF
AMI_HOST=127.0.0.1
AMI_PORT=5038
AMI_USER=monitorpanel
AMI_SECRET=$ami_secret
FREEPBX_DB_HOST=localhost
FREEPBX_DB_NAME=asterisk
FREEPBX_DB_USER=$fpbx_user
FREEPBX_DB_PASSWORD=$fpbx_pass
EOF
    chmod 600 "$dir/.env"

    # ВАЖНО: строго 1 воркер. Монитор ведёт единый фоновый поток опроса
    # AMI в памяти процесса — при >1 воркере несколько потоков дублируют
    # опрос и путают состояние (задокументированный урок этого проекта).
    write_gunicorn_unit "monitor-panel" "$dir" "127.0.0.1" 8093 "app:app" \
        "--workers 1 --threads 8 --worker-class gthread --backlog 256" \
        "Монитор АТС (статусы добавочных, одновременные вызовы)" "$dir/.env"
    set_ownership "$dir"
    enable_and_start "monitor-panel"
    wait_for_port 8093 && log_ok "Монитор АТС поднят на 127.0.0.1:8093" \
        || log_err "Не поднялся — journalctl -u monitor-panel -n 50"
}

install_cdr_panel() {
    header "Установка: CDR-панель"
    local dir="/opt/cdr-panel"
    deploy_source "$PAYLOAD_DIR/services/cdr-panel" "$dir"
    create_venv "$dir"

    local cdr_password
    if cdr_password=$(ensure_cdr_readonly_user); then
        cat > "$dir/.env" <<EOF
CDR_DB_HOST=localhost
CDR_DB_PORT=3306
CDR_DB_NAME=asteriskcdrdb
CDR_DB_USER=cdrpanel
CDR_DB_PASSWORD=$cdr_password
RECORDINGS_DIR=/var/spool/asterisk/monitor
EOF
        chmod 600 "$dir/.env"
    else
        log_warn "CDR-панель установлена, но БД не настроена — заполните $dir/.env вручную и перезапустите cdr-panel"
    fi

    write_gunicorn_unit "cdr-panel" "$dir" "127.0.0.1" 8092 "app:app" \
        "--workers 3" "CDR-панель (записи звонков)" "$dir/.env"
    set_ownership "$dir"

    # Файлы записей разговоров обычно принадлежат пользователю asterisk —
    # без членства www-data в этой группе кнопка "Удалить запись" будет
    # падать PermissionError. Панель уже умеет вернуть понятную ошибку
    # вместо падения, но лучше сразу дать права, чтобы кнопка реально
    # работала.
    #
    # ⚠️ Одного membership в группу НЕДОСТАТОЧНО: Asterisk каждый день
    # создаёт новую вложенную папку по дате (monitor/YYYY/MM/DD/) со
    # СВОИМИ правами (755 — группа только read+execute, без записи),
    # права родительской папки на неё не наследуются автоматически.
    # Значит на следующий день после установки кнопка снова сломается на
    # новой папке. Правильное решение — default ACL: тогда КАЖДАЯ новая
    # папка/файл, которые Asterisk создаст когда-либо в будущем, сразу
    # получат нужные права без ручного вмешательства (обнаружено и
    # исправлено 24.08.2026 — простого usermod хватило только на первый
    # день после установки).
    local monitor_dir="/var/spool/asterisk/monitor"
    if getent group asterisk >/dev/null 2>&1; then
        usermod -a -G asterisk www-data
        log_info "www-data добавлен в группу asterisk"
    fi
    if [[ -d "$monitor_dir" ]]; then
        ensure_packages acl
        if have_cmd setfacl; then
            setfacl -R -m g:www-data:rwx "$monitor_dir" 2>/dev/null
            setfacl -R -d -m g:www-data:rwx "$monitor_dir" 2>/dev/null
            log_info "ACL по умолчанию настроен на $monitor_dir — новые папки по дате тоже будут доступны для удаления"
        else
            log_warn "setfacl недоступен — удаление записей может не работать для папок, создаваемых Asterisk в будущем (после сегодняшнего дня)"
        fi
    fi

    enable_and_start "cdr-panel"
    wait_for_port 8092 && log_ok "CDR-панель поднята на 127.0.0.1:8092" \
        || log_err "Не поднялась — journalctl -u cdr-panel -n 50"
}

install_alert_panel() {
    header "Установка: Панель оповещения"
    local dir="/opt/alert-panel"
    deploy_source "$PAYLOAD_DIR/services/alert-panel" "$dir"
    create_venv "$dir"

    local ami_secret
    ami_secret=$(setup_ami_user "alertpanel" "system,call,originate,agent,dialplan" "system,call,originate,agent,dialplan,command")

    # AMI-настройки этой панели хранятся в её собственной SQLite (не в
    # .env) — сперва даём схеме создаться (она создаётся при первом
    # обращении к БД в коде приложения), затем сидируем значения напрямую.
    if [[ ! -f "$dir/alert_panel.db" ]]; then
        sqlite3 "$dir/alert_panel.db" < "$dir/schema.sql"
    fi
    sqlite3 "$dir/alert_panel.db" <<SQL
INSERT INTO settings (key, value) VALUES ('ami_host', '127.0.0.1')
    ON CONFLICT(key) DO UPDATE SET value=excluded.value;
INSERT INTO settings (key, value) VALUES ('ami_port', '5038')
    ON CONFLICT(key) DO UPDATE SET value=excluded.value;
INSERT INTO settings (key, value) VALUES ('ami_username', 'alertpanel')
    ON CONFLICT(key) DO UPDATE SET value=excluded.value;
INSERT INTO settings (key, value) VALUES ('ami_secret', '$ami_secret')
    ON CONFLICT(key) DO UPDATE SET value=excluded.value;
SQL

    # Панель звонит через AMI Originate в СВОЙ собственный dialplan-контекст
    # (alert-panel-room/join) — отвечает на звонок и проигрывает запись.
    # Без этого контекста в extensions_custom.conf каждый Originate
    # уходит в никуда: AMI-логин и список зарегистрированных контактов
    # выглядят полностью исправными, а реальный звонок так и не происходит
    # (это именно то, что случилось при первой чистой установке 21.08.2026 —
    # шаг был в СТАРОМ отдельном install.sh панели, но потерялся при
    # объединении всех пяти установщиков в один).
    local extensions_conf="/etc/asterisk/extensions_custom.conf"
    if ! grep -q "^\[alert-panel-room\]" "$extensions_conf" 2>/dev/null; then
        cat >> "$extensions_conf" <<'EOF'

[alert-panel-room]
exten => join,1,NoOp(Join alert room: ${ALERT_NAME})
 same => n,Answer()
 same => n,Wait(1)
 same => n,GotoIf($["${RECORDING_FILE}" = ""]?noann:ann)
 same => n(ann),Playback(${RECORDING_FILE})
 same => n(noann),NoOp(no recording)
 same => n,Hangup()
EOF
        log_info "Добавлен dialplan-контекст [alert-panel-room] в $extensions_conf"
        fwconsole reload >/dev/null 2>&1 || asterisk -rx "dialplan reload" >/dev/null 2>&1
    else
        log_info "Контекст [alert-panel-room] уже существует в $extensions_conf — не трогаю"
    fi

    write_gunicorn_unit "alert-panel" "$dir" "127.0.0.1" 8091 "app:app" \
        "--workers 2 --threads 4 --timeout 120" "Панель оповещения"
    set_ownership "$dir"
    enable_and_start "alert-panel"
    wait_for_port 8091 && log_ok "Панель оповещения поднята на 127.0.0.1:8091" \
        || log_err "Не поднялась — journalctl -u alert-panel -n 50"
}

install_phone_provisioning() {
    header "Установка: Провижининг телефонов"
    local dir="/opt/phone-provisioning"
    deploy_source "$PAYLOAD_DIR/services/phone-provisioning" "$dir"
    create_venv "$dir"

    # ЕДИНСТВЕННЫЙ сервис проекта, который НЕ переводится на 127.0.0.1 —
    # часть его маршрутов (/cfg<mac>.xml, /firmware/...) обязаны быть
    # доступны телефонам по всей LAN напрямую, без SSO. Защита от подделки
    # заголовка SSO встроена в само приложение (проверка remote_addr).
    # workers/threads с запасом: при массовой перезагрузке/сбросе сразу
    # нескольких телефонов конфиг + прошивка (~500-600 КБ) держат слот
    # занятым дольше обычного HTTP-запроса — 3×4=12 слотов оказывалось
    # тесно на практике при одновременном провижининге нескольких устройств.
    write_gunicorn_unit "phone-provisioning" "$dir" "0.0.0.0" 8090 "app:app" \
        "--workers 5 --threads 10 --worker-class gthread --backlog 512" \
        "Phone auto-provisioning service"
    set_ownership "$dir"
    enable_and_start "phone-provisioning"
    wait_for_port 8090 && log_ok "Провижининг поднят на 0.0.0.0:8090" \
        || log_err "Не поднялся — journalctl -u phone-provisioning -n 50"

    configure_phone_provisioning_firewall
}

# Порт 8090 нигде не входит в штатные сервисы Firewall FreePBX
# (fpbxsvc-http=80, fpbxsvc-https=443, fpbxsvc-provis=84 и т.п.) — трафик
# от телефонов из недоверенной сети на этот порт молча дропается политикой
# "запрещено всё, что явно не разрешено". Обнаружено на практике: сервер
# отвечал на curl с самого себя (localhost не фильтруется так же), а
# реальные телефоны из LAN не получали вообще ничего — ни ответа, ни
# ошибки, и запрос даже не долетал до лога приложения.
#
# Настройка подсети (или нескольких) вынесена в панель «Обслуживание»
# (раздел «Сеть телефонов») — там же, где и остальные системные действия,
# не привязана к моменту установки и не требует переустановки, чтобы
# что-то добавить или поменять.
configure_phone_provisioning_firewall() {
    log_warn "Не забудьте открыть порт 8090 для подсети телефонов — панель «Обслуживание» → «Сеть телефонов»."
}

install_confbridge_panel() {
    header "Установка: Конференц-панель"
    local dir="/opt/asterisk-panel"
    deploy_source "$PAYLOAD_DIR/services/confbridge-panel" "$dir"
    create_venv "$dir"

    local ami_secret
    ami_secret=$(setup_ami_user "dashboard" "all" "all")

    # ⚠️ У этой панели секрет AMI зашит ЛИТЕРАЛОМ в api.py (не через .env),
    # поэтому подставляем его через sed по месту — и делаем это ПОСЛЕ
    # deploy_source, но только если сейчас там ещё дефолтный плейсхолдер
    # (иначе на повторном запуске затрём уже настроенный реальный секрет).
    if grep -q "'ami_secret': 'CHANGE_ME'" "$dir/api.py"; then
        sed -i "s/'ami_secret': 'CHANGE_ME'/'ami_secret': '${ami_secret}'/" "$dir/api.py"
    fi

    write_direct_python_unit "asterisk-panel" "$dir" "api.py" \
        "Asterisk Conference Panel (ConfBridge)"
    set_ownership "$dir"
    enable_and_start "asterisk-panel"
    wait_for_port 5000 && log_ok "Конференц-панель поднята на 127.0.0.1:5000" \
        || log_err "Не поднялась — journalctl -u asterisk-panel -n 50"
}

install_maintenance_panel() {
    header "Установка: Обслуживание"
    local dir="/opt/maintenance-panel"
    deploy_source "$PAYLOAD_DIR/services/maintenance-panel" "$dir"
    create_venv "$dir"

    # Доступна только роли admin (проверяется и на портале, и внутри самой
    # панели — двойная защита). Работает от root: ей нужны права запускать
    # systemctl/journalctl над остальными пятью службами и читать/писать
    # чужие .env и manager_custom.conf при ротации AMI-секретов.
    local ami_secret
    ami_secret=$(setup_ami_user "maintpanel" "all" "system,call,command,agent,dialplan,originate,reporting")

    cat > "$dir/.env" <<EOF
AMI_HOST=127.0.0.1
AMI_PORT=5038
AMI_USER=maintpanel
AMI_SECRET=$ami_secret
EOF
    chmod 600 "$dir/.env"

    write_gunicorn_unit "maintenance-panel" "$dir" "127.0.0.1" 8094 "app:app" \
        "--workers 2" "Панель обслуживания (статус служб, AMI-консоль, логи)" \
        "$dir/.env" "root"
    set_ownership "$dir" "root:root"
    enable_and_start "maintenance-panel"
    wait_for_port 8094 && log_ok "Обслуживание поднято на 127.0.0.1:8094" \
        || log_err "Не поднялась — journalctl -u maintenance-panel -n 50"
}
