#!/usr/bin/env python3
"""Add a read-only audit library to matching Unity containers only."""
import json
import os
from pathlib import Path


def main():
    config = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
    args = os.sys.argv[1:]
    image = os.environ.get("UNITY_IMAGE", "")
    if args and args[0] == "run" and image and image in args[1:]:
        # Do not replace an existing loader audit policy.
        if any(arg == "LD_AUDIT" or arg.startswith("LD_AUDIT=") for arg in args[1:]):
            raise SystemExit("Unity descriptor observation cannot replace an existing LD_AUDIT")
        args = ["run", "--volume", config["library"] + ":/opt/unity-fd-observer.so:ro",
                "--env", "LD_AUDIT=/opt/unity-fd-observer.so", *args[1:]]
    os.execv(config["docker"], [config["docker"], *args])


if __name__ == "__main__":
    main()
