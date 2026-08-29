#!/usr/bin/env bash
# Собирает /etc/nginx/conf.d/portal.conf со всеми шестью панелями,
# заголовками auth_request/X-Forwarded-Prefix/X-Remote-Name для каждой,
# и per-панельными правами доступа операторов (X-Panel-Key -> портал
# сверяет со списком разрешённых панелей пользователя и отдаёт 403,
# если панель не входит в список — проверка на сервере, не только в
# интерфейсе, прямой переход по URL тоже блокируется).

configure_nginx() {
    header "Настройка nginx (единый портал, порт 8888)"

    # Классический sites-enabled/default с listen 80 конфликтует с
    # Apache/FreePBX-админкой — убираем, если есть (та же грабля, что мы
    # ловили при первой установке портала вручную).
    if [[ -f /etc/nginx/sites-enabled/default ]]; then
        log_info "Убираю конфликтующий /etc/nginx/sites-enabled/default (listen 80)"
        rm -f /etc/nginx/sites-enabled/default
    fi

    cat > /etc/nginx/conf.d/portal.conf <<'NGINXEOF'
server {
    listen 8888;
    server_name _;

    client_max_body_size 300M;

    location / {
        proxy_pass http://127.0.0.1:8080/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /static/ {
        proxy_pass http://127.0.0.1:8080/static/;
    }

    location = /_verify {
        internal;
        proxy_pass http://127.0.0.1:8080/_verify;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-URI $request_uri;
        proxy_set_header X-Panel-Key $panel_key;
    }

    location = /monitor {
        return 301 /monitor/;
    }

    location /monitor/ {
        set $panel_key "monitor";
        auth_request /_verify;
        auth_request_set $sso_user $upstream_http_x_remote_user;
        auth_request_set $sso_role $upstream_http_x_remote_role;
        auth_request_set $sso_name $upstream_http_x_remote_name;
        error_page 401 = @auth_redirect;
        error_page 403 = @access_denied;

        proxy_set_header X-Remote-User $sso_user;
        proxy_set_header X-Remote-Role $sso_role;
        proxy_set_header X-Remote-Name $sso_name;
        proxy_set_header X-Forwarded-Prefix /monitor;
        proxy_set_header Host $host;

        rewrite ^/monitor/?(.*)$ /$1 break;
        proxy_pass http://127.0.0.1:8093;
    }

    location = /cdr {
        return 301 /cdr/;
    }

    location /cdr/ {
        set $panel_key "cdr";
        auth_request /_verify;
        auth_request_set $sso_user $upstream_http_x_remote_user;
        auth_request_set $sso_role $upstream_http_x_remote_role;
        auth_request_set $sso_name $upstream_http_x_remote_name;
        error_page 401 = @auth_redirect;
        error_page 403 = @access_denied;

        proxy_set_header X-Remote-User $sso_user;
        proxy_set_header X-Remote-Role $sso_role;
        proxy_set_header X-Remote-Name $sso_name;
        proxy_set_header X-Forwarded-Prefix /cdr;
        proxy_set_header Host $host;

        rewrite ^/cdr/?(.*)$ /$1 break;
        proxy_pass http://127.0.0.1:8092;
    }

    location = /alert {
        return 301 /alert/;
    }

    location /alert/ {
        set $panel_key "alert";
        auth_request /_verify;
        auth_request_set $sso_user $upstream_http_x_remote_user;
        auth_request_set $sso_role $upstream_http_x_remote_role;
        auth_request_set $sso_name $upstream_http_x_remote_name;
        error_page 401 = @auth_redirect;
        error_page 403 = @access_denied;

        proxy_set_header X-Remote-User $sso_user;
        proxy_set_header X-Remote-Role $sso_role;
        proxy_set_header X-Remote-Name $sso_name;
        proxy_set_header X-Forwarded-Prefix /alert;
        proxy_set_header Host $host;

        rewrite ^/alert/?(.*)$ /$1 break;
        proxy_pass http://127.0.0.1:8091;
    }

    location = /provision {
        return 301 /provision/;
    }

    location /provision/ {
        set $panel_key "provision";
        auth_request /_verify;
        auth_request_set $sso_user $upstream_http_x_remote_user;
        auth_request_set $sso_role $upstream_http_x_remote_role;
        auth_request_set $sso_name $upstream_http_x_remote_name;
        error_page 401 = @auth_redirect;
        error_page 403 = @access_denied;

        proxy_set_header X-Remote-User $sso_user;
        proxy_set_header X-Remote-Role $sso_role;
        proxy_set_header X-Remote-Name $sso_name;
        proxy_set_header X-Forwarded-Prefix /provision;
        proxy_set_header Host $host;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;

        rewrite ^/provision/?(.*)$ /$1 break;
        proxy_pass http://127.0.0.1:8090;
    }

    location = /confbridge {
        return 301 /confbridge/;
    }

    location /confbridge/ {
        set $panel_key "confbridge";
        auth_request /_verify;
        auth_request_set $sso_user $upstream_http_x_remote_user;
        auth_request_set $sso_role $upstream_http_x_remote_role;
        auth_request_set $sso_name $upstream_http_x_remote_name;
        error_page 401 = @auth_redirect;
        error_page 403 = @access_denied;

        proxy_set_header X-Remote-User $sso_user;
        proxy_set_header X-Remote-Role $sso_role;
        proxy_set_header X-Remote-Name $sso_name;
        proxy_set_header X-Forwarded-Prefix /confbridge;
        proxy_set_header Host $host;

        rewrite ^/confbridge/?(.*)$ /$1 break;
        proxy_pass http://127.0.0.1:5000;
    }

    location = /maintenance {
        return 301 /maintenance/;
    }

    location /maintenance/ {
        auth_request /_verify;
        auth_request_set $sso_user $upstream_http_x_remote_user;
        auth_request_set $sso_role $upstream_http_x_remote_role;
        auth_request_set $sso_name $upstream_http_x_remote_name;
        error_page 401 = @auth_redirect;

        proxy_set_header X-Remote-User $sso_user;
        proxy_set_header X-Remote-Role $sso_role;
        proxy_set_header X-Remote-Name $sso_name;
        proxy_set_header X-Forwarded-Prefix /maintenance;
        proxy_set_header Host $host;

        rewrite ^/maintenance/?(.*)$ /$1 break;
        proxy_pass http://127.0.0.1:8094;
    }

    location @auth_redirect {
        return 302 /login?next=$request_uri;
    }

    location @access_denied {
        default_type text/plain;
        return 403 "Доступ к этой панели ограничен для вашей роли. Обратитесь к администратору портала.";
    }
}
NGINXEOF

    if nginx -t 2>&1 | grep -q "syntax is ok"; then
        systemctl enable nginx >/dev/null 2>&1
        systemctl reload nginx 2>/dev/null || systemctl restart nginx
        log_ok "nginx настроен и перезапущен — портал на порту 8888"
    else
        log_err "Ошибка в конфиге nginx — портал не будет доступен, пока не исправите:"
        nginx -t
        return 1
    fi
}
