"""Cancellation must reap Git children and close pipes before loop shutdown."""
import asyncio
import gc
import sys

import pytest

from agent.cli.gitinfo import GitProbe, GitSummary


@pytest.mark.parametrize("during_spawn", [False, True])
def test_cancelled_probe_reaps_child_and_closes_pipe(tmp_path, monkeypatch, during_spawn):
    original_spawn = asyncio.create_subprocess_exec
    processes = []
    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)

    async def scenario():
        spawned = asyncio.Event()
        communicating = asyncio.Event()
        release_spawn = asyncio.Event()

        async def slow_spawn(*_args, **_kwargs):
            process = await original_spawn(
                sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            processes.append(process)
            original_communicate = process.communicate

            async def tracked_communicate():
                communicating.set()
                return await original_communicate()

            process.communicate = tracked_communicate
            spawned.set()
            if during_spawn:
                await release_spawn.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
        probe = GitProbe(tmp_path)
        task = asyncio.create_task(probe.refresh())
        await asyncio.wait_for(spawned.wait(), timeout=5)
        if not during_spawn:
            await asyncio.wait_for(communicating.wait(), timeout=5)
        task.cancel()
        release_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].returncode is not None
        assert processes[0].stdout.at_eof()
        assert processes[0]._transport.is_closing()

    asyncio.run(scenario())
    processes.clear()
    gc.collect()
    assert unraisable == []


def test_cli_exit_waits_for_git_cleanup():
    from deepagents.backends import StateBackend
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from agent.cli.app import CliApplication
    from agent.runner import AgentRunner
    from tests.conftest import scripted_model

    async def scenario():
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        class SlowGit:
            async def refresh(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.01)
                    cleaned.set()
                return GitSummary()

        runner = AgentRunner(model=scripted_model([]), backend=StateBackend())
        try:
            with create_pipe_input() as pipe:
                app = CliApplication(runner, input=pipe, output=DummyOutput())
                app._git = SlowGit()
                task = asyncio.create_task(app.run_async())
                await asyncio.wait_for(entered.wait(), timeout=2)
                pipe.send_text('/quit\r')
                await asyncio.wait_for(task, timeout=2)
                assert cleaned.is_set()
                assert app._git_task is None
        finally:
            runner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('stderr,label', [
    (b'fatal: not a git repository (or any of the parent directories): .git', ''),
    (b'fatal: detected dubious ownership in repository', '⎇ git error'),
    (b'fatal: unable to read index', '⎇ git error'),
])
def test_probe_distinguishes_non_repository_from_git_failures(tmp_path, monkeypatch, stderr, label):
    class FailedProcess:
        returncode = 128
        async def communicate(self):
            return b'', stderr
    async def spawn(*_args, **kwargs):
        assert kwargs['env']['GIT_OPTIONAL_LOCKS'] == '0'
        assert kwargs['env']['LC_ALL'] == 'C'
        return FailedProcess()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    summary = asyncio.run(GitProbe(tmp_path).refresh())
    assert summary.label() == label


def test_probe_timeout_retains_branch_and_closes_real_child(tmp_path, monkeypatch):
    from agent.cli import gitinfo
    original_spawn = asyncio.create_subprocess_exec
    processes = []
    async def spawn(*_args, **kwargs):
        process = await original_spawn(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(gitinfo, 'TIMEOUT_SECONDS', 0.01)
    probe = GitProbe(tmp_path)
    probe.summary = GitSummary(branch='main', changed=3, available=True)
    summary = asyncio.run(probe.refresh())
    assert summary.label() == '⎇ main · git timeout'
    assert processes[0].returncode is not None
    assert processes[0].stdout.at_eof() and processes[0].stderr.at_eof()


def test_probe_missing_git_is_not_a_missing_repository(tmp_path, monkeypatch):
    async def spawn(*_args, **_kwargs):
        raise FileNotFoundError('git')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    assert asyncio.run(GitProbe(tmp_path).refresh()).label() == '⎇ git unavailable'
