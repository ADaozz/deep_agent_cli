import json
import pytest

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from deepagents.backends import StateBackend
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt

from agent.factory import create_agent
from agent.config import SandboxConfig, Settings
from agent.sandbox import UnsandboxedShellBackend
from agent.tools.examples import build_example_tools
from agent.permission import ASK_INTERRUPT_ON
from agent.runner import (
    AgentRunner,
    InterruptKind,
    UnknownInterruptError,
    _tool_decisions,
    classify_interrupt,
    is_valid_hitl_interrupt,
)
from tests.conftest import graph_tool_names, scripted_model


def test_factory_exposes_local_tools_and_marks_confirm(fake_done) -> None:
    prepared = create_agent(model=fake_done, backend=StateBackend(), skills=[])
    assert prepared.exposed_tool_names == []
    assert prepared.interrupt_on == ASK_INTERRUPT_ON
    assert "Coding Agent CLI" in prepared.system_prompt
    names = graph_tool_names(prepared.graph)
    assert "handoff_to_human" not in names
    assert "request_human_input" in names
    assert "lookup_docs" not in names
    assert "send_email" not in names
    assert "write_todos" in names
    assert "compact_conversation" in names
    assert "execute" not in names
    assert len(names) == 10


def test_manual_compaction_rejects_early_usage_without_changing_checkpoint() -> None:
    model = scripted_model([AIMessage(
        content="done",
        usage_metadata={"input_tokens": 1000, "output_tokens": 10, "total_tokens": 1010},
        response_metadata={"model_provider": "openai"},
    )])
    model.profile = {"max_input_tokens": 128_000}
    runner = AgentRunner(
        model=model, backend=StateBackend(),
        settings=Settings.from_mapping({"llm": {"context_window": "128k"}}),
    )
    assert runner.invoke("hello").status == "completed"
    before = runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
    result = runner.compact_context()
    assert result.status == "ineligible"
    assert result.percent == pytest.approx(1010 / 128000 * 100)
    after = runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
    assert after == before


def test_manual_compaction_archives_history_and_leaves_graph_ready() -> None:
    model = scripted_model([
        AIMessage(
            content="done",
            usage_metadata={"input_tokens": 60_000, "output_tokens": 10, "total_tokens": 60_010},
            response_metadata={"model_provider": "openai"},
        ),
        AIMessage(content="short summary"),
        AIMessage(content="next answer"),
    ])
    model.profile = {"max_input_tokens": 128_000}
    runner = AgentRunner(
        model=model, backend=StateBackend(),
        settings=Settings.from_mapping({"llm": {"context_window": "128k"}}),
    )
    assert runner.invoke("long " * 15_000).status == "completed"
    result = runner.compact_context()
    assert result.status == "compacted"
    state = runner.prepared.graph.get_state(runner._thread_config())
    assert state.next == ()
    event = state.values["_summarization_event"]
    assert event["summary_message"].content.endswith("short summary\n</summary>")
    assert event["file_path"] in state.values["files"]
    assert runner.latest_usage() == {}
    assert runner.invoke("next").output == "next answer"


def test_stale_approval_target_cannot_approve_other_calls() -> None:
    pending = [{"toolCallId": "a"}, {"toolCallId": "b"}]
    with pytest.raises(ValueError, match="no longer pending"):
        _tool_decisions(pending, decision_type="approve", tool_call_ids=["stale"])
    assert _tool_decisions(pending, decision_type="approve", tool_call_ids=["a"]) == {
        "decisions": [
            {"type": "approve"},
            {"type": "reject", "message": "另一个并发的待确认调用未包含在本次人工决策中，按拒绝处理"},
        ]
    }
    assert _tool_decisions(
        pending, decision_type="approve", tool_call_ids=["a", "b"], others="same",
    ) == {"decisions": [{"type": "approve"}, {"type": "approve"}]}


def test_allow_tool_runs_locally() -> None:
    prepared = create_agent(model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-1", "name": "lookup_docs", "args": {"query": "middleware"},
            }]),
            AIMessage(content="中间件栈已说明。"),
        ]), backend=StateBackend(), extra_tools=build_example_tools())
    runner = AgentRunner(
        prepared=prepared,
        thread_id="allow",
    )
    result = runner.invoke("查 middleware")
    assert result.status == "completed"
    assert result.output == "中间件栈已说明。"


def test_confirm_tool_interrupts_then_resumes() -> None:
    prepared = create_agent(model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-write",
                "name": "write_file",
                "args": {"file_path": "/workspace/note.txt", "content": "hello"},
            }]),
            AIMessage(content="写入完成。"),
        ]), backend=StateBackend())
    runner = AgentRunner(
        prepared=prepared,
        thread_id="confirm",
    )
    waiting = runner.invoke("写入 note.txt")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "write_file"
    assert waiting.pending_tool_calls[0]["toolCallId"] == "call-write"
    resumed = runner.approve_tool("call-write")
    assert resumed.status == "completed"
    assert resumed.output == "写入完成。"


def test_request_human_input_interrupts_then_resumes() -> None:
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "input-1",
                "name": "request_human_input",
                "args": {"reason": "缺业务决策", "question": "用方案 A 还是 B？"},
            }]),
            AIMessage(content="将按方案 A 继续。"),
        ]),
        backend=StateBackend(),
        thread_id="human",
    )
    waiting = runner.invoke("请实现")
    assert waiting.status == "waiting_human"
    assert waiting.human_input["question"] == "用方案 A 还是 B？"
    resumed = runner.submit_human_input({"text": "方案 A"})
    assert resumed.status == "completed"
    assert resumed.output == "将按方案 A 继续。"


def test_human_input_rejects_wrong_interrupt_entry() -> None:
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "input-reject",
                "name": "request_human_input",
                "args": {"reason": "缺业务决策", "question": "用方案 A 还是 B？"},
            }]),
            AIMessage(content="不应继续。"),
        ]),
        backend=StateBackend(),
        thread_id="human-reject",
    )
    waiting = runner.invoke("请实现")
    assert waiting.status == "waiting_human"
    with pytest.raises(ValueError, match="continue_run requires a paused interrupt"):
        runner.continue_run()
    assert runner.current_interrupt().kind is InterruptKind.WAITING_HUMAN
    with pytest.raises(ValueError, match="approve_tool requires a waiting_confirmation interrupt"):
        runner.approve_tool("input-reject")
    assert runner.current_interrupt().kind is InterruptKind.WAITING_HUMAN
    resumed = runner.submit_human_input({"text": "方案 A"})
    assert resumed.status == "completed"
    assert resumed.output == "不应继续。"


def test_removed_handoff_session_has_clear_resume_error() -> None:
    def handoff_to_human(reason: str, question: str) -> str:
        return str(interrupt({"type": "human_input", "reason": reason, "question": question}))

    saver = InMemorySaver()
    old_prepared = create_agent(
        model=scripted_model([AIMessage(content="", tool_calls=[{
            "id": "old-handoff", "name": "handoff_to_human",
            "args": {"reason": "缺信息", "question": "选哪个？"},
        }])]),
        backend=StateBackend(), skills=[], checkpointer=saver,
        extra_tools=[StructuredTool.from_function(
            func=handoff_to_human, name="handoff_to_human", description="Old human input tool",
        )],
    )
    assert AgentRunner(prepared=old_prepared, thread_id="old-human").invoke("问用户").status == "waiting_human"

    current = AgentRunner(
        model=scripted_model([AIMessage(content="done")]), backend=StateBackend(),
        checkpointer=saver, thread_id="old-human",
    )
    with pytest.raises(RuntimeError, match="removed handoff_to_human"):
        current.submit_human_input({"text": "A"})


def test_execute_tool_event_uses_artifact_status() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    events = []
    runner._emit_update_events({"tools": {"messages": [ToolMessage(
        content="Exit code: 7\nCancelled by user.", name="execute", tool_call_id="real-success",
        artifact={"exit_code": 0, "truncated": False, "termination_reason": None},
    )]}}, events.append)
    assert len(events) == 1
    assert events[0].result["exit_code"] == 0
    assert events[0].is_error is False


def test_ai_message_usage_reaches_runner_event() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    events = []
    runner._emit_update_events({"model": {"messages": [AIMessage(
        content="done",
        usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
    )]}}, events.append)
    assert [event.result for event in events if event.type == "usage"] == [
        {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
    ]


def test_ai_message_without_usage_emits_no_usage_event() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    events = []
    runner._emit_update_events({"model": {"messages": [AIMessage(content="done")]}}, events.append)
    assert not [event for event in events if event.type == "usage"]


def test_latest_usage_reads_the_most_recent_ai_message() -> None:
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="first", usage_metadata={
                "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
            }),
            AIMessage(content="second", usage_metadata={
                "input_tokens": 40, "output_tokens": 5, "total_tokens": 45,
            }),
        ]),
        backend=StateBackend(),
    )
    assert runner.latest_usage() == {}
    assert runner.invoke("hello").status == "completed"
    assert runner.latest_usage()["total_tokens"] == 12
    assert runner.invoke("again").status == "completed"
    assert runner.latest_usage()["total_tokens"] == 45


def test_context_window_comes_from_the_active_profile() -> None:
    settings = Settings.from_mapping({
        "llm": {
            "default": "big",
            "models": {"big": {"model": "qwen3.5-plus", "context_window": 1_000_000}},
        },
    })
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        settings=settings,
    )
    assert runner.context_window() == 1_000_000
    assert AgentRunner(
        model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(),
    ).context_window() == 0


def test_execute_artifact_reaches_runner_event_after_approval(tmp_path) -> None:
    backend = UnsandboxedShellBackend(SandboxConfig(
        workspace=tmp_path, allow_unsandboxed=True, max_output_bytes=8,
    ))
    prepared = create_agent(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "exec-artifact", "name": "execute",
                "args": {"command": "printf ABCDefgh1234"},
            }]),
            AIMessage(content="done"),
        ]),
        backend=backend, skills=[],
    )
    runner = AgentRunner(prepared=prepared, thread_id="exec-artifact")
    events = []
    assert runner.invoke("run", on_event=events.append).status == "waiting_confirmation"
    assert runner.approve_tool("exec-artifact", on_event=events.append).status == "completed"
    completed = [event for event in events if event.type == "tool_completed" and event.name == "execute"]
    assert len(completed) == 1
    assert completed[0].result["truncated"] is True
    assert completed[0].result["agent_log_path"].startswith("/workspace/.deep-agent/logs/exec/")
    assert completed[0].result["exit_code"] == 0


def test_request_human_input_structured_fields() -> None:
    fields = [{
        "id": "access_scope",
        "type": "single_select",
        "label": "请选择权限范围",
        "required": True,
        "options": [
            {"value": "department_only", "label": "仅允许访问所在部门"},
            {"value": "global", "label": "允许跨部门访问"},
        ],
    }, {"id": "comment", "type": "textarea", "label": "补充说明", "required": False}]
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "hi-1",
                "name": "request_human_input",
                "args": {
                    "reason": "需求与部门隔离冲突",
                    "question": "管理员是否允许跨部门访问项目？",
                    "interaction_type": "decision",
                    "title": "确认管理员权限边界",
                    "fields": fields,
                },
            }]),
            AIMessage(content="将按部门隔离继续设计。"),
        ]),
        backend=StateBackend(),
        thread_id="schema",
    )
    waiting = runner.invoke("请设计权限")
    assert waiting.status == "waiting_human"
    payload = waiting.human_input
    assert payload["interactionType"] == "decision"
    assert payload["fields"][0]["type"] == "single_select"
    assert payload["interactionId"]
    assert "RadioGroup" not in json.dumps(payload)
    resumed = runner.submit_human_input({
        "access_scope": "department_only", "comment": "超级管理员后续再设计",
    })
    assert resumed.status == "completed"


def test_request_human_input_recovers_from_stringified_fields() -> None:
    # 模型经常把嵌套参数整体 json.dumps 成字符串；工具要自己还原，不能让它变成校验失败。
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "hi-str",
                "name": "request_human_input",
                "args": {
                    "reason": "缺少接入信息",
                    "question": "demo 用哪种 SDK 形态？",
                    "fields": json.dumps([{
                        "id": "sdk_choice", "type": "single_select", "label": "SDK 形态",
                        "options": [{"id": "deepagents", "label": "官方 deepagents 包"}],
                    }], ensure_ascii=False),
                    "impact": json.dumps(["SDK 选择决定依赖清单"], ensure_ascii=False),
                },
            }]),
            AIMessage(content="按 deepagents 继续。"),
        ]),
        backend=StateBackend(),
        thread_id="stringified",
    )
    waiting = runner.invoke("写个 demo")
    assert waiting.status == "waiting_human"
    payload = waiting.human_input
    assert payload["fields"][0]["type"] == "single_select"
    assert payload["fields"][0]["options"][0]["value"] == "deepagents"
    assert payload["impact"] == ["SDK 选择决定依赖清单"]
    assert runner.submit_human_input({"sdk_choice": "deepagents"}).status == "completed"


def test_invalid_tool_args_return_actionable_hint() -> None:
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "hi-bad",
                "name": "request_human_input",
                "args": {"reason": "r", "question": "q", "fields": "五个字段，见上文"},
            }]),
            AIMessage(content="改成纯文本提问。"),
        ]),
        backend=StateBackend(),
        thread_id="bad-args",
    )
    assert runner.invoke("写个 demo").status == "completed"
    messages = runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
    errors = [m for m in messages if isinstance(m, ToolMessage) and m.status == "error"]
    assert len(errors) == 1
    assert errors[0].tool_call_id == "hi-bad"
    content = str(errors[0].content)
    assert "不要原样重发" in content
    assert "原生 JSON 数组" in content


def test_pause_interrupts_at_safe_point_then_continues() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="暂停后继续完成。")]),
        backend=StateBackend(),
        thread_id="pause",
    )
    runner.request_pause()
    paused = runner.invoke("开始")
    assert paused.status == "paused"
    resumed = runner.continue_run()
    assert resumed.status == "completed"
    assert resumed.output == "暂停后继续完成。"


def test_prepared_runner_uses_its_pause_control() -> None:
    prepared = create_agent(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    runner = AgentRunner(prepared=prepared)
    assert runner.control is prepared.run_controller
    runner.request_pause()
    assert runner.invoke("start").status == "paused"
    assert runner.continue_run().output == "done"


def test_tool_error_does_not_repeat_a_possible_side_effect() -> None:
    attempts = 0

    def update_record() -> str:
        """Update a record once."""
        nonlocal attempts
        attempts += 1
        raise TimeoutError("response lost after update")

    prepared = create_agent(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{"id": "update-1", "name": "update_record", "args": {}}]),
            AIMessage(content="I cannot verify the update"),
        ]),
        backend=StateBackend(),
        extra_tools=[StructuredTool.from_function(update_record)],
    )
    result = AgentRunner(prepared=prepared).invoke("update")
    assert result.status == "failed"
    assert "response lost" in result.error
    assert attempts == 1


def test_failed_run_arms_recovery_for_the_same_runner() -> None:
    def fail_once() -> str:
        """Simulate a failed tool."""
        raise TimeoutError("unknown outcome")

    prepared = create_agent(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{"id": "fail-1", "name": "fail_once", "args": {}}]),
            AIMessage(content="continued"),
        ]), backend=StateBackend(), extra_tools=[StructuredTool.from_function(fail_once)],
    )
    runner = AgentRunner(prepared=prepared)
    assert runner.invoke("first").status == "failed"
    assert runner._resume_context is not None
    assert runner.invoke("inspect the state").output == "continued"
    assert runner._resume_context is None


def test_runner_rejects_concurrent_run_before_changing_state() -> None:
    from threading import Event, Thread

    entered = Event()
    release = Event()
    runner = AgentRunner(
        prepared=create_agent(model=scripted_model([AIMessage(content="done")]),
                              backend=StateBackend(), skills=[]),
    )
    results = []

    def on_event(event) -> None:
        if event.type == "run_started":
            entered.set()
            assert release.wait(5)

    thread = Thread(target=lambda: results.append(runner.invoke("first", on_event=on_event)))
    thread.start()
    assert entered.wait(5)
    try:
        original_thread = runner.thread_id
        with pytest.raises(RuntimeError, match="active operation"):
            runner.invoke("second")
        with pytest.raises(RuntimeError, match="active operation"):
            runner.continue_run()
        with pytest.raises(RuntimeError, match="active operation"):
            runner.new_session()
        assert runner.thread_id == original_thread
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert results[0].status == "completed"


def _hitl_payload(name: str = "write_file", args: dict | None = None) -> dict:
    return {
        "action_requests": [{"name": name, "args": args or {"file_path": "/workspace/note.txt"}}],
        "review_configs": [{"action_name": name, "allowed_decisions": ["approve", "reject"]}],
    }


def _graph_with_tool_calls(calls: list | None = None):
    from types import SimpleNamespace

    return SimpleNamespace(get_state=lambda _config: SimpleNamespace(values={
        "messages": [AIMessage(content="", tool_calls=calls or [])],
    }))


def test_classify_interrupt_accepts_known_protocols() -> None:
    graph = _graph_with_tool_calls([{
        "id": "c1", "name": "write_file", "args": {"file_path": "/workspace/note.txt"},
    }])
    human = classify_interrupt(
        [{"type": "human_input", "fields": [], "question": "选哪个？"}], graph, {},
    )
    assert human.kind is InterruptKind.WAITING_HUMAN
    assert human.payload["question"] == "选哪个？"

    paused = classify_interrupt([{"type": "pause"}], graph, {})
    assert paused.kind is InterruptKind.PAUSED

    hitl = classify_interrupt([_hitl_payload()], graph, {})
    assert hitl.kind is InterruptKind.WAITING_CONFIRMATION
    assert hitl.pending_tools[0]["toolCallId"] == "c1"
    assert is_valid_hitl_interrupt(_hitl_payload())


def test_classify_interrupt_rejects_unknown_and_malformed() -> None:
    graph = _graph_with_tool_calls()
    with pytest.raises(UnknownInterruptError, match="Unsupported interrupt type"):
        classify_interrupt([{"type": "something_new"}], graph, {})
    with pytest.raises(UnknownInterruptError):
        classify_interrupt([{"action_requests": [{"name": "write_file", "args": {}}]}], graph, {})
    with pytest.raises(UnknownInterruptError):
        classify_interrupt([{
            "action_requests": [{"name": "write_file"}],
            "review_configs": [{"action_name": "write_file", "allowed_decisions": ["approve"]}],
        }], graph, {})
    with pytest.raises(UnknownInterruptError, match="no recognizable payload"):
        classify_interrupt(["not-a-dict"], graph, {})
    assert not is_valid_hitl_interrupt({"action_requests": [{"name": "write_file", "args": {}}]})


def test_current_interrupt_is_none_without_pending_interrupt() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    assert runner.current_interrupt() is None
    assert runner.invoke("go").status == "completed"
    assert runner.current_interrupt() is None
