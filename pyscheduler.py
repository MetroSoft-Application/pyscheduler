from __future__ import annotations

import os
import sys
from pathlib import Path


def _resolve_data_dir() -> Path:
    candidates: list[Path] = []

    env_dir = os.environ.get('TASK_SCHEDULER_DATA_DIR', '').strip()
    if env_dir:
        candidates.append(Path(env_dir).expanduser())

    if getattr(sys, 'frozen', False):
        candidates.append(Path(sys.executable).resolve().parent / 'data')

    candidates.append(Path(__file__).resolve().parent / 'data')
    candidates.append(Path.cwd() / 'data')

    seen: set[Path] = set()
    unique_candidates: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_candidates.append(resolved)

    for candidate in unique_candidates:
        if (candidate / 'tasks.json').exists():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate

    default_dir = unique_candidates[0]
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir


DATA_DIR = _resolve_data_dir()

from task_scheduler.storage import Storage
from task_scheduler.pid_store import PidStore
from task_scheduler.executor import TaskExecutor
from task_scheduler.scheduler import TaskScheduler
from task_scheduler.tray import TrayIcon
from task_scheduler.ui.main_window import MainWindow


def main() -> int:
    storage = Storage(DATA_DIR)
    pid_store = PidStore(DATA_DIR)
    executor = TaskExecutor(storage, pid_store)
    scheduler = TaskScheduler(executor, storage)

    # 起動時に前回セッションの孤立プロセス・死亡済みプロセスを処理する
    executor.startup_cleanup()

    # スケジューラを起動（バックグラウンドスレッド）
    scheduler.start()

    # メインウィンドウ
    app = MainWindow(storage, pid_store, executor, scheduler)

    # 終了処理（pystray のスレッドから呼ばれるため after() を経由する）
    def _quit() -> None:
        scheduler.stop()
        tray.stop()
        app.run_on_ui_thread(app.destroy)

    tray = TrayIcon(
        on_open=app.show_window,
        on_exit=_quit,
    )
    app.set_quit_callback(_quit)
    tray.start()

    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            scheduler.stop()
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
