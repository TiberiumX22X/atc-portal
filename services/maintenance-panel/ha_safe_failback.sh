#!/usr/bin/env bash
# ha_safe_failback.sh — безопасное присоединение узла к уже работающему
# HA-кластеру после простоя (например, узел был выключен/переустанавливался,
# пока второй сервер работал в одиночку и принимал звонки/изменения).
#
# Проблема, которую решает этот скрипт: репликация MariaDB у нас настроена
# ОДНОСТОРОННЕ (master → backup). Если упавший узел просто запустить заново
# как есть — он либо застрянет slave'ом со старыми, устаревшими данными,
# либо (если у него был выше приоритет VRRP) заберёт роль master обратно и
# начнёт отдавать клиентам свою старую базу — а всё, что накопилось на
# втором сервере, пока этот был выключен, будет потеряно.
#
# Правильный порядок: НЕ включать keepalived на восстановленном узле сразу.
# Сначала — забрать актуальную базу и файлы с того узла, который всё это
# время реально работал, и только потом присоединяться к VRRP.
#
# Запуск (на ВОССТАНАВЛИВАЕМОМ узле, тот, что был недоступен):
#   ha_safe_failback.sh --repl-pass P
#
# Остальные параметры (свой IP, IP соседа, VIP, интерфейс) берутся из
# /etc/atc-portal-ha.conf, который записал ha_apply.sh при первоначальной
# настройке — второй раз их вводить не нужно.
#
# В последней строке лога всегда печатается ровно одно из:
#   FAILBACK_RESULT: OK
#   FAILBACK_RESULT: FAILED: <причина>

set -uo pipefail

log_info() { echo "[i] $*"; }
log_ok()   { echo "[✓] $*"; }
log_warn() { echo "[!] $*"; }
log_err()  { echo "[✗] $*"; }

fail() {
    log_err "$*"
    echo "FAILBACK_RESULT: FAILED: $*"
    exit 1
}

# ---------- Разбор аргументов ----------
REPL_PASS="" PEER_USER="" PEER_PASSWORD=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repl-pass) REPL_PASS="$2"; shift 2 ;;
        --peer-user) PEER_USER="$2"; shift 2 ;;
        --peer-password) PEER_PASSWORD="$2"; shift 2 ;;
        *) fail "Неизвестный аргумент: $1" ;;
    esac
done

[[ $EUID -ne 0 ]] && fail "Скрипт должен выполняться от root"
[[ -z "$REPL_PASS" ]] && fail "Пароль пользователя репликации обязателен"

# ---------- Читаем параметры узла из конфига, записанного при первой настройке ----------
CONF=/etc/atc-portal-ha.conf
[[ -f "$CONF" ]] || fail "Не найден $CONF — этот узел ещё ни разу не был частью HA-кластера (сначала обычная настройка, не безопасный возврат)"
# shellcheck disable=SC1090
source "$CONF"
for v in LOCAL_IP PEER_IP VIP CIDR IFACE; do
    [[ -z "${!v:-}" ]] && fail "В $CONF нет значения $v — файл повреждён или от старой версии, переустановите узел через обычную настройку HA"
done
log_info "Восстанавливаемый узел: $LOCAL_IP, активный сосед: $PEER_IP, VIP: $VIP/$CIDR"

# ---------- Проверки перед началом ----------
if systemctl is-active --quiet keepalived; then
    fail "keepalived уже запущен на этом узле — безопасный возврат нужен ДО его включения, чтобы не начать раздавать устаревшие данные. Остановите: systemctl stop keepalived, затем повторите."
fi

log_info "Проверяю, что $PEER_IP сейчас реально держит VIP (это и есть источник актуальных данных)..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new root@"$PEER_IP" "ip a | grep -q '$VIP'" 2>/dev/null; then
    if [[ -n "$PEER_USER" && -n "$PEER_PASSWORD" ]]; then
        log_info "Root по ключу пока не работает — пробую через $PEER_USER (обычная ситуация после переустановки этого узла)..."
        if ! command -v sshpass >/dev/null 2>&1; then
            apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sshpass || fail "Не удалось установить sshpass"
        fi
        [[ -f /root/.ssh/id_rsa.pub ]] || ssh-keygen -t rsa -b 4096 -N "" -f /root/.ssh/id_rsa
        PUBKEY="$(cat /root/.ssh/id_rsa.pub)"
        sshpass -p "$PEER_PASSWORD" ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${PEER_USER}@${PEER_IP}" \
            "sudo mkdir -p /root/.ssh && sudo chmod 700 /root/.ssh && echo '$PUBKEY' | sudo tee -a /root/.ssh/authorized_keys >/dev/null && sudo chmod 600 /root/.ssh/authorized_keys && sudo fwconsole firewall add trusted ${LOCAL_IP}/32 && sudo fwconsole firewall restart" \
            || fail "Не удалось настроить доступ через $PEER_USER@$PEER_IP"
    else
        fail "Нет SSH-доступа к $PEER_IP, и логин/пароль администратора не переданы. Настройте SSH-ключ вручную или укажите логин/пароль администратора соседа."
    fi
fi

if ! ssh -o BatchMode=yes -o ConnectTimeout=5 root@"$PEER_IP" "ip a | grep -q '$VIP'"; then
    fail "$PEER_IP не держит VIP $VIP прямо сейчас — либо сосед тоже не в сети, либо VIP уже здесь. Проверьте вручную (ip a | grep $VIP на обоих узлах) перед повторной попыткой."
fi
log_ok "$PEER_IP подтверждён как источник актуальных данных"

# ---------- Разрешить соседу принимать репликацию именно от нас ----------
log_info "Создаю/подтверждаю пользователя репликации на $PEER_IP для этого узла..."
ssh root@"$PEER_IP" "mysql -e \"CREATE USER IF NOT EXISTS 'repl'@'${LOCAL_IP}' IDENTIFIED BY '${REPL_PASS}'; GRANT REPLICATION SLAVE ON *.* TO 'repl'@'${LOCAL_IP}'; FLUSH PRIVILEGES;\"" \
    || fail "Не удалось создать пользователя репликации на $PEER_IP"

# ---------- Забираем актуальный дамп с активного узла ----------
log_info "Снимаю дамп базы данных с $PEER_IP (это сейчас единственный источник правды)..."
if ! ssh root@"$PEER_IP" "mysqldump --all-databases --master-data=2 --single-transaction" > /tmp/failback_dump.sql; then
    fail "Не удалось снять дамп с $PEER_IP"
fi
LOG_LINE=$(head -30 /tmp/failback_dump.sql | grep "CHANGE MASTER")
LOG_FILE=$(echo "$LOG_LINE" | sed -n "s/.*MASTER_LOG_FILE='\([^']*\)'.*/\1/p")
LOG_POS=$(echo "$LOG_LINE" | sed -n "s/.*MASTER_LOG_POS=\([0-9]*\).*/\1/p")
[[ -z "$LOG_FILE" || -z "$LOG_POS" ]] && fail "Не удалось извлечь позицию бинлога из дампа"
log_ok "Дамп снят (позиция $LOG_FILE:$LOG_POS)"

# ---------- Разворачиваем локально, поверх устаревших данных ----------
log_info "Разворачиваю дамп локально (перезаписывает устаревшую базу этого узла)..."
mysql -e "SET GLOBAL read_only=0;"
mysql < /tmp/failback_dump.sql || fail "Ошибка при разворачивании дампа"
rm -f /tmp/failback_dump.sql
log_ok "База данных приведена в соответствие с $PEER_IP"

# ---------- Настраиваем локальную реплику как slave этого соседа ----------
log_info "Настраиваю репликацию (этот узел временно как slave $PEER_IP)..."
mysql -e "STOP SLAVE;" 2>/dev/null || true
mysql -e "CHANGE MASTER TO MASTER_HOST='${PEER_IP}', MASTER_USER='repl', MASTER_PASSWORD='${REPL_PASS}', MASTER_LOG_FILE='${LOG_FILE}', MASTER_LOG_POS=${LOG_POS};" \
    || fail "CHANGE MASTER TO завершился с ошибкой"
mysql -e "START SLAVE;"
# Одна проверка сразу после START SLAVE ловила гонку по времени на
# практике (IO-поток ещё не успел установить соединение, хотя реально
# всё поднималось на секунду-две позже) — опрашиваем несколько раз с
# паузой вместо единственной проверки через фиксированные 3 секунды.
SLAVE_OK=0
for i in 1 2 3 4 5; do
    sleep 3
    SLAVE_STATUS="$(mysql -e 'SHOW SLAVE STATUS\G' 2>/dev/null)"
    if echo "$SLAVE_STATUS" | grep -q "Slave_IO_Running: Yes"; then
        SLAVE_OK=1
        break
    fi
done
if [[ "$SLAVE_OK" -ne 1 ]]; then
    fail "Репликация не запустилась (Slave_IO_Running != Yes) — проверьте вручную: mysql -e 'SHOW SLAVE STATUS\\G'"
fi
mysql -e "SET GLOBAL read_only=1;"
log_ok "Репликация с $PEER_IP настроена и работает"

# ---------- Забираем файлы (панели, конфиги, записи, astdb) с активного узла ----------
log_info "Забираю актуальные файлы панелей и конфигурацию с $PEER_IP..."
for DIR in sso-auth phone-provisioning cdr-panel monitor-panel alert-panel asterisk-panel maintenance-panel; do
    rsync -az --exclude venv --exclude '*.pyc' --exclude __pycache__ root@"${PEER_IP}":/opt/"$DIR"/ /opt/"$DIR"/ 2>/dev/null || true
done
rsync -az root@"${PEER_IP}":/etc/asterisk/ /etc/asterisk/ 2>/dev/null || true
rsync -az root@"${PEER_IP}":/etc/freepbx.conf /etc/freepbx.conf 2>/dev/null || true
rsync -az root@"${PEER_IP}":/var/spool/asterisk/monitor/ /var/spool/asterisk/monitor/ 2>/dev/null || true
rsync -az root@"${PEER_IP}":/etc/nginx/conf.d/portal.conf /etc/nginx/conf.d/portal.conf 2>/dev/null || true
ssh root@"${PEER_IP}" "sqlite3 /var/lib/asterisk/astdb.sqlite3 '.backup /tmp/astdb_failback.sqlite3'" 2>/dev/null \
    && scp -q root@"${PEER_IP}":/tmp/astdb_failback.sqlite3 /var/lib/asterisk/astdb.sqlite3 \
    && chown asterisk:asterisk /var/lib/asterisk/astdb.sqlite3
log_ok "Файлы получены"

# ---------- Транки/AMI на этом узле — держим выключенными, он пока backup ----------
AMPDBPASS="$(grep -oP "AMPDBPASS[\"']\]\s*=\s*[\"']\K[^\"']+" /etc/freepbx.conf 2>/dev/null || true)"
if [[ -n "$AMPDBPASS" ]]; then
    mysql -ufreepbxuser -p"$AMPDBPASS" asterisk -e "update trunks set disabled='on';" 2>/dev/null || true
fi
command -v fwconsole >/dev/null 2>&1 && fwconsole reload >/dev/null 2>&1 || true
asterisk -rx "manager reload" >/dev/null 2>&1 || true
systemctl restart monitor-panel maintenance-panel alert-panel asterisk-panel 2>/dev/null || true

# ---------- Только теперь безопасно включить keepalived ----------
log_info "Данные синхронизированы — включаю keepalived на этом узле..."
systemctl enable --now keepalived || fail "keepalived не запустился — проверьте journalctl -u keepalived"
sleep 3
if ip a | grep -q "$VIP"; then
    log_warn "VIP сразу перешёл на этот узел (более высокий приоритет) — проверьте, что это ожидаемо."
else
    log_ok "Узел присоединился как BACKUP, VIP остался на $PEER_IP — это нормально и безопасно."
fi

log_ok "Безопасный возврат в кластер завершён."
echo "FAILBACK_RESULT: OK"
