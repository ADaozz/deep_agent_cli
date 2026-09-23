import json

from langchain_core.messages import AIMessage
from deepagents.backends import StateBackend

from agent.factory import create_agent
from agent.runner import AgentRunner
from tests.conftest import graph_tool_names, scripted_model


def test_factory_exposes_local_tools_and_marks_confirm(fake_done) -> None:
    prepared = create_agent(model=fake_done, backend=StateBackend(), skills=[])
    assert prepared.exposed_tool_names == ["lookup_docs", "send_email"]
    assert prepared.interrupt_on == {"send_email": {"allowed_decisions": ["approve", "reject"]}}
    assert "Deep Agent" in prepared.system_prompt
    names = graph_tool_names(prepared.graph)
    assert "handoff_to_human" in names
    assert "request_human_input" in names
    assert "lookup_docs" in names
    assert "send_email" in names
    assert "write_todos" in names
    assert "execute" not in names
    assert len(names) == 12


def test_allow_tool_runs_locally() -> None:
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-1", "name": "lookup_docs", "args": {"query": "middleware"},
            }]),
            AIMessage(content="中间件栈已说明。"),
        ]),
        backend=StateBackend(),
        thread_id="allow",
    )
    result = runner.invoke("查 middleware")
    assert result.status == "completed"
    assert result.output == "中间件栈已说明。"


def test_confirm_tool_interrupts_then_resumes() -> None:
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-mail",
                "name": "send_email",
                "args": {"to": "a@b.com", "subject": "hi", "body": "hello"},
            }]),
            AIMessage(content="邮件已发送。"),
        ]),
        backend=StateBackend(),
        thread_id="confirm",
    )
    waiting = runner.invoke("发一封邮件")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "send_email"
    assert waiting.pending_tool_calls[0]["toolCallId"] == "call-mail"
    resumed = runner.resume({"type": "approve", "toolCallId": "call-mail"})
    assert resumed.status == "completed"
    assert resumed.output == "邮件已发送。"


def test_handoff_to_human_interrupts_then_resumes() -> None:
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "handoff-1",
                "name": "handoff_to_human",
                "args": {"reason": "缺业务决策", "question": "用方案 A 还是 B？", "required_input": "A 或 B"},
            }]),
            AIMessage(content="将按方案 A 继续。"),
        ]),
        backend=StateBackend(),
        thread_id="human",
    )
    waiting = runner.invoke("请实现")
    assert waiting.status == "waiting_human"
    assert waiting.human_input["question"] == "用方案 A 还是 B？"
    resumed = runner.resume({"type": "human_input", "text": "方案 A"})
    assert resumed.status == "completed"
    assert resumed.output == "将按方案 A 继续。"


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
    resumed = runner.resume({
        "type": "human_input",
        "interactionId": payload["interactionId"],
        "values": {"access_scope": "department_only", "comment": "超级管理员后续再设计"},
    })
    assert resumed.status == "completed"


def test_pause_interrupts_at_safe_point_then_continues() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="暂停后继续完成。")]),
        backend=StateBackend(),
        thread_id="pause",
    )
    runner.request_pause()
    paused = runner.invoke("开始")
    assert paused.status == "paused"
    resumed = runner.resume({"type": "continue"})
    assert resumed.status == "completed"
    assert resumed.output == "暂停后继续完成。"
