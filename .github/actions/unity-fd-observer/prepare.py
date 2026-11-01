#!/usr/bin/env python3
"""Install an opt-in, job-local Docker observer; do not request a Unity seat."""
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import subprocess
import tempfile


def main():
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise SystemExit("Unity descriptor observation requires Linux x86_64/glibc")
    source = Path(__file__).resolve().parent
    real_docker = shutil.which("docker")
    if real_docker is None:
        raise SystemExit("Docker must be installed before descriptor observation")
    directory = Path(tempfile.mkdtemp(prefix="unity-fd-observer-", dir=os.environ["RUNNER_TEMP"]))
    library = directory / "audit.so"
    subprocess.run(["cc", "-std=c11", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                    "-O2", str(source / "audit.c"), "-o", str(library)], check=True)
    (directory / "config.json").write_text(json.dumps({"docker": real_docker, "library": str(library)}))
    wrapper = directory / "docker"
    shutil.copyfile(source / "docker.py", wrapper)
    wrapper.chmod(0o755)
    with open(os.environ["GITHUB_PATH"], "a") as output:
        output.write(str(directory) + "\n")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    print(f"Unity descriptor observer prepared; runner soft={soft} hard={hard} sysconf={os.sysconf('SC_OPEN_MAX')}")
    print("Only docker run commands for the current UNITY_IMAGE are observed; limits and exit status remain unchanged.")


if __name__ == "__main__":
    main()
