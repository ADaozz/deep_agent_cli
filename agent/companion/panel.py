"""Native prompt_toolkit layout for the auxiliary pane."""
from bisect import bisect_left
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import HSplit, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.utils import get_cwidth

from agent.cli.formatting import _truncate_cells

CYAN, GRAY, WHITE = "#00d7e6", "#777777", "#d0d0d0"
GREEN, YELLOW, BORDER = "#36e05c", "#f0c000", "#505050"


def geometry(columns: int, rows: int) -> tuple[int, int]:
    """Return the auxiliary width and animation height in terminal cells."""
    if columns < 107 or rows < 26:
        return 0, 0
    width = min(columns - 65, max(42, round((columns - 1) * 0.36)))
    height = min(rows - 20, round((rows - 2) * 0.38))
    return width, height


class Rows(UIControl):
    def __init__(self, draw, scroll=None):
        self.draw = draw
        self.scroll = scroll

    def create_content(self, width, height):
        try:
            lines = self.draw(width, height)
        except Exception:
            lines = [[(GRAY, "Unavailable")]]
        return UIContent(get_line=lambda i: lines[i], line_count=len(lines), show_cursor=False)

    def mouse_handler(self, event):
        if self.scroll and event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self.scroll(-1 if event.event_type == MouseEventType.SCROLL_UP else 1)
        return None


def rule():
    return Window(height=1, char="─", style=BORDER)


def box(body):
    def edge(char):
        return Window(width=1, char=char, style=BORDER)

    def horizontal(left, right):
        return VSplit([edge(left), rule(), edge(right)], height=1)

    return HSplit([horizontal("┌", "┐"), VSplit([edge("│"), Window(width=1), body, Window(width=1), edge("│")]), horizontal("└", "┘")])


def aligned(left, right, width):
    size = sum(get_cwidth(s) for _, s in left + right)
    if size > width:
        return left
    return [*left, ("", " " * (width - size)), *right]


class LogRows(Rows):
    """Keep the first visible wrapped line anchored across appends and eviction."""

    def __init__(self, companion):
        super().__init__(self.visible_lines, self.scroll_lines)
        self.companion = companion
        self.cache_key = None
        self.addresses = []
        self.lines = []
        self.top = self.maximum = 0

    def visible_lines(self, width, height):
        c = self.companion
        key = (c.log_sequence, width)
        if key != self.cache_key:
            self.cache_key = key
            self.addresses.clear()
            self.lines.clear()
            first = c.log_sequence - len(c.logs) + 1
            for sequence, (stamp, text) in enumerate(c.logs, first):
                rows = [[]]
                used = 0
                for color, part in [(GRAY, f"[{stamp}] "), (CYAN, "Lain: "), (WHITE, text)]:
                    chunk = ""
                    for char in part:
                        cells = get_cwidth(char)
                        if used and used + cells > width:
                            rows[-1].append((color, chunk))
                            rows.append([])
                            chunk, used = "", 0
                        chunk += char
                        used += cells
                    rows[-1].append((color, chunk))
                self.lines.extend(rows)
                self.addresses.extend((sequence, row) for row in range(len(rows)))
        self.maximum = max(0, len(self.lines) - height)
        self.top = self.maximum if c.anchor is None else min(self.maximum, bisect_left(self.addresses, c.anchor))
        return self.lines[self.top:self.top + height]

    def scroll_lines(self, direction):
        top = min(self.maximum, max(0, self.top + direction * 3))
        self.companion.anchor = None if top == self.maximum else self.addresses[top]
        self.companion.invalidate()


def build_panel(companion, git_summary):
    def status(w, h):
        return [[(CYAN, companion.behavior.upper())]]

    def resource(index, name, color):
        def draw(w, h):
            value = companion.metrics[index]
            capacity = max(0, w - 2)
            filled = 0 if value is None else min(capacity, max(0, round(capacity * value / 100)))
            bar = "[" + "█" * filled + "░" * (capacity - filled) + "]"
            percent = "N/A" if value is None else f"{value}%"
            return [aligned([(color, name)], [(WHITE, percent)], w), [(color, bar)]]
        return Window(Rows(draw))

    def git(w, h):
        g = git_summary()
        status_text = f"{g.changed} changed" if g.changed else "clean"
        status_text = g.error or (status_text if g.available else "—")
        lines = [[(CYAN, "git")]]
        for label, value, color in (
            ("Repo", "yes" if g.available else "—" if g.error else "no", GREEN),
            ("Branch", g.branch or "—", CYAN),
            ("Status", status_text, GRAY if g.error else GREEN),
        ):
            prefix = f"{label}: "
            lines.append([(WHITE, prefix), (color, _truncate_cells(value, w - len(prefix)))])
        return lines

    def service_rows(w, h):
        items = companion.services
        title = [(CYAN, "services")]
        count = [(GREEN, f"{len(items)} running" if items else "no")]
        if len("services ") + len(count[0][1]) <= w:
            lines = [aligned(title, count, w)]
        else:
            lines = [title, aligned([], count, w)]
        for row in items[:max(0, h - len(lines))]:
            pid = f"{'ns:' if row.sandbox else 'PID '}{row.pid}"
            label = _truncate_cells(row.name, w - len(pid) - 1)
            lines.append(aligned([(WHITE, label)], [(WHITE, pid)], w))
        return lines

    def separator():
        return Window(width=1, char="│", style=BORDER)

    title = FormattedText([(CYAN, "Companion Agent"), (WHITE, " [ Lain Iwakura ]")])
    content = HSplit([
        Window(FormattedTextControl(title), height=1), rule(),
        Window(FormattedTextControl(lambda: FormattedText(companion.frame)), height=lambda: companion.geometry()[1]),
        Window(height=1), Window(Rows(status), height=1, align=WindowAlign.CENTER), Window(height=1),
        VSplit([resource(0, "CPU", GREEN), separator(), resource(1, "GPU", GREEN),
                separator(), resource(2, "RAM", CYAN)], height=2, padding=1),
        Window(height=1),
        VSplit([box(Window(Rows(git))), box(Window(Rows(service_rows)))],
               height=lambda: 7 if companion.geometry()[1] >= 13 else 6, padding=2),
        Window(height=1),
        Window(FormattedTextControl(FormattedText([(CYAN, "lain.log")])), height=1),
        rule(), Window(LogRows(companion)),
    ])
    return VSplit([Window(width=2), content, Window(width=2)],
                  width=lambda: companion.geometry()[0] + 4, style="bg:#08090b #d0d0d0")
