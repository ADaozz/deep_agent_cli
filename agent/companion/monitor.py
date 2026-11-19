"""Read Linux counters and services started by this CLI, off the UI loop."""
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from agent.execution.service_observation import OWNER, OWNER_VARIABLE, active_commands


@dataclass(frozen=True)
class Service:
    host_pid: int
    started: str
    pid: int
    name: str
    sandbox: bool


def _listening(directory: Path) -> set[str]:
    inodes = set()
    for proto in ("tcp", "tcp6"):
        try:
            lines = (directory / "net" / proto).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A":
                inodes.add(f"socket:[{fields[9]}]")
    return inodes


def services(proc: Path = Path("/proc")) -> tuple[Service, ...]:
    prefix = f"{OWNER_VARIABLE}={OWNER}:".encode()
    active = active_commands()
    owned = {}
    listeners = {}
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            marker = next((v for v in (path / "environ").read_bytes().split(b"\0") if v.startswith(prefix)), None)
            if marker is None:
                continue
            pid = int(path.name)
            stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] == "Z":
                continue
            status = (path / "status").read_text().splitlines()
            namespace = next((line.split()[1:] for line in status if line.startswith("NSpid:")), [str(pid)])
            ns = os.readlink(path / "ns/net")
            if ns not in listeners:
                listeners[ns] = _listening(path)
            sockets = listeners[ns]
            listening = False
            for fd in (path / "fd").iterdir():
                try:
                    if os.readlink(fd) in sockets:
                        listening = True
                        break
                except OSError:
                    continue
            name = (path / "comm").read_text().strip()
            argv = (path / "cmdline").read_bytes().decode(errors="replace").split("\0")
            if name.startswith(("python", "node")) and len(argv) > 1:
                if argv[1] == "-m" and len(argv) > 2:
                    name = argv[2]
                elif argv[1] and not argv[1].startswith("-"):
                    name = Path(argv[1]).name
            record = Service(pid, stat[19], int(namespace[-1]), name, len(namespace) > 1)
            owned[pid] = (int(stat[1]), marker.split(b"=", 1)[1].decode() not in active, listening, record)
        except (OSError, ValueError, IndexError):
            continue
    parents = {parent for parent, *_ in owned.values()}
    return tuple(sorted((record for pid, (_, finished, listening, record) in owned.items()
                         if listening or (finished and pid not in parents)),
                        key=lambda row: row.host_pid))


class Resources:
    def __init__(self):
        self.previous = None

    def sample(self):
        cpu = ram = gpu = None
        try:
            ticks = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
            total, idle = sum(ticks), ticks[3] + ticks[4]
            if self.previous and total > self.previous[0]:
                cpu = round(100 * (1 - (idle - self.previous[1]) / (total - self.previous[0])))
            self.previous = total, idle
            memory = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()}
            ram = round(100 * (1 - memory["MemAvailable"] / memory["MemTotal"]))
        except (OSError, KeyError, ValueError, IndexError, ZeroDivisionError):
            pass
        try:
            path = next(Path("/sys/class/drm").glob("card*/device/gpu_busy_percent"), None)
            if path:
                gpu = int(path.read_text())
            elif exe := shutil.which("nvidia-smi"):
                result = subprocess.run([exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                                        capture_output=True, text=True, timeout=0.8, check=True)
                gpu = int(result.stdout.splitlines()[0])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
        return cpu, gpu, ram
