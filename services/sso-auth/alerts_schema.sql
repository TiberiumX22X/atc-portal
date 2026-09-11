-- Уведомления об ошибках системы (сервисы, AMI, диск, HA) — см.
-- alerts_check.py, наполняется по cron. alert_key — устойчивый
-- идентификатор конкретной проблемы (например 'service:cdr-panel'),
-- по нему определяется, новая это проблема или уже известная активная.
-- node — hostname узла, где обнаружена проблема (для объединённого вида
-- на портале, см. docs/alerts-notifications.md).
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key TEXT NOT NULL,
    node TEXT NOT NULL,
    source TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'error' CHECK(severity IN ('warning', 'error')),
    message TEXT NOT NULL,
    first_seen TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_active_key
    ON alerts(alert_key) WHERE resolved_at IS NULL;
