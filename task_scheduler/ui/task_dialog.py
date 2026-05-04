from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from datetime import datetime
from pathlib import Path
from typing import Any

from ..executor import guess_task_name, infer_runtime
from ..models import (
    RUNTIME_OPTIONS,
    SCHEDULE_TYPES,
    WEEKDAY_LABELS,
    TaskDefinition,
)


def _safe_int(val: str, label: str, minimum: int = 0, maximum: int | None = None) -> int:
    v = val.strip()
    if not v:
        raise ValueError(f'{label} は必須です。')
    n = int(v)
    if n < minimum:
        raise ValueError(f'{label} は {minimum} 以上で指定してください。')
    if maximum is not None and n > maximum:
        raise ValueError(f'{label} は {maximum} 以下で指定してください。')
    return n


def _optional_int(val: str, label: str) -> int | None:
    v = val.strip()
    return _safe_int(v, label, minimum=0) if v else None


class TaskDialog(tk.Toplevel):
    """タスク追加・編集ダイアログ"""

    def __init__(
        self,
        master: tk.Misc,
        task: TaskDefinition | None = None,
        tasks: list[TaskDefinition] | None = None,
    ) -> None:
        super().__init__(master)
        self.title('タスク編集' if task else '新規タスク')
        self.geometry('860x720')
        self.minsize(760, 640)
        self.transient(master)
        self.grab_set()
        self.resizable(True, True)
        self.result: TaskDefinition | None = None
        self._task = task

        # 連鎖元候補リスト(自分自身は除外)
        self._chain_tasks = [
            t for t in (tasks or []) if not task or t.id != task.id
        ]
        self._chain_task_name_to_id: dict[str, str] = {t.name: t.id for t in self._chain_tasks}

        # ---- 変数初期化 ----
        sched = (task.schedule if task else None) or {'type': 'interval'}

        self._name_var = tk.StringVar(value=task.name if task else '')
        self._desc_var = tk.StringVar(value=task.description if task else '')
        self._runtime_var = tk.StringVar(value=task.runtime if task else 'auto')
        self._cmd_var = tk.StringVar(value=task.command_text if task else '')
        self._workdir_var = tk.StringVar(value=task.working_directory if task else '')
        self._run_as_admin_var = tk.BooleanVar(value=task.run_as_admin if task else False)
        self._timeout_var = tk.StringVar(
            value='' if not task or task.timeout_seconds is None else str(task.timeout_seconds)
        )
        self._retry_count_var = tk.StringVar(value=str(task.retry_count if task else 0))
        self._retry_delay_var = tk.StringVar(value=str(task.retry_delay_seconds if task else 0))
        self._enabled_var = tk.BooleanVar(value=task.enabled if task else True)
        self._env_text_val = (
            '\n'.join(f'{k}={v}' for k, v in task.environment.items()) if task else ''
        )
        self._schedule_type_var = tk.StringVar(value=sched.get('type', 'interval'))

        # スケジュール詳細変数
        self._int_days_var = tk.StringVar(value=str(sched.get('days', 0)))
        self._int_hours_var = tk.StringVar(value=str(sched.get('hours', 1)))
        self._int_minutes_var = tk.StringVar(value=str(sched.get('minutes', 0)))
        self._int_seconds_var = tk.StringVar(value=str(sched.get('seconds', 0)))

        self._time_hour_var = tk.StringVar(value=str(sched.get('hour', 9)))
        self._time_minute_var = tk.StringVar(value=str(sched.get('minute', 0)))
        self._time_second_var = tk.StringVar(value=str(sched.get('second', 0)))

        self._weekly_day_vars: list[tk.BooleanVar] = []
        sel_weekdays = set(int(d) for d in sched.get('weekdays', []))
        for i in range(7):
            self._weekly_day_vars.append(tk.BooleanVar(value=(i in sel_weekdays)))

        self._monthly_day_var = tk.StringVar(value=str(sched.get('day', 1)))
        self._once_var = tk.StringVar(value=sched.get('run_at', ''))

        # chain 設定変数
        current_chain_name = ''
        if sched.get('after_task_id'):
            current_chain_name = (
                sched.get('after_task_name')
                or next((t.name for t in self._chain_tasks if t.id == sched['after_task_id']), '')
                or sched['after_task_id']
            )
        self._chain_task_var = tk.StringVar(value=current_chain_name)
        self._chain_fallback_var = tk.StringVar(value=str(sched.get('fallback_time', '')))
        self._chain_condition_var = tk.StringVar(value=str(sched.get('on_condition', 'success')))

        self._build_ui()
        self._on_schedule_type_changed()

        # コマンド変更時にタスク名・ランタイムを自動補完
        self._cmd_var.trace_add('write', self._on_cmd_changed)

    # ------------------------------------------------------------------ UI 構築

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill='both', expand=True)

        # 上部: 基本設定
        basic = ttk.LabelFrame(outer, text='基本設定', padding=8)
        basic.pack(fill='x', pady=(0, 6))
        basic.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(basic, text='タスク名:').grid(row=row, column=0, sticky='e', padx=4, pady=3)
        ttk.Entry(basic, textvariable=self._name_var).grid(
            row=row, column=1, columnspan=2, sticky='ew', pady=3
        )
        ttk.Checkbutton(basic, text='有効', variable=self._enabled_var).grid(
            row=row, column=3, sticky='w', padx=8
        )

        row += 1
        ttk.Label(basic, text='説明:').grid(row=row, column=0, sticky='e', padx=4, pady=3)
        ttk.Entry(basic, textvariable=self._desc_var).grid(
            row=row, column=1, columnspan=3, sticky='ew', pady=3
        )

        row += 1
        ttk.Label(basic, text='ランタイム:').grid(row=row, column=0, sticky='e', padx=4, pady=3)
        ttk.Combobox(
            basic,
            textvariable=self._runtime_var,
            values=list(RUNTIME_OPTIONS),
            state='readonly',
            width=14,
        ).grid(row=row, column=1, sticky='w', pady=3)

        row += 1
        ttk.Label(basic, text='コマンド:').grid(row=row, column=0, sticky='e', padx=4, pady=3)
        cmd_frame = ttk.Frame(basic)
        cmd_frame.grid(row=row, column=1, columnspan=3, sticky='ew', pady=3)
        cmd_frame.columnconfigure(0, weight=1)
        ttk.Entry(cmd_frame, textvariable=self._cmd_var).grid(row=0, column=0, sticky='ew')
        ttk.Button(cmd_frame, text='参照...', command=self._browse_command, width=8).grid(
            row=0, column=1, padx=(4, 0)
        )

        row += 1
        ttk.Label(basic, text='作業ディレクトリ:').grid(row=row, column=0, sticky='e', padx=4, pady=3)
        wd_frame = ttk.Frame(basic)
        wd_frame.grid(row=row, column=1, columnspan=3, sticky='ew', pady=3)
        wd_frame.columnconfigure(0, weight=1)
        ttk.Entry(wd_frame, textvariable=self._workdir_var).grid(row=0, column=0, sticky='ew')
        ttk.Button(wd_frame, text='参照...', command=self._browse_workdir, width=8).grid(
            row=0, column=1, padx=(4, 0)
        )

        # 中部: スケジュール
        sched_outer = ttk.LabelFrame(outer, text='スケジュール', padding=8)
        sched_outer.pack(fill='x', pady=(0, 6))

        type_frame = ttk.Frame(sched_outer)
        type_frame.pack(fill='x', pady=(0, 4))
        ttk.Label(type_frame, text='種類:').pack(side='left', padx=(0, 6))
        cb = ttk.Combobox(
            type_frame,
            textvariable=self._schedule_type_var,
            values=['interval', 'daily', 'weekly', 'monthly', 'once', 'chain'],
            state='readonly',
            width=12,
        )
        cb.pack(side='left')
        cb.bind('<<ComboboxSelected>>', lambda _e: self._on_schedule_type_changed())

        self._sched_detail_frame = ttk.Frame(sched_outer)
        self._sched_detail_frame.pack(fill='x')

        # 下部: 詳細オプション
        detail = ttk.LabelFrame(outer, text='詳細オプション', padding=8)
        detail.pack(fill='x', pady=(0, 6))
        detail.columnconfigure(1, weight=1)
        detail.columnconfigure(3, weight=1)

        ttk.Label(detail, text='タイムアウト(秒):').grid(row=0, column=0, sticky='e', padx=4, pady=3)
        ttk.Entry(detail, textvariable=self._timeout_var, width=10).grid(
            row=0, column=1, sticky='w', pady=3
        )
        ttk.Label(detail, text='(空欄=無制限)').grid(row=0, column=2, sticky='w', padx=4)

        ttk.Label(detail, text='リトライ回数:').grid(row=1, column=0, sticky='e', padx=4, pady=3)
        tk.Spinbox(detail, textvariable=self._retry_count_var, from_=0, to=99, width=6).grid(
            row=1, column=1, sticky='w', pady=3
        )
        ttk.Label(detail, text='リトライ間隔(秒):').grid(row=1, column=2, sticky='e', padx=4)
        tk.Spinbox(detail, textvariable=self._retry_delay_var, from_=0, to=3600, width=8).grid(
            row=1, column=3, sticky='w', pady=3
        )

        ttk.Checkbutton(
            detail,
            text='管理者権限で実行する',
            variable=self._run_as_admin_var,
        ).grid(row=2, column=1, columnspan=3, sticky='w', pady=3)

        ttk.Label(detail, text='環境変数:').grid(row=3, column=0, sticky='ne', padx=4, pady=3)
        env_frame = ttk.Frame(detail)
        env_frame.grid(row=3, column=1, columnspan=3, sticky='ew', pady=3)
        env_frame.columnconfigure(0, weight=1)
        self._env_text = tk.Text(env_frame, height=4, wrap='none')
        self._env_text.grid(row=0, column=0, sticky='ew')
        env_sb = ttk.Scrollbar(env_frame, orient='vertical', command=self._env_text.yview)
        env_sb.grid(row=0, column=1, sticky='ns')
        self._env_text.configure(yscrollcommand=env_sb.set)
        if self._env_text_val:
            self._env_text.insert('1.0', self._env_text_val)
        ttk.Label(detail, text='(KEY=VALUE 形式、1行1エントリ)').grid(
            row=4, column=1, columnspan=3, sticky='w', padx=4
        )

        # ボタン
        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill='x', side='bottom', pady=(4, 0))
        ttk.Button(btn_frame, text='OK', command=self._on_ok, width=10).pack(side='right', padx=4)
        ttk.Button(btn_frame, text='キャンセル', command=self.destroy, width=10).pack(
            side='right', padx=4
        )

    # ------------------------------------------------------------------ スケジュール詳細フレーム

    def _on_schedule_type_changed(self) -> None:
        for w in self._sched_detail_frame.winfo_children():
            w.destroy()

        t = self._schedule_type_var.get()
        f = self._sched_detail_frame

        if t == 'interval':
            self._build_interval_frame(f)
        elif t == 'daily':
            self._build_time_frame(f, show_weekdays=False, show_monthly_day=False)
        elif t == 'weekly':
            self._build_time_frame(f, show_weekdays=True, show_monthly_day=False)
        elif t == 'monthly':
            self._build_time_frame(f, show_weekdays=False, show_monthly_day=True)
        elif t == 'once':
            self._build_once_frame(f)
        elif t == 'chain':
            self._build_chain_frame(f)

    def _spinbox(self, parent, var: tk.StringVar, label: str, _from: int, to: int, width: int = 6):
        ttk.Label(parent, text=label).pack(side='left', padx=(6, 2))
        tk.Spinbox(parent, textvariable=var, from_=_from, to=to, width=width).pack(side='left')

    def _build_interval_frame(self, f: ttk.Frame) -> None:
        row = ttk.Frame(f)
        row.pack(fill='x', pady=4)
        self._spinbox(row, self._int_days_var, '日:', 0, 999)
        self._spinbox(row, self._int_hours_var, '時間:', 0, 23)
        self._spinbox(row, self._int_minutes_var, '分:', 0, 59)
        self._spinbox(row, self._int_seconds_var, '秒:', 0, 59)
        ttk.Label(f, text='ごとに繰り返す').pack(anchor='w', padx=6)

    def _build_time_frame(
        self,
        f: ttk.Frame,
        *,
        show_weekdays: bool,
        show_monthly_day: bool,
    ) -> None:
        if show_weekdays:
            wd_frame = ttk.Frame(f)
            wd_frame.pack(fill='x', pady=4)
            ttk.Label(wd_frame, text='曜日:').pack(side='left', padx=(0, 4))
            for i, label in enumerate(WEEKDAY_LABELS):
                ttk.Checkbutton(
                    wd_frame, text=label, variable=self._weekly_day_vars[i]
                ).pack(side='left')

        if show_monthly_day:
            md_frame = ttk.Frame(f)
            md_frame.pack(fill='x', pady=4)
            self._spinbox(md_frame, self._monthly_day_var, '日:', 1, 31)
            ttk.Label(md_frame, text='日').pack(side='left')

        time_row = ttk.Frame(f)
        time_row.pack(fill='x', pady=4)
        ttk.Label(time_row, text='実行時刻:').pack(side='left', padx=(0, 4))
        self._spinbox(time_row, self._time_hour_var, '時:', 0, 23, width=4)
        self._spinbox(time_row, self._time_minute_var, '分:', 0, 59, width=4)
        self._spinbox(time_row, self._time_second_var, '秒:', 0, 59, width=4)

    def _build_once_frame(self, f: ttk.Frame) -> None:
        row = ttk.Frame(f)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text='実行日時:').pack(side='left', padx=(0, 4))
        ttk.Entry(row, textvariable=self._once_var, width=22).pack(side='left')
        ttk.Label(row, text='(例: 2026-04-30 09:00:00)').pack(side='left', padx=6)

    def _build_chain_frame(self, f: ttk.Frame) -> None:
        task_names = [t.name for t in self._chain_tasks]

        row1 = ttk.Frame(f)
        row1.pack(fill='x', pady=4)
        ttk.Label(row1, text='前提タスク:').pack(side='left', padx=(0, 4))
        cb = ttk.Combobox(
            row1,
            textvariable=self._chain_task_var,
            values=task_names,
            state='readonly' if task_names else 'disabled',
            width=30,
        )
        cb.pack(side='left')
        if not task_names:
            ttk.Label(row1, text='(登録済みタスクがありません)', foreground='gray').pack(
                side='left', padx=6
            )

        row2 = ttk.Frame(f)
        row2.pack(fill='x', pady=4)
        ttk.Label(row2, text='実行条件:').pack(side='left', padx=(0, 4))
        ttk.Combobox(
            row2,
            textvariable=self._chain_condition_var,
            values=['success', 'failed', 'any'],
            state='readonly',
            width=12,
        ).pack(side='left')
        ttk.Label(
            row2,
            text='  success=正常終了時  /  failed=エラー終了時  /  any=完了時',
            foreground='gray',
        ).pack(side='left', padx=4)

        row3 = ttk.Frame(f)
        row3.pack(fill='x', pady=4)
        ttk.Label(row3, text='フォールバック時刻:').pack(side='left', padx=(0, 4))
        ttk.Entry(row3, textvariable=self._chain_fallback_var, width=12).pack(side='left')
        ttk.Label(row3, text='(例: 09:00 または 09:00:00、省略可)').pack(side='left', padx=6)
        ttk.Label(
            f,
            text='前提タスクが条件を満たしたとき、または指定時刻になったとき(どちらか早い方)に実行します。',
            foreground='gray',
        ).pack(anchor='w', padx=2, pady=(2, 0))

    # ------------------------------------------------------------------ イベント

    def _on_cmd_changed(self, *_) -> None:
        cmd = self._cmd_var.get()
        if not self._name_var.get():
            self._name_var.set(guess_task_name(cmd))
        if self._runtime_var.get() == 'auto':
            pass  # auto のまま表示する

    def _browse_command(self) -> None:
        path = filedialog.askopenfilename(
            title='スクリプト/実行ファイルを選択',
            filetypes=[
                ('スクリプトファイル', '*.py *.ps1 *.bat *.cmd *.exe'),
                ('Pythonスクリプト', '*.py'),
                ('PowerShellスクリプト', '*.ps1'),
                ('バッチファイル', '*.bat *.cmd'),
                ('すべてのファイル', '*.*'),
            ],
        )
        if path:
            self._cmd_var.set(path)

    def _browse_workdir(self) -> None:
        path = filedialog.askdirectory(title='作業ディレクトリを選択')
        if path:
            self._workdir_var.set(path)

    def _on_ok(self) -> None:
        try:
            result = self._collect()
        except ValueError as exc:
            messagebox.showerror('入力エラー', str(exc), parent=self)
            return
        self.result = result
        self.destroy()

    def _collect(self) -> TaskDefinition:
        name = self._name_var.get().strip()
        if not name:
            raise ValueError('タスク名は必須です。')

        cmd = self._cmd_var.get().strip()
        if not cmd:
            raise ValueError('コマンドは必須です。')

        timeout = _optional_int(self._timeout_var.get(), 'タイムアウト')
        retry_count = _safe_int(self._retry_count_var.get(), 'リトライ回数', 0, 99)
        retry_delay = _safe_int(self._retry_delay_var.get(), 'リトライ間隔', 0)

        env: dict[str, str] = {}
        for line in self._env_text.get('1.0', 'end').splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if '=' not in stripped:
                raise ValueError(f'環境変数は KEY=VALUE 形式で入力してください: {stripped}')
            k, v = stripped.split('=', 1)
            k = k.strip()
            if not k:
                raise ValueError('環境変数のキーが空です。')
            env[k] = v.strip()

        schedule = self._collect_schedule()
        now_str = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

        if self._task:
            import uuid as _uuid
            task_id = self._task.id
        else:
            import uuid as _uuid
            task_id = str(_uuid.uuid4())

        return TaskDefinition(
            id=task_id,
            name=name,
            description=self._desc_var.get().strip(),
            enabled=self._enabled_var.get(),
            runtime=self._runtime_var.get(),
            command_text=cmd,
            working_directory=self._workdir_var.get().strip(),
            run_as_admin=self._run_as_admin_var.get(),
            timeout_seconds=timeout,
            retry_count=retry_count,
            retry_delay_seconds=retry_delay,
            environment=env,
            schedule=schedule,
            created_at=self._task.created_at if self._task else now_str,
            updated_at=now_str,
        )

    def _collect_schedule(self) -> dict[str, Any]:
        t = self._schedule_type_var.get()
        sched: dict[str, Any] = {'type': t}

        if t == 'interval':
            days = _safe_int(self._int_days_var.get(), '日', 0)
            hours = _safe_int(self._int_hours_var.get(), '時間', 0)
            minutes = _safe_int(self._int_minutes_var.get(), '分', 0)
            seconds = _safe_int(self._int_seconds_var.get(), '秒', 0)
            if days + hours + minutes + seconds == 0:
                raise ValueError('interval の場合は合計が1秒以上になるよう設定してください。')
            sched.update({'days': days, 'hours': hours, 'minutes': minutes, 'seconds': seconds})

        elif t in ('daily', 'weekly', 'monthly'):
            sched.update({
                'hour': _safe_int(self._time_hour_var.get(), '時', 0, 23),
                'minute': _safe_int(self._time_minute_var.get(), '分', 0, 59),
                'second': _safe_int(self._time_second_var.get(), '秒', 0, 59),
            })
            if t == 'weekly':
                weekdays = [i for i, v in enumerate(self._weekly_day_vars) if v.get()]
                if not weekdays:
                    raise ValueError('weekly の場合は曜日を1つ以上選択してください。')
                sched['weekdays'] = weekdays
            if t == 'monthly':
                sched['day'] = _safe_int(self._monthly_day_var.get(), '日', 1, 31)

        elif t == 'once':
            run_at = self._once_var.get().strip()
            if not run_at:
                raise ValueError('once の場合は実行日時を入力してください。')
            try:
                datetime.fromisoformat(run_at.replace(' ', 'T'))
            except ValueError:
                raise ValueError('実行日時の形式が正しくありません。(例: 2026-04-30 09:00:00)')
            sched['run_at'] = run_at

        elif t == 'chain':
            after_task_name = self._chain_task_var.get().strip()
            if not after_task_name:
                raise ValueError('chain の場合は前提タスクを選択してください。')
            after_task_id = self._chain_task_name_to_id.get(after_task_name, '')
            if not after_task_id:
                raise ValueError('選択されたタスクが見つかりません。')
            sched['after_task_id'] = after_task_id
            sched['after_task_name'] = after_task_name

            condition = self._chain_condition_var.get().strip()
            if condition not in ('success', 'failed', 'any'):
                condition = 'success'
            sched['on_condition'] = condition

            fallback_time = self._chain_fallback_var.get().strip()
            if fallback_time:
                try:
                    parts = fallback_time.split(':')
                    h = int(parts[0])
                    m = int(parts[1]) if len(parts) > 1 else 0
                    s = int(parts[2]) if len(parts) > 2 else 0
                    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
                        raise ValueError()
                    sched['fallback_time'] = fallback_time
                except (ValueError, IndexError):
                    raise ValueError(
                        'フォールバック時刻の形式が正しくありません。(例: 09:00 または 09:00:00)'
                    )

        return sched
