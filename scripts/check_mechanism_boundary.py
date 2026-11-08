"""Check tracked mechanism files against an explicit repository-owned policy."""
import argparse
import json
from pathlib import Path
import subprocess


def check(root, policy):
    roots = policy["mechanism_roots"]
    literals = policy["forbidden_literals"]
    if not roots or not literals or any(not isinstance(s, str) or not s for s in [*roots, *literals]):
        raise ValueError("Mechanism roots and forbidden literals must be nonempty strings")
    if any(Path(s).is_absolute() or ".." in Path(s).parts for s in roots):
        raise ValueError("Mechanism roots must stay inside the repository")
    paths = subprocess.check_output(["git", "ls-files", "-z", "--", *roots], cwd=root).split(b"\0")
    findings = []
    for entry in filter(None, paths):
        path = root / entry.decode("utf-8")
        if path.is_symlink():
            raise ValueError(f"Mechanism path cannot be a symlink: {entry!r}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # Binary assets have no mechanism source text.
        for line, content in enumerate(text.splitlines(), 1):
            if any(literal in content for literal in literals):
                # Source may contain credentials: report only location, never content.
                findings.append(f"{path.relative_to(root)}:{line}: repository-specific literal in mechanism source")
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    findings = check(args.root, json.loads(args.policy.read_text(encoding="utf-8")))
    for finding in findings:
        print(finding)
    return bool(findings)


if __name__ == "__main__":
    raise SystemExit(main())
