#!/usr/bin/env python3
"""Base-owned scope and trusted head statuses gate every PR in a merge group.

Adapted from cloud#11840. Only queue refs, never commit subjects, identify PRs.
The caller checks out the group with fetch-depth: 0 and retains git credentials
for the read-only origin fetch. No dependency installation or PR code execution.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import urllib.request

QUEUE_REF = re.compile(r"^refs/heads/gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]{40}$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.PIPE).strip()


def queue_refs_by_sha(text: str) -> dict[str, int]:
    refs = {}
    for line in text.splitlines():
        parts = line.split()
        match = QUEUE_REF.fullmatch(parts[1]) if len(parts) == 2 else None
        if match and SHA.fullmatch(parts[0]):
            refs[parts[0]] = int(match[1])
    return refs


def group_pull_requests(range_shas: list[str], on_target: set[str], refs: dict[str, int]):
    prs, unmapped = [], []
    for sha in range_shas:
        if sha not in on_target:
            if sha in refs:
                prs.append((refs[sha], sha))
            else:
                unmapped.append(sha)
    return prs, unmapped


def trusted_ids(text: str) -> set[int]:
    parts = text.split(",")
    if not parts or any(not re.fullmatch(r"[1-9][0-9]*", p.strip()) for p in parts):
        raise ValueError("trusted-creator-ids must contain positive numeric GitHub IDs")
    return {int(p) for p in parts}


def creator_list(value, name: str) -> set[int]:
    if not isinstance(value, list) or not value or any(type(x) is not int or x <= 0 for x in value):
        raise ValueError(f"{name} must contain positive GitHub creator IDs")
    return set(value)


def qa_reviewers(policy: dict) -> set[int]:
    # The QA reviewer App's bot user, trusted from the pinned shared policy only.
    # An empty list is valid until the App is registered; callers cannot add or remove it.
    value = policy["opsReview"].get("qaReviewerCreatorIds", [])
    if not isinstance(value, list) or any(type(x) is not int or x <= 0 for x in value):
        raise ValueError("opsReview.qaReviewerCreatorIds must contain positive GitHub creator IDs")
    return set(value)


def validate_config(cfg: dict) -> dict:
    if not isinstance(cfg, dict) or not isinstance(cfg.get("context"), str) or not cfg["context"]:
        raise ValueError("config needs a nonempty context")
    for field in ("requireStampLabels", "requireStampPaths", "requireStampPathGlobs"):
        if not isinstance(cfg.get(field), list) or any(not isinstance(p, str) or not p for p in cfg[field]):
            raise ValueError(f"config needs a string array: {field}")
    if cfg.get("enforceMissing") is not True:
        raise ValueError("missing-stamp enforcement must be enabled")
    reviewer = cfg.get("productReview", {})
    if not isinstance(reviewer, dict) or not isinstance(reviewer.get("context"), str) or not reviewer["context"]:
        raise ValueError("config needs a productReview context")
    creator_list(reviewer.get("trustedCreatorIds"), "productReview.trustedCreatorIds")
    return cfg


def read_config(base: str, path: str) -> dict:
    if pathlib.PurePosixPath(path).is_absolute() or ".." in pathlib.PurePosixPath(path).parts or ":" in path:
        raise ValueError("config-path must be a repository-relative path")
    # A missing base object must not be mistaken for first-adoption bootstrap.
    git("cat-file", "-e", f"{base}^{{commit}}")
    exists = git("ls-tree", "--name-only", base, "--", path)
    if exists:
        text = git("show", f"{base}:{path}")
    else:
        print(f"{path} absent on base; reading queued copy for first-adoption bootstrap")
        text = pathlib.Path(path).read_text()
    return validate_config(json.loads(text))


def latest_stamp(statuses: list[dict], context: str, creators: set[int]):
    matches = [s for s in statuses if s.get("context") == context and (s.get("creator") or {}).get("id") in creators]
    # Stable sort retains API order for stamps created in the same second.
    return sorted(matches, key=lambda s: dt.datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")), reverse=True)[0] if matches else None


def path_matches(file: str, pattern: str) -> bool:
    # fnmatch globs span path separators; **/ also permits a root directory.
    return fnmatch.fnmatchcase(file, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(file, pattern[3:])
    )


def stamp_required(labels: list[str], files: list[str], cfg: dict):
    for label in labels:
        if label in cfg["requireStampLabels"]:
            return f"label {label}"
    for file in files:
        for path in cfg.get("requireStampPaths", []):
            if file == path or (path.endswith("/") and file.startswith(path)):
                return f"path {file}"
        for pattern in cfg.get("requireStampPathGlobs", []):
            if path_matches(file, pattern):
                return f"path {file} ({pattern})"
    return None


def ops_requirement(repository: str, labels: list[str], files: list[str], cfg: dict, policy: dict):
    if repository in policy["platformRepositories"]:
        return "platform repository"
    # Mandatory policy first; repository scope can add triggers, never subtract.
    return stamp_required(labels, files, policy) or stamp_required(labels, files, cfg)


def verdict(stamp, required: str, reviewer: dict, author_id=None, shared_ids=frozenset()) -> tuple[bool, str]:
    context = reviewer["context"]
    if not stamp:
        return False, f"no trusted {context} stamp; required by {required}"
    if stamp["state"] != "success":
        return False, f"{context} is {stamp['state']}"
    prefix = reviewer.get("descriptionPrefix")
    if prefix and not (stamp.get("description") or "").startswith(prefix):
        return False, f"{context} success lacks required description prefix {prefix}"
    creator = (stamp.get("creator") or {}).get("id")
    if author_id is not None and creator == author_id and creator not in shared_ids:
        return False, f"{context} was stamped by the PR author, not an independent reviewer"
    limit = "; shared identity cannot prove reviewer independence" if creator == author_id and creator in shared_ids else ""
    return True, f"{context} success ({required}){limit}"


def review_verdict(repository: str, labels: list[str], files: list[str], statuses: list[dict],
                   cfg: dict, policy: dict, ops_creators: set[int], author_id=None) -> tuple[bool, str]:
    ops = policy["opsReview"]
    qa = qa_reviewers(policy)
    # QA's reviewer App is one review gate with Ops: either identity's latest stamp decides.
    # Even ordinary product PRs cannot override an explicit trusted Ops or QA veto.
    ops_stamp = latest_stamp(statuses, ops["context"], ops_creators | qa)
    if ops_stamp and ops_stamp["state"] != "success":
        return False, f"{ops['context']} is {ops_stamp['state']}"
    required = ops_requirement(repository, labels, files, cfg, policy)
    if required:
        return verdict(ops_stamp, required, ops)
    reviewer = cfg["productReview"]
    stamp = latest_stamp(statuses, reviewer["context"], creator_list(reviewer["trustedCreatorIds"], "product reviewer") | qa)
    return verdict(stamp, "owning lane independent final review", reviewer, author_id, set(policy["sharedCreatorIds"]))


def api(path: str):
    request = urllib.request.Request(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}", headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def statuses_for(sha: str) -> list[dict]:
    statuses, page = [], 1
    while True:
        batch = api(f"/commits/{sha}/statuses?per_page=100&page={page}")
        statuses.extend(batch)
        if len(batch) < 100:
            return statuses
        page += 1


def main() -> int:
    base, base_ref, head_ref, head = (os.environ.get(k, "") for k in (
        "MERGE_GROUP_BASE_SHA", "MERGE_GROUP_BASE_REF", "MERGE_GROUP_HEAD_REF", "MERGE_GROUP_HEAD_SHA"))
    if not SHA.fullmatch(base) or not SHA.fullmatch(head) or not base_ref.startswith("refs/heads/") or not QUEUE_REF.fullmatch(head_ref):
        raise ValueError("review-stamp-gate runs on merge_group only")
    if git("rev-parse", "HEAD") != head:
        raise ValueError("checkout is not the merge group's head")
    creators = trusted_ids(os.environ["REVIEW_TRUSTED_CREATOR_IDS"])
    cfg = read_config(base, os.environ.get("REVIEW_CONFIG_PATH", ".github/review-stamp.json"))
    policy = json.loads(pathlib.Path(__file__).with_name("policy.json").read_text())
    if not creators <= creator_list(policy["opsReview"]["trustedCreatorIds"], "Ops reviewers"):
        raise ValueError("trusted-creator-ids includes an identity not authorized by shared Ops policy")
    branch = base_ref.removeprefix("refs/heads/")
    # Read refs before target: entries merged between reads remain mapped or on target.
    refs = queue_refs_by_sha(git("ls-remote", "origin", f"refs/heads/gh-readonly-queue/{branch}/*"))
    git("fetch", "-q", "--no-tags", "origin", f"+{base_ref}:refs/remotes/origin/{branch}")
    range_shas = git("rev-list", f"{base}..{head}").splitlines()
    on_target = set(git("rev-list", f"{base}..refs/remotes/origin/{branch}").splitlines())
    prs, unmapped = group_pull_requests(range_shas, on_target, refs)
    if unmapped:
        raise ValueError(f"group commits map to no queued pull request: {' '.join(unmapped)}")
    self_pr = int(QUEUE_REF.fullmatch(head_ref)[1])
    if not any(number == self_pr for number, _ in prs):
        raise ValueError(f"group's own pull request #{self_pr} is absent from its commit range")
    failed = False
    for number, sha in prs:
        pr = api(f"/pulls/{number}")
        files = git("diff", "--name-only", f"{sha}^", sha).splitlines()
        ok, reason = review_verdict(
            os.environ["GITHUB_REPOSITORY"], [label["name"] for label in pr["labels"]], files,
            statuses_for(pr["head"]["sha"]), cfg, policy, creators, (pr.get("user") or {}).get("id"),
        )
        print(f"{'::error::' if not ok else ''}#{number} head {pr['head']['sha'][:8]}: {reason}")
        failed |= not ok
    return int(failed)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"::error::review-stamp-gate: {error}")
        raise SystemExit(1)
