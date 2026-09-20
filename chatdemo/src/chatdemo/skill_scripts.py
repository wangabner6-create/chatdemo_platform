"""Executes a skill's packaged scripts under a set of protective constraints (the 'trusted scripts' tier).

Constraints applied: an ALLOWLIST (execution limited to files that actually
live in the skill's scripts/ folder, matched by exact name only — no path
traversal permitted), a SCRUBBED ENVIRONMENT (the child process sees nothing
but PATH; backend secrets such as CHATDEMO_API_KEY are never passed through),
argv passed as a list rather than through a shell (so shell injection isn't
possible), and a TIMEOUT ceiling. Note this buys *safety*, not full
isolation — the script can still touch the container's filesystem freely.
Untrusted third-party scripts belong in a sandboxed container instead
(no network, read-only mounts, non-root user).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Maps file extension to the interpreter command needed to launch it. Anything not listed here is rejected.
_INTERPRETERS: dict[str, list[str]] = {".py": [sys.executable], ".sh": ["bash"]}

_MAX_OUTPUT = 8000


def run_skill_script(
    scripts: dict[str, Path], name: str, args: list | None, timeout: int
) -> str:
    """Execute the allowlisted script identified by `name`, passing `args`, and
    return its merged stdout/stderr output (truncated to a cap). This function
    swallows failures internally and reports them as a string rather than raising."""
    path = scripts.get(name)  # only an exact allowlisted filename match is accepted
    if path is None:
        return f"script '{name}' not allowed. available: {sorted(scripts)}"
    interp = _INTERPRETERS.get(path.suffix)
    if interp is None:
        return f"unsupported script type '{path.suffix}' (allowed: {', '.join(_INTERPRETERS)})"

    argv = [str(a) for a in (args or [])]
    try:
        proc = subprocess.run(
            [*interp, str(path), *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(path.parent),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},  # scrubbed environment, secrets excluded
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"script '{name}' timed out after {timeout}s"
    except OSError as exc:  # e.g. the required interpreter isn't installed
        return f"script '{name}' failed to start: {exc}"

    out = proc.stdout or ""
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr
    out = out.strip()[:_MAX_OUTPUT]
    return out or f"(script '{name}' exited {proc.returncode} with no output)"
