"""Cell-aware terminal status and footer formatting."""

from pathlib import Path

from prompt_toolkit.utils import get_cwidth


def format_workspace_path(path: Path) -> str:
    """Abbreviate the current user's home directory for display only."""
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~/" if relative == Path(".") else f"~/{relative}"


def format_context_window(window: int) -> str:
    """1000000 -> "1.0m", 200000 -> "200k"."""
    if window >= 1_000_000:
        return f"{window / 1_000_000:.1f}m"
    if window >= 1_000:
        return f"{round(window / 1_000)}k"
    return str(window)


def format_context_usage(usage: dict[str, int], window: int, *, empty_session: bool = False) -> str:
    """Right-hand footer label, empty when the profile declares no window."""
    if window <= 0:
        return ""
    label = f"{format_context_window(window)} Context"
    used = usage.get("input_tokens")
    if empty_session and not usage:
        used = 0
    if type(used) is not int or used < 0:
        return f"{label} · Unknown"
    percent = used / window * 100
    return f"{label} · {percent:.1f}% used"


def _fit_footer_line(left: str, right: str, width: int) -> str:
    available = max(1, width)
    if not right:
        left = _truncate_cells(left, available)
        return left + (" " * max(0, available - get_cwidth(left)))
    left_fit, gap, right_fit = _fit_footer_parts(left, right, width)
    return f"{left_fit}{gap}{right_fit}"


def _fit_footer_parts(left: str, right: str, width: int) -> tuple[str, str, str]:
    available = max(1, width)
    right_fit = _truncate_cells(right, available)
    remaining = max(0, available - get_cwidth(right_fit) - 1)
    left_fit = _truncate_cells(left, remaining)
    gap = " " * max(1, available - get_cwidth(left_fit) - get_cwidth(right_fit))
    return left_fit, gap, right_fit


def _truncate_cells(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if get_cwidth(value) <= width:
        return value
    if width == 1:
        return "…"
    result: list[str] = []
    used = 0
    for char in value:
        char_width = get_cwidth(char)
        if used + char_width > width - 1:
            break
        result.append(char)
        used += char_width
    return "".join(result) + "…"
