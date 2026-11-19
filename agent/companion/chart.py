"""A time-aligned Braille chart. A page shows up to two tool series."""

from datetime import datetime
from math import ceil
from agent.cli.formatting import _truncate_cells
from agent.companion.theme import ACCENT, MUTED

COLORS = (ACCENT, "#B87898")
BITS = ((1, 8), (2, 16), (4, 32), (64, 128))


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
        group = self.companion.profile()
        state = self.companion.timeline
        key = (state, state.plot_version, group, width, height, self.page)
        if self.cache_key != key:
            self.rows = self.render(group, width, height)
            self.cache_key = key
        return self.rows

    def render(self, group, width, height):
        eligible = sorted(
            (key[2], s)
            for key, s in self.companion.timeline.series.items()
            if key[:2] == group and s.rate >= 15
        )
        if not eligible:
            return [[(MUTED, "No elevated failure rates")]]
        per_page = 2 if height >= 5 else 1
        self.pages = ceil(len(eligible) / per_page)
        self.page %= self.pages
        visible = eligible[self.page * per_page : (self.page + 1) * per_page]
        label = f"  {self.page + 1}/{self.pages} F7" if self.pages > 1 else ""
        rows = [[(MUTED, "Failure %" + label)]]
        for j, (name, s) in enumerate(visible):
            rows.append(
                [
                    (
                        COLORS[j],
                        _truncate_cells(
                            f"{'━' if not j else '┄'} {name} {s.rate:.0f}% n={len(s.recent)}",
                            width,
                        ),
                    )
                ]
            )
        show_axis = height >= 6
        lines = max(0, height - len(rows) - int(show_axis))
        if not lines or width <= 0:
            return rows[:height]
        stamps = [t for _, series in eligible for t, _ in series.points]
        start, end = min(stamps), max(stamps)
        grid = [[(0, 0) for _ in range(width)] for _ in range(lines)]
        for j, (_, s) in enumerate(visible):
            previous = None
            for t, v in s.points:
                x = round((t - start) / max(0.000001, end - start) * (width * 2 - 1))
                y = round((100 - v) / 100 * (lines * 4 - 1))
                x0, y0 = previous or (x, y)
                steps = max(1, abs(x - x0), abs(y - y0))
                for i in range(steps + 1):
                    px, py = (
                        x0 + round((x - x0) * i / steps),
                        y0 + round((y - y0) * i / steps),
                    )
                    if j and px % 5 == 3 and i != steps:
                        continue
                    mask, _ = grid[py // 4][px // 2]
                    grid[py // 4][px // 2] = mask | BITS[py % 4][px % 2], j
                previous = x, y
        rows.extend(
            [
                [(COLORS[j], chr(0x2800 + mask) if mask else " ") for mask, j in row]
                for row in grid
            ]
        )
        if show_axis:
            left, right = [
                datetime.fromtimestamp(t, self.companion.timezone).strftime("%H:%M:%S")
                for t in (start, end)
            ]
            rows.append([(MUTED, left + " " * max(1, width - 16) + right)])
        return rows
