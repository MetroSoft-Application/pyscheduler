from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import RunHistoryDetail, RunHistoryEntry, RunHistorySummary, TaskDefinition

SCHEMA_VERSION = 1

# レガシー JSONL 読み取り用（マイグレーション時のみ使用）
_HISTORY_STDOUT_FIELD_RE = re.compile(r',\s*"stdout"\s*:')
_HISTORY_DURATION_FIELD_RE = re.compile(r'"duration_seconds"\s*:')
_LEGACY_SUMMARY_SCAN_BYTES = 64 * 1024
_LEGACY_ID_SCAN_BYTES = 4 * 1024
_LEGACY_LARGE_LINE_THRESHOLD = 256 * 1024

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
        # マイグレーション元ディレクトリ（既存データ読み取り専用）
        self._history_dir = data_dir / 'history'
        self._history_summary_dir = data_dir / 'history_summary'
        self._history_detail_dir = data_dir / 'history_detail'
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
        self._migrate_jsonl_if_needed()

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

    # ---- マイグレーション (JSONL -> SQLite、初回のみ) ----

    def _migrate_jsonl_if_needed(self) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM db_meta WHERE key = 'jsonl_migration_done'"
            ).fetchone()
            if row:
                return

            summaries: list[tuple] = []
            details: list[tuple] = []

            # history_summary/*.jsonl から移行
            if self._history_summary_dir.exists():
                for p in self._history_summary_dir.glob('*.jsonl'):
                    self._collect_summary_jsonl(p, summaries)

            # history_detail/*.json から移行
            if self._history_detail_dir.exists():
                for p in self._history_detail_dir.glob('*.json'):
                    self._collect_detail_json(p, details)

            # legacy history/*.jsonl から移行（大きな行はサマリーのみ）
            if self._history_dir.exists():
                for p in self._history_dir.glob('*.jsonl'):
                    self._collect_legacy_jsonl(p, summaries, details)

            self._conn.execute('BEGIN')
            self._conn.executemany(
                """INSERT OR IGNORE INTO history_summaries
                   (id, task_id, task_name, started_at, finished_at, status,
                    exit_code, duration_seconds, trigger, attempt_count,
                    has_stdout, has_stderr)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                summaries,
            )
            self._conn.executemany(
                'INSERT OR IGNORE INTO history_details (history_id, stdout, stderr) VALUES (?,?,?)',
                details,
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('jsonl_migration_done', '1')"
            )
            self._conn.execute('COMMIT')

    def _collect_summary_jsonl(self, path: Path, out: list[tuple]) -> None:
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        out.append(self._summary_dict_to_tuple(d))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        pass
        except OSError:
            pass

    def _collect_detail_json(self, path: Path, out: list[tuple]) -> None:
        history_id = path.stem
        try:
            d = json.loads(path.read_text(encoding='utf-8', errors='replace'))
            out.append((history_id, str(d.get('stdout', '')), str(d.get('stderr', ''))))
        except (json.JSONDecodeError, OSError):
            pass

    def _collect_legacy_jsonl(
        self,
        path: Path,
        summaries: list[tuple],
        details: list[tuple],
    ) -> None:
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    stripped = line.rstrip('\r\n')
                    if not stripped:
                        continue
                    if len(stripped) <= _LEGACY_LARGE_LINE_THRESHOLD:
                        # 小さい行: 全フィールドをパース
                        try:
                            d = json.loads(stripped)
                            summaries.append(self._summary_dict_to_tuple(d))
                            stdout = str(d.get('stdout', ''))
                            stderr = str(d.get('stderr', ''))
                            if stdout or stderr:
                                details.append((str(d['id']), stdout, stderr))
                        except (json.JSONDecodeError, KeyError, ValueError):
                            pass
                    else:
                        # 大きい行: サマリーフィールドのみ正規表現で抽出
                        prefix = stripped[:_LEGACY_SUMMARY_SCAN_BYTES]
                        suffix = stripped[-_LEGACY_SUMMARY_SCAN_BYTES:]
                        summary = self._extract_legacy_summary_parts(prefix, suffix)
                        if summary is not None:
                            summaries.append(self._summary_to_tuple(summary))
        except OSError:
            pass

    def _extract_legacy_summary_parts(
        self,
        prefix_text: str,
        suffix_text: str,
    ) -> RunHistorySummary | None:
        stdout_match = _HISTORY_STDOUT_FIELD_RE.search(prefix_text)
        duration_match = _HISTORY_DURATION_FIELD_RE.search(suffix_text)
        if stdout_match is None or duration_match is None:
            return None
        try:
            prefix = json.loads(prefix_text[:stdout_match.start()] + '}')
            suffix = json.loads('{' + suffix_text[duration_match.start():])
        except (json.JSONDecodeError, ValueError):
            return None
        return self._history_summary_from_dict({**prefix, **suffix, 'has_stdout': True, 'has_stderr': True})

    # ---- 実行履歴 ----

    def append_history(self, entry: RunHistoryEntry) -> None:
        self._upsert_history(entry)

    def update_history(self, entry: RunHistoryEntry) -> None:
        """history_id が一致する行を更新する"""
        self._upsert_history(entry)

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
        include_legacy: bool = False,
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

    def get_last_history_all(self, include_legacy: bool = False) -> dict[str, RunHistorySummary]:
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
        include_legacy: bool = False,
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

    # ---- マイグレーション用ヘルパー ----

    def _summary_dict_to_tuple(self, d: dict[str, Any]) -> tuple:
        return (
            str(d['id']),
            str(d['task_id']),
            str(d.get('task_name', '')),
            str(d['started_at']),
            d.get('finished_at'),
            str(d.get('status', 'unknown')),
            d.get('exit_code'),
            d.get('duration_seconds'),
            str(d.get('trigger', 'scheduler')),
            int(d.get('attempt_count', 1)),
            1 if (bool(d.get('has_stdout')) or bool(d.get('stdout'))) else 0,
            1 if (bool(d.get('has_stderr')) or bool(d.get('stderr'))) else 0,
        )

    def _summary_to_tuple(self, s: RunHistorySummary) -> tuple:
        return (
            s.id, s.task_id, s.task_name or '',
            s.started_at, s.finished_at, s.status,
            s.exit_code, s.duration_seconds, s.trigger, s.attempt_count,
            1 if s.has_stdout else 0,
            1 if s.has_stderr else 0,
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

    def _history_summary_from_dict(self, d: dict[str, Any]) -> RunHistorySummary:
        return RunHistorySummary(
            id=d['id'],
            task_id=d['task_id'],
            task_name=d.get('task_name', ''),
            started_at=d['started_at'],
            finished_at=d.get('finished_at'),
            status=d.get('status', 'unknown'),
            exit_code=d.get('exit_code'),
            duration_seconds=d.get('duration_seconds'),
            trigger=d.get('trigger', 'scheduler'),
            attempt_count=int(d.get('attempt_count', 1)),
            has_stdout=bool(d.get('has_stdout')) or bool(d.get('stdout')),
            has_stderr=bool(d.get('has_stderr')) or bool(d.get('stderr')),
        )
