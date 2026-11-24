"""Load required prompt files."""

from pathlib import Path


def read_prompt(path: Path) -> str:
    """Load one required prompt file. Missing or empty files are errors."""
    if not path.is_file():
        raise FileNotFoundError(f"Required prompt file is missing: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise FileNotFoundError(f"Required prompt file is empty: {path}")
    return text
