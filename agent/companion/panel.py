"""Native prompt_toolkit layout for the auxiliary pane."""

from bisect import bisect_left
from datetime import datetime

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import HSplit, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.utils import get_cwidth

from agent.cli.formatting import _truncate_cells

from agent.companion.chart import ACTIVITY_HEIGHT
from agent.companion.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    LAIN_BUBBLE,
    MUTED,
    TEXT,
    USER_BUBBLE,
)
from agent.execution.environment import detect_runtime_environment, host_distribution


def geometry(columns: int, rows: int, *, editor_bottom_rows: int = 0) -> tuple[int, int]:
    """Return the auxiliary width and animation height in terminal cells."""
    if columns < 120 or rows < 32:
        return 0, 0
    width = round(columns * 0.28)
    capacity = rows - 25 - editor_bottom_rows
    height = min(capacity, round((rows - 2) * 0.36))
    if height >= 12:
        height = min(height, capacity - 2)
    # Leave room for the expanded service list, full heatmap, chat and editor padding.
    detail_rows = 3 if capacity >= 7 else 2
    return width, max(1, min(height - 1, capacity - detail_rows - 1))


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


def rule(style=BORDER):
    return Window(height=1, char="─", style=style)


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
        self.visible_addresses = []
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
                rows = self._message_rows(stamp, speaker, message, width)
                self.lines.extend(rows)
                self.addresses.extend((sequence, row) for row in range(len(rows)))
        lines, addresses = self.lines, self.addresses
        preview = c.chat_preview()
        if preview is not None:
            draft = self._message_rows(*preview, width)
            lines = lines + draft
            addresses = addresses + [(c.log_sequence + 1, row) for row in range(len(draft))]
        self.visible_addresses = addresses
        self.maximum = max(0, len(lines) - height)
        self.top = (
            self.maximum
            if c.anchor is None
            else min(self.maximum, bisect_left(addresses, c.anchor))
        )
        if not lines and height:
            return [[(MUTED, _truncate_cells("Watching current session...", width))]]
        return lines[self.top : self.top + height]

    @staticmethod
    def _message_rows(stamp, speaker, message, width):
        user = speaker == "You"
        name = {"You": "你", "Lain": "玲音", "Status": "提示"}.get(speaker, speaker)
        suffix = stamp if stamp.startswith("·") else f"· {stamp[:5]}"
        label = [(ACCENT if speaker in ("You", "Lain") else MUTED, f"{name} {suffix}")]
        rows = [aligned([], label, width) if user else label]
        if message is None:
            return rows

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
        size = max(get_cwidth(line) for line in text_rows) + 2
        indent = " " * max(0, width - size) if user else ""
        style = f"bg:{USER_BUBBLE if user else LAIN_BUBBLE} {TEXT}"
        for line in text_rows:
            padding = " " * (size - get_cwidth(line) - 1)
            rows.append([("", indent), (style, f" {line}{padding}")])
        return rows

    def scroll_lines(self, direction):
        top = min(self.maximum, max(0, self.top + direction * 3))
        self.companion.anchor = None if top == self.maximum else self.visible_addresses[top]
        self.companion.invalidate()


def build_panel(companion, git_summary, chart, editor, *, footer_rows):
    def status(w, h):
        return [[(ACCENT, companion.behavior.upper())]]

    def resource_width(index):
        # Three one-cell dividers, with one cell of padding on either side.
        width = max(0, companion.geometry()[0] - 9)
        base, remainder = divmod(width, 4)
        return base + (index < remainder)

    def workspace_left_width():
        # CPU, GPU, the first divider and the three spaces before the second divider.
        return resource_width(0) + resource_width(1) + 4

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

        return Window(Rows(draw), width=lambda: resource_width(index))

    def git(w, h):
        g = git_summary()
        detail = g.label().removeprefix("⎇ ") or "No repository"
        if g.available and get_cwidth(detail) > w:
            branch, _, state = detail.rpartition(" · ")
            room = w - get_cwidth(state) - 3
            detail = f"{_truncate_cells(branch, room)} · {state}" if room > 0 else state
        return [[(ACCENT, "GIT")], [(MUTED if g.error else TEXT, _truncate_cells(detail, w))]]

    def service_rows(w, h):
        items = companion.services
        lines = [[(ACCENT, "SERVICES")], [(TEXT, f"{len(items)} running")]]
        for row in items[:max(0, h - 2)]:
            pid = f"{'ns:' if row.sandbox else 'PID '}{row.pid}"
            ports = "".join(f" :{port}" for port in row.ports)
            if not row.sandbox and get_cwidth(row.name + " " + pid + ports) > w:
                pid = str(row.pid)
            detail = _truncate_cells(pid + ports, w - 2)
            label = _truncate_cells(row.name, w - get_cwidth(detail) - 1)
            lines.append(aligned([(TEXT, label)], [(MUTED, detail)], w))
        return lines

    def separator():
        return Window(width=1, char="│", style=BORDER)

    workspace = VSplit(
        [
            Window(Rows(git), width=workspace_left_width),
            separator(),
            Window(width=1),
            Window(Rows(service_rows)),
        ],
        height=lambda: 2 + min(len(companion.services), 3 if companion.geometry()[1] >= 5 else 2),
    )

    def chat_header(w, h):
        return [aligned([(ACCENT, "Talk TO Lain")], [(ACCENT, "Ctrl←/→")], w)]

    runtime = detect_runtime_environment()
    distribution = host_distribution()
    suffix = f" · {distribution}" if distribution else ""
    title = [(ACCENT, "Companion Agent"), (TEXT, " [ Lain Iwakura ]")]
    time_placeholder = "0000-00-00 00:00:00"
    full_header_width = (
        sum(get_cwidth(text) for _, text in title) + 1
        + get_cwidth(f"{time_placeholder} · {runtime}{suffix}")
    )

    def header_height():
        return 2 if distribution and companion.geometry()[0] < full_header_width else 1

    def header(w, h):
        now = datetime.now(companion.timezone).strftime("%Y-%m-%d %H:%M:%S")
        context = f"{now} · {runtime}"
        if w >= full_header_width:
            return [aligned(title, [(MUTED, context + suffix)], w)]
        rows = []
        for heading in (title, [(ACCENT, "Lain Iwakura")], [(ACCENT, "Lain")]):
            if sum(get_cwidth(text) for _, text in heading) + 1 + get_cwidth(context) <= w:
                rows.append(aligned(heading, [(MUTED, context)], w))
                break
        else:
            short_runtime = "容器" if runtime == "container" else runtime
            rows.append(aligned([(ACCENT, "Lain")], [(MUTED, f"{now}·{short_runtime}")], w))
        if distribution:
            rows.append(aligned([], [(MUTED, _truncate_cells(distribution, w))], w))
        return rows

    def heatmap_rows(w, h):
        return chart.draw(w, ACTIVITY_HEIGHT)

    def heatmap_height():
        return len(chart.draw(companion.geometry()[0], ACTIVITY_HEIGHT))

    content = HSplit(
        [
            Window(Rows(header), height=header_height),
            rule(ACCENT),
            Window(
                FormattedTextControl(lambda: FormattedText(companion.frame)),
                height=lambda: companion.geometry()[1],
            ),
            Window(Rows(status), height=1, align=WindowAlign.CENTER),
            rule(ACCENT),
            VSplit(
                [
                    resource(0, "CPU", ACCENT),
                    separator(),
                    resource(1, "GPU", ACCENT),
                    separator(),
                    resource(2, "RAM", ACCENT),
                    separator(),
                    resource(3, "DISK", ACCENT),
                ],
                height=2,
                padding=1,
            ),
            rule(ACCENT),
            workspace,
            rule(ACCENT),
            Window(Rows(heatmap_rows), height=heatmap_height),
            Window(height=1),
            Window(Rows(chat_header), height=1),
            Window(ChatRows(companion)),
            editor,
            Window(height=footer_rows),
        ]
    )
    return VSplit(
        [Window(width=2), content, Window(width=2)],
        width=lambda: companion.geometry()[0] + 4,
        style=f"bg:{BACKGROUND} {TEXT}",
    )
