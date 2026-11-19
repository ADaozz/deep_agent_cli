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
REFERENCE_COLUMNS = 150
REFERENCE_ROWS = 86


def frames(state: str, width: int, height: int):
    """Fit the reference portrait grid to the terminal, preserving GIF delays."""
    if state not in STATES:
        raise ValueError("unknown animation")
    cols = max(1, min(width, round(height * REFERENCE_COLUMNS / REFERENCE_ROWS)))
    rows = max(1, min(height, round(cols * REFERENCE_ROWS / REFERENCE_COLUMNS)))
    left = (width - cols) // 2
    top = (height - rows) // 2
    with Image.open(ASSETS / f"lain_{state}.gif") as clip:
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
