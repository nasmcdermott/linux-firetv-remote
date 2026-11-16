#!/usr/bin/env python3
"""Ensure the project venv matches the current machine and can import the app.

Distro upgrades can change ``python3`` from one minor version to another. A
venv built for the old interpreter then remains on disk but is effectively
empty, which makes the systemd services fail with ``ModuleNotFoundError``.

This script is stdlib-only so it can run with the system Python even when the
venv is broken. It is used three ways:

* ``install.sh`` before installing units,
* ``ExecStartPre`` for the voice and receiver services,
* the generated ``firetv-remote`` launcher.

When the venv is healthy it exits quickly. When it is stale or incomplete it
rebuilds it, preferring the system Python and falling back to a ``uv``-managed
interpreter when the voice wheels do not support that Python ABI yet.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".venv"
LOCK_PATH = REPO_ROOT / ".venv.lock"
REQUIRED_MODULES = ("numpy", "opuslib", "yaml", "faster_whisper")
MIN_PYTHON = (3, 11)
# The base stack works on 3.15, but onnxruntime (used only by faster-whisper's
# VAD filter) and the optional wake-word stack may not have wheels for the
# newest ABI. Keep the fallback bounded; the system Python is always tried first.
FALLBACK_PYTHON = ">=3.11,<3.15"


def venv_python() -> Path:
    return VENV_DIR / "bin" / "python"


def log(message: str) -> None:
    print(f"[venv] {message}", flush=True)


def _run(argv: list[str], cwd: Path = REPO_ROOT) -> bool:
    try:
        result = subprocess.run(argv, cwd=cwd, check=False)
    except OSError as exc:
        log(f"{argv[0]} failed: {exc}")
        return False
    return result.returncode == 0


def _python_version(executable: str | Path) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [
                str(executable),
                "-c",
                "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.strip().split(".", 1)
        return int(major), int(minor)
    except ValueError:
        return None


def _version_text(version: tuple[int, int] | None) -> str:
    return f"{version[0]}.{version[1]}" if version else "unknown"


def venv_healthy() -> bool:
    python = venv_python()
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [str(python), "-c", f"import {', '.join(REQUIRED_MODULES)}"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def find_uv() -> str | None:
    candidates = [
        os.environ.get("UV"),
        shutil.which("uv"),
        str(Path.home() / ".local" / "bin" / "uv"),
        "/usr/local/bin/uv",
        "/usr/bin/uv",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def python_candidates() -> list[str]:
    """System and versioned Pythons, system first, newest fallback next."""
    candidates: list[str] = [sys.executable]
    for name in ("python3", "python3.14", "python3.13", "python3.12", "python3.11"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        resolved = str(Path(candidate).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return unique


def _rebuild_with_uv(uv: str) -> bool:
    system_python = sys.executable
    log(
        "syncing dependencies with uv "
        f"(system Python {_version_text(_python_version(system_python))})"
    )
    if _run([uv, "sync", "--python", system_python]):
        return True
    log(
        "system Python has no wheels for the voice stack; "
        f"retrying with a uv-managed Python in {FALLBACK_PYTHON}"
    )
    return _run([uv, "sync", "--python", FALLBACK_PYTHON])


def _rebuild_with_pip() -> bool:
    requirements = REPO_ROOT / "requirements.txt"
    for candidate in python_candidates():
        version = _python_version(candidate)
        if version is None or version < MIN_PYTHON:
            continue
        log(f"creating venv with {candidate} (Python {_version_text(version)})")
        if not _run([candidate, "-m", "venv", str(VENV_DIR)]):
            continue
        python = venv_python()
        if not _run([str(python), "-m", "pip", "install", "--upgrade", "pip"]):
            continue
        if _run([str(python), "-m", "pip", "install", "-r", str(requirements)]):
            return True
    return False


def rebuild(force: bool = False) -> bool:
    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not force and venv_healthy():
            return True

        if VENV_DIR.exists() or VENV_DIR.is_symlink():
            log("project venv is stale or incomplete; rebuilding")
            shutil.rmtree(VENV_DIR, ignore_errors=True)

        uv = find_uv()
        if uv:
            ok = _rebuild_with_uv(uv)
        else:
            log("uv not found; using python -m venv + pip")
            ok = _rebuild_with_pip()
        if not ok:
            log(
                "could not rebuild the project venv. Install uv, or a Python "
                "in 3.11-3.14 with the dev packages, then retry."
            )
            return False
        if not venv_healthy():
            log("venv rebuilt but the required modules still do not import")
            return False
        version = _python_version(venv_python())
        if version:
            log(f"venv ready (Python {_version_text(version)})")
        return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    quiet = "--quiet" in args
    force = "--force" in args
    unknown = [arg for arg in args if arg not in ("--quiet", "--force")]
    if unknown:
        log(f"unknown option(s): {' '.join(unknown)}")
        return 2

    if not force and venv_healthy():
        if not quiet:
            version = _python_version(venv_python())
            if version:
                log(f"venv ready (Python {_version_text(version)})")
        return 0
    return 0 if rebuild(force=force) else 1


if __name__ == "__main__":
    raise SystemExit(main())
