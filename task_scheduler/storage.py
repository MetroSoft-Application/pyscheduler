from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from .models import RunHistoryDetail, RunHistoryEntry, RunHistorySummary, TaskDefinition

SCHEMA_VERSION = 1

_HISTORY_STDOUT_FIELD_RE = re.compile(r',\s*"stdout"\s*:')
_HISTORY_DURATION_FIELD_RE = re.compile(r'"duration_seconds"\s*:')
_SUMMARY_SCAN_BYTES = 64 * 1024
_SUMMARY_ID_SCAN_BYTES = 4 * 1024


class Storage:
    """tasks.json と history/*.jsonl を管理するストレージクラス"""

    def __init__(self, data_dir: Path) -> None:
        self._tasks_path = data_dir / 'tasks.json'
        self._history_dir = data_dir / 'history'
        self._history_summary_dir = data_dir / 'history_summary'
        self._history_detail_dir = data_dir / 'history_detail'
        self._lock = threading.RLock()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._history_dir.mkdir(parents=True, exist_ok=True)
        self._history_summary_dir.mkdir(parents=True, exist_ok=True)
        self._history_detail_dir.mkdir(parents=True, exist_ok=True)
        if not self._tasks_path.exists():
            self._save_raw({'schema_version': SCHEMA_VERSION, 'tasks': []})

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

    # ---- 実行履歴 ----

    def append_history(self, entry: RunHistoryEntry) -> None:
        path = self._summary_path(entry.task_id)
        line = json.dumps(self._history_summary_to_dict(entry), ensure_ascii=False)
        with self._lock:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        if entry.stdout or entry.stderr:
            self._write_detail(entry)

    def update_history(self, entry: RunHistoryEntry) -> None:
        """history_id が一致する行を更新する"""
        if self._update_summary_entry(entry):
            self._write_detail(entry)
            return
        if self._update_legacy_history(entry):
            return

        self.append_history(entry)

    def list_history(
        self,
        task_id: str,
        limit: int = 100,
        include_legacy: bool = False,
    ) -> list[RunHistorySummary]:
        entries = self._load_recent_summary_entries(self._summary_path(task_id), limit)
        if include_legacy:
            legacy_entries = self._load_recent_legacy_summaries(self._legacy_path(task_id), limit)
            entries.extend(legacy_entries)
            self._cache_history_summaries(task_id, legacy_entries)
        return self._merge_history_summaries(entries, limit)

    def get_last_history_all(self, include_legacy: bool = False) -> dict[str, RunHistorySummary]:
        """全タスクの最終実行履歴を {task_id: RunHistoryEntry} で一括返却する"""
        result: dict[str, RunHistorySummary] = {}
        with self._lock:
            summary_paths = {path.stem: path for path in self._history_summary_dir.glob('*.jsonl')}
            legacy_paths = {path.stem: path for path in self._history_dir.glob('*.jsonl')} if include_legacy else {}

        for task_id in sorted(set(summary_paths) | set(legacy_paths)):
            candidates: list[RunHistorySummary] = []
            summary_entry = self._load_last_summary_entry(summary_paths.get(task_id))
            if summary_entry is not None:
                candidates.append(summary_entry)
            legacy_entry = self._load_last_legacy_summary(legacy_paths.get(task_id))
            if legacy_entry is not None:
                candidates.append(legacy_entry)
            if candidates:
                result[task_id] = max(candidates, key=lambda entry: entry.started_at)
        return result

    def list_all_recent_history(
        self,
        limit: int = 50,
        include_legacy: bool = False,
    ) -> list[RunHistorySummary]:
        """全タスクの実行履歴を開始時刻の降順で返す"""
        all_entries: list[RunHistorySummary] = []
        with self._lock:
            summary_paths = {path.stem: path for path in self._history_summary_dir.glob('*.jsonl')}
            legacy_paths = {path.stem: path for path in self._history_dir.glob('*.jsonl')} if include_legacy else {}

        for task_id in sorted(set(summary_paths) | set(legacy_paths)):
            if task_id in summary_paths:
                all_entries.extend(self._load_recent_summary_entries(summary_paths[task_id], limit))
            if task_id in legacy_paths:
                all_entries.extend(self._load_recent_legacy_summaries(legacy_paths[task_id], limit))
        return self._merge_history_summaries(all_entries, limit)

    def get_history_detail(self, task_id: str, history_id: str) -> RunHistoryDetail:
        detail = self._load_detail(history_id)
        if detail is not None:
            return detail

        range_info = self._find_legacy_history_range(self._legacy_path(task_id), history_id, search_limit=None)
        if range_info is None:
            return RunHistoryDetail(stdout='', stderr='')

        entry = self._history_entry_from_legacy_range(self._legacy_path(task_id), *range_info)
        if entry is None:
            return RunHistoryDetail(stdout='', stderr='')
        return RunHistoryDetail(stdout=entry.stdout, stderr=entry.stderr)

    def get_history_entry(self, task_id: str, history_id: str) -> RunHistoryEntry | None:
        summary = self._find_summary_entry(self._summary_path(task_id), history_id)
        if summary is not None:
            detail = self._load_detail(history_id) or RunHistoryDetail(stdout='', stderr='')
            return self._to_history_entry(summary, detail)

        range_info = self._find_legacy_history_range(self._legacy_path(task_id), history_id, search_limit=None)
        if range_info is None:
            return None
        return self._history_entry_from_legacy_range(self._legacy_path(task_id), *range_info)

    def delete_history_entry(self, task_id: str, history_id: str) -> bool:
        deleted_summary = self._delete_summary_entries(self._summary_path(task_id), history_id)
        range_info = self._find_legacy_history_range(self._legacy_path(task_id), history_id, search_limit=None)
        deleted_legacy = False
        if range_info is not None:
            deleted_legacy = self._delete_file_range(self._legacy_path(task_id), *range_info)

        deleted_detail = self._delete_detail(history_id)
        return deleted_summary or deleted_legacy or deleted_detail

    def _legacy_path(self, task_id: str) -> Path:
        return self._history_dir / f'{task_id}.jsonl'

    def _summary_path(self, task_id: str) -> Path:
        return self._history_summary_dir / f'{task_id}.jsonl'

    def _detail_path(self, history_id: str) -> Path:
        return self._history_detail_dir / f'{history_id}.json'

    def _read_tail_lines(self, path: Path, max_lines: int) -> list[str]:
        if max_lines <= 0:
            return []

        with self._lock:
            try:
                with open(path, 'rb') as f:
                    f.seek(0, 2)
                    position = f.tell()
                    buffer = b''
                    newline_count = 0

                    while position > 0 and newline_count <= max_lines:
                        chunk_size = min(8192, position)
                        position -= chunk_size
                        f.seek(position)
                        chunk = f.read(chunk_size)
                        buffer = chunk + buffer
                        newline_count = buffer.count(b'\n')
            except OSError:
                return []

        lines = buffer.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return [line.decode('utf-8', errors='replace') for line in lines]

    def _append_summary_line(self, path: Path, entry: RunHistoryEntry) -> None:
        line = json.dumps(self._history_summary_to_dict(entry), ensure_ascii=False)
        with self._lock:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')

    def _write_detail(self, entry: RunHistoryEntry) -> None:
        path = self._detail_path(entry.id)
        payload = {
            'stdout': entry.stdout,
            'stderr': entry.stderr,
        }
        with self._lock:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    def _load_detail(self, history_id: str) -> RunHistoryDetail | None:
        path = self._detail_path(history_id)
        with self._lock:
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                return None
        return RunHistoryDetail(
            stdout=str(data.get('stdout', '')),
            stderr=str(data.get('stderr', '')),
        )

    def _delete_detail(self, history_id: str) -> bool:
        path = self._detail_path(history_id)
        with self._lock:
            if not path.exists():
                return False
            try:
                path.unlink()
            except OSError:
                return False
        return True

    def _update_summary_entry(self, entry: RunHistoryEntry) -> bool:
        path = self._summary_path(entry.task_id)
        with self._lock:
            if not path.exists():
                return False
            lines = path.read_text(encoding='utf-8').splitlines()
            updated = False
            for i in range(len(lines) - 1, -1, -1):
                stripped = lines[i].strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if obj.get('id') != entry.id:
                    continue
                lines[i] = json.dumps(self._history_summary_to_dict(entry), ensure_ascii=False)
                updated = True
                break

            if updated:
                suffix = '\n' if lines else ''
                path.write_text('\n'.join(lines) + suffix, encoding='utf-8')
            return updated

    def _update_legacy_history(self, entry: RunHistoryEntry) -> bool:
        path = self._legacy_path(entry.task_id)
        with self._lock:
            if not path.exists():
                return False
            lines = path.read_text(encoding='utf-8').splitlines()
            updated = False
            for i in range(len(lines) - 1, -1, -1):
                stripped = lines[i].strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if obj.get('id') != entry.id:
                    continue
                lines[i] = json.dumps(self._history_to_dict(entry), ensure_ascii=False)
                updated = True
                break
            if updated:
                path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            return updated

    def _load_recent_summary_entries(self, path: Path | None, limit: int) -> list[RunHistorySummary]:
        if path is None or not path.exists() or limit <= 0:
            return []

        entries: list[RunHistorySummary] = []
        for line in reversed(self._read_tail_lines(path, limit * 2)):
            entry = self._history_summary_from_summary_line(line)
            if entry is None:
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    def _load_last_summary_entry(self, path: Path | None) -> RunHistorySummary | None:
        if path is None or not path.exists():
            return None

        for line in reversed(self._read_tail_lines(path, 20)):
            entry = self._history_summary_from_summary_line(line)
            if entry is not None:
                return entry
        return None

    def _find_summary_entry(self, path: Path, history_id: str) -> RunHistorySummary | None:
        with self._lock:
            if not path.exists():
                return None
            lines = path.read_text(encoding='utf-8').splitlines()

        for line in reversed(lines):
            entry = self._history_summary_from_summary_line(line)
            if entry is not None and entry.id == history_id:
                return entry
        return None

    def _delete_summary_entries(self, path: Path, history_id: str) -> bool:
        with self._lock:
            if not path.exists():
                return False
            lines = path.read_text(encoding='utf-8').splitlines()

            kept_lines: list[str] = []
            deleted = False
            for line in lines:
                entry = self._history_summary_from_summary_line(line)
                if entry is not None and entry.id == history_id:
                    deleted = True
                    continue
                kept_lines.append(line)

            if not deleted:
                return False

            self._write_lines_or_delete(path, kept_lines)
            return True

    def _history_summary_from_summary_line(self, line: str) -> RunHistorySummary | None:
        stripped = line.strip()
        if not stripped:
            return None

        try:
            return self._history_summary_from_dict(json.loads(stripped))
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def _load_recent_legacy_summaries(self, path: Path | None, limit: int) -> list[RunHistorySummary]:
        if path is None or not path.exists() or limit <= 0:
            return []

        entries: list[RunHistorySummary] = []
        for start, end in reversed(self._read_recent_line_ranges(path, limit * 2)):
            entry = self._history_summary_from_legacy_range(path, start, end)
            if entry is None:
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    def _load_last_legacy_summary(self, path: Path | None) -> RunHistorySummary | None:
        if path is None or not path.exists():
            return None

        for start, end in reversed(self._read_recent_line_ranges(path, 20)):
            entry = self._history_summary_from_legacy_range(path, start, end)
            if entry is not None:
                return entry
        return None

    def _find_legacy_history_range(
        self,
        path: Path,
        history_id: str,
        search_limit: int | None = 400,
    ) -> tuple[int, int] | None:
        if not path.exists():
            return None

        needle = f'"id": "{history_id}"'
        if search_limit is None:
            return self._find_legacy_history_range_full(path, needle)

        for start, end in reversed(self._read_recent_line_ranges(path, search_limit)):
            prefix = self._read_range_start_text(path, start, end, _SUMMARY_ID_SCAN_BYTES)
            if needle in prefix:
                return start, end
        return None

    def _find_legacy_history_range_full(self, path: Path, needle: str) -> tuple[int, int] | None:
        with self._lock:
            try:
                with open(path, 'rb') as f:
                    start = 0
                    while True:
                        line = f.readline()
                        if not line:
                            return None
                        end = f.tell()
                        prefix = line[:_SUMMARY_ID_SCAN_BYTES].decode('utf-8', errors='ignore')
                        if needle in prefix:
                            return start, end
                        start = end
            except OSError:
                return None

    def _read_recent_line_ranges(self, path: Path, limit: int) -> list[tuple[int, int]]:
        if limit <= 0:
            return []

        with self._lock:
            try:
                with open(path, 'rb') as f:
                    f.seek(0, 2)
                    file_end = f.tell()
                    if file_end <= 0:
                        return []

                    starts: list[int] = []
                    position = file_end
                    while position > 0 and len(starts) < limit:
                        chunk_size = min(64 * 1024, position)
                        position -= chunk_size
                        f.seek(position)
                        chunk = f.read(chunk_size)
                        for idx in range(len(chunk) - 1, -1, -1):
                            if chunk[idx] != 0x0A:
                                continue
                            start = position + idx + 1
                            if start >= file_end:
                                continue
                            starts.append(start)
                            if len(starts) >= limit:
                                break
            except OSError:
                return []

        starts.append(0)
        starts = sorted(set(starts))
        if len(starts) > limit:
            starts = starts[-limit:]

        ranges: list[tuple[int, int]] = []
        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else file_end
            if end > start:
                ranges.append((start, end))
        return ranges

    def _read_range_text(self, path: Path, start: int, end: int) -> str:
        with self._lock:
            with open(path, 'rb') as f:
                f.seek(start)
                data = f.read(end - start)
        return data.decode('utf-8', errors='replace').rstrip('\r\n')

    def _read_range_start_text(self, path: Path, start: int, end: int, max_bytes: int) -> str:
        with self._lock:
            with open(path, 'rb') as f:
                f.seek(start)
                data = f.read(min(max_bytes, max(0, end - start)))
        return data.decode('utf-8', errors='ignore')

    def _read_range_end_text(self, path: Path, start: int, end: int, max_bytes: int) -> str:
        read_size = min(max_bytes, max(0, end - start))
        with self._lock:
            with open(path, 'rb') as f:
                f.seek(end - read_size)
                data = f.read(read_size)
        return data.decode('utf-8', errors='ignore')

    def _history_summary_from_legacy_range(
        self,
        path: Path,
        start: int,
        end: int,
    ) -> RunHistorySummary | None:
        line_size = end - start
        if line_size <= _SUMMARY_SCAN_BYTES:
            return self._history_summary_from_legacy_text(self._read_range_text(path, start, end))

        prefix_text = self._read_range_start_text(path, start, end, _SUMMARY_SCAN_BYTES)
        suffix_text = self._read_range_end_text(path, start, end, _SUMMARY_SCAN_BYTES)
        return self._history_summary_from_legacy_parts(prefix_text, suffix_text)

    def _history_entry_from_legacy_range(
        self,
        path: Path,
        start: int,
        end: int,
    ) -> RunHistoryEntry | None:
        try:
            return self._history_entry_from_dict(json.loads(self._read_range_text(path, start, end)))
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None

    def _history_summary_from_legacy_text(self, text: str) -> RunHistorySummary | None:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            return self._history_summary_from_dict(json.loads(stripped))
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def _history_summary_from_legacy_parts(
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
            suffix = json.loads('{' + suffix_text[duration_match.start():].rstrip('\r\n'))
        except (json.JSONDecodeError, ValueError):
            return None

        return self._history_summary_from_dict({
            **prefix,
            **suffix,
            'has_stdout': True,
            'has_stderr': True,
        })

    def _merge_history_summaries(
        self,
        entries: list[RunHistorySummary],
        limit: int,
    ) -> list[RunHistorySummary]:
        unique_entries: dict[str, RunHistorySummary] = {}
        for entry in entries:
            unique_entries[entry.id] = entry
        merged = list(unique_entries.values())
        merged.sort(key=lambda entry: entry.started_at, reverse=True)
        return merged[:limit]

    def _cache_history_summaries(
        self,
        task_id: str,
        entries: list[RunHistorySummary],
    ) -> None:
        if not entries:
            return

        path = self._summary_path(task_id)
        with self._lock:
            existing_ids: set[str] = set()
            if path.exists():
                for line in path.read_text(encoding='utf-8').splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        existing_ids.add(str(json.loads(stripped).get('id', '')))
                    except json.JSONDecodeError:
                        continue

            append_lines = [
                json.dumps(self._history_summary_record_to_dict(entry), ensure_ascii=False)
                for entry in entries
                if entry.id not in existing_ids
            ]
            if not append_lines:
                return

            with open(path, 'a', encoding='utf-8') as f:
                for line in append_lines:
                    f.write(line + '\n')

    def _write_lines_or_delete(self, path: Path, lines: list[str]) -> None:
        if lines:
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            return
        try:
            path.unlink()
        except OSError:
            path.write_text('', encoding='utf-8')

    def _delete_file_range(self, path: Path, start: int, end: int) -> bool:
        if end <= start:
            return False

        temp_path = path.with_name(path.name + '.tmp')
        with self._lock:
            if not path.exists():
                return False
            try:
                with open(path, 'rb') as src, open(temp_path, 'wb') as dst:
                    remaining = start
                    while remaining > 0:
                        chunk = src.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        dst.write(chunk)
                        remaining -= len(chunk)

                    src.seek(end)
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)

                if temp_path.stat().st_size == 0:
                    temp_path.unlink()
                    path.unlink()
                else:
                    temp_path.replace(path)
            except OSError:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    pass
                return False

        return True

    def _to_history_entry(
        self,
        summary: RunHistorySummary,
        detail: RunHistoryDetail,
    ) -> RunHistoryEntry:
        return RunHistoryEntry(
            id=summary.id,
            task_id=summary.task_id,
            task_name=summary.task_name,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            status=summary.status,
            exit_code=summary.exit_code,
            stdout=detail.stdout,
            stderr=detail.stderr,
            duration_seconds=summary.duration_seconds,
            trigger=summary.trigger,
            attempt_count=summary.attempt_count,
            has_stdout=summary.has_stdout,
            has_stderr=summary.has_stderr,
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

    def _history_to_dict(self, e: RunHistoryEntry) -> dict[str, Any]:
        return {
            'id': e.id,
            'task_id': e.task_id,
            'task_name': e.task_name,
            'started_at': e.started_at,
            'finished_at': e.finished_at,
            'status': e.status,
            'exit_code': e.exit_code,
            'stdout': e.stdout,
            'stderr': e.stderr,
            'duration_seconds': e.duration_seconds,
            'trigger': e.trigger,
            'attempt_count': e.attempt_count,
        }

    def _history_summary_to_dict(self, e: RunHistoryEntry) -> dict[str, Any]:
        return self._history_summary_record_to_dict(e)

    def _history_summary_record_to_dict(self, e: RunHistorySummary) -> dict[str, Any]:
        return {
            'id': e.id,
            'task_id': e.task_id,
            'task_name': e.task_name,
            'started_at': e.started_at,
            'finished_at': e.finished_at,
            'status': e.status,
            'exit_code': e.exit_code,
            'duration_seconds': e.duration_seconds,
            'trigger': e.trigger,
            'attempt_count': e.attempt_count,
            'has_stdout': e.has_stdout,
            'has_stderr': e.has_stderr,
        }

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

    def _history_entry_from_dict(self, d: dict[str, Any]) -> RunHistoryEntry:
        return RunHistoryEntry(
            id=d['id'],
            task_id=d['task_id'],
            task_name=d.get('task_name', ''),
            started_at=d['started_at'],
            finished_at=d.get('finished_at'),
            status=d.get('status', 'unknown'),
            exit_code=d.get('exit_code'),
            stdout=d.get('stdout', ''),
            stderr=d.get('stderr', ''),
            duration_seconds=d.get('duration_seconds'),
            trigger=d.get('trigger', 'scheduler'),
            attempt_count=int(d.get('attempt_count', 1)),
            has_stdout=bool(d.get('has_stdout')) or bool(d.get('stdout')),
            has_stderr=bool(d.get('has_stderr')) or bool(d.get('stderr')),
        )
