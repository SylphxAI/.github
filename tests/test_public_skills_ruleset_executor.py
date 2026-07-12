#!/usr/bin/env python3
"""Offline adversarial tests for the independently owned ruleset executor."""

from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "public-skills-ruleset-executor.py"
SPEC = importlib.util.spec_from_file_location("public_skills_ruleset_executor", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

EXECUTOR_HEAD = "a" * 40
DOCTRINE_HEAD = "b" * 40
SOURCE_SHA = "c" * 40
FIXTURE_BASE = "1" * 40
FIXTURE_BASE_TREE = "2" * 40
FIXTURE_HEAD = "3" * 40
FIXTURE_HEAD_TREE = "4" * 40
PR_BASE = "5" * 40
MERGE_SHA = "6" * 40
LOCAL_BYTES = SCRIPT.read_bytes()
ATTESTATION_POLICY_BYTES = (ROOT / module.ATTESTATION_POLICY_PATH).read_bytes()
FIXED_TIME = datetime(2026, 7, 11, 22, 0, tzinfo=timezone.utc)
PROVIDER_REQUEST_ID = "2F54:271FFE:842947:988994:6A52E19C"
ACTIVE_DOCTRINE_HEAD = "d" * 40
ATTESTATION_RULESET_ID = 654


def encoded(path: str, raw: bytes) -> dict:
    return {
        "type": "file",
        "path": path,
        "encoding": "base64",
        "content": base64.b64encode(raw).decode("ascii"),
        "size": len(raw),
        "sha": module.git_blob_sha(raw),
    }


def canonical_file(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def live_attestation_ruleset() -> dict:
    policy = module.validate_attestation_policy(
        module.strict_json_loads(ATTESTATION_POLICY_BYTES, label="test attestation policy")
    )
    return {
        **module.expected_attestation_ruleset(policy),
        "id": ATTESTATION_RULESET_ID,
        "source_type": "Organization",
        "source": module.ORGANIZATION,
        "current_user_can_bypass": "never",
    }


def base_record(*, phase: str = "expand", ruleset_id: int | None = None, enforcement: str = "evaluate") -> dict:
    return {
        "$schema": module.DOCTRINE_SCHEMA_REF,
        "schemaVersion": 1,
        "kind": "organization-required-workflow-ruleset",
        "id": module.DOCTRINE_RECORD_ID,
        "owner": module.DOCTRINE_REPOSITORY,
        "owningDecision": "public-skills-external-required-workflow",
        "organization": module.ORGANIZATION,
        "ruleset": {
            "rulesetId": ruleset_id,
            "name": module.RULESET_NAME,
            "target": "branch",
            "enforcement": enforcement,
            "bypassActors": [],
            "targetRepositories": [{
                "repositoryId": module.TARGET_REPOSITORY_ID,
                "acceptedNames": list(module.TARGET_REPOSITORY_NAMES),
                "finalName": module.TARGET_FINAL_NAME,
            }],
            "refInclude": ["~DEFAULT_BRANCH"],
            "refExclude": [],
            "doNotEnforceOnCreate": False,
        },
        "workflowSource": {
            "repositoryId": module.EXECUTOR_REPOSITORY_ID,
            "repository": module.EXECUTOR_REPOSITORY,
            "workflowPath": module.WORKFLOW_PATH,
            "workflowName": module.WORKFLOW_NAME,
            "requiredCheck": module.REQUIRED_CHECK,
            "localRequiredChecks": list(module.LOCAL_REQUIRED_CHECKS),
            "negativeControlPolicy": {
                "fixtureBaseSha": FIXTURE_BASE,
                "fixtureBaseTree": FIXTURE_BASE_TREE,
                "mutationClass": "package-script-neutralization",
                "mutationPath": "package.json",
                "scriptOverrides": {
                    "check": 'node -e "process.exit(0)"',
                    "verify:install": 'node -e "process.exit(0)"',
                },
            },
            "validatorPath": module.VALIDATOR_PATH,
            "policyPath": module.POLICY_PATH,
            "ref": module.EXECUTOR_BRANCH,
            "commitSha": SOURCE_SHA,
        },
        "migration": {
            "packetId": "public-skills-external-admission@2026-07-11.185b6776e16b",
            "class": "required-immediate",
            "phase": phase,
            "tracker": "https://github.com/SylphxAI/.github/issues/1",
            "compatibility": {"oldAcceptedUntil": None, "newRequiredAfter": None},
            "recoveryPlan": "Keep the same numeric ruleset and downgrade only through a protected Git ratchet.",
        },
        "activationEvidence": {
            "evaluateReadback": None,
            "pullRequestCanary": None,
            "mergeGroupCanary": None,
            "negativeControl": None,
            "effectiveRulesReadback": None,
            "activationTransition": None,
        },
        "recovery": None,
    }


class FakeAPI:
    def __init__(self) -> None:
        self.gets: dict[str, object] = {}
        self.page_values: dict[str, list] = {}
        self.get_calls: list[str] = []
        self.page_calls: list[str] = []
        self.mutations: list[tuple[str, str, dict]] = []
        self.lock_mutations: list[tuple[str, str, dict | None]] = []
        self.events: list[tuple[str, str]] = []
        self.git_tags: dict[str, dict] = {}
        self.git_refs: dict[str, dict] = {}
        self.post_created_overrides: dict[str, object] = {}
        self.get_optional_overrides: dict[str, object] = {}
        self.delete_overrides: dict[str, object] = {}
        self.post_readback_override: dict | None = None

    @staticmethod
    def override(value: object) -> object:
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value()
        return copy.deepcopy(value)

    def get(self, endpoint: str) -> object:
        self.get_calls.append(endpoint)
        self.events.append(("GET", endpoint))
        tag_prefix = f"{module.APPLY_LOCK_TAGS_ENDPOINT}/"
        if endpoint.startswith(tag_prefix):
            tag_sha = endpoint.removeprefix(tag_prefix)
            if tag_sha not in self.git_tags:
                raise module.ForgeError(f"unexpected missing tag {tag_sha}")
            return copy.deepcopy(self.git_tags[tag_sha])
        if endpoint not in self.gets:
            raise module.ForgeError(f"unexpected GET {endpoint}")
        value = self.gets[endpoint]
        if callable(value):
            value = value()
        return copy.deepcopy(value)

    def pages(self, endpoint: str) -> list:
        self.page_calls.append(endpoint)
        self.events.append(("PAGES", endpoint))
        if endpoint not in self.page_values:
            raise module.ForgeError(f"unexpected pages {endpoint}")
        return copy.deepcopy(self.page_values[endpoint])

    def post(self, endpoint: str, payload: dict) -> dict:
        self.events.append(("POST", endpoint))
        self.mutations.append(("POST", endpoint, copy.deepcopy(payload)))
        ruleset_id = 321
        post = {**copy.deepcopy(payload), "id": ruleset_id, "source_type": "Organization", "updated_at": "2026-07-11T22:00:00Z"}
        self.gets[f"/orgs/{module.ORGANIZATION}/rulesets/{ruleset_id}"] = self.post_readback_override or post
        return {"id": ruleset_id}

    def put(self, endpoint: str, payload: dict) -> dict:
        self.events.append(("PUT", endpoint))
        self.mutations.append(("PUT", endpoint, copy.deepcopy(payload)))
        ruleset_id = int(endpoint.rsplit("/", 1)[1])
        post = {
            **copy.deepcopy(payload),
            "id": ruleset_id,
            "source_type": "Organization",
            "updated_at": "2026-07-11T22:00:00Z",
        }
        self.gets[endpoint] = self.post_readback_override or post
        return {"id": ruleset_id}

    def put_observed(self, endpoint: str, payload: dict) -> tuple[dict, str]:
        return self.put(endpoint, payload), PROVIDER_REQUEST_ID

    def post_created(self, endpoint: str, payload: dict) -> dict:
        self.events.append(("POST", endpoint))
        self.lock_mutations.append(("POST", endpoint, copy.deepcopy(payload)))
        if endpoint in self.post_created_overrides:
            value = self.override(self.post_created_overrides[endpoint])
            if not isinstance(value, dict):
                raise module.ForgeError("fake POST-created override is not an object")
            return value
        if endpoint == module.APPLY_LOCK_TAGS_ENDPOINT:
            tag_sha = module.hashlib.sha1(b"tag\0" + module.canonical_bytes(payload)).hexdigest()
            response = {
                "sha": tag_sha,
                "tag": payload["tag"],
                "message": payload["message"],
                "object": {"type": payload["type"], "sha": payload["object"]},
            }
            if "tagger" in payload:
                response["tagger"] = copy.deepcopy(payload["tagger"])
            self.git_tags[tag_sha] = copy.deepcopy(response)
            return response
        if endpoint == module.APPLY_LOCK_REFS_ENDPOINT:
            ref_name = payload["ref"]
            if ref_name in self.git_refs:
                raise module.ForgeError("GitHub API POST create-ref failed with HTTP 422")
            response = {
                "ref": ref_name,
                "object": {"type": "tag", "sha": payload["sha"]},
            }
            self.git_refs[ref_name] = copy.deepcopy(response)
            return response
        raise module.ForgeError(f"unexpected POST-created {endpoint}")

    def get_optional(self, endpoint: str) -> object | None:
        self.get_calls.append(endpoint)
        self.events.append(("GET?", endpoint))
        if endpoint in self.get_optional_overrides:
            return self.override(self.get_optional_overrides[endpoint])
        if endpoint == module.APPLY_LOCK_REF_GET_ENDPOINT:
            ref_name = module.APPLY_LOCK_REF
        else:
            prefix = f"/repositories/{module.EXECUTOR_REPOSITORY_ID}/git/ref/tags/"
            if not endpoint.startswith(prefix):
                raise module.ForgeError(f"unexpected optional GET {endpoint}")
            ref_name = "refs/tags/" + endpoint.removeprefix(prefix)
        value = self.git_refs.get(ref_name)
        return copy.deepcopy(value)

    def delete(self, endpoint: str) -> None:
        self.events.append(("DELETE", endpoint))
        self.lock_mutations.append(("DELETE", endpoint, None))
        if endpoint in self.delete_overrides:
            self.override(self.delete_overrides[endpoint])
            return
        if endpoint != module.APPLY_LOCK_REF_DELETE_ENDPOINT:
            raise module.ForgeError(f"unexpected DELETE {endpoint}")
        if module.APPLY_LOCK_REF not in self.git_refs:
            raise module.ForgeError("GitHub API DELETE ref failed with HTTP 404")
        del self.git_refs[module.APPLY_LOCK_REF]


def base_api(record: dict, *, live: dict | None = None, effective: bool = True) -> FakeAPI:
    api = FakeAPI()
    workflow = (
        f"name: {module.WORKFLOW_NAME}\n"
        f"jobs:\n  admission:\n    name: {module.REQUIRED_CHECK}\n"
        f"    if: ${{{{ github.repository_id == {module.TARGET_REPOSITORY_ID} }}}}\n"
    ).encode()
    source_policy = {
        "target": {
            "repositoryId": module.TARGET_REPOSITORY_ID,
            "baseline": {"commit": FIXTURE_BASE, "tree": FIXTURE_BASE_TREE},
        }
    }
    api.gets.update({
        "/user": {
            "id": module.GITHUB_ACTOR_ID,
            "login": module.GITHUB_ACTOR_LOGIN,
            "type": module.GITHUB_ACTOR_TYPE,
        },
        f"/repositories/{module.EXECUTOR_REPOSITORY_ID}": {
            "id": module.EXECUTOR_REPOSITORY_ID,
            "node_id": module.EXECUTOR_REPOSITORY_NODE_ID,
            "full_name": module.EXECUTOR_REPOSITORY,
            "default_branch": "main",
            "archived": False,
            "disabled": False,
        },
        f"/repositories/{module.EXECUTOR_REPOSITORY_ID}/commits/main": {"sha": EXECUTOR_HEAD},
        module._content_endpoint(module.EXECUTOR_REPOSITORY_ID, module.EXECUTOR_PATH, EXECUTOR_HEAD): encoded(module.EXECUTOR_PATH, LOCAL_BYTES),
        module._content_endpoint(module.EXECUTOR_REPOSITORY_ID, module.ATTESTATION_POLICY_PATH, EXECUTOR_HEAD): encoded(module.ATTESTATION_POLICY_PATH, ATTESTATION_POLICY_BYTES),
        f"/repositories/{module.DOCTRINE_REPOSITORY_ID}": {
            "id": module.DOCTRINE_REPOSITORY_ID,
            "full_name": module.DOCTRINE_REPOSITORY,
            "default_branch": "main",
            "archived": False,
            "disabled": False,
        },
        f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/commits/main": {"sha": DOCTRINE_HEAD},
        module._content_endpoint(module.DOCTRINE_REPOSITORY_ID, module.DOCTRINE_RECORD_PATH, DOCTRINE_HEAD): encoded(module.DOCTRINE_RECORD_PATH, canonical_file(record)),
        f"/orgs/{module.ORGANIZATION}": {"id": module.ORGANIZATION_ID, "login": module.ORGANIZATION},
        f"/repositories/{module.TARGET_REPOSITORY_ID}": {
            "id": module.TARGET_REPOSITORY_ID,
            "node_id": module.TARGET_REPOSITORY_NODE_ID,
            "full_name": module.TARGET_REPOSITORY_NAMES[0],
            "default_branch": "main",
            "visibility": "private",
        },
        f"/repositories/{module.EXECUTOR_REPOSITORY_ID}/commits/{SOURCE_SHA}": {"sha": SOURCE_SHA},
        f"/repositories/{module.EXECUTOR_REPOSITORY_ID}/compare/{SOURCE_SHA}...main": {
            "status": "ahead",
            "base_commit": {"sha": SOURCE_SHA},
        },
        module._content_endpoint(module.EXECUTOR_REPOSITORY_ID, module.WORKFLOW_PATH, SOURCE_SHA): encoded(module.WORKFLOW_PATH, workflow),
        module._content_endpoint(module.EXECUTOR_REPOSITORY_ID, module.VALIDATOR_PATH, SOURCE_SHA): encoded(module.VALIDATOR_PATH, b"export {};\n"),
        module._content_endpoint(module.EXECUTOR_REPOSITORY_ID, module.POLICY_PATH, SOURCE_SHA): encoded(module.POLICY_PATH, canonical_file(source_policy)),
    })
    ruleset_summaries = [{"id": ATTESTATION_RULESET_ID, "name": module.ATTESTATION_RULESET_NAME}]
    if live is not None:
        ruleset_summaries.insert(0, {"id": live["id"], "name": live["name"]})
    api.page_values[f"/orgs/{module.ORGANIZATION}/rulesets"] = ruleset_summaries
    attestation_ruleset = live_attestation_ruleset()
    organization_attestation_ruleset = copy.deepcopy(attestation_ruleset)
    organization_attestation_ruleset.pop("current_user_can_bypass")
    api.gets[f"/orgs/{module.ORGANIZATION}/rulesets/{ATTESTATION_RULESET_ID}"] = organization_attestation_ruleset
    api.gets[
        f"/repos/{module.EXECUTOR_REPOSITORY}/rulesets/{ATTESTATION_RULESET_ID}?includes_parents=true"
    ] = copy.deepcopy(attestation_ruleset)
    if live is not None:
        ruleset_id = live["id"]
        api.gets[f"/orgs/{module.ORGANIZATION}/rulesets/{ruleset_id}"] = live
        api.page_values[f"/repositories/{module.TARGET_REPOSITORY_ID}/rulesets?includes_parents=true"] = [{"id": ruleset_id}] if effective else []
    return api


def live_ruleset(record: dict, *, enforcement: str | None = None) -> dict:
    payload = module.expected_ruleset(record, enforcement=enforcement)
    return {**payload, "id": record["ruleset"]["rulesetId"], "source_type": "Organization", "updated_at": "2026-07-11T21:00:00Z"}


def bindings(record: dict, head: str | None = None, suite: int | None = None) -> dict:
    return {
        "rulesetId": record["ruleset"]["rulesetId"],
        "targetRepositoryId": module.TARGET_REPOSITORY_ID,
        "sourceRepositoryId": module.EXECUTOR_REPOSITORY_ID,
        "sourceCommitSha": SOURCE_SHA,
        "headSha": head,
        "ruleSuiteId": suite,
    }


def configure_workflow_evidence(
    api: FakeAPI,
    record: dict,
    *,
    field: str,
    run_id: int,
    suite_id: int,
    head_sha: str,
    observed_at: str,
) -> dict:
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))

    def provider_time(offset_seconds: int) -> str:
        return (observed + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")

    event = "merge_group" if field == "mergeGroupCanary" else "pull_request"
    conclusion = "failure" if field == "negativeControl" else "success"
    result = "fail" if field == "negativeControl" else "pass"
    run = {
        "id": run_id,
        "run_attempt": 1,
        "repository": {"id": module.TARGET_REPOSITORY_ID},
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": head_sha,
        "path": module.WORKFLOW_PATH,
        "created_at": provider_time(5),
        "updated_at": provider_time(10),
    }
    job = {
        "id": run_id * 10,
        "name": module.REQUIRED_CHECK,
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": head_sha,
    }
    evaluation = {
        "rule_source": {"type": "Organization", "id": record["ruleset"]["rulesetId"], "name": module.RULESET_NAME},
        "rule_type": "workflows",
        "enforcement": "evaluate",
        "result": result,
    }
    suite = {
        "id": suite_id,
        "repository_id": module.TARGET_REPOSITORY_ID,
        "after_sha": head_sha,
        "ref": "refs/heads/main",
        "evaluation_result": result,
        "pushed_at": observed_at,
        "rule_evaluations": [evaluation],
    }
    api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/actions/runs/{run_id}"] = run
    api.gets[
        f"/repositories/{module.TARGET_REPOSITORY_ID}/actions/runs/{run_id}/jobs"
        "?filter=latest&per_page=100&page=1"
    ] = {"total_count": 1, "jobs": [job]}
    api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/rulesets/rule-suites/{suite_id}"] = suite
    evidence = {
        "kind": module.EVIDENCE_KINDS[field],
        "locator": f"https://github.com/{module.TARGET_REPOSITORY_NAMES[0]}/actions/runs/{run_id}",
        "observedAt": observed_at,
        "subjectDigest": "sha256:" + "0" * 64,
        "bindings": bindings(record, head_sha, suite_id),
        "negativeControl": None,
    }
    suite_observation = {
        "id": suite_id,
        "repositoryId": module.TARGET_REPOSITORY_ID,
        "afterSha": head_sha,
        "ref": "refs/heads/main",
        "evaluationResult": result,
        "pushedAt": observed_at,
        "ruleEvaluation": evaluation,
    }
    return evidence, run, job, module._run_observation(run, job, suite_observation)


def active_fixture() -> tuple[dict, FakeAPI]:
    record = base_record(phase="ratchet", ruleset_id=321, enforcement="active")
    live = live_ruleset(record, enforcement="evaluate")
    api = base_api(record, live=live)
    target_name = module.TARGET_REPOSITORY_NAMES[0]
    effective = [{
        "repositoryId": module.TARGET_REPOSITORY_ID,
        "rulesetId": 321,
        "rulesetPresent": True,
    }]
    evaluate = {
        "kind": "ruleset-readback",
        "locator": f"https://github.com/organizations/{module.ORGANIZATION}/settings/rules/321",
        "observedAt": "2026-07-11T21:00:00Z",
        "subjectDigest": module.canonical_digest(module.expected_ruleset(record, enforcement="evaluate")),
        "bindings": bindings(record),
        "negativeControl": None,
    }
    pull, _, _, pull_observation = configure_workflow_evidence(
        api, record, field="pullRequestCanary", run_id=101, suite_id=201,
        head_sha="7" * 40, observed_at="2026-07-11T21:01:00Z",
    )
    pull["subjectDigest"] = module.canonical_digest(pull_observation)
    merge, _, _, merge_observation = configure_workflow_evidence(
        api, record, field="mergeGroupCanary", run_id=102, suite_id=202,
        head_sha="8" * 40, observed_at="2026-07-11T21:02:00Z",
    )
    merge["subjectDigest"] = module.canonical_digest(merge_observation)
    negative, negative_run, negative_job, negative_observation = configure_workflow_evidence(
        api, record, field="negativeControl", run_id=103, suite_id=203,
        head_sha=FIXTURE_HEAD, observed_at="2026-07-11T21:03:00Z",
    )

    package_base = {
        "name": "skills",
        "scripts": {"check": "node scripts/check.mjs", "verify:install": "node scripts/install.mjs"},
    }
    package_head = copy.deepcopy(package_base)
    package_head["scripts"].update(record["workflowSource"]["negativeControlPolicy"]["scriptOverrides"])
    base_bytes = canonical_file(package_base)
    head_bytes = canonical_file(package_head)
    comparison_files = [{
        "filename": "package.json", "status": "modified", "sha": "9" * 40,
        "previous_filename": None, "additions": 2, "deletions": 2, "changes": 4,
        "patch": "@@ scripts @@",
    }]
    comparison_raw = {
        "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
        "base_commit": {"sha": FIXTURE_BASE}, "merge_base_commit": {"sha": FIXTURE_BASE},
        "commits": [{"sha": FIXTURE_HEAD}], "files": comparison_files,
    }
    comparison = module._normalized_comparison(comparison_raw)
    pr_files_raw = [
        *comparison_files,
        {"filename": "README.md", "status": "modified", "sha": "a" * 40, "previous_filename": None, "additions": 1, "deletions": 0, "changes": 1, "patch": "@@ launch @@"},
    ]
    pr_files = module._normalized_pull_files(pr_files_raw)
    merge_commit = {"sha": MERGE_SHA, "tree": {"sha": FIXTURE_HEAD_TREE}, "parents": [{"sha": PR_BASE}, {"sha": FIXTURE_HEAD}]}
    normalized_merge = module._normalized_commit(merge_commit)
    checks_raw = [
        {"id": 1, "name": module.LOCAL_REQUIRED_CHECKS[0], "status": "completed", "conclusion": "success", "head_sha": FIXTURE_HEAD, "details_url": "https://github.com/local/1", "app": {"id": 1, "slug": "github-actions"}},
        {"id": 2, "name": module.LOCAL_REQUIRED_CHECKS[1], "status": "completed", "conclusion": "success", "head_sha": FIXTURE_HEAD, "details_url": "https://github.com/local/2", "app": {"id": 1, "slug": "github-actions"}},
        {"id": 3, "name": module.REQUIRED_CHECK, "status": "completed", "conclusion": "failure", "head_sha": FIXTURE_HEAD, "details_url": f"https://github.com/{target_name}/actions/runs/103/job/1030", "app": {"id": 1, "slug": "github-actions"}},
    ]
    normalized_checks = sorted((module._normalized_check(item) for item in checks_raw), key=lambda item: (str(item["name"]), str(item["id"])))
    proof = {
        "pullRequestNumber": 77,
        "targetRef": "refs/heads/main",
        "headRef": "refs/heads/canary/negative",
        "fixtureBaseSha": FIXTURE_BASE,
        "fixtureBaseTree": FIXTURE_BASE_TREE,
        "fixtureHeadSha": FIXTURE_HEAD,
        "fixtureHeadTree": FIXTURE_HEAD_TREE,
        "pullRequestMergeCommitSha": MERGE_SHA,
        "mutationClass": "package-script-neutralization",
        "mutationPath": "package.json",
        "fixtureDigest": module.exact_digest(head_bytes),
        "fixtureSemanticDigest": module.canonical_digest(package_head),
        "fixtureComparisonDigest": module.canonical_digest(comparison),
        "pullRequestFilesDigest": module.canonical_digest(pr_files),
        "mergeCommitDigest": module.canonical_digest(normalized_merge),
        "contextsDigest": module.canonical_digest(normalized_checks),
    }
    negative["negativeControl"] = proof
    api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/pulls/77"] = {
        "number": 77, "merged_at": None, "merge_commit_sha": MERGE_SHA,
        "base": {"ref": "main", "sha": PR_BASE, "repo": {"id": module.TARGET_REPOSITORY_ID}},
        "head": {"ref": "canary/negative", "sha": FIXTURE_HEAD, "repo": {"id": module.TARGET_REPOSITORY_ID}},
    }
    api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/git/commits/{FIXTURE_BASE}"] = {"sha": FIXTURE_BASE, "tree": {"sha": FIXTURE_BASE_TREE}, "parents": []}
    api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/git/commits/{FIXTURE_HEAD}"] = {"sha": FIXTURE_HEAD, "tree": {"sha": FIXTURE_HEAD_TREE}, "parents": [{"sha": FIXTURE_BASE}]}
    api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/git/commits/{MERGE_SHA}"] = merge_commit
    api.page_values[f"/repositories/{module.TARGET_REPOSITORY_ID}/pulls/77/files"] = pr_files_raw
    api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/compare/{FIXTURE_BASE}...{FIXTURE_HEAD}"] = comparison_raw
    api.gets[module._content_endpoint(module.TARGET_REPOSITORY_ID, "package.json", FIXTURE_BASE)] = encoded("package.json", base_bytes)
    api.gets[module._content_endpoint(module.TARGET_REPOSITORY_ID, "package.json", FIXTURE_HEAD)] = encoded("package.json", head_bytes)
    api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/commits/{FIXTURE_HEAD}/check-runs?filter=latest&per_page=100"] = {"total_count": len(checks_raw), "check_runs": checks_raw}
    negative_detail = {
        "pullRequestNumber": 77, "targetRef": "refs/heads/main", "headRef": "refs/heads/canary/negative",
        "pullRequestBaseSha": PR_BASE, "fixtureBaseSha": FIXTURE_BASE,
        "fixtureBaseTree": FIXTURE_BASE_TREE, "fixtureHeadSha": FIXTURE_HEAD,
        "fixtureHeadTree": FIXTURE_HEAD_TREE,
        "sourceBaseline": {"repositoryId": module.TARGET_REPOSITORY_ID, "commit": FIXTURE_BASE, "tree": FIXTURE_BASE_TREE},
        "mutationClass": "package-script-neutralization", "mutationPath": "package.json",
        "comparisonDigest": module.canonical_digest(comparison),
        "pullRequestFilesDigest": module.canonical_digest(pr_files),
        "mergeCommit": normalized_merge, "fixtureDigest": module.exact_digest(head_bytes),
        "fixtureSemanticDigest": module.canonical_digest(package_head), "contexts": normalized_checks,
    }
    negative_observation["negativeControl"] = negative_detail
    negative["subjectDigest"] = module.canonical_digest(negative_observation)
    record["activationEvidence"] = {
        "evaluateReadback": evaluate,
        "pullRequestCanary": pull,
        "mergeGroupCanary": merge,
        "negativeControl": negative,
        "effectiveRulesReadback": {
            "kind": "effective-rules-readback",
            "locator": f"https://github.com/{target_name}/settings/rules",
            "observedAt": "2026-07-11T21:04:00Z",
            "subjectDigest": module.canonical_digest(effective),
            "bindings": bindings(record),
            "negativeControl": None,
        },
        "activationTransition": None,
    }
    api.gets[module._content_endpoint(module.DOCTRINE_REPOSITORY_ID, module.DOCTRINE_RECORD_PATH, DOCTRINE_HEAD)] = encoded(module.DOCTRINE_RECORD_PATH, canonical_file(record))
    return record, api


def configure_historical_authority(api: FakeAPI, report: dict, record: dict) -> None:
    desired_commit = report["desiredState"]["commitSha"]
    executor_commit = report["executor"]["commitSha"]
    api.gets[f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/commits/{desired_commit}"] = {
        "sha": desired_commit,
    }
    api.gets[
        f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/compare/{desired_commit}...main"
    ] = {"status": "ahead", "base_commit": {"sha": desired_commit}}
    api.gets[module._content_endpoint(
        module.DOCTRINE_REPOSITORY_ID,
        module.DOCTRINE_RECORD_PATH,
        desired_commit,
    )] = encoded(module.DOCTRINE_RECORD_PATH, canonical_file(record))
    api.gets[f"/repositories/{module.EXECUTOR_REPOSITORY_ID}/commits/{executor_commit}"] = {
        "sha": executor_commit,
    }
    api.gets[
        f"/repositories/{module.EXECUTOR_REPOSITORY_ID}/compare/{executor_commit}...main"
    ] = {"status": "ahead", "base_commit": {"sha": executor_commit}}
    api.gets[module._content_endpoint(
        module.EXECUTOR_REPOSITORY_ID,
        module.EXECUTOR_PATH,
        executor_commit,
    )] = encoded(module.EXECUTOR_PATH, LOCAL_BYTES)
    api.gets[module._content_endpoint(
        module.EXECUTOR_REPOSITORY_ID,
        module.ATTESTATION_POLICY_PATH,
        executor_commit,
    )] = encoded(module.ATTESTATION_POLICY_PATH, ATTESTATION_POLICY_BYTES)


def provider_audit_event(report: dict, **extra: object) -> dict:
    return {
        "_document_id": "audit-document-1",
        "action": module.AUDIT_ACTION,
        "actor": module.GITHUB_ACTOR_LOGIN,
        "actor_id": module.GITHUB_ACTOR_ID,
        "created_at": int(FIXED_TIME.timestamp() * 1000),
        "operation_type": "modify",
        "org": module.ORGANIZATION,
        "org_id": module.ORGANIZATION_ID,
        "request_id": report["mutation"]["requestId"],
        "ruleset_enforcement": "enabled",
        "ruleset_id": report["mutation"]["rulesetId"],
        "ruleset_name": module.RULESET_NAME,
        "ruleset_source_type": "Organization",
        **extra,
    }


def write_report(report: dict) -> tempfile.NamedTemporaryFile:
    handle = tempfile.NamedTemporaryFile("wb", delete=False)
    handle.write(module.canonical_bytes(report))
    handle.close()
    return handle


def collected_fixture() -> tuple[dict, dict, dict, FakeAPI]:
    record, api = active_fixture()
    executor = module.RulesetExecutor(
        api,
        LOCAL_BYTES,
        clock=lambda: FIXED_TIME,
        nonce_factory=lambda: "e" * 64,
        sleeper=lambda _seconds: None,
    )
    report = executor.run("apply")
    configure_historical_authority(api, report, record)
    api.gets[module._audit_endpoint(1)] = [provider_audit_event(
        report,
        actor_location={"country_code": "GB"},
        hashed_token="must-never-persist",
        token_scopes="repo,admin:org",
        user_agent="sensitive-agent",
        request_headers={"authorization": "Bearer secret"},
    )]
    handle = write_report(report)
    try:
        artifact = executor.collect_transition(Path(handle.name))
    finally:
        os.unlink(handle.name)
    return record, report, artifact, api


def sealed_active_fixture() -> tuple[dict, dict, dict, FakeAPI]:
    historical, report, artifact, api = collected_fixture()
    artifact_raw = canonical_file(artifact)
    transition = module.activation_transition_from_artifact(
        artifact,
        artifact_raw=artifact_raw,
        artifact_blob_sha=module.git_blob_sha(artifact_raw),
    )
    active = copy.deepcopy(historical)
    active["migration"]["phase"] = "active"
    active["activationEvidence"]["activationTransition"] = transition
    api.gets[f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/commits/main"] = {
        "sha": ACTIVE_DOCTRINE_HEAD,
    }
    api.gets[module._content_endpoint(
        module.DOCTRINE_REPOSITORY_ID,
        module.DOCTRINE_RECORD_PATH,
        ACTIVE_DOCTRINE_HEAD,
    )] = encoded(module.DOCTRINE_RECORD_PATH, canonical_file(active))
    api.gets[module._content_endpoint(
        module.DOCTRINE_REPOSITORY_ID,
        module.ACTIVATION_EVIDENCE_PATH,
        ACTIVE_DOCTRINE_HEAD,
    )] = encoded(module.ACTIVATION_EVIDENCE_PATH, artifact_raw)
    return active, report, artifact, api


class ContractTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_float_wide_integer_bom_and_depth(self) -> None:
        bad = [b'{"a":1,"a":2}', b'{"a":1.5}', b'{"a":9007199254740992}', b'\xef\xbb\xbf{}']
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(module.ContractError):
                    module.strict_json_loads(raw)
        value: object = 1
        for _ in range(module.MAX_JSON_DEPTH + 2):
            value = [value]
        with self.assertRaises(module.ContractError):
            module.canonical_bytes(value)

    def test_contents_api_accepts_only_provider_wrapped_base64(self) -> None:
        raw = (b"GitHub Contents API wrapped base64\n" * 8) + b"end"
        item = encoded("proof.txt", raw)
        compact = item["content"]
        item["content"] = "\n".join(
            compact[index:index + 60]
            for index in range(0, len(compact), 60)
        ) + "\n"
        self.assertEqual(module._decode_content(item, "proof.txt", "provider file"), raw)

        for bad in (
            compact[:8] + " " + compact[8:],
            compact[:8] + "\t" + compact[8:],
            compact[:8] + "\r\n" + compact[8:],
            "\n" + compact,
            compact[:8] + "\n\n" + compact[8:],
        ):
            poisoned = copy.deepcopy(item)
            poisoned["content"] = bad
            with self.subTest(bad=repr(bad[:20])), self.assertRaises(module.ForgeError):
                module._decode_content(poisoned, "proof.txt", "provider file")

    def test_candidate_authority_and_unknown_fields_are_rejected(self) -> None:
        record = base_record()
        record["applyAuthority"] = {"mode": "candidate-controls-executor"}
        with self.assertRaisesRegex(module.ContractError, "unsupported members"):
            module.validate_record(record)

    def test_immutable_identity_surface_is_closed(self) -> None:
        mutations = [
            ("organization", "Attacker"),
            ("ruleset.bypassActors", [{"actor_id": 1}]),
            ("ruleset.refInclude", ["~ALL"]),
            ("workflowSource.repositoryId", 1),
            ("workflowSource.workflowPath", ".github/workflows/evil.yml"),
            ("ruleset.targetRepositories.0.repositoryId", 999999999),
        ]
        for path, value in mutations:
            record = base_record()
            target: object = record
            parts = path.split(".")
            for part in parts[:-1]:
                target = target[int(part)] if part.isdigit() else target[part]  # type: ignore[index]
            if parts[-1].isdigit():
                target[int(parts[-1])] = value  # type: ignore[index]
            else:
                target[parts[-1]] = value  # type: ignore[index]
            with self.subTest(path=path), self.assertRaises(module.ContractError):
                module.validate_record(record)

    def test_payload_is_constructed_from_safety_invariants(self) -> None:
        record = base_record()
        expected = module.expected_ruleset(record)
        self.assertEqual(expected["conditions"]["repository_id"], {"repository_ids": [module.TARGET_REPOSITORY_ID]})
        self.assertEqual(expected["bypass_actors"], [])
        self.assertEqual(expected["rules"][0]["parameters"]["workflows"], [{
            "path": module.WORKFLOW_PATH,
            "repository_id": module.EXECUTOR_REPOSITORY_ID,
            "ref": "main",
            "sha": SOURCE_SHA,
        }])


class ExecutionTests(unittest.TestCase):
    def executor(self, api: FakeAPI, nonce: str | None = None) -> module.RulesetExecutor:
        nonce_factory = (lambda: nonce) if nonce is not None else None
        return module.RulesetExecutor(
            api,
            LOCAL_BYTES,
            clock=lambda: FIXED_TIME,
            nonce_factory=nonce_factory,
            sleeper=lambda _seconds: None,
        )

    def test_default_dry_run_plans_create_without_mutation_or_doctrine_code_fetch(self) -> None:
        record = base_record()
        api = base_api(record)
        report = self.executor(api).run("dry-run")
        self.assertEqual(report["status"], "DRIFT")
        self.assertEqual(report["plannedMutation"]["action"], "create")
        self.assertIsNone(report["applyLock"])
        self.assertEqual(report["actor"], {
            "id": module.GITHUB_ACTOR_ID,
            "login": module.GITHUB_ACTOR_LOGIN,
            "type": module.GITHUB_ACTOR_TYPE,
        })
        self.assertFalse(api.mutations)
        self.assertFalse(api.lock_mutations)
        doctrine_gets = [item for item in api.get_calls if f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/" in item]
        self.assertEqual(doctrine_gets, [
            f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/commits/main",
            module._content_endpoint(module.DOCTRINE_REPOSITORY_ID, module.DOCTRINE_RECORD_PATH, DOCTRINE_HEAD),
        ])
        self.assertNotIn("token", json.dumps(report).lower())

    def test_readback_never_plans_or_mutates(self) -> None:
        api = base_api(base_record())
        report = self.executor(api).run("readback")
        self.assertEqual(report["status"], "DRIFT")
        self.assertIsNone(report["plannedMutation"])
        self.assertIsNone(report["applyLock"])
        self.assertFalse(api.mutations)
        self.assertFalse(api.lock_mutations)

    def test_apply_creates_evaluate_then_requires_doctrine_id_ratchet(self) -> None:
        record = base_record()
        api = base_api(record)
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "DRIFT")
        self.assertEqual(report["mutation"], {"attempted": True, "action": "create", "outcome": "created", "rulesetId": 321})
        self.assertEqual([item[0] for item in api.mutations], ["POST"])
        self.assertEqual(api.mutations[0][2], module.expected_ruleset(record))
        self.assertGreaterEqual(api.get_calls.count(f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/commits/main"), 2)

    def test_post_create_readback_mismatch_is_not_success(self) -> None:
        api = base_api(base_record())
        api.post_readback_override = {"id": 321, "name": "wrong"}
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "ERROR")
        self.assertEqual(report["mutation"]["outcome"], "created-post-readback-mismatch")
        self.assertEqual(len(api.mutations), 1)

    def test_exact_reconcile_readback_passes_without_write(self) -> None:
        record = base_record(phase="reconcile", ruleset_id=321)
        api = base_api(record, live=live_ruleset(record))
        report = self.executor(api).run("dry-run")
        self.assertEqual(report["status"], "PASS")
        self.assertFalse(api.mutations)

    def test_non_recovery_phase_cannot_downgrade_active_enforcement(self) -> None:
        record = base_record(phase="reconcile", ruleset_id=321, enforcement="evaluate")
        api = base_api(record, live=live_ruleset(record, enforcement="active"))
        with self.assertRaisesRegex(module.ForgeError, "cannot downgrade"):
            self.executor(api).run("apply")
        self.assertFalse(api.mutations)

    def test_recovery_is_enforcement_only_and_never_delete_or_escalate(self) -> None:
        record = base_record(phase="recovery", ruleset_id=321, enforcement="disabled")
        record["recovery"] = {
            "reason": "Emergency enforcement downgrade",
            "tracker": "https://github.com/SylphxAI/.github/issues/1",
            "initiatedAt": "2026-07-11T21:00:00Z",
        }
        api = base_api(record, live=live_ruleset(record, enforcement="active"))
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual([item[0] for item in api.mutations], ["PUT"])
        self.assertEqual(api.mutations[0][2]["enforcement"], "disabled")
        self.assertEqual(api.mutations[0][2]["bypass_actors"], [])

        escalation = copy.deepcopy(record)
        escalation["ruleset"]["enforcement"] = "evaluate"
        api2 = base_api(escalation, live=live_ruleset(escalation, enforcement="disabled"))
        with self.assertRaisesRegex(module.ForgeError, "cannot escalate"):
            self.executor(api2).run("apply")
        self.assertFalse(api2.mutations)

    def test_active_transition_reverifies_all_live_evidence_then_updates(self) -> None:
        record, api = active_fixture()
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "APPLIED_PENDING_EVIDENCE")
        self.assertEqual(report["activationReadback"]["canaryNotBefore"], "2026-07-11T21:00:00Z")
        self.assertEqual(
            sorted(report["activationReadback"]["workflowEvidence"]),
            ["mergeGroupCanary", "negativeControl", "pullRequestCanary"],
        )
        self.assertEqual([item[0] for item in api.mutations], ["PUT"])
        self.assertEqual(api.mutations[0][2], module.expected_ruleset(record))
        self.assertEqual(report["applyLock"]["repositoryId"], module.EXECUTOR_REPOSITORY_ID)
        self.assertEqual(report["applyLock"]["ref"], module.APPLY_LOCK_REF)
        self.assertEqual(report["applyLock"]["executorCommitSha"], EXECUTOR_HEAD)
        self.assertRegex(report["applyLock"]["nonce"], r"^[0-9a-f]{64}$")
        self.assertRegex(report["applyLock"]["tagObjectSha"], r"^[0-9a-f]{40}$")
        self.assertRegex(report["applyLock"]["tagMessageDigest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(report["applyLock"]["acquireOutcome"], "acquired")
        self.assertEqual(report["applyLock"]["releaseOutcome"], "released")
        self.assertEqual(report["applyLock"]["finalRefAbsentAt"], "2026-07-11T22:00:00Z")
        lock_claim = json.loads(api.git_tags[report["applyLock"]["tagObjectSha"]]["message"])
        self.assertEqual(
            lock_claim["authorization"]["attestationRuleset"],
            report["attestationRuleset"],
        )
        self.assertNotIn(module.APPLY_LOCK_REF, api.git_refs)

    def test_apply_lock_orders_authority_before_doctrine_and_holds_through_post_readback(self) -> None:
        record, api = active_fixture()
        self.executor(api, "a" * 64).run("apply")
        tag_post = api.events.index(("POST", module.APPLY_LOCK_TAGS_ENDPOINT))
        ref_post = api.events.index(("POST", module.APPLY_LOCK_REFS_ENDPOINT))
        doctrine_reads = [
            index
            for index, event in enumerate(api.events)
            if event == ("GET", f"/repositories/{module.DOCTRINE_REPOSITORY_ID}")
        ]
        self.assertEqual(len(doctrine_reads), 4)
        phase_probe, doctrine_read, finalizer_pre_read, finalizer_post_read = doctrine_reads
        ruleset_put = api.events.index(("PUT", f"/orgs/{module.ORGANIZATION}/rulesets/321"))
        release = api.events.index(("DELETE", module.APPLY_LOCK_REF_DELETE_ENDPOINT))
        post_readback = max(
            index
            for index, event in enumerate(api.events)
            if event == ("GET", f"/orgs/{module.ORGANIZATION}/rulesets/321")
            and index < release
        )
        self.assertLess(api.events.index(("GET", "/user")), tag_post)
        self.assertLess(phase_probe, tag_post)
        self.assertLess(tag_post, ref_post)
        self.assertLess(ref_post, doctrine_read)
        self.assertLess(doctrine_read, ruleset_put)
        self.assertLess(ruleset_put, post_readback)
        self.assertLess(post_readback, release)
        self.assertLess(release, finalizer_pre_read)
        self.assertLess(finalizer_pre_read, finalizer_post_read)

    def test_two_logical_executors_contend_with_distinct_fencing_tokens(self) -> None:
        record, api = active_fixture()
        first = self.executor(api, "1" * 64)
        first._verify_executor()
        actor = first._verify_actor()
        first_lock = first._acquire_apply_lock(actor, "2026-07-11T22:00:00Z", {})
        try:
            second = self.executor(api, "2" * 64)
            with self.assertRaisesRegex(module.ForgeError, "held by another acquisition"):
                second.run("apply")
            self.assertFalse(api.mutations)
            self.assertEqual(len(api.git_tags), 2)
            self.assertNotEqual(first_lock["tagObjectSha"], next(
                tag_sha for tag_sha in api.git_tags if tag_sha != first_lock["tagObjectSha"]
            ))
            self.assertFalse(any(method == "DELETE" for method, _, _ in api.lock_mutations))
        finally:
            first._release_apply_lock(first_lock)

    def test_acquire_response_mismatch_releases_only_owned_ref_and_never_mutates_ruleset(self) -> None:
        record, api = active_fixture()
        original = api.post_created

        def mismatched_create(endpoint: str, payload: dict) -> dict:
            response = original(endpoint, payload)
            if endpoint == module.APPLY_LOCK_REFS_ENDPOINT:
                response["object"]["sha"] = "f" * 40
            return response

        setattr(api, "post_created", mismatched_create)
        with self.assertRaisesRegex(module.ForgeError, "failed closed"):
            self.executor(api, "3" * 64).run("apply")
        self.assertFalse(api.mutations)
        self.assertNotIn(module.APPLY_LOCK_REF, api.git_refs)
        self.assertEqual(
            [method for method, _, _ in api.lock_mutations],
            ["POST", "POST", "DELETE"],
        )

    def test_create_ref_transport_uncertainty_reconciles_and_releases_only_owned_ref(self) -> None:
        record, api = active_fixture()
        original = api.post_created

        def create_then_disconnect(endpoint: str, payload: dict) -> dict:
            response = original(endpoint, payload)
            if endpoint == module.APPLY_LOCK_REFS_ENDPOINT:
                raise module.ForgeError("injected transport loss after create")
            return response

        setattr(api, "post_created", create_then_disconnect)
        with self.assertRaisesRegex(module.ForgeError, "failed closed"):
            self.executor(api, "7" * 64).run("apply")
        self.assertFalse(api.mutations)
        self.assertNotIn(module.APPLY_LOCK_REF, api.git_refs)
        self.assertEqual(
            [method for method, _, _ in api.lock_mutations],
            ["POST", "POST", "DELETE"],
        )

    def test_delete_transport_uncertainty_accepts_only_proven_absence(self) -> None:
        record, api = active_fixture()
        original = api.delete

        def delete_then_disconnect(endpoint: str) -> None:
            original(endpoint)
            raise module.ForgeError("injected transport loss after delete")

        setattr(api, "delete", delete_then_disconnect)
        report = self.executor(api, "8" * 64).run("apply")
        self.assertEqual(report["status"], "APPLIED_PENDING_EVIDENCE")
        self.assertEqual(report["mutation"]["outcome"], "updated")
        self.assertEqual(report["applyLock"]["releaseOutcome"], "released")
        self.assertNotIn(module.APPLY_LOCK_REF, api.git_refs)

    def test_delete_uncertainty_never_deletes_or_accepts_successor_ref(self) -> None:
        record, api = active_fixture()
        original = api.delete
        successor_sha = "d" * 40

        def delete_then_install_successor(endpoint: str) -> None:
            original(endpoint)
            api.git_refs[module.APPLY_LOCK_REF] = {
                "ref": module.APPLY_LOCK_REF,
                "object": {"type": "tag", "sha": successor_sha},
            }
            raise module.ForgeError("injected successor race after delete")

        setattr(api, "delete", delete_then_install_successor)
        report = self.executor(api, "9" * 64).run("apply")
        self.assertEqual(report["status"], "ERROR")
        self.assertEqual(report["mutation"]["outcome"], "updated")
        self.assertEqual(report["applyLock"]["releaseOutcome"], "failed-or-uncertain")
        self.assertEqual(api.git_refs[module.APPLY_LOCK_REF]["object"]["sha"], successor_sha)
        self.assertEqual(
            sum(method == "DELETE" for method, _, _ in api.lock_mutations),
            1,
        )

    def test_ruleset_mutation_failure_releases_lock(self) -> None:
        record, api = active_fixture()

        def fail_put(endpoint: str, payload: dict) -> dict:
            api.events.append(("PUT", endpoint))
            raise module.ForgeError("injected ruleset mutation failure")

        setattr(api, "put", fail_put)
        report = self.executor(api, "4" * 64).run("apply")
        self.assertEqual(report["status"], "ERROR")
        self.assertEqual(report["applyLock"]["releaseOutcome"], "released")
        self.assertNotIn(module.APPLY_LOCK_REF, api.git_refs)

    def test_release_failure_preserves_mutation_evidence_and_returns_error(self) -> None:
        record, api = active_fixture()
        api.delete_overrides[module.APPLY_LOCK_REF_DELETE_ENDPOINT] = module.ForgeError(
            "injected release failure"
        )
        report = self.executor(api, "5" * 64).run("apply")
        self.assertEqual(report["status"], "ERROR")
        self.assertEqual(report["mutation"]["outcome"], "updated")
        self.assertIsNotNone(report["postReadback"])
        self.assertEqual(report["applyLock"]["releaseOutcome"], "failed-or-uncertain")
        self.assertIn(module.APPLY_LOCK_REF, api.git_refs)

    def test_stale_owner_never_deletes_successor_lock(self) -> None:
        api = base_api(base_record())
        owner = self.executor(api, "6" * 64)
        owner._verify_executor()
        lock = owner._acquire_apply_lock(owner._verify_actor(), "2026-07-11T22:00:00Z", {})
        successor_sha = "e" * 40
        api.git_refs[module.APPLY_LOCK_REF] = {
            "ref": module.APPLY_LOCK_REF,
            "object": {"type": "tag", "sha": successor_sha},
        }
        with self.assertRaisesRegex(module.ForgeError, "ownership was lost"):
            owner._release_apply_lock(lock)
        self.assertEqual(api.git_refs[module.APPLY_LOCK_REF]["object"]["sha"], successor_sha)
        self.assertFalse(any(method == "DELETE" for method, _, _ in api.lock_mutations))

    def test_lock_has_no_expiry_steal_force_or_input_override_surface(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ["lock_ttl", "expiresAt", "def patch(", '"force":', "--lock-ref", "steal_lock"]:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertEqual(
            module.APPLY_LOCK_REF,
            "refs/tags/sylph-locks/public-skills-ruleset-executor",
        )

    def test_actions_jobs_must_use_exact_bounded_object_shape(self) -> None:
        endpoint = (
            f"/repositories/{module.TARGET_REPOSITORY_ID}/actions/runs/101/jobs"
            "?filter=latest&per_page=100&page=1"
        )
        malformed = [
            [],
            {"total_count": 1, "jobs": []},
            {"total_count": 101, "jobs": []},
            {"total_count": 0, "jobs": [], "next": "attacker-controlled"},
        ]
        for response in malformed:
            record, api = active_fixture()
            api.gets[endpoint] = response
            with self.subTest(response=response), self.assertRaises(module.ForgeError):
                self.executor(api).run("apply")
            self.assertFalse(api.mutations)

    def test_provider_timestamps_must_be_strict_rfc3339(self) -> None:
        record, api = active_fixture()
        endpoint = f"/repositories/{module.TARGET_REPOSITORY_ID}/actions/runs/101"
        api.gets[endpoint]["created_at"] = "2026-07-11 21:00:10+00:00"
        with self.assertRaisesRegex(module.ForgeError, "RFC3339"):
            self.executor(api).run("apply")
        self.assertFalse(api.mutations)

    def test_run_cannot_predate_its_bound_rule_suite(self) -> None:
        record, api = active_fixture()
        endpoint = f"/repositories/{module.TARGET_REPOSITORY_ID}/actions/runs/101"
        api.gets[endpoint]["created_at"] = "2026-07-11T21:00:45Z"
        with self.assertRaisesRegex(module.ForgeError, "provider chronology is inconsistent"):
            self.executor(api).run("apply")
        self.assertFalse(api.mutations)

    def test_rule_suite_requires_real_pushed_at_provider_timestamp(self) -> None:
        endpoint = f"/repositories/{module.TARGET_REPOSITORY_ID}/rulesets/rule-suites/201"

        def substitute_nonexistent_fields(suite: dict) -> None:
            pushed_at = suite.pop("pushed_at")
            suite["created_at"] = pushed_at
            suite["updated_at"] = pushed_at

        attacks = {
            "missing": lambda suite: suite.pop("pushed_at"),
            "malformed": lambda suite: suite.update({"pushed_at": "not-rfc3339"}),
            "created-updated-substitution": substitute_nonexistent_fields,
        }
        for name, attack in attacks.items():
            record, api = active_fixture()
            attack(api.gets[endpoint])
            with self.subTest(name=name), self.assertRaises(module.ForgeError):
                self.executor(api).run("apply")
            self.assertFalse(api.mutations)

    def test_old_canary_cannot_be_replayed_after_source_and_ruleset_revision(self) -> None:
        record, api = active_fixture()
        new_source_sha = "d" * 40
        record["workflowSource"]["commitSha"] = new_source_sha
        for field in module.EVIDENCE_KINDS:
            record["activationEvidence"][field]["bindings"]["sourceCommitSha"] = new_source_sha
        evaluate = record["activationEvidence"]["evaluateReadback"]
        evaluate["observedAt"] = "2026-07-11T21:01:30Z"
        evaluate["subjectDigest"] = module.canonical_digest(
            module.expected_ruleset(record, enforcement="evaluate")
        )
        record["activationEvidence"]["pullRequestCanary"]["observedAt"] = "2026-07-11T21:02:00Z"
        record["activationEvidence"]["mergeGroupCanary"]["observedAt"] = "2026-07-11T21:03:00Z"
        record["activationEvidence"]["negativeControl"]["observedAt"] = "2026-07-11T21:04:00Z"
        record["activationEvidence"]["effectiveRulesReadback"]["observedAt"] = "2026-07-11T21:05:00Z"

        live_endpoint = f"/orgs/{module.ORGANIZATION}/rulesets/321"
        api.gets[live_endpoint]["updated_at"] = evaluate["observedAt"]
        api.gets[live_endpoint]["rules"][0]["parameters"]["workflows"][0]["sha"] = new_source_sha
        api.gets[f"/repositories/{module.EXECUTOR_REPOSITORY_ID}/commits/{new_source_sha}"] = {
            "sha": new_source_sha,
        }
        api.gets[
            f"/repositories/{module.EXECUTOR_REPOSITORY_ID}/compare/{new_source_sha}...main"
        ] = {"status": "ahead", "base_commit": {"sha": new_source_sha}}
        for path in module.SOURCE_PATHS:
            old_endpoint = module._content_endpoint(module.EXECUTOR_REPOSITORY_ID, path, SOURCE_SHA)
            new_endpoint = module._content_endpoint(module.EXECUTOR_REPOSITORY_ID, path, new_source_sha)
            api.gets[new_endpoint] = copy.deepcopy(api.gets[old_endpoint])
        api.gets[
            module._content_endpoint(
                module.DOCTRINE_REPOSITORY_ID,
                module.DOCTRINE_RECORD_PATH,
                DOCTRINE_HEAD,
            )
        ] = encoded(module.DOCTRINE_RECORD_PATH, canonical_file(record))

        with self.assertRaisesRegex(module.ForgeError, "predates the current evaluate ruleset revision"):
            self.executor(api).run("apply")
        self.assertFalse(api.mutations)

    def test_negative_control_attacks_block_before_mutation(self) -> None:
        attacks = {
            "foreign-head": lambda api: api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/pulls/77"]["head"].update({"sha": "f" * 40}),
            "wrong-base": lambda api: api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/pulls/77"]["base"].update({"ref": "release"}),
            "wrong-merge-tree": lambda api: api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/git/commits/{MERGE_SHA}"]["tree"].update({"sha": "e" * 40}),
            "extra-fixture-file": lambda api: api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/compare/{FIXTURE_BASE}...{FIXTURE_HEAD}"]["files"].append({"filename": "README.md", "status": "modified"}),
            "local-check-fails": lambda api: api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/commits/{FIXTURE_HEAD}/check-runs?filter=latest&per_page=100"]["check_runs"][0].update({"conclusion": "failure"}),
            "lookalike-run-url": lambda api: api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/commits/{FIXTURE_HEAD}/check-runs?filter=latest&per_page=100"]["check_runs"][2].update({"details_url": f"https://github.com/{module.TARGET_REPOSITORY_NAMES[0]}/actions/runs/999"}),
        }
        for name, attack in attacks.items():
            record, api = active_fixture()
            attack(api)
            with self.subTest(name=name), self.assertRaises(module.ForgeError):
                self.executor(api).run("apply")
            self.assertFalse(api.mutations)

    def test_foreign_target_selector_in_source_workflow_blocks(self) -> None:
        record = base_record()
        api = base_api(record)
        path = module._content_endpoint(module.EXECUTOR_REPOSITORY_ID, module.WORKFLOW_PATH, SOURCE_SHA)
        raw = f"name: {module.WORKFLOW_NAME}\nname: {module.REQUIRED_CHECK}\nif: github.repository_id == 999999999\n".encode()
        api.gets[path] = encoded(module.WORKFLOW_PATH, raw)
        with self.assertRaisesRegex(module.ForgeError, "workflow source"):
            self.executor(api).run("dry-run")

    def test_foreign_target_default_branch_blocks(self) -> None:
        record = base_record()
        api = base_api(record)
        api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}"]["default_branch"] = "release"
        with self.assertRaisesRegex(module.ForgeError, "default branch"):
            self.executor(api).run("dry-run")

    def test_authenticated_actor_is_exactly_pinned(self) -> None:
        api = base_api(base_record())
        api.gets["/user"] = {"id": 1, "login": module.GITHUB_ACTOR_LOGIN, "type": "User"}
        with self.assertRaisesRegex(module.ForgeError, "authenticated GitHub actor"):
            self.executor(api).run("dry-run")
        self.assertFalse(api.mutations)

    def test_executor_repository_node_identity_is_exactly_pinned(self) -> None:
        api = base_api(base_record())
        api.gets[f"/repositories/{module.EXECUTOR_REPOSITORY_ID}"]["node_id"] = "R_foreign"
        with self.assertRaisesRegex(module.ForgeError, "node identity differs"):
            self.executor(api).run("dry-run")
        self.assertFalse(api.mutations)

    def test_unknown_live_ruleset_semantics_fail_closed(self) -> None:
        attacks = {
            "condition": lambda live: live["conditions"].update({"actor_id": {"actor_ids": [1]}}),
            "repository-selector": lambda live: live["conditions"]["repository_id"].update({"repository_names": ["attacker/repo"]}),
            "ref-selector": lambda live: live["conditions"]["ref_name"].update({"protected": True}),
            "rule": lambda live: live["rules"][0].update({"priority": 1}),
            "workflow-parameter": lambda live: live["rules"][0]["parameters"].update({"strict": False}),
            "workflow-entry": lambda live: live["rules"][0]["parameters"]["workflows"][0].update({"repository_name": "attacker/repo"}),
        }
        for name, attack in attacks.items():
            record = base_record(phase="reconcile", ruleset_id=321)
            live = live_ruleset(record)
            attack(live)
            api = base_api(record, live=live)
            with self.subTest(name=name), self.assertRaisesRegex(module.ForgeError, "unsupported semantic"):
                self.executor(api).run("dry-run")
            self.assertFalse(api.mutations)

    def test_doctrine_toctou_blocks_before_write(self) -> None:
        record = base_record()
        api = base_api(record)
        endpoint = f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/commits/main"
        calls = iter([{"sha": DOCTRINE_HEAD}, {"sha": "f" * 40}])
        api.gets[endpoint] = lambda: next(calls)
        with self.assertRaisesRegex(module.ForgeError, "Doctrine main changed"):
            self.executor(api).run("apply")
        self.assertFalse(api.mutations)

    def test_live_ruleset_toctou_blocks_before_write(self) -> None:
        record = base_record(phase="reconcile", ruleset_id=321)
        initial = live_ruleset(record, enforcement="disabled")
        changed = copy.deepcopy(initial)
        changed["bypass_actors"] = [{"actor_id": 7, "actor_type": "Team", "bypass_mode": "always"}]
        api = base_api(record, live=initial)
        endpoint = f"/orgs/{module.ORGANIZATION}/rulesets/321"
        states = iter([initial, changed])
        api.gets[endpoint] = lambda: next(states)
        with self.assertRaisesRegex(
            module.ForgeError,
            "desired state, mutation plan, or pre-readback changed",
        ):
            self.executor(api).run("apply")
        self.assertFalse(api.mutations)

    def test_activation_evidence_is_rebuilt_after_final_doctrine_head_read(self) -> None:
        record, api = active_fixture()
        endpoint = f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/commits/main"
        calls = 0

        def doctrine_head() -> dict:
            nonlocal calls
            calls += 1
            if calls == 2:
                pull = api.gets[f"/repositories/{module.TARGET_REPOSITORY_ID}/pulls/77"]
                pull["head"]["sha"] = "f" * 40
            return {"sha": DOCTRINE_HEAD}

        api.gets[endpoint] = doctrine_head
        with self.assertRaisesRegex(module.ForgeError, "PR/run head differs"):
            self.executor(api).run("apply")
        self.assertEqual(calls, 3)
        self.assertFalse(api.mutations)

    def test_final_guard_detects_doctrine_change_during_activation_rebuild(self) -> None:
        record, api = active_fixture()
        doctrine_state = {"sha": DOCTRINE_HEAD}
        doctrine_endpoint = f"/repositories/{module.DOCTRINE_REPOSITORY_ID}/commits/main"
        api.gets[doctrine_endpoint] = lambda: {"sha": doctrine_state["sha"]}
        run_endpoint = f"/repositories/{module.TARGET_REPOSITORY_ID}/actions/runs/101"
        run_readback = copy.deepcopy(api.gets[run_endpoint])
        run_reads = 0

        def read_run() -> dict:
            nonlocal run_reads
            run_reads += 1
            if run_reads == 2:
                doctrine_state["sha"] = "f" * 40
            return run_readback

        api.gets[run_endpoint] = read_run
        with self.assertRaisesRegex(module.ForgeError, "Doctrine main changed"):
            self.executor(api).run("apply")
        self.assertEqual(run_reads, 2)
        self.assertFalse(api.mutations)

    def test_final_guard_detects_live_change_during_activation_rebuild(self) -> None:
        record, api = active_fixture()
        live_endpoint = f"/orgs/{module.ORGANIZATION}/rulesets/321"
        run_endpoint = f"/repositories/{module.TARGET_REPOSITORY_ID}/actions/runs/101"
        run_readback = copy.deepcopy(api.gets[run_endpoint])
        run_reads = 0

        def read_run() -> dict:
            nonlocal run_reads
            run_reads += 1
            if run_reads == 2:
                api.gets[live_endpoint]["updated_at"] = "2026-07-11T21:00:30Z"
            return run_readback

        api.gets[run_endpoint] = read_run
        with self.assertRaisesRegex(module.ForgeError, "precondition changed"):
            self.executor(api).run("apply")
        self.assertEqual(run_reads, 2)
        self.assertFalse(api.mutations)

    def test_report_digest_is_canonical_and_tamper_evident(self) -> None:
        report = self.executor(base_api(base_record())).run("dry-run")
        digest = report["evidenceDigest"]
        without = copy.deepcopy(report)
        without.pop("evidenceDigest")
        self.assertEqual(digest, module.canonical_digest(without))
        without["status"] = "PASS"
        self.assertNotEqual(digest, module.canonical_digest(without))


class ActivationEvidenceTests(unittest.TestCase):
    def executor(
        self,
        api: FakeAPI,
        *,
        sleeper=lambda _seconds: None,
    ) -> module.RulesetExecutor:
        return module.RulesetExecutor(
            api,
            LOCAL_BYTES,
            clock=lambda: FIXED_TIME,
            nonce_factory=lambda: "e" * 64,
            sleeper=sleeper,
        )

    def pending_fixture(self) -> tuple[dict, dict, FakeAPI]:
        record, api = active_fixture()
        report = self.executor(api).run("apply")
        configure_historical_authority(api, report, record)
        return record, report, api

    def collect(self, executor: module.RulesetExecutor, report: dict) -> dict:
        handle = write_report(report)
        try:
            return executor.collect_transition(Path(handle.name))
        finally:
            os.unlink(handle.name)

    def test_collector_emits_exact_sealed_privacy_safe_artifact(self) -> None:
        _, report, artifact, api = collected_fixture()
        self.assertEqual(
            set(artifact),
            {
                "schemaVersion", "kind", "capturedAt", "applyReport", "auditEvent",
                "liveCapture", "bodyDigest", "evidenceDigest",
            },
        )
        self.assertEqual(module.validate_activation_artifact(artifact), artifact)
        body = {field: artifact[field] for field in [
            "schemaVersion", "kind", "capturedAt", "applyReport", "auditEvent", "liveCapture",
        ]}
        self.assertEqual(artifact["bodyDigest"], module.canonical_digest(body))
        evidence_subject = copy.deepcopy(artifact)
        evidence_digest = evidence_subject.pop("evidenceDigest")
        self.assertEqual(evidence_digest, module.canonical_digest(evidence_subject))
        projection = artifact["auditEvent"]["providerProjection"]
        self.assertEqual(set(projection), module.AUDIT_PROVIDER_KEYS)
        serialized = module.canonical_bytes(artifact)
        for sentinel in [
            b"must-never-persist", b"actor_location", b"hashed_token",
            b"token_scopes", b"user_agent", b"request_headers", b"Bearer secret",
        ]:
            self.assertNotIn(sentinel, serialized)
        self.assertEqual(artifact["applyReport"]["evidenceDigest"], report["evidenceDigest"])
        self.assertEqual(len(api.mutations), 1)
        self.assertEqual(len(api.lock_mutations), 5)

    def test_collector_retries_delayed_audit_and_filters_locally(self) -> None:
        _, report, api = self.pending_fixture()
        calls = 0

        def delayed() -> list:
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            return [
                provider_audit_event(report, request_id="FOREIGN:REQUEST:0001"),
                provider_audit_event(report),
            ]

        sleeps: list[float] = []
        api.gets[module._audit_endpoint(1)] = delayed
        artifact = self.collect(self.executor(api, sleeper=sleeps.append), report)
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [1])
        self.assertEqual(
            artifact["auditEvent"]["normalized"]["requestId"],
            report["mutation"]["requestId"],
        )

    def test_collector_rechecks_live_state_after_audit_before_sealing(self) -> None:
        _, report, api = self.pending_fixture()
        live_endpoint = f"/orgs/{module.ORGANIZATION}/rulesets/{report['mutation']['rulesetId']}"

        def drift_during_audit() -> list:
            drifted = copy.deepcopy(api.gets[live_endpoint])
            drifted["enforcement"] = "evaluate"
            api.gets[live_endpoint] = drifted
            return [provider_audit_event(report)]

        api.gets[module._audit_endpoint(1)] = drift_during_audit
        with self.assertRaisesRegex(
            module.ForgeError,
            "live ruleset changed immediately before activation artifact sealing",
        ):
            self.collect(self.executor(api), report)

    def test_collector_rejects_multiple_or_foreign_exact_request_events(self) -> None:
        for events, message in [
            (
                lambda report: [provider_audit_event(report), provider_audit_event(report, _document_id="audit-document-2")],
                "multiple audit events",
            ),
            (
                lambda report: [provider_audit_event(report, ruleset_id=999)],
                "did not find",
            ),
        ]:
            with self.subTest(message=message):
                _, report, api = self.pending_fixture()
                api.gets[module._audit_endpoint(1)] = events(report)
                with self.assertRaisesRegex(module.ForgeError, message):
                    self.collect(self.executor(api), report)

    def test_apply_report_tamper_and_unsafe_paths_fail_closed(self) -> None:
        _, report, _ = self.pending_fixture()
        tampered = copy.deepcopy(report)
        tampered["mutation"]["requestId"] = "FOREIGN:REQUEST:0001"
        with self.assertRaisesRegex(module.ContractError, "evidenceDigest differs"):
            module.validate_apply_report(tampered)

        handle = write_report(report)
        link = handle.name + ".link"
        try:
            os.symlink(handle.name, link)
            with self.assertRaises(module.ContractError):
                module.read_sealed_report(Path(link))
            os.chmod(handle.name, 0o666)
            with self.assertRaisesRegex(module.ContractError, "group/world writable"):
                module.read_sealed_report(Path(handle.name))
        finally:
            if os.path.lexists(link):
                os.unlink(link)
            os.unlink(handle.name)

    def test_active_phase_verifies_exact_artifact_and_never_mutates(self) -> None:
        _, _, artifact, api = sealed_active_fixture()
        before = (len(api.mutations), len(api.lock_mutations))
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["phase"], "active")
        self.assertEqual(
            report["activationReadback"]["artifactEvidenceDigest"],
            artifact["evidenceDigest"],
        )
        self.assertEqual(before, (len(api.mutations), len(api.lock_mutations)))

    def test_active_phase_survives_historical_actions_and_negative_fixture_retention_expiry(self) -> None:
        _, report, artifact, api = sealed_active_fixture()
        expired_get_markers = (
            "/actions/runs/",
            "/rulesets/rule-suites/",
            "/pulls/77",
            f"/git/commits/{FIXTURE_BASE}",
            f"/git/commits/{FIXTURE_HEAD}",
            f"/git/commits/{MERGE_SHA}",
            f"/compare/{FIXTURE_BASE}...{FIXTURE_HEAD}",
            f"/commits/{FIXTURE_HEAD}/check-runs",
        )
        for endpoint in list(api.gets):
            if any(marker in endpoint for marker in expired_get_markers):
                del api.gets[endpoint]
        for endpoint in list(api.page_values):
            if "/pulls/77/files" in endpoint:
                del api.page_values[endpoint]
        expired_lock_tag_sha = report["applyLock"]["tagObjectSha"]
        del api.git_tags[expired_lock_tag_sha]
        api.get_calls.clear()
        api.page_calls.clear()
        before = (len(api.mutations), len(api.lock_mutations))
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            report["activationReadback"]["artifactEvidenceDigest"],
            artifact["evidenceDigest"],
        )
        self.assertEqual(before, (len(api.mutations), len(api.lock_mutations)))
        self.assertFalse(any(
            any(marker in endpoint for marker in expired_get_markers)
            for endpoint in api.get_calls
        ))
        self.assertNotIn(
            f"{module.APPLY_LOCK_TAGS_ENDPOINT}/{expired_lock_tag_sha}",
            api.get_calls,
        )

    def test_historical_workflow_summary_cross_binding_rejects_resealed_replay(self) -> None:
        historical, report, _, _ = collected_fixture()
        for field, member, value in [
            ("pullRequestCanary", "runId", 999999),
            ("mergeGroupCanary", "ruleSuiteId", 999999),
            ("negativeControl", "ruleSuitePushedAt", "2026-07-11T21:03:01Z"),
            ("pullRequestCanary", "observationDigest", "sha256:" + "f" * 64),
        ]:
            with self.subTest(field=field, member=member):
                replay = copy.deepcopy(report)
                replay["activationReadback"]["workflowEvidence"][field][member] = value
                replay = module.seal_report(replay)
                with self.assertRaisesRegex(module.ContractError, "historical desired evidence"):
                    module.validate_apply_report(replay, historical)

    def test_future_attestation_evidence_cutoff_cannot_postdate_artifact_capture(self) -> None:
        _, _, artifact, _ = collected_fixture()
        future = copy.deepcopy(artifact)
        future_report = future["applyReport"]
        future_report["applyLock"]["finalRefAbsentAt"] = "2026-07-11T23:00:00Z"
        future_report["activationAttestation"]["evidenceCutoffAt"] = "2026-07-11T23:00:00Z"
        future["applyReport"] = module.seal_report(future_report)
        resealed = module.seal_activation_artifact(
            future["applyReport"],
            future["auditEvent"],
            future["liveCapture"],
            future["capturedAt"],
        )
        with self.assertRaisesRegex(module.ContractError, "evidence cutoff postdates capture"):
            module.validate_activation_artifact(resealed)

    def test_active_phase_rejects_artifact_and_current_live_drift(self) -> None:
        for mutation, message in [
            (
                lambda artifact, api: artifact["auditEvent"]["normalized"].__setitem__("operationType", "tampered"),
                "bodyDigest differs",
            ),
            (
                lambda artifact, api: api.gets[
                    f"/orgs/{module.ORGANIZATION}/rulesets/321"
                ].__setitem__("updated_at", "2026-07-11T22:00:01Z"),
                "current active/effective readback differs",
            ),
        ]:
            with self.subTest(message=message):
                _, _, artifact, api = sealed_active_fixture()
                mutation(artifact, api)
                if "bodyDigest" in message:
                    raw = canonical_file(artifact)
                    api.gets[module._content_endpoint(
                        module.DOCTRINE_REPOSITORY_ID,
                        module.ACTIVATION_EVIDENCE_PATH,
                        ACTIVE_DOCTRINE_HEAD,
                    )] = encoded(module.ACTIVATION_EVIDENCE_PATH, raw)
                before = (len(api.mutations), len(api.lock_mutations))
                with self.assertRaisesRegex(module.ForgeError, message):
                    self.executor(api).run("apply")
                self.assertEqual(before, (len(api.mutations), len(api.lock_mutations)))

    def test_effective_rules_digest_is_stable_across_controlled_rename(self) -> None:
        record = base_record(phase="ratchet", ruleset_id=321, enforcement="active")
        api = base_api(record, live=live_ruleset(record, enforcement="evaluate"))
        executor = self.executor(api)
        before = executor._effective(
            {"id": module.TARGET_REPOSITORY_ID, "name": module.TARGET_REPOSITORY_NAMES[0]},
            321,
        )
        after = executor._effective(
            {"id": module.TARGET_REPOSITORY_ID, "name": module.TARGET_REPOSITORY_NAMES[1]},
            321,
        )
        self.assertEqual(before, after)
        self.assertEqual(module.canonical_digest(before), module.canonical_digest(after))

    def test_ratchet_live_active_blocks_pending_transition_without_ruleset_write(self) -> None:
        record, api = active_fixture()
        active_live = live_ruleset(record, enforcement="active")
        api.gets[f"/orgs/{module.ORGANIZATION}/rulesets/321"] = active_live
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["findings"], ["BLOCKED_PENDING_TRANSITION_EVIDENCE"])
        self.assertFalse(api.mutations)

    def test_ratchet_update_without_provider_request_id_is_not_success(self) -> None:
        _, api = active_fixture()

        def missing_request_id(endpoint: str, payload: dict) -> tuple[dict, None]:
            return api.put(endpoint, payload), None

        api.put_observed = missing_request_id  # type: ignore[method-assign]
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "ERROR")
        self.assertEqual(report["mutation"]["outcome"], "updated-without-provider-request-id")
        self.assertNotEqual(report["status"], "PASS")

    def test_run_observation_digest_covers_timestamps_job_identity_and_suite_time(self) -> None:
        record = base_record(phase="ratchet", ruleset_id=321, enforcement="active")
        api = base_api(record, live=live_ruleset(record, enforcement="evaluate"))
        _, run, job, original = configure_workflow_evidence(
            api,
            record,
            field="pullRequestCanary",
            run_id=501,
            suite_id=601,
            head_sha="7" * 40,
            observed_at="2026-07-11T21:01:00Z",
        )
        suite = copy.deepcopy(original["ruleSuite"])
        baseline = module.canonical_digest(module._run_observation(run, job, suite))
        mutations = [
            (run, "updated_at", "2026-07-11T21:01:11Z"),
            (job, "id", 9999),
            (job, "head_sha", "8" * 40),
            (suite, "pushedAt", "2026-07-11T21:01:01Z"),
        ]
        for subject, key, value in mutations:
            with self.subTest(key=key):
                changed_run = copy.deepcopy(run)
                changed_job = copy.deepcopy(job)
                changed_suite = copy.deepcopy(suite)
                selected = changed_run if subject is run else changed_job if subject is job else changed_suite
                selected[key] = value
                self.assertNotEqual(
                    baseline,
                    module.canonical_digest(module._run_observation(changed_run, changed_job, changed_suite)),
                )

    def test_attestation_ruleset_missing_or_mutable_blocks_before_any_write(self) -> None:
        mutations = {
            "missing": lambda api: api.page_values[
                f"/orgs/{module.ORGANIZATION}/rulesets"
            ].pop(),
            "bypass": lambda api: api.gets[
                f"/repos/{module.EXECUTOR_REPOSITORY}/rulesets/{ATTESTATION_RULESET_ID}?includes_parents=true"
            ].__setitem__("current_user_can_bypass", "always"),
            "creation-rule": lambda api: api.gets[
                f"/orgs/{module.ORGANIZATION}/rulesets/{ATTESTATION_RULESET_ID}"
            ]["rules"].append({"type": "creation"}),
            "mutable-enforcement": lambda api: api.gets[
                f"/orgs/{module.ORGANIZATION}/rulesets/{ATTESTATION_RULESET_ID}"
            ].__setitem__("enforcement", "evaluate"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                _, api = active_fixture()
                mutate(api)
                with self.assertRaises(module.ForgeError):
                    self.executor(api).run("apply")
                self.assertFalse(api.mutations)
                self.assertFalse(api.lock_mutations)

    def test_attestation_ruleset_drift_in_final_under_lock_guard_blocks_activation_write(self) -> None:
        _, api = active_fixture()
        endpoint = f"/orgs/{module.ORGANIZATION}/rulesets/{ATTESTATION_RULESET_ID}"
        stable = copy.deepcopy(api.gets[endpoint])
        drifted = copy.deepcopy(stable)
        drifted["enforcement"] = "evaluate"
        reads = 0

        def read_attestation_ruleset() -> dict:
            nonlocal reads
            reads += 1
            return drifted if reads == 3 else stable

        api.gets[endpoint] = read_attestation_ruleset
        with self.assertRaises(module.ForgeError):
            self.executor(api).run("apply")
        self.assertEqual(reads, 3)
        self.assertFalse(api.mutations)

    def test_attestation_ruleset_drift_after_ref_creation_cannot_report_evidence_ready(self) -> None:
        _, api = active_fixture()
        original = api.post_created
        ruleset_endpoint = f"/orgs/{module.ORGANIZATION}/rulesets/{ATTESTATION_RULESET_ID}"

        def drift_after_attestation_ref(endpoint: str, payload: dict) -> dict:
            response = original(endpoint, payload)
            if (
                endpoint == module.APPLY_LOCK_REFS_ENDPOINT
                and payload.get("ref", "").startswith(module.ATTESTATION_REF_PREFIX)
            ):
                drifted = copy.deepcopy(api.gets[ruleset_endpoint])
                drifted["enforcement"] = "evaluate"
                api.gets[ruleset_endpoint] = drifted
            return response

        api.post_created = drift_after_attestation_ref  # type: ignore[method-assign]
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "APPLIED_PENDING_ATTESTATION")
        self.assertIsNone(report["activationAttestation"])
        self.assertEqual(len(api.mutations), 1)
        self.assertIn(module._attestation_ref(report["applyLock"]["nonce"]), api.git_refs)

    def test_equal_provider_updated_at_still_proves_distinct_evaluate_to_active_states(self) -> None:
        record, api = active_fixture()
        api.post_readback_override = live_ruleset(record, enforcement="active")
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "APPLIED_PENDING_EVIDENCE")
        self.assertEqual(
            report["preReadback"]["updatedAt"],
            report["postReadback"]["updatedAt"],
        )
        self.assertNotEqual(
            report["preReadback"]["digest"],
            report["postReadback"]["digest"],
        )
        self.assertEqual(module.validate_apply_report(report, record), report)

    def test_attestation_ref_creation_uncertainty_accepts_only_exact_readback(self) -> None:
        _, api = active_fixture()
        original = api.post_created

        def create_then_disconnect(endpoint: str, payload: dict) -> dict:
            response = original(endpoint, payload)
            if (
                endpoint == module.APPLY_LOCK_REFS_ENDPOINT
                and payload.get("ref", "").startswith(module.ATTESTATION_REF_PREFIX)
            ):
                raise module.ForgeError("injected transport loss after attestation ref create")
            return response

        api.post_created = create_then_disconnect  # type: ignore[method-assign]
        report = self.executor(api).run("apply")
        self.assertEqual(report["status"], "APPLIED_PENDING_EVIDENCE")
        attestation = report["activationAttestation"]
        self.assertEqual(api.git_refs[attestation["ref"]]["object"]["sha"], attestation["tagObjectSha"])

    def test_pending_attestation_report_is_idempotently_finalized_without_second_ruleset_write(self) -> None:
        record, api = active_fixture()
        original = api.post_created

        def fail_attestation_ref(endpoint: str, payload: dict) -> dict:
            if (
                endpoint == module.APPLY_LOCK_REFS_ENDPOINT
                and payload.get("ref", "").startswith(module.ATTESTATION_REF_PREFIX)
            ):
                raise module.ForgeError("injected attestation ref outage")
            return original(endpoint, payload)

        api.post_created = fail_attestation_ref  # type: ignore[method-assign]
        executor = self.executor(api)
        pending = executor.run("apply")
        self.assertEqual(pending["status"], "APPLIED_PENDING_ATTESTATION")
        self.assertIsNone(pending["activationAttestation"])
        self.assertEqual(len(api.mutations), 1)
        configure_historical_authority(api, pending, record)
        del api.git_tags[pending["applyLock"]["tagObjectSha"]]

        api.post_created = original  # type: ignore[method-assign]
        finalized = executor._finalize_report_attestation(pending)
        self.assertEqual(finalized["status"], "APPLIED_PENDING_EVIDENCE")
        self.assertEqual(len(api.mutations), 1)
        before = len(api.lock_mutations)
        repeated = executor._finalize_report_attestation(finalized)
        self.assertEqual(repeated["activationAttestation"], finalized["activationAttestation"])
        self.assertEqual(len(api.lock_mutations), before)

    def test_foreign_or_tampered_attestation_ref_fails_closed(self) -> None:
        record, api = active_fixture()
        original = api.post_created

        def fail_attestation_ref(endpoint: str, payload: dict) -> dict:
            if endpoint == module.APPLY_LOCK_REFS_ENDPOINT and payload.get("ref", "").startswith(module.ATTESTATION_REF_PREFIX):
                raise module.ForgeError("injected attestation ref outage")
            return original(endpoint, payload)

        api.post_created = fail_attestation_ref  # type: ignore[method-assign]
        executor = self.executor(api)
        pending = executor.run("apply")
        configure_historical_authority(api, pending, record)
        nonce = pending["applyLock"]["nonce"]
        foreign_sha = "f" * 40
        api.git_refs[module._attestation_ref(nonce)] = {
            "ref": module._attestation_ref(nonce),
            "object": {"type": "tag", "sha": foreign_sha},
        }
        api.git_tags[foreign_sha] = {
            "sha": foreign_sha,
            "tag": f"{module.ATTESTATION_TAG_PREFIX}{nonce}",
            "message": "foreign",
            "tagger": {},
            "object": {"type": "commit", "sha": EXECUTOR_HEAD},
        }
        api.post_created = original  # type: ignore[method-assign]
        with self.assertRaisesRegex(module.ForgeError, "does not bind the exact transition"):
            executor._finalize_report_attestation(pending)
        self.assertEqual(len(api.mutations), 1)

    def test_attestation_projection_replay_and_provider_tag_tamper_are_rejected(self) -> None:
        _, api_one = active_fixture()
        report_one = module.RulesetExecutor(
            api_one,
            LOCAL_BYTES,
            clock=lambda: FIXED_TIME,
            nonce_factory=lambda: "f" * 64,
            sleeper=lambda _seconds: None,
        ).run("apply")
        _, report_two, _, api_two = collected_fixture()
        replay = copy.deepcopy(report_two)
        replay["activationAttestation"] = copy.deepcopy(report_one["activationAttestation"])
        replay = module.seal_report(replay)
        with self.assertRaises(module.ContractError):
            module.validate_apply_report(replay)

        tag_sha = report_two["activationAttestation"]["tagObjectSha"]
        api_two.git_tags[tag_sha]["message"] = "tampered"
        # Provider-tag verification itself is independent of the active record.
        with self.assertRaisesRegex(module.ForgeError, "does not bind the exact transition"):
            self.executor(api_two)._verify_attestation_tag(report_two, tag_sha)


class TransportTests(unittest.TestCase):
    def test_keyring_ignores_token_host_and_config_environment(self) -> None:
        observed: dict = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            observed["command"] = command
            observed["env"] = kwargs["env"]
            return subprocess.CompletedProcess(command, 0, stdout="keyring-token\n", stderr="")

        hostile = {
            "GH_TOKEN": "attacker",
            "GITHUB_TOKEN": "attacker",
            "GH_HOST": "attacker.invalid",
            "GH_CONFIG_DIR": "/tmp/attacker",
            "XDG_CONFIG_HOME": "/tmp/attacker-xdg",
        }
        with mock.patch.dict(os.environ, hostile, clear=False), mock.patch.object(module.shutil, "which", return_value="/bin/sh"), mock.patch.object(module.subprocess, "run", side_effect=fake_run):
            self.assertEqual(module.keyring_token(), "keyring-token")
        self.assertEqual(observed["command"][1:], [
            "auth", "token", "--hostname", "github.com", "--user", module.GITHUB_ACTOR_LOGIN,
        ])
        for key in hostile:
            self.assertNotIn(key, observed["env"])

    def test_transport_rejects_redirects_and_nonrelative_endpoints(self) -> None:
        handler = module._RejectRedirects()
        with self.assertRaises(module.ForgeError):
            handler.redirect_request(None, None, 302, "redirect", None, "https://attacker.invalid")
        api = module.GitHubAPI("in-memory-token")
        with self.assertRaises(module.ForgeError):
            api.get("https://attacker.invalid/user")

    def test_transport_supports_only_narrow_expected_404_and_exact_delete_204(self) -> None:
        api = module.GitHubAPI("in-memory-token")
        not_found = module.urllib.error.HTTPError(
            module.API_ROOT + module.APPLY_LOCK_REF_GET_ENDPOINT,
            404,
            "Not Found",
            {},
            None,
        )
        with mock.patch.object(api._opener, "open", side_effect=not_found):
            self.assertIsNone(api.get_optional(module.APPLY_LOCK_REF_GET_ENDPOINT))
        with mock.patch.object(api._opener, "open", side_effect=not_found):
            with self.assertRaisesRegex(module.ForgeError, "HTTP 404"):
                api.get(module.APPLY_LOCK_REF_GET_ENDPOINT)

        response = mock.MagicMock()
        response.status = 204
        response.read.return_value = b""
        response.__enter__.return_value = response
        with mock.patch.object(api._opener, "open", return_value=response) as opened:
            api.delete(module.APPLY_LOCK_REF_DELETE_ENDPOINT)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")

        response.status = 200
        with mock.patch.object(api._opener, "open", return_value=response):
            with self.assertRaisesRegex(module.ForgeError, "expected 204"):
                api.delete(module.APPLY_LOCK_REF_DELETE_ENDPOINT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
