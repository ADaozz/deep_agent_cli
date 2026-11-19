"""Rank current tool failure rates in terminal cells; F7/wheel pages the ranking."""

from math import ceil

from prompt_toolkit.utils import get_cwidth

from agent.cli.formatting import _truncate_cells
from agent.companion.theme import ACCENT, MUTED, TEXT

BLOCKS = " ▁▂▃▄▅▆▇█"


def centered(text, width):
    text = _truncate_cells(text, width)
    gap = width - get_cwidth(text)
    return " " * (gap // 2) + text + " " * (gap - gap // 2)


class FailureChart:
    def __init__(self, companion):
        self.companion = companion
        self.page = 0
        self.pages = 1
        self.cache_key = None
        self.rows = []

    def scroll(self, n):
        self.page = (self.page + n) % self.pages
        self.companion.invalidate()

    def draw(self, width, height):
        state = self.companion.timeline
        group = self.companion.profile()
        key = (state, state.plot_version, group, width, height, self.page)
        if key != self.cache_key:
            ranked = sorted(
                (
                    (name, series)
                    for (model, effort, name), series in state.series.items()
                    if (model, effort) == group and series.rate >= 15
                ),
                key=lambda item: (-item[1].rate, item[0]),
            )
            self.rows = self.render(ranked, width, height)
            self.cache_key = key
        return self.rows

    def render(self, ranked, width, height):
        if not ranked:
            self.page, self.pages = 0, 1
            return [[(MUTED, "No elevated failure rates")]]
        slot = min(width, max(12, 2 + max(get_cwidth(name) for name, _ in ranked)))
        per_page = max(1, width // max(1, slot))
        self.pages = ceil(len(ranked) / per_page)
        self.page %= self.pages
        visible = ranked[self.page * per_page : (self.page + 1) * per_page]
        page_hint = f"  {self.page + 1}/{self.pages} F7" if self.pages > 1 else ""
        rows = [[(MUTED, "Failure %" + page_hint)]]
        plot_height = max(1, height - 3)
        for y in reversed(range(plot_height)):
            line = []
            for _, series in visible:
                units = round(series.rate / 100 * plot_height * 8)
                block = BLOCKS[max(0, min(8, units - 8 * y))]
                line.append((ACCENT, centered(block * min(5, slot), slot)))
            rows.append(line)
        rows.append([(TEXT, centered(name, slot)) for name, _ in visible])
        rows.append(
            [
                (MUTED, centered(f"{s.rate:.0f}% n={len(s.recent)}", slot))
                for _, s in visible
            ]
        )
        return rows[:height]
