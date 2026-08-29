#!/usr/bin/env bash
# Создаёт (или переиспользует уже существующего) AMI-пользователя в
# /etc/asterisk/manager_custom.conf и возвращает его секрет.
#
# Идемпотентно: если стойка [username] уже есть в файле — секрет из неё
# читается и переиспользуется (а не перегенерируется), чтобы повторный
# запуск установщика не рвал уже настроенную интеграцию.
#
# Использование:
#   secret=$(setup_ami_user "monitorpanel" "system,call" "system,call,command")
#   echo "$secret"

MANAGER_CONF="/etc/asterisk/manager_custom.conf"

setup_ami_user() {
    local username="$1"
    local read_perms="$2"
    local write_perms="$3"

    mkdir -p "$(dirname "$MANAGER_CONF")"
    touch "$MANAGER_CONF"

    if grep -q "^\[${username}\]" "$MANAGER_CONF" 2>/dev/null; then
        # Пользователь уже настроен — берём существующий секрет, не трогаем.
        local existing
        existing=$(awk -v u="[$username]" '
            $0 == u { found=1; next }
            found && /^\[/ { found=0 }
            found && /^secret[[:space:]]*=/ { sub(/^secret[[:space:]]*=[[:space:]]*/, ""); print; exit }
        ' "$MANAGER_CONF")
        if [[ -n "$existing" ]]; then
            echo "$existing"
            return 0
        fi
    fi

    local secret
    secret=$(random_secret 20)

    {
        echo ""
        echo "[$username]"
        echo "secret = $secret"
        echo "deny = 0.0.0.0/0.0.0.0"
        echo "permit = 127.0.0.1/255.255.255.255"
        echo "read = $read_perms"
        echo "write = $write_perms"
    } >> "$MANAGER_CONF"

    if have_cmd asterisk; then
        asterisk -rx "manager reload" >/dev/null 2>&1 || true
    fi

    echo "$secret"
}
