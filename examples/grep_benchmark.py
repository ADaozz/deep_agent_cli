#!/usr/bin/env python3
"""Reproducible grep benchmark for the G01-G04 synthetic workspaces.

  python examples/grep_benchmark.py generate --root /tmp/grep-bench
  python examples/grep_benchmark.py run --root /tmp/grep-bench [--baseline-source DIR]

Each dataset has exactly N files: N-2 Python sources, .gitignore and .git/HEAD
(so ripgrep treats it as a repository). One file in the last directory holds
the rare marker, forcing a full traversal; every tenth file holds "needle".

Engines: ``new`` (this checkout, SANDBOXED bwrap + one rg), ``new-python``
(this checkout, UNSANDBOXED descriptor-confined Python engine), ``baseline``
(another checkout, e.g. a worktree of the previous PR #5 commit), ``rg``
(direct host rg) and ``main`` (Deep Agents FilesystemBackend.grep, the backend
used on main). Every measurement is the median of ``--repeat`` runs.

``rg_starts`` comes from strace of one extra run. Tracing slows the old
per-batch rg enough that a 40k-file baseline search hits the 10 second limit,
so that count is only complete when the traced result itself is not a timeout.
The batching rule verified on the completed sets is one probe plus one rg per
64 files (201 files → 5, 4001 → 64, 40001 → 627).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
DATASETS = {"G01": 201, "G02": 4001, "G03": 40001, "G04": 120000}
RARE = "rare_marker_7f3a"
QUERIES = {
    "rare/files": dict(pattern=RARE, output_mode="files_with_matches"),
    "needle/count": dict(pattern="needle", output_mode="count", max_count=1_000_000),
    "needle/content": dict(pattern="needle", output_mode="content"),
}


def generate(root: Path) -> None:
    for name, total in DATASETS.items():
        target = root / name
        marker = root / f"{name}.complete"
        if marker.exists():
            continue
        shutil.rmtree(target, ignore_errors=True)
        sources = total - 2
        for index in range(sources):
            directory = target / f"pkg{index // 1000:03d}" / f"mod{(index // 100) % 10}"
            directory.mkdir(parents=True, exist_ok=True)
            body = f"import os\n\n\ndef func_{index}(value):\n    return value + {index}\n"
            if index % 10 == 0:
                body += "# needle: synthetic match\n"
            if index == sources - 1:
                body += f"# {RARE}\n"
            (directory / f"f{index}.py").write_text(body)
        (target / ".gitignore").write_text("build/\n*.log\n")
        (target / ".git").mkdir()
        (target / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        marker.write_text(str(total))


def _backend(kind: str, workspace: Path):
    from agent.config import SandboxConfig
    from agent.sandbox import BubblewrapBackend, UnsandboxedShellBackend, WorkspaceCompositeBackend

    if kind == "python":
        executor = UnsandboxedShellBackend(SandboxConfig(workspace=workspace))
    else:
        executor = BubblewrapBackend(SandboxConfig(workspace=workspace), executable=shutil.which("bwrap"))
    return WorkspaceCompositeBackend(executor, skills_dir=None)


def _measure_once(engine: str, workspace: Path, query: dict) -> tuple[float, float, float, str]:
    """Return wall seconds, supervisor CPU, child CPU and the result's status line."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = time.process_time()
    start = time.perf_counter()
    if engine == "rg":
        argv = [shutil.which("rg"), "--no-config", "-c" if query["output_mode"] == "count" else "-l",
                query["pattern"], str(workspace)]
        result = subprocess.run(argv, capture_output=True, text=True).stdout
        status = f"{len(result.splitlines())} lines"
    elif engine == "main":
        from deepagents.backends import FilesystemBackend

        found = FilesystemBackend(root_dir=workspace, virtual_mode=True).grep(
            query["pattern"], "/", max_count=query.get("max_count", 1000))
        status = f"{len(found.matches or [])} matches{' truncated' if found.truncated else ''}"
    else:
        from agent.tools import grep as implementation

        result = implementation.search(_backend("python" if engine == "new-python" else "rg", workspace),
                                       query["pattern"], None, None, query["output_mode"],
                                       query.get("max_count"), False)
        output_lines = result.splitlines()
        if len(output_lines) == 1 and output_lines[0].startswith("Error:"):
            status = output_lines[0]
        else:
            notes = [line for line in output_lines if line.startswith(("Results", "Error:"))]
            status = f"{len(output_lines) - len(notes)} lines" + (f"; {notes[-1]}" if notes else "")
    wall = time.perf_counter() - start
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return wall, time.process_time() - cpu, child, status


def measure(engine: str, workspace: Path, query: str, repeat: int) -> dict:
    runs = [_measure_once(engine, workspace, QUERIES[query]) for _ in range(repeat)]
    return {
        "wall_ms": round(statistics.median(r[0] for r in runs) * 1000, 1),
        "supervisor_cpu_ms": round(statistics.median(r[1] for r in runs) * 1000, 1),
        "child_cpu_ms": round(statistics.median(r[2] for r in runs) * 1000, 1),
        "result": runs[-1][3],
    }


def _child(engine: str, workspace: Path, query: str, repeat: int, source: Path | None,
           strace: bool = False) -> dict:
    env = dict(os.environ, PYTHONPATH=str(source or ROOT))
    argv = [sys.executable, __file__, "measure", "--engine", engine, "--workspace", str(workspace),
            "--query", query, "--repeat", str(repeat)]
    if strace:
        with tempfile.NamedTemporaryFile(prefix="grep-bench-strace-") as trace:
            # -s keeps the executable path intact; batch invocations have huge argv
            # and strace truncates the rest of the line, so count the execve path only.
            subprocess.run(["strace", "-f", "-qq", "-s", "80", "-e", "trace=execve", "-o", trace.name, *argv],
                           env=env, check=True, capture_output=True, timeout=600)
            text = Path(trace.name).read_text(errors="replace")
        return {"rg_starts": len(re.findall(r'execve\("[^"]+/rg"', text))}
    output = subprocess.run(argv, env=env, check=True, capture_output=True, text=True, timeout=600).stdout
    return json.loads(output.splitlines()[-1])


def cancel_probe(workspace: Path) -> dict:
    """Cancel a full-scan sandboxed search shortly after start and verify reaping."""
    from agent.cancel import ToolCancelContext, clear_cancel_context, set_cancel_context
    from agent.tools import grep as implementation

    backend = _backend("rg", workspace)
    context = ToolCancelContext("grep", "bench", threading.Event())
    result = []

    def run():
        set_cancel_context(context)
        try:
            result.append(implementation.search(backend, RARE + "|zz(z|y)+q", None, None, "content", None, False))
        finally:
            clear_cancel_context()

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.02)
    start = time.perf_counter()
    context.request_cancel()
    thread.join(5)
    leftovers = [p.name for p in Path("/proc").iterdir() if p.name.isdigit()
                 and RARE.encode() in _cmdline(p)]
    after = implementation.search(backend, RARE, None, None, "files_with_matches", None, False)
    return {"return_ms": round((time.perf_counter() - start) * 1000, 1), "result": result[0] if result else None,
            "leftover_processes": leftovers, "next_search_ok": after.startswith("/workspace/")}


def _cmdline(path: Path) -> bytes:
    try:
        return (path / "cmdline").read_bytes()
    except OSError:
        return b""


def run(root: Path, repeat: int, baseline: Path | None, engines: list[str]) -> dict:
    generate(root)
    rg = shutil.which("rg")
    report = {
        "rg": rg, "rg_version": subprocess.run([rg, "--version"], capture_output=True, text=True).stdout.splitlines()[0],
        "bwrap_version": subprocess.run([shutil.which("bwrap"), "--version"], capture_output=True, text=True).stdout.strip(),
        "cpus": os.cpu_count(), "repeat": repeat, "datasets": {},
    }
    has_strace = shutil.which("strace") is not None
    for name, total in DATASETS.items():
        workspace = root / name
        candidates = subprocess.run([rg, "--no-config", "--files", str(workspace)], capture_output=True, text=True)
        entry = {"files": total, "rg_candidate_files": len(candidates.stdout.splitlines()), "queries": {}}
        for query in QUERIES:
            entry["queries"][query] = {}
            for engine in engines:
                if engine == "baseline" and baseline is None:
                    continue
                source = baseline if engine == "baseline" else None
                try:
                    measured = _child(engine, workspace, query, repeat, source)
                except subprocess.SubprocessError as exc:
                    measured = {"error": str(exc)[:300]}
                if has_strace and engine in ("new", "baseline") and query == "rare/files" and "error" not in measured:
                    try:
                        measured.update(_child(engine, workspace, query, 1, source, strace=True))
                    except subprocess.SubprocessError as exc:
                        measured["rg_starts_error"] = str(exc)[:300]
                entry["queries"][query][engine] = measured
        report["datasets"][name] = entry
    report["cancel_G04"] = cancel_probe(root / "G04")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate").add_argument("--root", type=Path, required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--root", type=Path, required=True)
    run_parser.add_argument("--repeat", type=int, default=3)
    run_parser.add_argument("--baseline-source", type=Path)
    run_parser.add_argument("--engines", default="baseline,new,new-python,rg,main")
    run_parser.add_argument("--report", type=Path)
    child = sub.add_parser("measure")
    child.add_argument("--engine", required=True)
    child.add_argument("--workspace", type=Path, required=True)
    child.add_argument("--query", required=True)
    child.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "generate":
        generate(args.root)
    elif args.command == "measure":
        print(json.dumps(measure(args.engine, args.workspace, args.query, args.repeat)))
    else:
        report = run(args.root, args.repeat, args.baseline_source, args.engines.split(","))
        text = json.dumps(report, indent=2, ensure_ascii=False)
        print(text)
        if args.report:
            args.report.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
