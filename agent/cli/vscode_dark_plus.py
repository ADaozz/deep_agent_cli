"""Pygments style approximating VS Code Dark+ (dark_vs + dark_plus).

Colors taken from the upstream theme-defaults JSON on vscode main:
https://github.com/microsoft/vscode/blob/main/extensions/theme-defaults/themes/
"""

from __future__ import annotations

from pygments.style import Style
from pygments.token import (
    Comment,
    Error,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Text,
    Token,
    Whitespace,
)

# Editor chrome
_FG = "#D4D4D4"
_BG = "#1E1E1E"

# dark_vs.json
_COMMENT = "#6A9955"
_KEYWORD = "#569CD6"
_STRING = "#CE9178"
_NUMBER = "#B5CEA8"
_REGEX = "#D16969"
_ESCAPE = "#D7BA7D"
_INVALID = "#F44747"

# dark_plus.json overlays
_FUNCTION = "#DCDCAA"
_TYPE = "#4EC9B0"
_CONTROL = "#C586C0"
_VARIABLE = "#9CDCFE"
_CONSTANT = "#4FC1FF"


class VSCodeDarkPlusStyle(Style):
    """VS Code default Dark+ token colors mapped onto Pygments tokens."""

    name = "vscode-dark-plus"
    background_color = _BG
    highlight_color = "#264F78"
    line_number_color = _FG

    styles = {
        Token: _FG,
        Text: _FG,
        Whitespace: _FG,
        Punctuation: _FG,
        Operator: _FG,
        Operator.Word: _KEYWORD,
        Comment: _COMMENT,
        Comment.Hashbang: _COMMENT,
        Comment.Multiline: _COMMENT,
        Comment.Preproc: _KEYWORD,
        Comment.Special: _COMMENT,
        Keyword: _KEYWORD,
        Keyword.Constant: _KEYWORD,
        Keyword.Declaration: _KEYWORD,
        Keyword.Namespace: _CONTROL,
        Keyword.Pseudo: _KEYWORD,
        Keyword.Reserved: _CONTROL,
        Keyword.Type: _KEYWORD,
        Name: _VARIABLE,
        Name.Attribute: _VARIABLE,
        Name.Builtin: _KEYWORD,
        Name.Builtin.Pseudo: _KEYWORD,
        Name.Class: _TYPE,
        Name.Constant: _CONSTANT,
        Name.Decorator: _TYPE,
        Name.Entity: _ESCAPE,
        Name.Exception: _TYPE,
        Name.Function: _FUNCTION,
        Name.Function.Magic: _FUNCTION,
        Name.Label: "#C8C8C8",
        Name.Namespace: _TYPE,
        Name.Other: _VARIABLE,
        Name.Property: _VARIABLE,
        Name.Tag: _KEYWORD,
        Name.Variable: _VARIABLE,
        Name.Variable.Class: _VARIABLE,
        Name.Variable.Global: _VARIABLE,
        Name.Variable.Instance: _VARIABLE,
        Name.Variable.Magic: _KEYWORD,
        Number: _NUMBER,
        Number.Bin: _NUMBER,
        Number.Float: _NUMBER,
        Number.Hex: _NUMBER,
        Number.Integer: _NUMBER,
        Number.Integer.Long: _NUMBER,
        Number.Oct: _NUMBER,
        String: _STRING,
        String.Affix: _STRING,
        String.Backtick: _STRING,
        String.Char: _STRING,
        String.Delimiter: _STRING,
        String.Doc: _STRING,
        String.Double: _STRING,
        String.Escape: _ESCAPE,
        String.Heredoc: _STRING,
        String.Interpol: _KEYWORD,
        String.Other: _STRING,
        String.Regex: _REGEX,
        String.Single: _STRING,
        String.Symbol: _STRING,
        Generic.Deleted: _STRING,
        Generic.Emph: f"italic {_CONTROL}",
        Generic.Error: _INVALID,
        Generic.Heading: f"bold {_KEYWORD}",
        Generic.Inserted: _NUMBER,
        Generic.Output: _FG,
        Generic.Prompt: _KEYWORD,
        Generic.Strong: f"bold {_KEYWORD}",
        Generic.Subheading: f"bold {_KEYWORD}",
        Generic.Traceback: _INVALID,
        Error: _INVALID,
    }
