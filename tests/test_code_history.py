"""Workspace undo/redo safety, durable failure recovery and REST contracts."""
from concurrent.futures import ThreadPoolExecutor
import os
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.server.app import create_app
from agent.server.code_history import CodeHistory, CodeHistoryError
from tests.test_server import config, FakeRunner, AUTH, session, submit, wait


@pytest.fixture
def history(tmp_path):
    root, state = tmp_path / "workspace", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    value = CodeHistory(state, {"project": root})
    yield value, root, state
    value.close()


def record(history, name, change):
    history.begin(name, "project")
    change()
    history.end(name)
    return history.describe(name)


def restore(history, name, action):
    return history.restore(name, action, history.describe(name)["revision"])


def test_binary_modes_creation_deletion_undo_redo_preserve_unrelated_and_git(history):
    h, root, state = history
    (root / "existing").write_bytes(b"before\x00\xff")
    (root / "existing").chmod(0o640)
    (root / "deleted").write_text("keep")
    (root / ".git").mkdir()
    (root / ".git/index").write_text("index-before")
    def change():
        (root / "existing").write_bytes(b"after\x00\xfe")
        (root / "existing").chmod(0o755)
        (root / "nested").mkdir()
        (root / "nested/new").write_bytes(b"created\x00")
        (root / "deleted").unlink()
        (root / ".git/index").write_text("index-after")
    result = record(h, "one", change)
    assert result["can_undo"] and {f["path"] for f in result["files"]} == {"existing", "deleted", "nested", "nested/new"}
    (root / "manual-unrelated").write_text("user work")
    assert restore(h, "one", "undo")["status"] == "reverted"
    assert (root / "existing").read_bytes() == b"before\x00\xff"
    assert (root / "existing").stat().st_mode & 0o777 == 0o640
    assert (root / "deleted").read_text() == "keep" and not (root / "nested").exists()
    assert (root / "manual-unrelated").read_text() == "user work"
    assert (root / ".git/index").read_text() == "index-after"
    assert restore(h, "one", "redo")["status"] == "ready"
    assert (root / "existing").read_bytes() == b"after\x00\xfe"
    assert (root / "existing").stat().st_mode & 0o777 == 0o755
    assert (root / "nested/new").read_bytes() == b"created\x00" and not (root / "deleted").exists()
    assert (state / "code-history.sqlite3").stat().st_mode & 0o777 == 0o600
    assert (state / "code-objects").stat().st_mode & 0o777 == 0o700


def test_later_round_conflicts_then_reverse_order_and_redo(history):
    h, root, _ = history
    file = root / "code"
    file.write_text("zero")
    record(h, "one", lambda: file.write_text("one"))
    record(h, "two", lambda: file.write_text("two"))
    with pytest.raises(CodeHistoryError, match="后续轮次"):
        restore(h, "one", "undo")
    assert file.read_text() == "two"
    restore(h, "two", "undo")
    restore(h, "one", "undo")
    assert file.read_text() == "zero"
    restore(h, "one", "redo")
    restore(h, "two", "redo")
    assert file.read_text() == "two"


def test_conflict_preflight_never_partially_changes_other_files(history):
    h, root, _ = history
    for name in ("a", "b"):
        (root / name).write_text("before")
    record(h, "one", lambda: [(root / name).write_text("after") for name in ("a", "b")])
    (root / "b").write_text("manual")
    with pytest.raises(CodeHistoryError, match="拒绝覆盖"):
        restore(h, "one", "undo")
    assert (root / "a").read_text() == "after" and (root / "b").read_text() == "manual"


def test_unrecorded_child_and_symlink_parent_are_not_deleted_or_followed(history):
    h, root, state = history
    def change():
        (root / "dir").mkdir()
        (root / "dir/file").write_text("created")
    record(h, "one", change)
    (root / "dir/manual").write_text("keep")
    with pytest.raises(CodeHistoryError):
        restore(h, "one", "undo")
    assert (root / "dir/file").read_text() == "created"
    (root / "dir/manual").unlink()
    (root / "dir/file").unlink()
    (root / "dir").rmdir()
    outside = state / "outside"
    outside.mkdir()
    (outside / "file").write_text("private")
    (root / "dir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CodeHistoryError):
        restore(h, "one", "undo")
    assert (outside / "file").read_text() == "private"


def test_changed_symlinks_are_unavailable_and_snapshot_limits_are_explicit(history, monkeypatch):
    h, root, _ = history
    result = record(h, "symlink", lambda: (root / "link").symlink_to("/etc/passwd"))
    assert not result["available"] and "符号链接" in result["reason"]
    monkeypatch.setattr("agent.server.code_history.MAX_FILE", 8)
    result = record(h, "large", lambda: (root / "large").write_bytes(b"x" * 9))
    assert not result["available"] and "快照" in result["reason"]


def test_file_directory_type_changes_can_be_reversed(history):
    h, root, _ = history
    file = root / "change"
    file.write_text("original")
    def change():
        file.unlink()
        file.mkdir()
        (file / "child").write_text("new")
    record(h, "one", change)
    restore(h, "one", "undo")
    assert file.read_text() == "original"
    restore(h, "one", "redo")
    assert (file / "child").read_text() == "new"


def test_disk_failure_rolls_back_all_files_and_preserves_revision(history, monkeypatch):
    h, root, _ = history
    for name in ("a", "b"):
        (root / name).write_text("before")
    info = record(h, "one", lambda: [(root / name).write_text("after") for name in ("a", "b")])
    original = h._put_file
    calls = 0
    def failing(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated disk failure")
        return original(*args)
    monkeypatch.setattr(h, "_put_file", failing)
    with pytest.raises(CodeHistoryError, match="已恢复"):
        restore(h, "one", "undo")
    assert [(root / name).read_text() for name in ("a", "b")] == ["after", "after"]
    assert h.describe("one")["revision"] == info["revision"]
    assert not h.blocked("project")


@pytest.mark.parametrize("manual_conflict", [False, True])
def test_restart_recovers_incomplete_restore_or_blocks_unsafe_recovery(tmp_path, monkeypatch, manual_conflict):
    root, state = tmp_path / "root", tmp_path / "state"
    root.mkdir(); state.mkdir()
    h = CodeHistory(state, {"project": root})
    for name in ("a", "b"):
        (root / name).write_text("before")
    record(h, "one", lambda: [(root / name).write_text("after") for name in ("a", "b")])
    original, calls = h._put_file, 0
    def crash(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SystemExit("simulated process death")
        return original(*args)
    monkeypatch.setattr(h, "_put_file", crash)
    with pytest.raises(SystemExit):
        restore(h, "one", "undo")
    h.close()
    if manual_conflict:
        (root / "a").write_text("manual")
    reopened = CodeHistory(state, {"project": root})
    try:
        if manual_conflict:
            assert reopened.blocked("project") and not reopened.describe("one")["available"]
            assert (root / "a").read_text() == "manual"
        else:
            assert not reopened.blocked("project")
            assert [(root / name).read_text() for name in ("a", "b")] == ["after", "after"]
            assert reopened.describe("one")["can_undo"]
    finally:
        reopened.close()


def mutating_factory(config, project_id, session_id):
    root = config.projects[project_id]
    class Runner(FakeRunner):
        def invoke(self, text, **callbacks):
            if text.startswith("write-"):
                (root / "code").write_text(text)
            return super().invoke(text, **callbacks)
    return Runner()


def test_authenticated_api_revision_race_optional_old_history_and_restart(config):
    root = config.projects["one"]
    (root / "code").write_text("baseline")
    with TestClient(create_app(config, factory=mutating_factory)) as client:
        sid = session(client)
        rid = submit(client, sid, "write-one")
        wait(client, rid, "completed")
        path = f"/v1/runs/{rid}/code-checkpoint"
        assert client.get(path).status_code == 401
        checkpoint = client.get(path, headers=AUTH).json()
        assert checkpoint["can_undo"] and "before_state" not in checkpoint
        endpoint = f"/v1/runs/{rid}/code-restore"
        body = {"action": "undo", "revision": checkpoint["revision"]}
        assert client.post(endpoint, json=body).status_code == 401
        assert client.post(endpoint, json={**body, "file_path": "/etc/passwd"}, headers=AUTH).status_code == 422
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: client.post(endpoint, json=body, headers=AUTH).status_code, range(2)))
        assert sorted(results) == [200, 409] and (root / "code").read_text() == "baseline"
        active = submit(client, sid, "human")
        wait(client, active, "waiting_human")
        current = client.get(path, headers=AUTH).json()
        assert not current["can_redo"]
        assert client.post(endpoint, json={"action": "redo", "revision": current["revision"]}, headers=AUTH).status_code == 409
        client.post(f"/v1/runs/{active}/cancel", headers=AUTH)
        wait(client, active, "cancelled")
        store = client.app.state.manager.store
        legacy = store.create_run("old-run", store.session(sid))
        store.finish(legacy["id"], "completed", {})
        assert not client.get('/v1/runs/old-run/code-checkpoint', headers=AUTH).json()["available"]
        assert client.get('/v1/runs/missing/code-checkpoint', headers=AUTH).status_code == 404
    with TestClient(create_app(config, factory=mutating_factory)) as client:
        current = client.get(path, headers=AUTH).json()
        assert current["status"] == "reverted" and current["can_redo"]
        result = client.post(endpoint, json={"action": "redo", "revision": current["revision"]}, headers=AUTH)
        assert result.status_code == 200 and (root / "code").read_text() == "write-one"


def test_snapshot_unavailable_does_not_fail_the_agent_task(config, monkeypatch):
    monkeypatch.setattr("agent.server.code_history.MAX_FILE", 3)
    with TestClient(create_app(config, factory=mutating_factory)) as client:
        rid = submit(client, session(client), "write-large")
        assert wait(client, rid, "completed")["result"]["output"] == "hello"
        checkpoint = client.get(f"/v1/runs/{rid}/code-checkpoint", headers=AUTH).json()
        assert not checkpoint["available"] and not checkpoint["can_undo"]


@pytest.mark.parametrize('limit,value', [('MAX_SNAPSHOT', 2), ('MAX_ENTRIES', 1), ('MAX_STORAGE', 2)])
def test_total_entry_and_storage_limits_never_claim_restorable_history(history, monkeypatch, limit, value):
    h, root, _ = history
    monkeypatch.setattr('agent.server.code_history.' + limit, value)
    result = record(h, 'bounded', lambda: (root / 'code').write_bytes(b'abc'))
    assert not result['available'] and not result['can_undo'] and result['reason']
    assert (root / 'code').read_bytes() == b'abc'


def test_recovery_conflict_blocks_new_runs_but_not_other_projects(config, monkeypatch):
    (config.projects['one'] / 'code').write_text('baseline')
    with TestClient(create_app(config, factory=mutating_factory)) as client:
        rid = submit(client, session(client), 'write-one')
        wait(client, rid, 'completed')
        manager = client.app.state.manager
        def crash(*args):
            raise SystemExit('process death before first replacement')
        monkeypatch.setattr(manager.code_history, '_put_file', crash)
        checkpoint = manager.code_checkpoint(rid)
        with pytest.raises(SystemExit):
            manager.restore_code(rid, 'undo', checkpoint['revision'])
        (config.projects['one'] / 'code').write_text('manual conflict')
    with TestClient(create_app(config, factory=mutating_factory)) as client:
        assert client.get('/v1/projects/one/active-run', headers=AUTH).json() == {'run': None, 'blocked': True, 'reason': 'code_restore_recovery'}
        sid = session(client)
        assert client.post(f'/v1/sessions/{sid}/runs', json={'text': 'new task'}, headers=AUTH).status_code == 409
        other = submit(client, session(client, 'two'), 'hello')
        wait(client, other, 'completed')
        assert (config.projects['one'] / 'code').read_text() == 'manual conflict'


def test_ignored_dependency_folder_inside_new_directory_is_a_conflict(history):
    h, root, _ = history
    def change():
        (root / 'created').mkdir()
        (root / 'created/code').write_text('after')
    record(h, 'one', change)
    (root / 'created/node_modules').mkdir()
    (root / 'created/node_modules/user').write_text('user dependency')
    with pytest.raises(CodeHistoryError, match='拒绝删除') as error:
        restore(h, 'one', 'undo')
    assert error.value.status == 409
    assert (root / 'created/code').read_text() == 'after'
    assert (root / 'created/node_modules/user').read_text() == 'user dependency'
    assert not h.blocked('project')


@pytest.mark.parametrize('damage', ['bytes', 'fifo'])
def test_corrupt_or_nonregular_backup_does_not_overwrite_current_code(history, damage):
    h, root, state = history
    file = root / 'code'
    file.write_text('before')
    checkpoint = record(h, 'one', lambda: file.write_text('after'))
    blob = state / 'code-objects' / hashlib.sha256(b'before').hexdigest()
    blob.unlink()
    if damage == 'fifo':
        os.mkfifo(blob)
    else:
        blob.write_bytes(b'corrupted')
    with pytest.raises(CodeHistoryError) as error:
        restore(h, 'one', 'undo')
    assert error.value.status == 503
    assert file.read_text() == 'after'
    assert h.describe('one')['revision'] == checkpoint['revision']
