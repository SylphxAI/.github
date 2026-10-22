"""Classify a failed run as infrastructure or code, per failing step's terminal error.

Reads {"jobs": [{name, conclusion, steps: [{name, conclusion}], annotations: [text]}]}
on stdin and prints `infra <signature>` or `none`.

A run is infrastructure only when EVERY failing step's TERMINAL ERROR is itself
an infrastructure signature: the installation-token mint, an artifact upload, a
cache 403, the runner set-up or the checkout, or a job that died with the runner
(no failed step, a runner-lost annotation). A test assertion, a clippy or build
error, or any other project-code failure is a code fault even when the SAME job
also logged infrastructure-shaped noise (ENOSPC, a cache warning): the failing
step's own terminal error decides, never the job's annotations taken as a set.
A job-level view that let unrelated noise hide a real assertion is exactly the
misjudgement this rule replaces.
"""
import json
import re
import sys

# (label, step-name pattern, terminal-error pattern). A step is that
# signature only when its name matches AND its terminal error names it: a
# step that merely runs after an upload is not an upload failure.
STEP_SIGNATURES = [
    ("installation-token-mint",
     re.compile(r"installation token|create-github-app-token|token mint", re.I),
     re.compile(r"installation token|create-github-app-token|token mint|Bad credentials|"
                r"could not create an installation token", re.I)),
    ("upload-artifact",
     re.compile(r"upload[- ]artifact|upload .*artifact", re.I),
     re.compile(r"upload[- ]artifact|artifact upload|failed to upload|"
                r"an error occurred while uploading", re.I)),
    # The platform failing before any project code ran: the runner
    # set-up and the source checkout (runner, network).
    ("runner-setup", re.compile(r"^set up job$", re.I),
     re.compile(r"^set up job$|runner|network|connection|timed out|shutdown", re.I)),
    ("checkout", re.compile(r"^(run )?(actions/)?checkout\b", re.I),
     re.compile(r"checkout|git clone|could not resolve host|connection|network|timed out", re.I)),
]
CACHE_STEP = re.compile(r"cache", re.I)
CACHE_403 = re.compile(r"\b403\b|forbidden", re.I)
# A terminal error that is unmistakably project code, whatever else the job
# logged: a test framework's own failure, a compiler or a linter. Every
# alternative is anchored on the form the tool actually prints, so an
# infrastructure line that merely starts with "Failed to ..." (an upload, a
# cache restore) never matches.
CODE_ERROR = re.compile(
    r"AssertionError|assert(ion)? failed|Traceback \(most recent call last\)|"
    r"^\s*(FAILED|ERROR) [^\s]+(::| - |\.py|\.rs|\.ts|\.js)|\bpanicked at\b|"
    r"^error\[E[0-9]+\]|^error: |^\s*(clippy|rustc|cargo)::",
    re.I | re.M,
)
# GitHub's own runner-lost annotations, verbatim (hosted, self-hosted,
# and the shutdown signal), anchored at the start of the message.
RUNNER_LOST = re.compile(
    r"The hosted runner lost communication with the server\."
    r"|The self-hosted runner: \S+ lost communication with the server\."
    r"|The runner has received a shutdown signal\."
)
# The runner's own exit-code annotation is not a project error by itself: a
# step that failed for infrastructure exits non-zero too, so it is read as the
# step's error line only with the step's own signature.
RUNNER_EXIT = re.compile(r"^Process completed with exit code \d+\.$", re.I)


def step_signature(step, notes):
    """The infra signature of ONE failing step, or None when it is project code.

    `notes` are the job's failure annotation messages (its terminal error
    lines). The step is an infrastructure signature only when its name and its
    own terminal error both name that infrastructure; a terminal error that
    names project code (an assertion, a compiler, a test) decides `None` first,
    so noise elsewhere in the job can never hide it.
    """
    name = step.get("name") or ""
    terminal = [str(n) for n in notes if not RUNNER_EXIT.match(str(n).strip())]
    if any(CODE_ERROR.search(n) for n in terminal):
        return None
    for label, step_rx, error_rx in STEP_SIGNATURES:
        if not step_rx.search(name):
            continue
        if not terminal:
            return label
        if any(error_rx.search(n) for n in terminal):
            return label
        return None
    if CACHE_STEP.search(name) and any(CACHE_403.search(n) for n in notes):
        return "cache-403"
    return None


def job_signature(job):
    notes = [str(n) for n in job.get("annotations") or []]
    failed = [s for s in job.get("steps") or [] if s.get("conclusion") == "failure"]
    sigs = [step_signature(s, notes) for s in failed]
    # Any failed step whose terminal error is not infrastructure (a test,
    # clippy, a build) is a code failure, whatever the job also logged.
    if any(sig is None for sig in sigs):
        return None
    if sigs:
        return sigs[0]
    # No step failed: the job died with the runner, and only a runner-lost
    # annotation (never project noise) says so.
    if any(RUNNER_LOST.match(n) for n in notes):
        return "runner-lost"
    return None


def main():
    jobs = [
        j for j in json.load(sys.stdin).get("jobs", [])
        if j.get("conclusion") in ("failure", "timed_out")
    ]
    signatures = [job_signature(j) for j in jobs]
    if jobs and all(signatures):
        print("infra " + ",".join(sorted(set(signatures))))
    else:
        print("none")


if __name__ == "__main__":
    main()
