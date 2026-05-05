from __future__ import annotations

import json
import threading
from pathlib import Path

import psutil

from .models import PidEntry


class PidStore:
    """実行中プロセスの PID を JSON ファイルに永続化するクラス。
    アプリが強制終了した際も、再起動時にプロセスを再追跡できるようにする。
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / 'pid_store.json'
        self._lock = threading.RLock()
        data_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        tmp = self._path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self._path)

    def register(self, task_id: str, entry: PidEntry) -> None:
        with self._lock:
            data = self._load()
            data[task_id] = {
                'pid': entry.pid,
                'started_at': entry.started_at,
                'history_id': entry.history_id,
                'task_name': entry.task_name,
                'proc_create_time': entry.proc_create_time,
            }
            self._save(data)

    def unregister(self, task_id: str) -> None:
        with self._lock:
            data = self._load()
            data.pop(task_id, None)
            self._save(data)

    def get(self, task_id: str) -> PidEntry | None:
        with self._lock:
            d = self._load().get(task_id)
        if d is None:
            return None
        return PidEntry(
            pid=d['pid'],
            started_at=d['started_at'],
            history_id=d['history_id'],
            task_name=d.get('task_name', ''),
            proc_create_time=d.get('proc_create_time'),
        )

    def get_all(self) -> dict[str, PidEntry]:
        with self._lock:
            data = self._load()
        result: dict[str, PidEntry] = {}
        for task_id, d in data.items():
            result[task_id] = PidEntry(
                pid=d['pid'],
                started_at=d['started_at'],
                history_id=d['history_id'],
                task_name=d.get('task_name', ''),
                proc_create_time=d.get('proc_create_time'),
            )
        return result

    def is_running(self, task_id: str) -> bool:
        entry = self.get(task_id)
        if entry is None:
            return False
        return pid_alive(entry.pid, entry.proc_create_time)

    def cleanup_dead_pids(self) -> dict[str, PidEntry]:
        """存在しない PID (または PID が再利用されたもの) を削除し、死亡済みエントリを返す"""
        dead: dict[str, PidEntry] = {}
        with self._lock:
            data = self._load()
            alive_data: dict = {}
            for task_id, d in data.items():
                create_time = d.get('proc_create_time')
                if pid_alive(d['pid'], create_time):
                    alive_data[task_id] = d
                else:
                    dead[task_id] = PidEntry(
                        pid=d['pid'],
                        started_at=d['started_at'],
                        history_id=d['history_id'],
                        task_name=d.get('task_name', ''),
                        proc_create_time=create_time,
                    )
            self._save(alive_data)
        return dead

    def kill(self, task_id: str) -> bool:
        """指定タスクのプロセスを停止する。成功したら True を返す"""
        entry = self.get(task_id)
        if entry is None:
            return False
        return kill_pid(entry.pid)


def pid_alive(pid: int, expected_create_time: float | None = None) -> bool:
    """プロセスが生存しているか確認する。
    expected_create_time が指定されている場合は PID 再利用も検出する (許容誤差 2 秒)。
    """
    try:
        proc = psutil.Process(pid)
        if not (proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE):
            return False
        if expected_create_time is not None:
            try:
                if abs(proc.create_time() - expected_create_time) > 2.0:
                    return False
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_pid(pid: int) -> bool:
    """プロセスとその子プロセスを強制終了する"""
    try:
        proc = psutil.Process(pid)
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        proc.kill()
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
