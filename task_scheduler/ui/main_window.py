from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Callable

from ..executor import TaskExecutor
from ..models import RunHistorySummary, TaskDefinition, schedule_summary
from ..pid_store import PidStore
from ..scheduler import TaskScheduler
from ..storage import Storage
from .gantt_window import GanttWindow
from .history_window import HistoryWindow
from .task_dialog import TaskDialog


class MainWindow(tk.Tk):
    """タスクスケジューラのメインウィンドウ"""

    _COL_DEFS = [
        ('name',         '名前',          200),
        ('status',       '状態',          100),
        ('command',      'コマンド',        260),
        ('schedule',     'スケジュール',  180),
        ('next_run',     '次回実行',      140),
        ('last_run',     '最終実行',      140),
        ('last_status',  '最終状態',       90),
    ]

    _HIST_COL_DEFS = [
        ('task_name',   'タスク名',   180),
        ('started_at',  '開始時刻',   140),
        ('finished_at', '終了時刻',   140),
        ('status',     '状態',        90),
        ('exit_code',  '終了コード',  80),
        ('duration',   '所要時間',    80),
        ('trigger',    'トリガー',    80),
    ]

    def __init__(
        self,
        storage: Storage,
        pid_store: PidStore,
        executor: TaskExecutor,
        scheduler: TaskScheduler,
    ) -> None:
        super().__init__()
        self.title('タスクスケジューラ V2')
        self.geometry('960x680')
        self.minsize(720, 480)

        self._storage = storage
        self._pid_store = pid_store
        self._executor = executor
        self._scheduler = scheduler
        self._on_quit: Callable[[], None] | None = None
        self._refresh_after_id: str | None = None
        self._tasks_cache: list[TaskDefinition] = []
        self._hist_entry_map: dict[str, RunHistorySummary] = {}
        self._ui_call_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._refresh_in_progress = False
        self._refresh_pending = False

        # ウィンドウを閉じたときは非表示にする（トレイに残る）
        self.protocol('WM_DELETE_WINDOW', self.withdraw)

        # 状態変化時に UI を更新するコールバックを登録
        self._executor.on_status_change = lambda _task_id: self.run_on_ui_thread(self._on_status_changed)

        self._build_ui()
        self._start_ui_queue_pump()
        self._trigger_background_refresh(debounce_ms=0)
        self._start_poll()

    def set_quit_callback(self, cb: Callable[[], None]) -> None:
        self._on_quit = cb

    def show_window(self) -> None:
        """トレイから呼ばれる: ウィンドウを前面に表示する"""
        self.run_on_ui_thread(self._do_show)

    def run_on_ui_thread(self, callback: Callable[[], None]) -> None:
        """別スレッドからの UI 更新要求をメインスレッドへ受け渡す"""
        self._ui_call_queue.put(callback)

    def _do_show(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

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

    # ------------------------------------------------------------------ UI 構築

    def _build_ui(self) -> None:
        # ツールバー
        tb = ttk.Frame(self, padding=(4, 4))
        tb.pack(fill='x', side='top')

        def btn(text: str, cmd: Callable, width: int = 8) -> ttk.Button:
            b = ttk.Button(tb, text=text, command=cmd, width=width)
            b.pack(side='left', padx=2)
            return b

        self._btn_add = btn('追加', self._on_add)
        self._btn_edit = btn('編集', self._on_edit)
        self._btn_copy = btn('コピー', self._on_copy)
        self._btn_delete = btn('削除', self._on_delete)
        self._btn_move_up = btn('↑ 上へ', self._on_move_up)
        self._btn_move_down = btn('↓ 下へ', self._on_move_down)
        ttk.Separator(tb, orient='vertical').pack(side='left', fill='y', padx=4)
        self._btn_run = btn('実行', self._on_run)
        self._btn_stop = btn('停止', self._on_stop)
        ttk.Separator(tb, orient='vertical').pack(side='left', fill='y', padx=4)
        self._btn_toggle = btn('有効化', self._on_toggle_enabled)
        ttk.Separator(tb, orient='vertical').pack(side='left', fill='y', padx=4)
        self._btn_history = btn('履歴/ログ', self._on_history)
        self._btn_history_all = btn('全体履歴', self._on_history_all, width=10)
        self._btn_gantt = btn('ガント', self._on_gantt, width=8)

        # ステータスバー（最下部に先に配置）
        self._status_var = tk.StringVar(value='準備完了')
        ttk.Label(self, textvariable=self._status_var, anchor='w', relief='sunken').pack(
            fill='x', side='bottom', padx=4, pady=(0, 2)
        )

        # PanedWindow で上下分割
        paned = ttk.PanedWindow(self, orient='vertical')
        paned.pack(fill='both', expand=True, padx=8, pady=(0, 4))

        # 上段：タスク一覧
        task_frame = ttk.Frame(paned)
        paned.add(task_frame, weight=3)

        cols = [c[0] for c in self._COL_DEFS]
        self._tree = ttk.Treeview(task_frame, columns=cols, show='headings', selectmode='browse')
        for col_id, heading, width in self._COL_DEFS:
            self._tree.heading(col_id, text=heading, command=lambda c=col_id: self._sort_by(c))
            self._tree.column(col_id, width=width, minwidth=60)

        vsb_t = ttk.Scrollbar(task_frame, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb_t.set)
        vsb_t.pack(side='right', fill='y')
        self._tree.pack(side='left', fill='both', expand=True)

        self._tree.tag_configure('running',  foreground='#0044bb')
        self._tree.tag_configure('disabled', foreground='#888888')
        self._tree.tag_configure('orphan',   foreground='#006688')

        self._tree.bind('<<TreeviewSelect>>', self._on_task_select)
        self._tree.bind('<Double-1>', lambda _e: self._on_edit())
        self._tree.bind('<Button-3>', self._on_tree_right_click)

        # コンテキストメニュー
        self._ctx_menu = tk.Menu(self, tearoff=False)
        self._ctx_menu.add_command(label='追加',     command=self._on_add)               # 0
        self._ctx_menu.add_separator()                                                    # 1
        self._ctx_menu.add_command(label='編集',     command=self._on_edit)              # 2
        self._ctx_menu.add_command(label='コピー',   command=self._on_copy)              # 3
        self._ctx_menu.add_command(label='削除',     command=self._on_delete)            # 4
        self._ctx_menu.add_separator()                                                    # 5
        self._ctx_menu.add_command(label='↑ 上へ',  command=self._on_move_up)           # 6
        self._ctx_menu.add_command(label='↓ 下へ',  command=self._on_move_down)         # 7
        self._ctx_menu.add_separator()                                                    # 8
        self._ctx_menu.add_command(label='今すぐ実行', command=self._on_run)             # 9
        self._ctx_menu.add_command(label='停止',     command=self._on_stop)              # 10
        self._ctx_menu.add_separator()                                                    # 11
        self._ctx_menu.add_command(label='有効化/無効化', command=self._on_toggle_enabled)  # 12
        self._ctx_menu.add_separator()                                                    # 13
        self._ctx_menu.add_command(label='履歴/ログ', command=self._on_history)           # 14
        self._CTX_IDX_TOGGLE = 12  # 有効化/無効化エントリのインデックス

        # 下段：実行履歴パネル
        hist_outer = ttk.Frame(paned)
        paned.add(hist_outer, weight=2)

        self._hist_label_var = tk.StringVar(value='実行履歴サマリ（全タスク・最新50件）')
        ttk.Label(hist_outer, textvariable=self._hist_label_var, anchor='w',
                  font=('', 9, 'bold')).pack(fill='x', padx=2, pady=(4, 0))

        hist_frame = ttk.Frame(hist_outer)
        hist_frame.pack(fill='both', expand=True, pady=(2, 0))

        hcols = [c[0] for c in self._HIST_COL_DEFS]
        self._hist_tree = ttk.Treeview(hist_frame, columns=hcols, show='headings',
                                       selectmode='browse', height=8)
        for col_id, heading, width in self._HIST_COL_DEFS:
            self._hist_tree.heading(col_id, text=heading)
            self._hist_tree.column(col_id, width=width, minwidth=50)

        vsb_h = ttk.Scrollbar(hist_frame, orient='vertical', command=self._hist_tree.yview)
        self._hist_tree.configure(yscrollcommand=vsb_h.set)
        vsb_h.pack(side='right', fill='y')
        self._hist_tree.pack(side='left', fill='both', expand=True)

        self._hist_tree.tag_configure('success', foreground='#006600')
        self._hist_tree.tag_configure('failed',  foreground='#cc0000')
        self._hist_tree.tag_configure('running', foreground='#0044bb')
        self._hist_tree.tag_configure('timeout', foreground='#885500')
        self._hist_tree.tag_configure('killed',  foreground='#884488')
        self._hist_tree.bind('<ButtonRelease-1>', self._on_hist_tree_click)
        self._hist_tree.bind('<Return>', self._on_hist_tree_enter)

    # ------------------------------------------------------------------ ツリー更新

    def _refresh_tree(
        self,
        tasks: list[TaskDefinition] | None = None,
        last_hist_all: dict[str, RunHistorySummary] | None = None,
    ) -> None:
        if tasks is None:
            tasks = self._storage.list_tasks()
        if last_hist_all is None:
            last_hist_all = self._storage.get_last_history_all()
        sel_id = self._selected_task_id()

        self._tree.delete(*self._tree.get_children())
        for task in tasks:
            running = self._pid_store.is_running(task.id)
            last_entry = last_hist_all.get(task.id)

            if running and task.enabled:
                status_label = '実行中'
                tag = 'running'
            elif not task.enabled:
                status_label = '無効'
                tag = 'disabled'
            else:
                status_label = '待機中'
                tag = ''

            next_run = self._scheduler.get_next_run_time(task.id) or '-'
            last_run = last_entry.started_at if last_entry else '-'
            last_status = (
                _STATUS_LABEL.get(last_entry.status, last_entry.status)
                if last_entry
                else '-'
            )

            self._tree.insert(
                '', 'end', iid=task.id,
                values=(
                    task.name,
                    status_label,
                    task.command_text,
                    schedule_summary(task.schedule),
                    next_run,
                    last_run,
                    last_status,
                ),
                tags=(tag,),
            )

        # 選択を復元
        if sel_id and self._tree.exists(sel_id):
            self._tree.selection_set(sel_id)
            self._tree.focus(sel_id)

        self._update_btn_states()
        total = len(tasks)
        running_count = sum(1 for t in tasks if self._pid_store.is_running(t.id))
        self._status_var.set(f'タスク合計: {total}件  実行中: {running_count}件')

    def _refresh_history_panel(
        self,
        entries: list[RunHistorySummary] | None = None,
        task_name_map: dict[str, str] | None = None,
        hist_label: str | None = None,
    ) -> None:
        """下部履歴パネルを更新する"""
        if entries is None:
            task = self._selected_task()
            if task is not None:
                entries = self._storage.list_history(task.id, limit=50)
                task_name_map = {task.id: task.name}
                hist_label = f'実行履歴サマリ — {task.name}（最新50件）'
            else:
                entries = self._storage.list_all_recent_history(limit=50)
                task_name_map = {t.id: t.name for t in self._storage.list_tasks()}
                hist_label = '実行履歴サマリ（全タスク・最新50件）'
        if hist_label is not None:
            self._hist_label_var.set(hist_label)

        self._hist_entry_map = {}
        self._hist_tree.delete(*self._hist_tree.get_children())
        for entry in entries:
            if entry.finished_at:
                try:
                    s = datetime.fromisoformat(entry.started_at)
                    e = datetime.fromisoformat(entry.finished_at)
                    secs = int((e - s).total_seconds())
                    duration = f'{secs}s'
                except ValueError:
                    duration = '-'
                finished = entry.finished_at
            else:
                duration = '-'
                finished = '-'

            tname = task_name_map.get(entry.task_id, entry.task_id)
            exit_code = str(entry.exit_code) if entry.exit_code is not None else '-'
            status_label = _STATUS_LABEL.get(entry.status, entry.status)
            tag = entry.status if entry.status in ('success', 'failed', 'running',
                                                   'timeout', 'killed') else ''
            self._hist_entry_map[entry.id] = entry
            self._hist_tree.insert(
                '', 'end',
                iid=entry.id,
                values=(tname, entry.started_at, finished, status_label,
                        exit_code, duration, entry.trigger),
                tags=(tag,),
            )

    # ------------------------------------------------------------------ イベント

    def _on_tree_right_click(self, event: tk.Event) -> None:
        """右クリックでコンテキストメニューを表示する"""
        # クリック位置の行を選択する
        row = self._tree.identify_row(event.y)
        if row:
            self._tree.selection_set(row)
            self._tree.focus(row)
        self._update_ctx_menu()
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _update_ctx_menu(self) -> None:
        """コンテキストメニューの有効・無効をボタン状態に合わせて更新する"""
        task = self._selected_task()
        has = task is not None
        running = has and self._pid_store.is_running(task.id)

        def _state(cond: bool) -> str:
            return 'normal' if cond else 'disabled'

        task_ids = [t.id for t in self._tasks_cache]
        idx = task_ids.index(task.id) if (task and task.id in task_ids) else -1
        can_up   = has and idx > 0
        can_down = has and 0 <= idx < len(task_ids) - 1

        self._ctx_menu.entryconfigure('編集',       state=_state(has))
        self._ctx_menu.entryconfigure('コピー',     state=_state(has))
        self._ctx_menu.entryconfigure('削除',       state=_state(has and not running))
        self._ctx_menu.entryconfigure('↑ 上へ',    state=_state(can_up))
        self._ctx_menu.entryconfigure('↓ 下へ',    state=_state(can_down))
        self._ctx_menu.entryconfigure('今すぐ実行', state=_state(has and not running))
        self._ctx_menu.entryconfigure('停止',       state=_state(running))
        self._ctx_menu.entryconfigure('履歴/ログ', state=_state(has))
        # 有効化/無効化はラベルが動的に変わるため数値インデックスで操作する
        toggle_label = '有効化' if (task and not task.enabled) else '無効化' if task else '有効化/無効化'
        self._ctx_menu.entryconfigure(self._CTX_IDX_TOGGLE, state=_state(has), label=toggle_label)

    def _on_task_select(self, _event: object = None) -> None:
        self._update_btn_states()
        self._trigger_background_refresh()

    def _on_status_changed(self) -> None:
        self._trigger_background_refresh()

    def _on_hist_tree_click(self, event: tk.Event) -> None:
        row = self._hist_tree.identify_row(event.y)
        if not row:
            return
        self._hist_tree.selection_set(row)
        self._hist_tree.focus(row)
        self._open_history_from_summary(row)

    def _on_hist_tree_enter(self, _event: tk.Event) -> None:
        sel = self._hist_tree.selection()
        if not sel:
            return
        self._open_history_from_summary(sel[0])

    def _open_history_from_summary(self, history_id: str) -> None:
        entry = self._hist_entry_map.get(history_id)
        if entry is None:
            return
        task = next((t for t in self._tasks_cache if t.id == entry.task_id), None)
        if task is None:
            task = self._storage.get_task(entry.task_id)
        if task is not None:
            HistoryWindow(
                self,
                self._storage,
                task=task,
                history_id=entry.id,
                on_history_changed=self._on_status_changed,
            )
            return
        HistoryWindow(self, self._storage, history_id=entry.id, on_history_changed=self._on_status_changed)

    def _trigger_background_refresh(self, debounce_ms: int = 150) -> None:
        """デバウンス付きでバックグラウンドリフレッシュをスケジュールする"""
        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except Exception:
                pass
        self._refresh_after_id = self.after(debounce_ms, self._launch_background_refresh)

    def _launch_background_refresh(self) -> None:
        """デバウンス後に実際にバックグラウンドスレッドを起動する"""
        self._refresh_after_id = None
        if self._refresh_in_progress:
            self._refresh_pending = True
            return

        self._refresh_in_progress = True
        sel_id = self._selected_task_id()
        threading.Thread(
            target=self._background_refresh,
            args=(sel_id,),
            daemon=True,
            name='UIRefresh',
        ).start()

    def _background_refresh(self, sel_id: str | None) -> None:
        """バックグラウンドスレッドで実行するデータ収集処理"""
        try:
            tasks = self._storage.list_tasks()
            self.run_on_ui_thread(lambda: self._apply_tasks_only(tasks))

            last_hist_all = self._storage.get_last_history_all()
            task_name_map = {t.id: t.name for t in tasks}
            sel_task = next((t for t in tasks if t.id == sel_id), None) if sel_id else None
            if sel_task is not None:
                hist_entries = self._storage.list_history(sel_id, limit=50)
                hist_label = f'実行履歴サマリ — {sel_task.name}（最新50件）'
            else:
                hist_entries = self._storage.list_all_recent_history(limit=50)
                hist_label = '実行履歴サマリ（全タスク・最新50件）'
            self.run_on_ui_thread(
                lambda: self._apply_refresh(tasks, last_hist_all, hist_entries, task_name_map, hist_label)
            )
        except Exception:
            self.run_on_ui_thread(self._finish_background_refresh)

    def _apply_tasks_only(self, tasks: list[TaskDefinition]) -> None:
        """履歴サマリ更新前でもタスク一覧だけは先に表示する"""
        self._tasks_cache = tasks
        self._refresh_tree(tasks, {})

    def _apply_refresh(
        self,
        tasks: list[TaskDefinition],
        last_hist_all: dict[str, RunHistorySummary],
        hist_entries: list[RunHistorySummary],
        task_name_map: dict[str, str],
        hist_label: str,
    ) -> None:
        """収集済みデータでUI（ツリーと履歴パネル）を更新する（メインスレッド）"""
        try:
            self._tasks_cache = tasks
            self._refresh_tree(tasks, last_hist_all)
            self._refresh_history_panel(hist_entries, task_name_map, hist_label)
        finally:
            self._finish_background_refresh()

    def _finish_background_refresh(self) -> None:
        self._refresh_in_progress = False
        if self._refresh_pending:
            self._refresh_pending = False
            self._trigger_background_refresh(debounce_ms=0)

    def _start_poll(self) -> None:
        self.after(3000, self._poll)

    def _poll(self) -> None:
        try:
            self._trigger_background_refresh(debounce_ms=0)
        finally:
            self.after(3000, self._poll)

    def _sort_by(self, col: str) -> None:
        items = [(self._tree.set(k, col), k) for k in self._tree.get_children()]
        items.sort()
        for idx, (_, k) in enumerate(items):
            self._tree.move(k, '', idx)

    # ------------------------------------------------------------------ ボタン状態

    def _update_btn_states(self) -> None:
        task = self._selected_task()
        has = task is not None
        running = has and self._pid_store.is_running(task.id)

        self._btn_edit.configure(state='normal' if has else 'disabled')
        self._btn_copy.configure(state='normal' if has else 'disabled')
        self._btn_delete.configure(state='normal' if has else 'disabled')
        self._btn_run.configure(state='normal' if has and not running else 'disabled')
        self._btn_stop.configure(state='normal' if running else 'disabled')
        self._btn_history.configure(state='normal' if has else 'disabled')

        if task:
            self._btn_toggle.configure(text='無効化' if task.enabled else '有効化')

        task_ids = [t.id for t in self._tasks_cache]
        idx = task_ids.index(task.id) if (task and task.id in task_ids) else -1
        self._btn_move_up.configure(state='normal' if (has and idx > 0) else 'disabled')
        self._btn_move_down.configure(state='normal' if (has and 0 <= idx < len(task_ids) - 1) else 'disabled')

    # ------------------------------------------------------------------ ハンドラ

    def _on_add(self) -> None:
        dlg = TaskDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            self._storage.save_task(dlg.result)
            self._scheduler.reload_task(dlg.result)
            self._trigger_background_refresh(debounce_ms=0)

    def _on_edit(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        dlg = TaskDialog(self, task=task)
        self.wait_window(dlg)
        if dlg.result:
            self._storage.save_task(dlg.result)
            self._scheduler.reload_task(dlg.result)
            self._trigger_background_refresh(debounce_ms=0)

    def _on_copy(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        import uuid
        now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        copied = TaskDefinition(
            id=str(uuid.uuid4()),
            name=task.name + ' (コピー)',
            description=task.description,
            enabled=False,
            runtime=task.runtime,
            command_text=task.command_text,
            working_directory=task.working_directory,
            run_as_admin=task.run_as_admin,
            timeout_seconds=task.timeout_seconds,
            retry_count=task.retry_count,
            retry_delay_seconds=task.retry_delay_seconds,
            environment=dict(task.environment),
            schedule=dict(task.schedule),
            created_at=now,
            updated_at=now,
        )
        dlg = TaskDialog(self, task=copied)
        self.wait_window(dlg)
        if dlg.result:
            self._storage.save_task(dlg.result)
            self._scheduler.reload_task(dlg.result)
            self._trigger_background_refresh(debounce_ms=0)

    def _on_delete(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        if self._pid_store.is_running(task.id):
            messagebox.showwarning('削除不可', '実行中のタスクは削除できません。先に停止してください。', parent=self)
            return
        if not messagebox.askyesno('削除確認', f'タスク「{task.name}」を削除しますか？', parent=self):
            return
        self._scheduler.remove_task(task.id)
        self._storage.delete_task(task.id)
        self._trigger_background_refresh(debounce_ms=0)

    def _on_run(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        self._scheduler.run_now(task.id)
        self.after(300, lambda: self._trigger_background_refresh(debounce_ms=0))

    def _on_stop(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        self._executor.kill_task(task.id)
        self.after(300, lambda: self._trigger_background_refresh(debounce_ms=0))

    def _on_move_up(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        if self._storage.reorder_task(task.id, -1):
            self._trigger_background_refresh(debounce_ms=0)

    def _on_move_down(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        if self._storage.reorder_task(task.id, +1):
            self._trigger_background_refresh(debounce_ms=0)

    def _on_toggle_enabled(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        task.enabled = not task.enabled
        task.updated_at = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        self._storage.save_task(task)
        self._scheduler.reload_task(task)
        self._trigger_background_refresh(debounce_ms=0)

    def _on_history(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        HistoryWindow(self, self._storage, task, on_history_changed=self._on_status_changed)

    def _on_history_all(self) -> None:
        HistoryWindow(self, self._storage, on_history_changed=self._on_status_changed)

    def _on_gantt(self) -> None:
        GanttWindow(self, self._storage, self._scheduler, on_schedule_changed=self._on_status_changed)

    # ------------------------------------------------------------------ ユーティリティ

    def _selected_task_id(self) -> str | None:
        sel = self._tree.selection()
        return sel[0] if sel else None

    def _selected_task(self) -> TaskDefinition | None:
        task_id = self._selected_task_id()
        if task_id is None:
            return None
        for task in self._tasks_cache:
            if task.id == task_id:
                return task
        return self._storage.get_task(task_id)


_STATUS_LABEL: dict[str, str] = {
    'running': '実行中',
    'success': '成功',
    'failed': '失敗',
    'timeout': 'タイムアウト',
    'killed': '中断',
    'unknown_exit': '不明終了',
}
