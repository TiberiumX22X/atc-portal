#!/usr/bin/env bash
# Достаёт значения вида $amp_conf['AMPDBUSER'] = 'freepbxuser'; из
# /etc/freepbx.conf — тот же формат, что и так уже разбирает провижининг
# в своём Python-коде (get_freepbx_db_config), просто здесь нужно то же
# самое во время установки, на bash.

FREEPBX_CONF="/etc/freepbx.conf"

read_freepbx_conf() {
    local key="$1"
    local default="${2:-}"
    if [[ ! -f "$FREEPBX_CONF" ]]; then
        echo "$default"
        return
    fi
    local value
    value=$(grep -oP "\\\$amp_conf\[['\"]${key}['\"]\]\s*=\s*['\"]\K[^'\"]*" "$FREEPBX_CONF" 2>/dev/null | head -1)
    if [[ -z "$value" ]]; then
        echo "$default"
    else
        echo "$value"
    fi
}
