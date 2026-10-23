"""Keep generic embedded MCP resources visible to text-only model inputs."""
from __future__ import annotations

import base64
import binascii
from typing import Any


def preserve_generic_resources(result: Any) -> Any:
    """Convert opaque file blocks before the model's media filter drops them.

    MCPAdapter returns (content, artifact). Only generic embedded files change;
    native media, resource links, ordering, and structured artifacts stay intact.
    """
    if isinstance(result, tuple) and len(result) == 2:
        content, artifact = result
        return preserve_generic_resources(content), artifact
    if not isinstance(result, list):
        return result
    return [_generic_resource(block) for block in result]


def _generic_resource(block: Any) -> Any:
    if not isinstance(block, dict) or block.get("type") != "file":
        return block
    mime = block.get("mime_type")
    if not isinstance(mime, str) or mime.split(";", 1)[0].strip().lower() != "application/octet-stream":
        return block
    encoded = block.get("base64")
    if not isinstance(encoded, str):
        return block
    header = f"Embedded MCP resource ({mime})"
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        text = f"{header}\nInvalid Base64 received; original data preserved below:\n{encoded}"
    else:
        try:
            decoded = raw.decode("utf-8")
            if any((ord(c) < 32 and c not in "\t\n\r") or 127 <= ord(c) < 160 for c in decoded):
                raise ValueError("Non-text control bytes")
        except (UnicodeDecodeError, ValueError):
            text = f"{header}, {len(raw)} bytes\nEncoding: Base64 (binary data; decode before processing)\n{encoded}"
        else:
            text = f"{header}, {len(raw)} bytes\nEncoding: UTF-8\n{decoded}"
    return {"type": "text", "text": text, **({"id": block["id"]} if "id" in block else {})}
