from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import utc_now_iso


class Persistence:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS task_state (
                    task_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    owner_notion_user_id TEXT NOT NULL DEFAULT '',
                    owner_email TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    deadline TEXT,
                    original_deadline TEXT,
                    last_edited_time TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    notion_last_edited_time TEXT,
                    processed_at TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    slack_channel TEXT,
                    slack_ts TEXT
                );

                CREATE TABLE IF NOT EXISTS task_counters (
                    task_id TEXT PRIMARY KEY,
                    delay_count INTEGER NOT NULL DEFAULT 0,
                    verification_flag_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS slack_threads (
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, status, ts)
                );

                CREATE TABLE IF NOT EXISTS task_reminders (
                    reminder_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    reminder_type TEXT NOT NULL,
                    owner_notion_user_id TEXT NOT NULL DEFAULT '',
                    slack_user_id TEXT NOT NULL DEFAULT '',
                    deadline_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS owner_mappings (
                    mapping_key TEXT PRIMARY KEY,
                    owner_name TEXT NOT NULL,
                    slack_user_id TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_run_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS dedupe_events (
                    key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS campaign_flags (
                    campaign_id TEXT NOT NULL,
                    flag_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (campaign_id, flag_type)
                );

                CREATE TABLE IF NOT EXISTS service_titan_audit_violations (
                    violation_key TEXT PRIMARY KEY,
                    service_titan_job_id TEXT NOT NULL,
                    appointment_id TEXT NOT NULL DEFAULT '',
                    technician_id TEXT NOT NULL DEFAULT '',
                    technician_name TEXT NOT NULL DEFAULT '',
                    dispatcher_id TEXT NOT NULL DEFAULT '',
                    dispatcher_name TEXT NOT NULL DEFAULT '',
                    rule_id TEXT NOT NULL,
                    ruleset TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    first_detected_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT,
                    alert_sent_at TEXT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    recommended_action TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def get_task_state(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def count_task_states(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM task_state").fetchone()
            return int(row["count"])

    def clear_task_state(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM task_state")
            conn.execute("DELETE FROM task_status_history")
            conn.execute("DELETE FROM slack_threads")
            conn.execute("DELETE FROM dedupe_events WHERE kind IN ('task_status_transition', 'notion_poll')")
            conn.execute("DELETE FROM kv WHERE key IN ('notion_tasks_last_processed', 'notion_tasks_baseline_initialized')")

    def upsert_task_state(
        self,
        *,
        task_id: str,
        name: str,
        owner_name: str,
        owner_notion_user_id: str,
        owner_email: str,
        status: str,
        deadline: str | None,
        original_deadline: str | None,
        last_edited_time: str | None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_state (
                    task_id, name, owner_name, owner_notion_user_id, owner_email, status,
                    deadline, original_deadline, last_edited_time, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    name=excluded.name,
                    owner_name=excluded.owner_name,
                    owner_notion_user_id=excluded.owner_notion_user_id,
                    owner_email=excluded.owner_email,
                    status=excluded.status,
                    deadline=excluded.deadline,
                    original_deadline=excluded.original_deadline,
                    last_edited_time=excluded.last_edited_time,
                    updated_at=excluded.updated_at
                """,
                (
                    task_id,
                    name,
                    owner_name,
                    owner_notion_user_id,
                    owner_email,
                    status,
                    deadline,
                    original_deadline,
                    last_edited_time,
                    utc_now_iso(),
                ),
            )

    def record_status_transition(
        self,
        *,
        task_id: str,
        from_status: str | None,
        to_status: str,
        notion_last_edited_time: str | None,
        dedupe_key: str,
    ) -> bool:
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO task_status_history (
                        task_id, from_status, to_status, notion_last_edited_time,
                        processed_at, dedupe_key
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, from_status, to_status, notion_last_edited_time, utc_now_iso(), dedupe_key),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def has_status_transition(self, dedupe_key: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM task_status_history WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            return row is not None

    def attach_slack_thread_to_transition(self, dedupe_key: str, channel: str, ts: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE task_status_history SET slack_channel = ?, slack_ts = ? WHERE dedupe_key = ?",
                (channel, ts, dedupe_key),
            )

    def save_slack_thread(self, task_id: str, status: str, channel: str, ts: str, dedupe_key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO slack_threads (task_id, status, channel, ts, dedupe_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, status, channel, ts, dedupe_key, utc_now_iso()),
            )

    def get_latest_slack_thread(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM slack_threads WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_transitions_missing_slack(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_status_history
                WHERE slack_ts IS NULL
                ORDER BY processed_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def has_task_reminder(self, reminder_key: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM task_reminders WHERE reminder_key = ?", (reminder_key,)).fetchone()
            return row is not None

    def record_task_reminder_sent(
        self,
        *,
        reminder_key: str,
        task_id: str,
        reminder_type: str,
        owner_notion_user_id: str,
        slack_user_id: str,
        deadline_at: str,
        sent_at: str,
    ) -> bool:
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO task_reminders (
                        reminder_key, task_id, reminder_type, owner_notion_user_id,
                        slack_user_id, deadline_at, sent_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (reminder_key, task_id, reminder_type, owner_notion_user_id, slack_user_id, deadline_at, sent_at),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_status_transition_counts(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    task_id,
                    to_status,
                    COUNT(*) AS transition_count,
                    MAX(processed_at) AS last_processed_at
                FROM task_status_history
                GROUP BY task_id, to_status
                ORDER BY last_processed_at DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_recent_run_logs(self, run_type: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_run_logs
                WHERE run_type = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (run_type, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def increment_delay_count(self, task_id: str) -> int:
        return self._increment_counter(task_id, "delay_count")

    def get_delay_count(self, task_id: str) -> int:
        return self._get_counter(task_id, "delay_count")

    def increment_verification_flag_count(self, task_id: str) -> int:
        return self._increment_counter(task_id, "verification_flag_count")

    def get_verification_flag_count(self, task_id: str) -> int:
        return self._get_counter(task_id, "verification_flag_count")

    def _increment_counter(self, task_id: str, column: str) -> int:
        if column not in {"delay_count", "verification_flag_count"}:
            raise ValueError("invalid counter")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_counters (task_id, delay_count, verification_flag_count, updated_at)
                VALUES (?, 0, 0, ?)
                ON CONFLICT(task_id) DO NOTHING
                """,
                (task_id, utc_now_iso()),
            )
            conn.execute(
                f"UPDATE task_counters SET {column} = {column} + 1, updated_at = ? WHERE task_id = ?",
                (utc_now_iso(), task_id),
            )
            row = conn.execute(f"SELECT {column} FROM task_counters WHERE task_id = ?", (task_id,)).fetchone()
            return int(row[column])

    def _get_counter(self, task_id: str, column: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(f"SELECT {column} FROM task_counters WHERE task_id = ?", (task_id,)).fetchone()
            return int(row[column]) if row else 0

    def mark_dedupe(self, key: str, kind: str) -> bool:
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO dedupe_events (key, kind, created_at) VALUES (?, ?, ?)",
                    (key, kind, utc_now_iso()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def has_dedupe(self, key: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM dedupe_events WHERE key = ?", (key,)).fetchone()
            return row is not None

    def get_kv(self, key: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_kv(self, key: str, value: str | None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utc_now_iso()),
            )

    def upsert_owner_mapping(self, mapping_key: str, owner_name: str, slack_user_id: str, email: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO owner_mappings (mapping_key, owner_name, slack_user_id, email, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mapping_key) DO UPDATE SET
                    owner_name=excluded.owner_name,
                    slack_user_id=excluded.slack_user_id,
                    email=excluded.email,
                    updated_at=excluded.updated_at
                """,
                (mapping_key, owner_name, slack_user_id, email, utc_now_iso()),
            )

    def get_slack_user_for_owner(self, keys: list[str]) -> str | None:
        keys = [key for key in keys if key]
        if not keys:
            return None
        placeholders = ",".join("?" for _ in keys)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT slack_user_id FROM owner_mappings WHERE mapping_key IN ({placeholders}) LIMIT 1",
                keys,
            ).fetchone()
            return str(row["slack_user_id"]) if row else None

    def log_run_start(self, run_type: str, details: dict[str, Any] | None = None) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO agent_run_logs (run_type, status, started_at, details_json) VALUES (?, ?, ?, ?)",
                (run_type, "started", utc_now_iso(), json.dumps(details or {})),
            )
            return int(cur.lastrowid)

    def log_run_complete(self, run_id: int, status: str, details: dict[str, Any] | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE agent_run_logs SET status = ?, completed_at = ?, details_json = ? WHERE id = ?",
                (status, utc_now_iso(), json.dumps(details or {}), run_id),
            )

    def mark_campaign_flag(self, campaign_id: str, flag_type: str, details: dict[str, Any] | None = None) -> bool:
        dedupe_key = f"campaign:{campaign_id}:{flag_type}"
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO campaign_flags (campaign_id, flag_type, dedupe_key, created_at, details_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (campaign_id, flag_type, dedupe_key, utc_now_iso(), json.dumps(details or {})),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def upsert_service_titan_violation(
        self,
        *,
        violation_key: str,
        service_titan_job_id: str,
        appointment_id: str,
        technician_id: str,
        technician_name: str,
        dispatcher_id: str,
        dispatcher_name: str,
        rule_id: str,
        ruleset: str,
        severity: str,
        title: str,
        description: str,
        recommended_action: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO service_titan_audit_violations (
                    violation_key, service_titan_job_id, appointment_id, technician_id,
                    technician_name, dispatcher_id, dispatcher_name, rule_id, ruleset,
                    severity, status, first_detected_at, last_seen_at, resolved_at,
                    title, description, recommended_action, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL, ?, ?, ?, ?)
                ON CONFLICT(violation_key) DO UPDATE SET
                    service_titan_job_id=excluded.service_titan_job_id,
                    appointment_id=excluded.appointment_id,
                    technician_id=excluded.technician_id,
                    technician_name=excluded.technician_name,
                    dispatcher_id=excluded.dispatcher_id,
                    dispatcher_name=excluded.dispatcher_name,
                    rule_id=excluded.rule_id,
                    ruleset=excluded.ruleset,
                    severity=excluded.severity,
                    status='open',
                    last_seen_at=excluded.last_seen_at,
                    resolved_at=NULL,
                    title=excluded.title,
                    description=excluded.description,
                    recommended_action=excluded.recommended_action,
                    metadata_json=excluded.metadata_json
                """,
                (
                    violation_key,
                    service_titan_job_id,
                    appointment_id,
                    technician_id,
                    technician_name,
                    dispatcher_id,
                    dispatcher_name,
                    rule_id,
                    ruleset,
                    severity,
                    now,
                    now,
                    title,
                    description,
                    recommended_action,
                    json.dumps(metadata),
                ),
            )
            row = conn.execute(
                "SELECT * FROM service_titan_audit_violations WHERE violation_key = ?",
                (violation_key,),
            ).fetchone()
            return dict(row)

    def mark_service_titan_alert_sent(self, violation_key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE service_titan_audit_violations SET alert_sent_at = ? WHERE violation_key = ?",
                (utc_now_iso(), violation_key),
            )

    def resolve_service_titan_violation(self, violation_key: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE service_titan_audit_violations
                SET status = 'resolved', resolved_at = ?, last_seen_at = ?
                WHERE violation_key = ? AND status != 'resolved'
                """,
                (utc_now_iso(), utc_now_iso(), violation_key),
            )
            return cur.rowcount > 0

    def get_service_titan_violation(self, violation_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM service_titan_audit_violations WHERE violation_key = ?",
                (violation_key,),
            ).fetchone()
            return dict(row) if row else None

    def get_service_titan_violation_summary(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved_count,
                    SUM(CASE WHEN alert_sent_at IS NOT NULL THEN 1 ELSE 0 END) AS alert_sent_count,
                    SUM(CASE WHEN status = 'open' AND alert_sent_at IS NULL THEN 1 ELSE 0 END) AS open_unsent_count
                FROM service_titan_audit_violations
                """
            ).fetchone()
            by_rule = conn.execute(
                """
                SELECT
                    rule_id,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN alert_sent_at IS NOT NULL THEN 1 ELSE 0 END) AS alert_sent_count,
                    SUM(CASE WHEN status = 'open' AND alert_sent_at IS NULL THEN 1 ELSE 0 END) AS open_unsent_count,
                    MAX(last_seen_at) AS last_seen_at
                FROM service_titan_audit_violations
                GROUP BY rule_id
                ORDER BY last_seen_at DESC
                """
            ).fetchall()
            latest = conn.execute(
                """
                SELECT
                    violation_key,
                    service_titan_job_id,
                    appointment_id,
                    rule_id,
                    status,
                    first_detected_at,
                    last_seen_at,
                    resolved_at,
                    alert_sent_at
                FROM service_titan_audit_violations
                ORDER BY last_seen_at DESC
                LIMIT 10
                """
            ).fetchall()
        return {
            "totals": dict(totals) if totals else {},
            "by_rule": [dict(row) for row in by_rule],
            "latest": [dict(row) for row in latest],
        }

    def get_service_titan_violations_between(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    violation_key,
                    service_titan_job_id,
                    rule_id,
                    ruleset,
                    severity,
                    status,
                    first_detected_at,
                    last_seen_at,
                    resolved_at,
                    alert_sent_at,
                    metadata_json
                FROM service_titan_audit_violations
                WHERE first_detected_at >= ? AND first_detected_at < ?
                ORDER BY first_detected_at ASC, ruleset ASC, rule_id ASC
                """,
                (start_iso, end_iso),
            ).fetchall()
            return [dict(row) for row in rows]

    def ping(self) -> bool:
        try:
            with self._lock, self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
