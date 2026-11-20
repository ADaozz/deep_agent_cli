"""Compare one bundled GIF frame at 28 × 16 cells. Run with the project installed."""

from itertools import zip_longest
import argparse
import json
import os
import re
import statistics
import sys
import time
import subprocess

from agent.companion.animation import ASSETS, frames
from agent.companion.theme import ACCENT


def compare():
    gif = ASSETS / "lain_idle.gif"
    portrait, _ = next(frames("idle", 28, 16))
    r, g, b = bytes.fromhex(ACCENT[1:])
    pink = f"\x1b[38;2;{r};{g};{b}m"
    samples = {
        "Braille": [pink + line + "\x1b[0m" for line in portrait[0][1].splitlines()]
    }
    for mode in ("half", "quarter"):
        result = subprocess.run(
            ["timg", f"--pixelation={mode}", "-g28x16", "--frames=1", str(gif)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # timg owns the cursor in standalone mode. The comparison prints static rows.
        samples[mode] = (
            result.stdout.replace("\x1b[?25l", "")
            .replace("\x1b[?25h", "")
            .rstrip()
            .splitlines()
        )
    print("   ".join(f"{mode:28}" for mode in samples))
    for row in zip_longest(*samples.values(), fillvalue=" " * 28):
        print("\x1b[0m   ".join(row) + "\x1b[0m")


def play_braille(width, height):
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output import ColorDepth
    from prompt_toolkit.output.vt100 import Vt100_Output

    frame = []
    output = Vt100_Output(
        sys.stdout,
        lambda: Size(height, width),
        default_color_depth=ColorDepth.DEPTH_24_BIT,
    )
    layout = Layout(Window(FormattedTextControl(lambda: FormattedText(frame))))
    app = Application(
        layout=layout, input=DummyInput(), output=output, full_screen=True
    )
    with set_app(app):
        for frame, delay in frames("idle", width, height):
            app.render_counter += 1
            app.renderer.render(app, layout)
            time.sleep(delay)


def measure(command, marker):
    # wait4 reports this renderer's CPU and peak RSS including process startup.
    start = time.monotonic()
    with subprocess.Popen(command, stdout=subprocess.PIPE) as child:
        data = bytearray()
        arrivals = []
        while block := os.read(child.stdout.fileno(), 65536):
            now = time.monotonic()
            data.extend(block)
            arrivals.extend([now] * block.count(marker))
        _, status, usage = os.wait4(child.pid, 0)
        child.returncode = os.waitstatus_to_exitcode(status)
        if child.returncode:
            raise RuntimeError(f"Renderer exited with {child.returncode}")
    intervals = sorted(round((b - a) * 1000, 1) for a, b in zip(arrivals, arrivals[1:]))
    plain = re.sub(rb"\x1b\[[?;0-9]*[A-Za-z]", b"", data).decode("utf8")
    return dict(
        wall_s=round(time.monotonic() - start, 3),
        cpu_s=round(usage.ru_utime + usage.ru_stime, 3),
        peak_mib=round(usage.ru_maxrss / 1024, 1),
        bytes=len(data),
        printed_cells=sum(c.isprintable() for c in plain),
        intervals=len(intervals),
        median_ms=statistics.median(intervals) if intervals else None,
        p95_ms=intervals[int(0.95 * len(intervals))] if intervals else None,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--braille", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--width", type=int, default=28)
    parser.add_argument("--height", type=int, default=16)
    args = parser.parse_args()
    if args.braille:
        play_braille(args.width, args.height)
    elif args.benchmark:
        geometry = f"{args.width}x{args.height}"
        result = {
            "Braille": measure(
                [
                    sys.executable,
                    __file__,
                    "--braille",
                    "--width",
                    str(args.width),
                    "--height",
                    str(args.height),
                ],
                b"\x1b[?25l",
            )
        }
        for mode in ("half", "quarter"):
            result[mode] = measure(
                [
                    "timg",
                    f"--pixelation={mode}",
                    "-g" + geometry,
                    "--loops=1",
                    str(ASSETS / "lain_idle.gif"),
                ],
                f"\x1b[{args.height}A".encode(),
            )
        print(json.dumps(result, indent=2))
    else:
        compare()
