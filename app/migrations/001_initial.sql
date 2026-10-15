CREATE TABLE confirmations (
    id INTEGER PRIMARY KEY,
    confirmed_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    consumed_by_job_id INTEGER
);

CREATE TABLE print_events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('started', 'finished')),
    filename TEXT,
    result TEXT,
    source TEXT NOT NULL CHECK (source IN ('live', 'history'))
);

CREATE TABLE watch_periods (
    id INTEGER PRIMARY KEY,
    began_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    file_modified REAL NOT NULL,
    scheduled_at_utc TEXT NOT NULL,
    head_map_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('pending', 'starting', 'started', 'skipped', 'failed', 'cancelled')),
    outcome_reason TEXT,
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX jobs_status_time ON jobs (status, scheduled_at_utc);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    job_id INTEGER,
    read INTEGER NOT NULL DEFAULT 0,
    telegram_status TEXT NOT NULL CHECK (telegram_status IN ('off', 'pending', 'sent', 'failed')),
    telegram_attempts INTEGER NOT NULL DEFAULT 0,
    telegram_error TEXT,
    telegram_next_at TEXT
);
