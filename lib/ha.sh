#!/usr/bin/env bash
# Настройка HA-кластера из двух серверов: keepalived (VRRP+VIP), репликация
# MariaDB master->backup, периодическая синхронизация панелей/конфигурации/
# astdb.sqlite3. Запускается ОТДЕЛЬНО на каждом из двух серверов, с разными
# параметрами (роль master на одном, backup на другом).
#
# На основе присланного пользователем черновика setup_ha.sh — доработано:
#   - валидация AMPDBPASS (раньше могла тихо стать пустой строкой)
#   - явная проверка структуры таблицы trunks перед прямой записью в неё
#     (тот же класс риска, что и с таблицей conferences сегодня — но
#     trunks гораздо стабильнее между версиями FreePBX, поэтому проверка
#     легче, не полный откат при малейшем расхождении)
#   - firewall правило именно на PEER_IP для порта MySQL, не на всю подсеть
#   - пишет общий конфиг /etc/atc-portal-ha.conf, который читает панель
#     "Обслуживание" для показа статуса кластера (роль/VIP/синхронизация)

write_ha_shared_config() {
    cat > /etc/atc-portal-ha.conf <<EOF
ROLE=${ROLE}
LOCAL_IP=${LOCAL_IP}
PEER_IP=${PEER_IP}
VIP=${VIP}
CIDR=${CIDR}
IFACE=${IFACE}
EOF
    chmod 644 /etc/atc-portal-ha.conf
}

setup_ha_cluster() {
    header "Настройка HA-кластера (keepalived + репликация MariaDB)"
    log_warn "Запускать ОТДЕЛЬНО на каждом из двух серверов, с разными параметрами."
    log_warn "Требует SSH-доступ по ключу ко второму серверу и root на обоих."
    echo

    read -r -p "Роль этого сервера (master/backup): " ROLE
    read -r -p "Локальный IP этого сервера: " LOCAL_IP
    read -r -p "IP второго сервера (peer): " PEER_IP
    read -r -p "Виртуальный IP (VIP): " VIP
    read -r -p "Маска сети в CIDR (например 21): " CIDR
    read -r -p "Сетевой интерфейс (см. ip a, например enp1s0): " IFACE
    read -r -sp "Пароль для VRRP-аутентификации (до 8 симв.): " VRRP_PASS; echo
    read -r -sp "Пароль пользователя репликации MySQL (repl): " REPL_PASS; echo

    if [[ "$ROLE" != "master" && "$ROLE" != "backup" ]]; then
        log_err "Роль должна быть master или backup"
        return 1
    fi
    if [[ ${#VRRP_PASS} -gt 8 ]]; then
        log_err "Пароль VRRP-аутентификации ограничен протоколом до 8 символов — введите короче"
        return 1
    fi

    local STATE="BACKUP" PRIORITY=100
    [[ "$ROLE" == "master" ]] && STATE="MASTER" && PRIORITY=150

    echo
    log_info "Роль: $ROLE, локальный IP: $LOCAL_IP, peer: $PEER_IP, VIP: $VIP/$CIDR, интерфейс: $IFACE"
    confirm "Продолжить?" || { log_info "Отменено."; return 0; }

    # ---------- Проверка SSH-ключа к соседу ----------
    log_info "Проверка SSH-доступа к $PEER_IP..."
    if ! ssh -o BatchMode=yes -o ConnectTimeout=5 root@"$PEER_IP" "echo OK" 2>/dev/null; then
        log_err "Нет SSH-доступа по ключу к $PEER_IP. Выполните на этом сервере:"
        echo "    ssh-keygen -t rsa -b 4096 -N '' -f ~/.ssh/id_rsa   # если ключа ещё нет"
        echo "    ssh-copy-id root@$PEER_IP"
        echo "  После этого запустите установку HA заново."
        return 1
    fi
    log_ok "SSH-доступ к $PEER_IP подтверждён"

    # ---------- keepalived ----------
    ensure_packages keepalived

    log_info "Health-check скрипт..."
    cat > /etc/keepalived/check_pbx.sh << 'EOF'
#!/bin/bash
asterisk -rx "core show uptime" >/dev/null 2>&1 || exit 1
systemctl is-active --quiet mariadb || exit 1
systemctl is-active --quiet nginx || exit 1
exit 0
EOF
    chmod +x /etc/keepalived/check_pbx.sh

    log_info "master.sh / backup.sh (переключение транков при смене роли)..."
    cat > /etc/keepalived/master.sh << 'EOF'
#!/bin/bash
LOG=/var/log/keepalived-notify.log
echo "$(date '+%F %T') master.sh запущен" >> "$LOG"

AMPDBPASS="$(grep -oP "AMPDBPASS[\"']\]\s*=\s*[\"']\K[^\"']+" /etc/freepbx.conf)"
if [[ -z "$AMPDBPASS" ]]; then
    echo "$(date '+%F %T') ОШИБКА: не удалось прочитать AMPDBPASS из /etc/freepbx.conf — транки НЕ включены" >> "$LOG"
    exit 1
fi

mysql -e "SET GLOBAL read_only=0;" 2>>"$LOG"

# Проверяем, что колонка disabled реально есть в trunks, прежде чем в неё
# писать — trunks гораздо стабильнее между версиями FreePBX, чем таблица
# конференций (с которой мы сегодня намучились), но лишняя защита не помешает.
if mysql -ufreepbxuser -p"$AMPDBPASS" asterisk -N -e "SHOW COLUMNS FROM trunks LIKE 'disabled'" 2>>"$LOG" | grep -q disabled; then
    mysql -ufreepbxuser -p"$AMPDBPASS" asterisk -e "update trunks set disabled='off';" 2>>"$LOG" \
        && echo "$(date '+%F %T') Транки включены (эта нода теперь master)" >> "$LOG"
else
    echo "$(date '+%F %T') ОШИБКА: колонка trunks.disabled не найдена — транки НЕ включены, проверьте схему БД вручную" >> "$LOG"
fi

fwconsole reload >/dev/null 2>&1 &
EOF

    cat > /etc/keepalived/backup.sh << 'EOF'
#!/bin/bash
LOG=/var/log/keepalived-notify.log
echo "$(date '+%F %T') backup.sh запущен" >> "$LOG"

AMPDBPASS="$(grep -oP "AMPDBPASS[\"']\]\s*=\s*[\"']\K[^\"']+" /etc/freepbx.conf)"
if [[ -n "$AMPDBPASS" ]]; then
    if mysql -ufreepbxuser -p"$AMPDBPASS" asterisk -N -e "SHOW COLUMNS FROM trunks LIKE 'disabled'" 2>>"$LOG" | grep -q disabled; then
        mysql -ufreepbxuser -p"$AMPDBPASS" asterisk -e "update trunks set disabled='on';" 2>>"$LOG" \
            && echo "$(date '+%F %T') Транки отключены (эта нода теперь backup/fault)" >> "$LOG"
    fi
else
    echo "$(date '+%F %T') ОШИБКА: не удалось прочитать AMPDBPASS — транки не тронуты" >> "$LOG"
fi

mysql -e "SET GLOBAL read_only=1;" 2>>"$LOG"
fwconsole reload >/dev/null 2>&1 &
EOF
    chmod +x /etc/keepalived/master.sh /etc/keepalived/backup.sh

    log_info "keepalived.conf..."
    local ROUTER_ID="PBX_$(echo "$LOCAL_IP" | tr '.' '_')"
    cat > /etc/keepalived/keepalived.conf << EOF
global_defs {
    router_id ${ROUTER_ID}
    script_user root
    enable_script_security
}

vrrp_script chk_pbx {
    script "/etc/keepalived/check_pbx.sh"
    interval 5
    fall 3
    rise 2
}

vrrp_instance VI_PBX {
    state ${STATE}
    interface ${IFACE}
    virtual_router_id 51
    priority ${PRIORITY}
    advert_int 1
    preempt_delay 15
    authentication {
        auth_type PASS
        auth_pass ${VRRP_PASS}
    }
    unicast_src_ip ${LOCAL_IP}
    unicast_peer { ${PEER_IP} }
    virtual_ipaddress {
        ${VIP}/${CIDR} dev ${IFACE}
    }
    track_script { chk_pbx }
    notify_master "/etc/keepalived/master.sh"
    notify_backup "/etc/keepalived/backup.sh"
    notify_fault "/etc/keepalived/backup.sh"
}
EOF

    # ---------- Firewall FreePBX: доверяем именно PEER_IP, не всю подсеть ----------
    log_info "Настройка Firewall FreePBX (доверенный узел — только peer)..."
    fwconsole firewall add trusted "${PEER_IP}/32" >/dev/null 2>&1 || true
    fwconsole firewall restart >/dev/null 2>&1 || true

    # ---------- MySQL: доступ к порту 3306 разрешаем только с PEER_IP ----------
    # (bind-address всё равно нужно открыть на 0.0.0.0 для репликации через
    # сеть, но порт на уровне ОС-фаервола ограничиваем конкретно соседом —
    # это лучше, чем доверять всей /21-подсети, как было в черновике)
    if have_cmd ufw && ufw status | grep -q "Status: active"; then
        ufw allow from "${PEER_IP}" to any port 3306 proto tcp >/dev/null 2>&1 || true
        log_info "ufw: разрешён доступ к порту 3306 только с $PEER_IP"
    elif have_cmd iptables; then
        iptables -C INPUT -p tcp -s "${PEER_IP}" --dport 3306 -j ACCEPT 2>/dev/null \
            || iptables -I INPUT -p tcp -s "${PEER_IP}" --dport 3306 -j ACCEPT
        log_info "iptables: разрешён доступ к порту 3306 только с $PEER_IP"
    else
        log_warn "Не нашёл ufw/iptables — порт 3306 останется доступен всем, кто может достучаться до сервера. Ограничьте доступ вручную."
    fi

    # ---------- MariaDB: bind-address, server-id, binlog ----------
    log_info "Настройка MariaDB..."
    sed -i 's/^bind-address.*=.*127.0.0.1/bind-address = 0.0.0.0/' /etc/mysql/mariadb.conf.d/50-server.cnf 2>/dev/null || true
    mkdir -p /var/log/mysql
    chown mysql:mysql /var/log/mysql

    local SERVER_ID=1
    [[ "$ROLE" == "backup" ]] && SERVER_ID=2

    cat > /etc/mysql/mariadb.conf.d/60-repl.cnf << EOF
[mysqld]
server-id = ${SERVER_ID}
log_bin = /var/log/mysql/mariadb-bin
binlog_format = ROW
EOF
    [[ "$ROLE" == "backup" ]] && echo "read_only = 1" >> /etc/mysql/mariadb.conf.d/60-repl.cnf

    systemctl restart mariadb
    log_ok "MariaDB перезапущена с настройками репликации"

    # ---------- Репликация: разное для master и backup ----------
    if [[ "$ROLE" == "master" ]]; then
        log_info "[master] Создание пользователя репликации (только для $PEER_IP)..."
        mysql -e "CREATE USER IF NOT EXISTS 'repl'@'${PEER_IP}' IDENTIFIED BY '${REPL_PASS}'; GRANT REPLICATION SLAVE ON *.* TO 'repl'@'${PEER_IP}'; FLUSH PRIVILEGES;"

        log_info "[master] Снятие дампа для переноса на backup — на живой системе, может занять время..."
        if ! mysqldump --all-databases --master-data=2 --single-transaction > /tmp/full_dump.sql; then
            log_err "mysqldump завершился с ошибкой — репликация не настроена, остальные шаги (keepalived) продолжатся"
        else
            local LOG_LINE LOG_FILE LOG_POS
            LOG_LINE=$(head -30 /tmp/full_dump.sql | grep "CHANGE MASTER")
            LOG_FILE=$(echo "$LOG_LINE" | sed -n "s/.*MASTER_LOG_FILE='\([^']*\)'.*/\1/p")
            LOG_POS=$(echo "$LOG_LINE" | sed -n "s/.*MASTER_LOG_POS=\([0-9]*\).*/\1/p")

            if [[ -z "$LOG_FILE" || -z "$LOG_POS" ]]; then
                log_err "Не удалось извлечь позицию бинлога из дампа — репликацию настройте вручную по /tmp/full_dump.sql"
            else
                log_info "[master] Передача дампа на $PEER_IP..."
                scp -q /tmp/full_dump.sql root@"${PEER_IP}":/tmp/

                log_info "[master] Разворачивание дампа и настройка slave на $PEER_IP (удалённо)..."
                if ssh root@"${PEER_IP}" "mysql < /tmp/full_dump.sql && \
                    mysql -e \"CHANGE MASTER TO MASTER_HOST='${LOCAL_IP}', MASTER_USER='repl', MASTER_PASSWORD='${REPL_PASS}', MASTER_LOG_FILE='${LOG_FILE}', MASTER_LOG_POS=${LOG_POS};\" && \
                    mysql -e 'START SLAVE;'"; then
                    log_ok "Slave настроен и запущен на $PEER_IP"
                    log_info "[master] Проверка статуса slave на $PEER_IP..."
                    ssh root@"${PEER_IP}" "mysql -e 'SHOW SLAVE STATUS\G'" | grep -E "Slave_IO_Running|Slave_SQL_Running"
                else
                    log_err "Настройка slave на $PEER_IP завершилась с ошибкой — проверьте вручную"
                fi
            fi
            rm -f /tmp/full_dump.sql
        fi
    else
        log_info "[backup] Репликация настраивается со стороны master — здесь ничего дополнительно делать не нужно."
    fi

    # ---------- astdb.sqlite3 — только на master (source of truth) ----------
    if [[ "$ROLE" == "master" ]]; then
        log_info "Настройка периодической синхронизации astdb.sqlite3 (раз в 5 минут)..."
        cat > /usr/local/bin/sync_astdb.sh << EOF
#!/bin/bash
sqlite3 /var/lib/asterisk/astdb.sqlite3 ".backup /tmp/astdb_backup.sqlite3"
scp -q /tmp/astdb_backup.sqlite3 root@${PEER_IP}:/tmp/astdb.sqlite3.new
ssh root@${PEER_IP} "cp /tmp/astdb.sqlite3.new /var/lib/asterisk/astdb.sqlite3 && chown asterisk:asterisk /var/lib/asterisk/astdb.sqlite3"
EOF
        chmod +x /usr/local/bin/sync_astdb.sh
        (crontab -l 2>/dev/null | grep -v sync_astdb.sh; echo "*/5 * * * * /usr/local/bin/sync_astdb.sh >> /var/log/sync_astdb.log 2>&1") | crontab -
    fi

    # ---------- rsync панелей — только на master ----------
    if [[ "$ROLE" == "master" ]]; then
        log_info "Настройка периодической синхронизации панелей и конфигурации (раз в 3 минуты)..."
        cat > /usr/local/bin/sync_panels.sh << EOF
#!/bin/bash
RHOST=${PEER_IP}
for DIR in sso-auth phone-provisioning cdr-panel monitor-panel alert-panel asterisk-panel maintenance-panel; do
  [ -d "/opt/\$DIR" ] && rsync -az --exclude venv --exclude '*.pyc' --exclude __pycache__ --exclude alerts_local.db --exclude peer_alerts.db /opt/\$DIR/ root@\$RHOST:/opt/\$DIR/
done
rsync -az /etc/asterisk/ root@\$RHOST:/etc/asterisk/
rsync -az /etc/freepbx.conf root@\$RHOST:/etc/freepbx.conf
rsync -az /var/spool/asterisk/monitor/ root@\$RHOST:/var/spool/asterisk/monitor/
rsync -az /etc/nginx/conf.d/portal.conf root@\$RHOST:/etc/nginx/conf.d/portal.conf 2>/dev/null || true
EOF
        chmod +x /usr/local/bin/sync_panels.sh
        (crontab -l 2>/dev/null | grep -v sync_panels.sh; echo "*/3 * * * * /usr/local/bin/sync_panels.sh >> /var/log/sync_panels.log 2>&1") | crontab -
    fi

    # ---------- Обмен уведомлениями об ошибках между узлами (в обе стороны,
    # независимо от роли — иначе с какого бы узла ни открыли портал через
    # VIP, были бы видны только локальные алерты ЭТОГО сервера) ----------
    # alerts_local.db у каждого узла — свои проверки (см.
    # services/sso-auth/alerts_check.py), исключён из sync_panels.sh выше,
    # чтобы push с master не затирал алерты backup. Здесь — обратный, лёгкий
    # pull копии файла с соседа: portal.html показывает объединённый список.
    log_info "Настройка обмена уведомлениями об ошибках между узлами (раз в 2 минуты)..."
    cat > /usr/local/bin/sync_peer_alerts.sh << EOF
#!/bin/bash
rsync -az root@${PEER_IP}:/opt/sso-auth/alerts_local.db /opt/sso-auth/peer_alerts.db 2>/dev/null || true
EOF
    chmod +x /usr/local/bin/sync_peer_alerts.sh
    (crontab -l 2>/dev/null | grep -v sync_peer_alerts.sh; echo "*/2 * * * * /usr/local/bin/sync_peer_alerts.sh >> /var/log/sync_peer_alerts.log 2>&1") | crontab -

    # ---------- Отключить транки, если это backup ----------
    if [[ "$ROLE" == "backup" ]]; then
        log_info "[backup] Отключение транков (резервный узел не должен принимать звонки)..."
        local AMPDBPASS
        AMPDBPASS="$(grep -oP "AMPDBPASS[\"']\]\s*=\s*[\"']\K[^\"']+" /etc/freepbx.conf)"
        if [[ -n "$AMPDBPASS" ]]; then
            mysql -ufreepbxuser -p"$AMPDBPASS" asterisk -e "update trunks set disabled='on';" 2>/dev/null || true
            fwconsole reload >/dev/null 2>&1 || true
        else
            log_warn "Не удалось прочитать AMPDBPASS — транки на backup не отключены, сделайте вручную"
        fi
    fi

    # ---------- Общий конфиг для панели "Обслуживание" ----------
    write_ha_shared_config
    log_ok "Записан /etc/atc-portal-ha.conf — панель «Обслуживание» будет показывать статус этого узла"

    # ---------- Запуск keepalived ----------
    log_info "Запуск keepalived..."
    systemctl enable --now keepalived

    echo
    header "HA-кластер настроен на этом узле"
    sleep 3
    if ip a | grep -q "$VIP"; then
        log_ok "VIP ($VIP) поднят на этом сервере"
    else
        log_info "VIP на этом сервере не активен — нормально для backup, если master жив"
    fi
    echo
    log_warn "Не забудьте выполнить этот же пункт меню со своими параметрами на ВТОРОМ сервере, если ещё не сделали этого."
}
