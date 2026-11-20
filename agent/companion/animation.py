"""Render the active GIF using the reference player's monochrome ASCII style."""
from pathlib import Path

from PIL import Image

from agent.companion import STATES
from agent.companion.theme import ACCENT
from agent.companion.ascii_art import PRESETS, convert, frame_iter

ASSETS = Path(__file__).with_name("assets")
LEVELS = {char: level for level, char in enumerate(PRESETS["standard"])}
# (row offset, column offset, Unicode Braille bit, ordered dither threshold)
DOTS = ((0, 0, 1, 1), (0, 1, 8, 6), (1, 0, 2, 7), (1, 1, 16, 3),
        (2, 0, 4, 2), (2, 1, 32, 5), (3, 0, 64, 8), (3, 1, 128, 4))
CELL_HEIGHT_TO_WIDTH = 2


def frames(state: str, width: int, height: int, *, path: Path | None = None):
    """Fit the GIF to the terminal, preserving its proportions and delays."""
    if state not in STATES:
        raise ValueError("unknown animation")
    with Image.open(path if path is not None else ASSETS / f"lain_{state}.gif") as clip:
        # A typical terminal cell is twice as tall as it is wide.
        columns_per_row = CELL_HEIGHT_TO_WIDTH * clip.width / clip.height
        cols = max(1, min(width, round(height * columns_per_row)))
        rows = max(1, min(height, round(cols / columns_per_row)))
        left = (width - cols) // 2
        top = (height - rows) // 2
        black = Image.new("RGBA", clip.size, "black")
        # Browser canvas uses local bilinear samples. Pillow.resize widens
        # its filter when shrinking, which softens the source's fine strokes.
        sample_width, sample_height = 2 * cols, 4 * rows
        transform = (clip.width / sample_width, 0, 0, 0, clip.height / sample_height, 0)
        for _, frame, delay in frame_iter(clip):
            flat = Image.alpha_composite(black, frame).convert("RGB")
            samples = flat.transform((sample_width, sample_height), Image.Transform.AFFINE, transform,
                                     resample=Image.Resampling.BILINEAR)
            portrait = convert(samples, width=sample_width, height=sample_height, aspect=False)["rows"]
            canvas = [" " * width for _ in range(height)]
            for y in range(rows):
                line = []
                for x in range(cols):
                    mask = sum(bit for dy, dx, bit, threshold in DOTS
                               if LEVELS[portrait[4*y + dy][2*x + dx]] >= threshold)
                    line.append(chr(0x2800 + mask) if mask else " ")
                canvas[top + y] = (" " * left + "".join(line)).ljust(width)
            yield [(ACCENT, "\n".join(canvas))], delay / 1000
