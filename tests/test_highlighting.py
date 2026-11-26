"""Unit tests for the shared CodeRenderer highlighting helpers."""

from __future__ import annotations

from io import StringIO

from pygments.lexers import BashLexer, PythonLexer, TextLexer, TypeScriptLexer
from rich.console import Console

from agent.cli.highlighting import (
    CodeRenderer,
    code_renderer,
    parse_numbered_change,
    rebuild_diff_sides,
)
from agent.cli.vscode_dark_plus import VSCodeDarkPlusStyle
from pygments.styles.monokai import MonokaiStyle


def test_detect_lexer_prefers_filename_and_language() -> None:
    assert isinstance(code_renderer.detect_lexer(filename="agent/runtime.py"), PythonLexer)
    assert isinstance(code_renderer.detect_lexer(language="typescript"), TypeScriptLexer)
    assert code_renderer.syntax_for_path("src/app.tsx") in {"tsx", "typescript"}


def test_detect_lexer_reads_diff_header_and_shebang() -> None:
    diff = "--- a/main.rs\n+++ b/main.rs\n@@ -1 +1 @@\n-a\n+b\n"
    assert code_renderer.detect_lexer(diff).name.lower() in {"rust", "rustlang"}
    assert isinstance(code_renderer.detect_lexer("#!/usr/bin/env bash\necho hi\n"), BashLexer)


def test_short_snippets_do_not_guess_language() -> None:
    assert isinstance(code_renderer.detect_lexer("x = 1"), TextLexer)
    assert isinstance(code_renderer.detect_lexer("def f():\n  return 1\n"), TextLexer)


def test_render_code_colors_python_keywords() -> None:
    assert code_renderer.theme is VSCodeDarkPlusStyle
    text = code_renderer.render_code("def foo():\n    return 1\n", filename="demo.py")
    assert text.plain.startswith("def")
    colors = {
        str(span.style.color).lower()
        for span in text.spans
        if span.style and span.style.color
    }
    # Dark+ keyword #569CD6, function #DCDCAA
    assert any("569cd6" in c or "86" in c for c in colors)
    assert any("dcdcaa" in c for c in colors)


def test_diff_highlighter_keeps_old_and_new_sides_independent() -> None:
    diff = """\
--- a/demo.py
+++ b/demo.py
@@ -1,2 +1,2 @@
 def foo():
-    return "unclosed
+    return "ok"
"""
    old, new, path = rebuild_diff_sides(diff)
    assert path == "demo.py"
    assert old[2] == '    return "unclosed'
    assert new[2] == '    return "ok"'
    highlighter = code_renderer.diff_from_unified(diff, path="demo.py")
    deleted = highlighter.style_body("delete", 2, old[2])
    added = highlighter.style_body("add", 2, new[2])
    assert deleted.plain == old[2]
    assert added.plain == new[2]
    # Both sides keep token colors; unclosed string must not wipe the add side.
    assert any(span.style and span.style.color for span in deleted.spans)
    assert any(span.style and span.style.color for span in added.spans)


def test_markdown_fences_use_code_renderer() -> None:
    console = Console(
        file=StringIO(), force_terminal=True, color_system="truecolor",
        width=40, no_color=False,
    )
    console.print(code_renderer.markdown("```python\ndef foo():\n    return 1\n```"))
    output = console.file.getvalue()
    assert "def" in output
    # Keyword #569CD6 as truecolor SGR
    assert "38;2;86;156;214" in output


def test_custom_theme_renderer_is_isolated() -> None:
    other = CodeRenderer(theme=MonokaiStyle)
    assert other.theme is MonokaiStyle
    assert code_renderer.theme is VSCodeDarkPlusStyle
    assert other.render_code("x = 1", language="python").plain == "x = 1"


def test_parse_numbered_change() -> None:
    assert parse_numbered_change("  12- hello") == ("  ", 12, "delete", "hello")
    assert parse_numbered_change("   3+") == ("   ", 3, "add", "")
    assert parse_numbered_change("   ?+ unknown") == ("   ", None, "add", "unknown")
    assert parse_numbered_change("plain") is None


def test_unknown_line_still_lexes_its_body() -> None:
    highlighter = code_renderer.diff_for_path("demo.py")
    styled = highlighter.style_body("add", 0, "def foo():")
    assert styled.plain == "def foo():"
    assert any(span.style and span.style.color for span in styled.spans)
