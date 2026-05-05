from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RUNTIME_OPTIONS = ('auto', 'powershell', 'python', 'command')
SCHEDULE_TYPES = ('interval', 'daily', 'weekly', 'monthly', 'once', 'chain')
# 0=月曜, 6=日曜 (Python weekday() と同じ)
WEEKDAY_LABELS = ['月', '火', '水', '木', '金', '土', '日']


@dataclass
class TaskDefinition:
    id: str
    name: str
    description: str
    enabled: bool
    runtime: str
    command_text: str
    working_directory: str
    run_as_admin: bool
    timeout_seconds: int | None
    retry_count: int
    retry_delay_seconds: int
    environment: dict[str, str]
    schedule: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass
class RunHistorySummary:
    id: str
    task_id: str
    task_name: str
    started_at: str
    finished_at: str | None
    # running / success / failed / timeout / killed / unknown_exit
    status: str
    exit_code: int | None
    duration_seconds: float | None
    trigger: str   # scheduler / manual
    attempt_count: int
    has_stdout: bool = False
    has_stderr: bool = False


@dataclass
class RunHistoryDetail:
    stdout: str
    stderr: str


@dataclass
class RunHistoryEntry(RunHistorySummary):
    stdout: str = ''
    stderr: str = ''


@dataclass
class PidEntry:
    pid: int
    started_at: str
    history_id: str
    task_name: str
    proc_create_time: float | None = None  # プロセス生成時刻 (unix epoch), PID 再利用検出に使用


def schedule_summary(schedule: dict[str, Any]) -> str:
    """スケジュール設定を人間が読めるテキストに変換する"""
    t = schedule.get('type', 'interval')

    if t == 'interval':
        parts = []
        for key, unit in [('days', '日'), ('hours', '時間'), ('minutes', '分'), ('seconds', '秒')]:
            v = int(schedule.get(key, 0) or 0)
            if v > 0:
                parts.append(f'{v}{unit}')
        return '毎 ' + ('・'.join(parts) if parts else '(未設定)')

    if t == 'daily':
        return (
            f"毎日 {int(schedule.get('hour', 0)):02d}:"
            f"{int(schedule.get('minute', 0)):02d}:"
            f"{int(schedule.get('second', 0)):02d}"
        )

    if t == 'weekly':
        wdays = [WEEKDAY_LABELS[int(d)] for d in sorted(schedule.get('weekdays', []))]
        label = '/'.join(wdays) if wdays else '(未設定)'
        return (
            f"週次({label}) {int(schedule.get('hour', 0)):02d}:"
            f"{int(schedule.get('minute', 0)):02d}:"
            f"{int(schedule.get('second', 0)):02d}"
        )

    if t == 'monthly':
        return (
            f"毎月{int(schedule.get('day', 1))}日 "
            f"{int(schedule.get('hour', 0)):02d}:"
            f"{int(schedule.get('minute', 0)):02d}:"
            f"{int(schedule.get('second', 0)):02d}"
        )

    if t == 'once':
        return f"1回 {schedule.get('run_at', '-')}"

    if t == 'chain':
        after_name = schedule.get('after_task_name') or schedule.get('after_task_id', '-')
        condition = schedule.get('on_condition', 'success')
        condition_label = {'success': '正常終了時', 'failed': 'エラー終了時', 'any': '完了時'}.get(
            condition, condition
        )
        fallback = str(schedule.get('fallback_time', '')).strip()
        label = f'チェーン: {after_name} ({condition_label})'
        if fallback:
            label += f' / フォールバック {fallback}'
        return label

    return t
