#!/usr/bin/env python3
"""Reconcile the public-skills organization required-workflow ruleset.

Doctrine owns the desired-state record.  This protected, independently owned
executor treats that record as inert JSON and never downloads or executes
Doctrine code, schemas, dependencies, or candidate refs.  Dry-run is the
default.  ``--apply`` is the only mutation boundary.
"""

from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"
ORGANIZATION = "SylphxAI"
ORGANIZATION_ID = 206448049
GITHUB_ACTOR_LOGIN = "shtse8"
GITHUB_ACTOR_ID = 8020099
GITHUB_ACTOR_TYPE = "User"

APPLY_LOCK_TAG_NAME = "sylph-locks/public-skills-ruleset-executor"
APPLY_LOCK_REF = f"refs/tags/{APPLY_LOCK_TAG_NAME}"
APPLY_LOCK_REF_PATH = f"tags/{APPLY_LOCK_TAG_NAME}"

EXECUTOR_REPOSITORY_ID = 1091169653
EXECUTOR_REPOSITORY = "SylphxAI/.github"
EXECUTOR_BRANCH = "main"
EXECUTOR_PATH = "scripts/public-skills-ruleset-executor.py"
APPLY_LOCK_TAGS_ENDPOINT = f"/repositories/{EXECUTOR_REPOSITORY_ID}/git/tags"
APPLY_LOCK_REFS_ENDPOINT = f"/repositories/{EXECUTOR_REPOSITORY_ID}/git/refs"
APPLY_LOCK_REF_GET_ENDPOINT = f"/repositories/{EXECUTOR_REPOSITORY_ID}/git/ref/{APPLY_LOCK_REF_PATH}"
APPLY_LOCK_REF_DELETE_ENDPOINT = f"/repositories/{EXECUTOR_REPOSITORY_ID}/git/refs/{APPLY_LOCK_REF_PATH}"

DOCTRINE_REPOSITORY_ID = 1265184361
DOCTRINE_REPOSITORY = "SylphxAI/doctrine"
DOCTRINE_BRANCH = "main"
DOCTRINE_RECORD_PATH = "control-plane/github-rulesets/public-skills-external-admission.json"
DOCTRINE_SCHEMA_REF = "../../schemas/organization-required-workflow-ruleset.schema.json"
DOCTRINE_RECORD_ID = "SylphxAI/doctrine:public-skills-external-admission"

TARGET_REPOSITORY_ID = 1297840366
TARGET_REPOSITORY_NODE_ID = "R_kgDOTVt47g"
TARGET_REPOSITORY_NAMES = ("SylphxAI/skills-public-cleanroom", "SylphxAI/skills")
TARGET_FINAL_NAME = "SylphxAI/skills"
TARGET_DEFAULT_BRANCH = "main"

RULESET_NAME = "public-skills-external-admission"
WORKFLOW_PATH = ".github/workflows/public-skills-admission.yml"
WORKFLOW_NAME = "public-skills-external-admission"
VALIDATOR_PATH = "scripts/public-skills-admission.mjs"
POLICY_PATH = "policies/public-skills-admission.json"
REQUIRED_CHECK = "public-skills-external-admission/pass"
LOCAL_REQUIRED_CHECKS = ("risk-classification/pass", "trunk-admission/pass")
SOURCE_PATHS = (WORKFLOW_PATH, VALIDATOR_PATH, POLICY_PATH)

MAX_RECORD_BYTES = 512 * 1024
MAX_JSON_DEPTH = 64
MAX_SAFE_INTEGER = 2**53 - 1
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
PHASE_ENFORCEMENT = {
    "expand": {"evaluate"},
    "reconcile": {"evaluate"},
    "ratchet": {"active"},
    "recovery": {"evaluate", "disabled"},
}
ENFORCEMENT_RANK = {"disabled": 0, "evaluate": 1, "active": 2}
EVIDENCE_KINDS = {
    "evaluateReadback": "ruleset-readback",
    "pullRequestCanary": "workflow-run",
    "mergeGroupCanary": "workflow-run",
    "negativeControl": "negative-control-run",
    "effectiveRulesReadback": "effective-rules-readback",
}
STATUS_EXIT = {"PASS": 0, "DRIFT": 1, "BLOCKED": 2, "ERROR": 3}


class ContractError(ValueError):
    """The canonical desired-state record violates the executor contract."""


class ForgeError(RuntimeError):
    """GitHub identity, readback, authentication, or mutation failed."""


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON member {key!r}")
        value[key] = item
    return value


def _json_integer(raw: str) -> int:
    value = int(raw)
    if abs(value) > MAX_SAFE_INTEGER:
        raise ContractError("JSON integer exceeds the interoperable safe-integer domain")
    return value


def _json_float(_raw: str) -> float:
    raise ContractError("floating-point values are outside this desired-state contract")


def _json_constant(raw: str) -> Any:
    raise ContractError(f"non-finite JSON value {raw!r} is forbidden")


def _validate_json_value(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ContractError("JSON nesting exceeds the executor limit")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("non-finite JSON value is forbidden")
        raise ContractError("floating-point values are forbidden")
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ContractError("lone UTF-16 surrogate is forbidden")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_json_value(key, depth + 1)
            _validate_json_value(item, depth + 1)
        return
    raise ContractError(f"unsupported JSON value type {type(value).__name__}")


def strict_json_loads(raw: bytes | str, *, label: str = "JSON") -> Any:
    if isinstance(raw, bytes):
        if len(raw) > MAX_RECORD_BYTES:
            raise ContractError(f"{label} exceeds {MAX_RECORD_BYTES} bytes")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError(f"{label} is not UTF-8") from exc
    else:
        text = raw
        if len(text.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ContractError(f"{label} exceeds {MAX_RECORD_BYTES} bytes")
    if text.startswith("\ufeff"):
        raise ContractError(f"{label} must not contain a UTF-8 BOM")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_int=_json_integer,
            parse_float=_json_float,
            parse_constant=_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ContractError(f"{label} is invalid JSON: {exc.msg}") from exc
    _validate_json_value(value)
    return value


def canonical_bytes(value: Any) -> bytes:
    """Return the RFC-8785-compatible bytes for this integer-only contract."""

    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def exact_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def git_blob_sha(value: bytes) -> str:
    header = f"blob {len(value)}\0".encode("ascii")
    return hashlib.sha1(header + value).hexdigest()  # noqa: S324 - Git object identity.


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _exact_keys(
    value: dict[str, Any],
    required: set[str],
    label: str,
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing:
        raise ContractError(f"{label} lacks required members {missing}")
    if unknown:
        raise ContractError(f"{label} contains unsupported members {unknown}")


def _string(value: Any, label: str, *, minimum: int = 1) -> str:
    if not isinstance(value, str) or len(value) < minimum:
        raise ContractError(f"{label} must be a string of length >= {minimum}")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{label} must be a positive integer")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise ContractError(f"{label} must be a lowercase 40-character Git SHA")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise ContractError(f"{label} must be a sha256 digest")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    text = _string(value, label)
    if RFC3339_RE.fullmatch(text) is None:
        raise ContractError(f"{label} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include a timezone")
    return parsed


def _github_url(value: Any, label: str) -> str:
    text = _string(value, label)
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.username or parsed.password:
        raise ContractError(f"{label} must be an https://github.com URL")
    return text


def _validate_bindings(value: Any, label: str, record: dict[str, Any], canary: bool) -> None:
    bindings = _object(value, label)
    _exact_keys(
        bindings,
        {"rulesetId", "targetRepositoryId", "sourceRepositoryId", "sourceCommitSha", "headSha", "ruleSuiteId"},
        label,
    )
    ruleset_id = _positive_integer(bindings["rulesetId"], f"{label}.rulesetId")
    if ruleset_id != record["ruleset"]["rulesetId"]:
        raise ContractError(f"{label}.rulesetId does not bind desired state")
    if bindings["targetRepositoryId"] != TARGET_REPOSITORY_ID:
        raise ContractError(f"{label}.targetRepositoryId differs from the safety invariant")
    if bindings["sourceRepositoryId"] != EXECUTOR_REPOSITORY_ID:
        raise ContractError(f"{label}.sourceRepositoryId differs from the safety invariant")
    if bindings["sourceCommitSha"] != record["workflowSource"]["commitSha"]:
        raise ContractError(f"{label}.sourceCommitSha does not bind desired state")
    if canary:
        _sha(bindings["headSha"], f"{label}.headSha")
        _positive_integer(bindings["ruleSuiteId"], f"{label}.ruleSuiteId")
    elif bindings["headSha"] is not None or bindings["ruleSuiteId"] is not None:
        raise ContractError(f"{label} must not contain canary identity")


def _validate_negative_proof(value: Any, label: str, policy: dict[str, Any]) -> None:
    proof = _object(value, label)
    required = {
        "pullRequestNumber", "targetRef", "headRef", "fixtureBaseSha", "fixtureBaseTree",
        "fixtureHeadSha", "fixtureHeadTree", "pullRequestMergeCommitSha", "mutationClass",
        "mutationPath", "fixtureDigest", "fixtureSemanticDigest", "fixtureComparisonDigest",
        "pullRequestFilesDigest", "mergeCommitDigest", "contextsDigest",
    }
    _exact_keys(proof, required, label)
    _positive_integer(proof["pullRequestNumber"], f"{label}.pullRequestNumber")
    for field in ["fixtureBaseSha", "fixtureBaseTree", "fixtureHeadSha", "fixtureHeadTree", "pullRequestMergeCommitSha"]:
        _sha(proof[field], f"{label}.{field}")
    for field in ["fixtureDigest", "fixtureSemanticDigest", "fixtureComparisonDigest", "pullRequestFilesDigest", "mergeCommitDigest", "contextsDigest"]:
        _digest(proof[field], f"{label}.{field}")
    for field in ["targetRef", "headRef"]:
        if re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", _string(proof[field], f"{label}.{field}")) is None:
            raise ContractError(f"{label}.{field} is not an exact branch ref")
    if proof["fixtureBaseSha"] != policy["fixtureBaseSha"] or proof["fixtureBaseTree"] != policy["fixtureBaseTree"]:
        raise ContractError(f"{label} does not bind the admitted fixture baseline")
    if proof["mutationClass"] != policy["mutationClass"] or proof["mutationPath"] != policy["mutationPath"]:
        raise ContractError(f"{label} does not bind the admitted mutation")


def _validate_evidence(value: Any, field: str, record: dict[str, Any]) -> datetime | None:
    if value is None:
        return None
    label = f"activationEvidence.{field}"
    evidence = _object(value, label)
    _exact_keys(evidence, {"kind", "locator", "observedAt", "subjectDigest", "bindings"}, label, {"negativeControl"})
    if evidence["kind"] != EVIDENCE_KINDS[field]:
        raise ContractError(f"{label}.kind must be {EVIDENCE_KINDS[field]}")
    _github_url(evidence["locator"], f"{label}.locator")
    observed = _timestamp(evidence["observedAt"], f"{label}.observedAt")
    _digest(evidence["subjectDigest"], f"{label}.subjectDigest")
    canary = field in {"pullRequestCanary", "mergeGroupCanary", "negativeControl"}
    _validate_bindings(evidence["bindings"], f"{label}.bindings", record, canary)
    negative = evidence.get("negativeControl")
    if field == "negativeControl":
        policy = record["workflowSource"].get("negativeControlPolicy")
        if policy is None:
            raise ContractError("ratchet negative control requires workflowSource.negativeControlPolicy")
        _validate_negative_proof(negative, f"{label}.negativeControl", policy)
    elif negative is not None:
        raise ContractError(f"{label} cannot carry negative-control proof")
    return observed


def validate_record(record: Any) -> dict[str, Any]:
    root = _object(record, "desired state")
    _exact_keys(
        root,
        {"$schema", "schemaVersion", "kind", "id", "owner", "owningDecision", "organization", "ruleset", "workflowSource", "migration", "activationEvidence", "recovery"},
        "desired state",
    )
    fixed = {
        "$schema": DOCTRINE_SCHEMA_REF,
        "schemaVersion": 1,
        "kind": "organization-required-workflow-ruleset",
        "id": DOCTRINE_RECORD_ID,
        "owner": DOCTRINE_REPOSITORY,
        "owningDecision": "public-skills-external-required-workflow",
        "organization": ORGANIZATION,
    }
    for field, expected in fixed.items():
        if root[field] != expected:
            raise ContractError(f"desired state {field} differs from the immutable executor contract")

    ruleset = _object(root["ruleset"], "ruleset")
    _exact_keys(ruleset, {"rulesetId", "name", "target", "enforcement", "bypassActors", "targetRepositories", "refInclude", "refExclude", "doNotEnforceOnCreate"}, "ruleset")
    if ruleset["rulesetId"] is not None:
        _positive_integer(ruleset["rulesetId"], "ruleset.rulesetId")
    expected_ruleset = {
        "name": RULESET_NAME,
        "target": "branch",
        "bypassActors": [],
        "refInclude": ["~DEFAULT_BRANCH"],
        "refExclude": [],
        "doNotEnforceOnCreate": False,
    }
    for field, expected in expected_ruleset.items():
        if ruleset[field] != expected:
            raise ContractError(f"ruleset.{field} differs from the immutable executor contract")
    targets = ruleset["targetRepositories"]
    if not isinstance(targets, list) or len(targets) != 1:
        raise ContractError("ruleset.targetRepositories must contain exactly one target")
    target = _object(targets[0], "ruleset.targetRepositories[0]")
    _exact_keys(target, {"repositoryId", "acceptedNames", "finalName"}, "ruleset.targetRepositories[0]")
    if target != {"repositoryId": TARGET_REPOSITORY_ID, "acceptedNames": list(TARGET_REPOSITORY_NAMES), "finalName": TARGET_FINAL_NAME}:
        raise ContractError("target repository identity differs from the immutable executor contract")

    source = _object(root["workflowSource"], "workflowSource")
    source_required = {"repositoryId", "repository", "workflowPath", "workflowName", "requiredCheck", "validatorPath", "policyPath", "ref", "commitSha"}
    source_optional = {"localRequiredChecks", "negativeControlPolicy"}
    _exact_keys(source, source_required, "workflowSource", source_optional)
    expected_source = {
        "repositoryId": EXECUTOR_REPOSITORY_ID,
        "repository": EXECUTOR_REPOSITORY,
        "workflowPath": WORKFLOW_PATH,
        "workflowName": WORKFLOW_NAME,
        "requiredCheck": REQUIRED_CHECK,
        "validatorPath": VALIDATOR_PATH,
        "policyPath": POLICY_PATH,
        "ref": EXECUTOR_BRANCH,
    }
    for field, expected in expected_source.items():
        if source[field] != expected:
            raise ContractError(f"workflowSource.{field} differs from the immutable executor contract")
    if source["commitSha"] is not None:
        _sha(source["commitSha"], "workflowSource.commitSha")
    if "localRequiredChecks" in source and source["localRequiredChecks"] != list(LOCAL_REQUIRED_CHECKS):
        raise ContractError("workflowSource.localRequiredChecks differs from the immutable executor contract")
    if "negativeControlPolicy" in source:
        policy = _object(source["negativeControlPolicy"], "workflowSource.negativeControlPolicy")
        _exact_keys(policy, {"fixtureBaseSha", "fixtureBaseTree", "mutationClass", "mutationPath", "scriptOverrides"}, "workflowSource.negativeControlPolicy")
        _sha(policy["fixtureBaseSha"], "workflowSource.negativeControlPolicy.fixtureBaseSha")
        _sha(policy["fixtureBaseTree"], "workflowSource.negativeControlPolicy.fixtureBaseTree")
        if policy["mutationClass"] != "package-script-neutralization" or policy["mutationPath"] != "package.json":
            raise ContractError("negative-control mutation identity differs from the immutable executor contract")
        if policy["scriptOverrides"] != {"check": 'node -e "process.exit(0)"', "verify:install": 'node -e "process.exit(0)"'}:
            raise ContractError("negative-control script overrides differ from the immutable executor contract")

    migration = _object(root["migration"], "migration")
    _exact_keys(migration, {"packetId", "class", "phase", "tracker", "compatibility", "recoveryPlan"}, "migration")
    if re.fullmatch(r"public-skills-external-admission@[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9a-f]{12}", _string(migration["packetId"], "migration.packetId")) is None:
        raise ContractError("migration.packetId is not the admitted packet identity")
    if migration["class"] != "required-immediate":
        raise ContractError("migration.class must be required-immediate")
    phase = migration["phase"]
    if phase not in PHASE_ENFORCEMENT:
        raise ContractError("migration.phase is unsupported")
    if ruleset["enforcement"] not in PHASE_ENFORCEMENT[phase]:
        raise ContractError(f"phase {phase} cannot desire enforcement {ruleset['enforcement']}")
    _github_url(migration["tracker"], "migration.tracker")
    compatibility = _object(migration["compatibility"], "migration.compatibility")
    _exact_keys(compatibility, {"oldAcceptedUntil", "newRequiredAfter"}, "migration.compatibility")
    for field in ["oldAcceptedUntil", "newRequiredAfter"]:
        if compatibility[field] is not None and DATE_RE.fullmatch(_string(compatibility[field], f"migration.compatibility.{field}")) is None:
            raise ContractError(f"migration.compatibility.{field} must be an ISO date or null")
    if compatibility["oldAcceptedUntil"] and compatibility["newRequiredAfter"] and compatibility["oldAcceptedUntil"] >= compatibility["newRequiredAfter"]:
        raise ContractError("migration compatibility dates are not monotonic")
    _string(migration["recoveryPlan"], "migration.recoveryPlan", minimum=20)

    evidence = _object(root["activationEvidence"], "activationEvidence")
    _exact_keys(evidence, set(EVIDENCE_KINDS), "activationEvidence")
    observed: dict[str, datetime] = {}
    for field in EVIDENCE_KINDS:
        value = _validate_evidence(evidence[field], field, root)
        if value is not None:
            observed[field] = value

    if source["commitSha"] is None:
        if phase != "expand" or ruleset["rulesetId"] is not None or ruleset["enforcement"] != "evaluate":
            raise ContractError("unresolved source SHA is allowed only in unbound expand/evaluate")
    if phase in {"reconcile", "ratchet", "recovery"} and ruleset["rulesetId"] is None:
        raise ContractError(f"phase {phase} requires an exact ruleset ID")
    if phase == "ratchet":
        missing = [field for field in EVIDENCE_KINDS if evidence[field] is None]
        if missing:
            raise ContractError(f"ratchet lacks activation evidence {missing}")
        if "localRequiredChecks" not in source or "negativeControlPolicy" not in source:
            raise ContractError("ratchet requires the complete negative-control policy")
        if not observed["evaluateReadback"] <= observed["pullRequestCanary"] <= observed["effectiveRulesReadback"]:
            raise ContractError("pull-request canary chronology is invalid")
        if not observed["evaluateReadback"] <= observed["mergeGroupCanary"] <= observed["effectiveRulesReadback"]:
            raise ContractError("merge-group canary chronology is invalid")
        if not observed["evaluateReadback"] <= observed["negativeControl"] <= observed["effectiveRulesReadback"]:
            raise ContractError("negative-control chronology is invalid")
    recovery = root["recovery"]
    if phase == "recovery":
        recovery_object = _object(recovery, "recovery")
        _exact_keys(recovery_object, {"reason", "tracker", "initiatedAt"}, "recovery")
        _string(recovery_object["reason"], "recovery.reason", minimum=10)
        _github_url(recovery_object["tracker"], "recovery.tracker")
        _timestamp(recovery_object["initiatedAt"], "recovery.initiatedAt")
    elif recovery is not None:
        raise ContractError("recovery data is allowed only in recovery phase")
    return root


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise ForgeError(f"GitHub API redirect {code} is forbidden")


class GitHubAPI:
    """Fixed-host GitHub REST transport with an in-memory keyring token."""

    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        if not token or "\n" in token or "\r" in token:
            raise ForgeError("GitHub keyring returned an invalid token")
        self._token = token
        self.timeout = timeout
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            _RejectRedirects(),
            urllib.request.HTTPSHandler(context=context),
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        expected_status: int | None = None,
        expected_not_found: bool = False,
    ) -> Any:
        if not endpoint.startswith("/") or endpoint.startswith("//") or "://" in endpoint:
            raise ForgeError("GitHub API endpoint must be a fixed-host relative path")
        data = canonical_bytes(payload) if payload is not None else None
        request = urllib.request.Request(
            API_ROOT + endpoint,
            data=data,
            method=method,
            headers={
                "Accept": ACCEPT,
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "SylphxAI-public-skills-ruleset-executor/1",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if expected_not_found and exc.code == 404:
                return None
            raise ForgeError(f"GitHub API {method} {endpoint} failed with HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ForgeError(f"GitHub API {method} {endpoint} failed: {type(exc).__name__}") from exc
        if expected_status is not None and status != expected_status:
            raise ForgeError(
                f"GitHub API {method} {endpoint} returned HTTP {status}, expected {expected_status}"
            )
        if not raw:
            return None
        try:
            return strict_json_loads(raw, label=f"GitHub API {endpoint}")
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc

    def get(self, endpoint: str) -> Any:
        return self._request("GET", endpoint)

    def get_optional(self, endpoint: str) -> Any:
        return self._request("GET", endpoint, expected_status=200, expected_not_found=True)

    def post(self, endpoint: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", endpoint, payload)

    def post_created(self, endpoint: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", endpoint, payload, expected_status=201)

    def put(self, endpoint: str, payload: dict[str, Any]) -> Any:
        return self._request("PUT", endpoint, payload)

    def delete(self, endpoint: str) -> None:
        self._request("DELETE", endpoint, expected_status=204)

    def pages(self, endpoint: str) -> list[Any]:
        values: list[Any] = []
        for page in range(1, 101):
            separator = "&" if "?" in endpoint else "?"
            result = self.get(f"{endpoint}{separator}per_page=100&page={page}")
            if not isinstance(result, list):
                raise ForgeError(f"GitHub API {endpoint} pagination returned a non-array")
            values.extend(result)
            if len(result) < 100:
                return values
        raise ForgeError(f"GitHub API {endpoint} exceeded the pagination bound")


def keyring_token() -> str:
    candidate = shutil.which("gh")
    if candidate is None:
        raise ForgeError("GitHub CLI is required only to access the existing github.com keyring")
    try:
        executable = Path(candidate).resolve(strict=True)
        mode = executable.stat().st_mode
    except OSError as exc:
        raise ForgeError("GitHub CLI realpath cannot be resolved") from exc
    if not executable.is_file() or mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ForgeError("GitHub CLI must be a regular file without group/world write access")
    home = pwd.getpwuid(os.getuid()).pw_dir
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GH_")
        and key not in {"GITHUB_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "XDG_CONFIG_HOME"}
    }
    environment["HOME"] = home
    try:
        result = subprocess.run(
            [
                str(executable),
                "auth",
                "token",
                "--hostname",
                "github.com",
                "--user",
                GITHUB_ACTOR_LOGIN,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeError("GitHub keyring access failed") from exc
    if result.returncode != 0:
        raise ForgeError("GitHub keyring has no usable github.com credential")
    token = result.stdout.strip()
    if not token or "\n" in token or "\r" in token:
        raise ForgeError("GitHub keyring returned an invalid token")
    return token


def _require_api_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ForgeError(f"{label} readback is not an object")
    return value


def _provider_timestamp(value: Any, label: str) -> datetime:
    """Parse one provider timestamp while preserving the readback error boundary."""

    try:
        return _timestamp(value, label)
    except ContractError as exc:
        raise ForgeError(str(exc)) from exc


def _jobs_readback(api: Any, endpoint: str) -> list[dict[str, Any]]:
    """Read one bounded Actions-jobs page in GitHub's object response shape."""

    value = _require_api_object(
        api.get(f"{endpoint}?filter=latest&per_page=100&page=1"),
        "workflow jobs",
    )
    if set(value) != {"total_count", "jobs"}:
        raise ForgeError("workflow jobs response has unsupported members")
    total_count = value.get("total_count")
    jobs = value.get("jobs")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or total_count > 100
        or not isinstance(jobs, list)
        or total_count != len(jobs)
        or any(not isinstance(item, dict) for item in jobs)
    ):
        raise ForgeError("workflow jobs response is incomplete or outside the one-page bound")
    return jobs


def _decode_content(item: Any, expected_path: str, label: str) -> bytes:
    value = _require_api_object(item, label)
    if value.get("type") != "file" or value.get("path") != expected_path or value.get("encoding") != "base64":
        raise ForgeError(f"{label} is not the exact regular file")
    content = value.get("content")
    if not isinstance(content, str):
        raise ForgeError(f"{label} lacks base64 content")
    if (
        "\r" in content
        or content.startswith("\n")
        or "\n\n" in content
        or any(character.isspace() and character != "\n" for character in content)
    ):
        raise ForgeError(f"{label} contains unsupported base64 whitespace")
    # GitHub's Contents API wraps base64 with LF line breaks.  Remove only
    # that provider-defined transport formatting; every other character still
    # passes through the strict RFC 4648 decoder below.
    normalized_content = content.replace("\n", "")
    try:
        raw = base64.b64decode(normalized_content, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ForgeError(f"{label} contains invalid base64") from exc
    if value.get("size") != len(raw) or value.get("sha") != git_blob_sha(raw):
        raise ForgeError(f"{label} byte identity differs from GitHub blob metadata")
    return raw


def _content_endpoint(repository_id: int, path: str, ref: str) -> str:
    quoted_path = urllib.parse.quote(path, safe="/")
    return f"/repositories/{repository_id}/contents/{quoted_path}?ref={ref}"


def _repository(api: Any, repository_id: int, name: str, branch: str, label: str) -> dict[str, Any]:
    value = _require_api_object(api.get(f"/repositories/{repository_id}"), label)
    if value.get("id") != repository_id or value.get("full_name") != name or value.get("default_branch") != branch:
        raise ForgeError(f"{label} immutable identity/default branch differs")
    if value.get("archived") is True or value.get("disabled") is True:
        raise ForgeError(f"{label} is archived or disabled")
    return value


def _head(api: Any, repository_id: int, branch: str, label: str) -> str:
    value = _require_api_object(api.get(f"/repositories/{repository_id}/commits/{urllib.parse.quote(branch, safe='')}"), label)
    sha = value.get("sha")
    if not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None:
        raise ForgeError(f"{label} lacks an exact SHA")
    return sha


def expected_ruleset(record: dict[str, Any], *, enforcement: str | None = None) -> dict[str, Any]:
    source_sha = record["workflowSource"]["commitSha"]
    if source_sha is None:
        raise ContractError("workflowSource.commitSha is unresolved")
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": enforcement or record["ruleset"]["enforcement"],
        "bypass_actors": [],
        "conditions": {
            "repository_id": {"repository_ids": [TARGET_REPOSITORY_ID]},
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
        },
        "rules": [{
            "type": "workflows",
            "parameters": {
                "do_not_enforce_on_create": False,
                "workflows": [{
                    "path": WORKFLOW_PATH,
                    "repository_id": EXECUTOR_REPOSITORY_ID,
                    "ref": EXECUTOR_BRANCH,
                    "sha": source_sha,
                }],
            },
        }],
    }


def normalize_ruleset(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ForgeError("ruleset readback is not an object")
    item = value

    def semantic_object(candidate: Any, label: str, keys: set[str]) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            raise ForgeError(f"{label} is not an object")
        missing = sorted(keys - candidate.keys())
        unknown = sorted(candidate.keys() - keys)
        if missing:
            raise ForgeError(f"{label} lacks semantic members {missing}")
        if unknown:
            raise ForgeError(f"{label} contains unsupported semantic members {unknown}")
        return candidate

    conditions = semantic_object(
        item.get("conditions"),
        "ruleset conditions",
        {"repository_id", "ref_name"},
    )
    repository = semantic_object(
        conditions["repository_id"],
        "ruleset repository selector",
        {"repository_ids"},
    )
    ref = semantic_object(
        conditions["ref_name"],
        "ruleset ref selector",
        {"include", "exclude"},
    )
    rules = item.get("rules")
    if not isinstance(rules, list):
        raise ForgeError("ruleset rules is not an array")
    normalized_rules: list[dict[str, Any]] = []
    for index, candidate in enumerate(rules):
        rule = semantic_object(candidate, f"ruleset rule {index}", {"type", "parameters"})
        parameters = semantic_object(
            rule["parameters"],
            f"ruleset rule {index} parameters",
            {"do_not_enforce_on_create", "workflows"},
        )
        workflows = parameters["workflows"]
        if not isinstance(workflows, list):
            raise ForgeError(f"ruleset rule {index} workflows is not an array")
        normalized_workflows: list[dict[str, Any]] = []
        for workflow_index, candidate_workflow in enumerate(workflows):
            workflow = semantic_object(
                candidate_workflow,
                f"ruleset rule {index} workflow {workflow_index}",
                {"path", "repository_id", "ref", "sha"},
            )
            normalized_workflows.append({
                "path": workflow["path"],
                "repository_id": workflow["repository_id"],
                "ref": workflow["ref"],
                "sha": workflow["sha"],
            })
        normalized_rules.append({
            "type": rule["type"],
            "parameters": {
                "do_not_enforce_on_create": parameters["do_not_enforce_on_create"],
                "workflows": normalized_workflows,
            },
        })
    repository_ids = repository["repository_ids"]
    if (
        not isinstance(repository_ids, list)
        or any(isinstance(repository_id, bool) or not isinstance(repository_id, int) for repository_id in repository_ids)
    ):
        raise ForgeError("ruleset repository selector IDs are not an integer array")
    return {
        "name": item.get("name"),
        "target": item.get("target"),
        "enforcement": item.get("enforcement"),
        "bypass_actors": item.get("bypass_actors") if isinstance(item.get("bypass_actors"), list) else None,
        "conditions": {
            "repository_id": {"repository_ids": sorted(repository_ids)},
            "ref_name": {"include": ref["include"], "exclude": ref["exclude"]},
        },
        "rules": normalized_rules,
    }


def _normalized_pull_files(values: list[Any]) -> list[dict[str, Any]]:
    result = [{
        "filename": value.get("filename"),
        "status": value.get("status"),
        "sha": value.get("sha"),
        "previousFilename": value.get("previous_filename"),
        "additions": value.get("additions"),
        "deletions": value.get("deletions"),
        "changes": value.get("changes"),
        "patch": value.get("patch"),
    } for value in values if isinstance(value, dict)]
    return sorted(result, key=lambda item: str(item["filename"]))


def _normalized_comparison(value: dict[str, Any]) -> dict[str, Any]:
    base = value.get("base_commit") if isinstance(value.get("base_commit"), dict) else {}
    merge = value.get("merge_base_commit") if isinstance(value.get("merge_base_commit"), dict) else {}
    commits = value.get("commits") if isinstance(value.get("commits"), list) else []
    files = value.get("files") if isinstance(value.get("files"), list) else []
    return {
        "status": value.get("status"),
        "aheadBy": value.get("ahead_by"),
        "behindBy": value.get("behind_by"),
        "totalCommits": value.get("total_commits"),
        "baseCommitSha": base.get("sha"),
        "mergeBaseCommitSha": merge.get("sha"),
        "commitShas": [item.get("sha") for item in commits if isinstance(item, dict)],
        "files": _normalized_pull_files(files),
    }


def _normalized_commit(value: dict[str, Any]) -> dict[str, Any]:
    tree = value.get("tree") if isinstance(value.get("tree"), dict) else {}
    parents = value.get("parents") if isinstance(value.get("parents"), list) else []
    return {"sha": value.get("sha"), "treeSha": tree.get("sha"), "parentShas": [item.get("sha") for item in parents if isinstance(item, dict)]}


def _normalized_check(value: dict[str, Any]) -> dict[str, Any]:
    app = value.get("app") if isinstance(value.get("app"), dict) else {}
    return {
        "id": value.get("id"), "name": value.get("name"), "status": value.get("status"),
        "conclusion": value.get("conclusion"), "headSha": value.get("head_sha"),
        "detailsUrl": value.get("details_url"), "app": {"id": app.get("id"), "slug": app.get("slug")},
    }


def _run_observation(run: dict[str, Any], job: dict[str, Any], suite: dict[str, Any]) -> dict[str, Any]:
    repository = run.get("repository") if isinstance(run.get("repository"), dict) else {}
    return {
        "runId": run.get("id"), "runAttempt": run.get("run_attempt"), "repositoryId": repository.get("id"),
        "event": run.get("event"), "status": run.get("status"), "conclusion": run.get("conclusion"),
        "headSha": run.get("head_sha"), "path": run.get("path"),
        "requiredCheck": {"name": job.get("name"), "status": job.get("status"), "conclusion": job.get("conclusion")},
        "ruleSuite": suite,
    }


class RulesetExecutor:
    def __init__(
        self,
        api: Any,
        local_executor_bytes: bytes,
        *,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.api = api
        self.local_executor_bytes = local_executor_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))
        self.executor_head: str | None = None
        self.doctrine_head: str | None = None

    def _verify_executor(self) -> dict[str, Any]:
        _repository(self.api, EXECUTOR_REPOSITORY_ID, EXECUTOR_REPOSITORY, EXECUTOR_BRANCH, "executor repository")
        head = _head(self.api, EXECUTOR_REPOSITORY_ID, EXECUTOR_BRANCH, "executor default branch")
        remote = _decode_content(
            self.api.get(_content_endpoint(EXECUTOR_REPOSITORY_ID, EXECUTOR_PATH, head)),
            EXECUTOR_PATH,
            "executor source",
        )
        if remote != self.local_executor_bytes:
            raise ForgeError("local executor bytes differ from protected executor main")
        self.executor_head = head
        return {"repositoryId": EXECUTOR_REPOSITORY_ID, "commitSha": head, "path": EXECUTOR_PATH, "exactBytesDigest": exact_digest(remote)}

    def _load_doctrine(self) -> tuple[dict[str, Any], dict[str, Any]]:
        _repository(self.api, DOCTRINE_REPOSITORY_ID, DOCTRINE_REPOSITORY, DOCTRINE_BRANCH, "Doctrine repository")
        head = _head(self.api, DOCTRINE_REPOSITORY_ID, DOCTRINE_BRANCH, "Doctrine default branch")
        item = self.api.get(_content_endpoint(DOCTRINE_REPOSITORY_ID, DOCTRINE_RECORD_PATH, head))
        raw = _decode_content(item, DOCTRINE_RECORD_PATH, "Doctrine desired-state record")
        record = validate_record(strict_json_loads(raw, label="Doctrine desired-state record"))
        self.doctrine_head = head
        metadata = _require_api_object(item, "Doctrine desired-state record")
        return record, {
            "repositoryId": DOCTRINE_REPOSITORY_ID, "commitSha": head, "path": DOCTRINE_RECORD_PATH,
            "gitBlobSha": metadata.get("sha"), "exactBytesDigest": exact_digest(raw),
            "semanticDigest": canonical_digest(record),
        }

    def _verify_source(self, record: dict[str, Any]) -> dict[str, Any]:
        source_sha = record["workflowSource"]["commitSha"]
        if source_sha is None:
            raise ForgeError("workflow source commit is unresolved")
        _repository(self.api, EXECUTOR_REPOSITORY_ID, EXECUTOR_REPOSITORY, EXECUTOR_BRANCH, "workflow source repository")
        commit = _require_api_object(self.api.get(f"/repositories/{EXECUTOR_REPOSITORY_ID}/commits/{source_sha}"), "workflow source commit")
        if commit.get("sha") != source_sha:
            raise ForgeError("workflow source commit readback differs")
        comparison = _require_api_object(
            self.api.get(f"/repositories/{EXECUTOR_REPOSITORY_ID}/compare/{source_sha}...{EXECUTOR_BRANCH}"),
            "workflow source ancestry",
        )
        base = comparison.get("base_commit") if isinstance(comparison.get("base_commit"), dict) else {}
        if base.get("sha") != source_sha or comparison.get("status") not in {"ahead", "identical"}:
            raise ForgeError("workflow source commit is not reachable from protected source main")
        files = []
        for path in SOURCE_PATHS:
            item = self.api.get(_content_endpoint(EXECUTOR_REPOSITORY_ID, path, source_sha))
            raw = _decode_content(item, path, f"workflow source {path}")
            metadata = _require_api_object(item, f"workflow source {path}")
            files.append({"path": path, "gitBlobSha": metadata.get("sha"), "exactBytesDigest": exact_digest(raw)})
            if path == WORKFLOW_PATH:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ForgeError("workflow source is not UTF-8") from exc
                expected_selector = f"if: ${{{{ github.repository_id == {TARGET_REPOSITORY_ID} }}}}"
                stripped_lines = [line.strip() for line in text.splitlines()]
                selector_lines = [line for line in stripped_lines if "github.repository_id" in line]
                if (
                    stripped_lines.count(f"name: {WORKFLOW_NAME}") != 1
                    or stripped_lines.count(f"name: {REQUIRED_CHECK}") != 1
                    or selector_lines != [expected_selector]
                ):
                    raise ForgeError("workflow source does not expose the immutable workflow/check/target identity")
        return {"repositoryId": EXECUTOR_REPOSITORY_ID, "commitSha": source_sha, "files": files}

    def _verify_target(self) -> dict[str, Any]:
        value = _require_api_object(self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}"), "target repository")
        if value.get("id") != TARGET_REPOSITORY_ID or value.get("node_id") != TARGET_REPOSITORY_NODE_ID:
            raise ForgeError("target repository numeric/node identity differs")
        if value.get("full_name") not in TARGET_REPOSITORY_NAMES:
            raise ForgeError("target repository name is outside the controlled rename states")
        if value.get("default_branch") != TARGET_DEFAULT_BRANCH:
            raise ForgeError("target repository default branch differs from the immutable executor contract")
        return {"id": value["id"], "nodeId": value["node_id"], "name": value["full_name"], "defaultBranch": value["default_branch"], "visibility": value.get("visibility")}

    def _verify_organization(self) -> None:
        value = _require_api_object(self.api.get(f"/orgs/{ORGANIZATION}"), "organization")
        if value.get("id") != ORGANIZATION_ID or value.get("login") != ORGANIZATION:
            raise ForgeError("organization identity differs from the immutable executor contract")

    def _verify_actor(self) -> dict[str, Any]:
        value = _require_api_object(self.api.get("/user"), "authenticated GitHub actor")
        if (
            value.get("id") != GITHUB_ACTOR_ID
            or value.get("login") != GITHUB_ACTOR_LOGIN
            or value.get("type") != GITHUB_ACTOR_TYPE
        ):
            raise ForgeError("authenticated GitHub actor differs from the immutable executor contract")
        return {
            "id": GITHUB_ACTOR_ID,
            "login": GITHUB_ACTOR_LOGIN,
            "type": GITHUB_ACTOR_TYPE,
        }

    def _lock_ref_sha(self, value: Any, label: str) -> str:
        ref = _require_api_object(value, label)
        target = ref.get("object") if isinstance(ref.get("object"), dict) else {}
        tag_sha = target.get("sha")
        if (
            ref.get("ref") != APPLY_LOCK_REF
            or target.get("type") != "tag"
            or not isinstance(tag_sha, str)
            or SHA_RE.fullmatch(tag_sha) is None
        ):
            raise ForgeError(f"{label} does not bind the fixed annotated-tag lock")
        return tag_sha

    def _read_lock_ref_sha(self) -> str | None:
        value = self.api.get_optional(APPLY_LOCK_REF_GET_ENDPOINT)
        if value is None:
            return None
        return self._lock_ref_sha(value, "apply lock ref")

    def _verify_lock_tag(self, lock: dict[str, Any], value: Any | None = None) -> None:
        tag_sha = lock["tagObjectSha"]
        item = _require_api_object(
            value if value is not None else self.api.get(f"{APPLY_LOCK_TAGS_ENDPOINT}/{tag_sha}"),
            "apply lock annotated tag",
        )
        target = item.get("object") if isinstance(item.get("object"), dict) else {}
        if (
            item.get("sha") != tag_sha
            or item.get("tag") != APPLY_LOCK_TAG_NAME
            or item.get("message") != lock["tagMessage"]
            or target.get("type") != "commit"
            or target.get("sha") != lock["executorCommitSha"]
        ):
            raise ForgeError("apply lock annotated tag does not bind this acquisition")

    def _verify_apply_lock(self, lock: dict[str, Any]) -> None:
        if self._read_lock_ref_sha() != lock["tagObjectSha"]:
            raise ForgeError("apply lock ownership was lost")
        self._verify_lock_tag(lock)

    def _release_apply_lock(self, lock: dict[str, Any]) -> None:
        self._verify_apply_lock(lock)
        try:
            self.api.delete(APPLY_LOCK_REF_DELETE_ENDPOINT)
        except ForgeError as exc:
            try:
                current = self._read_lock_ref_sha()
            except ForgeError as read_exc:
                raise ForgeError("apply lock release failed and final ownership is unreadable") from read_exc
            if current is None:
                return
            if current != lock["tagObjectSha"]:
                raise ForgeError("apply lock release lost ownership; successor lock was not deleted") from exc
            raise ForgeError("apply lock release failed or remained uncertain") from exc
        if self._read_lock_ref_sha() is not None:
            raise ForgeError("apply lock ref still exists after release")

    def _abort_failed_acquire(self, lock: dict[str, Any], cause: ForgeError) -> None:
        try:
            current = self._read_lock_ref_sha()
        except ForgeError as read_exc:
            raise ForgeError("apply lock acquisition failed and lock ownership is unreadable") from read_exc
        if current is None:
            raise ForgeError("apply lock acquisition failed before ownership was established") from cause
        if current != lock["tagObjectSha"]:
            raise ForgeError("apply lock is held by another acquisition") from cause
        try:
            self._release_apply_lock(lock)
        except ForgeError as release_exc:
            raise ForgeError("apply lock acquisition failed and owned-lock cleanup failed") from release_exc
        raise ForgeError("apply lock acquisition failed closed after releasing its owned ref") from cause

    def _acquire_apply_lock(
        self,
        actor: dict[str, Any],
        claimed_at: str,
    ) -> dict[str, Any]:
        if self.executor_head is None:
            raise ForgeError("executor head was not established before apply lock acquisition")
        nonce = self.nonce_factory()
        if not isinstance(nonce, str) or NONCE_RE.fullmatch(nonce) is None:
            raise ForgeError("apply lock nonce source returned an invalid 32-byte hex nonce")
        claim = {
            "schemaVersion": 1,
            "kind": "public-skills-ruleset-apply-lock",
            "repositoryId": EXECUTOR_REPOSITORY_ID,
            "ref": APPLY_LOCK_REF,
            "executorCommitSha": self.executor_head,
            "actor": actor,
            "nonce": nonce,
            "claimedAt": claimed_at,
        }
        message = canonical_bytes(claim).decode("utf-8")
        tag_payload = {
            "tag": APPLY_LOCK_TAG_NAME,
            "message": message,
            "object": self.executor_head,
            "type": "commit",
        }
        tag_response = _require_api_object(
            self.api.post_created(APPLY_LOCK_TAGS_ENDPOINT, tag_payload),
            "apply lock tag creation",
        )
        tag_sha = tag_response.get("sha")
        if not isinstance(tag_sha, str) or SHA_RE.fullmatch(tag_sha) is None:
            raise ForgeError("apply lock tag creation lacks an exact Git object SHA")
        lock = {
            "repositoryId": EXECUTOR_REPOSITORY_ID,
            "ref": APPLY_LOCK_REF,
            "tagObjectSha": tag_sha,
            "tagMessageDigest": exact_digest(message.encode("utf-8")),
            "executorCommitSha": self.executor_head,
            "nonce": nonce,
            "actor": copy.deepcopy(actor),
            "acquireOutcome": "acquired",
            "releaseOutcome": "pending",
            "tagMessage": message,
        }
        self._verify_lock_tag(lock, tag_response)
        try:
            ref_response = self.api.post_created(
                APPLY_LOCK_REFS_ENDPOINT,
                {"ref": APPLY_LOCK_REF, "sha": tag_sha},
            )
        except ForgeError as exc:
            self._abort_failed_acquire(lock, exc)
            raise AssertionError("unreachable")
        try:
            if self._lock_ref_sha(ref_response, "apply lock create response") != tag_sha:
                raise ForgeError("apply lock create response points to another tag object")
            self._verify_apply_lock(lock)
            if _head(self.api, EXECUTOR_REPOSITORY_ID, EXECUTOR_BRANCH, "executor post-lock head") != self.executor_head:
                raise ForgeError("executor main changed during apply lock acquisition")
        except ForgeError as exc:
            self._abort_failed_acquire(lock, exc)
            raise AssertionError("unreachable")
        return lock

    def _live_ruleset(self, record: dict[str, Any]) -> dict[str, Any] | None:
        summaries = self.api.pages(f"/orgs/{ORGANIZATION}/rulesets")
        matches = [item for item in summaries if isinstance(item, dict) and item.get("name") == RULESET_NAME]
        if len(matches) > 1:
            raise ForgeError("multiple organization rulesets share the immutable name")
        bound = record["ruleset"]["rulesetId"]
        if bound is None:
            if matches:
                raise ForgeError("live ruleset exists but Doctrine has not bound its numeric ID")
            return None
        if not matches or matches[0].get("id") != bound:
            raise ForgeError("Doctrine ruleset ID/name does not bind exactly one live ruleset")
        value = _require_api_object(self.api.get(f"/orgs/{ORGANIZATION}/rulesets/{bound}"), "organization ruleset")
        if value.get("id") != bound or value.get("name") != RULESET_NAME or value.get("source_type") not in {None, "Organization"}:
            raise ForgeError("bound live ruleset identity/ownership differs")
        return value

    def _effective(self, target: dict[str, Any], ruleset_id: int | None) -> list[dict[str, Any]]:
        if ruleset_id is None:
            return []
        values = self.api.pages(f"/repositories/{TARGET_REPOSITORY_ID}/rulesets?includes_parents=true")
        return [{
            "repositoryId": TARGET_REPOSITORY_ID,
            "repository": target["name"],
            "rulesetId": ruleset_id,
            "rulesetPresent": any(isinstance(item, dict) and item.get("id") == ruleset_id for item in values),
        }]

    def _verify_workflow_evidence(
        self,
        record: dict[str, Any],
        target: dict[str, Any],
        field: str,
        evidence: dict[str, Any],
        *,
        not_before: datetime | None,
    ) -> dict[str, Any]:
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/actions/runs/([1-9][0-9]*)/?", evidence["locator"])
        if match is None or match.group(1) != target["name"]:
            raise ForgeError(f"{field} locator does not bind the live target")
        run_id = int(match.group(2))
        bindings = evidence["bindings"]
        run = _require_api_object(self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}/actions/runs/{run_id}"), f"{field} run")
        repository = run.get("repository") if isinstance(run.get("repository"), dict) else {}
        expected_event = "merge_group" if field == "mergeGroupCanary" else "pull_request"
        expected_conclusion = "failure" if field == "negativeControl" else "success"
        if run.get("id") != run_id or repository.get("id") != TARGET_REPOSITORY_ID:
            raise ForgeError(f"{field} run identity differs")
        if run.get("head_sha") != bindings["headSha"] or run.get("event") != expected_event:
            raise ForgeError(f"{field} run head/event differs")
        if run.get("status") != "completed" or run.get("conclusion") != expected_conclusion:
            raise ForgeError(f"{field} run must be completed/{expected_conclusion}")
        path = run.get("path")
        if not isinstance(path, str) or not (path == WORKFLOW_PATH or path.startswith(WORKFLOW_PATH + "@")):
            raise ForgeError(f"{field} run path differs")
        run_created = _provider_timestamp(run.get("created_at"), f"{field} run.created_at")
        run_updated = _provider_timestamp(run.get("updated_at"), f"{field} run.updated_at")
        jobs = _jobs_readback(
            self.api,
            f"/repositories/{TARGET_REPOSITORY_ID}/actions/runs/{run_id}/jobs",
        )
        if any(item.get("head_sha") != bindings["headSha"] for item in jobs):
            raise ForgeError(f"{field} contains a foreign job head")
        matches = [item for item in jobs if item.get("name") == REQUIRED_CHECK]
        if len(matches) != 1:
            raise ForgeError(f"{field} does not resolve exactly one required-check job")
        job = matches[0]
        if job.get("status") != "completed" or job.get("conclusion") != expected_conclusion:
            raise ForgeError(f"{field} required-check job has the wrong conclusion")
        suite = _require_api_object(
            self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}/rulesets/rule-suites/{bindings['ruleSuiteId']}"),
            f"{field} rule suite",
        )
        if suite.get("id") != bindings["ruleSuiteId"] or suite.get("repository_id") != TARGET_REPOSITORY_ID or suite.get("after_sha") != bindings["headSha"]:
            raise ForgeError(f"{field} rule-suite identity/head differs")
        if suite.get("ref") != f"refs/heads/{target['defaultBranch']}":
            raise ForgeError(f"{field} rule-suite ref differs")
        expected_result = "fail" if field == "negativeControl" else "pass"
        if suite.get("evaluation_result") != expected_result:
            raise ForgeError(f"{field} rule-suite result differs")
        suite_pushed = _provider_timestamp(suite.get("pushed_at"), f"{field} rule-suite.pushed_at")
        evidence_observed = _provider_timestamp(evidence["observedAt"], f"{field} evidence.observedAt")
        if suite_pushed > run_created or run_created > run_updated:
            raise ForgeError(f"{field} provider chronology is inconsistent")
        if not_before is not None and any(
            observed <= not_before
            for observed in (run_created, run_updated, suite_pushed)
        ):
            raise ForgeError(f"{field} predates the current evaluate ruleset revision")
        if suite_pushed != evidence_observed:
            raise ForgeError(f"{field} rule-suite time does not bind evidence")
        evaluations = suite.get("rule_evaluations") if isinstance(suite.get("rule_evaluations"), list) else []
        selected = [item for item in evaluations if isinstance(item, dict) and isinstance(item.get("rule_source"), dict) and item["rule_source"].get("id") == bindings["rulesetId"] and item.get("rule_type") == "workflows" and item.get("enforcement") == "evaluate"]
        if len(selected) != 1 or selected[0].get("result") != expected_result:
            raise ForgeError(f"{field} lacks one exact evaluate-mode rule verdict")
        suite_observation = {
            "id": suite.get("id"), "repositoryId": suite.get("repository_id"), "afterSha": suite.get("after_sha"),
            "ref": suite.get("ref"), "evaluationResult": suite.get("evaluation_result"),
            # Preserve the Doctrine digest key while sourcing it from GitHub's real rule-suite timestamp.
            "updatedAt": suite.get("pushed_at"), "ruleEvaluation": selected[0],
        }
        observation = _run_observation(run, job, suite_observation)
        if field == "negativeControl":
            observation["negativeControl"] = self._verify_negative(record, target, run_id, run, evidence)
        if evidence["subjectDigest"] != canonical_digest(observation):
            raise ForgeError(f"{field} subject digest does not bind live evidence")
        return {
            "runId": run_id,
            "ruleSuiteId": bindings["ruleSuiteId"],
            "runCreatedAt": run["created_at"],
            "runUpdatedAt": run["updated_at"],
            "ruleSuitePushedAt": suite["pushed_at"],
            "observationDigest": canonical_digest(observation),
        }

    def _verify_negative(self, record: dict[str, Any], target: dict[str, Any], run_id: int, run: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        proof = evidence["negativeControl"]
        policy = record["workflowSource"]["negativeControlPolicy"]
        number = proof["pullRequestNumber"]
        pr = _require_api_object(self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}/pulls/{number}"), "negative-control pull request")
        base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
        head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
        if pr.get("number") != number or pr.get("merged_at") is not None or base_repo.get("id") != TARGET_REPOSITORY_ID or head_repo.get("id") != TARGET_REPOSITORY_ID:
            raise ForgeError("negative control must bind one unmerged same-repository PR")
        if proof["targetRef"] != f"refs/heads/{target['defaultBranch']}" or proof["targetRef"] != f"refs/heads/{base.get('ref')}" or proof["headRef"] != f"refs/heads/{head.get('ref')}":
            raise ForgeError("negative-control PR refs differ")
        fixture_base = proof["fixtureBaseSha"]
        fixture_head = proof["fixtureHeadSha"]
        if head.get("sha") != fixture_head or run.get("head_sha") != fixture_head:
            raise ForgeError("negative-control PR/run head differs")
        source_item = self.api.get(_content_endpoint(EXECUTOR_REPOSITORY_ID, POLICY_PATH, record["workflowSource"]["commitSha"]))
        source_policy = strict_json_loads(_decode_content(source_item, POLICY_PATH, "negative-control source policy"), label="negative-control source policy")
        source_target = source_policy.get("target") if isinstance(source_policy, dict) and isinstance(source_policy.get("target"), dict) else {}
        source_baseline = source_target.get("baseline") if isinstance(source_target.get("baseline"), dict) else {}
        if source_target.get("repositoryId") != TARGET_REPOSITORY_ID or source_baseline.get("commit") != fixture_base or source_baseline.get("tree") != proof["fixtureBaseTree"]:
            raise ForgeError("negative-control baseline differs from pinned source policy")
        base_commit = _normalized_commit(_require_api_object(self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}/git/commits/{fixture_base}"), "negative-control base commit"))
        head_commit = _normalized_commit(_require_api_object(self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}/git/commits/{fixture_head}"), "negative-control head commit"))
        if base_commit != {"sha": fixture_base, "treeSha": proof["fixtureBaseTree"], "parentShas": base_commit["parentShas"]}:
            raise ForgeError("negative-control base commit/tree differs")
        if head_commit != {"sha": fixture_head, "treeSha": proof["fixtureHeadTree"], "parentShas": [fixture_base]}:
            raise ForgeError("negative-control head is not an exact direct child")
        merge_sha = proof["pullRequestMergeCommitSha"]
        if pr.get("merge_commit_sha") != merge_sha:
            raise ForgeError("negative-control PR merge commit differs")
        merge = _normalized_commit(_require_api_object(self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}/git/commits/{merge_sha}"), "negative-control merge commit"))
        if merge != {"sha": merge_sha, "treeSha": proof["fixtureHeadTree"], "parentShas": [base.get("sha"), fixture_head]} or proof["mergeCommitDigest"] != canonical_digest(merge):
            raise ForgeError("negative-control merge composition/digest differs")
        pr_files = _normalized_pull_files(self.api.pages(f"/repositories/{TARGET_REPOSITORY_ID}/pulls/{number}/files"))
        if proof["pullRequestFilesDigest"] != canonical_digest(pr_files):
            raise ForgeError("negative-control full PR diff digest differs")
        comparison = _normalized_comparison(_require_api_object(self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}/compare/{fixture_base}...{fixture_head}"), "negative-control comparison"))
        if comparison["status"] != "ahead" or comparison["aheadBy"] != 1 or comparison["behindBy"] != 0 or comparison["totalCommits"] != 1 or comparison["baseCommitSha"] != fixture_base or comparison["mergeBaseCommitSha"] != fixture_base or comparison["commitShas"] != [fixture_head]:
            raise ForgeError("negative-control fixture is not exactly one commit ahead")
        if len(comparison["files"]) != 1 or comparison["files"][0]["filename"] != "package.json" or comparison["files"][0]["status"] != "modified" or proof["fixtureComparisonDigest"] != canonical_digest(comparison):
            raise ForgeError("negative-control fixture diff/digest differs")
        base_bytes = _decode_content(self.api.get(_content_endpoint(TARGET_REPOSITORY_ID, "package.json", fixture_base)), "package.json", "negative-control base fixture")
        head_bytes = _decode_content(self.api.get(_content_endpoint(TARGET_REPOSITORY_ID, "package.json", fixture_head)), "package.json", "negative-control head fixture")
        base_json = strict_json_loads(base_bytes, label="negative-control base fixture")
        head_json = strict_json_loads(head_bytes, label="negative-control head fixture")
        expected = copy.deepcopy(base_json)
        if not isinstance(expected, dict) or not isinstance(expected.get("scripts"), dict):
            raise ForgeError("negative-control base fixture lacks package scripts")
        for name, command in policy["scriptOverrides"].items():
            if not isinstance(expected["scripts"].get(name), str) or expected["scripts"][name] == command:
                raise ForgeError(f"negative-control base {name} script is absent or already neutral")
            expected["scripts"][name] = command
        if head_json != expected or proof["fixtureDigest"] != exact_digest(head_bytes) or proof["fixtureSemanticDigest"] != canonical_digest(head_json):
            raise ForgeError("negative-control fixture semantics/digests differ")
        checks = _require_api_object(self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}/commits/{fixture_head}/check-runs?filter=latest&per_page=100"), "negative-control checks")
        raw = checks.get("check_runs") if isinstance(checks.get("check_runs"), list) else []
        if checks.get("total_count") != len(raw):
            raise ForgeError("negative-control check readback is incomplete")
        normalized = sorted((_normalized_check(item) for item in raw if isinstance(item, dict)), key=lambda item: (str(item["name"]), str(item["id"])))
        for context in (*LOCAL_REQUIRED_CHECKS, REQUIRED_CHECK):
            selected = [item for item in normalized if item["name"] == context]
            expected_conclusion = "failure" if context == REQUIRED_CHECK else "success"
            if len(selected) != 1 or selected[0]["headSha"] != fixture_head or selected[0]["status"] != "completed" or selected[0]["conclusion"] != expected_conclusion:
                raise ForgeError(f"negative-control {context} is not completed/{expected_conclusion}")
            if context == REQUIRED_CHECK:
                details = re.fullmatch(rf"https://github\.com/{re.escape(target['name'])}/actions/runs/([1-9][0-9]*)(?:/job/[1-9][0-9]*)?/?", str(selected[0]["detailsUrl"]))
                if details is None or int(details.group(1)) != run_id:
                    raise ForgeError("negative-control external check URL differs from the admitted run")
        failure = {"action_required", "cancelled", "failure", "startup_failure", "stale", "timed_out"}
        failed = [item for item in normalized if item["conclusion"] in failure]
        if any(item["headSha"] != fixture_head for item in normalized) or len(failed) != 1 or failed[0]["name"] != REQUIRED_CHECK or proof["contextsDigest"] != canonical_digest(normalized):
            raise ForgeError("negative-control exact-head context set differs")
        return {
            "pullRequestNumber": number, "targetRef": proof["targetRef"], "headRef": proof["headRef"],
            "pullRequestBaseSha": base.get("sha"), "fixtureBaseSha": fixture_base,
            "fixtureBaseTree": proof["fixtureBaseTree"], "fixtureHeadSha": fixture_head,
            "fixtureHeadTree": proof["fixtureHeadTree"],
            "sourceBaseline": {"repositoryId": source_target.get("repositoryId"), "commit": source_baseline.get("commit"), "tree": source_baseline.get("tree")},
            "mutationClass": proof["mutationClass"], "mutationPath": "package.json",
            "comparisonDigest": canonical_digest(comparison), "pullRequestFilesDigest": canonical_digest(pr_files),
            "mergeCommit": merge, "fixtureDigest": exact_digest(head_bytes),
            "fixtureSemanticDigest": canonical_digest(head_json), "contexts": normalized,
        }

    def _verify_activation(
        self,
        record: dict[str, Any],
        live: dict[str, Any],
        target: dict[str, Any],
        effective: list[dict[str, Any]],
    ) -> dict[str, Any]:
        evidence = record["activationEvidence"]
        ruleset_id = record["ruleset"]["rulesetId"]
        evaluate = evidence["evaluateReadback"]
        expected_locator = f"https://github.com/organizations/{ORGANIZATION}/settings/rules/{ruleset_id}"
        if evaluate["locator"].rstrip("/") != expected_locator or evaluate["subjectDigest"] != canonical_digest(expected_ruleset(record, enforcement="evaluate")):
            raise ForgeError("evaluate readback evidence differs from exact evaluate desired state")
        live_evaluate = normalize_ruleset(live)
        live_enforcement = live_evaluate["enforcement"]
        ruleset_updated = _provider_timestamp(live.get("updated_at"), "live ruleset.updated_at")
        not_before: datetime | None = None
        if live_enforcement == "evaluate":
            if _provider_timestamp(evaluate["observedAt"], "evaluate readback observedAt") != ruleset_updated:
                raise ForgeError("evaluate readback time does not bind the current live ruleset revision")
            not_before = ruleset_updated
        elif live_enforcement == "active":
            live_evaluate["enforcement"] = "evaluate"
        if live_evaluate != expected_ruleset(record, enforcement="evaluate"):
            raise ForgeError("live pre-ratchet ruleset differs from exact evaluate state")
        workflow_readback: dict[str, Any] = {}
        for field in ["pullRequestCanary", "mergeGroupCanary", "negativeControl"]:
            workflow_readback[field] = self._verify_workflow_evidence(
                record,
                target,
                field,
                evidence[field],
                not_before=not_before,
            )
        effective_item = evidence["effectiveRulesReadback"]
        if effective_item["locator"] != f"https://github.com/{target['name']}/settings/rules" or not effective or not all(item["rulesetPresent"] for item in effective) or effective_item["subjectDigest"] != canonical_digest(effective):
            raise ForgeError("effective-rules evidence differs from live coverage")
        return {
            "rulesetId": ruleset_id,
            "liveEnforcement": live_enforcement,
            "rulesetUpdatedAt": live["updated_at"],
            "canaryNotBefore": live["updated_at"] if not_before is not None else None,
            "workflowEvidence": workflow_readback,
            "effectiveRulesDigest": canonical_digest(effective),
        }

    def _assert_heads_unchanged(self) -> None:
        if self.executor_head is None or self.doctrine_head is None:
            raise ForgeError("executor trust heads were not established")
        if _head(self.api, EXECUTOR_REPOSITORY_ID, EXECUTOR_BRANCH, "executor pre-mutation head") != self.executor_head:
            raise ForgeError("executor main changed before mutation")
        if _head(self.api, DOCTRINE_REPOSITORY_ID, DOCTRINE_BRANCH, "Doctrine pre-mutation head") != self.doctrine_head:
            raise ForgeError("Doctrine main changed before mutation")

    def _assert_live_precondition_unchanged(
        self,
        record: dict[str, Any],
        target: dict[str, Any],
        live: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        current_target = self._verify_target()
        if current_target != target:
            raise ForgeError("target repository identity changed before mutation")
        current_live = self._live_ruleset(record)
        if (live is None) != (current_live is None):
            raise ForgeError("organization ruleset presence changed before mutation")
        if live is not None and current_live is not None:
            if (
                live.get("id") != current_live.get("id")
                or live.get("updated_at") != current_live.get("updated_at")
                or normalize_ruleset(live) != normalize_ruleset(current_live)
            ):
                raise ForgeError("organization ruleset precondition changed before mutation")
        return current_target, current_live

    def _reconcile(
        self,
        mode: str,
        report: dict[str, Any],
        lock: dict[str, Any] | None,
    ) -> dict[str, Any]:
        record, desired = self._load_doctrine()
        report["desiredState"] = desired
        report["phase"] = record["migration"]["phase"]
        self._verify_organization()
        target = self._verify_target()
        report["target"] = target
        if record["workflowSource"]["commitSha"] is None:
            report["status"] = "BLOCKED"
            report["findings"].append("workflow source SHA is unresolved in canonical Doctrine main")
            return report
        report["source"] = self._verify_source(record)
        live = self._live_ruleset(record)
        ruleset_id = record["ruleset"]["rulesetId"]
        effective = self._effective(target, ruleset_id)
        phase = record["migration"]["phase"]
        if live is not None:
            normalized = normalize_ruleset(live)
            current_enforcement = normalized["enforcement"]
            desired_enforcement = record["ruleset"]["enforcement"]
            if current_enforcement not in ENFORCEMENT_RANK:
                raise ForgeError("live ruleset enforcement is unsupported")
            if (
                phase != "recovery"
                and ENFORCEMENT_RANK[desired_enforcement] < ENFORCEMENT_RANK[current_enforcement]
            ):
                raise ForgeError("non-recovery phases cannot downgrade live enforcement")
            report["preReadback"] = {
                "rulesetId": live.get("id"),
                "updatedAt": live.get("updated_at"),
                "normalized": normalized,
                "digest": canonical_digest(normalized),
                "effectiveRules": effective,
                "effectiveRulesDigest": canonical_digest(effective),
            }
        desired_payload = expected_ruleset(record)

        if phase == "recovery":
            if live is None or ruleset_id is None:
                raise ForgeError("recovery requires the exact bound live ruleset")
            normalized = normalize_ruleset(live)
            if normalized["bypass_actors"] != []:
                raise ForgeError("recovery refuses a live ruleset with bypass actors")
            current = normalized["enforcement"]
            desired_enforcement = record["ruleset"]["enforcement"]
            if current not in ENFORCEMENT_RANK or ENFORCEMENT_RANK[desired_enforcement] > ENFORCEMENT_RANK[current]:
                raise ForgeError("recovery cannot escalate enforcement")
            structural = copy.deepcopy(normalized)
            structural["enforcement"] = desired_enforcement
            if structural != desired_payload:
                raise ForgeError("recovery permits an enforcement-only downgrade, not structural drift")
            if current == desired_enforcement:
                return report
            action = "recovery-downgrade"
        elif live is None:
            if phase != "expand" or ruleset_id is not None or record["ruleset"]["enforcement"] != "evaluate":
                raise ForgeError("only unbound expand/evaluate may create the organization ruleset")
            action = "create"
        else:
            if ruleset_id is None:
                raise ForgeError("live organization ruleset is not bound in Doctrine")
            if phase == "ratchet":
                report["activationReadback"] = self._verify_activation(record, live, target, effective)
            if normalize_ruleset(live) == desired_payload:
                if record["ruleset"]["enforcement"] == "active" and (not effective or not all(item["rulesetPresent"] for item in effective)):
                    raise ForgeError("active ruleset is absent from effective-rules readback")
                return report
            action = "update"

        report["plannedMutation"] = {"action": action, "rulesetId": ruleset_id, "payload": desired_payload, "payloadDigest": canonical_digest(desired_payload)}
        if mode == "readback":
            report["status"] = "DRIFT"
            report["findings"].append("live ruleset differs; readback mode does not plan or apply changes")
            report["plannedMutation"] = None
            return report
        if mode == "dry-run":
            report["status"] = "DRIFT"
            return report

        if lock is None:
            raise ForgeError("apply mode reached mutation planning without the fixed apply lock")
        self._assert_heads_unchanged()
        current_target, current_live = self._assert_live_precondition_unchanged(record, target, live)
        if phase == "ratchet":
            if ruleset_id is None or current_live is None:
                raise ForgeError("ratchet pre-mutation ruleset disappeared")
            current_effective = self._effective(current_target, ruleset_id)
            report["activationReadback"] = self._verify_activation(
                record,
                current_live,
                current_target,
                current_effective,
            )
        self._assert_heads_unchanged()
        self._assert_live_precondition_unchanged(record, target, live)
        self._verify_apply_lock(lock)
        report["mutation"] = {"attempted": True, "action": action, "outcome": "request-sent"}
        if action == "create":
            try:
                response = _require_api_object(
                    self.api.post(f"/orgs/{ORGANIZATION}/rulesets", desired_payload),
                    "create ruleset response",
                )
                created_id = _positive_integer(response.get("id"), "created ruleset ID")
            except (ContractError, ForgeError) as exc:
                report["status"] = "ERROR"
                report["mutation"]["outcome"] = "request-failed-or-outcome-unknown"
                report["findings"].append(str(exc))
                return report
            report["mutation"] = {"attempted": True, "action": action, "outcome": "created", "rulesetId": created_id}
            try:
                post = _require_api_object(
                    self.api.get(f"/orgs/{ORGANIZATION}/rulesets/{created_id}"),
                    "post-create ruleset",
                )
            except ForgeError as exc:
                report["status"] = "ERROR"
                report["mutation"]["outcome"] = "created-post-readback-unavailable"
                report["findings"].append(str(exc))
                return report
            try:
                post_normalized = normalize_ruleset(post)
            except ForgeError as exc:
                report["status"] = "ERROR"
                report["mutation"]["outcome"] = "created-post-readback-mismatch"
                report["findings"].append(str(exc))
                return report
            if post.get("id") != created_id or post_normalized != desired_payload:
                report["status"] = "ERROR"
                report["mutation"]["outcome"] = "created-post-readback-mismatch"
                report["findings"].append("created ruleset differs on immediate readback")
                return report
            report["postReadback"] = {"rulesetId": created_id, "normalized": post_normalized, "digest": canonical_digest(post_normalized)}
            report["status"] = "DRIFT"
            report["findings"].append(f"ruleset {created_id} created in evaluate mode; commit its ID to Doctrine before further mutation")
            return report

        assert ruleset_id is not None
        try:
            response = _require_api_object(
                self.api.put(f"/orgs/{ORGANIZATION}/rulesets/{ruleset_id}", desired_payload),
                "update ruleset response",
            )
        except ForgeError as exc:
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "request-failed-or-outcome-unknown"
            report["findings"].append(str(exc))
            return report
        if response.get("id") != ruleset_id:
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "updated-response-mismatch"
            report["findings"].append("updated ruleset response ID differs")
            return report
        try:
            post = _require_api_object(
                self.api.get(f"/orgs/{ORGANIZATION}/rulesets/{ruleset_id}"),
                "post-update ruleset",
            )
        except ForgeError as exc:
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "updated-post-readback-unavailable"
            report["findings"].append(str(exc))
            return report
        try:
            post_normalized = normalize_ruleset(post)
        except ForgeError as exc:
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "updated-post-readback-mismatch"
            report["findings"].append(str(exc))
            return report
        if post.get("id") != ruleset_id or post_normalized != desired_payload:
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "updated-post-readback-mismatch"
            report["findings"].append("updated ruleset differs on immediate readback")
            return report
        try:
            post_effective = self._effective(target, ruleset_id)
        except ForgeError as exc:
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "updated-effective-readback-unavailable"
            report["findings"].append(str(exc))
            return report
        if record["ruleset"]["enforcement"] == "active" and (not post_effective or not all(item["rulesetPresent"] for item in post_effective)):
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "updated-effective-readback-mismatch"
            report["findings"].append("active ruleset is absent from post-update effective rules")
            return report
        report["mutation"] = {"attempted": True, "action": action, "outcome": "updated", "rulesetId": ruleset_id}
        report["postReadback"] = {"rulesetId": ruleset_id, "normalized": post_normalized, "digest": canonical_digest(post_normalized), "effectiveRules": post_effective, "effectiveRulesDigest": canonical_digest(post_effective)}
        return report

    def _public_apply_lock(self, lock: dict[str, Any]) -> dict[str, Any]:
        return {
            "repositoryId": lock["repositoryId"],
            "ref": lock["ref"],
            "tagObjectSha": lock["tagObjectSha"],
            "tagMessageDigest": lock["tagMessageDigest"],
            "executorCommitSha": lock["executorCommitSha"],
            "nonce": lock["nonce"],
            "actor": copy.deepcopy(lock["actor"]),
            "acquireOutcome": lock["acquireOutcome"],
            "releaseOutcome": lock["releaseOutcome"],
        }

    def run(self, mode: str) -> dict[str, Any]:
        observed_at = self.clock().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        executor = self._verify_executor()
        actor = self._verify_actor()
        report: dict[str, Any] = {
            "schemaVersion": 1,
            "kind": "public-skills-ruleset-execution-evidence",
            "mode": mode,
            "observedAt": observed_at,
            "status": "PASS",
            "findings": [],
            "executor": executor,
            "actor": actor,
            "applyLock": None,
            "desiredState": None,
            "source": None,
            "target": None,
            "phase": None,
            "preReadback": None,
            "activationReadback": None,
            "plannedMutation": None,
            "mutation": {"attempted": False, "outcome": "not-attempted"},
            "postReadback": None,
        }
        if mode != "apply":
            return seal_report(self._reconcile(mode, report, None))

        lock = self._acquire_apply_lock(actor, observed_at)
        report["applyLock"] = self._public_apply_lock(lock)
        result = report
        body_error: BaseException | None = None
        release_error: ForgeError | None = None
        try:
            result = self._reconcile(mode, report, lock)
        except BaseException as exc:  # The finally path must release even on cancellation.
            body_error = exc
        finally:
            try:
                self._release_apply_lock(lock)
                lock["releaseOutcome"] = "released"
            except ForgeError as exc:
                lock["releaseOutcome"] = "failed-or-uncertain"
                release_error = exc
            result["applyLock"] = self._public_apply_lock(lock)

        if body_error is not None:
            if release_error is not None:
                raise ForgeError(
                    f"apply failed with {type(body_error).__name__}: {body_error}; "
                    f"apply lock release also failed: {release_error}"
                ) from body_error
            raise body_error
        if release_error is not None:
            result["status"] = "ERROR"
            result["findings"].append(f"apply lock release failed: {release_error}")
        return seal_report(result)


def seal_report(report: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(report)
    value.pop("evidenceDigest", None)
    value["evidenceDigest"] = canonical_digest(value)
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="apply the admitted canonical transition")
    modes.add_argument("--readback", action="store_true", help="emit exact live readback without a mutation plan")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    mode = "apply" if args.apply else "readback" if args.readback else "dry-run"
    try:
        token = keyring_token()
        api = GitHubAPI(token)
        executor = RulesetExecutor(api, Path(__file__).resolve().read_bytes())
        report = executor.run(mode)
    except (ContractError, ForgeError) as exc:
        report = seal_report({
            "schemaVersion": 1,
            "kind": "public-skills-ruleset-execution-evidence",
            "mode": mode,
            "observedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "status": "ERROR",
            "findings": [str(exc)],
            "mutation": {"attempted": False, "outcome": "not-attempted-or-not-provable"},
        })
    print(canonical_bytes(report).decode("utf-8"))
    return STATUS_EXIT.get(report.get("status"), 3)


if __name__ == "__main__":
    raise SystemExit(main())
