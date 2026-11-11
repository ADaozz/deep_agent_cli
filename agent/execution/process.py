"""Command execution, cancellation, bounded output and execution logs."""

from __future__ import annotations

from codecs import IncrementalDecoder, getincrementaldecoder
from dataclasses import dataclass
from datetime import datetime
import logging
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid

from deepagents.backends.protocol import ExecuteResponse

from agent.runtime.cancellation import emit_tool_output, get_cancel_context
from agent.execution.pool import PersistentSandbox, SandboxLostError, SandboxPool
from agent.execution.policy import sandbox_workspace_root


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
    """收集一条命令合并后的 stdout/stderr：流式推送、截断并写日志。"""

    def __init__(self, *, tool_call_id: str, max_output_bytes: int, workspace_root: Path) -> None:
        self.tool_call_id = tool_call_id
        self.max_output_bytes = max_output_bytes
        self.workspace_root = workspace_root
        self.decoder: IncrementalDecoder = getincrementaldecoder("utf-8")(errors="replace")
        self.tail_bytes = bytearray()
        self.total_bytes = 0
        self.truncated = False
        self.log_fd: int | None = None
        self.log_directory_fd: int | None = None
        self.log_name: str | None = None
        self.log_error: str | None = None

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

    def append(self, raw: bytes) -> None:
        if not raw:
            return
        max_output_bytes = self.max_output_bytes
        remaining = max(0, max_output_bytes - self.total_bytes)
        accepted = raw[:remaining] if remaining > 0 else b""
        if accepted:
            text = self.decoder.decode(accepted)
            if text:
                emit_tool_output(self.tool_call_id, text, stream="merged")
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
        self.total_bytes += len(raw)
        self.tail_bytes.extend(raw)
        if len(self.tail_bytes) > max_output_bytes:
            del self.tail_bytes[:-max_output_bytes]
            _drop_partial_character_head(self.tail_bytes)
        if self.truncated:
            # 每个分块都用最新字节替换有界内存视图；完整流继续写入工作区日志。
            current_tail = _decode_tail_snapshot(bytes(self.tail_bytes))
            emit_tool_output(self.tool_call_id, current_tail, stream="tail_snapshot")

    def flush(self) -> None:
        """刷新解码器，处理末尾不完整的多字节序列。"""
        if not self.truncated:
            tail = self.decoder.decode(b"", final=True)
            if tail:
                emit_tool_output(self.tool_call_id, tail, stream="merged")

    def close(self) -> None:
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

    def response(
        self,
        *,
        exit_code: int,
        timeout: int | None,
        cancelled: bool = False,
        timed_out: bool = False,
        failure: str | None = None,
    ) -> LocalExecuteResponse:
        max_output_bytes = self.max_output_bytes
        truncated = self.truncated
        output = bytes(self.tail_bytes).decode("utf-8", errors="replace") if self.tail_bytes else ""
        host_log_path: str | None = None
        agent_log_path: str | None = None
        if truncated:
            detail = f"Output truncated: showing the last {max_output_bytes} bytes."
            if self.log_error is not None:
                detail += f"\nFull output could not be saved: {self.log_error}"
            elif self.log_name is not None:
                relative_log_path = Path(".deep-agent/logs/exec") / self.log_name
                host_log_path = str(self.workspace_root / relative_log_path)
                agent_log_path = f"{sandbox_workspace_root(self.workspace_root)}/{relative_log_path.as_posix()}"
                detail += f"\nFull output saved to: {host_log_path}"
                detail += f"\nAgent path: {agent_log_path}"
            output = f"{output}\n\n[{detail}]"

        result_metadata = {
            "host_log_path": host_log_path,
            "agent_log_path": agent_log_path,
            "log_error": self.log_error,
            "max_output_bytes": max_output_bytes,
        }

        if cancelled:
            return LocalExecuteResponse(
                f"{output.rstrip()}\n\nCancelled by user.".strip(), 130, truncated,
                termination_reason="cancelled", **result_metadata,
            )

        if timed_out:
            return LocalExecuteResponse(
                f"{output.rstrip()}\n\nError: Command timed out after {timeout} seconds.".strip(),
                124, truncated, termination_reason="timeout", **result_metadata,
            )

        if failure is not None:
            return LocalExecuteResponse(
                f"{output.rstrip()}\n\nError: {failure}".strip(), exit_code, truncated,
                termination_reason="sandbox_lost", **result_metadata,
            )

        if not output:
            output = "<no output>"
        if exit_code:
            output = f"{output.rstrip()}\n\nExit code: {exit_code}"
        return LocalExecuteResponse(output, exit_code, truncated, **result_metadata)


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
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            shell=shell,
            executable="/bin/sh" if shell else None,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
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
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if ctx is not None:
            ctx.clear_process()
        collector.close()

    return collector.response(
        exit_code=int(process.returncode or 0), timeout=timeout,
        cancelled=cancelled or (ctx is not None and ctx.cancelled), timed_out=timed_out,
    )


def _run_in_sandbox(
    pool: SandboxPool,
    sandbox: PersistentSandbox,
    command: str,
    *,
    timeout: int | None,
    max_output_bytes: int,
    workspace: Path,
) -> ExecuteResponse:
    """在持久沙箱里为一条命令启动新的 shell。

    取消和超时只杀掉该命令的进程组。Worker 若未及时确认 kill，视为已损坏，
    下一次 execute 会换新沙箱；本条命令不会重发。
    """
    ctx = get_cancel_context()
    tool_call_id = ctx.tool_call_id if ctx is not None else ""
    try:
        handle = sandbox.start_command(command)
    except SandboxLostError as exc:
        pool.discard(sandbox, str(exc))
        return LocalExecuteResponse(
            f"Error: sandbox is unavailable ({exc}). The command was not run.", 1, False,
            termination_reason="sandbox_lost",
        )
    if ctx is not None:
        ctx.register_process(handle)

    deadline = time.monotonic() + timeout if timeout is not None else None
    cancelled = False
    timed_out = False
    kill_sent_at: float | None = None
    exit_code = 0
    failure: str | None = None
    spawn_error: str | None = None
    collector = _OutputCollector(
        tool_call_id=tool_call_id, max_output_bytes=max_output_bytes, workspace_root=workspace,
    )
    try:
        while True:
            now = time.monotonic()
            if kill_sent_at is None:
                if ctx is not None and ctx.cancelled:
                    cancelled = True
                elif deadline is not None and now >= deadline:
                    timed_out = True
                if cancelled or timed_out:
                    handle.kill()
                    kill_sent_at = now
            elif now - kill_sent_at >= KILL_CONFIRM_SECONDS:
                pool.discard(sandbox, "sandbox worker did not confirm a command kill")
                break
            wait = 0.1
            if deadline is not None and kill_sent_at is None:
                wait = max(0.0, min(wait, deadline - now))
            event = handle.next_event(wait)
            if event is None:
                continue
            kind, payload = event
            if kind == "out":
                collector.append(payload)
            elif kind == "exit":
                exit_code = int(payload)
                break
            elif kind == "spawn_error":
                spawn_error = str(payload)
                break
            else:
                failure = (
                    f"Sandbox worker stopped while this command was running ({payload}). "
                    "The command was not retried; the next execute starts a fresh sandbox."
                )
                exit_code = 125
                pool.discard(sandbox, str(payload))
                break
        collector.flush()
    finally:
        if ctx is not None:
            ctx.clear_process()
        handle.release()
        collector.close()

    if spawn_error is not None and not (cancelled or timed_out):
        return LocalExecuteResponse(
            f"Error executing command ({spawn_error})", 1, False, termination_reason="spawn_error",
        )
    return collector.response(
        exit_code=exit_code, timeout=timeout,
        cancelled=cancelled or (ctx is not None and ctx.cancelled and not timed_out),
        timed_out=timed_out, failure=failure,
    )


def _kill_process_group_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
