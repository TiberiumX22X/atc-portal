#!/usr/bin/env bash
# Проверяет и ставит всё, что нужно ДО установки любого из 6 сервисов:
# Python с venv, nginx, sqlite3 (консольный клиент — пригодится для
# диагностики), curl (для проверок после установки).

check_dependencies() {
    header "Проверка системных зависимостей"

    ensure_packages python3 python3-venv python3-pip nginx sqlite3 curl

    # Модуль auth_request у некоторых сборок nginx идёт отдельным пакетом
    # (Debian/Ubuntu собирает его в состав nginx-full/nginx-extras, а не в
    # облегчённый nginx-light) — без него не заработает SSO-портал.
    if nginx -V 2>&1 | grep -q "http_auth_request_module"; then
        log_ok "nginx собран с auth_request (нужен для SSO)"
    else
        log_warn "Текущий nginx собран БЕЗ auth_request module — SSO-портал не заработает."
        log_warn "Переустанавливаю на nginx-extras (полная сборка со всеми модулями)..."
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx-extras
        if nginx -V 2>&1 | grep -q "http_auth_request_module"; then
            log_ok "auth_request module теперь на месте"
        else
            log_err "Не удалось получить auth_request module. Установка портала невозможна на этой системе."
            exit 1
        fi
    fi

    log_ok "Python: $(python3 --version)"
    log_ok "nginx: $(nginx -v 2>&1 | sed 's/nginx version: //')"
    log_ok "Все системные зависимости на месте"
}
