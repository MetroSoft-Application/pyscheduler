from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import RunHistoryDetail, RunHistoryEntry, RunHistorySummary, TaskDefinition

SCHEMA_VERSION = 1

_SQL_CREATE_META = """
CREATE TABLE IF NOT EXISTS db_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)"""

_SQL_CREATE_SUMMARIES = """
CREATE TABLE IF NOT EXISTS history_summaries (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    task_name        TEXT NOT NULL DEFAULT '',
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'unknown',
    exit_code        INTEGER,
    duration_seconds REAL,
    trigger          TEXT NOT NULL DEFAULT 'scheduler',
    attempt_count    INTEGER NOT NULL DEFAULT 1,
    has_stdout       INTEGER NOT NULL DEFAULT 0,
    has_stderr       INTEGER NOT NULL DEFAULT 0
)"""

_SQL_CREATE_DETAILS = """
CREATE TABLE IF NOT EXISTS history_details (
    history_id TEXT PRIMARY KEY,
    stdout     TEXT NOT NULL DEFAULT '',
    stderr     TEXT NOT NULL DEFAULT ''
)"""

_SQL_IDX_TASK = """
CREATE INDEX IF NOT EXISTS idx_hs_task_started
    ON history_summaries (task_id, started_at DESC)"""

_SQL_IDX_ALL = """
CREATE INDEX IF NOT EXISTS idx_hs_started
    ON history_summaries (started_at DESC)"""


class Storage:
    """tasks.json と履歴 SQLite DB を管理するストレージクラス"""

    def __init__(self, data_dir: Path) -> None:
        self._tasks_path = data_dir / 'tasks.json'
        self._db_path = data_dir / 'history.db'
        self._lock = threading.RLock()
        data_dir.mkdir(parents=True, exist_ok=True)
        if not self._tasks_path.exists():
            self._save_raw({'schema_version': SCHEMA_VERSION, 'tasks': []})
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._setup_db()

    def close(self) -> None:
        """DB 接続を閉じる。アプリ終了時に呼び出すこと。"""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---- タスク CRUD ----

    def _load_raw(self) -> dict[str, Any]:
        if not self._tasks_path.exists():
            return {'schema_version': SCHEMA_VERSION, 'tasks': []}
        try:
            return json.loads(self._tasks_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return {'schema_version': SCHEMA_VERSION, 'tasks': []}

    def _save_raw(self, data: dict[str, Any]) -> None:
        tmp = self._tasks_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self._tasks_path)

    def list_tasks(self) -> list[TaskDefinition]:
        with self._lock:
            return [self._task_from_dict(t) for t in self._load_raw().get('tasks', [])]

    def get_task(self, task_id: str) -> TaskDefinition | None:
        with self._lock:
            for t in self._load_raw().get('tasks', []):
                if t.get('id') == task_id:
                    return self._task_from_dict(t)
        return None

    def save_task(self, task: TaskDefinition) -> None:
        """追加 or 更新（id が存在すれば更新、なければ追加）"""
        with self._lock:
            data = self._load_raw()
            tasks: list[dict] = data.get('tasks', [])
            for i, t in enumerate(tasks):
                if t.get('id') == task.id:
                    tasks[i] = self._task_to_dict(task)
                    data['tasks'] = tasks
                    self._save_raw(data)
                    return
            tasks.append(self._task_to_dict(task))
            data['tasks'] = tasks
            self._save_raw(data)

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            data = self._load_raw()
            data['tasks'] = [t for t in data.get('tasks', []) if t.get('id') != task_id]
            self._save_raw(data)

    def reorder_task(self, task_id: str, delta: int) -> bool:
        """タスクを delta 分移動する（-1=上, +1=下）。移動できた場合 True を返す"""
        with self._lock:
            data = self._load_raw()
            tasks: list[dict] = data.get('tasks', [])
            idx = next((i for i, t in enumerate(tasks) if t.get('id') == task_id), None)
            if idx is None:
                return False
            new_idx = idx + delta
            if new_idx < 0 or new_idx >= len(tasks):
                return False
            tasks[idx], tasks[new_idx] = tasks[new_idx], tasks[idx]
            data['tasks'] = tasks
            self._save_raw(data)
            return True

    # ---- DB セットアップ ----

    def _setup_db(self) -> None:
        with self._lock:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA synchronous=NORMAL')
            self._conn.execute(_SQL_CREATE_META)
            self._conn.execute(_SQL_CREATE_SUMMARIES)
            self._conn.execute(_SQL_CREATE_DETAILS)
            self._conn.execute(_SQL_IDX_TASK)
            self._conn.execute(_SQL_IDX_ALL)

    # ---- 実行履歴 ----

    def append_history(self, entry: RunHistoryEntry) -> None:
        self._upsert_history(entry)

    def update_history(self, entry: RunHistoryEntry) -> None:
        """history_id が一致する行を更新する"""
        self._upsert_history(entry)

    def close_stale_running_entries(
        self, known_history_ids: set[str], finished_at: str
    ) -> int:
        """known_history_ids に含まれない status='running' のレコードを unknown_exit で閉じる。
        アプリクラッシュ後の幽霊レコードを起動時にクリーンアップするために使用する。
        更新したレコード件数を返す。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM history_summaries WHERE status = 'running'"
            ).fetchall()
            stale = [row['id'] for row in rows if row['id'] not in known_history_ids]
            if not stale:
                return 0
            self._conn.execute('BEGIN')
            self._conn.executemany(
                "UPDATE history_summaries SET status = 'unknown_exit', finished_at = ? WHERE id = ?",
                [(finished_at, hid) for hid in stale],
            )
            self._conn.execute('COMMIT')
        return len(stale)

    def _upsert_history(self, entry: RunHistoryEntry) -> None:
        has_stdout = bool(entry.stdout) or entry.has_stdout
        has_stderr = bool(entry.stderr) or entry.has_stderr
        with self._lock:
            self._conn.execute('BEGIN')
            self._conn.execute(
                """INSERT OR REPLACE INTO history_summaries
                   (id, task_id, task_name, started_at, finished_at, status,
                    exit_code, duration_seconds, trigger, attempt_count,
                    has_stdout, has_stderr)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry.id, entry.task_id, entry.task_name or '',
                    entry.started_at, entry.finished_at, entry.status,
                    entry.exit_code, entry.duration_seconds,
                    entry.trigger, entry.attempt_count,
                    1 if has_stdout else 0,
                    1 if has_stderr else 0,
                ),
            )
            if entry.stdout or entry.stderr:
                self._conn.execute(
                    'INSERT OR REPLACE INTO history_details (history_id, stdout, stderr) VALUES (?,?,?)',
                    (entry.id, entry.stdout, entry.stderr),
                )
            self._conn.execute('COMMIT')

    def list_history(
        self,
        task_id: str,
        limit: int = 100,
    ) -> list[RunHistorySummary]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM history_summaries
                   WHERE task_id = ?
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (task_id, limit),
            ).fetchall()
        return [self._summary_from_row(row) for row in rows]

    def get_last_history_all(self) -> dict[str, RunHistorySummary]:
        """全タスクの最終実行履歴を {task_id: RunHistorySummary} で一括返却する"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM (
                       SELECT *,
                              ROW_NUMBER() OVER (
                                  PARTITION BY task_id
                                  ORDER BY started_at DESC, rowid DESC
                              ) AS rn
                       FROM history_summaries
                   ) WHERE rn = 1"""
            ).fetchall()
        return {row['task_id']: self._summary_from_row(row) for row in rows}

    def list_all_recent_history(
        self,
        limit: int = 50,
    ) -> list[RunHistorySummary]:
        """全タスクの実行履歴を開始時刻の降順で返す"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM history_summaries
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._summary_from_row(row) for row in rows]

    def get_history_detail(self, task_id: str, history_id: str) -> RunHistoryDetail:
        with self._lock:
            row = self._conn.execute(
                'SELECT stdout, stderr FROM history_details WHERE history_id = ?',
                (history_id,),
            ).fetchone()
        if row is None:
            return RunHistoryDetail(stdout='', stderr='')
        return RunHistoryDetail(stdout=row['stdout'], stderr=row['stderr'])

    def get_history_entry(self, task_id: str, history_id: str) -> RunHistoryEntry | None:
        with self._lock:
            s_row = self._conn.execute(
                'SELECT * FROM history_summaries WHERE id = ?',
                (history_id,),
            ).fetchone()
            if s_row is None:
                return None
            d_row = self._conn.execute(
                'SELECT stdout, stderr FROM history_details WHERE history_id = ?',
                (history_id,),
            ).fetchone()
        stdout = d_row['stdout'] if d_row else ''
        stderr = d_row['stderr'] if d_row else ''
        return self._entry_from_row(s_row, stdout, stderr)

    def delete_history_entry(self, task_id: str, history_id: str) -> bool:
        with self._lock:
            self._conn.execute('BEGIN')
            cur = self._conn.execute(
                'DELETE FROM history_summaries WHERE id = ?', (history_id,)
            )
            self._conn.execute(
                'DELETE FROM history_details WHERE history_id = ?', (history_id,)
            )
            self._conn.execute('COMMIT')
        return cur.rowcount > 0

    # ---- DB 行 -> モデル変換 ----

    def _summary_from_row(self, row: sqlite3.Row) -> RunHistorySummary:
        return RunHistorySummary(
            id=row['id'],
            task_id=row['task_id'],
            task_name=row['task_name'] or '',
            started_at=row['started_at'],
            finished_at=row['finished_at'],
            status=row['status'],
            exit_code=row['exit_code'],
            duration_seconds=row['duration_seconds'],
            trigger=row['trigger'],
            attempt_count=int(row['attempt_count']),
            has_stdout=bool(row['has_stdout']),
            has_stderr=bool(row['has_stderr']),
        )

    def _entry_from_row(self, row: sqlite3.Row, stdout: str, stderr: str) -> RunHistoryEntry:
        return RunHistoryEntry(
            id=row['id'],
            task_id=row['task_id'],
            task_name=row['task_name'] or '',
            started_at=row['started_at'],
            finished_at=row['finished_at'],
            status=row['status'],
            exit_code=row['exit_code'],
            duration_seconds=row['duration_seconds'],
            trigger=row['trigger'],
            attempt_count=int(row['attempt_count']),
            has_stdout=bool(row['has_stdout']),
            has_stderr=bool(row['has_stderr']),
            stdout=stdout,
            stderr=stderr,
        )

    # ---- 変換ヘルパー ----

    def _task_to_dict(self, task: TaskDefinition) -> dict[str, Any]:
        return {
            'id': task.id,
            'name': task.name,
            'description': task.description,
            'enabled': task.enabled,
            'runtime': task.runtime,
            'command_text': task.command_text,
            'working_directory': task.working_directory,
            'run_as_admin': task.run_as_admin,
            'timeout_seconds': task.timeout_seconds,
            'retry_count': task.retry_count,
            'retry_delay_seconds': task.retry_delay_seconds,
            'environment': task.environment,
            'schedule': task.schedule,
            'created_at': task.created_at,
            'updated_at': task.updated_at,
        }

    def _task_from_dict(self, d: dict[str, Any]) -> TaskDefinition:
        return TaskDefinition(
            id=d['id'],
            name=d.get('name', ''),
            description=d.get('description', ''),
            enabled=d.get('enabled', True),
            runtime=d.get('runtime', 'auto'),
            command_text=d.get('command_text', ''),
            working_directory=d.get('working_directory', ''),
            run_as_admin=bool(d.get('run_as_admin', False)),
            timeout_seconds=d.get('timeout_seconds'),
            retry_count=int(d.get('retry_count', 0)),
            retry_delay_seconds=int(d.get('retry_delay_seconds', 0)),
            environment=d.get('environment') or {},
            schedule=d.get('schedule') or {'type': 'interval', 'hours': 1},
            created_at=d.get('created_at', ''),
            updated_at=d.get('updated_at', ''),
        )


