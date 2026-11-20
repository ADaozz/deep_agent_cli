"""Session tool activity in fixed groups of three recorded completions."""

from collections import Counter

from prompt_toolkit.utils import get_cwidth

from agent.cli.formatting import _fit_footer_parts, _truncate_cells
from agent.companion.theme import ACCENT, MUTED, TEXT


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
        counts = []
        for group in groups:
            buckets = [(obs.tool_name if obs.tool_name in top else None, obs.outcome) for obs in group]
            counts.append((
                Counter(name for name, _ in buckets),
                {name for name, outcome in buckets if outcome == "failed"},
            ))
        for name in [*top, None] if has_other else top:
            label = "other" if name is None else name
            if name == "other" and has_other:
                label = "other tool"
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
