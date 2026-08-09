import os
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
from autoswe.models import TaskStatus, RiskLevel


class StorageEngine:
    def __init__(self, db_path: str = "autoswe.db", storage_dir: str = "artifacts"):
        self.db_path = db_path
        self.storage_dir = os.path.abspath(storage_dir)
        self.artifact_dir = self.storage_dir
        os.makedirs(self.artifact_dir, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _row_to_task_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        status_str = row["status"]
        risk_str = row["risk_level"]
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "title": row["title"],
            "description": row["description"],
            "status": TaskStatus(status_str) if status_str in TaskStatus._value2member_map_ else status_str,
            "assigned_agent": row["assigned_agent"],
            "dependencies": json.loads(row["dependencies"] or "[]"),
            "risk_level": RiskLevel(risk_str) if risk_str in RiskLevel._value2member_map_ else risk_str,
            "metadata": json.loads(row["metadata"] or "{}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _resolve_artifact_path(self, key: str) -> str:
        resolved_path = os.path.abspath(os.path.join(self.artifact_dir, key))
        base_dir = self.artifact_dir
        if os.path.commonpath([base_dir, resolved_path]) != base_dir:
            raise ValueError(f"Path traversal detected for key: {key}")
        return resolved_path

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    assigned_agent TEXT,
                    dependencies TEXT DEFAULT '[]',
                    risk_level TEXT DEFAULT 'low',
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id)
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    key TEXT PRIMARY KEY,
                    result TEXT DEFAULT 'null',
                    status TEXT DEFAULT 'completed',
                    created_at TEXT NOT NULL
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT DEFAULT '{}',
                    timestamp TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def create_project(
        self,
        project_id: str,
        name: str,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        meta_dict = metadata or {}
        created_at = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO projects (id, name, description, metadata, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, name, description, json.dumps(meta_dict), created_at),
            )
            conn.commit()

        return {
            "id": project_id,
            "name": name,
            "description": description,
            "metadata": meta_dict,
            "created_at": created_at,
        }

    def create_task(
        self,
        task_id: str,
        project_id: str,
        title: str,
        description: str = "",
        status: Union[str, TaskStatus] = TaskStatus.PENDING,
        assigned_agent: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
        risk_level: Union[str, RiskLevel] = RiskLevel.LOW,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        status_str = status.value if isinstance(status, TaskStatus) else str(status)
        risk_str = risk_level.value if isinstance(risk_level, RiskLevel) else str(risk_level)
        deps_list = dependencies or []
        meta_dict = metadata or {}
        now = datetime.now(timezone.utc).isoformat()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO tasks (
                    id, project_id, title, description, status,
                    assigned_agent, dependencies, risk_level, metadata,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    project_id,
                    title,
                    description,
                    status_str,
                    assigned_agent,
                    json.dumps(deps_list),
                    risk_str,
                    json.dumps(meta_dict),
                    now,
                    now,
                ),
            )
            conn.commit()

        return {
            "id": task_id,
            "project_id": project_id,
            "title": title,
            "description": description,
            "status": TaskStatus(status_str) if status_str in TaskStatus._value2member_map_ else status_str,
            "assigned_agent": assigned_agent,
            "dependencies": deps_list,
            "risk_level": RiskLevel(risk_str) if risk_str in RiskLevel._value2member_map_ else risk_str,
            "metadata": meta_dict,
            "created_at": now,
            "updated_at": now,
        }

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_task_dict(row)

    def update_task_state(
        self,
        task_id: str,
        status: Union[str, TaskStatus],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        status_str = status.value if isinstance(status, TaskStatus) else str(status)
        now = datetime.now(timezone.utc).isoformat()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if row is None:
                return None

            updated_meta = json.loads(row["metadata"] or "{}")
            if metadata:
                updated_meta.update(metadata)

            cursor.execute(
                """
                UPDATE tasks
                SET status = ?, metadata = ?, updated_at = ?
                WHERE id = ?
                """,
                (status_str, json.dumps(updated_meta), now, task_id),
            )
            conn.commit()

            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            updated_row = cursor.fetchone()

        if updated_row is None:
            return None

        return self._row_to_task_dict(updated_row)

    def save_artifact(self, key: str, content: Union[str, bytes]) -> str:
        filepath = self._resolve_artifact_path(key)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        if isinstance(content, bytes):
            with open(filepath, "wb") as f:
                f.write(content)
        else:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

        return filepath

    def read_artifact(self, key: str, is_binary: bool = False) -> Union[str, bytes]:
        filepath = self._resolve_artifact_path(key)
        try:
            if is_binary:
                with open(filepath, "rb") as f:
                    return f.read()
            else:
                with open(filepath, "r", encoding="utf-8") as f:
                    return f.read()
        except FileNotFoundError:
            raise ValueError(f"Artifact not found: {key}")

    def save_idempotency_record(
        self, key: str, result: Any, status: str = "completed"
    ) -> Dict[str, Any]:
        created_at = datetime.now(timezone.utc).isoformat()
        result_json = json.dumps(result)

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO idempotency_records (key, result, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (key, result_json, status, created_at),
            )
            conn.commit()

        return {
            "key": key,
            "result": result,
            "status": status,
            "created_at": created_at,
        }

    def get_idempotency_record(self, key: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM idempotency_records WHERE key = ?", (key,))
            row = cursor.fetchone()

        if row is None:
            return None

        return {
            "key": row["key"],
            "result": json.loads(row["result"] or "null"),
            "status": row["status"],
            "created_at": row["created_at"],
        }

    def log_audit_event(
        self, event_type: str, actor: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        payload_dict = payload or {}
        timestamp = datetime.now(timezone.utc).isoformat()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO audit_logs (event_type, actor, payload, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (event_type, actor, json.dumps(payload_dict), timestamp),
            )
            conn.commit()
            log_id = cursor.lastrowid

        return {
            "id": log_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload_dict,
            "timestamp": timestamp,
        }
