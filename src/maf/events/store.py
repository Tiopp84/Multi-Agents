import json
import sqlite3
import time
from datetime import datetime, timezone


NOTIFICATIONS = {"approval.requested", "run.finished", "error", "blocked", "task.done"}


class EventStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                run_id TEXT NOT NULL,
                spec_version INTEGER NOT NULL,
                agent TEXT,
                task_id TEXT,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
        """)
        self.connection.execute("CREATE INDEX IF NOT EXISTS events_run_id ON events(run_id, id)")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS outbox (
                event_id INTEGER PRIMARY KEY,
                delivered INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS telegram_updates (
                owner TEXT NOT NULL,
                update_id INTEGER NOT NULL,
                PRIMARY KEY (owner, update_id)
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS telegram_runs (
                request TEXT PRIMARY KEY,
                run_id TEXT NOT NULL
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS notification_claims (
                event_id INTEGER PRIMARY KEY,
                expires_at REAL NOT NULL
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS pm_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                run_id TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL
            )
        """)
        self.connection.execute("CREATE INDEX IF NOT EXISTS pm_turns_owner ON pm_turns(owner, id)")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS pm_sessions (
                owner TEXT PRIMARY KEY
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                workspace TEXT UNIQUE NOT NULL,
                brief TEXT NOT NULL
            )
        """)
        self.project_attached = False
        self.connection.commit()

    def attach_project(self, database):
        if database.is_symlink():
            raise ValueError("Project events database cannot be a symbolic link")
        self.connection.execute("ATTACH DATABASE ? AS project", (str(database),))
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS project.events (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                run_id TEXT NOT NULL,
                spec_version INTEGER NOT NULL,
                agent TEXT,
                task_id TEXT,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
        """)
        self.project_attached = True

    def run_project(self, run_id: str):
        row = self.connection.execute("""
            SELECT json_extract(payload_json, '$.project_id') FROM events
            WHERE run_id = ? AND type = 'run.created' ORDER BY id LIMIT 1
        """, (run_id,)).fetchone()
        return row[0] if row else None

    def append(self, run_id, spec_version, event_type, payload, *, agent=None, task_id=None):
        with self.connection:
            inserted = self.connection.execute(
                """
                INSERT INTO events(ts, run_id, spec_version, agent, task_id, type, payload_json)
                SELECT ?, ?, ?, ?, ?, ?, ?
                WHERE ? IS NULL OR NOT EXISTS (
                    SELECT 1 FROM events
                    WHERE run_id = ? AND type = ? AND json_extract(payload_json, '$.gate_id') = ?
                )
                """,
                (
                    datetime.now(timezone.utc).isoformat(), run_id, spec_version,
                    agent, task_id, event_type, json.dumps(payload, ensure_ascii=False),
                    payload.get("gate_id"), run_id, event_type, payload.get("gate_id"),
                ),
            )

            if inserted.rowcount and event_type in NOTIFICATIONS:
                self.connection.execute("INSERT INTO outbox(event_id) VALUES (?)", (inserted.lastrowid,))
            if inserted.rowcount and self.project_attached:
                self.connection.execute(
                    "INSERT INTO project.events SELECT * FROM main.events WHERE id = ?", (inserted.lastrowid,)
                )
            return bool(inserted.rowcount)

    def claim_telegram_update(self, owner: str, update_id: int) -> bool:
        with self.connection:
            return bool(self.connection.execute(
                "INSERT OR IGNORE INTO telegram_updates VALUES (?, ?)", (owner, update_id)
            ).rowcount)

    def reserve_telegram_run(self, request: str, run_id: str) -> str:
        with self.connection:
            self.connection.execute("""
                INSERT INTO telegram_runs(request, run_id) VALUES (?, ?)
                ON CONFLICT(request) DO UPDATE SET run_id = excluded.run_id
                WHERE EXISTS (
                    SELECT 1 FROM events WHERE run_id = telegram_runs.run_id
                    AND type IN ('run.finished', 'error')
                )
            """, (request, run_id))
            return self.connection.execute(
                "SELECT run_id FROM telegram_runs WHERE request = ?", (request,)
            ).fetchone()[0]

    def release_telegram_run(self, request: str, run_id: str):
        with self.connection:
            self.connection.execute(
                "DELETE FROM telegram_runs WHERE request = ? AND run_id = ?", (request, run_id)
            )

    def obsolete_approval(self, event: dict) -> bool:
        if event["type"] != "approval.requested":
            return False
        return self.connection.execute("""
            SELECT 1 FROM events
            WHERE run_id = ? AND id > ?
                AND type IN ('approval.resolved', 'approval.requested', 'run.finished')
            LIMIT 1
        """, (event["run_id"], event["id"])).fetchone() is not None

    def claim_notification(self, event_id: int) -> bool:
        now = time.time()
        with self.connection:
            return bool(self.connection.execute("""
                INSERT INTO notification_claims(event_id, expires_at)
                SELECT ?, ? WHERE EXISTS (
                    SELECT 1 FROM outbox WHERE event_id = ? AND delivered = 0
                )
                ON CONFLICT(event_id) DO UPDATE SET expires_at = excluded.expires_at
                WHERE notification_claims.expires_at <= ?
            """, (event_id, now + 45, event_id, now)).rowcount)

    def get(self, event_id: int):
        row = self.connection.execute(
            "SELECT id, ts, spec_version, agent, task_id, type, payload_json, run_id "
            "FROM events WHERE id = ?", (event_id,),
        ).fetchone()
        return self._event(row) if row else None

    def delivered(self, event_id: int):
        row = self.connection.execute(
            "SELECT delivered FROM outbox WHERE event_id = ?", (event_id,),
        ).fetchone()
        return bool(row and row[0])

    def confirm_delivery(self, event_id: int):
        with self.connection:
            self.connection.execute("UPDATE outbox SET delivered = 1 WHERE event_id = ?", (event_id,))
            self.connection.execute("DELETE FROM notification_claims WHERE event_id = ?", (event_id,))

    @staticmethod
    def _event(row):
        event_id, ts, version, agent, task_id, event_type, payload, run_id = row
        return dict(id=event_id, ts=ts, run_id=run_id, spec_version=version, agent=agent,
                    task_id=task_id, type=event_type, payload=json.loads(payload))

    def replay(self, run_id: str, after: int = 0):
        rows = self.connection.execute(
            """
            SELECT id, ts, spec_version, agent, task_id, type, payload_json
            FROM events WHERE run_id = ? AND id > ? ORDER BY id
            """,
            (run_id, after),
        )
        return [self._event((*row, run_id)) for row in rows]

    def pm_is_open(self, owner: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM pm_sessions WHERE owner = ?", (owner,)
        ).fetchone() is not None

    def set_pm_open(self, owner: str, enabled: bool):
        with self.connection:
            if enabled:
                self.connection.execute("INSERT OR IGNORE INTO pm_sessions VALUES (?)", (owner,))
            else:
                self.connection.execute("DELETE FROM pm_sessions WHERE owner = ?", (owner,))

    def pm_history(self, owner: str):
        rows = self.connection.execute("""
            SELECT question, answer FROM pm_turns WHERE owner = ?
            ORDER BY id DESC LIMIT 8
        """, (owner,)).fetchall()
        return [{"question": question, "answer": answer} for question, answer in reversed(rows)]

    def record_pm_turn(self, owner: str, question: str, answer: str):
        with self.connection:
            self.connection.execute(
                "INSERT INTO pm_turns(owner, run_id, question, answer) VALUES (?, ?, ?, ?)",
                (owner, "", question, answer),
            )

    def recent_pm_events(self, run_id: str):
        rows = self.connection.execute("""
            SELECT id, ts, spec_version, agent, task_id, type, payload_json
            FROM events WHERE run_id = ? AND type IN (
                'task.started', 'task.done', 'approval.requested', 'approval.resolved',
                'approval.feedback_requested', 'test.result', 'spec.updated', 'error', 'run.finished'
            )
            ORDER BY id DESC LIMIT 12
        """, (run_id,)).fetchall()
        return [self._event((*row, run_id)) for row in reversed(rows)]

    def dashboard_runs(self, limit: int, project_id: str | None = None):
        cursor = self.connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        rows = self.connection.execute("""
            SELECT c.run_id, json_extract(c.payload_json, '$.request'), c.ts,
                   e.ts, e.spec_version, lifecycle.type,
                   json_extract(lifecycle.payload_json, '$.status'),
                   json_extract(lifecycle.payload_json, '$.gate'),
                   task.agent, json_extract(task.payload_json, '$.node'),
                   json_extract(c.payload_json, '$.project_id')
            FROM events AS c
            JOIN events AS e ON e.id = (
                SELECT MAX(id) FROM events WHERE run_id = c.run_id AND id <= ?
            )
            LEFT JOIN events AS lifecycle ON lifecycle.id = (
                SELECT MAX(id) FROM events WHERE run_id = c.run_id AND id <= ?
                AND type IN ('approval.requested', 'approval.resolved', 'run.finished', 'error', 'run.updated', 'blocked', 'run.paused')
            )
            LEFT JOIN events AS task ON task.id = (
                SELECT MAX(id) FROM events WHERE run_id = c.run_id AND id <= ?
                AND type = 'task.started' AND agent IS NOT NULL
            )
            WHERE c.type = 'run.created' AND c.id <= ?
                AND json_extract(c.payload_json, '$.project_id') IS NOT NULL
                AND (? IS NULL OR COALESCE(json_extract(c.payload_json, '$.project_id'), '') = ?)
            ORDER BY e.id DESC LIMIT ?
        """, (cursor, cursor, cursor, cursor, project_id, project_id, limit)).fetchall()
        statuses = {"approval.requested": "waiting_approval", "error": "failed", "blocked": "blocked", "run.paused": "paused"}
        runs = [
            dict(run_id=run_id, project_id=project, request=request, created_at=created, updated_at=updated,
                 spec_version=version, status=terminal or statuses.get(event_type, "running"),
                 gate=gate if event_type == "approval.requested" else None, agent=agent, node=node)
            for run_id, request, created, updated, version, event_type, terminal, gate, agent, node, project in rows
        ]
        return {"cursor": cursor, "runs": runs}

    def dashboard_changes(self, after: int):
        rows = self.connection.execute(
            """SELECT e.id, e.run_id FROM events AS e WHERE e.id > ?
            AND EXISTS (
                SELECT 1 FROM events AS c WHERE c.run_id=e.run_id AND c.type='run.created'
                AND json_extract(c.payload_json, '$.project_id') IS NOT NULL
            ) ORDER BY e.id LIMIT 200""", (after,)
        )
        return [{"id": event_id, "run_id": run_id} for event_id, run_id in rows]

    def last_completed_agent(self, run_id: str, spec_version: int):
        row = self.connection.execute("""
            SELECT agent FROM events
            WHERE run_id = ? AND spec_version = ? AND type = 'task.done' AND agent IS NOT NULL
            ORDER BY id DESC LIMIT 1
        """, (run_id, spec_version)).fetchone()
        return row[0] if row else None
