from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

from langchain_core.messages import AIMessage, HumanMessage
from deepagents.backends import StateBackend

from agent.attachments import (
    ATTACHMENT_META_KEY,
    AttachmentStore,
    ImageAttachmentRef,
    find_attachment_storage_keys,
    image_attachment_from_bytes,
)
from agent.config import ModelProfile, Settings
from agent.llm import chat_openai
from agent.runner import AgentRunner
from agent.session import SessionStore, messages_to_transcript
from tests.conftest import scripted_model


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image-content"


def _image(name: str = "screen.png"):
    return image_attachment_from_bytes(PNG, filename=name)


def test_attachment_store_deduplicates_and_rejects_bad_keys(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    first = store.put(_image("one.png"))
    second = store.put(_image("two.png"))
    assert first.storage_key == second.storage_key
    assert first.id != second.id
    assert len(list((tmp_path / "attachments").glob("*.png"))) == 1
    assert store.read(first).data == PNG
    try:
        store.read(ImageAttachmentRef("x", "x.png", "image/png", 1, "../x.png"))
    except ValueError as exc:
        assert "storage key" in str(exc)
    else:
        raise AssertionError("path traversal key was accepted")


def test_qwen_materializes_ref_only_for_request(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    ref = store.put(_image())
    message = HumanMessage(
        content="describe",
        additional_kwargs={ATTACHMENT_META_KEY: [ref.to_dict()]},
    )
    llm = chat_openai(
        model="qwen3.5-plus",
        api_key="test",
        base_url="http://localhost:8000/v1",
        attachment_store=store,
    )
    payload = llm._get_request_payload([message])
    blocks = payload["input"][0]["content"]
    assert blocks[0] == {"type": "input_text", "text": "describe"}
    assert blocks[1]["type"] == "input_image"
    assert blocks[1]["image_url"].startswith("data:image/png;base64,")
    assert message.content == "describe"
    assert message.additional_kwargs[ATTACHMENT_META_KEY] == [ref.to_dict()]


def test_refs_are_builtin_checkpoint_data_and_transcript_keeps_images(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    ref = store.put(_image())
    raw = ref.to_dict()
    assert find_attachment_storage_keys({"nested": [raw]}) == {ref.storage_key}
    message = HumanMessage(content="look", additional_kwargs={ATTACHMENT_META_KEY: [raw]})
    transcript = messages_to_transcript([message])
    assert transcript[0].attachments == (ref,)


def test_workspace_gc_keeps_checkpoint_refs_and_deletes_orphans(tmp_path: Path) -> None:
    database = tmp_path / "workspace.sqlite3"
    session = SessionStore(database)
    settings = Settings(
        llm_profiles=(ModelProfile("vision", "fake", input=("text", "image")),),
        llm_default="vision",
    )
    model = scripted_model([AIMessage(content="done")])
    object.__setattr__(model, "materializes_attachment_refs", True)
    runner = AgentRunner(
        model=model,
        backend=StateBackend(),
        session_store=session,
        settings=settings,
    )
    live = session.attachment_store.put(_image("live.png"))
    runner.invoke_with_attachment_refs("look", image_refs=(live,))
    orphan = session.attachment_store.put(image_attachment_from_bytes(
        b"\x89PNG\r\n\x1a\n" + b"orphan", filename="orphan.png",
    ))
    result = runner.cleanup_attachments()
    assert result.deleted == 1
    assert session.attachment_store.read(live).data == PNG
    assert not (session.attachment_store.root / orphan.storage_key).exists()

    persisted = database.read_bytes() + database.with_name(database.name + "-wal").read_bytes()
    assert b"data:image" not in persisted
    assert PNG not in persisted
    session.close()


def test_image_can_be_stored_during_active_run() -> None:
    entered = Event()
    release = Event()
    settings = Settings(
        llm_profiles=(ModelProfile("vision", "fake", input=("text", "image")),),
        llm_default="vision",
    )
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="done")]),
        backend=StateBackend(), settings=settings,
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
        ref = runner.store_image(_image())
        assert runner.attachment_store.read(ref).data == PNG
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert results[0].status == "completed"


def test_startup_gc_removes_unreferenced_workspace_attachment(tmp_path: Path) -> None:
    database = tmp_path / "startup.sqlite3"
    first = SessionStore(database)
    orphan = first.attachment_store.put(_image("orphan.png"))
    orphan_path = first.attachment_store.root / orphan.storage_key
    assert orphan_path.exists()
    first.close()

    reopened = SessionStore(database)
    assert reopened.startup_cleanup is not None
    assert reopened.startup_cleanup.deleted == 1
    assert not orphan_path.exists()
    reopened.close()
