"""Session tool activity in fixed groups of three recorded completions."""

from collections import Counter
from collections.abc import Iterable

from prompt_toolkit.utils import get_cwidth

from agent.cli.formatting import _fit_footer_parts, _truncate_cells
from agent.companion.theme import ACCENT, MUTED, TEXT
from agent.mcp.names import resolve_mcp_display
from agent.redaction import sanitize_display_text


ACTIVITY_HEIGHT = 9
CALLS_PER_GROUP = 3
TOOL_ROWS = 3
ACTIVITY_CELLS = tuple((color, "■") for color in ("#FFFFFF", "#FFD5EC", "#F7BDD7", ACCENT))
FAILED_CELL = ("#FF5585 bold", "×")


class ActivityHeatmap:
    def __init__(self, companion):
        self.companion = companion
        self.cache_key = None
        self.rows = []

    def draw(self, width, height):
        state = self.companion.timeline
        key = (state, state.activity_version, width, height)
        if key != self.cache_key:
            self.rows = self._render(state.recent_tool_calls, width)[:height]
            self.cache_key = key
        return self.rows

    @staticmethod
    def _render(calls, width):
        if not calls:
            return [
                [(ACCENT, _truncate_cells("Tool Health", width))],
                [(MUTED, _truncate_cells("No tool calls yet", width))],
            ]

        total = len(calls)
        failed = sum(obs.outcome == "failed" for obs in calls)
        success = total - failed
        rate = f"{100 * success / total:.1f}".rstrip("0").rstrip(".")
        title, gap, percent = _fit_footer_parts("Tool Health", f"{rate}% success", width)
        name_width = max(1, min(12, width // 4))
        columns = max(1, (width - name_width - 1) // 2)
        groups = [calls[i:i + CALLS_PER_GROUP] for i in range(0, total, CALLS_PER_GROUP)][-columns:]
        matrix_width = name_width + 2 + 2 * len(groups) - 1
        margin = " " * max(0, (width - matrix_width) // 2)
        shown = sum(map(len, groups))
        calls_label = "call" if total == 1 else "calls"
        window_label = f"Last {total} {calls_label}"
        if shown < total:
            window_label += f" · {shown} shown"
        rows = [
            [(ACCENT, title), ("", gap), (ACCENT, percent)],
            [(MUTED, _truncate_cells(window_label, width))],
        ]

        frequency = Counter(obs.tool_name for obs in calls)
        top = sorted(frequency, key=lambda name: (-frequency[name], name))[:TOOL_ROWS]
        has_other = len(frequency) > len(top)
        labels = tool_health_labels(calls, name_width)
        counts = []
        for group in groups:
            buckets = [(obs.tool_name if obs.tool_name in top else None, obs.outcome) for obs in group]
            counts.append((
                Counter(name for name, _ in buckets),
                {name for name, outcome in buckets if outcome == "failed"},
            ))
        for name in [*top, None] if has_other else top:
            label = "other" if name is None else labels.get(name, name)
            if name == "other" and has_other:
                label = "other tool"
            if name is None:
                label = _truncate_cells(label, name_width)
            row = [(TEXT, margin + label + " " * (name_width - get_cwidth(label) + 2))]
            for i, (activity, failures) in enumerate(counts):
                if i:
                    row.append(("", " "))
                row.append(FAILED_CELL if name in failures else ACTIVITY_CELLS[activity[name]])
            rows.append(row)

        summary = f"{total} {calls_label} · {success} success · {failed} failed"
        if get_cwidth(summary) > width:
            summary = summary.replace(" · ", "·")
        rows.extend((_legend(width), [(MUTED, _truncate_cells(summary, width))]))
        return rows


def _display_text(value: str) -> str:
    return " ".join(sanitize_display_text(value, keep_newlines=False).split())


def _clean_identity(name: str, server: str, remote: str) -> tuple[str, str] | None:
    identity = resolve_mcp_display(name, server, remote)
    if identity is None:
        return None
    cleaned_server, cleaned_remote = _display_text(identity[0]), _display_text(identity[1])
    if not cleaned_server or not cleaned_remote:
        return None
    return cleaned_server, cleaned_remote


def _recorded_identity(name: str, calls) -> tuple[str, str] | None:
    server = remote = ""
    for obs in calls:
        if getattr(obs, "tool_name", None) != name:
            continue
        recorded_server = getattr(obs, "mcp_server", "") or ""
        recorded_remote = getattr(obs, "mcp_remote", "") or ""
        if recorded_server and recorded_remote:
            server, remote = recorded_server, recorded_remote
            break
    return _clean_identity(name, server, remote)


def _distinct_servers(entries: list[tuple[str, str]]) -> dict[str, str]:
    """Shortest unique server prefix. Identical prefixes gain a stable number."""
    prefixes: dict[str, str] = {}
    servers = [server for _, server in entries]
    for name, server in entries:
        prefix = server
        for length in range(1, len(server) + 1):
            candidate = server[:length]
            if sum(other[:length] == candidate for other in servers) == 1:
                prefix = candidate
                break
        prefixes[name] = prefix
    grouped: dict[str, list[str]] = {}
    for name, prefix in prefixes.items():
        grouped.setdefault(prefix, []).append(name)
    labels: dict[str, str] = {}
    for prefix, names in grouped.items():
        for index, name in enumerate(sorted(names)):
            labels[name] = prefix if index == 0 else f"{prefix}{index + 1}"
    return labels


def _fit_health_label(remote: str, suffix: str, width: int) -> str:
    """Keep the distinguishing suffix. Truncate the tool name when the row is narrow."""
    label = f"{remote}{suffix}" if suffix else remote
    if width <= 0:
        return ""
    if get_cwidth(label) <= width:
        return label
    if not suffix or get_cwidth(suffix) >= width:
        return _truncate_cells(label, width)
    return _truncate_cells(remote, width - get_cwidth(suffix)) + suffix


def tool_health_labels(calls: Iterable[object], width: int) -> dict[str, str]:
    """Row labels for Tool Health. Counts stay keyed by the registered name.

    An external MCP tool shows its remote name. A server abbreviation is added
    only when another tool in the same window would otherwise use that label.
    """
    calls = list(calls)
    names = {str(getattr(obs, "tool_name", "") or "") for obs in calls}
    names.discard("")
    parsed = {name: _recorded_identity(name, calls) for name in names}
    short = {name: identity[1] if identity else name for name, identity in parsed.items()}
    shared = {label for label, count in Counter(short.values()).items() if count > 1}
    colliding = [
        (name, parsed[name][0])
        for name, label in short.items()
        if parsed[name] is not None and label in shared
    ]
    servers = _distinct_servers(colliding)
    labels: dict[str, str] = {}
    for name, identity in parsed.items():
        remote = short[name]
        suffix = f" · {servers[name]}" if name in servers else ""
        labels[name] = _fit_health_label(remote, suffix, width)
    return labels


def _legend(width):
    colors = [(MUTED, "Less ")]
    for i, cell in enumerate(ACTIVITY_CELLS):
        if i:
            colors.append(("", " "))
        colors.append(cell)
    colors.append((MUTED, " More"))
    legend_width = sum(get_cwidth(text) for _, text in colors)
    gap = max(1, width - legend_width - get_cwidth("× Failed"))
    return [*colors, ("", " " * gap), FAILED_CELL, (MUTED, " Failed")]
