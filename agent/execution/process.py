"""Command execution, cancellation, bounded output and execution logs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from codecs import IncrementalDecoder, getincrementaldecoder
from dataclasses import dataclass
from datetime import datetime
import logging
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
from typing import Any
import uuid

from deepagents.backends.protocol import ExecuteResponse

from agent.redaction import SecretRef, StreamingSecretRedactor

from agent.runtime.cancellation import (
    ToolOutputEmitter,
    emit_output_with,
    emit_tool_output,
    get_cancel_context,
    get_output_emitter,
)
from agent.execution.pool import PersistentSandbox, SandboxLostError, SandboxPool
from agent.execution.policy import sandbox_workspace_root
from agent.execution.service_observation import OWNER_VARIABLE, begin_command, end_command


LOG = logging.getLogger(__name__)
KILL_CONFIRM_SECONDS = 5.0

@dataclass
class LocalExecuteResponse(ExecuteResponse):
    """execute 结果，附带 UI ToolMessage artifact 所需的本地元数据。"""

    host_log_path: str | None = None
    agent_log_path: str | None = None
    log_error: str | None = None
    termination_reason: str | None = None
    max_output_bytes: int | None = None
    execution_id: str | None = None
    running: bool = False


def _effective_timeout(requested: int | None, default: int | None) -> int | None:
    """An explicit per-command timeout overrides the sandbox default."""
    if requested is not None and requested <= 0:
        raise ValueError(f"timeout must be positive, got {requested}")
    return requested if requested is not None else default


def _create_exec_log(workspace: Path) -> tuple[int, int, str]:
    """在工作区下创建私有日志，不跟随目录符号链接。"""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(workspace, directory_flags)
    try:
        for part in (".deep-agent", "logs", "exec"):
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}.log"
        file_fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory_fd,
        )
        return file_fd, directory_fd, name
    except Exception:
        os.close(directory_fd)
        raise


def _write_all(fd: int, data: bytes | bytearray) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("could not write command output log")
        view = view[written:]


def _drop_partial_character_head(data: bytearray) -> None:
    """丢掉窗口裁剪后留在头部的 UTF-8 续字节。

    ``del data[:-max]`` 可能把多字节字符拦腰切断；头部残留的续字节会在每次
    快照和最终尾巴里都解码成 U+FFFD。
    """
    head = 0
    while head < len(data) and (data[head] & 0xC0) == 0x80:
        head += 1
    del data[:head]


def _decode_tail_snapshot(data: bytes) -> str:
    """解码滚动尾巴，避免把被截断的字符闪成乱码。

    非最终的增量解码会把末尾不完整的多字节字符留在缓冲里，而不是立刻替换成
    U+FFFD；下一块数据补全后，下一次快照就会显示完整字符。
    """
    return getincrementaldecoder("utf-8")(errors="replace").decode(data)


class _OutputCollector:
    """收集一条命令合并后的 stdout/stderr：流式推送、截断并写日志。

    超过 ``max_output_bytes`` 后仍把新增输出写入有界 ``_pending``，供轮询读取。
    append / take_pending / snapshot 共用同一把锁。
    """

    def __init__(
        self,
        *,
        tool_call_id: str,
        max_output_bytes: int,
        workspace_root: Path,
        emit: ToolOutputEmitter | None = None,
        execution_id: str = "",
        thread_id: str = "",
        redaction_provider: Callable[[], Iterable[SecretRef]] | None = None,
    ) -> None:
        self.tool_call_id = tool_call_id
        self.max_output_bytes = max_output_bytes
        self.workspace_root = workspace_root
        self._emit = emit
        self.execution_id = execution_id
        self.thread_id = thread_id
        self._redactor = StreamingSecretRedactor(redaction_provider) if redaction_provider is not None else None
        self.decoder: IncrementalDecoder = getincrementaldecoder("utf-8")(errors="replace")
        self.tail_bytes = bytearray()
        self.total_bytes = 0
        self.truncated = False
        self._pending_bytes = bytearray()
        self._lock = threading.RLock()
        self.log_fd: int | None = None
        self.log_directory_fd: int | None = None
        self.log_name: str | None = None
        self.log_error: str | None = None

    def _emit_output(self, content: str, *, stream: str) -> None:
        if not content:
            return
        if self._emit is not None:
            emit_output_with(
                self._emit,
                self.tool_call_id,
                content,
                stream=stream,
                name="execute",
                thread_id=self.thread_id,
                execution_id=self.execution_id,
            )
        else:
            emit_tool_output(
                self.tool_call_id,
                content,
                stream=stream,
                name="execute",
                thread_id=self.thread_id,
                execution_id=self.execution_id,
            )

    def _discard_log(self, exc: OSError) -> None:
        self.log_error = f"{type(exc).__name__}: {exc}"
        if self.log_fd is not None:
            try:
                os.close(self.log_fd)
            except OSError:
                pass
            self.log_fd = None
        if self.log_directory_fd is not None and self.log_name is not None:
            try:
                os.unlink(self.log_name, dir_fd=self.log_directory_fd)
            except OSError:
                pass

    def _append_pending_text(self, text: str) -> None:
        if not text:
            return
        max_output_bytes = self.max_output_bytes
        if max_output_bytes <= 0:
            self._pending_bytes.clear()
            return
        self._pending_bytes.extend(text.encode("utf-8"))
        if len(self._pending_bytes) > max_output_bytes:
            del self._pending_bytes[:-max_output_bytes]
            _drop_partial_character_head(self._pending_bytes)

    def _ingest_safe_text_locked(self, safe_text: str) -> None:
        """Write already-redacted text to log, rolling tail, unread window, and UI stream."""
        if not safe_text:
            return
        safe_bytes = safe_text.encode("utf-8")
        max_output_bytes = self.max_output_bytes
        remaining = max(0, max_output_bytes - self.total_bytes)
        if not self.truncated and self.total_bytes + len(safe_bytes) > max_output_bytes:
            self.truncated = True
            if self.log_fd is None and self.log_error is None:
                try:
                    self.log_fd, self.log_directory_fd, self.log_name = _create_exec_log(self.workspace_root)
                    _write_all(self.log_fd, self.tail_bytes)
                    _write_all(self.log_fd, safe_bytes)
                except OSError as exc:
                    self._discard_log(exc)
        elif self.log_fd is not None:
            try:
                _write_all(self.log_fd, safe_bytes)
            except OSError as exc:
                self._discard_log(exc)
        self._append_pending_text(safe_text)
        self.total_bytes += len(safe_bytes)
        if max_output_bytes <= 0:
            self.tail_bytes.clear()
        else:
            self.tail_bytes.extend(safe_bytes)
            if len(self.tail_bytes) > max_output_bytes:
                del self.tail_bytes[:-max_output_bytes]
                _drop_partial_character_head(self.tail_bytes)
        if remaining > 0:
            accepted_bytes = safe_bytes[:remaining]
            accepted_text = _decode_tail_snapshot(accepted_bytes)
            if accepted_text:
                self._emit_output(accepted_text, stream="merged")
        if self.truncated:
            current_tail = _decode_tail_snapshot(bytes(self.tail_bytes))
            self._emit_output(current_tail, stream="tail_snapshot")

    def append(self, raw: bytes) -> None:
        if not raw:
            return
        with self._lock:
            if self._redactor is not None:
                decoded = self.decoder.decode(raw)
                if decoded:
                    safe_text = self._redactor.feed(decoded)
                    if safe_text:
                        self._ingest_safe_text_locked(safe_text)
                return
            max_output_bytes = self.max_output_bytes
            remaining = max(0, max_output_bytes - self.total_bytes)
            accepted = raw[:remaining] if remaining > 0 else b""
            overflow = raw[remaining:] if remaining < len(raw) else b""
            if not self.truncated and self.total_bytes + len(raw) > max_output_bytes:
                self.truncated = True
                if self.log_fd is None and self.log_error is None:
                    try:
                        self.log_fd, self.log_directory_fd, self.log_name = _create_exec_log(self.workspace_root)
                        _write_all(self.log_fd, self.tail_bytes)
                        _write_all(self.log_fd, raw)
                    except OSError as exc:
                        self._discard_log(exc)
            elif self.log_fd is not None:
                try:
                    _write_all(self.log_fd, raw)
                except OSError as exc:
                    self._discard_log(exc)
            # Always decode new bytes into the unread UTF-8 byte window, even after truncation.
            # Emit only the pre-truncation prefix on "merged" before switching to snapshots.
            accepted_text = self.decoder.decode(accepted) if accepted else ""
            if accepted_text:
                self._append_pending_text(accepted_text)
            overflow_text = self.decoder.decode(overflow) if overflow else ""
            if overflow_text:
                self._append_pending_text(overflow_text)
            self.total_bytes += len(raw)
            if max_output_bytes <= 0:
                self.tail_bytes.clear()
            else:
                self.tail_bytes.extend(raw)
                if len(self.tail_bytes) > max_output_bytes:
                    del self.tail_bytes[:-max_output_bytes]
                    _drop_partial_character_head(self.tail_bytes)
            if accepted_text:
                self._emit_output(accepted_text, stream="merged")
            if self.truncated:
                current_tail = _decode_tail_snapshot(bytes(self.tail_bytes))
                self._emit_output(current_tail, stream="tail_snapshot")

    def flush(self) -> None:
        """刷新解码器与流式脱敏尾部缓冲，处理末尾不完整的多字节序列。"""
        with self._lock:
            tail = self.decoder.decode(b"", final=True)
            if self._redactor is not None:
                safe_text = (self._redactor.feed(tail) if tail else "") + self._redactor.flush()
                if safe_text:
                    self._ingest_safe_text_locked(safe_text)
                return
            if not tail:
                return
            self._append_pending_text(tail)
            if not self.truncated:
                self._emit_output(tail, stream="merged")

    def take_pending(self) -> str:
        """Return and clear decoded text produced since the previous take (UTF-8 byte bounded)."""
        with self._lock:
            if not self._pending_bytes:
                return ""
            text = bytes(self._pending_bytes).decode("utf-8", errors="replace")
            self._pending_bytes.clear()
            return text

    def tail_text(self) -> str:
        """Return the bounded rolling tail decoded as UTF-8."""
        with self._lock:
            return bytes(self.tail_bytes).decode("utf-8", errors="replace") if self.tail_bytes else ""

    def log_metadata(self) -> dict[str, Any]:
        """Return unified truncation and log-path metadata without judging exit status."""
        with self._lock:
            truncated = self.truncated
            log_error = self.log_error
            log_name = self.log_name
            max_output_bytes = self.max_output_bytes
        host_log_path: str | None = None
        agent_log_path: str | None = None
        if truncated and log_error is None and log_name is not None:
            relative_log_path = Path(".deep-agent/logs/exec") / log_name
            host_log_path = str(self.workspace_root / relative_log_path)
            agent_log_path = f"{sandbox_workspace_root(self.workspace_root)}/{relative_log_path.as_posix()}"
        return {
            "truncated": truncated,
            "host_log_path": host_log_path,
            "agent_log_path": agent_log_path,
            "log_error": log_error,
            "max_output_bytes": max_output_bytes,
        }

    def truncation_detail(self) -> str | None:
        """Return the bounded truncation footer text when output was truncated."""
        meta = self.log_metadata()
        if not meta["truncated"]:
            return None
        detail = f"Output truncated: showing the last {meta['max_output_bytes']} bytes."
        if meta["log_error"] is not None:
            detail += f"\nFull output could not be saved: {meta['log_error']}"
        elif meta["host_log_path"] is not None and meta["agent_log_path"] is not None:
            detail += f"\nFull output saved to: {meta['host_log_path']}"
            detail += f"\nAgent path: {meta['agent_log_path']}"
        return detail

    def close(self) -> None:
        with self._lock:
            if self.log_fd is not None:
                try:
                    os.fsync(self.log_fd)
                    os.close(self.log_fd)
                    self.log_fd = None
                except OSError as exc:
                    self._discard_log(exc)
            if self.log_directory_fd is not None:
                os.close(self.log_directory_fd)
                self.log_directory_fd = None


def format_terminal_output(
    body: str,
    *,
    exit_code: int,
    termination_reason: str | None,
    timeout: int | None = None,
    failure: str | None = None,
    spawn_error: str | None = None,
    unread_only: bool = False,
    truncation_detail: str | None = None,
) -> str:
    """Format terminal output text from an already-decided exit_code and termination_reason."""
    if termination_reason == "spawn_error":
        return f"Error executing command ({spawn_error})"
    if unread_only:
        output = body.rstrip() if body.strip() else "<no new output>"
    else:
        output = body
        if truncation_detail is not None:
            output = f"{output}\n\n[{truncation_detail}]"
    if termination_reason == "cancelled":
        return f"{output.rstrip()}\n\nCancelled by user.".strip()
    if termination_reason == "timeout":
        return f"{output.rstrip()}\n\nError: Command timed out after {timeout} seconds.".strip()
    if termination_reason == "sandbox_lost":
        return f"{output.rstrip()}\n\nError: {failure}".strip()
    if not unread_only and not output:
        output = "<no output>"
    if exit_code != 0:
        output = f"{output.rstrip()}\n\nExit code: {exit_code}"
    return output


def _run_process(
    command: list[str] | str,
    *,
    timeout: int | None,
    max_output_bytes: int,
    workspace: Path | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> ExecuteResponse:
    ctx = get_cancel_context()
    tool_call_id = ctx.tool_call_id if ctx is not None else ""
    owner = begin_command()
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            shell=shell,
            executable="/bin/sh" if shell else None,
            cwd=str(cwd) if cwd else None,
            env={**(os.environ if env is None else env), OWNER_VARIABLE: owner},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        end_command(owner)
        return LocalExecuteResponse(
            f"Error executing command ({type(exc).__name__}): {exc}", 1, False,
            termination_reason="spawn_error",
        )

    if ctx is not None:
        ctx.register_process(process)

    deadline = time.monotonic() + timeout if timeout is not None else None
    cancelled = False
    timed_out = False
    collector = _OutputCollector(
        tool_call_id=tool_call_id, max_output_bytes=max_output_bytes,
        workspace_root=(workspace or cwd or Path.cwd()).resolve(),
    )
    post_exit_idle_since: float | None = None

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    try:
        while True:
            if ctx is not None and ctx.cancelled:
                cancelled = True
                _kill_process_group_pid(process.pid)
            elif deadline is not None and not cancelled and time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group_pid(process.pid)
            now = time.monotonic()
            stopping = cancelled or timed_out or process.poll() is not None
            if stopping:
                if not selector.get_map():
                    break
                if post_exit_idle_since is None:
                    post_exit_idle_since = now
                if now - post_exit_idle_since >= 0.1:
                    break
            wait = 0.1
            if deadline is not None and not cancelled and not timed_out:
                wait = max(0.0, min(0.1, deadline - now))
            if post_exit_idle_since is not None:
                wait = min(wait, max(0.0, 0.1 - (now - post_exit_idle_since)))
            if selector.get_map():
                events = selector.select(timeout=wait)
            else:
                time.sleep(wait)
                events = []
            if not events:
                if stopping and not selector.get_map():
                    break
                continue
            for key, _mask in events:
                try:
                    chunk = key.fileobj.read1(4096)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    chunk = b""
                if chunk:
                    collector.append(chunk)
                    if stopping or process.poll() is not None:
                        post_exit_idle_since = time.monotonic()
                else:
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:  # noqa: BLE001
                        pass
            if (cancelled or timed_out or process.poll() is not None) and not selector.get_map():
                break
        collector.flush()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _kill_process_group_pid(process.pid)
            process.wait(timeout=1)
    finally:
        end_command(owner)
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if ctx is not None:
            ctx.clear_process()
        collector.close()

    is_cancelled = cancelled or (ctx is not None and ctx.cancelled)
    if is_cancelled:
        exit_code, reason = 130, "cancelled"
    elif timed_out:
        exit_code, reason = 124, "timeout"
    else:
        exit_code, reason = int(process.returncode or 0), None
    meta = collector.log_metadata()
    output = format_terminal_output(
        collector.tail_text(),
        exit_code=exit_code,
        termination_reason=reason,
        timeout=timeout,
        unread_only=False,
        truncation_detail=collector.truncation_detail(),
    )
    return LocalExecuteResponse(
        output, exit_code, meta["truncated"],
        host_log_path=meta["host_log_path"],
        agent_log_path=meta["agent_log_path"],
        log_error=meta["log_error"],
        termination_reason=reason,
        max_output_bytes=meta["max_output_bytes"],
    )


def _run_in_sandbox(
    pool: SandboxPool,
    sandbox: PersistentSandbox,
    command: str,
    *,
    timeout: int | None,
    max_output_bytes: int,
    workspace: Path,
    yield_time: float | None = None,
    stdin_mode: str = "none",
    network: bool = False,
    sandbox_mode: str = "restricted",
    sandbox_env_provider: Callable[[str], dict[str, str]] | None = None,
    redaction_provider: Callable[[], Iterable[SecretRef]] | None = None,
) -> ExecuteResponse:
    """在持久沙箱里为一条命令启动新的 shell。

    取消和超时只杀掉该命令的进程组。Worker 若未及时确认 kill，视为已损坏，
    下一次 execute 会换新沙箱；本条命令不会重发。

    ``yield_time`` 为秒：到期后返回 ``execution_id`` 与当前输出，进程继续运行。
    同步与 yield 都只等待 ``CommandSession`` 状态；事件由后台 pump 独占消费。
    """
    from agent.execution.lifecycle import CommandSession
    from agent.execution.ownership import execution_thread_id, root_session_id

    if yield_time is not None and yield_time < 0:
        raise ValueError(f"yield_time must be non-negative, got {yield_time}")
    mode = stdin_mode if stdin_mode in {"none", "pipe", "pty"} else "none"
    thread_id = execution_thread_id()
    session_root = root_session_id()
    extra_env = (
        sandbox_env_provider(session_root)
        if sandbox_env_provider is not None and session_root
        else {}
    )
    # Linux resets PR_SET_DUMPABLE across execve('/bin/sh'), so concurrent processes
    # inside the same shared Worker PID namespace share /proc/<pid>/environ visibility.
    # Refuse cross-root-session sharing whenever sandbox credentials are involved.
    if extra_env and pool.executions.has_active_other_root_session(sandbox.id, session_root):
        return LocalExecuteResponse(
            "Error: cannot inject sandbox credentials while another session has an active "
            "process in the shared sandbox (same PID namespace /proc boundary).",
            1,
            False,
            termination_reason="spawn_error",
        )
    if pool.executions.has_active_other_root_session(sandbox.id, session_root, require_secrets=True):
        return LocalExecuteResponse(
            "Error: cannot start a command in the shared sandbox while another session's "
            "credential-bearing process is still running (same PID namespace /proc boundary).",
            1,
            False,
            termination_reason="spawn_error",
        )
    ctx = get_cancel_context()
    tool_call_id = ctx.tool_call_id if ctx is not None else ""
    emit = get_output_emitter()
    try:
        handle = sandbox.start_command(command, stdin_mode=mode, env=extra_env or None)
    except SandboxLostError as exc:
        pool.discard(sandbox, str(exc))
        return LocalExecuteResponse(
            f"Error: sandbox is unavailable ({exc}). The command was not run.", 125, False,
            termination_reason="sandbox_lost",
        )

    collector = _OutputCollector(
        tool_call_id=tool_call_id,
        max_output_bytes=max_output_bytes,
        workspace_root=workspace,
        emit=emit,
        execution_id=handle.execution_id,
        thread_id=thread_id,
        redaction_provider=redaction_provider,
    )
    session = CommandSession(
        handle=handle,
        pool=pool,
        collector=collector,
        thread_id=thread_id,
        timeout=timeout,
        deadline=time.monotonic() + timeout if timeout is not None else None,
        network=network,
        tool_call_id=tool_call_id,
        sandbox_mode=sandbox_mode,
        root_session_id=session_root,
        has_injected_secrets=bool(extra_env),
        emit=emit,
        cancel_context=ctx,
    )
    pool.executions.register(session)
    yield_deadline = time.monotonic() + yield_time if yield_time is not None else None
    try:
        response = session.wait_until(yield_deadline)
        if not response.running:
            session.mark_result_consumed()
        return response
    finally:
        session.detach_cancel_context()


def _kill_process_group_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
