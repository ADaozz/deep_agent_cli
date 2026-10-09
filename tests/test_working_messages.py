from agent.cli.working_messages import WorkingMessageRotation, load_working_messages
import pytest
import yaml

CONFIG = load_working_messages()
JOKES, FGO_LINES = CONFIG.jokes, CONFIG.fgo
WORKING_MESSAGES = CONFIG.messages


def test_rotation_shuffles_each_cycle_and_keeps_interval(monkeypatch) -> None:
    shuffles = []
    def reverse(items):
        shuffles.append(tuple(items))
        items.reverse()
    monkeypatch.setattr("agent.cli.working_messages.random.shuffle", reverse)
    rotation = WorkingMessageRotation()
    first = rotation.current(True, 100)
    assert first == JOKES[-1]
    assert rotation.current(True, 107.99) == first
    assert rotation.current(True, 108) == JOKES[-2]
    assert rotation.current(True, 116) == FGO_LINES[-1]
    seen = [rotation.current(True, 100 + index * 8) for index in range(len(WORKING_MESSAGES))]
    assert set(seen) == set(WORKING_MESSAGES)
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert len(shuffles) == 2
    next_message = rotation.current(True, 100 + len(WORKING_MESSAGES) * 8)
    assert next_message != seen[-1]
    assert len(shuffles) == 4


def test_rotation_stops_and_reshuffles_on_restart(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("agent.cli.working_messages.random.shuffle", lambda items: calls.append(tuple(items)))
    rotation = WorkingMessageRotation()
    rotation.current(True, 100)
    assert rotation.current(False, 117) is None
    assert rotation.current(False, 1000) is None
    assert len(calls) == 2
    assert rotation.current(True, 1001) in WORKING_MESSAGES
    assert len(calls) == 4
    rotation.reset()
    assert rotation.current(True, 1002) in WORKING_MESSAGES
    assert len(calls) == 6


def test_default_catalog_uses_every_joke_and_fgo_line() -> None:
    assert len(CONFIG.jokes) == 64
    assert set(CONFIG.jokes + CONFIG.fgo) <= set(CONFIG.messages)


def test_random_cycle_avoids_repeating_previous_message(monkeypatch) -> None:
    from agent.cli.working_messages import WorkingMessageConfig
    calls = []
    def shuffle(items):
        if items:
            calls.append(True)
            if len(calls) % 2 == 0:
                items.reverse()
    monkeypatch.setattr("agent.cli.working_messages.random.shuffle", shuffle)
    rotation = WorkingMessageRotation(WorkingMessageConfig(8, JOKES[:2], ()))
    seen = [rotation.current(True, 100 + i * 8) for i in range(6)]
    assert all(a != b for a, b in zip(seen, seen[1:]))


def test_user_config_is_seeded_once_and_edits_and_additions_are_loaded(tmp_path) -> None:
    path = tmp_path / "config" / "working_messages.yaml"
    initial = load_working_messages(path)
    assert initial.messages == CONFIG.messages
    custom = {
        "interval_seconds": 3,
        "jokes": ["需求改了八次，唯一没改的是交付日期。", "会议纪要很长，结论是再开一次。", "新增文案"],
        "fgo": [{"text": "流星一条！", "speaker": "阿拉什"}],
    }
    path.write_text(yaml.safe_dump(custom, allow_unicode=True), encoding="utf-8")
    saved = path.read_text()
    loaded = load_working_messages(path)
    assert not loaded.warning
    assert loaded.fgo[0].display == "流星一条！"
    assert path.read_text() == saved
    rotation = WorkingMessageRotation(loaded)
    seen = [rotation.current(True, 100 + i * 3).display for i in range(5)]
    assert set(seen) == set(custom["jokes"] + ["流星一条！"])
    assert rotation.current(True, 115).text in custom["jokes"]


@pytest.mark.parametrize("raw", [
    "jokes: [", "jokes: 文案", "interval_seconds: 0", "interval_seconds: .nan",
    "interval_seconds: true", "jokes: [123]", 'jokes: ["第一行\\n第二行"]',
    "jokes: [{text: 文案, speaker: 123}]", "jokes: ['']",
])
def test_invalid_config_falls_back_with_warning_without_overwriting(tmp_path, raw) -> None:
    path = tmp_path / "working_messages.yaml"
    path.write_text(raw, encoding="utf-8")
    loaded = load_working_messages(path)
    assert loaded.warning
    assert loaded.messages == CONFIG.messages
    assert path.read_text() == raw


@pytest.mark.parametrize("jokes,fgo", [([], []), (["只讲笑话"], []), ([], ["只有台词"])])
def test_empty_lists_and_single_category(tmp_path, jokes, fgo) -> None:
    path = tmp_path / "working_messages.yaml"
    path.write_text(yaml.safe_dump({"jokes": jokes, "fgo": fgo}, allow_unicode=True), encoding="utf-8")
    loaded = load_working_messages(path)
    assert not loaded.warning
    assert [m.text for m in loaded.messages] == jokes + fgo
    message = WorkingMessageRotation(loaded).current(True, 100)
    if not jokes and not fgo:
        assert message is None
    else:
        assert message.text == (jokes + fgo)[0]
