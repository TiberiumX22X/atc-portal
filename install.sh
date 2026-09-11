#!/usr/bin/env bash
# Единый установщик АТС-портала: SSO + 6 панелей (Монитор, CDR, Оповещение,
# Провижининг, Конференц-панель, Обслуживание) поверх Asterisk/FreePBX.
#
# Запуск: sudo ./install.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD_DIR="$SCRIPT_DIR"
APT_UPDATED=0

# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/dependencies.sh"
source "$SCRIPT_DIR/lib/freepbx.sh"
source "$SCRIPT_DIR/lib/mysql.sh"
source "$SCRIPT_DIR/lib/ami.sh"
source "$SCRIPT_DIR/lib/panel_common.sh"
source "$SCRIPT_DIR/lib/services.sh"
source "$SCRIPT_DIR/lib/ha.sh"
source "$SCRIPT_DIR/nginx/setup.sh"

ALL_SERVICES=(sso-auth monitor-panel cdr-panel alert-panel phone-provisioning asterisk-panel maintenance-panel)

# ---------------------------------------------------------------------------

install_full() {
    header "ПОЛНАЯ УСТАНОВКА: портал + все 6 панелей"
    ensure_packages rsync
    check_dependencies
    install_sso_auth
    install_monitor_panel
    install_cdr_panel
    install_alert_panel
    install_phone_provisioning
    install_confbridge_panel
    install_maintenance_panel
    configure_nginx
    print_summary
}

print_summary() {
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    ip="${ip:-<IP_сервера>}"

    header "УСТАНОВКА ЗАВЕРШЕНА"
    echo
    echo "Портал:            http://${ip}:8888/"
    echo
    echo "Первый вход:"
    echo "  1. Откройте http://${ip}:8888/setup"
    echo "  2. Создайте первого администратора (логин + пароль)"
    echo "  3. Дальше пользователей можно добавлять на странице «Пользователи» —"
    echo "     для роли «Оператор» там же отмечается, какие именно панели ему"
    echo "     доступны (по умолчанию — все, кроме «Обслуживание»)"
    echo
    echo "  Если на сервере уже были старые панели с локальными логинами и вы"
    echo "  хотите перенести их в SSO — запустите ДО первого /setup:"
    echo "    cd /opt/sso-auth && sudo ./venv/bin/python migrate_users.py --dry-run"
    echo
    echo "Разделы портала:"
    echo "  /monitor/     — Монитор АТС"
    echo "  /cdr/         — CDR / записи разговоров"
    echo "  /alert/       — Оповещение"
    echo "  /provision/   — Провижининг телефонов"
    echo "  /confbridge/  — Конференц-панель"
    echo "  /maintenance/ — Обслуживание (только для администратора: статус"
    echo "                  всех служб, AMI-консоль, ротация AMI-секретов,"
    echo "                  место на диске, резервное копирование/восстановление)"
    echo
    echo "Провижининг телефонов ДОПОЛНИТЕЛЬНО доступен напрямую на порту 8090"
    echo "(это нужно самим телефонам, DHCP option 66 должен указывать сюда):"
    echo "  http://${ip}:8090/"
    echo
    print_status
}

print_status() {
    header "Статус сервисов"
    for svc in "${ALL_SERVICES[@]}"; do
        check_service "$svc"
    done
    check_service "nginx"
}

uninstall_all() {
    header "УДАЛЕНИЕ ВСЕГО"
    log_warn "Это остановит и удалит ВСЕ сервисы портала и все 6 панелей,"
    log_warn "включая их базы данных, записи разговоров-ссылки и настройки."
    log_warn "AMI-пользователи в /etc/asterisk/manager_custom.conf останутся"
    log_warn "(их нужно убрать вручную, если действительно не нужны)."
    if ! confirm "Точно удалить всё?"; then
        log_info "Отменено."
        return
    fi

    for svc in "${ALL_SERVICES[@]}"; do
        systemctl stop "$svc" 2>/dev/null || true
        systemctl disable "$svc" 2>/dev/null || true
        rm -f "/etc/systemd/system/${svc}.service"
    done
    systemctl daemon-reload

    rm -rf /opt/sso-auth /opt/monitor-panel /opt/cdr-panel /opt/alert-panel \
           /opt/phone-provisioning /opt/asterisk-panel /opt/maintenance-panel
    rm -f /etc/nginx/conf.d/portal.conf
    systemctl reload nginx 2>/dev/null || true

    log_ok "Удалено."
}

menu_install_single_panel() {
    echo
    echo "Какую панель установить/обновить?"
    echo "  1) Монитор АТС"
    echo "  2) CDR-панель"
    echo "  3) Панель оповещения"
    echo "  4) Провижининг телефонов"
    echo "  5) Конференц-панель"
    echo "  6) Обслуживание (только для администратора)"
    echo "  0) Назад"
    read -r -p "Выбор: " choice
    ensure_packages rsync
    case "$choice" in
        1) check_dependencies; install_monitor_panel ;;
        2) check_dependencies; install_cdr_panel ;;
        3) check_dependencies; install_alert_panel ;;
        4) check_dependencies; install_phone_provisioning ;;
        5) check_dependencies; install_confbridge_panel ;;
        6) check_dependencies; install_maintenance_panel ;;
        0) return ;;
        *) log_warn "Не понял выбор." ;;
    esac
}

main_menu() {
    while true; do
        echo
        header "УСТАНОВЩИК АТС-ПОРТАЛА (SSO + 6 панелей)"
        echo "  1) Полная установка (портал + все 6 панелей) — для новой АТС"
        echo "  2) Установить/обновить только портал (SSO)"
        echo "  3) Установить/обновить одну панель"
        echo "  4) Настроить/обновить nginx (портал на порту 8888)"
        echo "  5) Проверить статус всех сервисов"
        echo "  6) Удалить всё"
        echo "  7) Настроить HA-кластер (keepalived + репликация MariaDB)"
        echo "  0) Выход"
        echo
        read -r -p "Выбор: " choice
        case "$choice" in
            1) install_full ;;
            2) ensure_packages rsync; check_dependencies; install_sso_auth ;;
            3) menu_install_single_panel ;;
            4) configure_nginx ;;
            5) print_status ;;
            6) uninstall_all ;;
            7) setup_ha_cluster ;;
            0) exit 0 ;;
            *) log_warn "Не понял выбор, попробуйте ещё раз." ;;
        esac
    done
}

# ---------------------------------------------------------------------------

need_root

case "${1:-}" in
    --full)        ensure_packages rsync; install_full ;;
    --status)      print_status ;;
    --uninstall)   uninstall_all ;;
    --nginx)       configure_nginx ;;
    --ha-setup)    setup_ha_cluster ;;
    "")            main_menu ;;
    *)
        echo "Использование: $0 [--full|--status|--uninstall|--nginx|--ha-setup]"
        echo "Без аргументов — интерактивное меню."
        exit 1
        ;;
esac
