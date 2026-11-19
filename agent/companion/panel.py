"""Native prompt_toolkit layout for the auxiliary pane."""

from bisect import bisect_left
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import HSplit, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.utils import get_cwidth

from agent.cli.formatting import _truncate_cells

from agent.companion.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    LAIN_BUBBLE,
    MUTED,
    TEXT,
    USER_BUBBLE,
)


def geometry(columns: int, rows: int) -> tuple[int, int]:
    """Return the auxiliary width and animation height in terminal cells."""
    if columns < 120 or rows < 32:
        return 0, 0
    width = round(columns * 0.28)
    height = min(rows - 25, round((rows - 2) * 0.36))
    return width, height


class Rows(UIControl):
    def __init__(self, draw, scroll=None):
        self.draw = draw
        self.scroll = scroll

    def create_content(self, width, height):
        try:
            lines = self.draw(width, height)
        except Exception:
            lines = [[(MUTED, "Unavailable")]]
        return UIContent(
            get_line=lambda i: lines[i], line_count=len(lines), show_cursor=False
        )

    def mouse_handler(self, event):
        if self.scroll and event.event_type in (
            MouseEventType.SCROLL_UP,
            MouseEventType.SCROLL_DOWN,
        ):
            self.scroll(-1 if event.event_type == MouseEventType.SCROLL_UP else 1)
        return None


def rule():
    return Window(height=1, char="─", style=BORDER)


def box(body):
    def edge(char):
        return Window(width=1, char=char, style=BORDER)

    def horizontal(left, right):
        return VSplit([edge(left), rule(), edge(right)], height=1)

    return HSplit(
        [
            horizontal("┌", "┐"),
            VSplit([edge("│"), Window(width=1), body, Window(width=1), edge("│")]),
            horizontal("└", "┘"),
        ]
    )


def aligned(left, right, width):
    size = sum(get_cwidth(s) for _, s in left + right)
    if size > width:
        return left
    return [*left, ("", " " * (width - size)), *right]


class ChatRows(Rows):
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
            for sequence, (stamp, speaker, message) in enumerate(c.logs, first):
                limit = max(2, width - 6)
                text_rows = []
                line, used = "", 0
                for char in message:
                    cells = get_cwidth(char)
                    if char == "\n":
                        text_rows.append(line.rstrip())
                        line, used = "", 0
                        continue
                    if used + cells > limit:
                        head, space, tail = line.rpartition(" ")
                        if space and head:
                            text_rows.append(head)
                            line, used = tail, get_cwidth(tail)
                        else:
                            text_rows.append(line)
                            line, used = "", 0
                    if line or char != " ":
                        line += char
                        used += cells
                text_rows.append(line)
                user = speaker == "You"
                size = max(get_cwidth(line) for line in text_rows) + 2
                indent = " " * max(0, width - size) if user else ""
                name = {"You": "你", "Lain": "玲音", "Status": "提示"}.get(
                    speaker, speaker
                )
                label = [(MUTED, f"{name} · {stamp[:5]}")]
                rows = [aligned([], label, width) if user else label]
                style = f"bg:{USER_BUBBLE if user else LAIN_BUBBLE} {TEXT}"
                for line in text_rows:
                    padding = " " * (size - get_cwidth(line) - 1)
                    rows.append([("", indent), (style, f" {line}{padding}")])
                self.lines.extend(rows)
                self.addresses.extend((sequence, row) for row in range(len(rows)))
        self.maximum = max(0, len(self.lines) - height)
        self.top = (
            self.maximum
            if c.anchor is None
            else min(self.maximum, bisect_left(self.addresses, c.anchor))
        )
        return self.lines[self.top : self.top + height]

    def scroll_lines(self, direction):
        top = min(self.maximum, max(0, self.top + direction * 3))
        self.companion.anchor = None if top == self.maximum else self.addresses[top]
        self.companion.invalidate()


def build_panel(companion, git_summary, chart, editor):
    def status(w, h):
        return [[(ACCENT, companion.behavior.upper())]]

    def resource(index, name, color):
        def draw(w, h):
            value = companion.metrics[index]
            capacity = max(0, w - 2)
            filled = (
                0
                if value is None
                else min(capacity, max(0, round(capacity * value / 100)))
            )
            bar = "[" + "█" * filled + "░" * (capacity - filled) + "]"
            percent = "N/A" if value is None else f"{value}%"
            if w < 8:
                return [[(color, name)], [(TEXT, percent)]]
            return [aligned([(color, name)], [(TEXT, percent)], w), [(color, bar)]]

        return Window(Rows(draw))

    def git(w, h):
        g = git_summary()
        status_text = f"{g.changed} changed" if g.changed else "clean"
        status_text = g.error or (status_text if g.available else "—")
        lines = [[(ACCENT, "git")]]
        for label, value, color in (
            ("Repo", "yes" if g.available else "—" if g.error else "no", ACCENT),
            ("Branch", g.branch or "—", ACCENT),
            ("Status", status_text, MUTED if g.error else ACCENT),
        ):
            prefix = f"{label}: "
            lines.append(
                [(TEXT, prefix), (color, _truncate_cells(value, w - len(prefix)))]
            )
        return lines

    def service_rows(w, h):
        items = companion.services
        title = [(ACCENT, "services")]
        count = [(ACCENT, f"{len(items)} running" if items else "no")]
        if len("services ") + len(count[0][1]) <= w:
            lines = [aligned(title, count, w)]
        else:
            lines = [title, aligned([], count, w)]
        for row in items[: max(0, h - len(lines))]:
            pid = f"{'ns:' if row.sandbox else 'PID '}{row.pid}"
            label = _truncate_cells(row.name, w - len(pid) - 1)
            lines.append(aligned([(TEXT, label)], [(TEXT, pid)], w))
        return lines

    def separator():
        return Window(width=1, char="│", style=BORDER)

    cards = VSplit(
        [box(Window(Rows(git))), box(Window(Rows(service_rows)))],
        height=lambda: 7 if companion.geometry()[1] >= 13 else 6,
        padding=2,
    )

    def chat_header(w, h):
        hint = "在看…" if companion.question else "F6 · 提问"
        return [aligned([(ACCENT, "对话")], [(MUTED, hint)], w)]

    title = FormattedText([(ACCENT, "Companion Agent"), (TEXT, " [ Lain Iwakura ]")])
    content = HSplit(
        [
            Window(
                FormattedTextControl(
                    lambda: (
                        title
                        if companion.geometry()[0] >= 31
                        else FormattedText([(ACCENT, "Lain Iwakura")])
                    )
                ),
                height=1,
            ),
            rule(),
            Window(
                FormattedTextControl(lambda: FormattedText(companion.frame)),
                height=lambda: companion.geometry()[1],
            ),
            Window(height=1),
            Window(Rows(status), height=1, align=WindowAlign.CENTER),
            Window(height=1),
            VSplit(
                [
                    resource(0, "CPU", ACCENT),
                    separator(),
                    resource(1, "GPU", ACCENT),
                    separator(),
                    resource(2, "RAM", ACCENT),
                    separator(),
                    resource(3, "ROM", ACCENT),
                ],
                height=2,
                padding=1,
            ),
            Window(height=1),
            cards,
            Window(
                Rows(chart.draw, chart.scroll),
                height=lambda: 6 if companion.geometry()[1] >= 12 else 4,
            ),
            Window(height=1),
            Window(Rows(chat_header), height=1),
            Window(ChatRows(companion)),
            Window(editor, height=1, style=f"bg:{USER_BUBBLE} {TEXT}"),
        ]
    )
    return VSplit(
        [Window(width=2), content, Window(width=2)],
        width=lambda: companion.geometry()[0] + 4,
        style=f"bg:{BACKGROUND} {TEXT}",
    )
