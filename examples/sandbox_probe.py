#!/usr/bin/env python3
"""Report actual sandbox support without weakening host policy or falling back."""
from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.config import SandboxConfig
from agent.sandbox import SandboxUnavailableError, select_backend


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-sandbox", action="store_true", help="Exit nonzero when the environment cannot provide isolation")
    parser.add_argument("--report", type=Path, help="Also write the JSON report to a file")
    args = parser.parse_args(argv)
    report = {"platform": platform.platform(), "python": platform.python_version(), "supported": False}
    with tempfile.TemporaryDirectory(prefix="deep-agent-probe-") as root:
        try:
            selected = select_backend(SandboxConfig(workspace=Path(root), allow_unsandboxed=False))
            result = selected.backend.execute("test \"$(pwd)\" = /workspace && printf isolated")
            supported = result.exit_code == 0 and result.output == "isolated"
            report.update(supported=supported, category="supported" if supported else "execution_failed", detail=result.output)
        except SandboxUnavailableError as exc:
            report.update(category=exc.kind, detail=str(exc))
    report["status"] = "SUPPORTED" if report["supported"] else "UNSUPPORTED SANDBOX ENVIRONMENT"
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.report:
        args.report.write_text(output + "\n", encoding="utf-8")
    return 2 if args.require_sandbox and not report["supported"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
