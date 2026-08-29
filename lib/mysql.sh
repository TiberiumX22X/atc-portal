#!/usr/bin/env bash
# CDR-панели нужен ОТДЕЛЬНЫЙ MySQL-пользователь с доступом только на чтение
# к asteriskcdrdb (не переиспользуем freepbxuser — у него полные права на
# всё, а панели он нужен только для SELECT).
#
# Пробуем подключиться как root (на Debian/FreePBX это обычно работает
# через unix_socket без пароля), иначе — учётными данными freepbxuser.

try_mysql_admin() {
    if mysql -u root -e "SELECT 1;" >/dev/null 2>&1; then
        echo "mysql -u root"
        return 0
    fi
    local fpbx_user fpbx_pass
    fpbx_user=$(read_freepbx_conf "AMPDBUSER" "freepbxuser")
    fpbx_pass=$(read_freepbx_conf "AMPDBPASS" "")
    if [[ -n "$fpbx_pass" ]] && mysql -u "$fpbx_user" -p"$fpbx_pass" -e "SELECT 1;" >/dev/null 2>&1; then
        echo "mysql -u $fpbx_user -p$fpbx_pass"
        return 0
    fi
    return 1
}

# Возвращает пароль нового пользователя. Приоритет источников:
#   1. Уже существующий CDR_DB_PASSWORD в /opt/cdr-panel/.env, если файл там
#      есть — это значение поддерживает актуальным HA-синхронизация между
#      узлами (sync_panels.sh), и оно главнее локального кэша ниже.
#   2. Локальный кэш-файл /root/.cdrpanel_mysql_password — подходит только
#      для одиночного (не-HA) сервера или самой первой установки.
#   3. Иначе — генерируем новый случайный пароль.
# Пароль в MySQL всегда ПРИВОДИТСЯ к выбранному значению (ALTER USER), а не
# только создаётся при отсутствии — иначе на повторных установках реальный
# пароль в базе может остаться прежним, даже если .env уже поменялся.
ensure_cdr_readonly_user() {
    local username="cdrpanel"
    local password_file="/root/.cdrpanel_mysql_password"
    local env_file="/opt/cdr-panel/.env"
    local password=""

    if [[ -f "$env_file" ]]; then
        password=$(grep -oP '^CDR_DB_PASSWORD=\K.*' "$env_file" 2>/dev/null)
    fi
    if [[ -z "$password" && -f "$password_file" ]]; then
        password=$(cat "$password_file")
    fi
    if [[ -z "$password" ]]; then
        password=$(random_secret 20)
    fi

    local admin_cmd
    if ! admin_cmd=$(try_mysql_admin); then
        log_warn "Не удалось подключиться к MySQL как администратор."
        log_warn "Создайте пользователя вручную и запустите установку заново:"
        echo "  CREATE USER 'cdrpanel'@'localhost' IDENTIFIED BY 'ВАШ_ПАРОЛЬ';"
        echo "  GRANT SELECT ON asteriskcdrdb.* TO 'cdrpanel'@'localhost';"
        echo "  FLUSH PRIVILEGES;"
        return 1
    fi

    $admin_cmd -e "
        CREATE USER IF NOT EXISTS 'cdrpanel'@'localhost' IDENTIFIED BY '${password}';
        ALTER USER 'cdrpanel'@'localhost' IDENTIFIED BY '${password}';
        GRANT SELECT ON asteriskcdrdb.* TO 'cdrpanel'@'localhost';
        FLUSH PRIVILEGES;
    " 2>/dev/null

    echo "$password" > "$password_file"
    chmod 600 "$password_file"
    echo "$password"
}
