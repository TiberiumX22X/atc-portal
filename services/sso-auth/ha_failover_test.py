#!/usr/bin/env python3
"""
Раз в неделю (см. install_ha_failover_test_cron в lib/ha.sh) проверяет,
что backup-узел ведёт себя корректно при перезапуске keepalived: не
перехватывает VIP (раз master жив и здоров), и сам сервис поднимается
чисто. Реального переключения ролей НЕ вызывает — рестарт именно на
backup при живом master не должен сдвигать VIP никуда, поэтому риск для
звонков минимален. Пишет результат в тот же alerts_local.db, что и
alerts_check.py (те же upsert/resolve).

Запускается только на узле, где ROLE=backup в /etc/atc-portal-ha.conf —
на master ничего не делает (см. main()).
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import db  # noqa: E402
from alerts_check import upsert_alert, resolve_alert, _read_ha_conf  # noqa: E402

KEY = "ha:failover-test"
SETTLE_SECONDS = 15


def _vip_present_locally(vip):
    proc = subprocess.run(["ip", "addr", "show"], capture_output=True, text=True, timeout=10)
    return vip in proc.stdout


def _vip_reachable(vip):
    proc = subprocess.run(["ping", "-c", "1", "-W", "2", vip], capture_output=True, timeout=5)
    return proc.returncode == 0


def main():
    ha_conf = _read_ha_conf()
    if ha_conf is None:
        return  # HA не настроен на этом сервере вообще

    if ha_conf.get("ROLE", "").lower() != "backup":
        return  # тест выполняется только со стороны backup, см. докстринг

    vip = ha_conf.get("VIP")
    if not vip:
        upsert_alert(KEY, "ha", "warning", "Тест failover пропущен: в /etc/atc-portal-ha.conf нет VIP")
        return

    if _vip_present_locally(vip):
        # На backup VIP и так не должен быть поднят — если он уже здесь,
        # это отдельная, более серьёзная проблема (split-brain?), не
        # связанная с самим тестом — проверка check_ha() её тоже увидит
        # своими средствами, дублировать не нужно, просто не мешаем.
        return

    subprocess.run(["systemctl", "restart", "keepalived"], capture_output=True, timeout=30)
    time.sleep(SETTLE_SECONDS)

    now_present = _vip_present_locally(vip)
    reachable = _vip_reachable(vip)

    if now_present:
        upsert_alert(
            KEY, "ha", "error",
            f"Тест failover: после перезапуска keepalived на backup VIP ({vip}) "
            f"неожиданно поднялся ЗДЕСЬ, хотя master должен быть жив — проверьте приоритеты VRRP",
        )
    elif not reachable:
        upsert_alert(
            KEY, "ha", "error",
            f"Тест failover: после перезапуска keepalived на backup VIP ({vip}) "
            f"не отвечает ни с одного узла кластера",
        )
    else:
        resolve_alert(KEY)


if __name__ == "__main__":
    db.init_local_alerts_db()
    main()
