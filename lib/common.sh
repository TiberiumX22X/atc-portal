#!/usr/bin/env bash
# Общие функции для всех шагов установщика.
# Подключается через `source lib/common.sh` в install.sh и в services/*/setup.sh.

set -uo pipefail

# --- цвета для вывода ---
C_RESET="\033[0m"
C_BOLD="\033[1m"
C_GREEN="\033[32m"
C_YELLOW="\033[33m"
C_RED="\033[31m"
C_CYAN="\033[36m"

log_info()  { echo -e "${C_CYAN}[i]${C_RESET} $*"; }
log_ok()    { echo -e "${C_GREEN}[✓]${C_RESET} $*"; }
log_warn()  { echo -e "${C_YELLOW}[!]${C_RESET} $*"; }
log_err()   { echo -e "${C_RED}[✗]${C_RESET} $*" >&2; }

header() {
    echo
    echo -e "${C_BOLD}================================================================${C_RESET}"
    echo -e "${C_BOLD}  $*${C_RESET}"
    echo -e "${C_BOLD}================================================================${C_RESET}"
}

need_root() {
    if [[ $EUID -ne 0 ]]; then
        log_err "Этот скрипт нужно запускать от root (sudo $0)"
        exit 1
    fi
}

# Возвращает 0, если команда есть в PATH
have_cmd() { command -v "$1" >/dev/null 2>&1; }

# Возвращает 0, если deb-пакет установлен
have_pkg() { dpkg -s "$1" >/dev/null 2>&1; }

# Ставит перечисленные пакеты, если их ещё нет. Обновляет apt один раз за
# весь прогон установщика (см. APT_UPDATED в install.sh), а не перед каждым
# отдельным пакетом — иначе на медленной сети установка всех 6 сервисов
# растягивается на пустом месте.
ensure_packages() {
    local missing=()
    for pkg in "$@"; do
        have_pkg "$pkg" || missing+=("$pkg")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        return 0
    fi
    log_info "Устанавливаю недостающие системные пакеты: ${missing[*]}"
    if [[ "${APT_UPDATED:-0}" != "1" ]]; then
        apt-get update -qq
        APT_UPDATED=1
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
}

# Генерирует случайную строку заданной длины (для секретов/паролей)
random_secret() {
    local len="${1:-32}"
    python3 -c "import secrets; print(secrets.token_urlsafe($len))" 2>/dev/null \
        || tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$len"
}

# Спрашивает подтверждение y/N, возвращает 0 при "да"
confirm() {
    local prompt="${1:-Продолжить?}"
    read -r -p "$prompt [y/N]: " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]]
}

# Ждёт, пока порт на 127.0.0.1 начнёт слушать (после запуска systemd-сервиса,
# чтобы не проверять статус на долю секунды раньше, чем процесс реально поднялся)
wait_for_port() {
    local port="$1"
    local tries="${2:-15}"
    for ((i = 0; i < tries; i++)); do
        if (echo > "/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# Проверяет, активен ли systemd-юнит, коротко печатает статус
check_service() {
    local name="$1"
    if systemctl is-active --quiet "$name" 2>/dev/null; then
        log_ok "$name — активен"
        return 0
    else
        log_err "$name — НЕ активен"
        return 1
    fi
}
