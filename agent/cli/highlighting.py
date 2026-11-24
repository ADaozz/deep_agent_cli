"""Independent CodeRenderer: one lexer + theme for fences, tool output, and diffs.

Language detection prefers path / explicit tags over content guessing.
Diff sides are lexed as separate blobs so multiline tokens stay correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import ClassVar, Literal

from pygments.lexer import Lexer
from pygments.lexers import (
    TextLexer,
    get_lexer_by_name,
    get_lexer_for_filename,
    guess_lexer,
)
from pygments.util import ClassNotFound
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Markdown
from rich.padding import Padding
from pygments.style import Style as PygmentsStyle
from rich.style import Style
from rich.syntax import PygmentsSyntaxTheme, Syntax
from rich.text import Text

from agent.cli.vscode_dark_plus import VSCodeDarkPlusStyle

# Default theme: VS Code Dark+; _fg_style strips token backgrounds for diff overlays.
DEFAULT_CODE_THEME: type[PygmentsStyle] = VSCodeDarkPlusStyle

# Short tool snippets mis-guess often; prefer plain text below this size.
_GUESS_MIN_CHARS = 120
_GUESS_MIN_LINES = 3

_DIFF_PATH = re.compile(r"^(?:\+\+\+|---) [ab]/(.+)$")
_SHEBANG = re.compile(r"^#!\s*(?:/usr/bin/env\s+)?(\S+)")
_NUMBERED_CHANGE = re.compile(
    r"^(?P<indent> *)(?P<number>\d+)(?P<mark>[-+])(?: (?P<body>.*))?$"
)

DiffSide = Literal["old", "new"]
DiffKind = Literal["add", "delete", "context"]


def _path_from_diff_header(code: str) -> str | None:
    for line in code.splitlines()[:8]:
        match = _DIFF_PATH.match(line)
        if match is None:
            continue
        path = match.group(1).strip()
        if path and path != "/dev/null":
            return path
    return None


def _shebang_language(code: str) -> str | None:
    first = code.lstrip("\ufeff").splitlines()[:1]
    if not first:
        return None
    match = _SHEBANG.match(first[0])
    if match is None:
        return None
    program = PurePosixPath(match.group(1)).name.lower()
    aliases = {
        "python": "python", "python2": "python", "python3": "python",
        "bash": "bash", "sh": "bash", "zsh": "bash", "dash": "bash",
        "node": "javascript", "nodejs": "javascript",
        "ruby": "ruby", "perl": "perl", "lua": "lua",
        "php": "php", "r": "r", "ts-node": "typescript",
    }
    return aliases.get(program)


def rebuild_diff_sides(full_diff: str) -> tuple[dict[int, str], dict[int, str], str | None]:
    """Rebuild old/new line maps and a path hint from a unified diff."""
    old: dict[int, str] = {}
    new: dict[int, str] = {}
    path: str | None = _path_from_diff_header(full_diff)
    old_number = new_number = 0
    seen_hunk = False
    for line in full_diff.splitlines():
        if line.startswith("@@"):
            seen_hunk = True
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_number, new_number = map(int, match.groups())
                if old_number == 0:
                    old_number = 1
            continue
        if line.startswith(("---", "+++")):
            continue
        if not seen_hunk:
            continue
        if line.startswith("-"):
            old[old_number] = line[1:]
            old_number += 1
        elif line.startswith("+"):
            new[new_number] = line[1:]
            new_number += 1
        elif line.startswith(" "):
            old[old_number] = line[1:]
            new[new_number] = line[1:]
            old_number += 1
            new_number += 1
    return old, new, path


def parse_numbered_change(content: str) -> tuple[str, int, DiffKind, str] | None:
    """Parse `  12- body` / `  12+ body` into indent, number, kind, body."""
    match = _NUMBERED_CHANGE.match(content)
    if match is None:
        return None
    kind: DiffKind = "delete" if match.group("mark") == "-" else "add"
    body = match.group("body") or ""
    return match.group("indent"), int(match.group("number")), kind, body


def lexer_alias(lexer: Lexer) -> str | None:
    """Stable short name for ToolPreview.syntax; None means plain text."""
    if isinstance(lexer, TextLexer):
        return None
    aliases = getattr(lexer, "aliases", None) or ()
    return aliases[0] if aliases else lexer.name


@dataclass(frozen=True)
class CodeRenderer:
    """Single entry for code fences, tool output, and diff body highlighting."""

    theme: type[PygmentsStyle] | str = DEFAULT_CODE_THEME

    def _theme(self):
        if isinstance(self.theme, type) and issubclass(self.theme, PygmentsStyle):
            return PygmentsSyntaxTheme(self.theme)
        return Syntax.get_theme(self.theme)

    def _fg_style(self, token_type) -> Style:
        """Foreground-only style so callers can paint their own backgrounds."""
        raw = self._theme().get_style_for_token(token_type)
        return Style(
            color=raw.color,
            bold=raw.bold,
            italic=raw.italic,
            underline=raw.underline,
            strike=raw.strike,
            blink=raw.blink,
            dim=raw.dim,
        )

    def detect_lexer(
        self,
        code: str = "",
        *,
        filename: str | None = None,
        language: str | None = None,
    ) -> Lexer:
        """Resolve a lexer: language → filename → diff path → shebang → cautious guess → text."""
        if language and language not in {"", "text", "plain"}:
            try:
                return get_lexer_by_name(language, stripnl=False)
            except ClassNotFound:
                pass

        path = filename
        if path:
            bare = path.split(":", 1)[0]
            try:
                return get_lexer_for_filename(bare, code, stripnl=False)
            except ClassNotFound:
                pass

        diff_path = _path_from_diff_header(code) if code else None
        if diff_path:
            try:
                return get_lexer_for_filename(diff_path, code, stripnl=False)
            except ClassNotFound:
                pass

        shebang = _shebang_language(code) if code else None
        if shebang:
            try:
                return get_lexer_by_name(shebang, stripnl=False)
            except ClassNotFound:
                pass

        if (
            code
            and len(code) >= _GUESS_MIN_CHARS
            and code.count("\n") + 1 >= _GUESS_MIN_LINES
        ):
            try:
                return guess_lexer(code)
            except ClassNotFound:
                pass

        return TextLexer(stripnl=False)

    def syntax_for_path(self, path: str | None) -> str | None:
        """Lexer alias from a file path alone (no content guessing)."""
        if not path:
            return None
        return lexer_alias(self.detect_lexer(filename=path))

    def render_code(
        self,
        code: str,
        *,
        filename: str | None = None,
        language: str | None = None,
    ) -> Text:
        """Highlight a code blob into a Rich Text (no forced background)."""
        lexer = self.detect_lexer(code, filename=filename, language=language)
        text = Text()
        if isinstance(lexer, TextLexer):
            text.append(code)
            return text
        for token_type, value in lexer.get_tokens(code):
            text.append(value, self._fg_style(token_type))
        if text.plain.endswith("\n") and not code.endswith("\n"):
            text = text[:-1]
        return text

    def _line_map_from_blob(
        self,
        blob: str,
        line_numbers: list[int],
        *,
        filename: str | None = None,
        language: str | None = None,
    ) -> dict[int, Text]:
        if not line_numbers:
            return {}
        styled = self.render_code(blob, filename=filename, language=language) if blob else Text()
        parts = list(styled.split("\n"))
        return {number: part for number, part in zip(line_numbers, parts)}

    def diff_from_unified(
        self,
        full_diff: str,
        *,
        path: str | None = None,
        language: str | None = None,
    ) -> DiffHighlighter:
        return DiffHighlighter.from_unified(
            full_diff, path=path, language=language, renderer=self,
        )

    def diff_for_path(
        self,
        path: str | None,
        *,
        language: str | None = None,
    ) -> DiffHighlighter:
        return DiffHighlighter.for_path(path, language=language, renderer=self)

    def markdown(self, markup: str, **kwargs) -> Markdown:
        """Markdown whose fenced code blocks use this renderer (not Rich Syntax)."""
        # code_theme is unused for coloring (fences go through render_code); keep a
        # string for Rich Markdown compatibility when callers pass code_theme=.
        if "code_theme" not in kwargs:
            kwargs["code_theme"] = getattr(self.theme, "name", None) or "vscode-dark-plus"
        return HighlightMarkdown(markup, renderer=self, **kwargs)


# Module singleton — tool cards, diffs, and Markdown all share this instance.
code_renderer = CodeRenderer()

# Backward-compatible aliases used by tests and older call sites.
CODE_THEME = DEFAULT_CODE_THEME


def detect_lexer(
    code: str = "",
    *,
    filename: str | None = None,
    language: str | None = None,
) -> Lexer:
    return code_renderer.detect_lexer(code, filename=filename, language=language)


def syntax_for_path(path: str | None) -> str | None:
    return code_renderer.syntax_for_path(path)


def highlight_code(
    code: str,
    *,
    filename: str | None = None,
    language: str | None = None,
) -> Text:
    return code_renderer.render_code(code, filename=filename, language=language)


@dataclass
class DiffHighlighter:
    """Per-side syntax styles for unified-diff rendering."""

    path: str | None = None
    language: str | None = None
    renderer: CodeRenderer = field(default_factory=lambda: code_renderer)
    _old_styles: dict[int, Text] = field(default_factory=dict)
    _new_styles: dict[int, Text] = field(default_factory=dict)
    _lexer_alias: str | None = None

    @classmethod
    def from_unified(
        cls,
        full_diff: str,
        *,
        path: str | None = None,
        language: str | None = None,
        renderer: CodeRenderer | None = None,
    ) -> DiffHighlighter:
        active = renderer or code_renderer
        old_lines, new_lines, header_path = rebuild_diff_sides(full_diff)
        resolved = path or header_path
        highlighter = cls(path=resolved, language=language, renderer=active)
        lexer = active.detect_lexer(
            "\n".join([*old_lines.values(), *new_lines.values()]),
            filename=resolved,
            language=language,
        )
        highlighter._lexer_alias = lexer_alias(lexer)
        if isinstance(lexer, TextLexer):
            return highlighter

        lang = language or highlighter._lexer_alias
        if old_lines:
            numbers = sorted(old_lines)
            blob = "\n".join(old_lines[n] for n in numbers)
            highlighter._old_styles = active._line_map_from_blob(
                blob, numbers, filename=resolved, language=lang,
            )
        if new_lines:
            numbers = sorted(new_lines)
            blob = "\n".join(new_lines[n] for n in numbers)
            highlighter._new_styles = active._line_map_from_blob(
                blob, numbers, filename=resolved, language=lang,
            )
        return highlighter

    @classmethod
    def for_path(
        cls,
        path: str | None,
        *,
        language: str | None = None,
        renderer: CodeRenderer | None = None,
    ) -> DiffHighlighter:
        """Path-only highlighter: styles individual bodies without cross-line state."""
        active = renderer or code_renderer
        alias = language or active.syntax_for_path(path)
        return cls(path=path, language=alias, renderer=active, _lexer_alias=alias)

    def style_body(self, kind: DiffKind, number: int, body: str) -> Text:
        """Return a styled copy of the line body (no line number or +/- marker)."""
        side: DiffSide = "old" if kind == "delete" else "new"
        cached = (self._old_styles if side == "old" else self._new_styles).get(number)
        if cached is not None and cached.plain == body:
            return cached.copy()
        if cached is not None and not body:
            return Text()
        lang = self._lexer_alias or self.language
        return self.renderer.render_code(body, filename=self.path, language=lang)


class _RendererCodeBlock(CodeBlock):
    """Fenced block that highlights through CodeRenderer instead of Rich Syntax."""

    renderer: ClassVar[CodeRenderer] = code_renderer

    def __rich_console__(
        self, console: Console, options: ConsoleOptions,
    ) -> RenderResult:
        code = str(self.text).rstrip()
        language = None if self.lexer_name in {"", "text", "plain"} else self.lexer_name
        text = self.renderer.render_code(code, language=language)
        yield Padding(text, 1)


class HighlightMarkdown(Markdown):
    """Markdown that routes fence/code_block elements through CodeRenderer."""

    def __init__(
        self,
        markup: str,
        *,
        renderer: CodeRenderer | None = None,
        **kwargs,
    ) -> None:
        active = renderer or code_renderer
        kwargs.setdefault(
            "code_theme",
            getattr(active.theme, "name", None) or "vscode-dark-plus",
        )
        super().__init__(markup, **kwargs)
        block = type(
            "_BoundCodeBlock",
            (_RendererCodeBlock,),
            {"renderer": active},
        )
        self.elements = {**self.elements, "fence": block, "code_block": block}
