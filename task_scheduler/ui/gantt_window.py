from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import ttk
from typing import Callable

from ..models import RunHistorySummary, TaskDefinition, schedule_summary
from ..scheduler import ScheduleOccurrence, TaskScheduler
from ..storage import Storage
from .task_dialog import TaskDialog

_RANGE_OPTIONS: dict[str, timedelta] = {
    '24時間': timedelta(hours=24),
    '3日': timedelta(days=3),
    '7日': timedelta(days=7),
    '14日': timedelta(days=14),
    '30日': timedelta(days=30),
}

_TYPE_COLORS: dict[str, str] = {
    'interval': '#4f8bd6',
    'daily': '#3f9d6a',
    'weekly': '#d99239',
    'monthly': '#9a6fd1',
    'once': '#c85f5f',
}


class GanttWindow(tk.Toplevel):
    """全タスクの今後のスケジュールをガント風に表示するウィンドウ"""

    _LABEL_WIDTH = 260
    _HEADER_HEIGHT = 82
    _ROW_HEIGHT = 44
    _BAR_TOP = 16
    _BAR_HEIGHT = 18

    def __init__(
        self,
        master: tk.Misc,
        storage: Storage,
        scheduler: TaskScheduler,
        on_schedule_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title('全タスク ガントチャート')
        self.geometry('1280x760')
        self.minsize(920, 520)

        self._storage = storage
        self._scheduler = scheduler
        self._on_schedule_changed = on_schedule_changed
        self._range_var = tk.StringVar(value='7日')
        self._status_var = tk.StringVar(value='ガントチャートを読み込み中です')
        self._info_var = tk.StringVar(value='')
        self._ui_call_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._refresh_token = 0
        self._tasks: list[TaskDefinition] = []
        self._occurrences: list[ScheduleOccurrence] = []
        self._window_start: datetime | None = None
        self._window_end: datetime | None = None
        self._task_map: dict[str, TaskDefinition] = {}

        self._build_ui()
        self._start_ui_queue_pump()
        self._refresh()

    def _build_ui(self) -> None:
        controls = ttk.Frame(self, padding=(8, 8, 8, 4))
        controls.pack(fill='x')

        ttk.Label(controls, text='表示期間:').pack(side='left')
        range_box = ttk.Combobox(
            controls,
            textvariable=self._range_var,
            values=list(_RANGE_OPTIONS.keys()),
            width=10,
            state='readonly',
        )
        range_box.pack(side='left', padx=(4, 8))
        range_box.bind('<<ComboboxSelected>>', lambda _e: self._refresh())
        ttk.Button(controls, text='更新', command=self._refresh, width=10).pack(side='left')

        ttk.Label(
            controls,
            text='色: 間隔=青 / 日次=緑 / 週次=橙 / 月次=紫 / 単発=赤 / 灰色=無効 / 破線=推定時間',
            anchor='w',
        ).pack(side='left', fill='x', expand=True, padx=(12, 0))

        chart_frame = ttk.Frame(self)
        chart_frame.pack(fill='both', expand=True, padx=8, pady=(0, 6))

        self._corner_canvas = tk.Canvas(
            chart_frame,
            background='#eef2f8',
            highlightthickness=0,
            width=self._LABEL_WIDTH,
            height=self._HEADER_HEIGHT,
        )
        self._header_canvas = tk.Canvas(
            chart_frame,
            background='#eef2f8',
            highlightthickness=0,
            height=self._HEADER_HEIGHT,
        )
        self._label_canvas = tk.Canvas(
            chart_frame,
            background='#fbfbfd',
            highlightthickness=0,
            width=self._LABEL_WIDTH,
        )
        self._canvas = tk.Canvas(chart_frame, background='#fbfbfd', highlightthickness=0)

        hsb = ttk.Scrollbar(chart_frame, orient='horizontal', command=self._on_horizontal_scroll)
        vsb = ttk.Scrollbar(chart_frame, orient='vertical', command=self._on_vertical_scroll)
        self._header_canvas.configure(xscrollcommand=hsb.set)
        self._label_canvas.configure(yscrollcommand=vsb.set)
        self._canvas.configure(xscrollcommand=hsb.set, yscrollcommand=vsb.set)

        self._corner_canvas.grid(row=0, column=0, sticky='nsew')
        self._header_canvas.grid(row=0, column=1, sticky='ew')
        self._label_canvas.grid(row=1, column=0, sticky='nsew')
        self._canvas.grid(row=1, column=1, sticky='nsew')
        vsb.grid(row=1, column=2, sticky='ns')
        hsb.grid(row=2, column=1, sticky='ew')
        chart_frame.columnconfigure(1, weight=1)
        chart_frame.rowconfigure(1, weight=1)

        self._bind_mousewheel(self._header_canvas)
        self._bind_mousewheel(self._label_canvas)
        self._bind_mousewheel(self._canvas)

        ttk.Label(self, textvariable=self._status_var, anchor='w').pack(fill='x', padx=8)
        self._info_label = ttk.Label(self, textvariable=self._info_var, anchor='w', relief='sunken')

    def _bind_mousewheel(self, widget: tk.Canvas) -> None:
        widget.bind('<Enter>', lambda _e, target=widget: target.focus_set())
        widget.bind('<MouseWheel>', self._on_mousewheel)
        widget.bind('<Shift-MouseWheel>', self._on_mousewheel)
        widget.bind('<Button-4>', self._on_mousewheel)
        widget.bind('<Button-5>', self._on_mousewheel)
        widget.bind('<Shift-Button-4>', self._on_mousewheel)
        widget.bind('<Shift-Button-5>', self._on_mousewheel)

    def _on_mousewheel(self, event: tk.Event) -> str:
        units = self._mousewheel_units(event)
        if units == 0:
            return 'break'

        if bool(getattr(event, 'state', 0) & 0x0001):
            self._scroll_x(units * 3)
        else:
            self._scroll_y(units * 3)
        return 'break'

    def _mousewheel_units(self, event: tk.Event) -> int:
        delta = int(getattr(event, 'delta', 0) or 0)
        if delta != 0:
            units = -int(delta / 120)
            if units == 0:
                return -1 if delta > 0 else 1
            return units

        num = getattr(event, 'num', None)
        if num == 4:
            return -1
        if num == 5:
            return 1
        return 0

    def _scroll_x(self, units: int) -> None:
        self._header_canvas.xview_scroll(units, 'units')
        self._canvas.xview_scroll(units, 'units')

    def _scroll_y(self, units: int) -> None:
        self._label_canvas.yview_scroll(units, 'units')
        self._canvas.yview_scroll(units, 'units')

    def _on_horizontal_scroll(self, *args: str) -> None:
        self._header_canvas.xview(*args)
        self._canvas.xview(*args)

    def _on_vertical_scroll(self, *args: str) -> None:
        self._label_canvas.yview(*args)
        self._canvas.yview(*args)

    def _clear_chart(self) -> None:
        self._corner_canvas.delete('all')
        self._header_canvas.delete('all')
        self._label_canvas.delete('all')
        self._canvas.delete('all')

    def _run_on_ui_thread(self, callback: Callable[[], None]) -> None:
        self._ui_call_queue.put(callback)

    def _start_ui_queue_pump(self) -> None:
        self.after(50, self._process_ui_queue)

    def _process_ui_queue(self) -> None:
        while True:
            try:
                callback = self._ui_call_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                pass
        try:
            self.after(50, self._process_ui_queue)
        except tk.TclError:
            pass

    def _refresh(self) -> None:
        self._refresh_token += 1
        token = self._refresh_token
        self._status_var.set('ガントチャートを読み込み中です')
        self._set_info_message('')
        self._clear_chart()
        self._header_canvas.configure(scrollregion=(0, 0, 1, self._HEADER_HEIGHT))
        self._label_canvas.configure(scrollregion=(0, 0, self._LABEL_WIDTH, 1))
        self._canvas.configure(scrollregion=(0, 0, 1, 1))
        self._on_horizontal_scroll('moveto', '0')
        self._on_vertical_scroll('moveto', '0')
        threading.Thread(
            target=self._load_chart_data,
            args=(token,),
            daemon=True,
            name='GanttWindowRefresh',
        ).start()

    def _load_chart_data(self, token: int) -> None:
        try:
            tasks = self._storage.list_tasks()
            last_history = self._storage.get_last_history_all()
            span = _RANGE_OPTIONS.get(self._range_var.get(), timedelta(days=7))

            now = datetime.now()
            if span <= timedelta(hours=24):
                window_start = now.replace(minute=0, second=0, microsecond=0)
            else:
                window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            window_end = window_start + span

            preview_start, preview_end, occurrences = self._scheduler.preview_schedule(
                tasks,
                start=window_start,
                end=window_end,
                last_history=last_history,
                limit_per_task=2000,
            )
            self._run_on_ui_thread(
                lambda: self._apply_chart_data(token, tasks, preview_start, preview_end, occurrences, last_history)
            )
        except Exception as exc:
            self._run_on_ui_thread(lambda: self._apply_error(token, str(exc)))

    def _apply_chart_data(
        self,
        token: int,
        tasks: list[TaskDefinition],
        window_start: datetime,
        window_end: datetime,
        occurrences: list[ScheduleOccurrence],
        last_history: dict[str, RunHistorySummary],
    ) -> None:
        if token != self._refresh_token:
            return

        self._tasks = tasks
        self._task_map = {task.id: task for task in tasks}
        self._occurrences = occurrences
        self._window_start = window_start
        self._window_end = window_end
        self._draw_chart(last_history)

    def _apply_error(self, token: int, message: str) -> None:
        if token != self._refresh_token:
            return
        self._clear_chart()
        self._canvas.create_text(24, 24, anchor='nw', text=f'ガントチャートの読込に失敗しました: {message}')
        self._status_var.set('ガントチャートの読込に失敗しました')

    def _draw_chart(self, last_history: dict[str, RunHistorySummary]) -> None:
        self._clear_chart()
        if self._window_start is None or self._window_end is None:
            return

        tasks = self._tasks
        occurrences = self._occurrences
        if not tasks:
            self._canvas.create_text(24, 24, anchor='nw', text='表示対象のタスクがありません。')
            self._status_var.set('表示対象のタスクがありません')
            return

        span_hours = max(1.0, (self._window_end - self._window_start).total_seconds() / 3600.0)
        pixels_per_hour = self._pixels_per_hour(span_hours)
        timeline_width = max(640, int(span_hours * pixels_per_hour))
        total_width = timeline_width + 24
        body_height = len(tasks) * self._ROW_HEIGHT + 20
        self._corner_canvas.configure(scrollregion=(0, 0, self._LABEL_WIDTH, self._HEADER_HEIGHT))
        self._header_canvas.configure(scrollregion=(0, 0, total_width, self._HEADER_HEIGHT))
        self._label_canvas.configure(scrollregion=(0, 0, self._LABEL_WIDTH, body_height))
        self._canvas.configure(scrollregion=(0, 0, total_width, body_height))

        by_task: dict[str, list[ScheduleOccurrence]] = {task.id: [] for task in tasks}
        for occurrence in occurrences:
            by_task.setdefault(occurrence.task_id, []).append(occurrence)

        self._draw_header(total_width, span_hours, pixels_per_hour)

        for index, task in enumerate(tasks):
            y0 = index * self._ROW_HEIGHT
            y1 = y0 + self._ROW_HEIGHT
            bg = '#ffffff' if index % 2 == 0 else '#f7f8fb'
            label_bg = '#f3f4f8' if index % 2 == 0 else '#eef1f6'
            task_tag = f'task-{task.id}'

            self._canvas.create_rectangle(0, y0, total_width, y1, fill=bg, outline='#e6e8ef')
            self._label_canvas.create_rectangle(
                0,
                y0,
                self._LABEL_WIDTH,
                y1,
                fill=label_bg,
                outline='#d9dde8',
                tags=(task_tag,),
            )

            name = task.name + (' [無効]' if not task.enabled else '')
            self._label_canvas.create_text(10, y0 + 11, anchor='w', text=name, fill='#222222', tags=(task_tag,))
            self._label_canvas.create_text(
                10,
                y0 + 29,
                anchor='w',
                text=schedule_summary(task.schedule),
                fill='#666666',
                font=('', 8),
                tags=(task_tag,),
            )
            self._label_canvas.tag_bind(task_tag, '<Button-1>', lambda _e, task_id=task.id: self._open_task_dialog(task_id))
            self._label_canvas.tag_bind(task_tag, '<Enter>', lambda _e: self._label_canvas.configure(cursor='hand2'))
            self._label_canvas.tag_bind(task_tag, '<Leave>', lambda _e: self._label_canvas.configure(cursor=''))

        self._draw_body_grid(span_hours, pixels_per_hour, body_height)

        for index, task in enumerate(tasks):
            y0 = index * self._ROW_HEIGHT
            row_occurrences = by_task.get(task.id, [])
            if not row_occurrences:
                self._canvas.create_text(
                    10,
                    y0 + self._ROW_HEIGHT / 2,
                    anchor='w',
                    text='予定なし',
                    fill='#999999',
                    font=('', 8),
                )
                continue

            for occ_index, occurrence in enumerate(row_occurrences):
                self._draw_occurrence_bar(
                    task,
                    last_history.get(task.id),
                    occurrence,
                    occ_index,
                    pixels_per_hour,
                    y0,
                )

        self._status_var.set(
            f'{len(tasks)} 件のタスク / 予定 {len(occurrences)} 件 / '
            f'{self._window_start.strftime("%Y-%m-%d %H:%M")} - {self._window_end.strftime("%Y-%m-%d %H:%M")}'
        )

    def _draw_header(self, total_width: int, span_hours: float, pixels_per_hour: float) -> None:
        self._corner_canvas.create_rectangle(
            0,
            0,
            self._LABEL_WIDTH,
            self._HEADER_HEIGHT,
            fill='#eef2f8',
            outline='#d9dde8',
        )
        self._header_canvas.create_rectangle(
            0,
            0,
            total_width,
            self._HEADER_HEIGHT,
            fill='#eef2f8',
            outline='#d9dde8',
        )
        self._corner_canvas.create_text(10, 16, anchor='w', text='タスク', fill='#333333')
        self._header_canvas.create_text(
            10,
            12,
            anchor='w',
            text=f'期間: {self._window_start.strftime("%m/%d %H:%M")} - {self._window_end.strftime("%m/%d %H:%M")}',
            fill='#333333',
        )

        minor_grid_hours = self._minor_grid_step_hours(span_hours)
        minor_step = timedelta(hours=minor_grid_hours)
        minor_tick = self._align_tick(self._window_start, minor_grid_hours)
        while minor_tick < self._window_end:
            segment_start = max(minor_tick, self._window_start)
            segment_end = min(minor_tick + minor_step, self._window_end)
            x = self._time_to_x(segment_start, pixels_per_hour)
            self._header_canvas.create_line(x, 30, x, self._HEADER_HEIGHT, fill='#dfe3eb')
            if segment_end > segment_start:
                self._header_canvas.create_text(
                    self._segment_mid_x(segment_start, segment_end, pixels_per_hour),
                    54,
                    anchor='n',
                    text=segment_start.strftime('%H:%M'),
                    fill='#5a6070',
                    font=('', 8),
                )
            minor_tick += minor_step

        day_tick = self._window_start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day_tick < self._window_end:
            day_start = max(day_tick, self._window_start)
            day_end = min(day_tick + timedelta(days=1), self._window_end)
            x = self._time_to_x(day_start, pixels_per_hour)
            self._header_canvas.create_line(
                x,
                0,
                x,
                self._HEADER_HEIGHT,
                fill='#c8cfde',
                width=2,
            )
            if day_end > day_start:
                self._header_canvas.create_text(
                    self._segment_mid_x(day_start, day_end, pixels_per_hour),
                    30,
                    anchor='n',
                    text=day_tick.strftime('%m/%d'),
                    fill='#344054',
                    font=('', 8, 'bold'),
                )
            day_tick += timedelta(days=1)

        now = datetime.now(self._window_start.tzinfo)
        if self._window_start <= now <= self._window_end:
            x = self._time_to_x(now, pixels_per_hour)
            self._header_canvas.create_line(
                x,
                0,
                x,
                self._HEADER_HEIGHT,
                fill='#d14d4d',
                width=2,
            )
            self._header_canvas.create_text(
                x + 4,
                2,
                anchor='nw',
                text='now',
                fill='#b63f3f',
                font=('', 8, 'bold'),
            )

    def _draw_body_grid(self, span_hours: float, pixels_per_hour: float, body_height: int) -> None:
        minor_grid_hours = self._minor_grid_step_hours(span_hours)
        minor_tick = self._align_tick(self._window_start, minor_grid_hours)
        while minor_tick <= self._window_end:
            x = self._time_to_x(minor_tick, pixels_per_hour)
            self._canvas.create_line(x, 0, x, body_height, fill='#e2e5ed')
            minor_tick += timedelta(hours=minor_grid_hours)

        day_tick = self._window_start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day_tick <= self._window_end:
            x = self._time_to_x(day_tick, pixels_per_hour)
            self._canvas.create_line(x, 0, x, body_height, fill='#c8cfde', width=2)
            day_tick += timedelta(days=1)

        now = datetime.now(self._window_start.tzinfo)
        if self._window_start <= now <= self._window_end:
            x = self._time_to_x(now, pixels_per_hour)
            self._canvas.create_line(x, 0, x, body_height, fill='#d14d4d', width=2)

    def _draw_occurrence_bar(
        self,
        task: TaskDefinition,
        last_history: RunHistorySummary | None,
        occurrence: ScheduleOccurrence,
        occ_index: int,
        pixels_per_hour: float,
        y0: int,
    ) -> None:
        x0 = self._time_to_x(occurrence.start_time, pixels_per_hour)
        x1 = self._time_to_x(occurrence.end_time, pixels_per_hour)
        if x1 < x0 + 4:
            x1 = x0 + 4
        y_top = y0 + self._BAR_TOP
        y_bottom = y_top + self._BAR_HEIGHT
        fill = self._bar_color(occurrence)
        outline = '#344054' if occurrence.enabled else '#7d8596'
        tag = f'occ-{task.id}-{occ_index}'

        self._canvas.create_rectangle(
            x0,
            y_top,
            x1,
            y_bottom,
            fill=fill,
            outline=outline,
            width=1,
            dash=(4, 2) if occurrence.estimated else (),
            tags=(tag,),
        )
        label_text = occurrence.start_time.strftime('%H:%M')
        if x1 - x0 >= 42:
            self._canvas.create_text(
                x0 + 4,
                y_top + self._BAR_HEIGHT / 2,
                anchor='w',
                text=label_text,
                fill='white',
                font=('', 8, 'bold'),
                tags=(tag,),
            )
        else:
            self._canvas.create_text(
                x0 + 2,
                y0 + 3,
                anchor='nw',
                text=label_text,
                fill='#344054',
                font=('', 7),
                tags=(tag,),
            )

        info = self._format_occurrence_info(task, occurrence, last_history)
        self._canvas.tag_bind(tag, '<Button-1>', lambda _e, text=info: self._set_info_message(text))

    def _format_occurrence_info(
        self,
        task: TaskDefinition,
        occurrence: ScheduleOccurrence,
        last_history: RunHistorySummary | None,
    ) -> str:
        estimate_label = '前回実績' if not occurrence.estimated else '仮置き'
        last_label = '-'
        if last_history and last_history.duration_seconds is not None:
            last_label = self._format_duration(last_history.duration_seconds)
        return (
            f'{task.name} / {schedule_summary(task.schedule)} / '
            f'{occurrence.start_time.strftime("%Y-%m-%d %H:%M:%S")} - '
            f'{occurrence.end_time.strftime("%Y-%m-%d %H:%M:%S")} / '
            f'表示長: {self._format_duration(occurrence.duration_seconds)} ({estimate_label}) / '
            f'前回実績: {last_label}'
        )

    def _bar_color(self, occurrence: ScheduleOccurrence) -> str:
        if not occurrence.enabled:
            return '#b7bcc8'
        return _TYPE_COLORS.get(occurrence.schedule_type, '#4f8bd6')

    def _time_to_x(self, value: datetime, pixels_per_hour: float) -> float:
        assert self._window_start is not None
        delta = value - self._window_start
        return (delta.total_seconds() / 3600.0) * pixels_per_hour

    def _pixels_per_hour(self, span_hours: float) -> float:
        if span_hours <= 24:
            return 96.0
        if span_hours <= 72:
            return 48.0
        if span_hours <= 168:
            return 24.0
        if span_hours <= 336:
            return 14.0
        return 8.0

    def _minor_grid_step_hours(self, span_hours: float) -> int:
        if span_hours <= 24:
            return 1
        if span_hours <= 72:
            return 2
        if span_hours <= 168:
            return 3
        if span_hours <= 336:
            return 6
        return 12

    def _align_tick(self, start: datetime, step_hours: int) -> datetime:
        if step_hours >= 24:
            tick = start.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            aligned_hour = (start.hour // step_hours) * step_hours
            tick = start.replace(hour=aligned_hour, minute=0, second=0, microsecond=0)
        if tick > start:
            tick -= timedelta(hours=step_hours)
        return tick

    def _format_duration(self, seconds: float) -> str:
        total_seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f'{hours}時間{minutes}分{secs}秒'
        if minutes > 0:
            return f'{minutes}分{secs}秒'
        return f'{secs}秒'

    def _segment_mid_x(self, start: datetime, end: datetime, pixels_per_hour: float) -> float:
        return (self._time_to_x(start, pixels_per_hour) + self._time_to_x(end, pixels_per_hour)) / 2

    def _set_info_message(self, text: str) -> None:
        self._info_var.set(text)
        if text:
            if not self._info_label.winfo_manager():
                self._info_label.pack(fill='x', padx=8, pady=(4, 8))
            return
        if self._info_label.winfo_manager():
            self._info_label.pack_forget()

    def _open_task_dialog(self, task_id: str) -> None:
        task = self._task_map.get(task_id)
        if task is None:
            task = self._storage.get_task(task_id)
        if task is None:
            return

        dlg = TaskDialog(self, task=task, tasks=list(self._task_map.values()))
        self.wait_window(dlg)
        if dlg.result is None:
            return

        self._storage.save_task(dlg.result)
        self._scheduler.reload_task(dlg.result)
        if self._on_schedule_changed is not None:
            self._on_schedule_changed()
        self._refresh()