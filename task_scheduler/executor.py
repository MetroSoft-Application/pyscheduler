from __future__ import annotations

import locale
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .models import PidEntry, RunHistoryEntry, TaskDefinition
from .pid_store import PidStore, kill_pid, pid_alive
from .storage import Storage

PYTHON_EXTENSIONS = {'.py', '.pyw'}
POWERSHELL_EXTENSIONS = {'.ps1', '.psm1', '.psd1'}
COMMAND_EXTENSIONS = {'.bat', '.cmd', '.exe', '.com'}


def _now_str() -> str:
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def _creation_flags() -> int:
    if os.name != 'nt':
        return 0
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)


def _decode_output_chunk(chunk: bytes) -> str:
    if not chunk:
        return ''

    encodings = ['utf-8', 'utf-8-sig']
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encodings.append(preferred)
    if os.name == 'nt':
        encodings.extend(['cp932', 'mbcs'])

    seen: set[str] = set()
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return chunk.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue

    return chunk.decode('utf-8', errors='replace')


def _read_output_file(path: Path) -> str:
    try:
        return _decode_output_chunk(path.read_bytes())
    except OSError:
        return ''


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _resolve_task_path(task: TaskDefinition, target: str) -> Path:
    path = Path(target).expanduser()
    if path.is_absolute():
        return path

    workdir = task.working_directory.strip()
    if workdir:
        return Path(workdir).expanduser() / path
    return path


def _task_runtime(task: TaskDefinition) -> str:
    return task.runtime if task.runtime != 'auto' else infer_runtime(task.command_text)


def _build_task_env_overrides(task: TaskDefinition) -> dict[str, str]:
    overrides = dict(task.environment)
    runtime = _task_runtime(task)
    if runtime in ('python', 'powershell'):
        overrides.setdefault('PYTHONUTF8', '1')
        overrides.setdefault('PYTHONIOENCODING', 'utf-8')
    return overrides


def _build_process_env(task: TaskDefinition) -> dict[str, str] | None:
    overrides = _build_task_env_overrides(task)
    if not overrides:
        return None
    return {**os.environ, **overrides}


def _build_powershell_command(script: str, args: list[str]) -> list[str]:
    invocation = '& ' + ' '.join([_powershell_literal(script), *(_powershell_literal(arg) for arg in args)])
    command = (
        "$utf8NoBom = New-Object System.Text.UTF8Encoding($false); "
        "[Console]::InputEncoding = $utf8NoBom; "
        "[Console]::OutputEncoding = $utf8NoBom; "
        "$OutputEncoding = $utf8NoBom; "
        f"{invocation}"
    )
    return [
        'powershell',
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-NonInteractive',
        '-Command', command,
    ]


def infer_runtime(command_text: str) -> str:
    """コマンドテキストからランタイムを自動判定する"""
    tokens = shlex.split(command_text.strip(), posix=False)
    if not tokens:
        return 'command'
    suffix = Path(tokens[0].strip('"\'').strip()).suffix.lower()
    if suffix in PYTHON_EXTENSIONS:
        return 'python'
    if suffix in POWERSHELL_EXTENSIONS:
        return 'powershell'
    return 'command'


def guess_task_name(command_text: str) -> str:
    tokens = shlex.split(command_text.strip(), posix=False)
    if tokens:
        stem = Path(tokens[0].strip('"\'').strip()).stem
        return stem or tokens[0][:40]
    return command_text[:40]


def build_command(task: TaskDefinition) -> list[str]:
    """タスク定義からサブプロセスに渡す引数リストを構築する"""
    runtime = _task_runtime(task)
    raw = task.command_text.strip()
    tokens = shlex.split(raw, posix=False)
    if not tokens:
        return []

    # 最初のトークンから引用符を除去してパスとして扱う
    script = tokens[0].strip('"\'').strip()
    args = tokens[1:]

    if runtime == 'python':
        return [sys.executable, '-X', 'utf8', script] + args

    if runtime == 'powershell':
        return _build_powershell_command(script, args)

    # command / その他: cmd /c で実行
    return ['cmd', '/c'] + tokens


def validate_task_command(task: TaskDefinition) -> str | None:
    raw = task.command_text.strip()
    tokens = shlex.split(raw, posix=False)
    if not tokens:
        return 'コマンドが空です。'

    runtime = _task_runtime(task)
    target = tokens[0].strip('"\'').strip()

    if runtime in ('python', 'powershell'):
        script_path = _resolve_task_path(task, target)
        if not script_path.exists():
            return f'スクリプトが存在しません: {script_path}'
        return None

    if runtime == 'command':
        has_path_hint = ('\\' in target or '/' in target) or Path(target).suffix.lower() in COMMAND_EXTENSIONS
        if has_path_hint:
            command_path = _resolve_task_path(task, target)
            if command_path.suffix and not command_path.exists():
                return f'実行ファイルが存在しません: {command_path}'

    return None


class TaskExecutor:
    """タスクをサブプロセスとして実行・管理するクラス"""

    def __init__(self, storage: Storage, pid_store: PidStore) -> None:
        self._storage = storage
        self._pid_store = pid_store
        self._kill_requested: set[str] = set()
        self._kill_lock = threading.Lock()
        # 状態変化を通知するコールバック(task_id を引数に受け取る)
        self.on_status_change: Callable[[str], None] | None = None
        # 成功完了時に通知するコールバック(task_id を引数に受け取る)
        self.on_task_success: Callable[[str], None] | None = None
        # タスク完了時に通知するコールバック(task_id, final_status を引数に受け取る)
        self.on_task_complete: Callable[[str, str], None] | None = None

    def kill_task(self, task_id: str) -> bool:
        """実行中タスクを停止する"""
        with self._kill_lock:
            self._kill_requested.add(task_id)
        return self._pid_store.kill(task_id)

    def execute(self, task: TaskDefinition, trigger: str = 'scheduler') -> None:
        """タスクを実行する（ブロッキング）。必ずスレッドから呼ぶこと。
        retry_count > 0 の場合は失敗時にリトライする。
        """
        if self._pid_store.is_running(task.id):
            return  # 二重起動防止

        max_attempts = max(1, task.retry_count + 1)
        history_id = str(uuid.uuid4())
        started_at = _now_str()

        # 実行中として先行記録
        entry = RunHistoryEntry(
            id=history_id,
            task_id=task.id,
            task_name=task.name,
            started_at=started_at,
            finished_at=None,
            status='running',
            exit_code=None,
            stdout='',
            stderr='',
            duration_seconds=None,
            trigger=trigger,
            attempt_count=0,
        )
        self._storage.append_history(entry)

        final_status = 'failed'
        final_exit_code: int | None = None
        accumulated_stdout: list[str] = []
        accumulated_stderr: list[str] = []
        total_start = time.monotonic()

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                # リトライ前に停止要求を確認する
                with self._kill_lock:
                    if task.id in self._kill_requested:
                        break
                time.sleep(task.retry_delay_seconds)

            entry.attempt_count = attempt
            status, exit_code, stdout_str, stderr_str = self._run_once(
                task, history_id
            )

            accumulated_stdout.append(
                f'--- Attempt {attempt} ---\n{stdout_str}' if attempt > 1 else stdout_str
            )
            accumulated_stderr.append(
                f'--- Attempt {attempt} ---\n{stderr_str}' if attempt > 1 else stderr_str
            )

            final_status = status
            final_exit_code = exit_code

            # 成功 / キル / タイムアウトはリトライしない
            if status in ('success', 'killed', 'timeout'):
                break

        duration = time.monotonic() - total_start
        entry.finished_at = _now_str()
        entry.status = final_status
        entry.exit_code = final_exit_code
        entry.stdout = ''.join(accumulated_stdout)
        entry.stderr = ''.join(accumulated_stderr)
        entry.has_stdout = bool(entry.stdout)
        entry.has_stderr = bool(entry.stderr)
        entry.duration_seconds = round(duration, 3)
        self._storage.update_history(entry)

        if self.on_status_change:
            self.on_status_change(task.id)

        if final_status == 'success' and self.on_task_success:
            self.on_task_success(task.id)

        if self.on_task_complete:
            self.on_task_complete(task.id, final_status)

    def _run_once(
        self,
        task: TaskDefinition,
        history_id: str,
    ) -> tuple[str, int | None, str, str]:
        """1回分の実行を行い (status, exit_code, stdout, stderr) を返す"""
        cmd = build_command(task)
        if not cmd:
            return 'failed', None, '', 'コマンドが空です。'

        validation_error = validate_task_command(task)
        if validation_error is not None:
            return 'failed', None, '', validation_error

        cwd = task.working_directory.strip() or None
        env = _build_process_env(task)
        env_overrides = _build_task_env_overrides(task)

        if os.name == 'nt' and task.run_as_admin:
            return self._run_once_elevated(task, cmd, cwd, env_overrides, history_id)

        return self._run_once_standard(task, cmd, cwd, env, history_id)

    def _run_once_standard(
        self,
        task: TaskDefinition,
        cmd: list[str],
        cwd: str | None,
        env: dict[str, str] | None,
        history_id: str,
    ) -> tuple[str, int | None, str, str]:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                creationflags=_creation_flags(),
            )
        except Exception as exc:
            return 'failed', None, '', str(exc)

        # PID を永続化
        self._pid_store.register(
            task.id,
            PidEntry(
                pid=proc.pid,
                started_at=_now_str(),
                history_id=history_id,
                task_name=task.name,
            ),
        )
        if self.on_status_change:
            self.on_status_change(task.id)

        # stdout / stderr を別スレッドで読み取る（デッドロック防止）
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _read(stream, chunks: list[str]) -> None:
            if stream is None:
                return
            try:
                for line in iter(stream.readline, b''):
                    chunks.append(_decode_output_chunk(line))
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=_read, args=(proc.stdout, stdout_chunks), daemon=True)
        t_err = threading.Thread(target=_read, args=(proc.stderr, stderr_chunks), daemon=True)
        t_out.start()
        t_err.start()

        start_time = time.monotonic()
        timed_out = False

        while True:
            try:
                proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                # ユーザーによる停止要求を確認
                with self._kill_lock:
                    killed_requested = task.id in self._kill_requested
                if killed_requested:
                    kill_pid(proc.pid)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    break

                # タイムアウト確認
                if task.timeout_seconds:
                    elapsed = time.monotonic() - start_time
                    if elapsed >= task.timeout_seconds:
                        timed_out = True
                        kill_pid(proc.pid)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        break

        t_out.join(timeout=5)
        t_err.join(timeout=5)

        stdout_str = ''.join(stdout_chunks)
        stderr_str = ''.join(stderr_chunks)
        exit_code = proc.returncode

        # PID 登録解除
        self._pid_store.unregister(task.id)

        with self._kill_lock:
            killed_by_user = task.id in self._kill_requested
            self._kill_requested.discard(task.id)

        if timed_out:
            status = 'timeout'
        elif killed_by_user:
            status = 'killed'
        elif exit_code == 0:
            status = 'success'
        else:
            status = 'failed'

        return status, exit_code, stdout_str, stderr_str

    def _run_once_elevated(
        self,
        task: TaskDefinition,
        cmd: list[str],
        cwd: str | None,
        env_overrides: dict[str, str],
        history_id: str,
    ) -> tuple[str, int | None, str, str]:
        temp_dir = Path(tempfile.mkdtemp(prefix='task_scheduler_'))
        stdout_path = temp_dir / 'stdout.log'
        stderr_path = temp_dir / 'stderr.log'
        exit_code_path = temp_dir / 'exit_code.txt'
        pid_path = temp_dir / 'target_pid.txt'
        wrapper_path = temp_dir / 'elevated_wrapper.ps1'
        launcher_path = temp_dir / 'elevated_launcher.ps1'

        try:
            wrapper_path.write_text(
                self._build_elevated_wrapper_script(
                    cmd,
                    cwd,
                    env_overrides,
                    stdout_path,
                    stderr_path,
                    exit_code_path,
                    pid_path,
                ),
                encoding='utf-8',
            )
            launcher_path.write_text(
                self._build_elevated_launcher_script(wrapper_path),
                encoding='utf-8',
            )

            launcher_cmd = [
                'powershell',
                '-NoProfile',
                '-ExecutionPolicy', 'Bypass',
                '-NonInteractive',
                '-File', str(launcher_path),
            ]

            try:
                launcher = subprocess.Popen(
                    launcher_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=_creation_flags(),
                )
            except Exception as exc:
                return 'failed', None, '', str(exc)

            launcher_stdout_bytes, launcher_stderr_bytes = launcher.communicate()
            launcher_stdout = _decode_output_chunk(launcher_stdout_bytes).strip()
            launcher_stderr = _decode_output_chunk(launcher_stderr_bytes).strip()

            if launcher.returncode != 0:
                message = launcher_stderr or launcher_stdout or '管理者権限での起動に失敗しました。'
                return 'failed', None, '', message

            wrapper_pid = self._extract_first_pid(launcher_stdout)
            if wrapper_pid is None:
                message = launcher_stderr or launcher_stdout or '管理者権限での起動後にプロセス情報を取得できませんでした。'
                return 'failed', None, '', message

            target_pid = self._wait_for_target_pid(pid_path, wrapper_pid, timeout_seconds=30.0)
            if target_pid is None:
                stderr_str = _read_output_file(stderr_path).strip()
                message = stderr_str or launcher_stderr or '管理者権限で開始したプロセス PID を取得できませんでした。'
                exit_code = self._wait_for_exit_code(exit_code_path, timeout_seconds=1.0)
                return 'failed', exit_code, '', message

            self._pid_store.register(
                task.id,
                PidEntry(
                    pid=target_pid,
                    started_at=_now_str(),
                    history_id=history_id,
                    task_name=task.name,
                ),
            )
            if self.on_status_change:
                self.on_status_change(task.id)

            start_time = time.monotonic()
            timed_out = False

            while pid_alive(target_pid):
                with self._kill_lock:
                    killed_requested = task.id in self._kill_requested
                if killed_requested:
                    kill_pid(target_pid)
                    break

                if task.timeout_seconds:
                    elapsed = time.monotonic() - start_time
                    if elapsed >= task.timeout_seconds:
                        timed_out = True
                        kill_pid(target_pid)
                        break

                time.sleep(1.0)

            self._pid_store.unregister(task.id)

            stdout_str = _read_output_file(stdout_path)
            stderr_str = _read_output_file(stderr_path)
            exit_code = self._wait_for_exit_code(exit_code_path, timeout_seconds=10.0)

            with self._kill_lock:
                killed_by_user = task.id in self._kill_requested
                self._kill_requested.discard(task.id)

            if timed_out:
                status = 'timeout'
            elif killed_by_user:
                status = 'killed'
            elif exit_code == 0:
                status = 'success'
            else:
                status = 'failed'

            return status, exit_code, stdout_str, stderr_str
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _build_elevated_wrapper_script(
        self,
        cmd: list[str],
        cwd: str | None,
        env_overrides: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
        exit_code_path: Path,
        pid_path: Path,
    ) -> str:
        arguments = subprocess.list2cmdline(cmd[1:]) if len(cmd) > 1 else ''
        process_line = f"$process = Start-Process -FilePath {_powershell_literal(cmd[0])} "
        if arguments:
            process_line += f"-ArgumentList {_powershell_literal(arguments)} "
        if cwd:
            process_line += f"-WorkingDirectory {_powershell_literal(cwd)} "
        process_line += (
            "-RedirectStandardOutput $stdoutPath "
            "-RedirectStandardError $stderrPath "
            "-PassThru"
        )

        lines = [
            "$ErrorActionPreference = 'Stop'",
            f"$stdoutPath = {_powershell_literal(str(stdout_path))}",
            f"$stderrPath = {_powershell_literal(str(stderr_path))}",
            f"$exitPath = {_powershell_literal(str(exit_code_path))}",
            f"$pidPath = {_powershell_literal(str(pid_path))}",
        ]
        for key, value in sorted(env_overrides.items()):
            lines.append(
                f"[System.Environment]::SetEnvironmentVariable({_powershell_literal(key)}, {_powershell_literal(value)}, 'Process')"
            )
        lines.extend([
            'try {',
            f'    {process_line}',
            '    Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding Ascii',
            '    Wait-Process -Id $process.Id',
            '    try { $process.Refresh() } catch {}',
            '    Set-Content -LiteralPath $exitPath -Value $process.ExitCode -Encoding Ascii',
            '    exit 0',
            '} catch {',
            '    ($_ | Out-String) | Out-File -LiteralPath $stderrPath -Append -Encoding utf8',
            '    Set-Content -LiteralPath $exitPath -Value 1 -Encoding Ascii',
            '    exit 1',
            '}',
        ])
        return '\n'.join(lines) + '\n'

    def _build_elevated_launcher_script(self, wrapper_path: Path) -> str:
        powershell_exe = shutil.which('powershell') or 'powershell'
        arguments = subprocess.list2cmdline([
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-NonInteractive',
            '-File', str(wrapper_path),
        ])
        lines = [
            "$ErrorActionPreference = 'Stop'",
            (
                f"$process = Start-Process -FilePath {_powershell_literal(powershell_exe)} "
                f"-ArgumentList {_powershell_literal(arguments)} -Verb RunAs -PassThru"
            ),
            '[Console]::Out.WriteLine($process.Id)',
            'exit 0',
        ]
        return '\n'.join(lines) + '\n'

    def _extract_first_pid(self, text: str) -> int | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.isdigit():
                return int(stripped)
        return None

    def _wait_for_target_pid(
        self,
        pid_path: Path,
        wrapper_pid: int,
        timeout_seconds: float,
    ) -> int | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                if pid_path.exists():
                    return int(pid_path.read_text(encoding='ascii').strip())
            except (OSError, ValueError):
                pass

            if not pid_alive(wrapper_pid):
                break

            time.sleep(0.2)

        return None

    def _wait_for_exit_code(self, exit_code_path: Path, timeout_seconds: float) -> int | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                if exit_code_path.exists():
                    return int(exit_code_path.read_text(encoding='ascii').strip())
            except (OSError, ValueError):
                pass
            time.sleep(0.2)

        return None

    def startup_cleanup(self) -> None:
        """起動時に前回セッションの孤立プロセスを処理する。
        - 死亡済みプロセス: 履歴を unknown_exit で閉じる
        - 生存プロセス: 監視スレッドを起動して完了時に履歴を更新する
        """
        dead = self._pid_store.cleanup_dead_pids()
        for task_id, entry in dead.items():
            self._close_orphan_history(task_id, entry, 'unknown_exit')

        for task_id, entry in self._pid_store.get_all().items():
            if pid_alive(entry.pid):
                t = threading.Thread(
                    target=self._monitor_orphan,
                    args=(task_id, entry),
                    name=f'OrphanMonitor-{task_id}',
                    daemon=True,
                )
                t.start()

    def _monitor_orphan(self, task_id: str, entry: PidEntry) -> None:
        """孤立プロセスの終了を監視し、履歴を更新する"""
        import psutil
        try:
            proc = psutil.Process(entry.pid)
            proc.wait()
        except Exception:
            pass

        with self._kill_lock:
            killed = task_id in self._kill_requested
            self._kill_requested.discard(task_id)

        self._pid_store.unregister(task_id)
        status = 'killed' if killed else 'unknown_exit'
        self._close_orphan_history(task_id, entry, status)

        if self.on_status_change:
            self.on_status_change(task_id)

    def _close_orphan_history(
        self, task_id: str, entry: PidEntry, status: str
    ) -> None:
        current = self._storage.get_history_entry(task_id, entry.history_id)
        if current is None or current.status != 'running':
            return
        current.status = status
        current.finished_at = _now_str()
        self._storage.update_history(current)
