#!/usr/bin/env python3
"""Add a read-only audit library to matching Unity containers only."""
from collections import deque
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile


def summarize(log):
    """Publish outside Unity's process tree without changing Docker streams."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        lines = deque(maxlen=40)
        count = 0
        with log.open() as stream:
            for line in stream:
                count += 1
                lines.append(line.rstrip("\n"))
        with open(summary, "a") as output:
            output.write(f"\n### Unity descriptor observation ({log.name})\n\n")
            output.write(f"{count} events; complete file: `{log}`. Last 40 events:\n\n```text\n")
            output.write("\n".join(lines) + "\n```\n")
    except OSError:
        # An unavailable summary must not change the editor's result or streams.
        pass


def run_observed(docker, args, log):
    process = subprocess.Popen([docker, *args])
    def forward(signum, _frame):
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    status = process.wait()
    summarize(log)
    if status < 0:
        signal.signal(-status, signal.SIG_DFL)
        os.kill(os.getpid(), -status)
    raise SystemExit(status)


def main():
    config = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
    args = os.sys.argv[1:]
    image = os.environ.get("UNITY_IMAGE", "")
    if args and args[0] == "run" and image and image in args[1:]:
        # Do not replace an existing loader audit policy.
        if any(arg == "LD_AUDIT" or arg.startswith("LD_AUDIT=") for arg in args[1:]):
            raise SystemExit("Unity descriptor observation cannot replace an existing LD_AUDIT")
        descriptor, filename = tempfile.mkstemp(prefix="launch-", suffix=".log", dir=config["logs"])
        os.fchmod(descriptor, 0o666)
        os.close(descriptor)
        log = Path(filename)
        args = ["run", "--volume", config["library"] + ":/opt/unity-fd-observer.so:ro",
                "--volume", str(log) + ":/opt/unity-fd-observer.log",
                "--env", "LD_AUDIT=/opt/unity-fd-observer.so",
                "--env", "UNITY_FD_OBSERVER_LOG=/opt/unity-fd-observer.log", *args[1:]]
        run_observed(config["docker"], args, log)
    os.execv(config["docker"], [config["docker"], *args])


if __name__ == "__main__":
    main()
