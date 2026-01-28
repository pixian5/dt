#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQ_FILE = ROOT / "requirements.txt"
BOOT_ENV = "DT_STARTPY_BOOTSTRAP"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _in_venv() -> bool:
    return bool(os.environ.get("VIRTUAL_ENV")) or (getattr(sys, "base_prefix", sys.prefix) != sys.prefix)


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, check=True, env=env)


def _ensure_venv() -> None:
    if VENV_DIR.exists():
        return
    _run([sys.executable, "-m", "venv", str(VENV_DIR)])


def _reexec_in_venv(argv: list[str]) -> None:
    env = os.environ.copy()
    env[BOOT_ENV] = "1"
    vpy = _venv_python()
    _run([str(vpy), str(Path(__file__).name), *argv], env=env)


def _install_deps() -> None:
    _run([sys.executable, "-m", "pip", "install", "-r", str(REQ_FILE)])
    _run([sys.executable, "-m", "playwright", "install"])


def _run_watch(argv: list[str]) -> None:
    _run([sys.executable, str(ROOT / "watch.py"), *argv])


def main(argv: list[str]) -> None:
    os.chdir(ROOT)

    if not _in_venv():
        _ensure_venv()
        _reexec_in_venv(argv)
        return

    if os.environ.get(BOOT_ENV) != "1":
        # If user manually activated venv, still install deps but avoid reexec loops.
        os.environ[BOOT_ENV] = "1"

    _install_deps()
    _run_watch(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
