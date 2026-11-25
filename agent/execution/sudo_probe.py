"""Feasibility verification for sudo privilege escalation inside Bubblewrap.

Unprivileged Bubblewrap sandboxes enforce kernel ``PR_SET_NO_NEW_PRIVS``
(``NoNewPrivs: 1``) in both ``restricted`` and ``full-access`` modes, which
prevents setuid ``sudo`` from escalating privileges without breaking sandbox
isolation. In addition, processes inside a shared same-UID Worker PID namespace
cannot authenticate an unforgeable ``SUDO_ASKPASS`` caller.

Per Sections 21-22 of the architecture specification, sudo Runtime Authentication
MUST remain disabled unless both privilege escalation and a trusted, unforgeable
host password delivery channel are verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SudoFeasibility:
    """Result of probing whether sudo privilege escalation is viable inside the sandbox."""

    supported: bool
    sandbox_mode: str
    no_new_privs: bool | None
    reason: str
    sudo_output: str = ""


def probe_sudo_feasibility(backend: Any, *, timeout: int = 5) -> SudoFeasibility:
    """Run a read-only probe inside ``backend`` to verify real sudo/NoNewPrivs boundaries."""
    executor = getattr(backend, "executor", backend)
    config = getattr(executor, "config", None)
    mode = str(getattr(config, "mode", "unknown") or "unknown")
    probe_cmd = (
        "grep -m1 '^NoNewPrivs:' /proc/self/status 2>/dev/null || echo 'NoNewPrivs: unknown'; "
        "echo '---SUDO---'; "
        "if command -v sudo >/dev/null 2>&1; then sudo -n true 2>&1 || true; "
        "else echo 'sudo: command not found'; fi"
    )
    response = backend.execute(probe_cmd, timeout=timeout)
    output = str(getattr(response, "output", "") or "")
    header, _, sudo_part = output.partition("---SUDO---")
    no_new_privs: bool | None = None
    for line in header.splitlines():
        if line.startswith("NoNewPrivs:"):
            val = line.split(":", 1)[1].strip()
            if val == "1":
                no_new_privs = True
            elif val == "0":
                no_new_privs = False
            break
    sudo_output = sudo_part.strip()
    if no_new_privs is True or "no new privileges" in sudo_output.lower():
        return SudoFeasibility(
            supported=False,
            sandbox_mode=mode,
            no_new_privs=True,
            reason=(
                f"Bubblewrap ({mode}) sets kernel PR_SET_NO_NEW_PRIVS (NoNewPrivs: 1), "
                "which blocks setuid sudo privilege escalation without relaxing sandbox isolation. "
                "sudo Runtime Authentication is disabled."
            ),
            sudo_output=sudo_output,
        )
    if "sudo: command not found" in sudo_output.lower():
        return SudoFeasibility(
            supported=False,
            sandbox_mode=mode,
            no_new_privs=no_new_privs,
            reason=f"sudo is not available inside the {mode} sandbox. sudo Runtime Authentication is disabled.",
            sudo_output=sudo_output,
        )
    return SudoFeasibility(
        supported=False,
        sandbox_mode=mode,
        no_new_privs=no_new_privs,
        reason=(
            f"Shared same-UID Worker PID namespace in {mode} mode cannot establish an unforgeable "
            "trusted SUDO_ASKPASS channel without exposing credentials to sibling sandbox processes. "
            "sudo Runtime Authentication remains disabled."
        ),
        sudo_output=sudo_output,
    )
