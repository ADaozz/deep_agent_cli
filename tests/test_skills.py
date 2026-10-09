"""验证技能目录、输入块及原生技能中间件的联动。"""
import asyncio
import re
from pathlib import Path

import pytest
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.messages import AIMessage, HumanMessage
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.cli.app import CliApplication, SlashCompleter
from agent.cli.commands import command_table
from agent.cli.skills import SkillDraft, load_skill_catalog
from agent.cli.rendering import _capture, _message, render_transcript
from agent.cli.state import CliState, MessageBlock
from agent.factory import create_agent
from agent.runner import AgentRunner, RunEvent
from agent.session import messages_to_transcript
from agent.sandbox import WorkspaceCompositeBackend
from tests.conftest import scripted_model


async def select_skill(app):
    """定时驱动测试事件循环，避免未启动的终端界面缺少刷新唤醒。"""
    task = asyncio.create_task(app.select_skill())
    try:
        for _ in range(500):
            if task.done():
                return await task
            await asyncio.sleep(0.01)
        raise TimeoutError("技能选择未完成")
    finally:
        if not task.done():
            task.cancel()


def write_skill(root: Path, name: str, body: str = "技能正文") -> Path:
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {name} 的用途\n---\n{body}\n", encoding="utf-8")
    return path


def make_runner(root: Path, messages=None) -> AgentRunner:
    backend = FilesystemBackend(root_dir=root, virtual_mode=True)
    prepared = create_agent(
        model=scripted_model(messages or [AIMessage(content="done")]),
        backend=backend, skills=["/skills/"],
    )
    return AgentRunner(prepared=prepared)


def test_catalog_uses_native_metadata_and_reloads(tmp_path):
    write_skill(tmp_path, "alpha")
    bad = tmp_path / "skills" / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("缺少元数据", encoding="utf-8")
    runner = make_runner(tmp_path)
    first = load_skill_catalog(runner.prepared)["skills_metadata"]
    assert [(item["name"], item["path"]) for item in first] == [("alpha", "/skills/alpha/SKILL.md")]
    write_skill(tmp_path, "beta")
    assert {item["name"] for item in load_skill_catalog(runner.prepared)["skills_metadata"]} == {"alpha", "beta"}


def test_skill_draft_deletion_and_edited_marker():
    draft = SkillDraft()
    label = draft.display("alpha", "/skills/alpha/SKILL.md")
    assert draft.expand(label + " 检查项目") == "使用技能 alpha，先读取 /skills/alpha/SKILL.md 并遵循其说明。 检查项目"
    assert not draft.has_invalid_marker(label)
    assert draft.has_invalid_marker("[Skill: alph]")
    assert not draft.has_invalid_marker("检查项目")
    assert draft.expand("检查项目") == "检查项目"
    draft.clear()
    assert not draft.labels


@pytest.mark.parametrize("suffix", ["", " 检查项目"])
def test_picker_inserts_without_running_then_submits(tmp_path, monkeypatch, suffix):
    write_skill(tmp_path, "beta")
    write_skill(tmp_path, "alpha")
    runner = make_runner(tmp_path)
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        calls = []
        monkeypatch.setattr(app, "_start_run", lambda text, **kwargs: calls.append((text, kwargs)))
        asyncio.run(select_skill(app))
        assert app.interaction.kind == "skill"
        assert [item["label"] for item in app.interaction.fields[0]["options"]] == ["alpha", "beta"]
        app.interaction.move(1)
        app._submit_buffer("steer")
        assert app.interaction is None
        assert app.buffer.text == "[Skill: beta] "
        assert not calls
        app.buffer.text += suffix
        app._submit_buffer("steer")
        assert len(calls) == 1
        assert "/skills/beta/SKILL.md" in calls[0][0]
        assert "[Skill:" not in calls[0][0]
        assert calls[0][0].endswith(suffix.strip())
        assert app.buffer.text == ""
        assert not app._skill_draft.has_blocks
        assert app.buffer.history.get_strings()[-1] == calls[0][0]


def test_picker_cancel_preserves_skill_and_paste_draft(tmp_path):
    write_skill(tmp_path, "alpha")
    runner = make_runner(tmp_path)
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        label = app._skill_draft.display("alpha", "/skills/alpha/SKILL.md")
        paste = app._pasted_content.display("甲" * 600)
        original = f"{label} {paste}"
        app.buffer.text = original
        app.buffer.cursor_position = 3
        asyncio.run(select_skill(app))
        app._finish_interaction(cancelled=True)
        assert app.buffer.text == original
        assert app.buffer.cursor_position == 3
        assert app._skill_draft.is_present(original)
        assert app._pasted_content.expand(original).endswith("甲" * 600)


def test_reselection_preserves_paste_and_replaces_skill(tmp_path, monkeypatch):
    write_skill(tmp_path, "alpha")
    write_skill(tmp_path, "beta")
    runner = make_runner(tmp_path)
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        label = app._skill_draft.display("alpha", "/skills/alpha/SKILL.md")
        paste = app._pasted_content.display("甲" * 600)
        app.buffer.text = f"{label} {paste}"
        app.buffer.cursor_position = len(app.buffer.text)
        asyncio.run(select_skill(app))
        app.interaction.move(1)
        app._submit_buffer("steer")
        assert "[Skill: alpha]" not in app.buffer.text
        assert "[Skill: beta]" in app.buffer.text
        calls = []
        monkeypatch.setattr(app, "_start_run", lambda text, **kwargs: calls.append(text))
        app._submit_buffer("steer")
        assert "甲" * 600 in calls[0]
        assert "/skills/beta/SKILL.md" in calls[0]


def test_missing_skill_and_edited_marker_block_submission(tmp_path, monkeypatch):
    path = write_skill(tmp_path, "alpha")
    runner = make_runner(tmp_path)
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        calls = []
        monkeypatch.setattr(app, "_start_run", lambda text, **kwargs: calls.append(text))
        label = app._skill_draft.display("alpha", "/skills/alpha/SKILL.md")
        app.buffer.text = "[Skill: alph]"
        app._submit_buffer("steer")
        assert not calls
        app.buffer.text = label
        path.unlink()
        app._submit_buffer("steer")
        assert not calls
        assert app.buffer.text == label
        app.buffer.text = "普通输入"
        app._submit_buffer("steer")
        assert calls == ["普通输入"]


def test_empty_catalog_does_not_start_interaction(tmp_path):
    (tmp_path / "skills").mkdir()
    with create_pipe_input() as pipe:
        app = CliApplication(make_runner(tmp_path), input=pipe, output=DummyOutput())
        asyncio.run(select_skill(app))
        assert app.interaction is None
        assert any("No valid skills" in block.content for block in app.state.blocks)


def test_skill_command_completion():
    completer = SlashCompleter(command_table())
    assert any(item.text == "/skill" for item in completer.get_completions(Document("/ski"), None))


def test_native_middleware_and_read_file_work_together(tmp_path):
    write_skill(tmp_path, "alpha", "执行技能时输出特定内容")
    runner = make_runner(tmp_path, [
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "/skills/alpha/SKILL.md"}, "id": "skill-read"}]),
        AIMessage(content="done"),
    ])
    config = {"configurable": {"thread_id": "skills-integration"}}
    result = runner.prepared.graph.invoke(
        {"messages": [{"role": "user", "content": "使用技能 alpha，先读取 /skills/alpha/SKILL.md 并遵循其说明。"}]}, config,
    )
    snapshot = runner.prepared.graph.get_state(config)
    assert snapshot.values["skills_metadata"][0]["name"] == "alpha"
    assert any(message.type == "tool" and "执行技能时输出特定内容" in message.content for message in result["messages"])


def test_catalog_through_read_only_skills_route(tmp_path):
    write_skill(tmp_path, "alpha")
    executor = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    backend = WorkspaceCompositeBackend(executor, skills_dir=tmp_path / "skills")
    prepared = create_agent(model=scripted_model([AIMessage(content="done")]), backend=backend, skills=["/skills/"])
    assert load_skill_catalog(prepared)["skills_metadata"][0]["name"] == "alpha"
    assert "read-only" in backend.write("/skills/alpha/SKILL.md", "修改").error


def test_keyboard_enter_opens_picker_and_ctrl_c_clears_block(tmp_path):
    write_skill(tmp_path, "alpha")

    async def wait_until(predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise TimeoutError("终端交互未完成")

    async def scenario():
        with create_pipe_input() as pipe:
            app = CliApplication(make_runner(tmp_path), input=pipe, output=DummyOutput())
            task = asyncio.create_task(app.run_async())
            try:
                await asyncio.sleep(0.03)
                pipe.send_text("/skill")
                await wait_until(lambda: app.buffer.text == "/skill")
                pipe.send_text("\r")
                await wait_until(lambda: app.interaction is not None)
                assert app.interaction.kind == "skill"
                pipe.send_text("\r")
                await wait_until(lambda: "[Skill: alpha]" in app.buffer.text)
                assert not app.state.running
                pipe.send_text("\x03")
                await wait_until(lambda: app.buffer.text == "")
                assert not app._skill_draft.has_blocks
            finally:
                app.exit()
                await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())


def test_skill_keeps_image_attachments_and_queues_expanded_text(tmp_path, monkeypatch):
    write_skill(tmp_path, "alpha")
    runner = make_runner(tmp_path)
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        calls = []
        image = object()
        app.state.attachments.append(image)
        app.buffer.text = app._skill_draft.display("alpha", "/skills/alpha/SKILL.md")
        monkeypatch.setattr(app, "_start_run", lambda text, **kwargs: calls.append(kwargs))
        app._submit_buffer("steer")
        assert calls[0]["image_refs"] == (image,)
        app.state.attachments.clear()
        app.state.running = True
        app.buffer.text = app._skill_draft.display("alpha", "/skills/alpha/SKILL.md")
        queued = []
        monkeypatch.setattr(runner, "follow_up", queued.append)
        app._submit_buffer("followUp")
        assert "/skills/alpha/SKILL.md" in queued[0]
        assert "[Skill:" not in queued[0]


def test_session_change_clears_marker_without_losing_text(tmp_path):
    write_skill(tmp_path, "alpha")
    with create_pipe_input() as pipe:
        app = CliApplication(make_runner(tmp_path), input=pipe, output=DummyOutput())
        app.buffer.text = app._skill_draft.display("alpha", "/skills/alpha/SKILL.md") + " 检查项目"
        app.new_session()
        assert not app._skill_draft.has_blocks
        assert "[Skill:" not in app.buffer.text
        assert app.buffer.text.endswith("检查项目")


@pytest.mark.parametrize("pending", [False, True])
def test_sent_skill_stays_a_colored_block_with_markdown_instructions(pending):
    """发送和排队时保持技能块显示，传给模型的指令与用户正文不变。"""
    draft = SkillDraft()
    label = draft.display("playwright-cli", "/skills/playwright-cli/SKILL.md")
    payload = draft.expand(label + " **检查登录**\n\n- 记录截图\n- 验证按钮")
    block = MessageBlock(kind="user", content=payload, pending=pending)
    rendered = _capture(_message(block, True)[0], 100)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    assert "[Skill: playwright-cli]" in plain
    assert "使用技能" not in plain and "/skills/" not in plain
    assert "检查登录" in plain and "**检查登录**" not in plain
    assert "• 记录截图" in plain and "• 验证按钮" in plain
    assert "48;2;37;75;112" in rendered
    assert block.content == payload


def test_restored_skill_message_and_steering_keep_block_display():
    """历史会话与追加指令使用同一显示规则，无需改写保存的模型消息。"""
    draft = SkillDraft()
    payload = draft.expand(draft.display("alpha", "/skills/alpha/SKILL.md") + " 检查项目")
    message = HumanMessage(content=payload)
    state = CliState()
    state.load_transcript(messages_to_transcript([message]))
    rendered = render_transcript(state, 100)
    assert "[Skill: alpha]" in rendered and "使用技能" not in rendered
    assert message.content == payload
    state = CliState()
    state.apply(RunEvent(type="steering_queued", content=payload, result={"mode": "steer", "id": "skill-steer"}))
    assert "[Skill: alpha]" in render_transcript(state, 100)
    state.apply(RunEvent(type="steering_applied", content=payload, result={"id": "skill-steer"}))
    assert "[Skill: alpha]" in render_transcript(state, 100)
    assert not state.blocks[0].pending


def test_skill_block_style_survives_wrapping_and_ordinary_text_is_unchanged():
    """技能标签自动折行后保留样式，普通正文和代码不受影响。"""
    draft = SkillDraft()
    payload = draft.expand(draft.display("playwright-cli", "/skills/playwright-cli/SKILL.md"))
    rendered = _capture(_message(MessageBlock(kind="user", content=payload), True)[0], 20)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
    assert "[Skill:playwright-cli]" in "".join(plain.split())
    assert rendered.count("48;2;37;75;112") >= 2
    ordinary = _capture(_message(MessageBlock(kind="user", content="普通文字和 `代码`"), True)[0], 100)
    assert "48;2;37;75;112" not in ordinary
