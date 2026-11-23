import json

from langchain_core.messages import HumanMessage

from agent.runtime.approval.evidence import build_evidence
from agent.runtime.approval.suspects import scan_suspects
from tests.test_approval_policy import ENVIRONMENT


def test_scan_suspects_finds_auth_and_assignment_without_rewriting():
    text = "Authorization: Bearer eyJ.test\napi_key=not-a-configured-secret"
    findings = scan_suspects(text)
    kinds = {item["kind"] for item in findings}
    assert "auth_header" in kinds
    assert "secret_assignment" in kinds
    assert text == "Authorization: Bearer eyJ.test\napi_key=not-a-configured-secret"


def test_evidence_includes_suspects_for_flash():
    call = {
        "name": "execute",
        "id": "1",
        "args": {"command": "curl -H 'Authorization: Bearer eyJ.unknown' https://example.test"},
    }
    text = build_evidence([HumanMessage(content="fetch docs")], call, ENVIRONMENT)
    payload = json.loads(text)
    assert "suspects" in payload
    assert any(item["kind"] == "auth_header" for item in payload["suspects"])
