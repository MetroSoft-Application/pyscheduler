from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from .executor import TaskExecutor
from .models import RunHistorySummary, TaskDefinition
from .storage import Storage


def _local_tz():
    try:
        from tzlocal import get_localzone
        return get_localzone()
    except Exception:
        import datetime as _dt
        return _dt.timezone.utc


@dataclass(slots=True)
class ScheduleOccurrence:
    task_id: str
    task_name: str
    schedule_type: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    estimated: bool
    enabled: bool


class TaskScheduler:
    """APScheduler 3.x をラップしてタスクのスケジュール管理を行うクラス"""

    def __init__(self, executor: TaskExecutor, storage: Storage) -> None:
        self._executor = executor
        self._storage = storage
        self._scheduler = BackgroundScheduler(
            job_defaults={
                'misfire_grace_time': 60,   # 60秒以内の遅延は実行する
                'coalesce': True,            # 同一タスクの多重実行をまとめる
                'max_instances': 1,          # 同時実行数の上限
            },
            timezone=_local_tz(),
        )
        self._running_threads: dict[str, threading.Thread] = {}
        self._thread_lock = threading.Lock()

    def start(self) -> None:
        self._executor.on_task_success = self._on_task_success
        self._executor.on_task_complete = self._on_task_complete
        for task in self._storage.list_tasks():
            if task.enabled:
                self._add_job(task)
        self._scheduler.start()

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)

    def reload_task(self, task: TaskDefinition) -> None:
        """タスク追加・更新時にスケジュールを再設定する"""
        self._remove_job(task.id)
        if task.enabled:
            self._add_job(task)

    def remove_task(self, task_id: str) -> None:
        self._remove_job(task_id)

    def run_now(self, task_id: str) -> bool:
        """タスクを今すぐ手動実行する"""
        task = self._storage.get_task(task_id)
        if task is None:
            return False
        t = threading.Thread(
            target=self._executor.execute,
            args=(task, 'manual'),
            name=f'TaskWorker-manual-{task_id}',
            daemon=True,
        )
        t.start()
        return True

    def get_next_run_time(self, task_id: str) -> str | None:
        for jid in (task_id, f'{task_id}__fallback'):
            try:
                job = self._scheduler.get_job(jid)
                if job and job.next_run_time:
                    return job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
        return None

    def preview_schedule(
        self,
        tasks: list[TaskDefinition],
        start: datetime | None = None,
        end: datetime | None = None,
        last_history: dict[str, RunHistorySummary] | None = None,
        limit_per_task: int = 1000,
    ) -> tuple[datetime, datetime, list[ScheduleOccurrence]]:
        tz = self._scheduler.timezone
        window_start = self._normalize_preview_datetime(start or datetime.now(tz))
        window_end = self._normalize_preview_datetime(end or (window_start + timedelta(days=7)))
        if window_end <= window_start:
            window_end = window_start + timedelta(hours=1)

        history_map = last_history or {}
        occurrences: list[ScheduleOccurrence] = []
        for task in tasks:
            occurrences.extend(
                self._preview_task_schedule(
                    task,
                    window_start,
                    window_end,
                    history_map.get(task.id),
                    limit_per_task,
                )
            )

        occurrences.sort(key=lambda entry: (entry.start_time, entry.task_name))
        return window_start, window_end, occurrences

    def _normalize_preview_datetime(self, value: datetime) -> datetime:
        tz = self._scheduler.timezone
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)

    def _estimate_task_duration(
        self,
        task: TaskDefinition,
        last_history: RunHistorySummary | None,
    ) -> tuple[float, bool]:
        if last_history and last_history.duration_seconds and last_history.duration_seconds > 0:
            return max(30.0, float(last_history.duration_seconds)), False
        if task.timeout_seconds:
            return max(30.0, min(float(task.timeout_seconds), 4 * 3600.0)), True
        return 300.0, True

    def _preview_task_schedule(
        self,
        task: TaskDefinition,
        window_start: datetime,
        window_end: datetime,
        last_history: RunHistorySummary | None,
        limit_per_task: int,
    ) -> list[ScheduleOccurrence]:
        if task.schedule.get('type') == 'chain':
            return self._preview_chain_schedule(task, window_start, window_end, last_history, limit_per_task)

        trigger = self._make_trigger(task)
        if trigger is None:
            return []

        duration_seconds, estimated = self._estimate_task_duration(task, last_history)
        schedule_type = str(task.schedule.get('type', 'interval'))
        occurrences: list[ScheduleOccurrence] = []
        previous_fire_time = None
        search_time = window_start

        for _ in range(max(1, limit_per_task)):
            fire_time = trigger.get_next_fire_time(previous_fire_time, search_time)
            if fire_time is None:
                break

            fire_time = self._normalize_preview_datetime(fire_time)
            if fire_time >= window_end:
                break

            end_time = min(fire_time + timedelta(seconds=duration_seconds), window_end)
            occurrences.append(
                ScheduleOccurrence(
                    task_id=task.id,
                    task_name=task.name,
                    schedule_type=schedule_type,
                    start_time=fire_time,
                    end_time=end_time,
                    duration_seconds=duration_seconds,
                    estimated=estimated,
                    enabled=task.enabled,
                )
            )
            previous_fire_time = fire_time
            search_time = fire_time + timedelta(microseconds=1)

        return occurrences

    def _add_job(self, task: TaskDefinition) -> None:
        if task.schedule.get('type') == 'chain':
            # chain はイベント駆動のため APScheduler には登録しない
            # fallback_time が設定されている場合はデイリークロンも追加する
            fallback_time = str(task.schedule.get('fallback_time', '')).strip()
            if fallback_time:
                trigger = self._make_fallback_trigger(fallback_time)
                if trigger is not None:
                    self._scheduler.add_job(
                        func=self._run_task_fallback,
                        trigger=trigger,
                        args=[task.id],
                        id=f'{task.id}__fallback',
                        replace_existing=True,
                    )
            return

        trigger = self._make_trigger(task)
        if trigger is None:
            return
        self._scheduler.add_job(
            func=self._run_task,
            trigger=trigger,
            args=[task.id],
            id=task.id,
            replace_existing=True,
        )

    def _remove_job(self, task_id: str) -> None:
        for jid in (task_id, f'{task_id}__fallback'):
            try:
                self._scheduler.remove_job(jid)
            except Exception:
                pass

    def _run_task(self, task_id: str) -> None:
        """APScheduler から呼ばれるジョブ本体"""
        task = self._storage.get_task(task_id)
        if task is None or not task.enabled:
            return

        # once タスクは実行後に無効化する
        if task.schedule.get('type') == 'once':
            task.enabled = False
            task.updated_at = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            self._storage.save_task(task)

        with self._thread_lock:
            prev = self._running_threads.get(task_id)
            if prev and prev.is_alive():
                return  # 二重起動防止（max_instances=1 の念押し）

        t = threading.Thread(
            target=self._executor.execute,
            args=(task, 'scheduler'),
            name=f'TaskWorker-{task_id}',
            daemon=True,
        )
        with self._thread_lock:
            self._running_threads[task_id] = t
        t.start()

    def _make_trigger(self, task: TaskDefinition):
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.date import DateTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        sched = task.schedule
        t = sched.get('type', 'interval')

        if t == 'interval':
            total_seconds = (
                int(sched.get('days', 0) or 0) * 86400
                + int(sched.get('hours', 0) or 0) * 3600
                + int(sched.get('minutes', 0) or 0) * 60
                + int(sched.get('seconds', 0) or 0)
            )
            if total_seconds <= 0:
                return None
            return IntervalTrigger(seconds=total_seconds)

        if t == 'daily':
            return CronTrigger(
                hour=int(sched.get('hour', 9)),
                minute=int(sched.get('minute', 0)),
                second=int(sched.get('second', 0)),
            )

        if t == 'weekly':
            weekdays = sorted({int(d) for d in sched.get('weekdays', [0])})
            if not weekdays:
                return None
            dow = ','.join(str(d) for d in weekdays)
            return CronTrigger(
                day_of_week=dow,
                hour=int(sched.get('hour', 9)),
                minute=int(sched.get('minute', 0)),
                second=int(sched.get('second', 0)),
            )

        if t == 'monthly':
            return CronTrigger(
                day=int(sched.get('day', 1)),
                hour=int(sched.get('hour', 9)),
                minute=int(sched.get('minute', 0)),
                second=int(sched.get('second', 0)),
            )

        if t == 'once':
            run_at_str = str(sched.get('run_at', '')).strip()
            if not run_at_str:
                return None
            try:
                # ISO形式またはスペース区切りを受け付ける
                run_at = datetime.fromisoformat(run_at_str.replace(' ', 'T'))
                if run_at <= datetime.now():
                    return None
                return DateTrigger(run_date=run_at)
            except ValueError:
                return None

        return None

    def _run_task_fallback(self, task_id: str) -> None:
        """fallback_time から呼ばれるジョブ。当日中にすでに実行されていればスキップする。"""
        today = datetime.now().strftime('%Y-%m-%d')
        recent = self._storage.list_history(task_id, limit=1)
        if recent and recent[0].started_at.startswith(today):
            # 当日中に chain または手動で実行済みのためスキップ
            return
        self._run_task(task_id)

    def _on_task_success(self, task_id: str) -> None:
        """後方互換のため残存。実処理は _on_task_complete が担う。"""
        pass

    def _on_task_complete(self, task_id: str, final_status: str) -> None:
        """あるタスクが完了したとき、on_condition に合致するチェーンタスクを起動する"""
        # 失敗とみなすステータス
        _FAILED_STATUSES = frozenset({'failed', 'timeout', 'unknown_exit'})

        for task in self._storage.list_tasks():
            if not task.enabled:
                continue
            sched = task.schedule
            if sched.get('type') != 'chain':
                continue
            if sched.get('after_task_id') != task_id:
                continue

            condition = str(sched.get('on_condition', 'success'))
            matched = (
                condition == 'any'
                or (condition == 'success' and final_status == 'success')
                or (condition == 'failed' and final_status in _FAILED_STATUSES)
            )
            if not matched:
                continue

            t = threading.Thread(
                target=self._executor.execute,
                args=(task, 'chain'),
                name=f'TaskWorker-chain-{task.id}',
                daemon=True,
            )
            t.start()

    def _preview_chain_schedule(
        self,
        task: TaskDefinition,
        window_start: datetime,
        window_end: datetime,
        last_history: RunHistorySummary | None,
        limit_per_task: int,
    ) -> list[ScheduleOccurrence]:
        """chain タイプのプレビュー: fallback_time がある場合のみ表示する"""
        fallback_time = str(task.schedule.get('fallback_time', '')).strip()
        if not fallback_time:
            return []
        trigger = self._make_fallback_trigger(fallback_time)
        if trigger is None:
            return []

        duration_seconds, _ = self._estimate_task_duration(task, last_history)
        occurrences: list[ScheduleOccurrence] = []
        previous_fire_time = None
        search_time = window_start
        for _ in range(max(1, limit_per_task)):
            fire_time = trigger.get_next_fire_time(previous_fire_time, search_time)
            if fire_time is None:
                break
            fire_time = self._normalize_preview_datetime(fire_time)
            if fire_time >= window_end:
                break
            end_time = min(fire_time + timedelta(seconds=duration_seconds), window_end)
            occurrences.append(
                ScheduleOccurrence(
                    task_id=task.id,
                    task_name=task.name,
                    schedule_type='chain',
                    start_time=fire_time,
                    end_time=end_time,
                    duration_seconds=duration_seconds,
                    estimated=True,
                    enabled=task.enabled,
                )
            )
            previous_fire_time = fire_time
            search_time = fire_time + timedelta(microseconds=1)
        return occurrences

    def _make_fallback_trigger(self, fallback_time: str):
        """フォールバック時刻文字列(HH:MM または HH:MM:SS)から CronTrigger を生成"""
        from apscheduler.triggers.cron import CronTrigger
        try:
            parts = fallback_time.split(':')
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            second = int(parts[2]) if len(parts) > 2 else 0
            return CronTrigger(hour=hour, minute=minute, second=second)
        except (ValueError, IndexError):
            return None
