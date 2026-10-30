"""Compressed first-page bytes from web-smoke's cold, pre-interaction request report.

The title owns its measured baseline. Compare it with the PR/merge-group base,
refuse increases, and require a new lower record after a material reduction.
Compression matches the existing pack size report: Brotli quality 11, lgwin 24.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit


# Matches the existing Keel release-pack module growth tolerance.
TOLERANCE_PERCENT = 2


def baseline_bytes(text: str) -> int:
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"version", "brotli_bytes"}:
        raise ValueError("baseline must contain version and brotli_bytes only")
    size = data["brotli_bytes"]
    if type(data["version"]) is not int or data["version"] != 1 or type(size) is not int or size <= 0:
        raise ValueError("baseline needs version 1 and a positive integer brotli_bytes")
    return size


def baseline_text(size: int) -> str:
    return json.dumps({"version": 1, "brotli_bytes": size}, indent=2) + "\n"


def base_record(baseline: Path, ref: str) -> int | None:
    """Read the same baseline path from a locally available, trusted base commit."""
    if not ref:
        return None
    cwd = baseline.resolve().parent
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                          text=True, capture_output=True, check=True).stdout.strip()
    relative = baseline.resolve().relative_to(root).as_posix()
    commit = subprocess.run(["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
                            cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    listing = subprocess.run(["git", "ls-tree", "--name-only", commit, "--", relative],
                             cwd=root, text=True, capture_output=True, check=True).stdout.strip()
    if not listing:
        return None  # Initial adoption still has to equal a real measurement.
    content = subprocess.run(["git", "show", commit + ":" + relative], cwd=root,
                             text=True, capture_output=True, check=True).stdout
    return baseline_bytes(content)


def requested_files(pack: Path, evidence: dict) -> tuple[list[Path], list[str]]:
    """Use the browser's observed set, including CSS, fonts, images and data.

    Resolve paths as static_host.py does. Unrequested pack files stay lazy;
    an observed local URL that cannot be mapped is incomplete evidence.
    """
    if evidence.get("ok") is not True:
        raise ValueError("a successful cold boot report is required")
    page = urlsplit(evidence.get("url", ""))
    requests = evidence.get("requests")
    if not page.scheme or not page.netloc or not isinstance(requests, list) or not requests:
        raise ValueError("boot report has no request evidence; use a newer keel-ref")
    # The smoke serves index.html under the pack's <base href>.
    if not page.path.endswith("/index.html"):
        raise ValueError("boot report URL must name the packed index.html")
    base = unquote(page.path[:-len("index.html")])
    root = pack.resolve()
    found, external = set(), set()
    for url in requests:
        if not isinstance(url, str):
            raise ValueError("request evidence contains a non-URL")
        target = urlsplit(url)
        if target.scheme in {"data", "blob"}:
            continue  # Their bytes are already in a fetched file.
        if (target.scheme, target.netloc) != (page.scheme, page.netloc):
            external.add(url)
            continue  # API responses are outside the built-pack byte budget.
        path = unquote(target.path)
        if not path.startswith(base):
            raise ValueError("requested URL is outside the pack base: " + url)
        relative = path[len(base):] or "index.html"
        candidate = root / relative
        choices = [candidate, Path(str(candidate) + ".html"), candidate / "index.html"]
        for choice in choices:
            resolved = choice.resolve()
            if not resolved.is_relative_to(root):
                raise ValueError("requested path leaves the pack: " + url)
            if resolved.is_file():
                found.add(resolved)
                break
        else:
            raise ValueError("requested file is missing from the pack: " + url)
    if root / "index.html" not in found:
        raise ValueError("request evidence does not include index.html")
    return sorted(found), sorted(external)


def measure(pack: Path, evidence: dict, compress_size) -> dict:
    files, external = requested_files(pack, evidence)
    rows = []
    for path in files:
        raw = path.read_bytes()
        if not raw:
            raise ValueError("requested file is empty: " + str(path))
        rows.append({"path": path.relative_to(pack.resolve()).as_posix(),
                     "raw_bytes": len(raw), "brotli_bytes": compress_size(raw)})
    return {"version": 1, "compression": "brotli-q11-lgwin24", "files": rows,
            "brotli_bytes": sum(row["brotli_bytes"] for row in rows),
            "external_requests": external, "first_frame_s": evidence.get("first_frame_s")}


def judge(measured: int, head: int | None, base: int | None) -> tuple[list[str], int]:
    errors = []
    if head is None:
        errors.append("commit the measured first-load baseline")
    elif base is None and head != measured:
        errors.append("initial baseline must equal the measured first load")
    elif base is not None and head > base:
        errors.append("first-load baseline grew; it can only shrink")
    ceiling = min(x for x in (head, base, measured if head is None else None) if x is not None)
    if measured * 100 > ceiling * (100 + TOLERANCE_PERCENT):
        errors.append("first load grew beyond the baseline plus 2%")
    if head is not None and measured * (100 + TOLERANCE_PERCENT) < head * 100:
        errors.append("first load shrank; commit the lower measured baseline")
    return errors, min(measured, ceiling)


def check(pack: Path, evidence_file: Path, baseline: Path, base: str,
          report: Path, compress_size, first_paint_ms=None) -> tuple[bool, str]:
    figures = measure(pack, json.loads(evidence_file.read_text()), compress_size)
    head = baseline_bytes(baseline.read_text()) if baseline.is_file() else None
    previous = base_record(baseline, base) if base else head
    errors, next_size = judge(figures["brotli_bytes"], head, previous)
    figures.update(baseline_bytes=head, base_baseline_bytes=previous,
                   tolerance_percent=TOLERANCE_PERCENT, ok=not errors, errors=errors,
                   slow_3g_first_contentful_paint_ms=first_paint_ms)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(figures, indent=2) + "\n")
    candidate = report.with_name("web-first-load-baseline.json")
    candidate.write_text(baseline_text(next_size))
    lines = ["first-load: %d B Brotli q11, baseline %s B, base %s B, tolerance 2%%"
             % (figures["brotli_bytes"], head, previous)]
    lines.append("startup: Slow 3G first contentful paint %s ms; ready frame %s s"
                 % (first_paint_ms, figures["first_frame_s"]))
    lines += ["  %(path)s: %(brotli_bytes)d B (%(raw_bytes)d B raw)" % row for row in figures["files"]]
    lines += ["FAIL: " + error for error in errors]
    if errors:
        lines.append("Proposed baseline: %s (copy to %s)" % (candidate, baseline))
    if figures["external_requests"]:
        lines.append("External requests excluded from the built-pack budget: %d" % len(figures["external_requests"]))
    return not errors, "\n".join(lines)
