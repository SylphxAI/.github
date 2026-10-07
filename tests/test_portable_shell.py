#!/usr/bin/env python3
"""The shared actions' shell scripts run on Linux and on macOS runners."""

from __future__ import annotations

import pathlib
import re
import unittest

ACTIONS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "actions"


class PortableRegexTest(unittest.TestCase):
    """macOS runners use bash 3.2 on the BSD regex library, whose RE_DUP_MAX is
    255: a bounded repeat above it ({1,8192}) does not compile there, so `=~`
    returns 2 and every check built on it fails. glibc allows 32767, so a Linux
    run cannot show it; read the bounds instead."""

    def test_no_bounded_repeat_over_255(self) -> None:
        for script in sorted(ACTIONS.rglob("*.sh")):
            for n, line in enumerate(script.read_text().splitlines(), 1):
                if "=~" not in line and "_re=" not in line:
                    continue
                for lo, hi in re.findall(r"\{(\d+)(?:,(\d*))?\}", line):
                    for bound in (lo, hi):
                        if bound:
                            self.assertLessEqual(int(bound), 255, f"{script.name}:{n}: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
