#!/usr/bin/env bash
# ha_apply.sh — неинтерактивная версия установки HA-кластера (keepalived +
# репликация MariaDB + синхронизация панелей/astdb), для запуска из панели
# «Обслуживание» (ha_setup.py) через subprocess, а не из консоли руками.
#
# Логика — прямое перенесение lib/ha.sh (interactive-версия для install.sh),
# без диалоговых read/confirm: все параметры приходят аргументами. Файл
# самодостаточный (без source lib/common.sh — на целевом сервере его нет,
# только сам этот файл лежит рядом с панелью в /opt/maintenance-panel/).
#
# Запуск:
#   ha_apply.sh --role master|backup --local-ip X --peer-ip Y --vip Z \
#               --cidr N --iface enpXsY --vrrp-pass P --repl-pass P
#
# В последней строке лога всегда печатается ровно одно из:
#   HA_SETUP_RESULT: OK
#   HA_SETUP_RESULT: FAILED: <причина>
# — панель определяет по этой строке, что скрипт завершился и с каким исходом.

set -uo pipefail

log_info() { echo "[i] $*"; }
log_ok()   { echo "[✓] $*"; }
log_warn() { echo "[!] $*"; }
log_err()  { echo "[✗] $*"; }

fail() {
    log_err "$*"
    echo "HA_SETUP_RESULT: FAILED: $*"
    exit 1
}

# ---------- Разбор аргументов ----------
ROLE="" LOCAL_IP="" PEER_IP="" VIP="" CIDR="" IFACE="" VRRP_PASS="" REPL_PASS=""
PEER_USER="" PEER_PASSWORD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role) ROLE="$2"; shift 2 ;;
        --local-ip) LOCAL_IP="$2"; shift 2 ;;
        --peer-ip) PEER_IP="$2"; shift 2 ;;
        --vip) VIP="$2"; shift 2 ;;
        --cidr) CIDR="$2"; shift 2 ;;
        --iface) IFACE="$2"; shift 2 ;;
        --vrrp-pass) VRRP_PASS="$2"; shift 2 ;;
        --repl-pass) REPL_PASS="$2"; shift 2 ;;
        --peer-user) PEER_USER="$2"; shift 2 ;;
        --peer-password) PEER_PASSWORD="$2"; shift 2 ;;
        *) fail "Неизвестный аргумент: $1" ;;
    esac
done

[[ $EUID -ne 0 ]] && fail "Скрипт должен выполняться от root"
[[ "$ROLE" != "master" && "$ROLE" != "backup" ]] && fail "Роль должна быть master или backup"
[[ -z "$LOCAL_IP" || -z "$PEER_IP" || -z "$VIP" || -z "$CIDR" || -z "$IFACE" ]] && fail "Не все обязательные параметры переданы"
[[ ${#VRRP_PASS} -gt 8 ]] && fail "Пароль VRRP-аутентификации ограничен протоколом до 8 символов"
[[ -z "$VRRP_PASS" || -z "$REPL_PASS" ]] && fail "Пароли VRRP и репликации обязательны"

STATE="BACKUP"; PRIORITY=100
[[ "$ROLE" == "master" ]] && STATE="MASTER" && PRIORITY=150

log_info "Роль: $ROLE, локальный IP: $LOCAL_IP, peer: $PEER_IP, VIP: $VIP/$CIDR, интерфейс: $IFACE"

# ---------- Firewall FreePBX: доверяем peer ДО проверки SSH ----------
# Это первое, что нужно сделать при чистой установке — при переустановке ОС
# или FreePBX это правило теряется вместе со всей остальной конфигурацией
# (в отличие от кода панелей, который тянется из git), и без него SSH к
# соседу будет молча блокирован ещё до того, как дойдёт до самой проверки
# ключа — раньше это давало запутанное "зависание" на голом TCP-подключении,
# без понятной причины в логе.
log_info "Добавление $PEER_IP в доверенную зону Firewall FreePBX..."
if ! command -v fwconsole >/dev/null 2>&1; then
    fail "fwconsole не найден — это не сервер FreePBX, либо FreePBX ещё не установлен"
fi
if ! fwconsole firewall add trusted "${PEER_IP}/32"; then
    fail "Не удалось добавить $PEER_IP в доверенную зону Firewall FreePBX — проверьте вручную: fwconsole firewall add trusted ${PEER_IP}/32"
fi
if ! fwconsole firewall restart; then
    fail "Firewall FreePBX не перезапустился после добавления правила — проверьте вручную: fwconsole firewall restart"
fi
log_ok "Firewall настроен — $PEER_IP в доверенной зоне"

# ---------- Автоматический бутстрап SSH-доступа к соседу (опционально) ----------
# Root по паролю на свежей установке FreePBX закрыт намеренно (PermitRootLogin
# prohibit-password) — это правильно и трогать это постоянно не стоит. Но
# обычный sudo-пользователь по паролю заходит без ограничений, поэтому если
# логин/пароль такого пользователя переданы — используем его один раз, чтобы
# самим положить публичный ключ в /root/.ssh/authorized_keys на соседе, вместо
# того чтобы просить сделать это руками (ssh-keygen/ssh-copy-id, временное
# включение PermitRootLogin и т.п. — то, с чем сегодня провозились).
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new root@"$PEER_IP" "echo OK" >/dev/null 2>&1; then
    if [[ -n "$PEER_USER" && -n "$PEER_PASSWORD" ]]; then
        log_info "Root-доступ по ключу к $PEER_IP пока не работает — пробую настроить автоматически через пользователя $PEER_USER..."
        log_warn "Требует, чтобы sudo у этого пользователя не спрашивал пароль повторно (NOPASSWD) — так обычно настроено у административных учётных записей FreePBX. Если шаг ниже не пройдёт с ошибкой sudo — настройте SSH-ключ вручную (см. подсказку выше) или добавьте NOPASSWD для этого пользователя."

        if ! command -v sshpass >/dev/null 2>&1; then
            log_info "Устанавливаю sshpass..."
            apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sshpass \
                || fail "Не удалось установить sshpass"
        fi

        [[ -f /root/.ssh/id_rsa.pub ]] || ssh-keygen -t rsa -b 4096 -N "" -f /root/.ssh/id_rsa
        PUBKEY="$(cat /root/.ssh/id_rsa.pub)"

        if ! sshpass -p "$PEER_PASSWORD" ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
            "${PEER_USER}@${PEER_IP}" \
            "sudo mkdir -p /root/.ssh && sudo chmod 700 /root/.ssh && \
             echo '$PUBKEY' | sudo tee -a /root/.ssh/authorized_keys >/dev/null && \
             sudo chmod 600 /root/.ssh/authorized_keys && \
             sudo fwconsole firewall add trusted ${LOCAL_IP}/32 && sudo fwconsole firewall restart && \
             sudo fail2ban-client set sshd unbanip ${LOCAL_IP} 2>/dev/null; true"; then
            fail "Не удалось автоматически настроить доступ через $PEER_USER@$PEER_IP — проверьте логин/пароль, или настройте SSH-ключ вручную (ssh-keygen + ssh-copy-id) и повторите без указания этих полей"
        fi
        log_ok "Ключ передан через $PEER_USER, доверие и бан на стороне соседа обновлены"
    else
        fail "Нет SSH-доступа по ключу к $PEER_IP, и логин/пароль администратора для автоматической настройки не указаны. Выполните ssh-keygen + ssh-copy-id вручную, либо укажите логин/пароль администратора второго сервера в форме."
    fi
fi
log_ok "SSH-доступ к $PEER_IP подтверждён"

# ---------- keepalived ----------
if ! dpkg -s keepalived >/dev/null 2>&1; then
    log_info "Устанавливаю keepalived..."
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq keepalived \
        || fail "Не удалось установить keepalived"
fi

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
    echo "$(date '+%F %T') ОШИБКА: не удалось прочитать AMPDBPASS — транки НЕ включены" >> "$LOG"
    exit 1
fi
mysql -e "SET GLOBAL read_only=0;" 2>>"$LOG"
if mysql -ufreepbxuser -p"$AMPDBPASS" asterisk -N -e "SHOW COLUMNS FROM trunks LIKE 'disabled'" 2>>"$LOG" | grep -q disabled; then
    mysql -ufreepbxuser -p"$AMPDBPASS" asterisk -e "update trunks set disabled='off';" 2>>"$LOG" \
        && echo "$(date '+%F %T') Транки включены (эта нода теперь master)" >> "$LOG"
else
    echo "$(date '+%F %T') ОШИБКА: колонка trunks.disabled не найдена — транки НЕ включены" >> "$LOG"
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
ROUTER_ID="PBX_$(echo "$LOCAL_IP" | tr '.' '_')"
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

# Firewall для peer уже настроен в самом начале скрипта (до проверки SSH) —
# здесь дополнительно ничего делать не нужно.

# ---------- MySQL: доступ к 3306 только с PEER_IP ----------
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow from "${PEER_IP}" to any port 3306 proto tcp >/dev/null 2>&1 || true
    log_info "ufw: разрешён доступ к порту 3306 только с $PEER_IP"
elif command -v iptables >/dev/null 2>&1; then
    iptables -C INPUT -p tcp -s "${PEER_IP}" --dport 3306 -j ACCEPT 2>/dev/null \
        || iptables -I INPUT -p tcp -s "${PEER_IP}" --dport 3306 -j ACCEPT
    log_info "iptables: разрешён доступ к порту 3306 только с $PEER_IP"
else
    log_warn "Не нашёл ufw/iptables — порт 3306 останется доступен всем, кто может достучаться до сервера"
fi

# ---------- MariaDB: bind-address, server-id, binlog ----------
log_info "Настройка MariaDB..."
sed -i 's/^bind-address.*=.*127.0.0.1/bind-address = 0.0.0.0/' /etc/mysql/mariadb.conf.d/50-server.cnf 2>/dev/null || true
mkdir -p /var/log/mysql
chown mysql:mysql /var/log/mysql

SERVER_ID=1
[[ "$ROLE" == "backup" ]] && SERVER_ID=2

cat > /etc/mysql/mariadb.conf.d/60-repl.cnf << EOF
[mysqld]
server-id = ${SERVER_ID}
log_bin = /var/log/mysql/mariadb-bin
binlog_format = ROW
EOF
[[ "$ROLE" == "backup" ]] && echo "read_only = 1" >> /etc/mysql/mariadb.conf.d/60-repl.cnf

systemctl restart mariadb || fail "MariaDB не запустилась после изменения конфигурации — проверьте journalctl -u mariadb"
log_ok "MariaDB перезапущена с настройками репликации"

# ---------- Репликация ----------
if [[ "$ROLE" == "master" ]]; then
    log_info "[master] Создание пользователя репликации (только для $PEER_IP)..."
    mysql -e "CREATE USER IF NOT EXISTS 'repl'@'${PEER_IP}' IDENTIFIED BY '${REPL_PASS}'; GRANT REPLICATION SLAVE ON *.* TO 'repl'@'${PEER_IP}'; FLUSH PRIVILEGES;" \
        || log_err "Не удалось создать пользователя репликации — репликацию потребуется настроить вручную"

    log_info "[master] Снятие дампа для переноса на backup..."
    if ! mysqldump --all-databases --master-data=2 --single-transaction > /tmp/full_dump.sql; then
        log_err "mysqldump завершился с ошибкой — репликация не настроена, остальные шаги продолжатся"
    else
        LOG_LINE=$(head -30 /tmp/full_dump.sql | grep "CHANGE MASTER")
        LOG_FILE=$(echo "$LOG_LINE" | sed -n "s/.*MASTER_LOG_FILE='\([^']*\)'.*/\1/p")
        LOG_POS=$(echo "$LOG_LINE" | sed -n "s/.*MASTER_LOG_POS=\([0-9]*\).*/\1/p")

        if [[ -z "$LOG_FILE" || -z "$LOG_POS" ]]; then
            log_err "Не удалось извлечь позицию бинлога из дампа — настройте репликацию вручную"
        else
            log_info "[master] Передача дампа на $PEER_IP..."
            scp -q /tmp/full_dump.sql root@"${PEER_IP}":/tmp/ || log_err "Не удалось передать дамп на $PEER_IP"

            log_info "[master] Разворачивание дампа и настройка slave на $PEER_IP (удалённо)..."
            if ssh root@"${PEER_IP}" "mysql < /tmp/full_dump.sql && \
                mysql -e \"CHANGE MASTER TO MASTER_HOST='${LOCAL_IP}', MASTER_USER='repl', MASTER_PASSWORD='${REPL_PASS}', MASTER_LOG_FILE='${LOG_FILE}', MASTER_LOG_POS=${LOG_POS};\" && \
                mysql -e 'START SLAVE;'"; then
                log_ok "Slave настроен и запущен на $PEER_IP"
                ssh root@"${PEER_IP}" "mysql -e 'SHOW SLAVE STATUS\G'" | grep -E "Slave_IO_Running|Slave_SQL_Running" || true
            else
                log_err "Настройка slave на $PEER_IP завершилась с ошибкой — проверьте вручную"
            fi
        fi
        rm -f /tmp/full_dump.sql
    fi

    log_info "Настройка периодической синхронизации astdb.sqlite3 (раз в 5 минут)..."
    cat > /usr/local/bin/sync_astdb.sh << EOF
#!/bin/bash
sqlite3 /var/lib/asterisk/astdb.sqlite3 ".backup /tmp/astdb_backup.sqlite3"
scp -q /tmp/astdb_backup.sqlite3 root@${PEER_IP}:/tmp/astdb.sqlite3.new
ssh root@${PEER_IP} "cp /tmp/astdb.sqlite3.new /var/lib/asterisk/astdb.sqlite3 && chown asterisk:asterisk /var/lib/asterisk/astdb.sqlite3"
EOF
    chmod +x /usr/local/bin/sync_astdb.sh
    (crontab -l 2>/dev/null | grep -v sync_astdb.sh; echo "*/5 * * * * /usr/local/bin/sync_astdb.sh >> /var/log/sync_astdb.log 2>&1") | crontab -

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

# .env синхронизируется вместе с кодом панели (в отличие от установки, где он
# намеренно исключён), но уже запущенный процесс не перечитывает переменные
# окружения на лету — без рестарта на backup остаётся старый AMI-секрет,
# из-за чего AMI-логин начинает падать после смены секрета на master, хотя
# файл на диске уже обновлён. Перезапускаем на стороне backup при каждом
# цикле — дёшево и не мешает, раз транки там и так выключены.
ssh root@\$RHOST "systemctl restart monitor-panel maintenance-panel alert-panel asterisk-panel 2>/dev/null" || true

# Самовосстановление рассинхрона MySQL-паролей и AMI-секретов между узлами.
# Причина, с которой мы столкнулись на практике: каждый узел при собственной
# независимой установке (sng_freepbx_debian_install.sh + install.sh панелей)
# генерирует СВОИ случайные пароли для служебных пользователей MariaDB
# (freepbxuser, cdrpanel и т.п.) и вносит их в свою локальную БД — а простое
# копирование .env/manager_custom.conf файлом эту локальную БД соседа не
# трогает. Ниже — приводим пароли на backup к тем же значениям, что реально
# лежат в .env/freepbx.conf на master, при каждом цикле синхронизации.

# freepbxuser — из /etc/freepbx.conf
FPBX_DB_PASS="\$(grep AMPDBPASS /etc/freepbx.conf | awk -F'"' '{print \$4}')"
if [ -n "\$FPBX_DB_PASS" ]; then
    ssh root@\$RHOST "mysql -e \"ALTER USER 'freepbxuser'@'localhost' IDENTIFIED BY '\$FPBX_DB_PASS'; FLUSH PRIVILEGES;\"" 2>/dev/null || true
fi

# Остальные панели — читаем пары <ПРЕФИКС>_DB_USER / <ПРЕФИКС>_DB_PASSWORD
# из каждого .env, что реально есть на этом (master) сервере.
for envfile in /opt/*/.env; do
    [ -f "\$envfile" ] || continue
    for prefix in \$(grep -oP '^\K[A-Z0-9]+(?=_DB_USER=)' "\$envfile" 2>/dev/null); do
        dbuser="\$(grep "^\${prefix}_DB_USER=" "\$envfile" | cut -d= -f2-)"
        dbpass="\$(grep "^\${prefix}_DB_PASSWORD=" "\$envfile" | cut -d= -f2-)"
        if [ -n "\$dbuser" ] && [ -n "\$dbpass" ]; then
            ssh root@\$RHOST "mysql -e \"ALTER USER '\$dbuser'@'localhost' IDENTIFIED BY '\$dbpass'; FLUSH PRIVILEGES;\"" 2>/dev/null || true
        fi
    done
done

# AMI: сам файл manager_custom.conf уже скопирован rsync'ом выше, но Asterisk
# на backup не перечитывает его сам — без явного reload там остаётся старый
# секрет в памяти, и AMI-логин с панелей начинает падать с Authentication failed.
ssh root@\$RHOST "asterisk -rx 'manager reload'" 2>/dev/null || true
EOF
    chmod +x /usr/local/bin/sync_panels.sh
    (crontab -l 2>/dev/null | grep -v sync_panels.sh; echo "*/3 * * * * /usr/local/bin/sync_panels.sh >> /var/log/sync_panels.log 2>&1") | crontab -
else
    log_info "[backup] Репликация настраивается со стороны master — здесь дополнительно ничего не требуется."
    log_info "[backup] Отключение транков (резервный узел не должен принимать звонки)..."
    AMPDBPASS="$(grep -oP "AMPDBPASS[\"']\]\s*=\s*[\"']\K[^\"']+" /etc/freepbx.conf)"
    if [[ -n "$AMPDBPASS" ]]; then
        mysql -ufreepbxuser -p"$AMPDBPASS" asterisk -e "update trunks set disabled='on';" 2>/dev/null || true
        command -v fwconsole >/dev/null 2>&1 && fwconsole reload >/dev/null 2>&1 || true
    else
        log_warn "Не удалось прочитать AMPDBPASS — транки на backup не отключены, сделайте вручную"
    fi
fi

# ---------- Обмен уведомлениями об ошибках между узлами (в обе стороны,
# независимо от роли — иначе с какого бы узла ни открыли портал через
# VIP, были бы видны только локальные алерты ЭТОГО сервера) ----------
log_info "Настройка обмена уведомлениями об ошибках между узлами (раз в 2 минуты)..."
cat > /usr/local/bin/sync_peer_alerts.sh << EOF
#!/bin/bash
rsync -az root@${PEER_IP}:/opt/sso-auth/alerts_local.db /opt/sso-auth/peer_alerts.db 2>/dev/null || true
EOF
chmod +x /usr/local/bin/sync_peer_alerts.sh
(crontab -l 2>/dev/null | grep -v sync_peer_alerts.sh; echo "*/2 * * * * /usr/local/bin/sync_peer_alerts.sh >> /var/log/sync_peer_alerts.log 2>&1") | crontab -

# ---------- Общий конфиг для панели «Обслуживание» ----------
cat > /etc/atc-portal-ha.conf << EOF
ROLE=${ROLE}
LOCAL_IP=${LOCAL_IP}
PEER_IP=${PEER_IP}
VIP=${VIP}
CIDR=${CIDR}
IFACE=${IFACE}
EOF
chmod 644 /etc/atc-portal-ha.conf
log_ok "Записан /etc/atc-portal-ha.conf"

# ---------- Надёжный автозапуск Asterisk (реальный кейс с прод) ----------
# Найдено на практике: LSB-обёртка /etc/init.d/asterisk (через которую
# работает штатный asterisk.service) не проверяет, реально ли поднялся
# процесс — просто запускает команду и репортует "Started", даже если
# Asterisk не стартовал (например, диск/сеть не успели подготовиться при
# загрузке после нештатной перезагрузки). А "правильный" freepbx.service
# (fwconsole start) на backup-узле падает на попытке записи в БД, пока
# read_only=1 — и без Restart= остаётся упавшим навсегда, даже когда
# read_only снимается.
log_info "Настройка надёжного автозапуска Asterisk..."

cat > /usr/local/bin/wait_for_asterisk.sh << 'EOF'
#!/bin/bash
for i in $(seq 1 90); do
    /usr/sbin/asterisk -rx "core show uptime" >/dev/null 2>&1 && exit 0
    sleep 1
done
exit 1
EOF
chmod +x /usr/local/bin/wait_for_asterisk.sh

mkdir -p /etc/systemd/system/asterisk.service.d
cat > /etc/systemd/system/asterisk.service.d/override.conf << 'EOF'
[Service]
ExecStartPre=/bin/bash -c 'pkill -9 asterisk 2>/dev/null; rm -f /var/run/asterisk/asterisk.pid /var/run/asterisk/asterisk.ctl; sleep 1; exit 0'
ExecStartPost=/usr/local/bin/wait_for_asterisk.sh
Restart=on-failure
RestartSec=5
TimeoutStartSec=120
EOF
# ВАЖНО: pkill без -f (по имени процесса, не по командной строке) — с -f
# команда матчит саму себя внутри bash -c и убивает собственный control
# process (status=9/KILL), из-за чего asterisk.service падает в цикл
# мгновенно и бесконечно. Наступили на это на практике — не возвращать -f.

mkdir -p /etc/systemd/system/freepbx.service.d
cat > /etc/systemd/system/freepbx.service.d/override.conf << 'EOF'
[Unit]
StartLimitIntervalSec=120
StartLimitBurst=6

[Service]
Restart=on-failure
RestartSec=10
EOF
# На backup-узле (read_only=1) freepbx.service ЗАКОНОМЕРНО продолжает
# падать — это ожидаемо, не баг: fwconsole start пытается писать в БД.
# Как только read_only снимается (узел становится master), следующий
# ручной/автоматический restart проходит успешно.

systemctl daemon-reload
log_ok "Автозапуск Asterisk настроен (override для asterisk.service и freepbx.service)"

# ---------- Запуск keepalived ----------
log_info "Запуск keepalived..."
systemctl enable --now keepalived || fail "keepalived не запустился — проверьте journalctl -u keepalived"

sleep 3
if ip a | grep -q "$VIP"; then
    log_ok "VIP ($VIP) поднят на этом сервере"
else
    log_info "VIP на этом сервере не активен — нормально для backup, если master жив"
fi

log_ok "HA-кластер настроен на этом узле."
log_warn "Не забудьте выполнить настройку со своими параметрами и на ВТОРОМ сервере, если ещё не сделали этого."
echo "HA_SETUP_RESULT: OK"
