#!/usr/bin/env bash
# Общие шаги, одинаковые для установки/обновления любого из 6 сервисов.

# Копирует исходники сервиса в целевой каталог. При повторном запуске
# (обновление) НЕ трогает уже накопленные данные — БД, секреты, .env —
# обновляется только код. Это то же самое, чего мы добивались вручную
# сегодня при миграции панелей на SSO (и один раз из-за этого потеряли
# настроенный AMI-секрет конференц-панели при полной перезаписи файла).
deploy_source() {
    local src_dir="$1"
    local install_dir="$2"

    mkdir -p "$install_dir"
    rsync -a \
        --exclude='*.db' \
        --exclude='.secret_key' \
        --exclude='.flash_secret' \
        --exclude='.env' \
        --exclude='venv' \
        --exclude='__pycache__' \
        --exclude='INITIAL_PASSWORD.txt' \
        "$src_dir"/ "$install_dir"/
}

create_venv() {
    local install_dir="$1"
    if [[ ! -d "$install_dir/venv" ]]; then
        log_info "Создаю venv в $install_dir/venv"
        python3 -m venv "$install_dir/venv"
    fi
    "$install_dir/venv/bin/pip" install --upgrade pip -q
    if [[ -f "$install_dir/requirements.txt" ]]; then
        "$install_dir/venv/bin/pip" install -q -r "$install_dir/requirements.txt"
    fi
}

set_ownership() {
    local install_dir="$1"
    local owner="${2:-www-data:www-data}"
    chown -R "$owner" "$install_dir"
    chmod -R 755 "$install_dir"
}

# Юнит для сервисов на gunicorn. По умолчанию работает от www-data — кроме
# панели обслуживания, которой нужны права root (systemctl/journalctl над
# остальными службами, AMI-консоль) — для неё девятым параметром передаём
# "root".
write_gunicorn_unit() {
    local unit="$1" install_dir="$2" bind_host="$3" port="$4" entry="$5"
    local gunicorn_args="$6" description="$7" env_file="${8:-}" run_user="${9:-www-data}"

    local env_line=""
    if [[ -n "$env_file" ]]; then
        env_line="EnvironmentFile=$env_file"
    fi

    local user_group_lines="User=$run_user"
    if [[ "$run_user" != "root" ]]; then
        user_group_lines="User=$run_user
Group=$run_user"
    fi

    cat > "/etc/systemd/system/${unit}.service" <<EOF
[Unit]
Description=$description
After=network.target

[Service]
Type=simple
WorkingDirectory=$install_dir
$env_line
ExecStart=$install_dir/venv/bin/gunicorn $gunicorn_args --bind ${bind_host}:${port} --access-logfile - --error-logfile - $entry
Restart=on-failure
RestartSec=3
$user_group_lines

[Install]
WantedBy=multi-user.target
EOF
}

# Юнит для сервисов, запускаемых напрямую (конференц-панель — python3 api.py,
# не gunicorn; так было в оригинале, не меняем).
write_direct_python_unit() {
    local unit="$1" install_dir="$2" entry_script="$3" description="$4"

    cat > "/etc/systemd/system/${unit}.service" <<EOF
[Unit]
Description=$description
After=network.target

[Service]
Type=simple
WorkingDirectory=$install_dir
ExecStart=$install_dir/venv/bin/python3 $install_dir/$entry_script
Restart=on-failure
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF
}

enable_and_start() {
    local unit="$1"
    systemctl daemon-reload
    systemctl enable "$unit" >/dev/null 2>&1
    systemctl restart "$unit"
}
