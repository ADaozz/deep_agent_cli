"""Process cleanup for the footer's background git probe."""

import asyncio

import pytest

from agent.cli import gitinfo


class _HungProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.entered = asyncio.Event()
        self.killed = False
        self.waited = False

    async def communicate(self):
        self.entered.set()
        await asyncio.Event().wait()

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        self.returncode = -9
        return self.returncode


@pytest.mark.parametrize("cancel", [False, True])
def test_git_probe_reaps_hung_process(tmp_path, monkeypatch, cancel: bool) -> None:
    process = _HungProcess()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(gitinfo.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(gitinfo, "TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        task = asyncio.create_task(gitinfo.GitProbe(tmp_path).refresh())
        await process.entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == gitinfo.GitSummary()
        assert process.killed
        assert process.waited

    asyncio.run(scenario())
