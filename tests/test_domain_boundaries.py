"""Protect domain rules from adapter dependencies and exercise their invariants."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from agent.execution.types import ExecutionMode
from agent.models.catalog import ModelCatalog
from agent.models.profile import ModelProfile
from agent.runtime.configuration import RuntimeConfiguration
from agent.runtime.permissions import PermissionMode
from agent.sessions.contracts import SessionInfo
from agent.sessions.lifecycle import SessionRuntime


PURE_MODULES = (
    "models/profile.py", "models/catalog.py", "execution/policy.py", "execution/types.py",
    "runtime/contracts.py", "runtime/configuration.py", "runtime/permissions.py",
    "sessions/contracts.py", "sessions/images.py", "sessions/recovery.py", "sessions/ports.py",
    "sessions/snapshots.py", "sessions/summary.py", "mcp/contracts.py", "subagents/configuration.py",
)


@pytest.mark.parametrize("module", PURE_MODULES)
def test_domain_rules_do_not_import_infrastructure(module):
    source = Path("agent") / module
    tree = ast.parse(source.read_text())
    forbidden = ("agent.bootstrap", "agent.cli", "agent.tools", "agent.middleware",
                 "sqlite3", "yaml", "httpx", "langchain", "langgraph", "deepagents")
    forbidden_modules = {"agent.sessions.store", "agent.sessions.attachments", "agent.sessions.locks",
                         "agent.execution.backend", "agent.execution.process", "agent.execution.pool"}
    for node in ast.walk(tree):
        modules = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                   else [v.name for v in node.names] if isinstance(node, ast.Import) else [])
        for dependency in modules:
            assert not dependency.startswith(forbidden), (source, dependency)
            assert dependency not in forbidden_modules, (source, dependency)


@pytest.mark.parametrize("module", (
    "model_selection", "access_selection", "attachments_controller", "transcript_navigation",
    "run_dispatch", "run_events_controller", "interaction_flow", "session_controller", "lifecycle",
    "skill_selection", "context_compaction", "session_summaries",
))
def test_ui_workflows_do_not_depend_on_the_application(module):
    tree = ast.parse(Path(f"agent/cli/{module}.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "agent.cli.app"
        if isinstance(node, ast.Attribute):
            assert node.attr != "_app"


def test_loading_domain_contracts_does_not_initialize_runtime_adapters():
    code = '''
import sys
from agent import ModelProfile, RunEvent, ImageAttachmentRef
from agent.mcp import MCPResult, ServerStatus
assert not any(name in sys.modules for name in ("yaml", "sqlite3", "httpx", "agent.runtime.runner"))
'''
    subprocess.run([sys.executable, "-c", code], check=True)


def test_invalid_model_request_keeps_previous_pending_selection():
    configuration = RuntimeConfiguration(current_model_id="alpha")
    beta = ModelProfile(id="beta", model="beta", reasoning_efforts=("high",))
    configuration.request_model(beta, "high")
    with pytest.raises(ValueError, match="Unsupported reasoning effort"):
        configuration.request_model(beta, "invalid")
    assert configuration.pending_model_id == "beta"
    assert configuration.pending_reasoning_effort == "high"
    assert configuration.current_model_id == "alpha"
    configuration.request_model(ModelProfile(id="alpha", model="alpha"))
    assert not configuration.has_pending()


def test_allow_is_rejected_without_a_sandbox_without_changing_pending_state():
    configuration = RuntimeConfiguration()
    with pytest.raises(ValueError, match="SANDBOXED"):
        configuration.request_permission("allow", ExecutionMode.CUSTOM)
    assert configuration.pending_permission_mode is None
    assert configuration.permission_mode is PermissionMode.ASK
    configuration.request_permission("allow", ExecutionMode.SANDBOXED)
    assert configuration.pending_permission_mode is PermissionMode.ALLOW
    assert configuration.permission_mode is PermissionMode.ASK


def test_restore_policy_uses_model_catalog_without_configuration_io():
    catalog = ModelCatalog((ModelProfile(id="available", model="local"),), "available")
    runtime = SessionRuntime(session_store=None, checkpointer=None, settings=catalog)
    now = datetime.now(timezone.utc)
    info = SessionInfo(id="saved", title="", created_at=now, updated_at=now, status="waiting",
                       model_id="removed", reasoning_effort="high", permission_mode="allow")
    plan = runtime.restore_plan(info, execution_mode=ExecutionMode.CUSTOM)
    assert plan.model_id == "available"
    assert plan.reasoning_effort == "default"
    assert plan.permission_mode is PermissionMode.ASK
    assert len(plan.notices) == 3


def test_session_checkpoint_lookup_uses_repository_port():
    class Repository:
        def has_checkpoint(self, thread_id):
            return thread_id == "saved"
    runtime = SessionRuntime(session_store=Repository(), checkpointer=None, settings=None)
    assert runtime.thread_has_checkpoint("saved")
    assert not runtime.thread_has_checkpoint("missing")
