from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable

from ..models import RunHistoryDetail, RunHistorySummary, TaskDefinition
from ..storage import Storage

if TYPE_CHECKING:
    pass

_STATUS_LABELS: dict[str, str] = {
    'running': '実行中',
    'success': '成功',
    'failed': '失敗',
    'timeout': 'タイムアウト',
    'killed': '中断',
    'unknown_exit': '不明終了',
}

_STATUS_TAGS: dict[str, str] = {
    'running': 'running',
    'success': 'success',
    'failed': 'failed',
    'timeout': 'timeout',
    'killed': 'killed',
    'unknown_exit': 'unknown',
}


class _LineNumberedText(ttk.Frame):
    """行番号ガターを持つテキスト表示ウィジェット"""

    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master)
        vsb = ttk.Scrollbar(self, orient='vertical')
        hsb = ttk.Scrollbar(self, orient='horizontal')
        self._lineno = tk.Text(
            self,
            width=4,
            padx=4,
            state='disabled',
            cursor='arrow',
            background='#f0f0f0',
            foreground='#888888',
            relief='flat',
            wrap='none',
            takefocus=False,
        )
        self._text = tk.Text(
            self,
            yscrollcommand=self._sync_scroll,
            xscrollcommand=hsb.set,
            **kwargs,
        )
        vsb.configure(command=self._yview)
        hsb.configure(command=self._text.xview)
        self._lineno.grid(row=0, column=0, sticky='nsew')
        self._text.grid(row=0, column=1, sticky='nsew')
        vsb.grid(row=0, column=2, sticky='ns')
        hsb.grid(row=1, column=1, sticky='ew')
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._vsb = vsb

    def _sync_scroll(self, first: str, last: str) -> None:
        self._vsb.set(first, last)
        self._lineno.yview_moveto(first)

    def _yview(self, *args) -> None:
        self._text.yview(*args)
        self._lineno.yview(*args)

    def configure(self, **kwargs) -> None:  # type: ignore[override]
        state = kwargs.pop('state', None)
        if state is not None:
            self._text.configure(state=state)
        if kwargs:
            super().configure(**kwargs)

    def delete(self, index1: str, index2: str) -> None:
        self._text.delete(index1, index2)

    def insert(self, index: str, chars: str) -> None:
        self._text.insert(index, chars)
        self._update_linenos()

    def _update_linenos(self) -> None:
        count = int(self._text.index('end-1c').split('.')[0])
        width = max(len(str(count)), 3)
        numbers = '\n'.join(str(i) for i in range(1, count + 1))
        self._lineno.configure(state='normal', width=width + 1)
        self._lineno.delete('1.0', 'end')
        self._lineno.insert('1.0', numbers)
        self._lineno.configure(state='disabled')


class HistoryWindow(tk.Toplevel):
    """タスク実行履歴と詳細ログを表示するウィンドウ"""

    def __init__(
        self,
        master: tk.Misc,
        storage: Storage,
        task: TaskDefinition | None = None,
        limit: int | None = None,
        history_id: str | None = None,
        on_history_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self._task = task
        self._limit = limit if limit is not None else (500 if task is None else 200)
        self._show_task_name = task is None
        self.title('全体履歴/ログ' if task is None else f'履歴/ログ: {task.name}')
        self.geometry('900x560')
        self.minsize(700, 400)
        self._storage = storage
        self._entries: list[RunHistorySummary] = []
        self._detail_cache: dict[str, RunHistoryDetail] = {}
        self._requested_history_id = history_id
        self._on_history_changed = on_history_changed
        self._refresh_token = 0
        self._detail_token = 0
        self._ui_call_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._build_ui()
        self._start_ui_queue_pump()
        self._refresh()

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

    def _build_ui(self) -> None:
        # 上部: Treeview
        top = ttk.Frame(self)
        top.pack(fill='both', expand=True, padx=8, pady=(8, 0))

        cols: tuple[str, ...]
        if self._show_task_name:
            cols = ('task_name', 'started_at', 'finished_at', 'status', 'exit_code', 'duration', 'trigger', 'attempt')
        else:
            cols = ('started_at', 'finished_at', 'status', 'exit_code', 'duration', 'trigger', 'attempt')
        self._tree = ttk.Treeview(top, columns=cols, show='headings', selectmode='extended')
        if self._show_task_name:
            self._tree.heading('task_name', text='タスク名')
        self._tree.heading('started_at', text='開始時刻')
        self._tree.heading('finished_at', text='終了時刻')
        self._tree.heading('status', text='状態')
        self._tree.heading('exit_code', text='終了コード')
        self._tree.heading('duration', text='所要時間')
        self._tree.heading('trigger', text='トリガー')
        self._tree.heading('attempt', text='試行回数')

        if self._show_task_name:
            self._tree.column('task_name', width=200, minwidth=160)
        self._tree.column('started_at', width=160, minwidth=140)
        self._tree.column('finished_at', width=160, minwidth=140)
        self._tree.column('status', width=100, minwidth=80)
        self._tree.column('exit_code', width=80, minwidth=60, anchor='center')
        self._tree.column('duration', width=90, minwidth=70, anchor='center')
        self._tree.column('trigger', width=80, minwidth=70, anchor='center')
        self._tree.column('attempt', width=70, minwidth=60, anchor='center')

        vsb = ttk.Scrollbar(top, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self._tree.pack(side='left', fill='both', expand=True)

        self._tree.tag_configure('success', foreground='#1a7a1a')
        self._tree.tag_configure('failed', foreground='#bb2222')
        self._tree.tag_configure('timeout', foreground='#b06000')
        self._tree.tag_configure('killed', foreground='#666600')
        self._tree.tag_configure('running', foreground='#0044bb')
        self._tree.tag_configure('unknown', foreground='#888888')

        self._tree.bind('<<TreeviewSelect>>', self._on_select)
        self._tree.bind('<Delete>', self._on_delete_key)

        # ボタン行
        btn_row = ttk.Frame(self)
        btn_row.pack(fill='x', padx=8, pady=4)
        ttk.Label(
            btn_row,
            text='上段は実行履歴、下段タブは選択した実行の詳細ログです。',
            anchor='w',
        ).pack(side='left', fill='x', expand=True)
        self._btn_delete = ttk.Button(btn_row, text='削除', command=self._delete_selected_history, width=10)
        self._btn_delete.pack(side='right', padx=(0, 4))
        ttk.Button(btn_row, text='更新', command=self._refresh, width=10).pack(side='right')

        # 下部: stdout/stderr ペイン
        sep = ttk.Separator(self, orient='horizontal')
        sep.pack(fill='x', padx=8)

        detail_nb = ttk.Notebook(self)
        detail_nb.pack(fill='both', expand=True, padx=8, pady=(4, 8))

        self._stdout_text = _LineNumberedText(detail_nb, wrap='none', height=10, state='disabled')
        self._stderr_text = _LineNumberedText(detail_nb, wrap='none', height=10, state='disabled')
        detail_nb.add(self._stdout_text, text='標準出力 (stdout)')
        detail_nb.add(self._stderr_text, text='標準エラー (stderr)')

    def _refresh(self) -> None:
        self._refresh_token += 1
        token = self._refresh_token
        self._detail_token += 1
        self._detail_cache.clear()
        self._entries = []
        self._tree.delete(*self._tree.get_children())
        self._set_text(self._stdout_text, '(履歴を読み込み中です)')
        self._set_text(self._stderr_text, '(履歴を読み込み中です)')
        self._update_delete_button_state()
        threading.Thread(
            target=self._load_entries_in_background,
            args=(token,),
            daemon=True,
            name='HistoryWindowRefresh',
        ).start()

    def _load_entries_in_background(self, token: int) -> None:
        try:
            if self._task is None:
                entries = self._storage.list_all_recent_history(
                    limit=self._limit,
                    include_legacy=True,
                )
            else:
                entries = self._storage.list_history(
                    self._task.id,
                    limit=self._limit,
                    include_legacy=True,
                )
            self._run_on_ui_thread(lambda: self._apply_entries(token, entries))
        except Exception as exc:
            self._run_on_ui_thread(lambda: self._apply_load_error(token, str(exc)))

    def _apply_entries(self, token: int, entries: list[RunHistorySummary]) -> None:
        if token != self._refresh_token:
            return
        self._entries = entries
        self._tree.delete(*self._tree.get_children())
        for entry in self._entries:
            dur = f'{entry.duration_seconds:.1f}s' if entry.duration_seconds is not None else '-'
            tag = _STATUS_TAGS.get(entry.status, '')
            label = _STATUS_LABELS.get(entry.status, entry.status)
            values: tuple[object, ...]
            if self._show_task_name:
                values = (
                    entry.task_name or entry.task_id,
                    entry.started_at,
                    entry.finished_at or '-',
                    label,
                    entry.exit_code if entry.exit_code is not None else '-',
                    dur,
                    'スケジューラ' if entry.trigger == 'scheduler' else '手動',
                    entry.attempt_count,
                )
            else:
                values = (
                    entry.started_at,
                    entry.finished_at or '-',
                    label,
                    entry.exit_code if entry.exit_code is not None else '-',
                    dur,
                    'スケジューラ' if entry.trigger == 'scheduler' else '手動',
                    entry.attempt_count,
                )
            self._tree.insert(
                '',
                'end',
                iid=entry.id,
                values=values,
                tags=(tag,),
            )
        self._set_text(self._stdout_text, '(履歴を選択すると詳細ログを読み込みます)')
        self._set_text(self._stderr_text, '(履歴を選択すると詳細ログを読み込みます)')
        self._update_delete_button_state()
        if self._requested_history_id and self._tree.exists(self._requested_history_id):
            self._tree.selection_set(self._requested_history_id)
            self._tree.focus(self._requested_history_id)
            self._tree.see(self._requested_history_id)
            self._update_delete_button_state()
            self._load_selected_detail()
            self._requested_history_id = None

    def _apply_load_error(self, token: int, message: str) -> None:
        if token != self._refresh_token:
            return
        self._entries = []
        self._tree.delete(*self._tree.get_children())
        self._set_text(self._stdout_text, f'(履歴の読込に失敗しました: {message})')
        self._set_text(self._stderr_text, '(履歴の読込に失敗したため詳細は表示できません)')
        self._update_delete_button_state()

    def _on_select(self, _event: tk.Event) -> None:
        self._update_delete_button_state()
        self._load_selected_detail()

    def _on_delete_key(self, _event: tk.Event) -> None:
        self._delete_selected_history()

    def _selected_entries(self) -> list[RunHistorySummary]:
        sel = self._tree.selection()
        if not sel:
            return []
        selected_ids = set(sel)
        return [entry for entry in self._entries if entry.id in selected_ids]

    def _selected_entry(self) -> RunHistorySummary | None:
        entries = self._selected_entries()
        if not entries:
            return None

        focused_id = self._tree.focus()
        if focused_id:
            for entry in entries:
                if entry.id == focused_id:
                    return entry
        return entries[0]

    def _update_delete_button_state(self) -> None:
        entries = self._selected_entries()
        can_delete = bool(entries) and all(entry.status != 'running' for entry in entries)
        self._btn_delete.configure(state='normal' if can_delete else 'disabled')

    def _load_selected_detail(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return

        detail = self._detail_cache.get(entry.id)
        if detail is None:
            self._detail_token += 1
            token = self._detail_token
            self._set_text(self._stdout_text, '(標準出力を読み込み中です)')
            self._set_text(self._stderr_text, '(標準エラーを読み込み中です)')
            threading.Thread(
                target=self._load_detail_in_background,
                args=(token, entry.task_id, entry.id),
                daemon=True,
                name='HistoryDetailLoad',
            ).start()
            return

        self._set_text(self._stdout_text, detail.stdout)
        self._set_text(self._stderr_text, detail.stderr)

    def _load_detail_in_background(self, token: int, task_id: str, history_id: str) -> None:
        try:
            detail = self._storage.get_history_detail(task_id, history_id)
            self._run_on_ui_thread(lambda: self._apply_detail(token, history_id, detail))
        except Exception as exc:
            self._run_on_ui_thread(lambda: self._apply_detail_error(token, history_id, str(exc)))

    def _apply_detail(self, token: int, history_id: str, detail: RunHistoryDetail) -> None:
        if token != self._detail_token:
            return
        sel = self._tree.selection()
        if not sel or history_id not in sel:
            return
        self._detail_cache[history_id] = detail
        self._set_text(self._stdout_text, detail.stdout)
        self._set_text(self._stderr_text, detail.stderr)

    def _apply_detail_error(self, token: int, history_id: str, message: str) -> None:
        if token != self._detail_token:
            return
        sel = self._tree.selection()
        if not sel or history_id not in sel:
            return
        self._set_text(self._stdout_text, '(詳細ログの読込に失敗しました)')
        self._set_text(self._stderr_text, message)

    def _delete_selected_history(self) -> None:
        entries = self._selected_entries()
        if not entries:
            return
        running_entries = [entry for entry in entries if entry.status == 'running']
        if running_entries:
            messagebox.showwarning('削除不可', '実行中の履歴は削除できません。', parent=self)
            return

        target_count = len(entries)
        preview = '\n'.join(
            f'- {(entry.task_name or entry.task_id)} / {entry.started_at}'
            for entry in entries[:5]
        )
        if target_count > 5:
            preview += f'\n- ほか {target_count - 5} 件'
        if not messagebox.askyesno(
            '履歴削除',
            f'{target_count} 件の履歴を削除しますか？\n\n{preview}',
            parent=self,
        ):
            return

        failed_entries = [
            entry for entry in entries
            if not self._storage.delete_history_entry(entry.task_id, entry.id)
        ]
        deleted_count = target_count - len(failed_entries)
        if deleted_count <= 0:
            messagebox.showwarning(
                '削除失敗',
                f'{len(failed_entries)} 件の履歴を削除できませんでした。',
                parent=self,
            )
            return

        self._requested_history_id = None
        self._detail_token += 1
        self._set_text(self._stdout_text, f'({deleted_count} 件の履歴を削除しました)')
        self._set_text(self._stderr_text, f'({deleted_count} 件の履歴を削除しました)')
        if self._on_history_changed is not None:
            self._on_history_changed()
        self._refresh()
        if failed_entries:
            messagebox.showwarning(
                '一部削除失敗',
                f'{deleted_count} 件を削除し、{len(failed_entries)} 件は削除できませんでした。',
                parent=self,
            )

    def _set_text(self, widget: _LineNumberedText, content: str) -> None:
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', content or '(出力なし)')
        widget.configure(state='disabled')
