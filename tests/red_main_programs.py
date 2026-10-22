#!/usr/bin/env python3
"""Where the red-main handler's programs live now.

The handler's shell helpers and Python programs are checked in under
`.github/red_main/`, not embedded as heredocs in the workflow's `env:`. A test
that needs a program reads the reviewed file here.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAMS = ROOT / ".github" / "red_main"


def program(name: str) -> str:
    """The text of one checked-in program, e.g. `program("lib.sh")`."""
    return (PROGRAMS / name).read_text()
