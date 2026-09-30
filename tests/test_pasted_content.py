from agent.cli.pasted_content import PASTED_CONTENT_MIN_CHARS, PastedContentDraft


def test_long_pastes_have_visible_labels_and_expand_in_order() -> None:
    draft = PastedContentDraft()
    first = "甲" * PASTED_CONTENT_MIN_CHARS
    second = "乙" * PASTED_CONTENT_MIN_CHARS
    first_label = draft.display(first)
    second_label = draft.display(second)
    assert first_label == f"[Pasted Content {PASTED_CONTENT_MIN_CHARS} chars]"
    assert second_label == f"[Pasted Content {PASTED_CONTENT_MIN_CHARS} chars #2]"
    assert draft.expand(f"前言 {first_label} 中间 {second_label} 结尾") == f"前言 {first} 中间 {second} 结尾"
    draft.clear()
    assert not draft.has_blocks


def test_short_paste_stays_editable_text() -> None:
    draft = PastedContentDraft()
    assert draft.display("短文本") == "短文本"
    assert not draft.has_blocks


def test_edited_marker_is_flagged_before_submit() -> None:
    draft = PastedContentDraft()
    label = draft.display("甲" * PASTED_CONTENT_MIN_CHARS)
    assert not draft.has_invalid_marker(f"前 {label} 后")
    assert draft.has_invalid_marker("[Pasted Content 999 chars]")
    assert draft.has_invalid_marker("看 [Pasted Content")
    assert not draft.has_invalid_marker("普通文本没有标记")
    assert not draft.has_invalid_marker(f"谈及 Pasted Content 即可 {label}")
    draft.clear()
    assert not draft.has_invalid_marker("[Pasted Content 999 chars]")
