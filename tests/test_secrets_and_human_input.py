"""Stage 3 tests: SecretStore, secure human input, isolated TUI secret editor, /secrets, runtime_auth."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout.processors import PasswordProcessor
from prompt_toolkit.output import DummyOutput
from pydantic import ValidationError
import pytest

from agent.cli.app import CliApplication
from agent.cli.interactions import InteractionController
from agent.cli.rendering import render_interaction
from agent.cli.secrets_ui import format_secrets_table, handle_secrets_command
from agent.mcp.oauth_storage import oauth_token_storage
from agent.runtime.contracts import RunEvent
from agent.runtime.runner import AgentRunner
from agent.secrets import (
    SecretAccessError,
    SecretExistsError,
    SecretStore,
    SecretSubmission,
    load_or_create_fernet,
)
from agent.sessions.store import SessionStore
from agent.tools.human_input import HumanInputArgs
from agent.tools.human_interaction import (
    build_runtime_auth_request,
    normalize_interaction_request,
)
from tests.conftest import scripted_model


# ---------------------------------------------------------------------------
# 1. Unified SecretStore
# ---------------------------------------------------------------------------


def test_secret_store_session_isolation_and_cleanup(tmp_path: Path) -> None:
    store = SecretStore(tmp_path, environ={})
    store.set_secret("API_TOKEN", "val-session-a", scope="sandbox", storage="session", session_id="sess-a")
    store.set_secret("API_TOKEN", "val-session-b", scope="sandbox", storage="session", session_id="sess-b")

    res_a = store.resolve_secret("API_TOKEN", scope="sandbox", session_id="sess-a", consumer="sandbox")
    res_b = store.resolve_secret("API_TOKEN", scope="sandbox", session_id="sess-b", consumer="sandbox")
    res_c = store.resolve_secret("API_TOKEN", scope="sandbox", session_id="sess-c", consumer="sandbox")
    assert res_a is not None and res_a.value == "val-session-a"
    assert res_b is not None and res_b.value == "val-session-b"
    assert res_c is None
    assert "val-session-a" not in repr(res_a)

    # clear_session only drops sess-a
    store.clear_session("sess-a")
    assert store.resolve_secret("API_TOKEN", scope="sandbox", session_id="sess-a", consumer="sandbox") is None
    assert store.resolve_secret("API_TOKEN", scope="sandbox", session_id="sess-b", consumer="sandbox") is not None


def test_secret_store_persistent_encryption_permissions_and_shared_fernet(tmp_path: Path) -> None:
    secret_val = "ghp_super_secret_persistent_value_98765"
    store = SecretStore(tmp_path, environ={})
    meta = store.set_secret(
        "GITHUB_TOKEN",
        secret_val,
        scope="host",
        storage="persistent",
        consumers=("mcp:github@https://api.github.com",),
    )
    assert meta.name == "GITHUB_TOKEN"
    assert meta.scope == "host"
    assert meta.storage == "persistent"
    assert secret_val not in repr(meta)

    secrets_dir = tmp_path / "secrets"
    key_path = secrets_dir / "key"
    entry_path = secrets_dir / "entries" / "GITHUB_TOKEN.enc"
    assert stat.S_IMODE(os.stat(secrets_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(entry_path).st_mode) == 0o600

    # Disk must not contain plaintext anywhere under secrets_dir.
    for path in secrets_dir.rglob("*"):
        if path.is_file():
            assert secret_val.encode("utf-8") not in path.read_bytes()

    # Reload via a brand new SecretStore instance.
    reloaded = SecretStore(tmp_path, environ={})
    resolved = reloaded.resolve_secret(
        "GITHUB_TOKEN",
        scope="host",
        consumer="mcp:github@https://api.github.com",
    )
    assert resolved is not None and resolved.value == secret_val and resolved.storage == "persistent"

    # Unauthorized host consumer or wrong scope is denied.
    with pytest.raises(SecretAccessError):
        reloaded.resolve_secret("GITHUB_TOKEN", scope="host", consumer="mcp:other@https://evil.example")
    with pytest.raises(SecretAccessError):
        reloaded.resolve_secret("GITHUB_TOKEN", scope="sandbox", consumer="sandbox")

    # Shared load_or_create_fernet helper powers oauth_token_storage cleanly too.
    oauth_dir = tmp_path / "oauth"
    oauth_token_storage(oauth_dir)
    assert stat.S_IMODE(os.stat(oauth_dir / "key").st_mode) == 0o600
    assert load_or_create_fernet(oauth_dir) is not None


def test_secret_store_rejects_symlink_entries(tmp_path: Path) -> None:
    store = SecretStore(tmp_path, environ={})
    store.set_secret("SAFE_KEY", "ok", scope="host", storage="persistent", consumers=("host:*",))
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("decoy", encoding="utf-8")
    symlink_entry = tmp_path / "secrets" / "entries" / "LINKED_KEY.enc"
    os.symlink(decoy, symlink_entry)
    with pytest.raises(OSError):
        store.resolve_secret("LINKED_KEY", scope="host", consumer="host:*")


def test_secret_store_priority_and_env_fallback(tmp_path: Path) -> None:
    env = {"MY_API_KEY": "from-env"}
    store = SecretStore(tmp_path, environ=env)

    # 1) Env fallback only when allow_env=True
    assert store.resolve_secret("MY_API_KEY", scope="host", consumer="mcp:demo", allow_env=False) is None
    from_env = store.resolve_secret("MY_API_KEY", scope="host", consumer="mcp:demo", allow_env=True)
    assert from_env is not None and from_env.value == "from-env" and from_env.storage == "environment"

    # 2) Persistent overrides env
    store.set_secret(
        "MY_API_KEY", "from-persistent", scope="host", storage="persistent", consumers=("mcp:demo",),
    )
    from_pers = store.resolve_secret("MY_API_KEY", scope="host", consumer="mcp:demo", allow_env=True)
    assert from_pers is not None and from_pers.value == "from-persistent" and from_pers.storage == "persistent"

    # 3) Session overrides persistent (with overwrite=True)
    store.set_secret(
        "MY_API_KEY",
        "from-session",
        scope="host",
        storage="session",
        session_id="s1",
        consumers=("mcp:demo",),
        overwrite=True,
    )
    from_sess = store.resolve_secret(
        "MY_API_KEY", scope="host", session_id="s1", consumer="mcp:demo", allow_env=True,
    )
    assert from_sess is not None and from_sess.value == "from-session" and from_sess.storage == "session"

    # 4) Global consumer with allow_session=False refuses session entry instead of leaking/caching it
    with pytest.raises(SecretAccessError):
        store.resolve_secret(
            "MY_API_KEY", scope="host", session_id="s1", consumer="mcp:demo", allow_session=False,
        )


def test_sandbox_env_batch_reuses_single_secret_resolution_priority_and_auth(
    tmp_path: Path,
) -> None:
    store = SecretStore(tmp_path, environ={"ENV_ONLY": "from-env"})
    store.set_secret("PERSIST", "persistent", scope="sandbox", storage="persistent")
    store.set_secret(
        "HOST_ONLY", "host", scope="host", storage="persistent", consumers=("host:*",),
    )
    store.set_secret(
        "PERSIST", "session", scope="sandbox", storage="session",
        session_id="sid", overwrite=True,
    )
    store.set_secret(
        "SHADOW", "persistent-sandbox", scope="sandbox", storage="persistent",
    )
    store.set_secret(
        "SHADOW", "session-host", scope="host", storage="session",
        session_id="sid", overwrite=True,
    )

    env = store.sandbox_env_for_session("sid")
    assert env == {"PERSIST": "session"}
    for name in ("PERSIST", "HOST_ONLY", "SHADOW", "ENV_ONLY"):
        try:
            resolved = store.resolve_secret(
                name, scope="sandbox", session_id="sid", consumer="sandbox",
            )
        except SecretAccessError:
            resolved = None
        assert env.get(name) == (resolved.value if resolved is not None else None)


def test_secret_store_overwrite_guard_atomic_rollback_and_retired_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path, environ={})
    store.set_secret("FIRST_KEY", "old-first", scope="sandbox", storage="session", session_id="s1")

    with pytest.raises(SecretExistsError) as exc:
        store.set_secret("FIRST_KEY", "new-first", scope="sandbox", storage="session", session_id="s1")
    assert exc.value.names == ("FIRST_KEY",)
    assert store.resolve_secret("FIRST_KEY", scope="sandbox", session_id="s1", consumer="sandbox").value == "old-first"

    # Simulate mid-batch persistent disk failure -> session + earlier persistent writes roll back!
    orig_write = store._write_persistent_locked
    calls = {"n": 0}

    def flaky_write(record, fernet=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return orig_write(record, fernet)

    monkeypatch.setattr(store, "_write_persistent_locked", flaky_write)
    with pytest.raises(OSError, match="disk full"):
        store.set_secrets_atomic(
            [
                ("BATCH_SESS", "sess-secret", "sandbox", "session", ()),
                ("BATCH_P1", "pers-secret-1", "sandbox", "persistent", ()),
                ("BATCH_P2", "pers-secret-2", "sandbox", "persistent", ()),
            ],
            session_id="s1",
        )
    assert store.resolve_secret("BATCH_SESS", scope="sandbox", session_id="s1", consumer="sandbox") is None
    assert store.resolve_secret("BATCH_P1", scope="sandbox", session_id="s1", consumer="sandbox") is None
    assert store.resolve_secret("BATCH_P2", scope="sandbox", session_id="s1", consumer="sandbox") is None

    # Update & delete retain retired values in redaction_refs for in-flight process output redaction.
    monkeypatch.setattr(store, "_write_persistent_locked", orig_write)
    store.set_secret(
        "FIRST_KEY", "rotated-first", scope="sandbox", storage="session", session_id="s1", overwrite=True,
    )
    assert store.delete_secret("FIRST_KEY", session_id="s1") is True
    redacted_vals = {ref.value for ref in store.redaction_refs()}
    assert "old-first" in redacted_vals
    assert "rotated-first" in redacted_vals


def test_secret_store_thread_safety(tmp_path: Path) -> None:
    store = SecretStore(tmp_path, environ={})

    def worker(idx: int) -> None:
        name = f"KEY_{idx}"
        val = f"secret-val-{idx}"
        store.set_secret(name, val, scope="sandbox", storage="persistent", overwrite=True)
        resolved = store.resolve_secret(name, scope="sandbox", consumer="sandbox")
        assert resolved is not None and resolved.value == val

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(18)))

    assert len(store.list_secrets()) == 18


# ---------------------------------------------------------------------------
# 2. request_human_input schema & AgentRunner.submit_human_input isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_type", ["password", "token", "secret_text", "api_key", "credential", "passphrase"])
def test_secret_type_aliases_are_rejected_and_never_degrade_to_text(bad_type: str) -> None:
    with pytest.raises(ValidationError):
        HumanInputArgs.model_validate({
            "reason": "need auth",
            "question": "enter secret",
            "fields": [{"id": "tok", "type": bad_type, "label": "Token"}],
        })
    with pytest.raises(ValueError, match="type='secret'"):
        normalize_interaction_request(
            reason="need auth",
            question="enter secret",
            fields=[{"id": "tok", "type": bad_type, "label": "Token"}],
        )


def test_secret_field_validation_rules() -> None:
    # Missing secret_name / secret_scope
    with pytest.raises(ValidationError):
        HumanInputArgs.model_validate({
            "reason": "r", "question": "q",
            "fields": [{"id": "tok", "type": "secret", "label": "Token"}],
        })
    # Reserved secret_name (PATH, LD_PRELOAD, etc.)
    with pytest.raises(ValidationError):
        HumanInputArgs.model_validate({
            "reason": "r", "question": "q",
            "fields": [{"id": "tok", "type": "secret", "secret_name": "LD_PRELOAD", "secret_scope": "sandbox"}],
        })
    # Invalid scope
    with pytest.raises(ValidationError):
        HumanInputArgs.model_validate({
            "reason": "r", "question": "q",
            "fields": [{"id": "tok", "type": "secret", "secret_name": "API_KEY", "secret_scope": "global"}],
        })
    # Options not allowed on secret field
    with pytest.raises(ValidationError):
        HumanInputArgs.model_validate({
            "reason": "r", "question": "q",
            "fields": [{
                "id": "tok", "type": "secret", "secret_name": "API_KEY", "secret_scope": "host",
                "options": [{"value": "a", "label": "A"}],
            }],
        })
    # secret_name / secret_scope disallowed on non-secret field
    with pytest.raises(ValidationError):
        HumanInputArgs.model_validate({
            "reason": "r", "question": "q",
            "fields": [{"id": "tok", "type": "text", "secret_name": "API_KEY"}],
        })

    # Valid secret field normalizes cleanly
    args = HumanInputArgs.model_validate({
        "reason": "Need GitHub token",
        "question": "Provide your token",
        "fields": [{
            "id": "gh", "type": "secret", "label": "GitHub Token",
            "secret_name": "GITHUB_TOKEN", "secret_scope": "host",
        }],
    })
    payload = normalize_interaction_request(
        reason=args.reason, question=args.question, fields=[f.model_dump() for f in args.fields],
    )
    assert payload["source"] == "agent_input"
    assert payload["fields"][0]["type"] == "secret"
    assert payload["fields"][0]["secret_name"] == "GITHUB_TOKEN"
    assert payload["fields"][0]["secret_scope"] == "host"
    assert payload["fields"][0]["options"] == []


def test_runner_submit_human_input_keeps_plaintext_out_of_graph_checkpoint_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_plain = "sk_live_NEVER_LEAK_THIS_TO_GRAPH_999"
    store_db = SessionStore(tmp_path / "sessions.db")
    secret_store = SecretStore(tmp_path, environ={})
    seen_model_messages: list[str] = []

    model = scripted_model([
        AIMessage(content="", tool_calls=[{
            "id": "call-hi",
            "name": "request_human_input",
            "args": {
                "reason": "Need service name and API key",
                "question": "Configure upstream service",
                "fields": [
                    {"id": "service", "type": "text", "label": "Service", "required": True},
                    {
                        "id": "api_key",
                        "type": "secret",
                        "label": "API Key",
                        "required": True,
                        "secret_name": "UPSTREAM_API_KEY",
                        "secret_scope": "sandbox",
                    },
                ],
            },
        }]),
        AIMessage(content="Configured upstream service with reference UPSTREAM_API_KEY."),
    ])
    orig_generate = type(model)._generate

    def recording_generate(self_model, messages, *args, **kwargs):
        if self_model is model:
            for msg in messages:
                seen_model_messages.append(repr(msg))
        return orig_generate(self_model, messages, *args, **kwargs)

    monkeypatch.setattr(type(model), "_generate", recording_generate)
    runner = AgentRunner(
        model=model,
        backend=StateBackend(),
        thread_id="secret-e2e",
        session_store=store_db,
        secret_store=secret_store,
    )
    events: list[RunEvent] = []
    try:
        waiting = runner.invoke("setup service", on_event=events.append)
        assert waiting.status == "waiting_human"

        # 1) Reject raw secret passed in normal values dict -> no write, stays interrupted
        with pytest.raises(ValueError, match="isolated secrets"):
            runner.submit_human_input({"service": "acme", "api_key": secret_plain})
        assert secret_store.resolve_secret("UPSTREAM_API_KEY", scope="sandbox", session_id="secret-e2e", consumer="sandbox") is None
        assert runner.current_interrupt() is not None

        # 2) Mixed-field validation failure (missing required normal field) -> zero secret writes!
        with pytest.raises(ValueError, match="Required human input fields"):
            runner.submit_human_input(
                {},
                secrets={"api_key": SecretSubmission(value=secret_plain, storage="session")},
            )
        assert secret_store.resolve_secret("UPSTREAM_API_KEY", scope="sandbox", session_id="secret-e2e", consumer="sandbox") is None
        assert runner.current_interrupt() is not None

        # 3) Valid submission: writes secret first, resumes Graph with reference name only
        done = runner.submit_human_input(
            {"service": "acme"},
            secrets={"api_key": SecretSubmission(value=secret_plain, storage="session")},
            on_event=events.append,
        )
        assert done.status == "completed"

        # Secret is stored in SecretStore for this session
        resolved = secret_store.resolve_secret(
            "UPSTREAM_API_KEY", scope="sandbox", session_id="secret-e2e", consumer="sandbox",
        )
        assert resolved is not None and resolved.value == secret_plain

        # Model messages received reference name, NEVER plaintext
        joined_model = "\n".join(seen_model_messages)
        assert "UPSTREAM_API_KEY" in joined_model
        assert secret_plain not in joined_model

        # ToolMessage in LangGraph state has only reference name, NEVER plaintext
        state_values = runner.prepared.graph.get_state({"configurable": {"thread_id": runner.thread_id}}).values
        tool_msgs = [m for m in state_values.get("messages", []) if isinstance(m, ToolMessage)]
        assert tool_msgs
        assert "UPSTREAM_API_KEY" in str(tool_msgs[-1].content)
        assert secret_plain not in repr(state_values)

        # Checkpoint SQLite DB (and WAL) bytes NEVER contain plaintext
        store_db.close()
        db_files = [p for p in tmp_path.glob("sessions.db*") if p.is_file()]
        assert db_files
        for db_file in db_files:
            assert secret_plain.encode("utf-8") not in db_file.read_bytes()

        # RunEvents NEVER contain plaintext
        assert all(secret_plain not in repr(ev) for ev in events)
    finally:
        runner.close()
        store_db.close()


def test_runner_submit_human_input_save_failure_and_resume_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_store = SecretStore(tmp_path, environ={})
    # Pre-populate existing secret to trigger SecretExistsError when overwrite_secrets=False
    secret_store.set_secret("EXISTING_TOK", "original-val", scope="sandbox", storage="session", session_id="retry-t")

    model = scripted_model([
        AIMessage(content="", tool_calls=[{
            "id": "call-hi-2",
            "name": "request_human_input",
            "args": {
                "reason": "Need token",
                "question": "Enter token",
                "fields": [{
                    "id": "tok", "type": "secret", "label": "Token",
                    "secret_name": "EXISTING_TOK", "secret_scope": "sandbox",
                }],
            },
        }]),
        AIMessage(content="ok"),
    ])
    runner = AgentRunner(
        model=model, backend=StateBackend(), thread_id="retry-t", secret_store=secret_store,
    )
    try:
        waiting = runner.invoke("go")
        assert waiting.status == "waiting_human"

        # 1) Save fails (overwrite not confirmed) -> Graph NOT resumed, original value intact
        with pytest.raises(SecretExistsError):
            runner.submit_human_input(
                {},
                secrets={"tok": SecretSubmission(value="new-val", storage="session")},
                overwrite_secrets=False,
            )
        assert runner.current_interrupt() is not None
        assert secret_store.resolve_secret("EXISTING_TOK", scope="sandbox", session_id="retry-t", consumer="sandbox").value == "original-val"

        # 2) Save succeeds (overwrite_secrets=True) but Graph resume raises transiently -> retry succeeds cleanly!
        orig_stream = runner._stream_once
        failed_once = {"flag": False}

        def fail_first_resume(*args, **kwargs):
            if not failed_once["flag"]:
                failed_once["flag"] = True
                raise RuntimeError("transient resume failure")
            return orig_stream(*args, **kwargs)

        monkeypatch.setattr(runner, "_stream_once", fail_first_resume)
        with pytest.raises(RuntimeError, match="transient resume failure"):
            runner.submit_human_input(
                {},
                secrets={"tok": SecretSubmission(value="new-val", storage="session")},
                overwrite_secrets=True,
            )
        assert secret_store.resolve_secret("EXISTING_TOK", scope="sandbox", session_id="retry-t", consumer="sandbox").value == "new-val"
        assert runner.current_interrupt() is not None

        # Retry without re-triggering SecretExistsError even if overwrite_secrets=False now
        done = runner.submit_human_input(
            {},
            secrets={"tok": SecretSubmission(value="new-val", storage="session")},
            overwrite_secrets=False,
        )
        assert done.status == "completed"
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# 3. TUI InteractionController, masked secret_buffer, runtime_auth, /secrets
# ---------------------------------------------------------------------------


def test_interaction_controller_secret_rendering_storage_and_isolation() -> None:
    controller = InteractionController.human({
        "interactionId": "sec-1",
        "title": "API Credentials",
        "question": "Enter token",
        "fields": [
            {"id": "service", "type": "text", "label": "Service", "required": True},
            {
                "id": "token", "type": "secret", "label": "GitHub Token", "required": True,
                "secret_name": "GITHUB_TOKEN", "secret_scope": "host",
            },
        ],
    })
    assert controller.accepts_text is True
    assert controller.accepts_secret is False
    assert controller.accept("github") is False

    # Now on secret field
    assert controller.accepts_secret is True
    assert controller.accepts_text is False
    assert controller.current_secret_storage() == "session"
    rendered = render_interaction(controller, 100)
    assert "GitHub Token · Secure Input" in rendered
    assert "GITHUB_TOKEN" in rendered
    assert "Scope: Host" in rendered
    assert "Session" in rendered
    assert "Persistent" in rendered
    assert "Only the credential name is returned to the agent." in rendered

    # Up/Down toggles storage
    controller.move(1)
    assert controller.current_secret_storage() == "persistent"
    controller.move(-1)
    assert controller.current_secret_storage() == "session"

    secret_val = "ghp_secret_must_never_appear_in_repr_or_values"
    assert controller.accept_secret(secret_val) is True
    assert "token" not in controller.values
    assert secret_val not in repr(controller)
    assert secret_val not in render_interaction(controller, 100)

    subs = controller.secret_submissions()
    assert subs["token"].value == secret_val
    assert subs["token"].storage == "session"

    controller.clear_secrets()
    assert controller.secret_submissions() == {}


def test_cli_app_secret_buffer_isolation_masking_paste_shifttab_and_overwrite(tmp_path: Path) -> None:
    secret_store = SecretStore(tmp_path, environ={})
    secret_store.set_secret("DEMO_TOK", "existing-value", scope="sandbox", storage="session", session_id="cli-sec")

    model = scripted_model([
        AIMessage(content="", tool_calls=[{
            "id": "call-cli-sec",
            "name": "request_human_input",
            "args": {
                "reason": "Need demo token",
                "question": "Provide DEMO_TOK",
                "fields": [
                    {"id": "env", "type": "text", "label": "Env", "required": True},
                    {
                        "id": "tok", "type": "secret", "label": "Demo Token",
                        "secret_name": "DEMO_TOK", "secret_scope": "sandbox",
                    },
                ],
            },
        }]),
        AIMessage(content="done"),
    ])
    runner = AgentRunner(
        model=model, backend=StateBackend(), thread_id="cli-sec", secret_store=secret_store,
    )
    waiting = runner.invoke("go")
    assert waiting.status == "waiting_human"

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            # Secret editor masks characters with bullets
            assert any(isinstance(p, PasswordProcessor) for p in app.secret_editor.input_processors)

            app.run_events._handle_result(waiting)
            assert app.dialogs.current is not None
            assert app.dialogs.current.accepts_text is True

            # Enter normal field
            app.buffer.text = "staging"
            app.run_dispatch._submit_buffer("steer")
            assert app.dialogs.current.accepts_secret is True

            # Type/paste into secret_buffer; normal buffer, history, and drafts.pasted stay untouched!
            secret_val = "s3cr3t-value-xyz"
            app.secret_buffer.text = secret_val
            assert app.buffer.text == ""
            assert not app.drafts.pasted.has_blocks

            # Submit secret -> existing secret triggers overwrite confirmation instead of immediate write
            app.run_dispatch._submit_buffer("steer")
            assert app.secret_buffer.text == ""
            assert app.secret_buffer._undo_stack == []
            assert secret_val not in list(app.buffer.history.get_strings())
            assert app.dialogs.current is not None
            assert app.dialogs.current.current.get("id") == "__confirm_overwrite__"

            # Confirm overwrite (move down to "Overwrite" and submit)
            app.dialogs.current.move(1)
            app.run_dispatch._submit_buffer("steer")
            if app.run_view.task is not None:
                await app.run_view.task

            assert secret_store.resolve_secret("DEMO_TOK", scope="sandbox", session_id="cli-sec", consumer="sandbox").value == secret_val
            assert app.secret_buffer.text == ""
            assert secret_val not in list(app.buffer.history.get_strings())

    try:
        asyncio.run(scenario())
    finally:
        runner.close()


def test_cli_app_runtime_auth_callback_and_secrets_command(tmp_path: Path) -> None:
    secret_store = SecretStore(tmp_path, environ={})
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="cmd-sec",
        secret_store=secret_store,
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())

            # 1) runtime_auth uses the SAME InteractionController, calls callback directly, never writes SecretStore
            received: list[str | None] = []
            req = build_runtime_auth_request(
                interaction_id="auth-1",
                title="sudo authentication",
                question="Enter sudo password",
                reason="Elevated command requested",
            )
            app.dialogs.current = InteractionController.human(req, auth_callback=received.append)
            rendered = render_interaction(app.dialogs.current, 100)
            assert "Password is passed directly to sudo and is not stored." in rendered
            assert "Storage:" not in rendered
            app.secret_buffer.text = "my-sudo-pass"
            app.run_dispatch._submit_buffer("steer")
            assert received == ["my-sudo-pass"]
            assert app.secret_buffer.text == ""
            assert secret_store.list_secrets(session_id="cmd-sec") == []

            # Cancel runtime_auth sends None to callback
            received.clear()
            app.dialogs.current = InteractionController.human(req, auth_callback=received.append)
            app.secret_buffer.text = "aborted-pass"
            app.interaction_flow._finish_interaction(cancelled=True)
            assert received == [None]
            assert app.secret_buffer.text == ""

            # 2) /secrets set MY_KEY -> scope -> masked value -> saved -> /secrets list hides value -> delete
            await handle_secrets_command(app, "set MY_KEY")
            assert app.dialogs.current is not None and app.dialogs.current.kind == "secret_scope"
            # Select "sandbox" (index 1)
            app.dialogs.current.move(1)
            app.run_dispatch._submit_buffer("steer")
            assert app.dialogs.current is not None and app.dialogs.current.kind == "secret_value"
            assert app.dialogs.current.accepts_secret is True
            app.secret_buffer.text = "super-secret-cmd-value"
            app.run_dispatch._submit_buffer("steer")
            assert app.dialogs.current is None
            assert app.secret_buffer.text == ""
            assert secret_store.resolve_secret("MY_KEY", scope="sandbox", session_id="cmd-sec", consumer="sandbox").value == "super-secret-cmd-value"

            # /secrets list never shows plaintext
            await handle_secrets_command(app, "list")
            table = format_secrets_table(runner.list_secrets())
            assert "MY_KEY" in table
            assert "super-secret-cmd-value" not in table
            assert all("super-secret-cmd-value" not in repr(block) for block in app.state.blocks)

            # /secrets delete MY_KEY confirms before removing
            await handle_secrets_command(app, "delete MY_KEY")
            assert app.dialogs.current is not None and app.dialogs.current.kind == "secret_delete_confirm"
            app.dialogs.current.move(1)  # "Delete"
            app.run_dispatch._submit_buffer("steer")
            assert secret_store.resolve_secret("MY_KEY", scope="sandbox", session_id="cmd-sec", consumer="sandbox") is None

            # 3) BracketedPaste and Shift+Tab isolation in secret input mode
            controller = InteractionController.human({
                "interactionId": "sec-nav",
                "question": "Enter two secrets",
                "fields": [
                    {"id": "s1", "type": "secret", "label": "S1", "secret_name": "S1", "secret_scope": "sandbox"},
                    {"id": "s2", "type": "secret", "label": "S2", "secret_name": "S2", "secret_scope": "sandbox"},
                ],
            })
            app.dialogs.current = controller
            # Simulate BracketedPaste via registered keybinding handler
            from prompt_toolkit.keys import Keys
            paste_bindings = [b for b in app.bindings.bindings if b.keys == (Keys.BracketedPaste,)]
            assert paste_bindings

            class _FakeEvent:
                def __init__(self, data: str = "") -> None:
                    self.data = data
                    self.current_buffer = app.secret_buffer

            paste_bindings[0].handler(_FakeEvent("pasted-line1\npasted-line2\r\n"))
            assert app.secret_buffer.text == "pasted-line1pasted-line2"
            assert app.buffer.text == ""
            assert not app.drafts.pasted.has_blocks

            # Advance from s1 to s2, then press Shift+Tab back to s1 -> s1 must be erased, never copied to buffer
            app.run_dispatch._submit_buffer("steer")
            assert controller.index == 1
            assert app.secret_buffer.text == ""
            stab_bindings = [b for b in app.bindings.bindings if b.keys == (Keys.BackTab,)]
            assert stab_bindings
            stab_bindings[0].handler(_FakeEvent())
            assert controller.index == 0
            assert app.buffer.text == ""
            assert app.secret_buffer.text == ""
            assert controller.secret_submissions() == {}

            # reset_session_view clears secret_buffer & active controller secrets
            app.secret_buffer.text = "in-progress-secret"
            controller._secret_values["s1"] = "stored-secret"
            app.reset_session_view()
            assert app.secret_buffer.text == ""
            assert app.secret_buffer._undo_stack == []
            assert controller.secret_submissions() == {}

    try:
        asyncio.run(scenario())
    finally:
        runner.close()
