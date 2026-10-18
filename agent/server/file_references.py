"""Web path-reference validation; no file bytes or core Runner changes."""
import json
from pathlib import Path, PurePosixPath

RULES = json.loads(Path(__file__).with_name('file_reference_rules.json').read_text(encoding='utf-8'))


def is_image_path(value: str) -> bool:
    return PurePosixPath(value.strip().replace('\\', '/')).suffix.lower() in RULES['image_extensions']
