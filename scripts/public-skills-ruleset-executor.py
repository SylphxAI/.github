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
import time
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
GITHUB_ACTIONS_APP = {"id": 15368, "slug": "github-actions", "name": "GitHub Actions"}

APPLY_LOCK_TAG_NAME = "sylph-locks/public-skills-ruleset-executor"
APPLY_LOCK_REF = f"refs/tags/{APPLY_LOCK_TAG_NAME}"
APPLY_LOCK_REF_PATH = f"tags/{APPLY_LOCK_TAG_NAME}"

EXECUTOR_REPOSITORY_ID = 1091169653
EXECUTOR_REPOSITORY_NODE_ID = "R_kgDOQQntdQ"
EXECUTOR_REPOSITORY = "SylphxAI/.github"
EXECUTOR_BRANCH = "main"
EXECUTOR_PATH = "scripts/public-skills-ruleset-executor.py"
ATTESTATION_POLICY_PATH = "policies/public-skills-activation-attestation-ruleset.json"
ATTESTATION_RULESET_NAME = "immutable-public-skills-activation-attestations"
ATTESTATION_REF_PREFIX = "refs/tags/sylph-attestations/public-skills-ruleset/"
ATTESTATION_TAG_PREFIX = "sylph-attestations/public-skills-ruleset/"
ATTESTATION_RULESET_REF_PATTERN = f"{ATTESTATION_REF_PREFIX}*"
ATTESTATION_KIND = "public-skills-ruleset-activation-attestation"
APPLY_LOCK_TAGS_ENDPOINT = f"/repositories/{EXECUTOR_REPOSITORY_ID}/git/tags"
APPLY_LOCK_REFS_ENDPOINT = f"/repositories/{EXECUTOR_REPOSITORY_ID}/git/refs"
APPLY_LOCK_REF_GET_ENDPOINT = f"/repositories/{EXECUTOR_REPOSITORY_ID}/git/ref/{APPLY_LOCK_REF_PATH}"
APPLY_LOCK_REF_DELETE_ENDPOINT = f"/repositories/{EXECUTOR_REPOSITORY_ID}/git/refs/{APPLY_LOCK_REF_PATH}"

DOCTRINE_REPOSITORY_ID = 1265184361
DOCTRINE_REPOSITORY = "SylphxAI/doctrine"
DOCTRINE_BRANCH = "main"
DOCTRINE_RECORD_PATH = "control-plane/github-rulesets/public-skills-external-admission.json"
ACTIVATION_EVIDENCE_PATH = "control-plane/evidence/public-skills-ruleset-activation.json"
QUEUE_BARRIER_ACTIVATION_EVIDENCE_PATH = "control-plane/evidence/public-skills-queue-barrier-activation.json"
DOCTRINE_SCHEMA_REF = "../../schemas/organization-required-workflow-ruleset.schema.json"
DOCTRINE_RECORD_ID = "SylphxAI/doctrine:public-skills-external-admission"

TARGET_REPOSITORY_ID = 1297840366
TARGET_REPOSITORY_NODE_ID = "R_kgDOTVt47g"
TARGET_STAGING_NAME = "SylphxAI/skills-public-cleanroom"
TARGET_FINAL_NAME = "SylphxAI/skills"
TARGET_REPOSITORY_NAMES = (TARGET_STAGING_NAME, TARGET_FINAL_NAME)
TARGET_DEFAULT_BRANCH = "main"

RULESET_NAME = "public-skills-external-admission"
WORKFLOW_PATH = ".github/workflows/public-skills-admission.yml"
WORKFLOW_NAME = "public-skills-external-admission"
VALIDATOR_PATH = "scripts/public-skills-admission.mjs"
POLICY_PATH = "policies/public-skills-admission.json"
REQUIRED_CHECK = "public-skills-external-admission/pass"
BARRIER_WORKFLOW_PATH = ".github/workflows/public-skills-merge-queue-barrier.yml"
BARRIER_CONTROLLER_PATH = "scripts/public-skills-merge-queue-barrier.mjs"
BARRIER_POLICY_PATH = "policies/public-skills-merge-queue-barrier.json"
BARRIER_RULESET_NAME = "public-skills-merge-queue-barrier"
BARRIER_WORKFLOW_NAME = "public-skills-merge-queue-barrier"
BARRIER_REQUIRED_CHECK = "public-skills-merge-queue-barrier/pass"
PROTECTED_SOURCE_RELATION = "same-protected-source-commit"
PROTECTED_SOURCE_RUNTIME_REVISION_INPUT = "github.workflow_sha"
LOCAL_REQUIRED_CHECKS = ("risk-classification/pass", "trunk-admission/pass")
SOURCE_IDENTITIES = {
    WORKFLOW_PATH: {
        "gitBlobSha": "19b2a68bb01775bbb075535f8c1d367836e2a13e",
        "exactBytesDigest": "sha256:0c2eb925686b57ea3c3af99243d3c6b8bf267cd5eeb303d170df806f9d7d4d62",
    },
    VALIDATOR_PATH: {
        "gitBlobSha": "3d923a5a20b187173140c8bc067eb7f9afa4dfea",
        "exactBytesDigest": "sha256:85a6913a9d0144c81c69d992cb3f96528be4b14b38e626e1e557aed765ff5447",
    },
    POLICY_PATH: {
        "gitBlobSha": "55a328efe169b99ab69e11cf3ca1ad36b58eb87b",
        "exactBytesDigest": "sha256:4f7e22f770346e35d6986757b31544efe571b0a19179c898ce9a64ebe289cd45",
    },
}
V4_ADDITIONAL_SOURCE_IDENTITIES = {
    BARRIER_WORKFLOW_PATH: {
        "gitBlobSha": "b5f373a38994a3282e5b6aa7bec12b1305277670",
        "exactBytesDigest": "sha256:e1167eec8bafdb9a9817538d2c225f4781705dfd38d7b9c761411a3f012e6c01",
    },
    BARRIER_CONTROLLER_PATH: {
        "gitBlobSha": "9e163e9846e533dbcb0c7a41ea0d4c218d675798",
        "exactBytesDigest": "sha256:d5bb947a018d2e027832e49a9b939a6a168252954a74a4df794e4e01757841d1",
    },
    BARRIER_POLICY_PATH: {
        "gitBlobSha": "29044bd16e0888ecbadd64ec227407f463f72470",
        "exactBytesDigest": "sha256:95bb962112a654e9f9eb566d2ad2937700e5f520174c44fbf12c89367681aff1",
    },
}
LEGACY_SOURCE_COMMIT_SHA = "f29ea0026e7e018f1cd8777983e548b90c23b569"
LEGACY_SOURCE_IDENTITIES = {
    WORKFLOW_PATH: {
        "gitBlobSha": "7032d81fc0625360ef4650ed5f326efd6dc7ca3d",
        "exactBytesDigest": "sha256:d4551484eadcb9a0dedb88b64109d80e8f5fe867800bc969656fa016af5c4952",
    },
    VALIDATOR_PATH: {
        "gitBlobSha": "e60b17e3b5e521d9a68db7f2d197f0bd97687eb8",
        "exactBytesDigest": "sha256:9ddced29fe6b4e323113d3343dd4938cd56d5dbdabebb9b0427469aa5ca8b89c",
    },
    POLICY_PATH: {
        "gitBlobSha": "6f4d12f62f1c2130aefebdc5a6021d8c6f9c9e85",
        "exactBytesDigest": "sha256:0db4a6a94717f58f78bd628591db16b518155a3d7071cae8fa6295f8f844f743",
    },
}
SOURCE_PATHS = tuple(SOURCE_IDENTITIES)
V4_SOURCE_PATHS = (
    WORKFLOW_PATH,
    VALIDATOR_PATH,
    POLICY_PATH,
    BARRIER_WORKFLOW_PATH,
    BARRIER_CONTROLLER_PATH,
    BARRIER_POLICY_PATH,
    EXECUTOR_PATH,
)

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
    "active": {"active"},
    "recovery": {"evaluate", "disabled"},
}
QUEUE_BARRIER_PHASE_ENFORCEMENT = {
    "expand": {"evaluate"},
    "reconcile": {"evaluate"},
    "canary": {"evaluate"},
    "ratchet": {"active"},
    "active": {"active"},
    "recovery": {"evaluate", "disabled"},
}
V5_QUEUE_BARRIER_PHASE_ENFORCEMENT = {
    **QUEUE_BARRIER_PHASE_ENFORCEMENT,
    "post-activation": {"active"},
    "verified": {"active"},
}
QUEUE_BARRIER_EVIDENCE_KINDS = {
    "evaluateReadback": "queue-barrier-ruleset-readback",
    "pullRequestNoMutationCanary": "queue-barrier-pull-request-no-mutation-canary",
    "evaluateMergeGroupFailureCanary": "queue-barrier-evaluate-merge-group-failure-canary",
    "effectiveRulesReadback": "queue-barrier-effective-rules-readback",
    "activeProviderRemovalCanary": "queue-barrier-active-provider-removal-canary",
    "activePassThroughCanary": "queue-barrier-active-pass-through-canary",
    "activeExternalFailureCanary": "queue-barrier-active-external-failure-canary",
}
V5_PULL_REQUEST_BINDING_KEYS = (
    "pullRequestHeadRef",
    "pullRequestHeadSha",
    "pullRequestBaseRef",
    "pullRequestBaseSha",
)
V5_PULL_REQUEST_CANARY_HEAD_REF_RE = re.compile(
    r"^canary/public-skills/pre-launch/barrier-pull-request-[0-9a-f]{12}$"
)
QUEUE_BARRIER_PRE_ACTIVATION_EVIDENCE = (
    "evaluateReadback",
    "pullRequestNoMutationCanary",
    "evaluateMergeGroupFailureCanary",
)
QUEUE_BARRIER_ACTIVE_EVIDENCE = (
    *QUEUE_BARRIER_PRE_ACTIVATION_EVIDENCE,
    "activationTransition",
    "effectiveRulesReadback",
    "activeProviderRemovalCanary",
)
V5_EXTERNAL_DETAILS_FIELDS = (
    "evaluateMergeGroupFailureCanary",
    "activeProviderRemovalCanary",
    "activePassThroughCanary",
    "activeExternalFailureCanary",
)
ENFORCEMENT_RANK = {"disabled": 0, "evaluate": 1, "active": 2}
LEGACY_EVIDENCE_KINDS = {
    "evaluateReadback": "ruleset-readback",
    "pullRequestCanary": "workflow-run",
    "mergeGroupCanary": "workflow-run",
    "negativeControl": "negative-control-run",
    "effectiveRulesReadback": "effective-rules-readback",
}
EVIDENCE_KINDS = {
    "evaluateReadback": "ruleset-readback",
    "pullRequestCanary": "workflow-run",
    "mergeGroupCanary": "workflow-run",
    "negativeControl": "negative-control-run",
    "evaluateRuleSuiteReadback": "evaluate-rule-suite-readback",
}
STATUS_EXIT = {
    "PASS": 0,
    "DRIFT": 1,
    "BLOCKED": 2,
    "ERROR": 3,
    "APPLIED_PENDING_EVIDENCE": 4,
    "APPLIED_PENDING_ATTESTATION": 5,
}
ACTIVATION_REPORT_KIND = "public-skills-ruleset-activation-evidence"
EXECUTION_REPORT_KIND = "public-skills-ruleset-execution-evidence"
PENDING_EVIDENCE_FINDING = "activation applied; protected transition evidence is not yet committed"
PENDING_ATTESTATION_FINDING = "activation applied; immutable provider attestation is not yet confirmed"
TRANSITION_KIND = "ruleset-activation-transition"
AUDIT_ACTION = "repository_ruleset.update"
AUDIT_MAX_ATTEMPTS = 6
AUDIT_MAX_PAGES = 3
AUDIT_RETRY_SECONDS = (0, 1, 2, 4, 8, 15)
AUDIT_LOWER_SKEW_SECONDS = 60
AUDIT_UPPER_SKEW_SECONDS = 300
AUDIT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9:-]{8,128}$")
AUDIT_PROVIDER_KEYS = {
    "_document_id", "action", "actor", "actor_id", "created_at", "operation_type",
    "org", "org_id", "request_id", "ruleset_enforcement", "ruleset_id",
    "ruleset_name", "ruleset_source_type",
}


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


def _evidence_kinds(record: dict[str, Any]) -> dict[str, str]:
    """Select the immutable activation-evidence vocabulary for one record."""

    return EVIDENCE_KINDS if record.get("schemaVersion") == 3 else LEGACY_EVIDENCE_KINDS


def _authority_schema_version(record: dict[str, Any]) -> int:
    """Version every mutation-authority envelope with its desired-state contract."""

    return {4: 4, 3: 2}.get(record.get("schemaVersion"), 1)


def _validate_evidence(value: Any, field: str, record: dict[str, Any]) -> datetime | None:
    if value is None:
        return None
    label = f"activationEvidence.{field}"
    evidence = _object(value, label)
    _exact_keys(evidence, {"kind", "locator", "observedAt", "subjectDigest", "bindings"}, label, {"negativeControl"})
    evidence_kinds = _evidence_kinds(record)
    if evidence["kind"] != evidence_kinds[field]:
        raise ContractError(f"{label}.kind must be {evidence_kinds[field]}")
    _github_url(evidence["locator"], f"{label}.locator")
    observed = _timestamp(evidence["observedAt"], f"{label}.observedAt")
    _digest(evidence["subjectDigest"], f"{label}.subjectDigest")
    canary = field in {
        "pullRequestCanary",
        "mergeGroupCanary",
        "negativeControl",
        "evaluateRuleSuiteReadback",
    }
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


def _validate_actor(value: Any, label: str, *, include_type: bool) -> dict[str, Any]:
    actor = _object(value, label)
    keys = {"id", "login", "type"} if include_type else {"id", "login"}
    _exact_keys(actor, keys, label)
    if actor.get("id") != GITHUB_ACTOR_ID or actor.get("login") != GITHUB_ACTOR_LOGIN:
        raise ContractError(f"{label} differs from the admitted actor")
    if include_type and actor.get("type") != GITHUB_ACTOR_TYPE:
        raise ContractError(f"{label}.type differs from the admitted actor type")
    return actor


def _activation_evidence_digest(record: dict[str, Any]) -> str:
    evidence = record["activationEvidence"]
    return canonical_digest({field: evidence[field] for field in _evidence_kinds(record)})


def _validate_transition_readback(
    value: Any,
    label: str,
    *,
    ruleset_id: int,
    enforcement: str,
) -> dict[str, Any]:
    readback = _object(value, label)
    _exact_keys(
        readback,
        {"rulesetId", "enforcement", "updatedAt", "stateDigest", "effectiveRulesDigest"},
        label,
    )
    if _positive_integer(readback["rulesetId"], f"{label}.rulesetId") != ruleset_id:
        raise ContractError(f"{label}.rulesetId differs from desired state")
    if readback["enforcement"] != enforcement:
        raise ContractError(f"{label}.enforcement must be {enforcement}")
    _timestamp(readback["updatedAt"], f"{label}.updatedAt")
    _digest(readback["stateDigest"], f"{label}.stateDigest")
    _digest(readback["effectiveRulesDigest"], f"{label}.effectiveRulesDigest")
    return readback


def _validate_policy_identity(value: Any, label: str) -> dict[str, Any]:
    policy = _object(value, label)
    _exact_keys(
        policy,
        {"repositoryId", "commitSha", "path", "gitBlobSha", "exactBytesDigest", "semanticDigest"},
        label,
    )
    if policy["repositoryId"] != EXECUTOR_REPOSITORY_ID or policy["path"] != ATTESTATION_POLICY_PATH:
        raise ContractError(f"{label} source identity differs")
    for field in ["commitSha", "gitBlobSha"]:
        _sha(policy[field], f"{label}.{field}")
    for field in ["exactBytesDigest", "semanticDigest"]:
        _digest(policy[field], f"{label}.{field}")
    return policy


def _validate_attestation_projection(value: Any, label: str) -> dict[str, Any]:
    attestation = _object(value, label)
    _exact_keys(
        attestation,
        {
            "repositoryId", "ref", "tagObjectSha", "tagMessageDigest", "claimDigest",
            "evidenceCutoffAt", "policy", "ruleset",
        },
        label,
    )
    if attestation["repositoryId"] != EXECUTOR_REPOSITORY_ID:
        raise ContractError(f"{label}.repositoryId differs")
    ref = _string(attestation["ref"], f"{label}.ref")
    nonce = ref.removeprefix(ATTESTATION_REF_PREFIX)
    if not ref.startswith(ATTESTATION_REF_PREFIX) or NONCE_RE.fullmatch(nonce) is None:
        raise ContractError(f"{label}.ref is not the exact nonce-scoped attestation ref")
    _sha(attestation["tagObjectSha"], f"{label}.tagObjectSha")
    for field in ["tagMessageDigest", "claimDigest"]:
        _digest(attestation[field], f"{label}.{field}")
    _timestamp(attestation["evidenceCutoffAt"], f"{label}.evidenceCutoffAt")
    _validate_policy_identity(attestation["policy"], f"{label}.policy")
    ruleset = _object(attestation["ruleset"], f"{label}.ruleset")
    _exact_keys(ruleset, {"rulesetId", "stateDigest"}, f"{label}.ruleset")
    _positive_integer(ruleset["rulesetId"], f"{label}.ruleset.rulesetId")
    _digest(ruleset["stateDigest"], f"{label}.ruleset.stateDigest")
    return attestation


def _validate_activation_transition(value: Any, record: dict[str, Any]) -> dict[str, Any]:
    transition = _object(value, "activationEvidence.activationTransition")
    _exact_keys(
        transition,
        {"kind", "schemaVersion", "authorization", "pre", "mutation", "post", "audit", "executorReport", "capturedAt"},
        "activationEvidence.activationTransition",
    )
    expected_version = _authority_schema_version(record)
    if transition["kind"] != TRANSITION_KIND or transition["schemaVersion"] != expected_version:
        raise ContractError("activation transition identity is unsupported")
    captured_at = _timestamp(transition["capturedAt"], "activationTransition.capturedAt")
    ruleset_id = _positive_integer(record["ruleset"]["rulesetId"], "ruleset.rulesetId")

    authorization = _object(transition["authorization"], "activationTransition.authorization")
    _exact_keys(
        authorization,
        {"desiredState", "executor", "desiredPayloadDigest", "activationEvidenceDigest"},
        "activationTransition.authorization",
    )
    desired = _object(authorization["desiredState"], "activationTransition.authorization.desiredState")
    _exact_keys(desired, {"repositoryId", "commitSha", "path", "gitBlobSha", "exactBytesDigest", "semanticDigest"}, "activationTransition.authorization.desiredState")
    if desired["repositoryId"] != DOCTRINE_REPOSITORY_ID or desired["path"] != DOCTRINE_RECORD_PATH:
        raise ContractError("activation transition desired-state identity differs")
    _sha(desired["commitSha"], "activationTransition.authorization.desiredState.commitSha")
    _sha(desired["gitBlobSha"], "activationTransition.authorization.desiredState.gitBlobSha")
    for field in ["exactBytesDigest", "semanticDigest"]:
        _digest(desired[field], f"activationTransition.authorization.desiredState.{field}")
    executor = _object(authorization["executor"], "activationTransition.authorization.executor")
    _exact_keys(executor, {"repositoryId", "commitSha", "path", "exactBytesDigest"}, "activationTransition.authorization.executor")
    if executor["repositoryId"] != EXECUTOR_REPOSITORY_ID or executor["path"] != EXECUTOR_PATH:
        raise ContractError("activation transition executor identity differs")
    _sha(executor["commitSha"], "activationTransition.authorization.executor.commitSha")
    _digest(executor["exactBytesDigest"], "activationTransition.authorization.executor.exactBytesDigest")
    _digest(authorization["desiredPayloadDigest"], "activationTransition.authorization.desiredPayloadDigest")
    _digest(authorization["activationEvidenceDigest"], "activationTransition.authorization.activationEvidenceDigest")
    if authorization["desiredPayloadDigest"] != canonical_digest(expected_ruleset(record, enforcement="active")):
        raise ContractError("activation transition desired-payload digest differs")
    if authorization["activationEvidenceDigest"] != _activation_evidence_digest(record):
        raise ContractError("activation transition canary-evidence digest differs")

    pre = _validate_transition_readback(transition["pre"], "activationTransition.pre", ruleset_id=ruleset_id, enforcement="evaluate")
    post = _validate_transition_readback(transition["post"], "activationTransition.post", ruleset_id=ruleset_id, enforcement="active")
    if pre["stateDigest"] != canonical_digest(expected_ruleset(record, enforcement="evaluate")):
        raise ContractError("activation transition pre-state digest differs")
    if post["stateDigest"] != authorization["desiredPayloadDigest"]:
        raise ContractError("activation transition post-state digest differs")
    if record["schemaVersion"] < 3 and pre["effectiveRulesDigest"] != post["effectiveRulesDigest"]:
        raise ContractError("activation transition changed immutable effective-rules coverage")

    mutation = _object(transition["mutation"], "activationTransition.mutation")
    _exact_keys(
        mutation,
        {"action", "outcome", "actor", "requestId", "applyLock", "activationAttestation"},
        "activationTransition.mutation",
    )
    if mutation["action"] != "update" or mutation["outcome"] != "updated":
        raise ContractError("activation transition mutation is not the one admitted update")
    actor = _validate_actor(mutation["actor"], "activationTransition.mutation.actor", include_type=True)
    request_id = _string(mutation["requestId"], "activationTransition.mutation.requestId")
    if AUDIT_REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ContractError("activation transition requestId is not a provider request ID")
    lock = _object(mutation["applyLock"], "activationTransition.mutation.applyLock")
    _exact_keys(
        lock,
        {"repositoryId", "ref", "tagObjectSha", "tagMessageDigest", "executorCommitSha", "nonce", "claimedAt", "actor", "acquireOutcome", "releaseOutcome", "finalRefAbsent"},
        "activationTransition.mutation.applyLock",
    )
    if lock["repositoryId"] != EXECUTOR_REPOSITORY_ID or lock["ref"] != APPLY_LOCK_REF:
        raise ContractError("activation transition apply lock identity differs")
    _sha(lock["tagObjectSha"], "activationTransition.mutation.applyLock.tagObjectSha")
    _digest(lock["tagMessageDigest"], "activationTransition.mutation.applyLock.tagMessageDigest")
    if _sha(lock["executorCommitSha"], "activationTransition.mutation.applyLock.executorCommitSha") != executor["commitSha"]:
        raise ContractError("activation transition lock/executor commits differ")
    if not isinstance(lock["nonce"], str) or NONCE_RE.fullmatch(lock["nonce"]) is None:
        raise ContractError("activation transition apply-lock nonce is invalid")
    _timestamp(lock["claimedAt"], "activationTransition.mutation.applyLock.claimedAt")
    if _validate_actor(lock["actor"], "activationTransition.mutation.applyLock.actor", include_type=True) != actor:
        raise ContractError("activation transition lock/mutation actors differ")
    if lock["acquireOutcome"] != "acquired" or lock["releaseOutcome"] != "released" or lock["finalRefAbsent"] is not True:
        raise ContractError("activation transition lacks a complete lock lifecycle")
    attestation = _validate_attestation_projection(
        mutation["activationAttestation"],
        "activationTransition.mutation.activationAttestation",
    )
    if attestation["ref"] != f"{ATTESTATION_REF_PREFIX}{lock['nonce']}":
        raise ContractError("activation transition attestation/lock nonce differs")
    if attestation["policy"]["commitSha"] != executor["commitSha"]:
        raise ContractError("activation transition attestation policy/executor commits differ")
    if _timestamp(
        attestation["evidenceCutoffAt"],
        "activationTransition.mutation.activationAttestation.evidenceCutoffAt",
    ) > captured_at:
        raise ContractError("activation transition evidence cutoff postdates capture")

    audit = _object(transition["audit"], "activationTransition.audit")
    _exact_keys(
        audit,
        {"documentId", "action", "actor", "organization", "createdAtEpochMs", "operationType", "requestId", "ruleset", "providerProjectionDigest", "normalizedDigest"},
        "activationTransition.audit",
    )
    _string(audit["documentId"], "activationTransition.audit.documentId")
    if audit["action"] != AUDIT_ACTION:
        raise ContractError("activation transition audit action differs")
    audit_actor = _validate_actor(audit["actor"], "activationTransition.audit.actor", include_type=False)
    organization = _object(audit["organization"], "activationTransition.audit.organization")
    _exact_keys(organization, {"id", "login"}, "activationTransition.audit.organization")
    if organization != {"id": ORGANIZATION_ID, "login": ORGANIZATION}:
        raise ContractError("activation transition audit organization differs")
    created_ms = _positive_integer(audit["createdAtEpochMs"], "activationTransition.audit.createdAtEpochMs")
    _string(audit["operationType"], "activationTransition.audit.operationType")
    if audit["requestId"] != request_id or audit_actor != {"id": actor["id"], "login": actor["login"]}:
        raise ContractError("activation transition audit request/actor differs")
    audit_ruleset = _object(audit["ruleset"], "activationTransition.audit.ruleset")
    _exact_keys(audit_ruleset, {"id", "name", "sourceType", "enforcement"}, "activationTransition.audit.ruleset")
    if audit_ruleset != {"id": ruleset_id, "name": RULESET_NAME, "sourceType": "Organization", "enforcement": "active"}:
        raise ContractError("activation transition audit ruleset differs")
    for field in ["providerProjectionDigest", "normalizedDigest"]:
        _digest(audit[field], f"activationTransition.audit.{field}")
    if datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) > captured_at:
        raise ContractError("activation transition audit event postdates capture")

    report = _object(transition["executorReport"], "activationTransition.executorReport")
    _exact_keys(report, {"path", "gitBlobSha", "exactBytesDigest", "bodyDigest", "evidenceDigest"}, "activationTransition.executorReport")
    if report["path"] != ACTIVATION_EVIDENCE_PATH:
        raise ContractError("activation transition executor-report path differs")
    _sha(report["gitBlobSha"], "activationTransition.executorReport.gitBlobSha")
    for field in ["exactBytesDigest", "bodyDigest", "evidenceDigest"]:
        _digest(report[field], f"activationTransition.executorReport.{field}")
    return transition


def validate_record(record: Any) -> dict[str, Any]:
    root = _object(record, "desired state")
    _exact_keys(
        root,
        {"$schema", "schemaVersion", "kind", "id", "owner", "owningDecision", "organization", "ruleset", "workflowSource", "migration", "activationEvidence", "recovery"},
        "desired state",
    )
    fixed = {
        "$schema": DOCTRINE_SCHEMA_REF,
        "kind": "organization-required-workflow-ruleset",
        "id": DOCTRINE_RECORD_ID,
        "owner": DOCTRINE_REPOSITORY,
        "owningDecision": "public-skills-external-required-workflow",
        "organization": ORGANIZATION,
    }
    for field, expected in fixed.items():
        if root[field] != expected:
            raise ContractError(f"desired state {field} differs from the immutable executor contract")
    if root["schemaVersion"] not in {2, 3}:
        raise ContractError("desired state schemaVersion differs from the immutable executor contract")

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

    evidence_kinds = _evidence_kinds(root)
    evidence = _object(root["activationEvidence"], "activationEvidence")
    _exact_keys(evidence, {*evidence_kinds, "activationTransition"}, "activationEvidence")
    observed: dict[str, datetime] = {}
    for field in evidence_kinds:
        value = _validate_evidence(evidence[field], field, root)
        if value is not None:
            observed[field] = value

    if source["commitSha"] is None:
        if phase != "expand" or ruleset["rulesetId"] is not None or ruleset["enforcement"] != "evaluate":
            raise ContractError("unresolved source SHA is allowed only in unbound expand/evaluate")
    if phase in {"reconcile", "ratchet", "active", "recovery"} and ruleset["rulesetId"] is None:
        raise ContractError(f"phase {phase} requires an exact ruleset ID")
    if phase in {"ratchet", "active"}:
        missing = [field for field in evidence_kinds if evidence[field] is None]
        if missing:
            raise ContractError(f"{phase} lacks activation evidence {missing}")
        if "localRequiredChecks" not in source or "negativeControlPolicy" not in source:
            raise ContractError(f"{phase} requires the complete negative-control policy")
        coverage_field = (
            "evaluateRuleSuiteReadback"
            if root["schemaVersion"] == 3
            else "effectiveRulesReadback"
        )
        if root["schemaVersion"] == 3:
            coverage_bindings = evidence[coverage_field]["bindings"]
            canary_bindings = [
                evidence[field]["bindings"]
                for field in ["pullRequestCanary", "mergeGroupCanary", "negativeControl"]
            ]
            if coverage_bindings["headSha"] in {
                item["headSha"] for item in canary_bindings
            }:
                raise ContractError(
                    "evaluate rule-suite readback must bind a dedicated synthetic head"
                )
            if coverage_bindings["ruleSuiteId"] in {
                item["ruleSuiteId"] for item in canary_bindings
            }:
                raise ContractError(
                    "evaluate rule-suite readback must bind a dedicated rule suite"
                )
        locators = [evidence[field]["locator"] for field in evidence_kinds]
        if len(locators) != len(set(locators)):
            raise ContractError("ratchet activation evidence locators must be distinct")
        if not observed["evaluateReadback"] <= observed["pullRequestCanary"] <= observed[coverage_field]:
            raise ContractError("pull-request canary chronology is invalid")
        if not observed["evaluateReadback"] <= observed["mergeGroupCanary"] <= observed[coverage_field]:
            raise ContractError("merge-group canary chronology is invalid")
        if not observed["evaluateReadback"] <= observed["negativeControl"] <= observed[coverage_field]:
            raise ContractError("negative-control chronology is invalid")
    transition = evidence["activationTransition"]
    if phase == "active":
        _validate_activation_transition(transition, root)
    elif transition is not None:
        raise ContractError("activation transition is allowed only in active phase")
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


def validate_v4_protected_source_envelope(
    record: Any,
    runtime_workflow_sha: str,
    *,
    schema_version: int = 4,
) -> dict[str, Any]:
    """Bind an additive v4/v5 shared source envelope to one runtime revision."""

    if schema_version not in {4, 5}:
        raise ContractError("protected-source envelope version is unsupported")
    root = _object(record, f"v{schema_version} desired state")
    if root.get("schemaVersion") != schema_version:
        raise ContractError(
            f"v{schema_version} protected-source envelope requires schemaVersion {schema_version}"
        )
    for field, expected in {
        "kind": "organization-required-workflow-ruleset",
        "id": DOCTRINE_RECORD_ID,
        "owner": DOCTRINE_REPOSITORY,
        "organization": ORGANIZATION,
    }.items():
        if root.get(field) != expected:
            raise ContractError(f"v4 desired state {field} differs")
    runtime_sha = _sha(runtime_workflow_sha, "runtime github.workflow_sha")
    sequencing = _object(root.get("activationSequencing"), "activationSequencing")
    bundle = _object(
        sequencing.get("protectedSourceBundle"),
        "activationSequencing.protectedSourceBundle",
    )
    _exact_keys(
        bundle,
        {
            "relation",
            "repositoryId",
            "repository",
            "ref",
            "commitSha",
            "runtimeRevisionInput",
            "members",
        },
        "activationSequencing.protectedSourceBundle",
    )
    expected_members = [
        {"role": "external-admission-workflow", "path": WORKFLOW_PATH},
        {"role": "merge-queue-barrier-workflow", "path": BARRIER_WORKFLOW_PATH},
        {"role": "organization-ruleset-executor", "path": EXECUTOR_PATH},
    ]
    barrier_source = _object(
        _object(root.get("queueBarrier"), "queueBarrier").get("workflowSource"),
        "queueBarrier.workflowSource",
    )
    resolved_sha = barrier_source.get("commitSha")
    if resolved_sha is not None:
        resolved_sha = _sha(resolved_sha, "queueBarrier.workflowSource.commitSha")
    expected_bundle = {
        "relation": PROTECTED_SOURCE_RELATION,
        "repositoryId": EXECUTOR_REPOSITORY_ID,
        "repository": EXECUTOR_REPOSITORY,
        "ref": EXECUTOR_BRANCH,
        "commitSha": resolved_sha,
        "runtimeRevisionInput": PROTECTED_SOURCE_RUNTIME_REVISION_INPUT,
        "members": expected_members,
    }
    if bundle != expected_bundle:
        raise ContractError(
            "v4 protected source bundle does not bind all members to runtime github.workflow_sha"
        )

    external_source = _object(root.get("workflowSource"), "workflowSource")
    barrier = _object(root.get("queueBarrier"), "queueBarrier")
    barrier_source = _object(barrier.get("workflowSource"), "queueBarrier.workflowSource")
    executor = _object(sequencing.get("executor"), "activationSequencing.executor")
    _exact_keys(
        executor,
        {"repositoryId", "repository", "path", "ref", "commitSha", "exactBytesDigest"},
        "activationSequencing.executor",
    )
    if (
        executor["repositoryId"] != EXECUTOR_REPOSITORY_ID
        or executor["repository"] != EXECUTOR_REPOSITORY
        or executor["path"] != EXECUTOR_PATH
        or executor["ref"] != EXECUTOR_BRANCH
    ):
        raise ContractError("activationSequencing.executor identity differs")
    if resolved_sha is None:
        if bundle != expected_bundle:
            raise ContractError("unresolved v4 protected source bundle identity differs")
        if executor.get("commitSha") is not None or executor.get("exactBytesDigest") is not None:
            raise ContractError("unresolved v4 source cannot bind a partial executor identity")
        return copy.deepcopy(expected_bundle)
    if resolved_sha != runtime_sha:
        raise ContractError("resolved v4 source commit differs from runtime github.workflow_sha")
    for label, value, expected_path in [
        ("workflowSource", external_source, WORKFLOW_PATH),
        ("queueBarrier.workflowSource", barrier_source, BARRIER_WORKFLOW_PATH),
        ("activationSequencing.executor", executor, EXECUTOR_PATH),
    ]:
        if (
            value.get("repositoryId") != EXECUTOR_REPOSITORY_ID
            or value.get("repository") != EXECUTOR_REPOSITORY
            or value.get("ref") != EXECUTOR_BRANCH
            or value.get("commitSha") != runtime_sha
        ):
            raise ContractError(f"{label} does not share runtime github.workflow_sha")
        path = value.get("workflowPath") if label != "activationSequencing.executor" else value.get("path")
        if path != expected_path:
            raise ContractError(f"{label} path differs from the protected source bundle")
    return copy.deepcopy(expected_bundle)


def _v4_external_projection(record: dict[str, Any]) -> dict[str, Any]:
    projected = {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key not in {"queueBarrier", "activationSequencing"}
    }
    projected["schemaVersion"] = 3
    return projected


def _v4_nested_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for item in value.values() for key in _v4_nested_keys(item)
        }
    if isinstance(value, list):
        return {key for item in value for key in _v4_nested_keys(item)}
    return set()


def _v4_queue_canary_subject(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(item[key])
        for key in [
            "kind",
            "locator",
            "observedAt",
            "bindings",
            "providerVerdicts",
            "report",
            "queueOutcome",
            "failureProof",
        ]
    }


def _v4_terminal_subject(provider: dict[str, Any], terminal: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": provider["id"],
        "repositoryId": provider["repositoryId"],
        "beforeSha": provider["beforeSha"],
        "afterSha": provider["afterSha"],
        "ref": provider["ref"],
        "result": terminal["result"],
        "observedAt": terminal["observedAt"],
    }


def _validate_v4_queue_evidence(
    record: dict[str, Any],
    field: str,
    value: Any,
) -> datetime | None:
    if value is None:
        return None
    label = f"queueBarrier.activationEvidence.{field}"
    item = _object(value, label)
    _exact_keys(
        item,
        {
            "kind", "locator", "observedAt", "subjectDigest", "bindings",
            "providerVerdicts", "report", "queueOutcome", "failureProof",
        },
        label,
    )
    if item["kind"] != QUEUE_BARRIER_EVIDENCE_KINDS[field]:
        raise ContractError(f"{label}.kind differs")
    _github_url(item["locator"], f"{label}.locator")
    observed = _timestamp(item["observedAt"], f"{label}.observedAt")
    _digest(item["subjectDigest"], f"{label}.subjectDigest")
    barrier = record["queueBarrier"]
    bindings = _object(item["bindings"], f"{label}.bindings")
    _exact_keys(
        bindings,
        {
            "barrierRulesetId", "guardedRulesetId", "targetRepositoryId",
            "sourceRepositoryId", "sourceCommitSha", "headSha", "ruleSuiteId",
            "runId", "checkRunId", "pullRequestNumber",
        },
        f"{label}.bindings",
    )
    expected_bindings = {
        "barrierRulesetId": barrier["ruleset"]["rulesetId"],
        "guardedRulesetId": record["ruleset"]["rulesetId"],
        "targetRepositoryId": TARGET_REPOSITORY_ID,
        "sourceRepositoryId": EXECUTOR_REPOSITORY_ID,
        "sourceCommitSha": barrier["workflowSource"]["commitSha"],
    }
    if any(bindings[key] != expected for key, expected in expected_bindings.items()):
        raise ContractError(f"{label}.bindings do not bind desired state")

    pure_readback = field in {"evaluateReadback", "effectiveRulesReadback"}
    canary_identity = ["headSha", "runId", "checkRunId", "pullRequestNumber"]
    if pure_readback:
        if any(bindings[key] is not None for key in [*canary_identity, "ruleSuiteId"]):
            raise ContractError(f"{label} borrows canary identity")
        if any(item[key] is not None for key in ["providerVerdicts", "report", "queueOutcome", "failureProof"]):
            raise ContractError(f"{label} must be a pure ruleset readback")
        return observed
    for key in canary_identity:
        _positive_integer(bindings[key], f"{label}.bindings.{key}") if key != "headSha" else _sha(bindings[key], f"{label}.bindings.headSha")
    run_match = re.fullmatch(
        r"https://github\.com/[^/]+/[^/]+/actions/runs/([1-9][0-9]*)/?",
        item["locator"],
    )
    if run_match is None or int(run_match.group(1)) != bindings["runId"]:
        raise ContractError(f"{label}.locator does not bind runId")

    report = _object(item["report"], f"{label}.report")
    _exact_keys(
        report,
        {
            "event", "decision", "conclusion", "mutationAuthority",
            "mutationCount", "queueMutation", "permissions", "runAttempt",
            "candidateSha", "artifactDigest",
        },
        f"{label}.report",
    )
    if (
        report["mutationAuthority"] != "none"
        or report["mutationCount"] != 0
        or report["queueMutation"] != {"owner": "github-provider", "attempted": False}
        or report["permissions"] != {
            "actions": "read",
            "checks": "read",
            "contents": "read",
            "pullRequests": "read",
        }
        or report["candidateSha"] != bindings["headSha"]
    ):
        raise ContractError(f"{label}.report claims forbidden mutation authority or foreign identity")
    _positive_integer(report["runAttempt"], f"{label}.report.runAttempt")
    _digest(report["artifactDigest"], f"{label}.report.artifactDigest")

    if field == "pullRequestNoMutationCanary":
        if (
            bindings["ruleSuiteId"] is not None
            or item["providerVerdicts"] is not None
            or item["queueOutcome"] is not None
            or item["failureProof"] is not None
            or (report["event"], report["decision"], report["conclusion"])
            != ("pull_request", "pass-pull-request-identity", "success")
        ):
            raise ContractError("queue-barrier pull-request canary borrowed merge-group authority")
        if item["subjectDigest"] != canonical_digest(_v4_queue_canary_subject(item)):
            raise ContractError("queue-barrier pull-request canary subject digest differs")
        return observed

    _positive_integer(bindings["ruleSuiteId"], f"{label}.bindings.ruleSuiteId")
    provider = _object(item["providerVerdicts"], f"{label}.providerVerdicts")
    _exact_keys(
        provider,
        {
            "id", "repositoryId", "beforeSha", "afterSha", "ref", "pushedAt",
            "barrierEnforcement", "aggregateResult", "externalRuleEvaluation",
            "terminalAggregate",
        },
        f"{label}.providerVerdicts",
    )
    if (
        provider["id"] != bindings["ruleSuiteId"]
        or provider["repositoryId"] != TARGET_REPOSITORY_ID
        or provider["afterSha"] != bindings["headSha"]
        or provider["ref"] != "refs/heads/main"
        or provider["pushedAt"] != item["observedAt"]
        or provider["barrierEnforcement"] not in {"evaluate", "active"}
        or provider["aggregateResult"] not in {None, "pass", "fail"}
    ):
        raise ContractError(f"{label}.providerVerdicts does not bind the exact candidate suite")
    if provider["beforeSha"] is not None:
        _sha(provider["beforeSha"], f"{label}.providerVerdicts.beforeSha")
    _timestamp(provider["pushedAt"], f"{label}.providerVerdicts.pushedAt")
    external = _object(
        provider["externalRuleEvaluation"],
        f"{label}.providerVerdicts.externalRuleEvaluation",
    )
    _exact_keys(
        external,
        {"ruleSource", "ruleType", "enforcement", "result"},
        f"{label}.providerVerdicts.externalRuleEvaluation",
    )
    source = _object(external["ruleSource"], f"{label}.providerVerdicts.externalRuleEvaluation.ruleSource")
    if source != {
        "id": record["ruleset"]["rulesetId"],
        "name": RULESET_NAME,
        "type": "ruleset",
    } or external["ruleType"] != "workflows":
        raise ContractError(f"{label}.external rule identity differs")
    if external["enforcement"] not in {"evaluate", "active"} or external["result"] not in {"pass", "fail"}:
        raise ContractError(f"{label}.external rule state is unsupported")
    terminal = provider["terminalAggregate"]
    if terminal is not None:
        terminal = _object(terminal, f"{label}.providerVerdicts.terminalAggregate")
        _exact_keys(terminal, {"result", "observedAt", "subjectDigest"}, f"{label}.providerVerdicts.terminalAggregate")
        if terminal["result"] not in {"pass", "fail"}:
            raise ContractError(f"{label}.terminal result is unsupported")
        terminal_at = _timestamp(terminal["observedAt"], f"{label}.terminalAggregate.observedAt")
        _digest(terminal["subjectDigest"], f"{label}.terminalAggregate.subjectDigest")
        if terminal_at < observed or terminal["subjectDigest"] != canonical_digest(_v4_terminal_subject(provider, terminal)):
            raise ContractError(f"{label}.terminal aggregate identity or chronology differs")

    outcome = _object(item["queueOutcome"], f"{label}.queueOutcome")
    outcome_keys = {
        "owner", "cause", "outcome", "targetVisibility", "preQueueEntryId",
        "postQueueEntry", "pullRequestMerged", "pullRequestHeadSha",
        "pullRequestHeadShaAfter", "pullRequestHeadTree", "pullRequestBaseSha",
        "pullRequestBaseTree", "candidateSha", "candidateTree",
        "defaultBranchBeforeSha", "defaultBranchAfterSha",
        "defaultBranchBeforeTree", "defaultBranchAfterTree", "observedAt",
    }
    _exact_keys(outcome, outcome_keys, f"{label}.queueOutcome")
    if (
        outcome["owner"] != "github-provider"
        or outcome["targetVisibility"] != "private"
        or outcome["postQueueEntry"] is not None
        or outcome["candidateSha"] != bindings["headSha"]
        or outcome["pullRequestHeadShaAfter"] != outcome["pullRequestHeadSha"]
        or outcome["pullRequestBaseSha"] != outcome["defaultBranchBeforeSha"]
        or outcome["defaultBranchAfterTree"] != outcome["defaultBranchBeforeTree"]
    ):
        raise ContractError(f"{label}.queueOutcome does not prove provider ownership")
    _string(outcome["preQueueEntryId"], f"{label}.queueOutcome.preQueueEntryId")
    for key in outcome_keys - {
        "owner", "cause", "outcome", "targetVisibility", "preQueueEntryId",
        "postQueueEntry", "pullRequestMerged", "observedAt",
    }:
        _sha(outcome[key], f"{label}.queueOutcome.{key}")
    _timestamp(outcome["observedAt"], f"{label}.queueOutcome.observedAt")
    failure = item["failureProof"]
    if failure is not None:
        failure = _object(failure, f"{label}.failureProof")
        _exact_keys(failure, {"barrierCheck", "externalCheck", "failingChecks", "otherRequiredChecksAllPass"}, f"{label}.failureProof")
        if failure["otherRequiredChecksAllPass"] is not True:
            raise ContractError(f"{label}.failureProof does not isolate required checks")
    if provider["beforeSha"] != outcome["pullRequestBaseSha"]:
        raise ContractError(f"{label}.provider before SHA differs from the pull-request base")

    expected = {
        "evaluateMergeGroupFailureCanary": {
            "report": ("merge_group", "reject-merge-group", "failure"),
            "barrierEnforcement": "evaluate",
            "external": ("evaluate", "pass"),
            "outcome": ("evaluate-mode-observation", "merged", True),
            "terminal": "fail",
            "failure": {
                "barrierCheck": "failure",
                "externalCheck": "success",
                "failingChecks": [BARRIER_REQUIRED_CHECK],
                "otherRequiredChecksAllPass": True,
            },
            "sameTree": True,
        },
        "activeProviderRemovalCanary": {
            "report": ("merge_group", "reject-merge-group", "failure"),
            "barrierEnforcement": "active",
            "external": ("evaluate", "pass"),
            "outcome": ("required-check-failure", "provider-removed", False),
            "terminal": "fail",
            "failure": {
                "barrierCheck": "failure",
                "externalCheck": "success",
                "failingChecks": [BARRIER_REQUIRED_CHECK],
                "otherRequiredChecksAllPass": True,
            },
            "sameTree": True,
        },
        "activePassThroughCanary": {
            "report": ("merge_group", "pass-active-admission", "success"),
            "barrierEnforcement": "active",
            "external": ("active", "pass"),
            "outcome": ("required-check-success", "merged", True),
            "terminal": "pass",
            "failure": None,
            "sameTree": True,
        },
        "activeExternalFailureCanary": {
            "report": ("merge_group", "reject-merge-group", "failure"),
            "barrierEnforcement": "active",
            "external": ("active", "fail"),
            "outcome": ("required-check-failure", "provider-removed", False),
            "terminal": "fail",
            "failure": {
                "barrierCheck": "failure",
                "externalCheck": "failure",
                "failingChecks": [BARRIER_REQUIRED_CHECK, REQUIRED_CHECK],
                "otherRequiredChecksAllPass": True,
            },
            "sameTree": False,
        },
    }[field]
    if (
        (report["event"], report["decision"], report["conclusion"]) != expected["report"]
        or provider["barrierEnforcement"] != expected["barrierEnforcement"]
        or (external["enforcement"], external["result"]) != expected["external"]
        or (outcome["cause"], outcome["outcome"], outcome["pullRequestMerged"]) != expected["outcome"]
        or failure != expected["failure"]
    ):
        raise ContractError(f"{label} provider outcome matrix differs")
    if terminal is not None and terminal["result"] != expected["terminal"]:
        raise ContractError(f"{label} terminal aggregate contradicts provider outcome")
    if expected["sameTree"]:
        trees = {
            outcome["pullRequestHeadTree"],
            outcome["pullRequestBaseTree"],
            outcome["candidateTree"],
            outcome["defaultBranchBeforeTree"],
            outcome["defaultBranchAfterTree"],
        }
        if len(trees) != 1:
            raise ContractError(f"{label} is not an exact same-tree canary")
    if outcome["outcome"] == "merged":
        if (
            outcome["defaultBranchAfterSha"] != outcome["candidateSha"]
            or outcome["pullRequestMerged"] is not True
        ):
            raise ContractError(f"{label} lacks exact provider merge readback")
    elif (
        outcome["defaultBranchAfterSha"] != outcome["defaultBranchBeforeSha"]
        or outcome["pullRequestMerged"] is not False
    ):
        raise ContractError(f"{label} lacks exact provider removal readback")
    if item["subjectDigest"] != canonical_digest(_v4_queue_canary_subject(item)):
        raise ContractError(f"{label}.subjectDigest differs")
    return observed


def _v5_queue_evidence_as_v4(item: dict[str, Any]) -> dict[str, Any]:
    """Project v5-only sealed detail away for reuse of immutable v4 invariants."""

    legacy = copy.deepcopy(item)
    if isinstance(legacy.get("bindings"), dict):
        legacy["bindings"] = {
            key: copy.deepcopy(legacy["bindings"][key])
            for key in [
                "barrierRulesetId",
                "guardedRulesetId",
                "targetRepositoryId",
                "sourceRepositoryId",
                "sourceCommitSha",
                "headSha",
                "ruleSuiteId",
                "runId",
                "checkRunId",
                "pullRequestNumber",
            ]
        }
    if isinstance(legacy.get("report"), dict):
        legacy["report"] = {
            key: copy.deepcopy(legacy["report"][key])
            for key in [
                "event",
                "decision",
                "conclusion",
                "mutationAuthority",
                "mutationCount",
                "queueMutation",
                "permissions",
                "runAttempt",
                "candidateSha",
                "artifactDigest",
            ]
        }
    if isinstance(legacy.get("queueOutcome"), dict):
        legacy["queueOutcome"] = {
            key: copy.deepcopy(legacy["queueOutcome"][key])
            for key in [
                "owner",
                "cause",
                "outcome",
                "targetVisibility",
                "preQueueEntryId",
                "postQueueEntry",
                "pullRequestMerged",
                "pullRequestHeadSha",
                "pullRequestHeadShaAfter",
                "pullRequestHeadTree",
                "pullRequestBaseSha",
                "pullRequestBaseTree",
                "candidateSha",
                "candidateTree",
                "defaultBranchBeforeSha",
                "defaultBranchAfterSha",
                "defaultBranchBeforeTree",
                "defaultBranchAfterTree",
                "observedAt",
            ]
        }
    legacy["subjectDigest"] = canonical_digest(_v4_queue_canary_subject(legacy))
    return legacy


def _validate_v5_ruleset_revision(
    value: Any,
    *,
    label: str,
    ruleset_id: int,
    enforcement: str,
    subject_digest: str,
) -> tuple[datetime, datetime]:
    revision = _object(value, label)
    _exact_keys(
        revision,
        {
            "rulesetId",
            "enforcement",
            "updatedAt",
            "subjectDigest",
            "confirmedUnchangedAt",
        },
        label,
    )
    if (
        revision["rulesetId"] != ruleset_id
        or revision["enforcement"] != enforcement
        or revision["subjectDigest"] != subject_digest
    ):
        raise ContractError(f"{label} does not bind the exact desired revision")
    updated = _timestamp(revision["updatedAt"], f"{label}.updatedAt")
    confirmed = _timestamp(
        revision["confirmedUnchangedAt"], f"{label}.confirmedUnchangedAt"
    )
    if confirmed <= updated:
        raise ContractError(f"{label} unchanged confirmation does not postdate revision")
    return updated, confirmed


def _v5_details_url_repository_name(
    value: Any,
    *,
    run_id: int,
    job_id: int,
    label: str,
) -> str:
    """Return the exact closed-lifecycle repository name in one job URL."""

    for name in TARGET_REPOSITORY_NAMES:
        if value == f"https://github.com/{name}/actions/runs/{run_id}/job/{job_id}":
            return name
    raise ContractError(f"{label} differs from the exact admitted target run/job URL")


def _validate_v5_queue_evidence(
    record: dict[str, Any],
    field: str,
    value: Any,
) -> datetime | None:
    """Validate additive v5 revision, PR, external-run, and queue-entry proofs."""

    if value is None:
        return None
    if field in {"evaluateReadback", "effectiveRulesReadback"}:
        return _validate_v4_queue_evidence(record, field, value)
    item = _object(value, f"queueBarrier.activationEvidence.{field}")
    observed = _validate_v4_queue_evidence(
        record,
        field,
        _v5_queue_evidence_as_v4(item),
    )
    assert observed is not None
    report = _object(item["report"], f"queueBarrier.activationEvidence.{field}.report")
    _exact_keys(
        report,
        {
            "event",
            "decision",
            "conclusion",
            "mutationAuthority",
            "mutationCount",
            "queueMutation",
            "permissions",
            "runAttempt",
            "runCreatedAt",
            "runUpdatedAt",
            "candidateSha",
            "rulesetRevision",
            "externalRulesetRevision",
            "pullRequest",
            "externalAdmission",
            "artifactDigest",
        },
        f"queueBarrier.activationEvidence.{field}.report",
    )
    barrier_enforcement = (
        "evaluate"
        if field in {
            "pullRequestNoMutationCanary",
            "evaluateMergeGroupFailureCanary",
        }
        else "active"
    )
    external_enforcement = (
        "active"
        if field in {"activePassThroughCanary", "activeExternalFailureCanary"}
        else "evaluate"
    )
    barrier_payload = expected_v4_ruleset(
        record, "queueBarrier", enforcement=barrier_enforcement
    )
    external_payload = expected_v4_ruleset(
        record, "externalAdmission", enforcement=external_enforcement
    )
    barrier_updated, barrier_confirmed = _validate_v5_ruleset_revision(
        report["rulesetRevision"],
        label=f"queueBarrier.activationEvidence.{field}.report.rulesetRevision",
        ruleset_id=record["queueBarrier"]["ruleset"]["rulesetId"],
        enforcement=barrier_enforcement,
        subject_digest=canonical_digest(barrier_payload),
    )
    external_updated, external_confirmed = _validate_v5_ruleset_revision(
        report["externalRulesetRevision"],
        label=f"queueBarrier.activationEvidence.{field}.report.externalRulesetRevision",
        ruleset_id=record["ruleset"]["rulesetId"],
        enforcement=external_enforcement,
        subject_digest=canonical_digest(external_payload),
    )
    if barrier_confirmed != external_confirmed:
        raise ContractError(f"queueBarrier.activationEvidence.{field} dual final reread differs")
    run_created = _timestamp(
        report["runCreatedAt"],
        f"queueBarrier.activationEvidence.{field}.report.runCreatedAt",
    )
    run_updated = _timestamp(
        report["runUpdatedAt"],
        f"queueBarrier.activationEvidence.{field}.report.runUpdatedAt",
    )
    latest_revision = max(barrier_updated, external_updated)
    if run_created <= latest_revision or run_updated < run_created:
        raise ContractError(
            f"queueBarrier.activationEvidence.{field} run does not postdate both revisions"
        )
    if barrier_confirmed < run_updated:
        raise ContractError(
            f"queueBarrier.activationEvidence.{field} final reread predates run completion"
        )
    pull = _object(
        report["pullRequest"],
        f"queueBarrier.activationEvidence.{field}.report.pullRequest",
    )
    _exact_keys(
        pull,
        {"number", "headRef", "headSha", "baseRef", "baseSha"},
        f"queueBarrier.activationEvidence.{field}.report.pullRequest",
    )
    # The executor cannot recreate historical, transient queue state after the
    # capture window.  Schema v5 therefore accepts only the normalized values
    # emitted by Doctrine's read-only provider collector and closes every PR
    # leaf over a separate binding.  The artifact digest remains byte lineage;
    # it is deliberately not treated as an oracle for unbound PR claims.
    bindings = _object(
        item["bindings"], f"queueBarrier.activationEvidence.{field}.bindings"
    )
    _exact_keys(
        bindings,
        {
            "barrierRulesetId",
            "guardedRulesetId",
            "targetRepositoryId",
            "sourceRepositoryId",
            "sourceCommitSha",
            "headSha",
            "ruleSuiteId",
            "runId",
            "checkRunId",
            "pullRequestNumber",
            *V5_PULL_REQUEST_BINDING_KEYS,
        },
        f"queueBarrier.activationEvidence.{field}.bindings",
    )
    for key in ["pullRequestHeadSha", "pullRequestBaseSha"]:
        _sha(bindings[key], f"queueBarrier.activationEvidence.{field}.bindings.{key}")
    for key in ["pullRequestHeadRef", "pullRequestBaseRef"]:
        _string(
            bindings[key], f"queueBarrier.activationEvidence.{field}.bindings.{key}"
        )
    if (
        pull["number"] != bindings["pullRequestNumber"]
        or pull["baseRef"] != "main"
        or pull["headRef"] != bindings["pullRequestHeadRef"]
        or pull["headSha"] != bindings["pullRequestHeadSha"]
        or pull["baseRef"] != bindings["pullRequestBaseRef"]
        or pull["baseSha"] != bindings["pullRequestBaseSha"]
    ):
        raise ContractError(
            f"queueBarrier.activationEvidence.{field} pull-request identity differs"
        )
    for key in ["headSha", "baseSha"]:
        _sha(pull[key], f"queueBarrier.activationEvidence.{field}.report.pullRequest.{key}")
    _string(
        pull["headRef"],
        f"queueBarrier.activationEvidence.{field}.report.pullRequest.headRef",
    )

    external_created: datetime | None = None
    external_updated_at: datetime | None = None
    external = report["externalAdmission"]
    if field == "pullRequestNoMutationCanary":
        if external is not None:
            raise ContractError("v5 pull-request canary cannot borrow external-run authority")
        if V5_PULL_REQUEST_CANARY_HEAD_REF_RE.fullmatch(pull["headRef"]) is None:
            raise ContractError(
                "v5 pull-request canary head ref is outside the canonical canary target"
            )
    else:
        external = _object(
            external,
            f"queueBarrier.activationEvidence.{field}.report.externalAdmission",
        )
        _exact_keys(
            external,
            {"attempt", "check", "workflowRun", "digest"},
            f"queueBarrier.activationEvidence.{field}.report.externalAdmission",
        )
        check = _object(external["check"], f"{field} external check")
        workflow_run = _object(external["workflowRun"], f"{field} external workflow run")
        _exact_keys(
            check,
            {"id", "name", "headSha", "status", "conclusion", "runId", "detailsUrl", "app"},
            f"{field} external check",
        )
        _exact_keys(
            workflow_run,
            {
                "id",
                "runAttempt",
                "path",
                "event",
                "headSha",
                "status",
                "conclusion",
                "createdAt",
                "updatedAt",
                "jobId",
            },
            f"{field} external workflow run",
        )
        expected_conclusion = "failure" if field == "activeExternalFailureCanary" else "success"
        allowed_paths = {
            WORKFLOW_PATH,
            f"{WORKFLOW_PATH}@{record['workflowSource']['commitSha']}",
        }
        attempt = _positive_integer(external["attempt"], f"{field} external attempt")
        check_id = _positive_integer(check["id"], f"{field} external check id")
        check_run_id = _positive_integer(
            check["runId"], f"{field} external check runId"
        )
        workflow_run_id = _positive_integer(
            workflow_run["id"], f"{field} external workflow run id"
        )
        workflow_attempt = _positive_integer(
            workflow_run["runAttempt"], f"{field} external workflow run attempt"
        )
        workflow_job_id = _positive_integer(
            workflow_run["jobId"], f"{field} external workflow job id"
        )
        _v5_details_url_repository_name(
            check["detailsUrl"],
            run_id=workflow_run_id,
            job_id=workflow_job_id,
            label=f"{field} external check detailsUrl",
        )
        if (
            attempt != workflow_attempt
            or check_id != workflow_job_id
            or check["name"] != REQUIRED_CHECK
            or check["headSha"] != bindings["headSha"]
            or check["status"] != "completed"
            or check["conclusion"] != expected_conclusion
            or check_run_id != workflow_run_id
            or check["app"] != GITHUB_ACTIONS_APP
            or workflow_run["path"] not in allowed_paths
            or workflow_run["event"] != "merge_group"
            or workflow_run["headSha"] != bindings["headSha"]
            or workflow_run["status"] != "completed"
            or workflow_run["conclusion"] != expected_conclusion
            or external["digest"]
            != canonical_digest({"check": check, "workflowRun": workflow_run})
        ):
            raise ContractError(
                f"queueBarrier.activationEvidence.{field} sealed external identity differs"
            )
        external_created = _timestamp(workflow_run["createdAt"], f"{field} external createdAt")
        external_updated_at = _timestamp(workflow_run["updatedAt"], f"{field} external updatedAt")
        if (
            external_created <= latest_revision
            or external_updated_at < external_created
            or external_updated_at > run_updated
            or barrier_confirmed < external_updated_at
        ):
            raise ContractError(
                f"queueBarrier.activationEvidence.{field} external run chronology differs"
            )

    provider = item["providerVerdicts"]
    merge_completion_at: datetime | None = None
    if isinstance(provider, dict):
        if external_created is None or external_updated_at is None:
            raise ContractError(
                f"queueBarrier.activationEvidence.{field} lacks external run chronology"
            )
        suite_at = _timestamp(provider["pushedAt"], f"{field} provider pushedAt")
        if (
            suite_at <= latest_revision
            or suite_at > run_created
            or suite_at > external_created
            or barrier_confirmed < suite_at
        ):
            raise ContractError(
                f"queueBarrier.activationEvidence.{field} rule suite revision chronology differs"
            )
        merge_completion_at = max(run_updated, external_updated_at)
        terminal = _object(
            provider["terminalAggregate"], f"{field} terminal aggregate"
        )
        terminal_at = _timestamp(
            terminal["observedAt"], f"{field} terminal aggregate observedAt"
        )
        if terminal_at < merge_completion_at or terminal_at > barrier_confirmed:
            raise ContractError(
                f"queueBarrier.activationEvidence.{field} terminal aggregate chronology differs"
            )
    outcome = item["queueOutcome"]
    if isinstance(outcome, dict):
        _exact_keys(
            outcome,
            {
                "owner",
                "cause",
                "outcome",
                "targetVisibility",
                "preQueueEntryId",
                "postQueueEntry",
                "postQueueEntryReadback",
                "pullRequestMerged",
                "pullRequestHeadRef",
                "pullRequestHeadSha",
                "pullRequestHeadShaAfter",
                "pullRequestHeadTree",
                "pullRequestBaseRef",
                "pullRequestBaseSha",
                "pullRequestBaseTree",
                "candidateSha",
                "candidateTree",
                "defaultBranchBeforeSha",
                "defaultBranchAfterSha",
                "defaultBranchBeforeTree",
                "defaultBranchAfterTree",
                "defaultBranchRef",
                "observedAt",
            },
            f"queueBarrier.activationEvidence.{field}.queueOutcome",
        )
        post_queue = _object(
            outcome["postQueueEntryReadback"], f"{field} postQueueEntryReadback"
        )
        _exact_keys(
            post_queue,
            {
                "surface",
                "repositoryId",
                "repositoryNodeId",
                "pullRequestNumber",
                "pullRequestHeadRef",
                "pullRequestHeadSha",
                "mergeQueueEntry",
                "observedAt",
            },
            f"{field} postQueueEntryReadback",
        )
        if (
            outcome["postQueueEntry"] is not None
            or post_queue["surface"]
            != "graphql-v4:repository.pullRequest.mergeQueueEntry"
            or post_queue["repositoryId"] != TARGET_REPOSITORY_ID
            or post_queue["repositoryNodeId"] != TARGET_REPOSITORY_NODE_ID
            or post_queue["pullRequestNumber"] != bindings["pullRequestNumber"]
            or post_queue["pullRequestHeadRef"] != outcome["pullRequestHeadRef"]
            or post_queue["pullRequestHeadSha"] != outcome["pullRequestHeadShaAfter"]
            or post_queue["mergeQueueEntry"] is not None
            or post_queue["observedAt"] != outcome["observedAt"]
            or outcome["defaultBranchRef"] != "refs/heads/main"
            or pull["headRef"] != outcome["pullRequestHeadRef"]
            or pull["headSha"] != outcome["pullRequestHeadSha"]
            or pull["baseRef"] != outcome["pullRequestBaseRef"]
            or pull["baseSha"] != outcome["pullRequestBaseSha"]
            or outcome["pullRequestHeadRef"] != bindings["pullRequestHeadRef"]
            or outcome["pullRequestHeadSha"] != bindings["pullRequestHeadSha"]
            or outcome["pullRequestBaseRef"] != bindings["pullRequestBaseRef"]
            or outcome["pullRequestBaseSha"] != bindings["pullRequestBaseSha"]
        ):
            raise ContractError(
                f"queueBarrier.activationEvidence.{field} current queue/PR binding differs"
            )
        outcome_at = _timestamp(outcome["observedAt"], f"{field} queue outcome observedAt")
        if merge_completion_at is None or outcome_at < merge_completion_at:
            raise ContractError(
                f"queueBarrier.activationEvidence.{field} queue outcome predates run completion"
            )
        if barrier_confirmed < outcome_at:
            raise ContractError(
                f"queueBarrier.activationEvidence.{field} final reread predates queue outcome"
            )
    if item["subjectDigest"] != canonical_digest(_v4_queue_canary_subject(item)):
        raise ContractError(f"queueBarrier.activationEvidence.{field}.subjectDigest differs")
    return observed


def _validate_v4_queue_transition(record: dict[str, Any], value: Any) -> dict[str, Any]:
    transition = _object(value, "queueBarrier.activationEvidence.activationTransition")
    _exact_keys(
        transition,
        {
            "kind", "schemaVersion", "authorization", "pre", "mutation", "post",
            "effectiveRulesReadback", "audit", "executorReport", "capturedAt",
        },
        "queueBarrier.activationEvidence.activationTransition",
    )
    if transition["kind"] != "queue-barrier-ruleset-activation-transition" or transition["schemaVersion"] != 1:
        raise ContractError("queue-barrier activation transition identity differs")
    captured = _timestamp(transition["capturedAt"], "queue-barrier transition capturedAt")
    barrier = record["queueBarrier"]
    sequencing = record["activationSequencing"]
    ruleset_id = _positive_integer(barrier["ruleset"]["rulesetId"], "queueBarrier.ruleset.rulesetId")
    authorization = _object(transition["authorization"], "queue-barrier transition authorization")
    _exact_keys(authorization, {"desiredState", "executor", "desiredPayloadDigest", "activationEvidenceDigest"}, "queue-barrier transition authorization")
    desired = _object(authorization["desiredState"], "queue-barrier transition desiredState")
    _exact_keys(desired, {"repositoryId", "commitSha", "path", "gitBlobSha", "exactBytesDigest", "semanticDigest"}, "queue-barrier transition desiredState")
    if desired["repositoryId"] != DOCTRINE_REPOSITORY_ID or desired["path"] != DOCTRINE_RECORD_PATH:
        raise ContractError("queue-barrier transition desired-state authority differs")
    for field in ["commitSha", "gitBlobSha"]:
        _sha(desired[field], f"queue-barrier transition desiredState.{field}")
    for field in ["exactBytesDigest", "semanticDigest"]:
        _digest(desired[field], f"queue-barrier transition desiredState.{field}")
    executor = _object(authorization["executor"], "queue-barrier transition executor")
    _exact_keys(executor, {"repositoryId", "commitSha", "path", "exactBytesDigest"}, "queue-barrier transition executor")
    expected_executor = {
        key: sequencing["executor"][key]
        for key in ["repositoryId", "commitSha", "path", "exactBytesDigest"]
    }
    if executor != expected_executor:
        raise ContractError("queue-barrier transition executor differs from shared source authority")
    active_payload = expected_v4_ruleset(record, "queueBarrier", enforcement="active")
    evaluate_payload = expected_v4_ruleset(record, "queueBarrier", enforcement="evaluate")
    evidence = barrier["activationEvidence"]
    expected_evidence_digest = canonical_digest({
        field: evidence[field] for field in QUEUE_BARRIER_PRE_ACTIVATION_EVIDENCE
    })
    if (
        authorization["desiredPayloadDigest"] != canonical_digest(active_payload)
        or authorization["activationEvidenceDigest"] != expected_evidence_digest
    ):
        raise ContractError("queue-barrier transition authorization digest differs")
    pre = _validate_transition_readback(transition["pre"], "queue-barrier transition pre", ruleset_id=ruleset_id, enforcement="evaluate")
    post = _validate_transition_readback(transition["post"], "queue-barrier transition post", ruleset_id=ruleset_id, enforcement="active")
    effective = evidence["effectiveRulesReadback"]
    if (
        pre["stateDigest"] != canonical_digest(evaluate_payload)
        or post["stateDigest"] != canonical_digest(active_payload)
        or effective is None
        or post["effectiveRulesDigest"] != effective["subjectDigest"]
        or transition["effectiveRulesReadback"] != effective
    ):
        raise ContractError("queue-barrier transition readback digest differs")

    mutation = _object(transition["mutation"], "queue-barrier transition mutation")
    _exact_keys(mutation, {"action", "outcome", "actor", "requestId", "subjectRuleset", "applyLock", "activationAttestation"}, "queue-barrier transition mutation")
    actor = _validate_actor(mutation["actor"], "queue-barrier transition mutation actor", include_type=True)
    request_id = _string(mutation["requestId"], "queue-barrier transition requestId")
    if mutation["action"] != "update" or mutation["outcome"] != "updated" or AUDIT_REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ContractError("queue-barrier transition mutation differs")
    if mutation["subjectRuleset"] != {
        "rulesetId": ruleset_id,
        "name": BARRIER_RULESET_NAME,
        "sourceCommitSha": barrier["workflowSource"]["commitSha"],
    }:
        raise ContractError("queue-barrier transition subject differs")
    lock = _object(mutation["applyLock"], "queue-barrier transition applyLock")
    _exact_keys(lock, {"repositoryId", "ref", "tagObjectSha", "tagMessageDigest", "executorCommitSha", "nonce", "claimedAt", "actor", "acquireOutcome", "releaseOutcome", "finalRefAbsent"}, "queue-barrier transition applyLock")
    if (
        lock["repositoryId"] != EXECUTOR_REPOSITORY_ID
        or lock["ref"] != APPLY_LOCK_REF
        or lock["executorCommitSha"] != executor["commitSha"]
        or lock["actor"] != actor
        or lock["acquireOutcome"] != "acquired"
        or lock["releaseOutcome"] != "released"
        or lock["finalRefAbsent"] is not True
    ):
        raise ContractError("queue-barrier transition lock lifecycle differs")
    _sha(lock["tagObjectSha"], "queue-barrier transition lock tag")
    _digest(lock["tagMessageDigest"], "queue-barrier transition lock digest")
    if not isinstance(lock["nonce"], str) or NONCE_RE.fullmatch(lock["nonce"]) is None:
        raise ContractError("queue-barrier transition lock nonce differs")
    claimed = _timestamp(lock["claimedAt"], "queue-barrier transition lock claimedAt")
    attestation = _validate_attestation_projection(mutation["activationAttestation"], "queue-barrier transition attestation")
    if (
        attestation["ref"] != f"{ATTESTATION_REF_PREFIX}{lock['nonce']}"
        or attestation["policy"]["commitSha"] != executor["commitSha"]
        or _timestamp(attestation["evidenceCutoffAt"], "queue-barrier transition evidence cutoff") < claimed
        or _timestamp(attestation["evidenceCutoffAt"], "queue-barrier transition evidence cutoff") > captured
    ):
        raise ContractError("queue-barrier transition attestation differs")

    audit = _object(transition["audit"], "queue-barrier transition audit")
    _exact_keys(audit, {"documentId", "action", "actor", "organization", "createdAtEpochMs", "operationType", "requestId", "ruleset", "providerProjectionDigest", "normalizedDigest"}, "queue-barrier transition audit")
    normalized_audit = {
        key: copy.deepcopy(value)
        for key, value in audit.items()
        if key not in {"providerProjectionDigest", "normalizedDigest"}
    }
    if (
        audit["action"] != AUDIT_ACTION
        or audit["actor"] != {"id": GITHUB_ACTOR_ID, "login": GITHUB_ACTOR_LOGIN}
        or audit["organization"] != {"id": ORGANIZATION_ID, "login": ORGANIZATION}
        or audit["requestId"] != request_id
        or audit["ruleset"] != {
            "id": ruleset_id,
            "name": BARRIER_RULESET_NAME,
            "sourceType": "Organization",
            "enforcement": "active",
        }
        or audit["normalizedDigest"] != canonical_digest(normalized_audit)
    ):
        raise ContractError("queue-barrier transition audit differs")
    _positive_integer(audit["createdAtEpochMs"], "queue-barrier transition audit createdAtEpochMs")
    for field in ["providerProjectionDigest", "normalizedDigest"]:
        _digest(audit[field], f"queue-barrier transition audit {field}")
    report = _object(transition["executorReport"], "queue-barrier transition executorReport")
    _exact_keys(report, {"path", "gitBlobSha", "exactBytesDigest", "bodyDigest", "evidenceDigest"}, "queue-barrier transition executorReport")
    if report["path"] != QUEUE_BARRIER_ACTIVATION_EVIDENCE_PATH:
        raise ContractError("queue-barrier transition executor report path differs")
    _sha(report["gitBlobSha"], "queue-barrier transition executor report blob")
    for field in ["exactBytesDigest", "bodyDigest", "evidenceDigest"]:
        _digest(report[field], f"queue-barrier transition executor report {field}")
    return transition


def validate_v4_record(
    record: Any,
    runtime_workflow_sha: str,
    runtime_executor_digest: str,
    *,
    schema_version: int = 4,
) -> dict[str, Any]:
    if schema_version not in {4, 5}:
        raise ContractError("shared queue-barrier schema version is unsupported")
    root = _object(record, f"v{schema_version} desired state")
    _exact_keys(
        root,
        {
            "$schema", "schemaVersion", "kind", "id", "owner", "owningDecision",
            "organization", "ruleset", "workflowSource", "migration",
            "activationEvidence", "queueBarrier", "activationSequencing", "recovery",
        },
        f"v{schema_version} desired state",
    )
    if root["schemaVersion"] != schema_version:
        raise ContractError(f"v{schema_version} desired state schemaVersion differs")
    external_source = _object(root["workflowSource"], "workflowSource")
    for required_field in ["localRequiredChecks", "negativeControlPolicy"]:
        if required_field not in external_source:
            raise ContractError(
                f"v4 workflowSource requires {required_field} in every phase"
            )
    external_evidence = _object(root["activationEvidence"], "activationEvidence")
    for field in EVIDENCE_KINDS:
        item = external_evidence.get(field)
        if item is not None and (
            not isinstance(item, dict) or "negativeControl" not in item
        ):
            raise ContractError(
                f"v4 activationEvidence.{field} requires negativeControl member"
            )
    validate_record(_v4_external_projection(root))
    bundle = validate_v4_protected_source_envelope(
        root,
        runtime_workflow_sha,
        schema_version=schema_version,
    )
    sequencing = _object(root["activationSequencing"], "activationSequencing")
    _exact_keys(sequencing, {"kind", "protectedSourceBundle", "executor", "applyLock", "activationOrder", "recoveryOrder", "externalActivationPrecondition"}, "activationSequencing")
    if (
        sequencing["kind"] != "public-skills-ruleset-activation-sequence"
        or sequencing["applyLock"] != {
            "repositoryId": EXECUTOR_REPOSITORY_ID,
            "ref": APPLY_LOCK_REF,
            "fencing": "annotated-tag-claim",
        }
        or sequencing["activationOrder"] != [
            "public-skills-merge-queue-barrier-active-effective",
            "public-skills-external-admission-active-effective",
        ]
        or sequencing["recoveryOrder"] != [
            "public-skills-external-admission-non-active-effective",
            "public-skills-merge-queue-barrier-downgrade",
        ]
    ):
        raise ContractError("v4 activation sequencing identity differs")
    if bundle["commitSha"] is not None:
        executor = sequencing["executor"]
        if executor["exactBytesDigest"] != _digest(runtime_executor_digest, "runtime executor digest"):
            raise ContractError("v4 executor exact bytes digest differs from runtime")

    barrier = _object(root["queueBarrier"], "queueBarrier")
    _exact_keys(barrier, {"kind", "ruleset", "workflowSource", "runtimeContract", "migration", "activationEvidence", "recovery"}, "queueBarrier")
    if barrier["kind"] != "public-skills-merge-queue-barrier":
        raise ContractError("queueBarrier kind differs")
    ruleset = _object(barrier["ruleset"], "queueBarrier.ruleset")
    _exact_keys(ruleset, {"rulesetId", "name", "target", "enforcement", "bypassActors", "targetRepositories", "refInclude", "refExclude", "doNotEnforceOnCreate"}, "queueBarrier.ruleset")
    if ruleset["rulesetId"] is not None:
        _positive_integer(ruleset["rulesetId"], "queueBarrier.ruleset.rulesetId")
    if {
        "name": ruleset["name"],
        "target": ruleset["target"],
        "bypassActors": ruleset["bypassActors"],
        "targetRepositories": ruleset["targetRepositories"],
        "refInclude": ruleset["refInclude"],
        "refExclude": ruleset["refExclude"],
        "doNotEnforceOnCreate": ruleset["doNotEnforceOnCreate"],
    } != {
        "name": BARRIER_RULESET_NAME,
        "target": "branch",
        "bypassActors": [],
        "targetRepositories": root["ruleset"]["targetRepositories"],
        "refInclude": ["~DEFAULT_BRANCH"],
        "refExclude": [],
        "doNotEnforceOnCreate": False,
    }:
        raise ContractError("queueBarrier ruleset identity differs")
    if ruleset["rulesetId"] is not None and ruleset["rulesetId"] == root["ruleset"]["rulesetId"]:
        raise ContractError("queueBarrier and external ruleset IDs collide")

    source = _object(barrier["workflowSource"], "queueBarrier.workflowSource")
    _exact_keys(source, {"repositoryId", "repository", "workflowPath", "workflowName", "requiredCheck", "controllerPath", "policyPath", "ref", "commitSha"}, "queueBarrier.workflowSource")
    expected_source = {
        "repositoryId": EXECUTOR_REPOSITORY_ID,
        "repository": EXECUTOR_REPOSITORY,
        "workflowPath": BARRIER_WORKFLOW_PATH,
        "workflowName": BARRIER_WORKFLOW_NAME,
        "requiredCheck": BARRIER_REQUIRED_CHECK,
        "controllerPath": BARRIER_CONTROLLER_PATH,
        "policyPath": BARRIER_POLICY_PATH,
        "ref": EXECUTOR_BRANCH,
    }
    if any(source[key] != expected for key, expected in expected_source.items()):
        raise ContractError("queueBarrier workflow source identity differs")
    if source["commitSha"] is not None:
        _sha(source["commitSha"], "queueBarrier.workflowSource.commitSha")

    runtime = _object(barrier["runtimeContract"], "queueBarrier.runtimeContract")
    expected_runtime = {
        "guardedRulesetId": root["ruleset"]["rulesetId"],
        "guardedRequiredCheck": REQUIRED_CHECK,
        "pullRequestDecision": "identity-check-and-pass",
        "mergeGroupDecision": "pass-only-when-external-active-effective-and-check-success",
        "credentialMutationAuthority": "none",
        "queueRemovalOwner": "github-provider",
        "externalCheckObservation": {
            "attempts": 120,
            "intervalMilliseconds": 5000,
            "terminalConclusions": ["failure", "success"],
            "requiredConclusion": "success",
            "timeoutDisposition": "failure",
        },
        "admissionCanaryContract": {
            "sourcePolicyPath": POLICY_PATH,
            "canaryClass": "strict-same-tree-no-diff",
            "launchBaseAdvance": "authorized-empty-ancestry-with-unchanged-fixture-tree",
        },
        "permissions": {
            "actions": "read",
            "checks": "read",
            "contents": "read",
            "pullRequests": "read",
        },
    }
    if runtime != expected_runtime:
        raise ContractError("queueBarrier runtime contract differs")
    forbidden = {
        "clientMutationId", "compareAndSwap", "dequeueMutation",
        "dequeuePullRequest", "mutationResponse", "pullRequestsWrite",
        "writePermission",
    }
    if _v4_nested_keys(barrier) & forbidden:
        raise ContractError("queueBarrier claims forbidden queue mutation authority")

    migration = _object(barrier["migration"], "queueBarrier.migration")
    _exact_keys(migration, {"packetId", "class", "phase", "tracker", "compatibility", "recoveryPlan"}, "queueBarrier.migration")
    phase = migration["phase"]
    phase_enforcement = (
        V5_QUEUE_BARRIER_PHASE_ENFORCEMENT
        if schema_version == 5
        else QUEUE_BARRIER_PHASE_ENFORCEMENT
    )
    if phase not in phase_enforcement or ruleset["enforcement"] not in phase_enforcement[phase]:
        raise ContractError("queueBarrier phase/enforcement differs")
    if re.fullmatch(r"public-skills-merge-queue-barrier@[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9a-f]{12}", _string(migration["packetId"], "queueBarrier.migration.packetId")) is None:
        raise ContractError("queueBarrier migration packet differs")
    if migration["class"] != "required-immediate":
        raise ContractError("queueBarrier migration class differs")
    _github_url(migration["tracker"], "queueBarrier.migration.tracker")
    _string(migration["recoveryPlan"], "queueBarrier.migration.recoveryPlan", minimum=20)
    compatibility = _object(migration["compatibility"], "queueBarrier.migration.compatibility")
    _exact_keys(compatibility, {"oldAcceptedUntil", "newRequiredAfter"}, "queueBarrier.migration.compatibility")
    for field in ["oldAcceptedUntil", "newRequiredAfter"]:
        if compatibility[field] is not None and DATE_RE.fullmatch(
            _string(compatibility[field], f"queueBarrier.migration.compatibility.{field}")
        ) is None:
            raise ContractError(f"queueBarrier migration {field} must be an ISO date or null")
    if (
        compatibility["oldAcceptedUntil"] is not None
        and compatibility["newRequiredAfter"] is not None
        and compatibility["oldAcceptedUntil"] >= compatibility["newRequiredAfter"]
    ):
        raise ContractError("queueBarrier migration compatibility is not monotonic")

    evidence = _object(barrier["activationEvidence"], "queueBarrier.activationEvidence")
    _exact_keys(evidence, {*QUEUE_BARRIER_EVIDENCE_KINDS, "activationTransition"}, "queueBarrier.activationEvidence")
    observed = {
        field: result
        for field in QUEUE_BARRIER_EVIDENCE_KINDS
        if (
            result := (
                _validate_v5_queue_evidence(root, field, evidence[field])
                if schema_version == 5
                else _validate_v4_queue_evidence(root, field, evidence[field])
            )
        )
        is not None
    }
    if (
        evidence["evaluateReadback"] is not None
        and evidence["evaluateReadback"]["subjectDigest"]
        != canonical_digest(
            expected_v4_ruleset(root, "queueBarrier", enforcement="evaluate")
        )
    ):
        raise ContractError("queueBarrier evaluate readback digest differs")
    if evidence["effectiveRulesReadback"] is not None:
        expected_effective = [{
            "repositoryId": TARGET_REPOSITORY_ID,
            "rulesetId": ruleset["rulesetId"],
            "rulesetPresent": True,
        }]
        if evidence["effectiveRulesReadback"]["subjectDigest"] != canonical_digest(expected_effective):
            raise ContractError("queueBarrier effective-rules readback digest differs")
    transition = evidence["activationTransition"]
    activated_phases = (
        {"active", "post-activation", "verified"}
        if schema_version == 5
        else {"active"}
    )
    if phase in activated_phases:
        _validate_v4_queue_transition(root, transition)
    elif transition is not None:
        raise ContractError(
            "queueBarrier activation transition is allowed only in activated phases"
        )
    required: tuple[str, ...] = ()
    if phase in {"canary", "ratchet"}:
        required = QUEUE_BARRIER_PRE_ACTIVATION_EVIDENCE
    elif phase in activated_phases:
        required = QUEUE_BARRIER_ACTIVE_EVIDENCE
    missing = [field for field in required if evidence[field] is None]
    if missing:
        raise ContractError(f"queueBarrier {phase} lacks evidence {missing}")
    if source["commitSha"] is None:
        if phase != "expand" or ruleset["rulesetId"] is not None or ruleset["enforcement"] != "evaluate":
            raise ContractError("unresolved queueBarrier source is allowed only in unbound expand")
    elif phase in {
        "reconcile",
        "canary",
        "ratchet",
        "active",
        "post-activation",
        "verified",
        "recovery",
    } and ruleset["rulesetId"] is None:
        raise ContractError(f"queueBarrier {phase} requires a ruleset ID")

    if phase == "recovery":
        if root["migration"]["phase"] != "recovery" or barrier["recovery"] is None:
            raise ContractError("queueBarrier recovery must follow external recovery")
        recovery = _object(barrier["recovery"], "queueBarrier.recovery")
        _exact_keys(recovery, {"reason", "tracker", "initiatedAt"}, "queueBarrier.recovery")
        _string(recovery["reason"], "queueBarrier.recovery.reason", minimum=10)
        _github_url(recovery["tracker"], "queueBarrier.recovery.tracker")
        _timestamp(recovery["initiatedAt"], "queueBarrier.recovery.initiatedAt")
    elif barrier["recovery"] is not None:
        raise ContractError("queueBarrier recovery record is out of phase")
    external_phase = root["migration"]["phase"]
    external_precondition = sequencing["externalActivationPrecondition"]
    active_removal = evidence["activeProviderRemovalCanary"]
    if external_phase in {"ratchet", "active"}:
        expected_external_barrier_phases = (
            {"active"}
            if schema_version == 4 or external_phase == "ratchet"
            else {"post-activation", "verified"}
        )
        if phase not in expected_external_barrier_phases or transition is None or evidence["effectiveRulesReadback"] is None or active_removal is None:
            raise ContractError("external activation lacks active/effective queueBarrier proof")
        precondition = _object(external_precondition, "activationSequencing.externalActivationPrecondition")
        _exact_keys(precondition, {"barrierRulesetId", "barrierSourceCommitSha", "barrierActivationTransitionDigest", "barrierEffectiveRulesDigest", "barrierProviderRemovalDigest", "barrierAttestationClaimDigest", "executorCommitSha", "applyLockRef", "observedAt"}, "activationSequencing.externalActivationPrecondition")
        expected_precondition = {
            "barrierRulesetId": ruleset["rulesetId"],
            "barrierSourceCommitSha": source["commitSha"],
            "barrierActivationTransitionDigest": canonical_digest(transition),
            "barrierEffectiveRulesDigest": evidence["effectiveRulesReadback"]["subjectDigest"],
            "barrierProviderRemovalDigest": active_removal["subjectDigest"],
            "barrierAttestationClaimDigest": transition["mutation"]["activationAttestation"]["claimDigest"],
            "executorCommitSha": sequencing["executor"]["commitSha"],
            "applyLockRef": APPLY_LOCK_REF,
        }
        if any(precondition[key] != expected for key, expected in expected_precondition.items()):
            raise ContractError("external activation precondition differs from queueBarrier proof")
        precondition_at = _timestamp(precondition["observedAt"], "external activation precondition observedAt")
        if precondition_at < _timestamp(active_removal["queueOutcome"]["observedAt"], "active removal observedAt"):
            raise ContractError("external activation precondition predates provider removal")
        external_observed: list[datetime] = []
        barrier_active_at = _timestamp(
            transition["capturedAt"], "queueBarrier transition capturedAt"
        )
        for external_field in [
            "evaluateReadback", "pullRequestCanary", "mergeGroupCanary",
            "negativeControl", "evaluateRuleSuiteReadback",
        ]:
            external_item = root["activationEvidence"][external_field]
            if external_item is None:
                continue
            observed_at = _timestamp(
                external_item["observedAt"],
                f"activationEvidence.{external_field}.observedAt",
            )
            external_observed.append(observed_at)
            if external_field != "evaluateReadback" and observed_at < barrier_active_at:
                raise ContractError(
                    f"external {external_field} predates queueBarrier activation"
                )
        if external_observed and precondition_at < max(external_observed):
            raise ContractError("external activation precondition predates evaluate-canary evidence")
        external_merge = root["activationEvidence"]["mergeGroupCanary"]
        removal_bindings = active_removal["bindings"]
        if external_merge is None:
            raise ContractError("external merge canary does not dual-observe barrier removal")
        external_bindings = external_merge["bindings"]
        run_match = re.fullmatch(
            r"https://github\.com/[^/]+/[^/]+/actions/runs/([1-9][0-9]*)/?",
            external_merge["locator"],
        )
        if (
            external_bindings["headSha"] != removal_bindings["headSha"]
            or external_bindings["ruleSuiteId"] != removal_bindings["ruleSuiteId"]
            or run_match is None
            or int(run_match.group(1)) == removal_bindings["runId"]
        ):
            raise ContractError(
                "external merge canary and barrier removal do not bind one suite with distinct runs"
            )
    elif external_precondition is not None:
        raise ContractError("external activation precondition is out of phase")
    final_barrier_fields = ["activePassThroughCanary", "activeExternalFailureCanary"]
    if external_phase != "active" and any(evidence[field] is not None for field in final_barrier_fields):
        raise ContractError("queueBarrier final canaries are allowed only after external activation")
    if external_phase == "active":
        final_required = schema_version == 4 or phase == "verified"
        missing_final = [field for field in final_barrier_fields if evidence[field] is None]
        if final_required and missing_final:
            raise ContractError(f"active external admission lacks queueBarrier final canaries {missing_final}")
        external_transition = root["activationEvidence"]["activationTransition"]
        if external_transition is None:
            raise ContractError("active external admission lacks its activation transition")
        external_active_at = _timestamp(
            external_transition["capturedAt"],
            "external activation transition capturedAt",
        )
        for field in final_barrier_fields:
            if evidence[field] is None:
                continue
            final_at = _timestamp(
                evidence[field]["queueOutcome"]["observedAt"],
                f"{field} queue outcome",
            )
            if final_at < external_active_at:
                raise ContractError(f"{field} predates external activation")

    canary_fields = [
        field for field in [
            "pullRequestNoMutationCanary", "evaluateMergeGroupFailureCanary",
            "activeProviderRemovalCanary", "activePassThroughCanary",
            "activeExternalFailureCanary",
        ] if evidence[field] is not None
    ]
    for key in ["headSha", "ruleSuiteId", "runId", "checkRunId", "pullRequestNumber"]:
        values = [evidence[field]["bindings"][key] for field in canary_fields if evidence[field]["bindings"][key] is not None]
        if len(values) != len(set(values)):
            raise ContractError(f"queueBarrier canaries reuse {key}")
    chronology: list[tuple[str, datetime]] = []
    if evidence["evaluateReadback"] is not None:
        chronology.append(("evaluateReadback", observed["evaluateReadback"]))
    if evidence["pullRequestNoMutationCanary"] is not None:
        chronology.append(("pullRequestNoMutationCanary", observed["pullRequestNoMutationCanary"]))
    if evidence["evaluateMergeGroupFailureCanary"] is not None:
        chronology.append((
            "evaluateMergeGroupFailureCanary",
            _timestamp(
                evidence["evaluateMergeGroupFailureCanary"]["queueOutcome"]["observedAt"],
                "evaluate merge queue outcome",
            ),
        ))
    if transition is not None:
        chronology.append(("activationTransition", _timestamp(transition["capturedAt"], "queueBarrier transition capturedAt")))
    if evidence["effectiveRulesReadback"] is not None:
        chronology.append(("effectiveRulesReadback", observed["effectiveRulesReadback"]))
    for active_field in [
        "activeProviderRemovalCanary",
        "activePassThroughCanary",
        "activeExternalFailureCanary",
    ]:
        if evidence[active_field] is not None:
            chronology.append((
                active_field,
                _timestamp(
                    evidence[active_field]["queueOutcome"]["observedAt"],
                    f"{active_field} queue outcome",
                ),
            ))
    for (left_name, left), (right_name, right) in zip(chronology, chronology[1:]):
        if left > right:
            raise ContractError(
                f"queueBarrier evidence chronology moves backward from {left_name} to {right_name}"
            )
    return root


def validate_v5_record(
    record: Any,
    runtime_workflow_sha: str,
    runtime_executor_digest: str,
) -> dict[str, Any]:
    """Validate additive schema v5 without changing any v1-v4 branch."""

    return validate_v4_record(
        record,
        runtime_workflow_sha,
        runtime_executor_digest,
        schema_version=5,
    )


def validate_v5_target_url_lifecycle(
    record: dict[str, Any],
    target: dict[str, Any],
) -> None:
    """Cross-bind sealed job URLs to the provider-observed rename lifecycle."""

    if (
        target.get("id") != TARGET_REPOSITORY_ID
        or target.get("nodeId") != TARGET_REPOSITORY_NODE_ID
        or target.get("name") not in TARGET_REPOSITORY_NAMES
    ):
        raise ContractError("v5 target URL lifecycle lacks the immutable provider repository identity")
    evidence = record["queueBarrier"]["activationEvidence"]
    observed_names: set[str] = set()
    for field in V5_EXTERNAL_DETAILS_FIELDS:
        item = evidence[field]
        if item is None:
            continue
        external = item["report"]["externalAdmission"]
        if external is None:
            raise ContractError(f"v5 {field} lacks sealed external admission identity")
        workflow_run = external["workflowRun"]
        observed_names.add(
            _v5_details_url_repository_name(
                external["check"]["detailsUrl"],
                run_id=workflow_run["id"],
                job_id=workflow_run["jobId"],
                label=f"v5 {field} external check detailsUrl",
            )
        )
    if len(observed_names) > 1:
        raise ContractError("v5 external evidence mixes staging and final target names")

    live_name = target["name"]
    if live_name == TARGET_STAGING_NAME:
        if observed_names and observed_names != {TARGET_STAGING_NAME}:
            raise ContractError("v5 final-name evidence is stale while the live target is staging")
        return

    external_phase = record["migration"]["phase"]
    barrier_phase = record["queueBarrier"]["migration"]["phase"]
    if external_phase == "recovery" and barrier_phase == "recovery":
        return
    complete = (
        external_phase == "active"
        and barrier_phase == "verified"
        and all(evidence[field] is not None for field in V5_EXTERNAL_DETAILS_FIELDS)
        and evidence["activationTransition"] is not None
        and evidence["effectiveRulesReadback"] is not None
        and record["activationEvidence"]["activationTransition"] is not None
        and record["activationSequencing"]["externalActivationPrecondition"] is not None
    )
    if not complete:
        raise ContractError(
            "v5 live final target requires verified complete transition/canary evidence"
        )


def validate_attestation_policy(value: Any) -> dict[str, Any]:
    policy = _object(value, "attestation-ruleset policy")
    _exact_keys(policy, {"schemaVersion", "kind", "sourceRepository", "ruleset"}, "attestation-ruleset policy")
    if policy["schemaVersion"] != 1 or policy["kind"] != "public-skills-activation-attestation-ruleset-policy":
        raise ContractError("attestation-ruleset policy identity differs")
    source = _object(policy["sourceRepository"], "attestation-ruleset policy.sourceRepository")
    _exact_keys(source, {"repositoryId", "repositoryNodeId", "repository", "defaultBranch"}, "attestation-ruleset policy.sourceRepository")
    if source != {
        "repositoryId": EXECUTOR_REPOSITORY_ID,
        "repositoryNodeId": EXECUTOR_REPOSITORY_NODE_ID,
        "repository": EXECUTOR_REPOSITORY,
        "defaultBranch": EXECUTOR_BRANCH,
    }:
        raise ContractError("attestation-ruleset policy source identity differs")
    ruleset = _object(policy["ruleset"], "attestation-ruleset policy.ruleset")
    _exact_keys(
        ruleset,
        {
            "name", "target", "enforcement", "bypassActors", "repositoryIds",
            "refInclude", "refExclude", "rules",
        },
        "attestation-ruleset policy.ruleset",
    )
    if ruleset != {
        "name": ATTESTATION_RULESET_NAME,
        "target": "tag",
        "enforcement": "active",
        "bypassActors": [],
        "repositoryIds": [EXECUTOR_REPOSITORY_ID],
        "refInclude": [ATTESTATION_RULESET_REF_PATTERN],
        "refExclude": [],
        "rules": ["update", "deletion", "non_fast_forward"],
    }:
        raise ContractError("attestation-ruleset policy desired state differs")
    return policy


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
        include_metadata: bool = False,
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
                request_id = response.headers.get("X-GitHub-Request-Id")
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
            result = None
            return (result, {"requestId": request_id}) if include_metadata else result
        try:
            result = strict_json_loads(raw, label=f"GitHub API {endpoint}")
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        return (result, {"requestId": request_id}) if include_metadata else result

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

    def put_observed(self, endpoint: str, payload: dict[str, Any]) -> tuple[Any, str | None]:
        body, metadata = self._request("PUT", endpoint, payload, include_metadata=True)
        request_id = metadata.get("requestId")
        if request_id is not None and (
            not isinstance(request_id, str) or AUDIT_REQUEST_ID_RE.fullmatch(request_id) is None
        ):
            raise ForgeError("GitHub PUT response has a malformed X-GitHub-Request-Id")
        return body, request_id

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


def _rule_suite_locator(locator: Any) -> tuple[str, int] | None:
    if not isinstance(locator, str):
        return None
    match = re.fullmatch(
        r"https://github\.com/([^/]+/[^/]+)/rules/rule-suites/([1-9][0-9]*)/?",
        locator,
    )
    return (match.group(1), int(match.group(2))) if match is not None else None


def _normalized_evaluate_rule_suite(
    suite: dict[str, Any],
    rule_evaluation: dict[str, Any] | None,
) -> dict[str, Any]:
    source = (
        rule_evaluation.get("rule_source")
        if isinstance(rule_evaluation, dict)
        and isinstance(rule_evaluation.get("rule_source"), dict)
        else {}
    )
    return {
        "id": suite.get("id"),
        "repositoryId": suite.get("repository_id"),
        "beforeSha": suite.get("before_sha"),
        "afterSha": suite.get("after_sha"),
        "ref": suite.get("ref"),
        "aggregateResult": suite.get("result"),
        "pushedAt": suite.get("pushed_at"),
        "ruleEvaluation": (
            {
                "ruleSource": {
                    "id": source.get("id"),
                    "type": source.get("type"),
                    "name": source.get("name"),
                },
                "ruleType": rule_evaluation.get("rule_type"),
                "enforcement": rule_evaluation.get("enforcement"),
                "result": rule_evaluation.get("result"),
                "details": rule_evaluation.get("details"),
            }
            if isinstance(rule_evaluation, dict)
            else None
        ),
    }


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


def _audit_endpoint(page: int) -> str:
    phrase = f"action:{AUDIT_ACTION} actor:{GITHUB_ACTOR_LOGIN}"
    query = urllib.parse.urlencode({
        "phrase": phrase,
        "include": "all",
        "order": "desc",
        "per_page": 100,
        "page": page,
    })
    return f"/orgs/{ORGANIZATION}/audit-log?{query}"


def _attestation_ref(nonce: str) -> str:
    if NONCE_RE.fullmatch(nonce) is None:
        raise ContractError("attestation nonce is invalid")
    return f"{ATTESTATION_REF_PREFIX}{nonce}"


def _attestation_ref_get_endpoint(nonce: str) -> str:
    return f"/repositories/{EXECUTOR_REPOSITORY_ID}/git/ref/tags/sylph-attestations/public-skills-ruleset/{nonce}"


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


def expected_v4_ruleset(
    record: dict[str, Any],
    subject: str,
    *,
    enforcement: str | None = None,
) -> dict[str, Any]:
    """Project either schema-v4 ruleset into one exact provider payload."""

    if subject not in {"externalAdmission", "queueBarrier"}:
        raise ContractError("v4 ruleset subject is unsupported")
    owner = record if subject == "externalAdmission" else record["queueBarrier"]
    ruleset = owner["ruleset"]
    source = owner["workflowSource"]
    source_sha = source["commitSha"]
    if source_sha is None:
        raise ContractError(f"{subject} workflow source commit is unresolved")
    return {
        "name": ruleset["name"],
        "target": "branch",
        "enforcement": enforcement or ruleset["enforcement"],
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
                    "path": source["workflowPath"],
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


def plan_v4_ruleset_actions(
    record: dict[str, Any],
    observations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive at most one ordered schema-v4 provider mutation."""

    _exact_keys(
        observations,
        {"externalAdmission", "queueBarrier"},
        "v4 live observations",
    )
    for key in observations:
        _exact_keys(observations[key], {"live", "effective"}, f"v4 observation {key}")
        if not isinstance(observations[key]["effective"], bool):
            raise ContractError(f"v4 observation {key}.effective must be boolean")
    if record["queueBarrier"]["workflowSource"]["commitSha"] is None:
        return []

    def subject_state(subject: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, bool]:
        owner = record if subject == "externalAdmission" else record["queueBarrier"]
        desired = expected_v4_ruleset(record, subject)
        live = observations[subject]["live"]
        if live is not None and not isinstance(live, dict):
            raise ContractError(f"v4 observation {subject}.live must be an object or null")
        return owner, desired, live, observations[subject]["effective"]

    def mutation_for(subject: str) -> dict[str, Any] | None:
        owner, desired, live, effective = subject_state(subject)
        ruleset_id = owner["ruleset"]["rulesetId"]
        phase = owner["migration"]["phase"]
        if live is None:
            if subject != "queueBarrier" or phase != "expand" or ruleset_id is not None:
                raise ContractError(f"{subject} bound or non-expand ruleset is absent")
            return {
                "subject": subject,
                "action": "create",
                "rulesetId": None,
                "payload": desired,
                "payloadDigest": canonical_digest(desired),
            }
        if ruleset_id is None:
            raise ContractError(f"{subject} live ruleset exists before Doctrine binds its ID")
        current = live.get("enforcement")
        desired_enforcement = desired["enforcement"]
        if current not in ENFORCEMENT_RANK:
            raise ContractError(f"{subject} live enforcement is unsupported")
        if phase != "recovery" and ENFORCEMENT_RANK[desired_enforcement] < ENFORCEMENT_RANK[current]:
            raise ContractError(f"{subject} non-recovery phase cannot downgrade enforcement")
        if phase == "recovery" and ENFORCEMENT_RANK[desired_enforcement] > ENFORCEMENT_RANK[current]:
            raise ContractError(f"{subject} recovery cannot escalate enforcement")
        if phase == "recovery":
            structural = copy.deepcopy(live)
            structural["enforcement"] = desired_enforcement
            if structural != desired:
                raise ContractError(
                    f"{subject} recovery permits only an enforcement downgrade"
                )
        if live == desired:
            if desired_enforcement == "active" and not effective:
                raise ContractError(f"{subject} active desired state lacks effective coverage")
            return None
        if phase == "active":
            raise ContractError(f"{subject} active phase does not admit provider repair writes")
        if phase == "canary":
            raise ContractError(f"{subject} canary phase does not admit provider repair writes")
        if phase == "ratchet":
            evaluate = copy.deepcopy(desired)
            evaluate["enforcement"] = "evaluate"
            if live != evaluate:
                raise ContractError(
                    f"{subject} ratchet admits only the exact evaluate-to-active transition"
                )
        return {
            "subject": subject,
            "action": "update",
            "rulesetId": ruleset_id,
            "payload": desired,
            "payloadDigest": canonical_digest(desired),
        }

    barrier_action = mutation_for("queueBarrier")
    if barrier_action is not None and barrier_action["action"] == "create":
        external_action = mutation_for("externalAdmission")
        if external_action is not None:
            raise ContractError(
                "schema-v4 simultaneous unsequenced subject drift is ambiguous"
            )
        return [barrier_action]

    external_phase = record["migration"]["phase"]
    barrier_phase = record["queueBarrier"]["migration"]["phase"]
    if external_phase == "recovery":
        external_action = mutation_for("externalAdmission")
        if external_action is not None:
            return [external_action]
        external_live = observations["externalAdmission"]["live"]
        if external_live is None or external_live.get("enforcement") == "active":
            raise ContractError("queueBarrier recovery requires external non-active live readback")
        return [] if barrier_action is None else [barrier_action]

    external_action = mutation_for("externalAdmission")
    if barrier_action is not None:
        if external_action is not None:
            explicitly_ordered_replay = (
                external_phase == "ratchet"
                and barrier_phase == "active"
                and barrier_action["payload"]["enforcement"] == "active"
                and external_action["payload"]["enforcement"] == "active"
            )
            if not explicitly_ordered_replay:
                raise ContractError(
                    "schema-v4 simultaneous unsequenced subject drift is ambiguous"
                )
        return [barrier_action]

    if external_action is None:
        return []
    if external_action["payload"]["enforcement"] == "active":
        barrier_owner, barrier_desired, barrier_live, barrier_effective = subject_state("queueBarrier")
        if (
            barrier_phase != "active"
            or barrier_owner["ruleset"]["enforcement"] != "active"
            or barrier_live != barrier_desired
            or not barrier_effective
            or record["activationSequencing"]["externalActivationPrecondition"] is None
        ):
            raise ContractError("external activation is ordered behind active-effective queueBarrier proof")
    return [external_action]


def plan_v5_ruleset_actions(
    record: dict[str, Any],
    observations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reuse v4 ordering while making post-activation/verified write-closed."""

    projected = copy.deepcopy(record)
    if projected["queueBarrier"]["migration"]["phase"] in {
        "post-activation",
        "verified",
    }:
        projected["queueBarrier"]["migration"]["phase"] = "active"
    return plan_v4_ruleset_actions(projected, observations)


def _v4_subject_activation_evidence_digest(
    record: dict[str, Any],
    subject: str,
) -> str:
    if subject == "externalAdmission":
        return _activation_evidence_digest(_v4_external_projection(record))
    if subject == "queueBarrier":
        evidence = record["queueBarrier"]["activationEvidence"]
        return canonical_digest({
            field: evidence[field]
            for field in QUEUE_BARRIER_PRE_ACTIVATION_EVIDENCE
        })
    raise ContractError("schema-v4 activation subject differs")


def expected_attestation_ruleset(policy: dict[str, Any]) -> dict[str, Any]:
    desired = policy["ruleset"]
    return {
        "name": desired["name"],
        "target": desired["target"],
        "enforcement": desired["enforcement"],
        "bypass_actors": copy.deepcopy(desired["bypassActors"]),
        "conditions": {
            "repository_id": {"repository_ids": copy.deepcopy(desired["repositoryIds"])},
            "ref_name": {
                "include": copy.deepcopy(desired["refInclude"]),
                "exclude": copy.deepcopy(desired["refExclude"]),
            },
        },
        "rules": [{"type": rule_type} for rule_type in sorted(desired["rules"])],
    }


def normalize_attestation_ruleset(
    value: Any,
    *,
    actor_bypass: str | None = None,
    include_repository_selector: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ForgeError("attestation ruleset readback is not an object")
    conditions = value.get("conditions")
    expected_condition_keys = (
        {"repository_id", "ref_name"}
        if include_repository_selector
        else {"ref_name"}
    )
    if not isinstance(conditions, dict) or set(conditions) != expected_condition_keys:
        raise ForgeError("attestation ruleset conditions differ")
    repository = conditions.get("repository_id")
    ref = conditions["ref_name"]
    if include_repository_selector and (
        not isinstance(repository, dict) or set(repository) != {"repository_ids"}
    ):
        raise ForgeError("attestation ruleset repository selector differs")
    if not isinstance(ref, dict) or set(ref) != {"include", "exclude"}:
        raise ForgeError("attestation ruleset ref selector differs")
    rules = value.get("rules")
    if not isinstance(rules, list):
        raise ForgeError("attestation ruleset rules are not an array")
    normalized_rules: list[dict[str, Any]] = []
    for index, item in enumerate(rules):
        if not isinstance(item, dict) or set(item) != {"type"} or not isinstance(item["type"], str):
            raise ForgeError(f"attestation ruleset rule {index} differs")
        normalized_rules.append({"type": item["type"]})
    normalized_rules.sort(key=lambda item: item["type"])
    observed_actor_bypass = value.get("current_user_can_bypass")
    actor_bypass = observed_actor_bypass if actor_bypass is None else actor_bypass
    if actor_bypass != "never" or observed_actor_bypass not in {None, actor_bypass}:
        raise ForgeError("attestation ruleset permits or ambiguously reports current-user bypass")
    normalized_conditions = {
        "ref_name": {"include": ref.get("include"), "exclude": ref.get("exclude")},
    }
    if include_repository_selector:
        assert isinstance(repository, dict)
        normalized_conditions["repository_id"] = {
            "repository_ids": repository.get("repository_ids"),
        }
    return {
        "name": value.get("name"),
        "target": value.get("target"),
        "enforcement": value.get("enforcement"),
        "bypass_actors": value.get("bypass_actors"),
        "conditions": normalized_conditions,
        "rules": normalized_rules,
        "current_user_can_bypass": actor_bypass,
    }


def expected_attestation_ruleset_readback(policy: dict[str, Any]) -> dict[str, Any]:
    return {**expected_attestation_ruleset(policy), "current_user_can_bypass": "never"}


def _validated_normalized_ruleset(value: Any, label: str) -> dict[str, Any]:
    """Validate an already-normalized ruleset without widening its surface."""

    try:
        normalized = normalize_ruleset(value)
    except ForgeError as exc:
        raise ContractError(f"{label} is invalid: {exc}") from exc
    if normalized != value:
        raise ContractError(f"{label} is not canonical normalized ruleset state")
    return normalized


def _validate_effective_rules(
    value: Any,
    label: str,
    ruleset_id: int,
    *,
    require_present: bool,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 1:
        raise ContractError(f"{label} must contain exactly one target projection")
    item = _object(value[0], f"{label}[0]")
    _exact_keys(item, {"repositoryId", "rulesetId", "rulesetPresent"}, f"{label}[0]")
    if (
        item.get("repositoryId") != TARGET_REPOSITORY_ID
        or item.get("rulesetId") != ruleset_id
        or not isinstance(item.get("rulesetPresent"), bool)
    ):
        raise ContractError(f"{label} does not prove the exact target/ruleset binding")
    if require_present and item["rulesetPresent"] is not True:
        raise ContractError(f"{label} does not prove active target coverage")
    return value


def _validate_attestation_ruleset_evidence(value: Any, label: str) -> dict[str, Any]:
    evidence = _object(value, label)
    _exact_keys(evidence, {"policy", "rulesetId", "normalized", "stateDigest"}, label)
    _validate_policy_identity(evidence["policy"], f"{label}.policy")
    _positive_integer(evidence["rulesetId"], f"{label}.rulesetId")
    try:
        normalized = normalize_attestation_ruleset(evidence["normalized"])
    except ForgeError as exc:
        raise ContractError(f"{label}.normalized is invalid: {exc}") from exc
    if normalized != evidence["normalized"]:
        raise ContractError(f"{label}.normalized is not canonical")
    if _digest(evidence["stateDigest"], f"{label}.stateDigest") != canonical_digest(normalized):
        raise ContractError(f"{label}.stateDigest differs")
    return evidence


def _validate_report_readback(
    value: Any,
    label: str,
    *,
    ruleset_id: int,
    enforcement: str,
    include_observed_at: bool,
    require_effective_coverage: bool,
) -> dict[str, Any]:
    readback = _object(value, label)
    keys = {
        "rulesetId", "updatedAt", "normalized", "digest",
        "effectiveRules", "effectiveRulesDigest",
    }
    if include_observed_at:
        keys.add("observedAt")
    _exact_keys(readback, keys, label)
    if _positive_integer(readback["rulesetId"], f"{label}.rulesetId") != ruleset_id:
        raise ContractError(f"{label}.rulesetId differs")
    _timestamp(readback["updatedAt"], f"{label}.updatedAt")
    if include_observed_at:
        _timestamp(readback["observedAt"], f"{label}.observedAt")
    normalized = _validated_normalized_ruleset(readback["normalized"], f"{label}.normalized")
    if normalized.get("enforcement") != enforcement:
        raise ContractError(f"{label} enforcement must be {enforcement}")
    if _digest(readback["digest"], f"{label}.digest") != canonical_digest(normalized):
        raise ContractError(f"{label}.digest differs from normalized state")
    effective = _validate_effective_rules(
        readback["effectiveRules"],
        f"{label}.effectiveRules",
        ruleset_id,
        require_present=require_effective_coverage,
    )
    if _digest(readback["effectiveRulesDigest"], f"{label}.effectiveRulesDigest") != canonical_digest(effective):
        raise ContractError(f"{label}.effectiveRulesDigest differs")
    return readback


def _validate_activation_readback(
    value: Any,
    report: dict[str, Any],
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    label = "apply report.activationReadback"
    readback = _object(value, label)
    report_version = report["schemaVersion"]
    coverage_field = (
        "evaluateRuleSuiteEvidence" if report_version == 2 else "effectiveRulesDigest"
    )
    _exact_keys(
        readback,
        {
            "rulesetId", "liveEnforcement", "rulesetUpdatedAt", "canaryNotBefore",
            "activationEvidenceDigest", "workflowEvidence", coverage_field,
        },
        label,
    )
    ruleset_id = report["mutation"]["rulesetId"]
    if readback["rulesetId"] != ruleset_id or readback["liveEnforcement"] != "evaluate":
        raise ContractError("apply report activation readback does not bind evaluate state")
    if readback["rulesetUpdatedAt"] != report["preReadback"]["updatedAt"]:
        raise ContractError("apply report activation/pre updatedAt differs")
    _timestamp(readback["canaryNotBefore"], f"{label}.canaryNotBefore")
    _digest(readback["activationEvidenceDigest"], f"{label}.activationEvidenceDigest")
    if record is not None and readback["activationEvidenceDigest"] != _activation_evidence_digest(record):
        raise ContractError("apply report activation-evidence digest differs")
    if report_version == 1 and readback["effectiveRulesDigest"] != report["preReadback"]["effectiveRulesDigest"]:
        raise ContractError("apply report activation effective-rules digest differs")
    workflows = _object(readback["workflowEvidence"], f"{label}.workflowEvidence")
    _exact_keys(workflows, {"pullRequestCanary", "mergeGroupCanary", "negativeControl"}, f"{label}.workflowEvidence")
    for field, item_value in workflows.items():
        item = _object(item_value, f"{label}.workflowEvidence.{field}")
        _exact_keys(
            item,
            {
                "runId", "runAttempt", "requiredCheckJobId", "ruleSuiteId",
                "runCreatedAt", "runUpdatedAt", "ruleSuitePushedAt", "observationDigest",
            },
            f"{label}.workflowEvidence.{field}",
        )
        for integer_field in ["runId", "runAttempt", "requiredCheckJobId", "ruleSuiteId"]:
            _positive_integer(item[integer_field], f"{label}.workflowEvidence.{field}.{integer_field}")
        for timestamp_field in ["runCreatedAt", "runUpdatedAt", "ruleSuitePushedAt"]:
            _timestamp(item[timestamp_field], f"{label}.workflowEvidence.{field}.{timestamp_field}")
        _digest(item["observationDigest"], f"{label}.workflowEvidence.{field}.observationDigest")
        if record is not None:
            evidence = record["activationEvidence"][field]
            locator = re.fullmatch(
                r"https://github\.com/[^/]+/[^/]+/actions/runs/([1-9][0-9]*)/?",
                evidence["locator"],
            )
            if (
                locator is None
                or item["runId"] != int(locator.group(1))
                or item["ruleSuiteId"] != evidence["bindings"]["ruleSuiteId"]
                or item["ruleSuitePushedAt"] != evidence["observedAt"]
                or item["observationDigest"] != evidence["subjectDigest"]
            ):
                raise ContractError(
                    f"apply report workflow summary {field} differs from historical desired evidence"
                )
            pushed = _timestamp(item["ruleSuitePushedAt"], f"{label}.{field}.ruleSuitePushedAt")
            created = _timestamp(item["runCreatedAt"], f"{label}.{field}.runCreatedAt")
            updated = _timestamp(item["runUpdatedAt"], f"{label}.{field}.runUpdatedAt")
            if not pushed <= created <= updated:
                raise ContractError(f"apply report workflow summary {field} chronology differs")
            pre_updated = _timestamp(
                report["preReadback"]["updatedAt"],
                "apply report.preReadback.updatedAt",
            )
            if any(timestamp <= pre_updated for timestamp in (pushed, created, updated)):
                raise ContractError(
                    f"apply report workflow summary {field} predates evaluate revision"
                )
    if report_version == 2:
        suite = _object(
            readback["evaluateRuleSuiteEvidence"],
            f"{label}.evaluateRuleSuiteEvidence",
        )
        _exact_keys(
            suite,
            {"ruleSuiteId", "ruleSuitePushedAt", "observationDigest"},
            f"{label}.evaluateRuleSuiteEvidence",
        )
        _positive_integer(
            suite["ruleSuiteId"],
            f"{label}.evaluateRuleSuiteEvidence.ruleSuiteId",
        )
        pushed = _timestamp(
            suite["ruleSuitePushedAt"],
            f"{label}.evaluateRuleSuiteEvidence.ruleSuitePushedAt",
        )
        _digest(
            suite["observationDigest"],
            f"{label}.evaluateRuleSuiteEvidence.observationDigest",
        )
        if pushed <= _timestamp(
            report["preReadback"]["updatedAt"],
            "apply report.preReadback.updatedAt",
        ):
            raise ContractError("apply report evaluate rule-suite summary predates evaluate revision")
        if record is not None:
            evidence = record["activationEvidence"]["evaluateRuleSuiteReadback"]
            if (
                suite["ruleSuiteId"] != evidence["bindings"]["ruleSuiteId"]
                or suite["ruleSuitePushedAt"] != evidence["observedAt"]
                or suite["observationDigest"] != evidence["subjectDigest"]
            ):
                raise ContractError(
                    "apply report evaluate rule-suite summary differs from historical desired evidence"
                )
    return readback


def validate_v4_apply_report(
    value: Any,
    record: dict[str, Any] | None = None,
    *,
    expected_schema_version: int = 4,
) -> dict[str, Any]:
    report = _object(value, "schema-v4 apply report")
    _exact_keys(
        report,
        {
            "schemaVersion", "kind", "mode", "observedAt", "status", "findings",
            "executor", "actor", "applyLock", "desiredState", "source", "target",
            "phase", "preReadback", "activationReadback", "plannedMutation",
            "mutation", "postReadback", "attestationRuleset",
            "activationAttestation", "activationSequencingDigest", "subjects",
            "evidenceDigest",
        },
        "schema-v4 apply report",
    )
    if (
        report["schemaVersion"] != expected_schema_version
        or report["kind"] != EXECUTION_REPORT_KIND
        or report["mode"] != "apply"
        or report["status"] not in {
            "APPLIED_PENDING_ATTESTATION",
            "APPLIED_PENDING_EVIDENCE",
        }
    ):
        raise ContractError("schema-v4 apply report identity differs")
    expected_finding = (
        PENDING_ATTESTATION_FINDING
        if report["status"] == "APPLIED_PENDING_ATTESTATION"
        else PENDING_EVIDENCE_FINDING
    )
    if report["findings"] != [expected_finding]:
        raise ContractError("schema-v4 apply report pending finding differs")
    observed_at = _timestamp(report["observedAt"], "schema-v4 apply report observedAt")
    digest = report["evidenceDigest"]
    _digest(digest, "schema-v4 apply report evidenceDigest")
    body = copy.deepcopy(report)
    body.pop("evidenceDigest", None)
    if digest != canonical_digest(body):
        raise ContractError("schema-v4 apply report evidenceDigest differs")

    executor = _object(report["executor"], "schema-v4 executor")
    _exact_keys(
        executor,
        {"repositoryId", "commitSha", "path", "exactBytesDigest"},
        "schema-v4 executor",
    )
    if executor["repositoryId"] != EXECUTOR_REPOSITORY_ID or executor["path"] != EXECUTOR_PATH:
        raise ContractError("schema-v4 executor identity differs")
    _sha(executor["commitSha"], "schema-v4 executor.commitSha")
    _digest(executor["exactBytesDigest"], "schema-v4 executor.exactBytesDigest")
    actor = _validate_actor(report["actor"], "schema-v4 actor", include_type=True)

    desired = _object(report["desiredState"], "schema-v4 desiredState")
    _exact_keys(
        desired,
        {
            "repositoryId", "commitSha", "path", "gitBlobSha",
            "exactBytesDigest", "semanticDigest",
        },
        "schema-v4 desiredState",
    )
    if desired["repositoryId"] != DOCTRINE_REPOSITORY_ID or desired["path"] != DOCTRINE_RECORD_PATH:
        raise ContractError("schema-v4 desired-state identity differs")
    for field in ["commitSha", "gitBlobSha"]:
        _sha(desired[field], f"schema-v4 desiredState.{field}")
    for field in ["exactBytesDigest", "semanticDigest"]:
        _digest(desired[field], f"schema-v4 desiredState.{field}")

    source = _object(report["source"], "schema-v4 source")
    _exact_keys(source, {"repositoryId", "commitSha", "files", "relation"}, "schema-v4 source")
    if (
        source["repositoryId"] != EXECUTOR_REPOSITORY_ID
        or source["relation"] != PROTECTED_SOURCE_RELATION
        or source["commitSha"] != executor["commitSha"]
    ):
        raise ContractError("schema-v4 source authority differs")
    _sha(source["commitSha"], "schema-v4 source.commitSha")
    files = source["files"]
    if not isinstance(files, list) or len(files) != len(V4_SOURCE_PATHS):
        raise ContractError("schema-v4 source file set differs")
    for expected_path, item_value in zip(V4_SOURCE_PATHS, files):
        item = _object(item_value, f"schema-v4 source file {expected_path}")
        _exact_keys(
            item,
            {"path", "gitBlobSha", "exactBytesDigest"},
            f"schema-v4 source file {expected_path}",
        )
        if item["path"] != expected_path:
            raise ContractError("schema-v4 source file order/path differs")
        _sha(item["gitBlobSha"], f"schema-v4 source file {expected_path}.gitBlobSha")
        _digest(
            item["exactBytesDigest"],
            f"schema-v4 source file {expected_path}.exactBytesDigest",
        )

    target = _object(report["target"], "schema-v4 target")
    _exact_keys(target, {"id", "nodeId", "name", "defaultBranch", "visibility"}, "schema-v4 target")
    if (
        target["id"] != TARGET_REPOSITORY_ID
        or target["nodeId"] != TARGET_REPOSITORY_NODE_ID
        or target["name"] not in TARGET_REPOSITORY_NAMES
        or target["defaultBranch"] != TARGET_DEFAULT_BRANCH
        or target["visibility"] not in {"private", "public", "internal"}
    ):
        raise ContractError("schema-v4 target identity differs")

    phase = _object(report["phase"], "schema-v4 phase")
    _exact_keys(phase, {"externalAdmission", "queueBarrier"}, "schema-v4 phase")
    if phase["externalAdmission"] not in PHASE_ENFORCEMENT or phase["queueBarrier"] not in QUEUE_BARRIER_PHASE_ENFORCEMENT:
        raise ContractError("schema-v4 phase differs")
    _digest(
        report["activationSequencingDigest"],
        "schema-v4 activationSequencingDigest",
    )

    planned = _object(report["plannedMutation"], "schema-v4 plannedMutation")
    _exact_keys(
        planned,
        {"subject", "action", "rulesetId", "payload", "payloadDigest"},
        "schema-v4 plannedMutation",
    )
    subject = planned["subject"]
    if subject not in {"externalAdmission", "queueBarrier"}:
        raise ContractError("schema-v4 apply report subject differs")
    ruleset_id = _positive_integer(planned["rulesetId"], "schema-v4 plannedMutation.rulesetId")
    planned_payload = _validated_normalized_ruleset(
        planned["payload"], "schema-v4 plannedMutation.payload"
    )
    if planned["action"] != "update" or planned_payload["enforcement"] != "active":
        raise ContractError("schema-v4 attested report is not one activation update")
    if _digest(planned["payloadDigest"], "schema-v4 plannedMutation.payloadDigest") != canonical_digest(planned_payload):
        raise ContractError("schema-v4 planned payload digest differs")

    mutation = _object(report["mutation"], "schema-v4 mutation")
    _exact_keys(
        mutation,
        {
            "attempted", "subject", "action", "outcome", "rulesetId",
            "requestSentAt", "requestId",
        },
        "schema-v4 mutation",
    )
    if (
        mutation["attempted"] is not True
        or mutation["subject"] != subject
        or mutation["action"] != "update"
        or mutation["outcome"] != "updated"
        or mutation["rulesetId"] != ruleset_id
        or not isinstance(mutation["requestId"], str)
        or AUDIT_REQUEST_ID_RE.fullmatch(mutation["requestId"]) is None
    ):
        raise ContractError("schema-v4 mutation projection differs")
    request_sent_at = _timestamp(
        mutation["requestSentAt"], "schema-v4 mutation.requestSentAt"
    )

    pre = _validate_report_readback(
        report["preReadback"],
        "schema-v4 preReadback",
        ruleset_id=ruleset_id,
        enforcement="evaluate",
        include_observed_at=False,
        require_effective_coverage=False,
    )
    post_value = _object(report["postReadback"], "schema-v4 postReadback")
    _exact_keys(
        post_value,
        {
            "subject", "rulesetId", "updatedAt", "observedAt", "normalized",
            "digest", "effectiveRules", "effectiveRulesDigest",
        },
        "schema-v4 postReadback",
    )
    if post_value["subject"] != subject:
        raise ContractError("schema-v4 post-readback subject differs")
    post = _validate_report_readback(
        {key: copy.deepcopy(value) for key, value in post_value.items() if key != "subject"},
        "schema-v4 postReadback",
        ruleset_id=ruleset_id,
        enforcement="active",
        include_observed_at=True,
        require_effective_coverage=True,
    )
    if post["normalized"] != planned_payload or post["digest"] != planned["payloadDigest"]:
        raise ContractError("schema-v4 pre/post activation readback differs")

    subjects = _object(report["subjects"], "schema-v4 subjects")
    _exact_keys(subjects, {"externalAdmission", "queueBarrier"}, "schema-v4 subjects")
    for item_subject, item_value in subjects.items():
        item = _object(item_value, f"schema-v4 subjects.{item_subject}")
        _exact_keys(
            item,
            {"rulesetId", "preReadback", "effectiveRules", "effectiveRulesDigest"},
            f"schema-v4 subjects.{item_subject}",
        )
        item_ruleset_id = _positive_integer(
            item["rulesetId"], f"schema-v4 subjects.{item_subject}.rulesetId"
        )
        if item["preReadback"] is None:
            raise ContractError("schema-v4 activation subject snapshot is unresolved")
        snapshot = _object(
            item["preReadback"], f"schema-v4 subjects.{item_subject}.preReadback"
        )
        snapshot_enforcement = snapshot.get("normalized", {}).get("enforcement")
        if snapshot_enforcement not in ENFORCEMENT_RANK:
            raise ContractError("schema-v4 subject snapshot enforcement differs")
        _validate_report_readback(
            snapshot,
            f"schema-v4 subjects.{item_subject}.preReadback",
            ruleset_id=item_ruleset_id,
            enforcement=snapshot_enforcement,
            include_observed_at=False,
            require_effective_coverage=snapshot_enforcement == "active",
        )
        effective = _validate_effective_rules(
            item["effectiveRules"],
            f"schema-v4 subjects.{item_subject}.effectiveRules",
            item_ruleset_id,
            require_present=snapshot_enforcement == "active",
        )
        if (
            item["effectiveRules"] != snapshot["effectiveRules"]
            or _digest(
                item["effectiveRulesDigest"],
                f"schema-v4 subjects.{item_subject}.effectiveRulesDigest",
            )
            != canonical_digest(effective)
            or item["effectiveRulesDigest"] != snapshot["effectiveRulesDigest"]
        ):
            raise ContractError("schema-v4 subject effective snapshot differs")
    if subjects[subject]["preReadback"] != pre:
        raise ContractError("schema-v4 selected pre-readback differs from subject snapshot")

    lock = _object(report["applyLock"], "schema-v4 applyLock")
    _exact_keys(
        lock,
        {
            "repositoryId", "ref", "tagObjectSha", "tagMessageDigest",
            "executorCommitSha", "nonce", "actor", "claimedAt", "acquireOutcome",
            "releaseOutcome", "finalRefAbsentAt",
        },
        "schema-v4 applyLock",
    )
    if (
        lock["repositoryId"] != EXECUTOR_REPOSITORY_ID
        or lock["ref"] != APPLY_LOCK_REF
        or lock["executorCommitSha"] != executor["commitSha"]
        or lock["actor"] != actor
        or lock["acquireOutcome"] != "acquired"
        or lock["releaseOutcome"] != "released"
    ):
        raise ContractError("schema-v4 apply lock lifecycle differs")
    _sha(lock["tagObjectSha"], "schema-v4 applyLock.tagObjectSha")
    _digest(lock["tagMessageDigest"], "schema-v4 applyLock.tagMessageDigest")
    if not isinstance(lock["nonce"], str) or NONCE_RE.fullmatch(lock["nonce"]) is None:
        raise ContractError("schema-v4 apply-lock nonce differs")
    claimed_at = _timestamp(lock["claimedAt"], "schema-v4 applyLock.claimedAt")
    absent_at = _timestamp(
        lock["finalRefAbsentAt"], "schema-v4 applyLock.finalRefAbsentAt"
    )
    post_observed_at = _timestamp(post["observedAt"], "schema-v4 postReadback.observedAt")
    if not (claimed_at <= request_sent_at <= post_observed_at <= absent_at):
        raise ContractError("schema-v4 mutation/readback lies outside the lock lifecycle")
    if observed_at > claimed_at:
        raise ContractError("schema-v4 execution observation postdates lock acquisition")

    attestation_ruleset = _validate_attestation_ruleset_evidence(
        report["attestationRuleset"], "schema-v4 attestationRuleset"
    )
    if attestation_ruleset["policy"]["commitSha"] != executor["commitSha"]:
        raise ContractError("schema-v4 attestation policy/executor commits differ")

    activation = _object(report["activationReadback"], "schema-v4 activationReadback")
    if activation.get("subject") != subject:
        raise ContractError("schema-v4 activation-readback subject differs")
    if subject == "externalAdmission":
        _exact_keys(
            activation,
            {
                "subject", "rulesetId", "liveEnforcement", "rulesetUpdatedAt",
                "canaryNotBefore", "activationEvidenceDigest", "workflowEvidence",
                "evaluateRuleSuiteEvidence",
            },
            "schema-v4 external activationReadback",
        )
        legacy_activation = {
            key: copy.deepcopy(value)
            for key, value in activation.items()
            if key != "subject"
        }
        legacy_report = {
            "schemaVersion": 2,
            "mutation": {"rulesetId": ruleset_id},
            "preReadback": pre,
        }
        _validate_activation_readback(
            legacy_activation,
            legacy_report,
            _v4_external_projection(record) if record is not None else None,
        )
    else:
        _exact_keys(
            activation,
            {
                "subject", "rulesetId", "liveEnforcement", "rulesetUpdatedAt",
                "canaryNotBefore", "activationEvidenceDigest", "preEvidence",
            },
            "schema-v4 queueBarrier activationReadback",
        )
        if (
            activation["rulesetId"] != ruleset_id
            or activation["liveEnforcement"] != "evaluate"
            or activation["rulesetUpdatedAt"] != pre["updatedAt"]
            or activation["canaryNotBefore"] != pre["updatedAt"]
        ):
            raise ContractError("schema-v4 queueBarrier activation readback differs")
        _timestamp(
            activation["canaryNotBefore"],
            "schema-v4 queueBarrier activationReadback.canaryNotBefore",
        )
        _digest(
            activation["activationEvidenceDigest"],
            "schema-v4 queueBarrier activationReadback.activationEvidenceDigest",
        )
        pre_evidence = _object(
            activation["preEvidence"],
            "schema-v4 queueBarrier activationReadback.preEvidence",
        )
        _exact_keys(
            pre_evidence,
            set(QUEUE_BARRIER_PRE_ACTIVATION_EVIDENCE),
            "schema-v4 queueBarrier activationReadback.preEvidence",
        )

    if record is not None:
        owner = record if subject == "externalAdmission" else record["queueBarrier"]
        if (
            phase[subject] != "ratchet"
            or owner["ruleset"]["enforcement"] != "active"
            or ruleset_id != owner["ruleset"]["rulesetId"]
            or planned_payload != expected_v4_ruleset(record, subject, enforcement="active")
            or pre["normalized"] != expected_v4_ruleset(record, subject, enforcement="evaluate")
            or desired["semanticDigest"] != canonical_digest(record)
            or source["commitSha"] != record["activationSequencing"]["protectedSourceBundle"]["commitSha"]
            or executor != {
                key: record["activationSequencing"]["executor"][key]
                for key in ["repositoryId", "commitSha", "path", "exactBytesDigest"]
            }
            or report["activationSequencingDigest"]
            != canonical_digest(record["activationSequencing"])
            or activation["activationEvidenceDigest"]
            != _v4_subject_activation_evidence_digest(record, subject)
        ):
            raise ContractError("schema-v4 apply report differs from desired state")
        if subject == "queueBarrier" and activation["preEvidence"] != {
            field: record["queueBarrier"]["activationEvidence"][field]
            for field in QUEUE_BARRIER_PRE_ACTIVATION_EVIDENCE
        }:
            raise ContractError("schema-v4 queueBarrier pre-evidence snapshot differs")
        for item_subject in ["externalAdmission", "queueBarrier"]:
            item_owner = record if item_subject == "externalAdmission" else record["queueBarrier"]
            snapshot = subjects[item_subject]["preReadback"]
            expected_enforcement = "evaluate" if item_subject == subject else item_owner["ruleset"]["enforcement"]
            if (
                subjects[item_subject]["rulesetId"] != item_owner["ruleset"]["rulesetId"]
                or snapshot["normalized"]
                != expected_v4_ruleset(record, item_subject, enforcement=expected_enforcement)
            ):
                raise ContractError("schema-v4 subject snapshot differs from desired state")

    attestation = report["activationAttestation"]
    if report["status"] == "APPLIED_PENDING_EVIDENCE":
        attestation = _validate_attestation_projection(
            attestation, "schema-v4 activationAttestation"
        )
        if (
            attestation["ref"] != f"{ATTESTATION_REF_PREFIX}{lock['nonce']}"
            or attestation["evidenceCutoffAt"] != lock["finalRefAbsentAt"]
            or attestation["policy"] != attestation_ruleset["policy"]
            or attestation["ruleset"] != {
                "rulesetId": attestation_ruleset["rulesetId"],
                "stateDigest": attestation_ruleset["stateDigest"],
            }
        ):
            raise ContractError("schema-v4 activation attestation binding differs")
        if attestation["claimDigest"] != canonical_digest(activation_attestation_claim(report)):
            raise ContractError("schema-v4 activation attestation claim differs")
    elif attestation is not None:
        raise ContractError("pending schema-v4 report cannot preclaim attestation")
    return report


def validate_apply_report(value: Any, record: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("schemaVersion") == 4:
        return validate_v4_apply_report(value, record)
    if isinstance(value, dict) and value.get("schemaVersion") == 5:
        return validate_v4_apply_report(
            value,
            record,
            expected_schema_version=5,
        )
    """Validate the inert, sealed output of the single admitted ratchet apply."""

    report = _object(value, "apply report")
    _exact_keys(
        report,
        {
            "schemaVersion", "kind", "mode", "observedAt", "status", "findings",
            "executor", "actor", "applyLock", "desiredState", "source", "target",
            "phase", "preReadback", "activationReadback", "plannedMutation",
            "mutation", "postReadback", "attestationRuleset", "activationAttestation",
            "evidenceDigest",
        },
        "apply report",
    )
    if report["schemaVersion"] not in {1, 2} or report["kind"] != EXECUTION_REPORT_KIND:
        raise ContractError("apply report identity is unsupported")
    if record is not None and report["schemaVersion"] != _authority_schema_version(record):
        raise ContractError("apply report version differs from historical desired state")
    if report["mode"] != "apply" or report["status"] not in {
        "APPLIED_PENDING_ATTESTATION",
        "APPLIED_PENDING_EVIDENCE",
    }:
        raise ContractError("apply report is not the pending-evidence apply state")
    expected_finding = (
        PENDING_ATTESTATION_FINDING
        if report["status"] == "APPLIED_PENDING_ATTESTATION"
        else PENDING_EVIDENCE_FINDING
    )
    if report["findings"] != [expected_finding] or report["phase"] != "ratchet":
        raise ContractError("apply report does not represent the one ratchet transition")
    _timestamp(report["observedAt"], "apply report.observedAt")
    supplied_digest = _digest(report["evidenceDigest"], "apply report.evidenceDigest")
    digest_subject = copy.deepcopy(report)
    digest_subject.pop("evidenceDigest")
    if supplied_digest != canonical_digest(digest_subject):
        raise ContractError("apply report evidenceDigest differs")

    executor = _object(report["executor"], "apply report.executor")
    _exact_keys(executor, {"repositoryId", "commitSha", "path", "exactBytesDigest"}, "apply report.executor")
    if executor["repositoryId"] != EXECUTOR_REPOSITORY_ID or executor["path"] != EXECUTOR_PATH:
        raise ContractError("apply report executor identity differs")
    _sha(executor["commitSha"], "apply report.executor.commitSha")
    _digest(executor["exactBytesDigest"], "apply report.executor.exactBytesDigest")
    actor = _validate_actor(report["actor"], "apply report.actor", include_type=True)

    desired = _object(report["desiredState"], "apply report.desiredState")
    _exact_keys(desired, {"repositoryId", "commitSha", "path", "gitBlobSha", "exactBytesDigest", "semanticDigest"}, "apply report.desiredState")
    if desired["repositoryId"] != DOCTRINE_REPOSITORY_ID or desired["path"] != DOCTRINE_RECORD_PATH:
        raise ContractError("apply report desired-state identity differs")
    for field in ["commitSha", "gitBlobSha"]:
        _sha(desired[field], f"apply report.desiredState.{field}")
    for field in ["exactBytesDigest", "semanticDigest"]:
        _digest(desired[field], f"apply report.desiredState.{field}")

    lock = _object(report["applyLock"], "apply report.applyLock")
    _exact_keys(
        lock,
        {
            "repositoryId", "ref", "tagObjectSha", "tagMessageDigest", "executorCommitSha",
            "nonce", "actor", "claimedAt", "acquireOutcome", "releaseOutcome", "finalRefAbsentAt",
        },
        "apply report.applyLock",
    )
    if lock["repositoryId"] != EXECUTOR_REPOSITORY_ID or lock["ref"] != APPLY_LOCK_REF:
        raise ContractError("apply report apply-lock identity differs")
    _sha(lock["tagObjectSha"], "apply report.applyLock.tagObjectSha")
    _digest(lock["tagMessageDigest"], "apply report.applyLock.tagMessageDigest")
    if lock["executorCommitSha"] != executor["commitSha"]:
        raise ContractError("apply report lock/executor commits differ")
    if not isinstance(lock["nonce"], str) or NONCE_RE.fullmatch(lock["nonce"]) is None:
        raise ContractError("apply report lock nonce is invalid")
    if _validate_actor(lock["actor"], "apply report.applyLock.actor", include_type=True) != actor:
        raise ContractError("apply report lock/actor differs")
    claimed_at = _timestamp(lock["claimedAt"], "apply report.applyLock.claimedAt")
    absent_at = _timestamp(lock["finalRefAbsentAt"], "apply report.applyLock.finalRefAbsentAt")
    if lock["acquireOutcome"] != "acquired" or lock["releaseOutcome"] != "released" or absent_at < claimed_at:
        raise ContractError("apply report lacks a complete lock lifecycle")

    attestation_ruleset = _validate_attestation_ruleset_evidence(
        report["attestationRuleset"],
        "apply report.attestationRuleset",
    )
    if attestation_ruleset["policy"]["commitSha"] != executor["commitSha"]:
        raise ContractError("apply report attestation policy/executor commits differ")
    activation_attestation = report["activationAttestation"]
    if report["status"] == "APPLIED_PENDING_ATTESTATION":
        if activation_attestation is not None:
            raise ContractError("pending-attestation report already contains an attestation")
    else:
        activation_attestation = _validate_attestation_projection(
            activation_attestation,
            "apply report.activationAttestation",
        )
        if activation_attestation["ref"] != f"{ATTESTATION_REF_PREFIX}{lock['nonce']}":
            raise ContractError("apply report attestation/lock nonce differs")
        if activation_attestation["evidenceCutoffAt"] != lock["finalRefAbsentAt"]:
            raise ContractError("apply report attestation does not bind final lock absence")
        if activation_attestation["policy"] != attestation_ruleset["policy"]:
            raise ContractError("apply report attestation policy differs from ruleset evidence")
        if activation_attestation["ruleset"] != {
            "rulesetId": attestation_ruleset["rulesetId"],
            "stateDigest": attestation_ruleset["stateDigest"],
        }:
            raise ContractError("apply report attestation ruleset binding differs")

    source = _object(report["source"], "apply report.source")
    _exact_keys(source, {"repositoryId", "commitSha", "files"}, "apply report.source")
    if source["repositoryId"] != EXECUTOR_REPOSITORY_ID:
        raise ContractError("apply report workflow source repository differs")
    _sha(source["commitSha"], "apply report.source.commitSha")
    files = source["files"]
    if not isinstance(files, list) or len(files) != len(SOURCE_PATHS):
        raise ContractError("apply report workflow source file set differs")
    for expected_path, item_value in zip(SOURCE_PATHS, files):
        item = _object(item_value, f"apply report.source.files[{expected_path}]")
        _exact_keys(item, {"path", "gitBlobSha", "exactBytesDigest"}, f"apply report.source.files[{expected_path}]")
        if item["path"] != expected_path:
            raise ContractError("apply report workflow source file order/path differs")
        _sha(item["gitBlobSha"], f"apply report.source.files[{expected_path}].gitBlobSha")
        _digest(item["exactBytesDigest"], f"apply report.source.files[{expected_path}].exactBytesDigest")

    target = _object(report["target"], "apply report.target")
    _exact_keys(target, {"id", "nodeId", "name", "defaultBranch", "visibility"}, "apply report.target")
    if (
        target["id"] != TARGET_REPOSITORY_ID
        or target["nodeId"] != TARGET_REPOSITORY_NODE_ID
        or target["name"] not in TARGET_REPOSITORY_NAMES
        or target["defaultBranch"] != TARGET_DEFAULT_BRANCH
        or target["visibility"] not in {"private", "public", "internal"}
    ):
        raise ContractError("apply report target identity differs")

    mutation = _object(report["mutation"], "apply report.mutation")
    _exact_keys(mutation, {"attempted", "action", "outcome", "rulesetId", "requestSentAt", "requestId"}, "apply report.mutation")
    if mutation["attempted"] is not True or mutation["action"] != "update" or mutation["outcome"] != "updated":
        raise ContractError("apply report mutation is not the one admitted update")
    request_sent_at = _timestamp(mutation["requestSentAt"], "apply report.mutation.requestSentAt")
    request_id = _string(mutation["requestId"], "apply report.mutation.requestId")
    if AUDIT_REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ContractError("apply report mutation lacks a provider request ID")
    if not (claimed_at <= request_sent_at <= absent_at):
        raise ContractError("apply report mutation lies outside the lock lifecycle")

    ruleset_id = _positive_integer(mutation["rulesetId"], "apply report mutation ruleset ID")
    pre = _validate_report_readback(
        report["preReadback"],
        "apply report.preReadback",
        ruleset_id=ruleset_id,
        enforcement="evaluate",
        include_observed_at=False,
        require_effective_coverage=report["schemaVersion"] == 1,
    )
    post = _validate_report_readback(
        report["postReadback"],
        "apply report.postReadback",
        ruleset_id=ruleset_id,
        enforcement="active",
        include_observed_at=True,
        require_effective_coverage=True,
    )
    planned = _object(report["plannedMutation"], "apply report.plannedMutation")
    _exact_keys(planned, {"action", "rulesetId", "payload", "payloadDigest"}, "apply report.plannedMutation")
    planned_payload = _validated_normalized_ruleset(planned["payload"], "apply report.plannedMutation.payload")
    if (
        planned["action"] != "update"
        or planned["rulesetId"] != ruleset_id
        or planned_payload["enforcement"] != "active"
        or _digest(planned["payloadDigest"], "apply report.plannedMutation.payloadDigest") != canonical_digest(planned_payload)
    ):
        raise ContractError("apply report mutation plan is invalid")
    _validate_activation_readback(report["activationReadback"], report, record)
    if record is None:
        return report
    if record["migration"]["phase"] != "ratchet" or record["ruleset"]["enforcement"] != "active":
        raise ContractError("apply report historical desired state is not ratchet/active")
    historical_ruleset_id = _positive_integer(record["ruleset"]["rulesetId"], "historical ruleset ID")
    if historical_ruleset_id != ruleset_id:
        raise ContractError("apply report historical ruleset ID differs")
    if mutation["rulesetId"] != ruleset_id:
        raise ContractError("apply report mutation ruleset ID differs")
    if desired["semanticDigest"] != canonical_digest(record):
        raise ContractError("apply report historical desired-state semantic digest differs")
    if source["commitSha"] != record["workflowSource"]["commitSha"]:
        raise ContractError("apply report workflow source commit differs from desired state")
    expected_pre = expected_ruleset(record, enforcement="evaluate")
    expected_post = expected_ruleset(record, enforcement="active")
    if pre["normalized"] != expected_pre or post["normalized"] != expected_post:
        raise ContractError("apply report pre/post state differs from desired transition")
    if post["digest"] == pre["digest"]:
        raise ContractError("apply report pre/post state digests do not prove an enforcement transition")
    if _timestamp(post["observedAt"], "apply report.postReadback.observedAt") > absent_at:
        raise ContractError("apply report post readback occurred after final lock absence")
    if planned["action"] != "update" or planned["rulesetId"] != ruleset_id or planned["payload"] != expected_post:
        raise ContractError("apply report mutation plan differs")
    if planned["payloadDigest"] != canonical_digest(expected_post):
        raise ContractError("apply report planned payload digest differs")
    return report


def read_sealed_report(path: Path) -> dict[str, Any]:
    """Read one caller-owned regular file without following the final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContractError("apply report path cannot be opened as an inert local file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ContractError("apply report must be a caller-owned regular file")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ContractError("apply report must not be group/world writable")
        if metadata.st_size < 2 or metadata.st_size > MAX_RECORD_BYTES:
            raise ContractError("apply report size is outside the fixed bound")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise ContractError("apply report changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ContractError("apply report grew while being read")
    finally:
        os.close(descriptor)
    return validate_apply_report(strict_json_loads(b"".join(chunks), label="apply report"))


def _audit_provider_projection(value: Any) -> dict[str, Any]:
    """Project the provider audit object immediately; never persist its raw form."""

    if not isinstance(value, dict) or not AUDIT_PROVIDER_KEYS.issubset(value):
        raise ForgeError("audit event lacks the fixed provider projection")
    projection = {key: value[key] for key in sorted(AUDIT_PROVIDER_KEYS)}
    for field in ["_document_id", "action", "actor", "operation_type", "org", "request_id", "ruleset_enforcement", "ruleset_name", "ruleset_source_type"]:
        if not isinstance(projection[field], str) or not projection[field]:
            raise ForgeError("audit event provider projection contains an invalid string")
    for field in ["actor_id", "created_at", "org_id", "ruleset_id"]:
        if isinstance(projection[field], bool) or not isinstance(projection[field], int) or projection[field] < 1:
            raise ForgeError("audit event provider projection contains an invalid integer")
    return projection


def _normalized_audit_projection(projection: dict[str, Any]) -> dict[str, Any]:
    if projection["ruleset_enforcement"] != "enabled":
        raise ForgeError("audit event does not prove active enforcement")
    return {
        "documentId": projection["_document_id"],
        "action": projection["action"],
        "actor": {"id": projection["actor_id"], "login": projection["actor"]},
        "organization": {"id": projection["org_id"], "login": projection["org"]},
        "createdAtEpochMs": projection["created_at"],
        "operationType": projection["operation_type"],
        "requestId": projection["request_id"],
        "ruleset": {
            "id": projection["ruleset_id"],
            "name": projection["ruleset_name"],
            "sourceType": projection["ruleset_source_type"],
            "enforcement": "active",
        },
    }


def _validate_audit_event(value: Any) -> dict[str, Any]:
    event = _object(value, "activation artifact.auditEvent")
    _exact_keys(event, {"providerProjection", "providerProjectionDigest", "normalized", "normalizedDigest"}, "activation artifact.auditEvent")
    projection = _object(event["providerProjection"], "activation artifact.auditEvent.providerProjection")
    _exact_keys(projection, AUDIT_PROVIDER_KEYS, "activation artifact.auditEvent.providerProjection")
    try:
        expected_normalized = _normalized_audit_projection(_audit_provider_projection(projection))
    except ForgeError as exc:
        raise ContractError(str(exc)) from exc
    if _digest(event["providerProjectionDigest"], "activation artifact.auditEvent.providerProjectionDigest") != canonical_digest(projection):
        raise ContractError("activation artifact provider-projection digest differs")
    normalized = _object(event["normalized"], "activation artifact.auditEvent.normalized")
    if normalized != expected_normalized:
        raise ContractError("activation artifact normalized audit projection differs")
    if _digest(event["normalizedDigest"], "activation artifact.auditEvent.normalizedDigest") != canonical_digest(normalized):
        raise ContractError("activation artifact normalized audit digest differs")
    return event


def _validate_live_capture(value: Any, ruleset_id: int) -> dict[str, Any]:
    capture = _object(value, "activation artifact.liveCapture")
    _exact_keys(
        capture,
        {
            "capturedAt", "rulesetId", "updatedAt", "normalized", "stateDigest",
            "effectiveRules", "effectiveRulesDigest",
        },
        "activation artifact.liveCapture",
    )
    _timestamp(capture["capturedAt"], "activation artifact.liveCapture.capturedAt")
    if capture["rulesetId"] != ruleset_id:
        raise ContractError("activation artifact live ruleset ID differs")
    _timestamp(capture["updatedAt"], "activation artifact.liveCapture.updatedAt")
    normalized = _validated_normalized_ruleset(capture["normalized"], "activation artifact.liveCapture.normalized")
    if normalized["enforcement"] != "active":
        raise ContractError("activation artifact live capture is not active")
    if _digest(capture["stateDigest"], "activation artifact.liveCapture.stateDigest") != canonical_digest(normalized):
        raise ContractError("activation artifact live state digest differs")
    effective = _validate_effective_rules(
        capture["effectiveRules"],
        "activation artifact.liveCapture.effectiveRules",
        ruleset_id,
        require_present=True,
    )
    if _digest(capture["effectiveRulesDigest"], "activation artifact.liveCapture.effectiveRulesDigest") != canonical_digest(effective):
        raise ContractError("activation artifact live effective-rules digest differs")
    return capture


def seal_activation_artifact(
    apply_report: dict[str, Any],
    audit_event: dict[str, Any],
    live_capture: dict[str, Any],
    captured_at: str,
) -> dict[str, Any]:
    report_version = _positive_integer(
        apply_report.get("schemaVersion"),
        "activation artifact apply-report version",
    )
    if report_version not in {1, 2, 4, 5}:
        raise ContractError("activation artifact apply-report version is unsupported")
    body = {
        "schemaVersion": report_version,
        "kind": ACTIVATION_REPORT_KIND,
        "capturedAt": captured_at,
        "applyReport": copy.deepcopy(apply_report),
        "auditEvent": copy.deepcopy(audit_event),
        "liveCapture": copy.deepcopy(live_capture),
    }
    artifact = {**body, "bodyDigest": canonical_digest(body)}
    artifact["evidenceDigest"] = canonical_digest(artifact)
    return artifact


def validate_activation_artifact(
    value: Any,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact = _object(value, "activation artifact")
    _exact_keys(
        artifact,
        {
            "schemaVersion", "kind", "capturedAt", "applyReport", "auditEvent",
            "liveCapture", "bodyDigest", "evidenceDigest",
        },
        "activation artifact",
    )
    if artifact["schemaVersion"] not in {1, 2, 4, 5} or artifact["kind"] != ACTIVATION_REPORT_KIND:
        raise ContractError("activation artifact identity is unsupported")
    captured_at = _timestamp(artifact["capturedAt"], "activation artifact.capturedAt")
    body = {field: artifact[field] for field in ["schemaVersion", "kind", "capturedAt", "applyReport", "auditEvent", "liveCapture"]}
    if _digest(artifact["bodyDigest"], "activation artifact.bodyDigest") != canonical_digest(body):
        raise ContractError("activation artifact bodyDigest differs")
    evidence_subject = copy.deepcopy(artifact)
    supplied_evidence_digest = _digest(evidence_subject.pop("evidenceDigest"), "activation artifact.evidenceDigest")
    if supplied_evidence_digest != canonical_digest(evidence_subject):
        raise ContractError("activation artifact evidenceDigest differs")
    report = validate_apply_report(artifact["applyReport"], record)
    if report["schemaVersion"] != artifact["schemaVersion"]:
        raise ContractError("activation artifact/report versions differ")
    if report["status"] != "APPLIED_PENDING_EVIDENCE":
        raise ContractError("activation artifact apply report lacks immutable attestation")
    if _timestamp(
        report["activationAttestation"]["evidenceCutoffAt"],
        "activation artifact evidence cutoff",
    ) > captured_at:
        raise ContractError("activation artifact evidence cutoff postdates capture")
    ruleset_id = _positive_integer(report["mutation"]["rulesetId"], "activation artifact ruleset ID")
    audit = _validate_audit_event(artifact["auditEvent"])
    live = _validate_live_capture(artifact["liveCapture"], ruleset_id)
    if artifact["capturedAt"] != live["capturedAt"]:
        raise ContractError("activation artifact capture timestamps differ")
    normalized_audit = audit["normalized"]
    subject = (
        report["plannedMutation"]["subject"]
        if report["schemaVersion"] in {4, 5}
        else "externalAdmission"
    )
    expected_ruleset_name = (
        BARRIER_RULESET_NAME if subject == "queueBarrier" else RULESET_NAME
    )
    if (
        normalized_audit["requestId"] != report["mutation"]["requestId"]
        or normalized_audit["actor"] != {"id": report["actor"]["id"], "login": report["actor"]["login"]}
        or normalized_audit["organization"] != {"id": ORGANIZATION_ID, "login": ORGANIZATION}
        or normalized_audit["ruleset"] != {
            "id": ruleset_id,
            "name": expected_ruleset_name,
            "sourceType": "Organization",
            "enforcement": "active",
        }
    ):
        raise ContractError("activation artifact audit/report binding differs")
    if datetime.fromtimestamp(normalized_audit["createdAtEpochMs"] / 1000, tz=timezone.utc) > captured_at:
        raise ContractError("activation artifact audit event postdates capture")
    if live["stateDigest"] != report["postReadback"]["digest"] or live["effectiveRulesDigest"] != report["postReadback"]["effectiveRulesDigest"]:
        raise ContractError("activation artifact live/post-apply state differs")
    return artifact


def _v4_queue_effective_readback(
    report: dict[str, Any],
    live_capture: dict[str, Any],
) -> dict[str, Any]:
    item = {
        "kind": QUEUE_BARRIER_EVIDENCE_KINDS["effectiveRulesReadback"],
        "locator": f"https://github.com/{report['target']['name']}/settings/rules",
        "observedAt": live_capture["capturedAt"],
        "subjectDigest": live_capture["effectiveRulesDigest"],
        "bindings": {
            "barrierRulesetId": report["mutation"]["rulesetId"],
            "guardedRulesetId": report["subjects"]["externalAdmission"][
                "rulesetId"
            ],
            "targetRepositoryId": TARGET_REPOSITORY_ID,
            "sourceRepositoryId": EXECUTOR_REPOSITORY_ID,
            "sourceCommitSha": report["source"]["commitSha"],
            "headSha": None,
            "ruleSuiteId": None,
            "runId": None,
            "checkRunId": None,
            "pullRequestNumber": None,
        },
        "providerVerdicts": None,
        "report": None,
        "queueOutcome": None,
        "failureProof": None,
    }
    return item


def activation_transition_from_artifact(
    artifact: dict[str, Any],
    *,
    artifact_raw: bytes,
    artifact_blob_sha: str,
    historical_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = artifact["applyReport"]
    audit = artifact["auditEvent"]
    pre = report["preReadback"]
    post = report["postReadback"]
    lock = report["applyLock"]
    is_v4 = artifact["schemaVersion"] in {4, 5}
    subject = (
        report["plannedMutation"]["subject"] if is_v4 else "externalAdmission"
    )
    if is_v4 and historical_record is None:
        raise ContractError(
            "schema-v4 activation transition requires historical desired authority"
        )
    if is_v4:
        assert historical_record is not None
        activation_evidence_digest = _v4_subject_activation_evidence_digest(
            historical_record, subject
        )
    else:
        activation_evidence_digest = report["activationReadback"][
            "activationEvidenceDigest"
        ]
    transition: dict[str, Any] = {
        "kind": (
            "queue-barrier-ruleset-activation-transition"
            if subject == "queueBarrier"
            else TRANSITION_KIND
        ),
        "schemaVersion": 1 if subject == "queueBarrier" else (2 if is_v4 else artifact["schemaVersion"]),
        "authorization": {
            "desiredState": copy.deepcopy(report["desiredState"]),
            "executor": copy.deepcopy(report["executor"]),
            "desiredPayloadDigest": report["plannedMutation"]["payloadDigest"],
            "activationEvidenceDigest": activation_evidence_digest,
        },
        "pre": {
            "rulesetId": pre["rulesetId"],
            "enforcement": "evaluate",
            "updatedAt": pre["updatedAt"],
            "stateDigest": pre["digest"],
            "effectiveRulesDigest": pre["effectiveRulesDigest"],
        },
        "mutation": {
            "action": report["mutation"]["action"],
            "outcome": report["mutation"]["outcome"],
            "actor": copy.deepcopy(report["actor"]),
            "requestId": report["mutation"]["requestId"],
            "applyLock": {
                "repositoryId": lock["repositoryId"],
                "ref": lock["ref"],
                "tagObjectSha": lock["tagObjectSha"],
                "tagMessageDigest": lock["tagMessageDigest"],
                "executorCommitSha": lock["executorCommitSha"],
                "nonce": lock["nonce"],
                "claimedAt": lock["claimedAt"],
                "actor": copy.deepcopy(lock["actor"]),
                "acquireOutcome": lock["acquireOutcome"],
                "releaseOutcome": lock["releaseOutcome"],
                "finalRefAbsent": True,
            },
            "activationAttestation": copy.deepcopy(report["activationAttestation"]),
        },
        "post": {
            "rulesetId": post["rulesetId"],
            "enforcement": "active",
            "updatedAt": post["updatedAt"],
            "stateDigest": post["digest"],
            "effectiveRulesDigest": post["effectiveRulesDigest"],
        },
        "audit": {
            **copy.deepcopy(audit["normalized"]),
            "providerProjectionDigest": audit["providerProjectionDigest"],
            "normalizedDigest": audit["normalizedDigest"],
        },
        "executorReport": {
            "path": (
                QUEUE_BARRIER_ACTIVATION_EVIDENCE_PATH
                if subject == "queueBarrier"
                else ACTIVATION_EVIDENCE_PATH
            ),
            "gitBlobSha": artifact_blob_sha,
            "exactBytesDigest": exact_digest(artifact_raw),
            "bodyDigest": artifact["bodyDigest"],
            "evidenceDigest": artifact["evidenceDigest"],
        },
        "capturedAt": artifact["capturedAt"],
    }
    if subject == "queueBarrier":
        assert historical_record is not None
        transition["mutation"]["subjectRuleset"] = {
            "rulesetId": report["mutation"]["rulesetId"],
            "name": BARRIER_RULESET_NAME,
            "sourceCommitSha": historical_record["queueBarrier"]["workflowSource"][
                "commitSha"
            ],
        }
        transition["effectiveRulesReadback"] = _v4_queue_effective_readback(
            report,
            artifact["liveCapture"],
        )
    return transition


def apply_lock_authorization_from_report(report: dict[str, Any]) -> dict[str, Any]:
    planned = _object(report.get("plannedMutation"), "apply preflight plannedMutation")
    if report.get("schemaVersion") in {4, 5}:
        subject = planned.get("subject")
        if subject not in {"externalAdmission", "queueBarrier"}:
            raise ContractError("schema-v4 apply preflight lacks one exact subject")
        subject_report = _object(
            _object(report.get("subjects"), "schema-v4 subjects").get(subject),
            f"schema-v4 subject {subject}",
        )
        pre = subject_report.get("preReadback")
        return {
            "desiredState": copy.deepcopy(report.get("desiredState")),
            "subject": subject,
            "desiredPayloadDigest": planned.get("payloadDigest"),
            "plannedAction": planned.get("action"),
            "preReadback": copy.deepcopy(pre),
            "subjectsDigest": canonical_digest(report.get("subjects")),
            "activationReadbackDigest": canonical_digest(
                report.get("activationReadback")
            ),
            "activationSequencingDigest": report.get("activationSequencingDigest"),
            "attestationRuleset": copy.deepcopy(report.get("attestationRuleset")),
        }
    pre = report.get("preReadback")
    pre_revision = None
    if pre is not None:
        pre_object = _object(pre, "apply preflight preReadback")
        pre_revision = {
            "rulesetId": pre_object.get("rulesetId"),
            "updatedAt": pre_object.get("updatedAt"),
            "stateDigest": pre_object.get("digest"),
            "effectiveRulesDigest": pre_object.get("effectiveRulesDigest"),
        }
    return {
        "desiredState": copy.deepcopy(report.get("desiredState")),
        "desiredPayloadDigest": planned.get("payloadDigest"),
        "plannedAction": planned.get("action"),
        "preReadback": pre_revision,
        "attestationRuleset": copy.deepcopy(report.get("attestationRuleset")),
    }


def activation_attestation_claim(report: dict[str, Any]) -> dict[str, Any]:
    lock = report["applyLock"]
    pre = report["preReadback"]
    post = report["postReadback"]
    ruleset = report["attestationRuleset"]
    lock_claim = {
        "schemaVersion": report["schemaVersion"],
        "kind": "public-skills-ruleset-apply-lock",
        "repositoryId": EXECUTOR_REPOSITORY_ID,
        "ref": APPLY_LOCK_REF,
        "executorCommitSha": report["executor"]["commitSha"],
        "actor": copy.deepcopy(report["actor"]),
        "nonce": lock["nonce"],
        "claimedAt": lock["claimedAt"],
        "authorization": apply_lock_authorization_from_report(report),
    }
    claim = {
        "schemaVersion": report["schemaVersion"],
        "kind": ATTESTATION_KIND,
        "repositoryId": EXECUTOR_REPOSITORY_ID,
        "ref": _attestation_ref(lock["nonce"]),
        "executor": copy.deepcopy(report["executor"]),
        "actor": copy.deepcopy(report["actor"]),
        "applyLock": {
            "claim": lock_claim,
            "tagObjectSha": lock["tagObjectSha"],
            "releaseOutcome": lock["releaseOutcome"],
            "finalRefAbsent": True,
            "finalRefAbsentAt": lock["finalRefAbsentAt"],
        },
        "desiredState": copy.deepcopy(report["desiredState"]),
        "desiredPayloadDigest": report["plannedMutation"]["payloadDigest"],
        "rulesetId": report["mutation"]["rulesetId"],
        "pre": {
            "updatedAt": pre["updatedAt"],
            "stateDigest": pre["digest"],
            "effectiveRulesDigest": pre["effectiveRulesDigest"],
        },
        "post": {
            "updatedAt": post["updatedAt"],
            "stateDigest": post["digest"],
            "effectiveRulesDigest": post["effectiveRulesDigest"],
        },
        "mutation": {
            "action": report["mutation"]["action"],
            "outcome": report["mutation"]["outcome"],
            "requestId": report["mutation"]["requestId"],
        },
        "attestationRuleset": {
            "policy": copy.deepcopy(ruleset["policy"]),
            "rulesetId": ruleset["rulesetId"],
            "stateDigest": ruleset["stateDigest"],
        },
        "evidenceCutoffAt": lock["finalRefAbsentAt"],
    }
    if report.get("schemaVersion") in {4, 5}:
        claim["subject"] = report["plannedMutation"]["subject"]
        claim["mutation"]["subject"] = report["mutation"]["subject"]
        claim["activationSequencingDigest"] = report["activationSequencingDigest"]
    return claim


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
        "createdAt": run.get("created_at"), "updatedAt": run.get("updated_at"),
        "requiredCheck": {
            "id": job.get("id"), "name": job.get("name"), "headSha": job.get("head_sha"),
            "status": job.get("status"), "conclusion": job.get("conclusion"),
        },
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
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.api = api
        self.local_executor_bytes = local_executor_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))
        self.sleeper = sleeper or time.sleep
        self.executor_head: str | None = None
        self.doctrine_head: str | None = None
        self.attestation_policy: dict[str, Any] | None = None
        self.attestation_policy_evidence: dict[str, Any] | None = None

    def _attestation_policy_at(
        self,
        commit_sha: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        item = self.api.get(
            _content_endpoint(EXECUTOR_REPOSITORY_ID, ATTESTATION_POLICY_PATH, commit_sha)
        )
        raw = _decode_content(item, ATTESTATION_POLICY_PATH, "attestation-ruleset policy")
        try:
            policy = validate_attestation_policy(
                strict_json_loads(raw, label="attestation-ruleset policy")
            )
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        metadata = _require_api_object(item, "attestation-ruleset policy")
        evidence = {
            "repositoryId": EXECUTOR_REPOSITORY_ID,
            "commitSha": commit_sha,
            "path": ATTESTATION_POLICY_PATH,
            "gitBlobSha": metadata.get("sha"),
            "exactBytesDigest": exact_digest(raw),
            "semanticDigest": canonical_digest(policy),
        }
        try:
            _validate_policy_identity(evidence, "attestation policy evidence")
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        return policy, evidence

    def _verify_executor(self) -> dict[str, Any]:
        repository = _repository(
            self.api,
            EXECUTOR_REPOSITORY_ID,
            EXECUTOR_REPOSITORY,
            EXECUTOR_BRANCH,
            "executor repository",
        )
        if repository.get("node_id") != EXECUTOR_REPOSITORY_NODE_ID:
            raise ForgeError("executor repository node identity differs")
        head = _head(self.api, EXECUTOR_REPOSITORY_ID, EXECUTOR_BRANCH, "executor default branch")
        remote = _decode_content(
            self.api.get(_content_endpoint(EXECUTOR_REPOSITORY_ID, EXECUTOR_PATH, head)),
            EXECUTOR_PATH,
            "executor source",
        )
        if remote != self.local_executor_bytes:
            raise ForgeError("local executor bytes differ from protected executor main")
        policy, policy_evidence = self._attestation_policy_at(head)
        self.executor_head = head
        self.attestation_policy = policy
        self.attestation_policy_evidence = policy_evidence
        return {"repositoryId": EXECUTOR_REPOSITORY_ID, "commitSha": head, "path": EXECUTOR_PATH, "exactBytesDigest": exact_digest(remote)}

    def _load_doctrine(self) -> tuple[dict[str, Any], dict[str, Any]]:
        _repository(self.api, DOCTRINE_REPOSITORY_ID, DOCTRINE_REPOSITORY, DOCTRINE_BRANCH, "Doctrine repository")
        head = _head(self.api, DOCTRINE_REPOSITORY_ID, DOCTRINE_BRANCH, "Doctrine default branch")
        record, metadata = self._load_doctrine_at(head)
        self.doctrine_head = head
        return record, metadata

    def _load_doctrine_at(self, commit_sha: str) -> tuple[dict[str, Any], dict[str, Any]]:
        _sha(commit_sha, "Doctrine commit SHA")
        item = self.api.get(_content_endpoint(DOCTRINE_REPOSITORY_ID, DOCTRINE_RECORD_PATH, commit_sha))
        raw = _decode_content(item, DOCTRINE_RECORD_PATH, "Doctrine desired-state record")
        parsed = strict_json_loads(raw, label="Doctrine desired-state record")
        if isinstance(parsed, dict) and parsed.get("schemaVersion") in {4, 5}:
            if self.executor_head is None:
                raise ForgeError("schema-v4/v5 execution requires the protected executor runtime SHA")
            try:
                record = (
                    validate_v5_record(
                        parsed,
                        self.executor_head,
                        exact_digest(self.local_executor_bytes),
                    )
                    if parsed["schemaVersion"] == 5
                    else validate_v4_record(
                        parsed,
                        self.executor_head,
                        exact_digest(self.local_executor_bytes),
                    )
                )
            except ContractError as exc:
                raise ForgeError(str(exc)) from exc
        else:
            record = validate_record(parsed)
        metadata = _require_api_object(item, "Doctrine desired-state record")
        return record, {
            "repositoryId": DOCTRINE_REPOSITORY_ID, "commitSha": commit_sha, "path": DOCTRINE_RECORD_PATH,
            "gitBlobSha": metadata.get("sha"), "exactBytesDigest": exact_digest(raw),
            "semanticDigest": canonical_digest(record),
        }

    def _verify_source(self, record: dict[str, Any]) -> dict[str, Any]:
        is_shared_source = record.get("schemaVersion") in {4, 5}
        source_sha = (
            record["queueBarrier"]["workflowSource"]["commitSha"]
            if is_shared_source
            else record["workflowSource"]["commitSha"]
        )
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
        source_identities = (
            LEGACY_SOURCE_IDENTITIES
            if source_sha == LEGACY_SOURCE_COMMIT_SHA
            else SOURCE_IDENTITIES
        )
        paths = V4_SOURCE_PATHS if is_shared_source else SOURCE_PATHS
        for path in paths:
            item = self.api.get(_content_endpoint(EXECUTOR_REPOSITORY_ID, path, source_sha))
            raw = _decode_content(item, path, f"workflow source {path}")
            metadata = _require_api_object(item, f"workflow source {path}")
            evidence = {
                "path": path,
                "gitBlobSha": metadata.get("sha"),
                "exactBytesDigest": exact_digest(raw),
            }
            if path == EXECUTOR_PATH:
                if raw != self.local_executor_bytes or source_sha != self.executor_head:
                    raise ForgeError("shared executor bytes differ from the protected runtime source")
            else:
                expected_identity = (
                    V4_ADDITIONAL_SOURCE_IDENTITIES[path]
                    if path in V4_ADDITIONAL_SOURCE_IDENTITIES
                    else source_identities[path]
                )
                if evidence != {"path": path, **expected_identity}:
                    raise ForgeError(f"workflow source {path} differs from the executor-owned exact identity")
            files.append(evidence)
        result = {
            "repositoryId": EXECUTOR_REPOSITORY_ID,
            "commitSha": source_sha,
            "files": files,
        }
        if is_shared_source:
            result["relation"] = PROTECTED_SOURCE_RELATION
        return result

    def _verify_target(self) -> dict[str, Any]:
        value = _require_api_object(self.api.get(f"/repositories/{TARGET_REPOSITORY_ID}"), "target repository")
        if value.get("id") != TARGET_REPOSITORY_ID or value.get("node_id") != TARGET_REPOSITORY_NODE_ID:
            raise ForgeError("target repository numeric/node identity differs")
        if value.get("full_name") not in TARGET_REPOSITORY_NAMES:
            raise ForgeError("target repository name is outside the controlled rename states")
        if value.get("default_branch") != TARGET_DEFAULT_BRANCH:
            raise ForgeError("target repository default branch differs from the immutable executor contract")
        return {"id": value["id"], "nodeId": value["node_id"], "name": value["full_name"], "defaultBranch": value["default_branch"], "visibility": value.get("visibility")}

    def _assert_v4_target_snapshot(
        self,
        record: dict[str, Any],
        expected: dict[str, Any],
        stage: str,
    ) -> dict[str, Any]:
        """Re-read and cross-bind the exact target/lifecycle snapshot."""

        current = self._verify_target()
        if current != expected:
            raise ForgeError(f"schema-v4 target changed {stage}")
        if record.get("schemaVersion") == 5:
            try:
                validate_v5_target_url_lifecycle(record, current)
            except ContractError as exc:
                raise ForgeError(str(exc)) from exc
        return current

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

    def _attestation_ref_sha(self, value: Any, expected_ref: str) -> str:
        ref = _require_api_object(value, "activation-attestation ref")
        target = ref.get("object") if isinstance(ref.get("object"), dict) else {}
        tag_sha = target.get("sha")
        if (
            ref.get("ref") != expected_ref
            or target.get("type") != "tag"
            or not isinstance(tag_sha, str)
            or SHA_RE.fullmatch(tag_sha) is None
        ):
            raise ForgeError("activation-attestation ref identity differs")
        return tag_sha

    def _read_attestation_ref_sha(self, nonce: str) -> str | None:
        value = self.api.get_optional(_attestation_ref_get_endpoint(nonce))
        if value is None:
            return None
        return self._attestation_ref_sha(value, _attestation_ref(nonce))

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

    def _attestation_tag_material(
        self,
        report: dict[str, Any],
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        claim = activation_attestation_claim(report)
        message = canonical_bytes(claim).decode("utf-8")
        nonce = report["applyLock"]["nonce"]
        payload = {
            "tag": f"{ATTESTATION_TAG_PREFIX}{nonce}",
            "message": message,
            "object": report["executor"]["commitSha"],
            "type": "commit",
            "tagger": {
                "name": report["actor"]["login"],
                "email": f"{report['actor']['id']}+{report['actor']['login']}@users.noreply.github.com",
                "date": claim["evidenceCutoffAt"],
            },
        }
        return claim, message, payload

    def _verify_attestation_tag(
        self,
        report: dict[str, Any],
        tag_sha: str,
        value: Any | None = None,
    ) -> dict[str, Any]:
        claim, message, payload = self._attestation_tag_material(report)
        item = _require_api_object(
            value if value is not None else self.api.get(f"{APPLY_LOCK_TAGS_ENDPOINT}/{tag_sha}"),
            "activation-attestation annotated tag",
        )
        target = item.get("object") if isinstance(item.get("object"), dict) else {}
        if (
            item.get("sha") != tag_sha
            or item.get("tag") != payload["tag"]
            or item.get("message") != message
            or item.get("tagger") != payload["tagger"]
            or target != {"type": "commit", "sha": report["executor"]["commitSha"]}
        ):
            raise ForgeError("activation-attestation tag does not bind the exact transition claim")
        ruleset = report["attestationRuleset"]
        return {
            "repositoryId": EXECUTOR_REPOSITORY_ID,
            "ref": claim["ref"],
            "tagObjectSha": tag_sha,
            "tagMessageDigest": exact_digest(message.encode("utf-8")),
            "claimDigest": canonical_digest(claim),
            "evidenceCutoffAt": claim["evidenceCutoffAt"],
            "policy": copy.deepcopy(ruleset["policy"]),
            "ruleset": {
                "rulesetId": ruleset["rulesetId"],
                "stateDigest": ruleset["stateDigest"],
            },
        }

    def _create_or_verify_attestation(self, report: dict[str, Any]) -> dict[str, Any]:
        nonce = report["applyLock"]["nonce"]
        expected_ref = _attestation_ref(nonce)
        existing = self._read_attestation_ref_sha(nonce)
        if existing is not None:
            return self._verify_attestation_tag(report, existing)

        _claim, _message, payload = self._attestation_tag_material(report)
        tag_response = _require_api_object(
            self.api.post_created(APPLY_LOCK_TAGS_ENDPOINT, payload),
            "activation-attestation tag creation",
        )
        tag_sha = tag_response.get("sha")
        if not isinstance(tag_sha, str) or SHA_RE.fullmatch(tag_sha) is None:
            raise ForgeError("activation-attestation tag creation lacks exact object identity")
        self._verify_attestation_tag(report, tag_sha, tag_response)
        try:
            ref_response = self.api.post_created(
                APPLY_LOCK_REFS_ENDPOINT,
                {"ref": expected_ref, "sha": tag_sha},
            )
        except ForgeError as exc:
            current = self._read_attestation_ref_sha(nonce)
            if current is None:
                raise ForgeError("activation-attestation ref creation failed or remained uncertain") from exc
            if current != tag_sha:
                raise ForgeError("activation-attestation ref was occupied by a foreign tag") from exc
            return self._verify_attestation_tag(report, current)
        if self._attestation_ref_sha(ref_response, expected_ref) != tag_sha:
            raise ForgeError("activation-attestation create response points to another tag")
        if self._read_attestation_ref_sha(nonce) != tag_sha:
            raise ForgeError("activation-attestation ref readback differs after creation")
        return self._verify_attestation_tag(report, tag_sha)

    def _verify_apply_lock(self, lock: dict[str, Any]) -> None:
        if self._read_lock_ref_sha() != lock["tagObjectSha"]:
            raise ForgeError("apply lock ownership was lost")
        self._verify_lock_tag(lock)

    def _verify_v5_apply_target_fence(
        self,
        record: dict[str, Any],
        lock: dict[str, Any] | None,
    ) -> None:
        """Require the shared fixed fence around every schema-v5 provider write."""

        if record.get("schemaVersion") != 5:
            return
        if (
            lock is None
            or lock.get("repositoryId") != EXECUTOR_REPOSITORY_ID
            or lock.get("ref") != APPLY_LOCK_REF
            or lock.get("executorCommitSha") != self.executor_head
            or lock.get("acquireOutcome") != "acquired"
            or lock.get("releaseOutcome") != "pending"
        ):
            raise ForgeError("schema-v5 provider mutation lacks the fixed target-lifecycle apply fence")
        self._verify_apply_lock(lock)

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
        authorization: dict[str, Any],
        authority_schema_version: int = 1,
    ) -> dict[str, Any]:
        if self.executor_head is None:
            raise ForgeError("executor head was not established before apply lock acquisition")
        nonce = self.nonce_factory()
        if not isinstance(nonce, str) or NONCE_RE.fullmatch(nonce) is None:
            raise ForgeError("apply lock nonce source returned an invalid 32-byte hex nonce")
        if authority_schema_version not in {1, 2, 4, 5}:
            raise ForgeError("apply lock authority version is unsupported")
        claim = {
            "schemaVersion": authority_schema_version,
            "kind": "public-skills-ruleset-apply-lock",
            "repositoryId": EXECUTOR_REPOSITORY_ID,
            "ref": APPLY_LOCK_REF,
            "executorCommitSha": self.executor_head,
            "actor": actor,
            "nonce": nonce,
            "claimedAt": claimed_at,
            "authorization": copy.deepcopy(authorization),
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
            "claimedAt": claimed_at,
            "authorization": copy.deepcopy(authorization),
            "acquireOutcome": "acquired",
            "releaseOutcome": "pending",
            "finalRefAbsentAt": None,
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

    def _verify_historical_apply_authority(
        self,
        report: dict[str, Any],
        *,
        admitted_current_lock: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        desired = report["desiredState"]
        desired_commit = desired["commitSha"]
        commit = _require_api_object(
            self.api.get(f"/repositories/{DOCTRINE_REPOSITORY_ID}/commits/{desired_commit}"),
            "historical Doctrine commit",
        )
        if commit.get("sha") != desired_commit:
            raise ForgeError("historical Doctrine commit identity differs")
        comparison = _require_api_object(
            self.api.get(f"/repositories/{DOCTRINE_REPOSITORY_ID}/compare/{desired_commit}...{DOCTRINE_BRANCH}"),
            "historical Doctrine ancestry",
        )
        base = comparison.get("base_commit") if isinstance(comparison.get("base_commit"), dict) else {}
        if base.get("sha") != desired_commit or comparison.get("status") not in {"ahead", "identical"}:
            raise ForgeError("historical Doctrine desired state is not reachable from protected main")
        historical_record, historical_metadata = self._load_doctrine_at(desired_commit)
        if historical_metadata != desired:
            raise ForgeError("apply report desired-state bytes/metadata differ from protected Doctrine")
        try:
            validate_apply_report(report, historical_record)
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc

        executor = report["executor"]
        executor_commit = executor["commitSha"]
        commit = _require_api_object(
            self.api.get(f"/repositories/{EXECUTOR_REPOSITORY_ID}/commits/{executor_commit}"),
            "historical executor commit",
        )
        if commit.get("sha") != executor_commit:
            raise ForgeError("historical executor commit identity differs")
        comparison = _require_api_object(
            self.api.get(f"/repositories/{EXECUTOR_REPOSITORY_ID}/compare/{executor_commit}...{EXECUTOR_BRANCH}"),
            "historical executor ancestry",
        )
        base = comparison.get("base_commit") if isinstance(comparison.get("base_commit"), dict) else {}
        if base.get("sha") != executor_commit or comparison.get("status") not in {"ahead", "identical"}:
            raise ForgeError("historical executor commit is not reachable from protected main")
        raw = _decode_content(
            self.api.get(_content_endpoint(EXECUTOR_REPOSITORY_ID, EXECUTOR_PATH, executor_commit)),
            EXECUTOR_PATH,
            "historical executor source",
        )
        if exact_digest(raw) != executor["exactBytesDigest"]:
            raise ForgeError("historical executor source bytes differ from the apply report")
        policy, policy_evidence = self._attestation_policy_at(executor_commit)
        attestation_ruleset = report["attestationRuleset"]
        if policy_evidence != attestation_ruleset["policy"]:
            raise ForgeError("historical attestation policy bytes differ from the apply report")
        if attestation_ruleset["normalized"] != expected_attestation_ruleset_readback(policy):
            raise ForgeError("apply report attestation ruleset differs from historical source policy")
        if self._verify_source(historical_record) != report["source"]:
            raise ForgeError("historical workflow source differs from the apply report")

        public_lock = report["applyLock"]
        claim = {
            "schemaVersion": report["schemaVersion"],
            "kind": "public-skills-ruleset-apply-lock",
            "repositoryId": EXECUTOR_REPOSITORY_ID,
            "ref": APPLY_LOCK_REF,
            "executorCommitSha": executor_commit,
            "actor": copy.deepcopy(report["actor"]),
            "nonce": public_lock["nonce"],
            "claimedAt": public_lock["claimedAt"],
            "authorization": apply_lock_authorization_from_report(report),
        }
        message = canonical_bytes(claim).decode("utf-8")
        if exact_digest(message.encode("utf-8")) != public_lock["tagMessageDigest"]:
            raise ForgeError("apply report lock claim digest differs")
        if admitted_current_lock is None:
            if self._read_lock_ref_sha() is not None:
                raise ForgeError("apply lock ref exists during post-release evidence verification")
        else:
            self._verify_apply_lock(admitted_current_lock)
        return historical_record, historical_metadata

    def _collect_audit_event(self, report: dict[str, Any]) -> dict[str, Any]:
        mutation = report["mutation"]
        ruleset_id = mutation["rulesetId"]
        request_id = mutation["requestId"]
        subject = (
            report["plannedMutation"]["subject"]
            if report.get("schemaVersion") in {4, 5}
            else "externalAdmission"
        )
        expected_ruleset_name = (
            BARRIER_RULESET_NAME if subject == "queueBarrier" else RULESET_NAME
        )
        lower = _timestamp(mutation["requestSentAt"], "apply report request time").timestamp() * 1000
        upper = _timestamp(report["postReadback"]["observedAt"], "apply report post time").timestamp() * 1000
        lower -= AUDIT_LOWER_SKEW_SECONDS * 1000
        upper += AUDIT_UPPER_SKEW_SECONDS * 1000

        for delay in AUDIT_RETRY_SECONDS[:AUDIT_MAX_ATTEMPTS]:
            if delay:
                self.sleeper(delay)
            matches: list[dict[str, Any]] = []
            for page in range(1, AUDIT_MAX_PAGES + 1):
                raw_page = self.api.get(_audit_endpoint(page))
                if not isinstance(raw_page, list):
                    raise ForgeError("organization audit log returned a non-array")
                if len(raw_page) > 100:
                    raise ForgeError("organization audit log exceeded the fixed page bound")
                for raw_event in raw_page:
                    if not isinstance(raw_event, dict):
                        continue
                    exact_request = raw_event.get("request_id") == request_id
                    try:
                        projection = _audit_provider_projection(raw_event)
                        normalized = _normalized_audit_projection(projection)
                    except ForgeError:
                        if exact_request:
                            raise ForgeError("provider audit event for the mutation is malformed")
                        continue
                    created_at = normalized["createdAtEpochMs"]
                    if (
                        normalized["requestId"] == request_id
                        and normalized["action"] == AUDIT_ACTION
                        and normalized["actor"] == {"id": GITHUB_ACTOR_ID, "login": GITHUB_ACTOR_LOGIN}
                        and normalized["organization"] == {"id": ORGANIZATION_ID, "login": ORGANIZATION}
                        and normalized["ruleset"] == {
                            "id": ruleset_id,
                            "name": expected_ruleset_name,
                            "sourceType": "Organization",
                            "enforcement": "active",
                        }
                        and lower <= created_at <= upper
                    ):
                        matches.append({
                            "providerProjection": projection,
                            "providerProjectionDigest": canonical_digest(projection),
                            "normalized": normalized,
                            "normalizedDigest": canonical_digest(normalized),
                        })
                if len(raw_page) < 100:
                    break
            if len(matches) > 1:
                raise ForgeError("multiple audit events match the one activation mutation")
            if len(matches) == 1:
                return matches[0]
        raise ForgeError("bounded audit readback did not find the exact activation mutation")

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

    def _live_attestation_ruleset(
        self,
        policy: dict[str, Any] | None = None,
        policy_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        policy = policy if policy is not None else self.attestation_policy
        policy_evidence = (
            policy_evidence
            if policy_evidence is not None
            else self.attestation_policy_evidence
        )
        if policy is None or policy_evidence is None:
            raise ForgeError("attestation policy was not established from protected executor main")
        summaries = self.api.pages(f"/orgs/{ORGANIZATION}/rulesets")
        matches = [
            item
            for item in summaries
            if isinstance(item, dict) and item.get("name") == ATTESTATION_RULESET_NAME
        ]
        if len(matches) != 1:
            raise ForgeError("exactly one immutable activation-attestation ruleset is required")
        ruleset_id = matches[0].get("id")
        if isinstance(ruleset_id, bool) or not isinstance(ruleset_id, int) or ruleset_id < 1:
            raise ForgeError("activation-attestation ruleset lacks an exact numeric ID")
        live = _require_api_object(
            self.api.get(f"/orgs/{ORGANIZATION}/rulesets/{ruleset_id}"),
            "activation-attestation ruleset",
        )
        if (
            live.get("id") != ruleset_id
            or live.get("source_type") != "Organization"
            or live.get("source") != ORGANIZATION
            or live.get("target") != "tag"
        ):
            raise ForgeError("activation-attestation ruleset identity/ownership differs")
        actor_live = _require_api_object(
            self.api.get(
                f"/repos/{EXECUTOR_REPOSITORY}/rulesets/{ruleset_id}?includes_parents=true"
            ),
            "actor-effective activation-attestation ruleset",
        )
        if (
            actor_live.get("id") != ruleset_id
            or actor_live.get("name") != ATTESTATION_RULESET_NAME
            or actor_live.get("source_type") != "Organization"
            or actor_live.get("source") != ORGANIZATION
            or actor_live.get("target") != "tag"
        ):
            raise ForgeError("actor-effective activation-attestation ruleset identity differs")
        actor_bypass = actor_live.get("current_user_can_bypass")
        normalized = normalize_attestation_ruleset(live, actor_bypass=actor_bypass)
        actor_normalized = normalize_attestation_ruleset(
            actor_live,
            actor_bypass=actor_bypass,
            include_repository_selector=False,
        )
        actor_expected = copy.deepcopy(normalized)
        actor_expected["conditions"].pop("repository_id")
        if actor_normalized != actor_expected:
            raise ForgeError("actor-effective activation-attestation ruleset state differs")
        if normalized != expected_attestation_ruleset_readback(policy):
            raise ForgeError("activation-attestation ruleset differs from canonical source policy")
        return {
            "policy": copy.deepcopy(policy_evidence),
            "rulesetId": ruleset_id,
            "normalized": normalized,
            "stateDigest": canonical_digest(normalized),
        }

    def _effective(self, target: dict[str, Any], ruleset_id: int | None) -> list[dict[str, Any]]:
        if ruleset_id is None:
            return []
        values = self.api.pages(f"/repositories/{TARGET_REPOSITORY_ID}/rulesets?includes_parents=true")
        return [{
            "repositoryId": TARGET_REPOSITORY_ID,
            "rulesetId": ruleset_id,
            "rulesetPresent": any(isinstance(item, dict) and item.get("id") == ruleset_id for item in values),
        }]

    def _verify_evaluate_rule_suite_evidence(
        self,
        record: dict[str, Any],
        target: dict[str, Any],
        evidence: dict[str, Any],
        *,
        not_before: datetime | None,
    ) -> dict[str, Any]:
        parsed = _rule_suite_locator(evidence.get("locator"))
        if parsed is None or parsed[0] != target["name"]:
            raise ForgeError("evaluate rule-suite locator does not bind the live target")
        locator_suite_id = parsed[1]
        bindings = evidence["bindings"]
        if locator_suite_id != bindings["ruleSuiteId"]:
            raise ForgeError("evaluate rule-suite locator does not bind its rule-suite ID")
        suite = _require_api_object(
            self.api.get(
                f"/repositories/{TARGET_REPOSITORY_ID}/rulesets/rule-suites/{locator_suite_id}"
            ),
            "evaluate rule-suite readback",
        )
        if (
            suite.get("id") != locator_suite_id
            or suite.get("repository_id") != TARGET_REPOSITORY_ID
        ):
            raise ForgeError("evaluate rule-suite identity differs")
        if suite.get("after_sha") != bindings["headSha"]:
            raise ForgeError("evaluate rule-suite does not bind the synthetic head")
        if suite.get("ref") != f"refs/heads/{target['defaultBranch']}":
            raise ForgeError("evaluate rule-suite does not bind the live target default ref")
        if not isinstance(suite.get("before_sha"), str) or SHA_RE.fullmatch(suite["before_sha"]) is None:
            raise ForgeError("evaluate rule-suite lacks a full before SHA")
        if suite.get("result") != "pass":
            raise ForgeError("evaluate rule-suite aggregate result must pass")
        pushed_at = _provider_timestamp(
            suite.get("pushed_at"),
            "evaluate rule-suite pushed_at",
        )
        evidence_observed = _provider_timestamp(
            evidence["observedAt"],
            "evaluate rule-suite evidence observedAt",
        )
        if pushed_at != evidence_observed:
            raise ForgeError("evaluate rule-suite observedAt does not bind pushed_at")
        if not_before is not None and pushed_at <= not_before:
            raise ForgeError("evaluate rule-suite predates the current evaluate ruleset revision")
        evaluations = (
            suite.get("rule_evaluations")
            if isinstance(suite.get("rule_evaluations"), list)
            else []
        )
        source_evaluations = [
            item
            for item in evaluations
            if isinstance(item, dict)
            and isinstance(item.get("rule_source"), dict)
            and item["rule_source"].get("id") == bindings["rulesetId"]
        ]
        matching = [
            item
            for item in source_evaluations
            if item.get("rule_type") == "workflows"
            and item.get("enforcement") == "evaluate"
        ]
        if (
            len(source_evaluations) != 1
            or len(matching) != 1
            or matching[0].get("result") != "pass"
            or matching[0]["rule_source"].get("type") != "ruleset"
            or matching[0]["rule_source"].get("name") != record["ruleset"]["name"]
        ):
            raise ForgeError(
                "evaluate rule-suite lacks one exact organization-owned "
                "ruleset workflows/evaluate/pass verdict"
            )
        observation = _normalized_evaluate_rule_suite(suite, matching[0])
        if evidence["subjectDigest"] != canonical_digest(observation):
            raise ForgeError("evaluate rule-suite subject digest does not bind live readback")
        return {
            "ruleSuiteId": locator_suite_id,
            "ruleSuitePushedAt": suite["pushed_at"],
            "observationDigest": canonical_digest(observation),
        }

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
        if not isinstance(suite.get("before_sha"), str) or SHA_RE.fullmatch(suite["before_sha"]) is None:
            raise ForgeError(f"{field} rule-suite before SHA differs")
        expected_result = "fail" if field == "negativeControl" else "pass"
        if suite.get("result") != expected_result:
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
        if (
            len(selected) != 1
            or selected[0].get("result") != expected_result
            or selected[0]["rule_source"].get("type") != "ruleset"
            or selected[0]["rule_source"].get("name") != record["ruleset"]["name"]
        ):
            raise ForgeError(f"{field} lacks one exact evaluate-mode rule verdict")
        suite_observation = {
            "id": suite.get("id"), "repositoryId": suite.get("repository_id"),
            "beforeSha": suite.get("before_sha"), "afterSha": suite.get("after_sha"),
            "ref": suite.get("ref"), "aggregateResult": suite.get("result"),
            "pushedAt": suite.get("pushed_at"), "ruleEvaluation": selected[0],
        }
        observation = _run_observation(run, job, suite_observation)
        if field == "negativeControl":
            observation["negativeControl"] = self._verify_negative(record, target, run_id, run, evidence)
        if evidence["subjectDigest"] != canonical_digest(observation):
            raise ForgeError(f"{field} subject digest does not bind live evidence")
        return {
            "runId": run_id,
            "runAttempt": run["run_attempt"],
            "requiredCheckJobId": job["id"],
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
        coverage: dict[str, Any]
        if record["schemaVersion"] == 3:
            coverage = {
                "evaluateRuleSuiteEvidence": self._verify_evaluate_rule_suite_evidence(
                    record,
                    target,
                    evidence["evaluateRuleSuiteReadback"],
                    not_before=not_before,
                )
            }
        else:
            effective_item = evidence["effectiveRulesReadback"]
            if (
                effective_item["locator"]
                != f"https://github.com/{target['name']}/settings/rules"
                or not effective
                or not all(item["rulesetPresent"] for item in effective)
                or effective_item["subjectDigest"] != canonical_digest(effective)
            ):
                raise ForgeError("effective-rules evidence differs from live coverage")
            coverage = {"effectiveRulesDigest": canonical_digest(effective)}
        return {
            "rulesetId": ruleset_id,
            "liveEnforcement": live_enforcement,
            "rulesetUpdatedAt": live["updated_at"],
            "canaryNotBefore": live["updated_at"] if not_before is not None else None,
            "activationEvidenceDigest": _activation_evidence_digest(record),
            "workflowEvidence": workflow_readback,
            **coverage,
        }

    def _verify_report_canaries(
        self,
        report: dict[str, Any],
        historical_record: dict[str, Any],
    ) -> None:
        pre = report["preReadback"]
        synthetic_pre_live = {
            **copy.deepcopy(pre["normalized"]),
            "id": pre["rulesetId"],
            "source_type": "Organization",
            "updated_at": pre["updatedAt"],
        }
        verified = self._verify_activation(
            historical_record,
            synthetic_pre_live,
            report["target"],
            copy.deepcopy(pre["effectiveRules"]),
        )
        if verified != report["activationReadback"]:
            raise ForgeError("live canary verification differs from the sealed apply report")

    def _activation_artifact_at(
        self,
        doctrine_commit: str,
    ) -> tuple[dict[str, Any], bytes, str]:
        item = self.api.get(
            _content_endpoint(DOCTRINE_REPOSITORY_ID, ACTIVATION_EVIDENCE_PATH, doctrine_commit)
        )
        raw = _decode_content(item, ACTIVATION_EVIDENCE_PATH, "Doctrine activation artifact")
        try:
            artifact = validate_activation_artifact(
                strict_json_loads(raw, label="Doctrine activation artifact")
            )
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        metadata = _require_api_object(item, "Doctrine activation artifact")
        blob_sha = metadata.get("sha")
        if not isinstance(blob_sha, str) or SHA_RE.fullmatch(blob_sha) is None:
            raise ForgeError("Doctrine activation artifact lacks exact blob identity")
        return artifact, raw, blob_sha

    def _v4_activation_artifact_at(
        self,
        doctrine_commit: str,
        subject: str,
    ) -> tuple[dict[str, Any], bytes, str]:
        path = (
            QUEUE_BARRIER_ACTIVATION_EVIDENCE_PATH
            if subject == "queueBarrier"
            else ACTIVATION_EVIDENCE_PATH
        )
        item = self.api.get(
            _content_endpoint(DOCTRINE_REPOSITORY_ID, path, doctrine_commit)
        )
        raw = _decode_content(item, path, f"schema-v4 {subject} activation artifact")
        try:
            artifact = validate_activation_artifact(
                strict_json_loads(raw, label=f"schema-v4 {subject} activation artifact")
            )
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        if (
            artifact["schemaVersion"] not in {4, 5}
            or artifact["applyReport"]["plannedMutation"]["subject"] != subject
        ):
            raise ForgeError("schema-v4 activation artifact subject differs")
        metadata = _require_api_object(item, f"schema-v4 {subject} activation artifact")
        blob_sha = metadata.get("sha")
        if not isinstance(blob_sha, str) or SHA_RE.fullmatch(blob_sha) is None:
            raise ForgeError("schema-v4 activation artifact lacks exact blob identity")
        return artifact, raw, blob_sha

    def _finalize_report_attestation(
        self,
        report_value: dict[str, Any],
    ) -> dict[str, Any]:
        report = seal_report(report_value)
        try:
            validate_apply_report(report)
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc

        current_record, target, _live, _effective = self._attestation_finalization_state(
            report,
            stage="before activation attestation creation",
        )

        public_lock = report["applyLock"]
        lock_claim = activation_attestation_claim(report)["applyLock"]["claim"]
        lock_message = canonical_bytes(lock_claim).decode("utf-8")
        if exact_digest(lock_message.encode("utf-8")) != public_lock["tagMessageDigest"]:
            raise ForgeError("apply lock authorization claim differs before attestation")

        projection = self._create_or_verify_attestation(report)
        post_record, post_target, _post_live, _post_effective = self._attestation_finalization_state(
            report,
            stage="after activation attestation creation",
        )
        if post_record != current_record or post_target != target:
            raise ForgeError("authority changed across activation attestation creation")
        tag_sha = self._read_attestation_ref_sha(report["applyLock"]["nonce"])
        if tag_sha is None or tag_sha != projection["tagObjectSha"]:
            raise ForgeError("activation-attestation ref changed after creation")
        if self._verify_attestation_tag(report, tag_sha) != projection:
            raise ForgeError("activation-attestation tag changed after creation")
        existing = report.get("activationAttestation")
        if existing is not None and existing != projection:
            raise ForgeError("sealed activation-attestation projection differs from provider ref")
        report.pop("evidenceDigest", None)
        report["activationAttestation"] = projection
        report["status"] = "APPLIED_PENDING_EVIDENCE"
        report["findings"] = [PENDING_EVIDENCE_FINDING]
        finalized = seal_report(report)
        try:
            return validate_apply_report(finalized, current_record)
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc

    def _finalize_v4_report_attestation(
        self,
        report_value: dict[str, Any],
    ) -> dict[str, Any]:
        report = seal_report(report_value)
        schema_version = report.get("schemaVersion")
        if schema_version not in {4, 5}:
            raise ForgeError("shared queue-barrier attestation report version differs")
        try:
            validate_v4_apply_report(
                report,
                expected_schema_version=schema_version,
            )
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        if self._verify_executor() != report["executor"]:
            raise ForgeError("schema-v4 executor changed before attestation")
        if self._verify_actor() != report["actor"]:
            raise ForgeError("schema-v4 actor changed before attestation")
        self._verify_organization()
        if self._verify_target() != report["target"]:
            raise ForgeError("schema-v4 target changed before attestation")
        record, desired = self._load_doctrine()
        if record.get("schemaVersion") != schema_version or desired != report["desiredState"]:
            raise ForgeError("schema-v4 Doctrine authority changed before attestation")
        if self._verify_source(record) != report["source"]:
            raise ForgeError("schema-v4 source bundle changed before attestation")
        policy, policy_evidence = self._attestation_policy_at(
            report["executor"]["commitSha"]
        )
        if self._live_attestation_ruleset(policy, policy_evidence) != report["attestationRuleset"]:
            raise ForgeError("schema-v4 attestation ruleset changed before attestation")
        subject = report["plannedMutation"]["subject"]
        observations, observation_evidence = self._v4_live_observations(
            record, report["target"]
        )
        selected_readback = observation_evidence[subject]["preReadback"]
        expected_selected_readback = {
            key: copy.deepcopy(report["postReadback"][key])
            for key in [
                "rulesetId", "updatedAt", "normalized", "digest",
                "effectiveRules", "effectiveRulesDigest",
            ]
        }
        if selected_readback != expected_selected_readback:
            raise ForgeError("schema-v4 active live state changed before attestation")
        for other_subject in {"externalAdmission", "queueBarrier"} - {subject}:
            if observation_evidence[other_subject] != report["subjects"][other_subject]:
                raise ForgeError(
                    f"schema-v4 {other_subject} authority changed before attestation"
                )
        if (
            observations[subject]["live"] != report["postReadback"]["normalized"]
            or observations[subject]["effective"] is not True
        ):
            raise ForgeError("schema-v4 active-effective state changed before attestation")
        if self._read_lock_ref_sha() is not None:
            raise ForgeError("schema-v4 apply lock exists during attestation")
        lock_claim = activation_attestation_claim(report)["applyLock"]["claim"]
        if exact_digest(canonical_bytes(lock_claim)) != report["applyLock"]["tagMessageDigest"]:
            raise ForgeError("schema-v4 apply lock claim digest differs before attestation")
        projection = self._create_or_verify_attestation(report)
        post_record, post_desired = self._load_doctrine()
        post_observations, post_observation_evidence = self._v4_live_observations(
            post_record, report["target"]
        )
        if (
            post_record != record
            or post_desired != desired
            or post_observations != observations
            or post_observation_evidence != observation_evidence
        ):
            raise ForgeError("schema-v4 authority changed across attestation creation")
        if self._live_attestation_ruleset(policy, policy_evidence) != report["attestationRuleset"]:
            raise ForgeError(
                "schema-v4 attestation ruleset changed across attestation creation"
            )
        report.pop("evidenceDigest", None)
        report["activationAttestation"] = projection
        report["status"] = "APPLIED_PENDING_EVIDENCE"
        report["findings"] = [PENDING_EVIDENCE_FINDING]
        finalized = seal_report(report)
        try:
            return validate_v4_apply_report(
                finalized,
                record,
                expected_schema_version=schema_version,
            )
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc

    def _attestation_finalization_state(
        self,
        report: dict[str, Any],
        *,
        stage: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        current_executor = self._verify_executor()
        if current_executor != report["executor"]:
            raise ForgeError(f"executor changed {stage}")
        if self._verify_actor() != report["actor"]:
            raise ForgeError(f"actor changed {stage}")
        self._verify_organization()
        target = self._verify_target()
        if target != report["target"]:
            raise ForgeError(f"target changed {stage}")
        current_record, current_metadata = self._load_doctrine()
        if current_metadata != report["desiredState"]:
            raise ForgeError(f"Doctrine changed {stage}")
        try:
            validate_apply_report(report, current_record)
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        policy, policy_evidence = self._attestation_policy_at(report["executor"]["commitSha"])
        if self._live_attestation_ruleset(policy, policy_evidence) != report["attestationRuleset"]:
            raise ForgeError(f"attestation ruleset changed {stage}")
        live = self._live_ruleset(current_record)
        if (
            live is None
            or live.get("id") != report["postReadback"]["rulesetId"]
            or live.get("updated_at") != report["postReadback"]["updatedAt"]
            or normalize_ruleset(live) != report["postReadback"]["normalized"]
        ):
            raise ForgeError(f"live ruleset changed {stage}")
        effective = self._effective(target, current_record["ruleset"]["rulesetId"])
        if effective != report["postReadback"]["effectiveRules"]:
            raise ForgeError(f"effective rules changed {stage}")
        if self._read_lock_ref_sha() is not None:
            raise ForgeError(f"apply lock ref reappeared {stage}")
        return current_record, target, live, effective

    def _verify_active_transition(
        self,
        record: dict[str, Any],
        live: dict[str, Any],
        target: dict[str, Any],
        effective: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.doctrine_head is None:
            raise ForgeError("Doctrine head was not established for active verification")
        artifact, raw, blob_sha = self._activation_artifact_at(self.doctrine_head)
        report = artifact["applyReport"]
        historical_record, _ = self._verify_historical_apply_authority(report)
        policy, policy_evidence = self._attestation_policy_at(report["executor"]["commitSha"])
        if self._live_attestation_ruleset(policy, policy_evidence) != report["attestationRuleset"]:
            raise ForgeError("live immutable-attestation ruleset differs from sealed evidence")
        attestation = report["activationAttestation"]
        tag_sha = self._read_attestation_ref_sha(report["applyLock"]["nonce"])
        if tag_sha is None or tag_sha != attestation["tagObjectSha"]:
            raise ForgeError("durable activation-attestation ref is missing or foreign")
        if self._verify_attestation_tag(report, tag_sha) != attestation:
            raise ForgeError("durable activation-attestation tag differs from sealed evidence")
        transition = activation_transition_from_artifact(
            artifact,
            artifact_raw=raw,
            artifact_blob_sha=blob_sha,
        )
        if transition != record["activationEvidence"]["activationTransition"]:
            raise ForgeError("active desired state does not bind the exact sealed activation artifact")
        expected_active = copy.deepcopy(historical_record)
        expected_active["migration"]["phase"] = "active"
        expected_active["activationEvidence"]["activationTransition"] = transition
        if record != expected_active:
            raise ForgeError("active desired state changed beyond phase and activation transition")
        try:
            validate_apply_report(report, historical_record)
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        live_normalized = normalize_ruleset(live)
        capture = artifact["liveCapture"]
        if (
            live.get("id") != capture["rulesetId"]
            or live.get("updated_at") != capture["updatedAt"]
            or live_normalized != capture["normalized"]
            or canonical_digest(live_normalized) != capture["stateDigest"]
            or effective != capture["effectiveRules"]
            or canonical_digest(effective) != capture["effectiveRulesDigest"]
        ):
            raise ForgeError("current active/effective readback differs from sealed activation evidence")
        if target["id"] != TARGET_REPOSITORY_ID:
            raise ForgeError("active transition target identity differs")
        return {
            "artifactPath": ACTIVATION_EVIDENCE_PATH,
            "artifactGitBlobSha": blob_sha,
            "artifactExactBytesDigest": exact_digest(raw),
            "artifactBodyDigest": artifact["bodyDigest"],
            "artifactEvidenceDigest": artifact["evidenceDigest"],
            "activationEvidenceDigest": report["activationReadback"]["activationEvidenceDigest"],
            "auditNormalizedDigest": artifact["auditEvent"]["normalizedDigest"],
            "liveStateDigest": capture["stateDigest"],
            "effectiveRulesDigest": capture["effectiveRulesDigest"],
        }

    def _v4_collector_state(
        self,
        report: dict[str, Any],
        record: dict[str, Any],
        target: dict[str, Any],
        *,
        stage: str,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        schema_version = report.get("schemaVersion")
        if schema_version not in {4, 5}:
            raise ForgeError("shared queue-barrier collector report version differs")
        try:
            validate_v4_apply_report(
                report,
                record,
                expected_schema_version=schema_version,
            )
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        if self._verify_target() != target:
            raise ForgeError(f"schema-v4 target changed {stage}")
        if self._verify_source(record) != report["source"]:
            raise ForgeError(f"schema-v4 protected source changed {stage}")
        policy, policy_evidence = self._attestation_policy_at(
            report["executor"]["commitSha"]
        )
        if self._live_attestation_ruleset(policy, policy_evidence) != report["attestationRuleset"]:
            raise ForgeError(f"schema-v4 attestation ruleset changed {stage}")
        observations, evidence = self._v4_live_observations(record, target)
        subject = report["plannedMutation"]["subject"]
        selected = evidence[subject]["preReadback"]
        expected_selected = {
            key: copy.deepcopy(report["postReadback"][key])
            for key in [
                "rulesetId", "updatedAt", "normalized", "digest",
                "effectiveRules", "effectiveRulesDigest",
            ]
        }
        if selected != expected_selected or observations[subject]["effective"] is not True:
            raise ForgeError(f"schema-v4 selected subject changed {stage}")
        for other_subject in {"externalAdmission", "queueBarrier"} - {subject}:
            if evidence[other_subject] != report["subjects"][other_subject]:
                raise ForgeError(f"schema-v4 {other_subject} changed {stage}")
        if self._read_lock_ref_sha() is not None:
            raise ForgeError(f"schema-v4 apply lock ref exists {stage}")
        return observations, evidence

    def _collect_v4_transition(
        self,
        report: dict[str, Any],
        *,
        current_executor: dict[str, Any],
        actor: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        if report["actor"] != actor:
            raise ForgeError("schema-v4 collector actor differs from apply")
        current_record, current_metadata = self._load_doctrine()
        if current_metadata != report["desiredState"]:
            raise ForgeError("Doctrine moved beyond the schema-v4 ratchet authority")
        historical_record, _ = self._verify_historical_apply_authority(report)
        if historical_record != current_record:
            raise ForgeError("schema-v4 historical/current ratchet authority differs")
        if report["executor"] != current_executor:
            raise ForgeError("schema-v4 collector executor differs from apply")
        if report["target"] != target:
            raise ForgeError("schema-v4 collector target differs from apply")
        verified_activation = self._v4_activation_readback(
            historical_record,
            report["plannedMutation"]["subject"],
            target,
            report["subjects"],
        )
        if verified_activation != report["activationReadback"]:
            raise ForgeError("schema-v4 activation evidence changed after apply")
        before_observations, before_evidence = self._v4_collector_state(
            report,
            historical_record,
            target,
            stage="before activation attestation",
        )
        report = self._finalize_v4_report_attestation(report)
        audit_event = self._collect_audit_event(report)
        final_record, final_metadata = self._load_doctrine()
        if final_record != historical_record or final_metadata != current_metadata:
            raise ForgeError("schema-v4 Doctrine authority changed before artifact sealing")
        after_observations, after_evidence = self._v4_collector_state(
            report,
            final_record,
            target,
            stage="before activation artifact sealing",
        )
        if after_observations != before_observations or after_evidence != before_evidence:
            raise ForgeError("schema-v4 two-subject authority changed during collection")
        attestation = report["activationAttestation"]
        tag_sha = self._read_attestation_ref_sha(report["applyLock"]["nonce"])
        if tag_sha is None or tag_sha != attestation["tagObjectSha"]:
            raise ForgeError("schema-v4 durable activation attestation is missing or foreign")
        if self._verify_attestation_tag(report, tag_sha) != attestation:
            raise ForgeError("schema-v4 durable activation attestation changed")
        subject = report["plannedMutation"]["subject"]
        live_readback = after_evidence[subject]["preReadback"]
        assert live_readback is not None
        captured_at = self.clock().astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        live_capture = {
            "capturedAt": captured_at,
            "rulesetId": live_readback["rulesetId"],
            "updatedAt": live_readback["updatedAt"],
            "normalized": copy.deepcopy(live_readback["normalized"]),
            "stateDigest": live_readback["digest"],
            "effectiveRules": copy.deepcopy(live_readback["effectiveRules"]),
            "effectiveRulesDigest": live_readback["effectiveRulesDigest"],
        }
        artifact = seal_activation_artifact(
            report, audit_event, live_capture, captured_at
        )
        try:
            return validate_activation_artifact(artifact, historical_record)
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc

    def collect_transition(self, path: Path) -> dict[str, Any]:
        current_executor = self._verify_executor()
        actor = self._verify_actor()
        self._verify_organization()
        target = self._verify_target()
        report = read_sealed_report(path)
        if report.get("schemaVersion") in {4, 5}:
            return self._collect_v4_transition(
                report,
                current_executor=current_executor,
                actor=actor,
                target=target,
            )
        if report["actor"] != actor:
            raise ForgeError("collector actor differs from the sealed apply report")
        current_record, current_metadata = self._load_doctrine()
        if current_metadata != report["desiredState"]:
            raise ForgeError("Doctrine main moved beyond the sealed ratchet desired state")
        historical_record, _ = self._verify_historical_apply_authority(report)
        if historical_record != current_record:
            raise ForgeError("historical and current ratchet desired states differ")
        if report["executor"]["commitSha"] != current_executor["commitSha"]:
            raise ForgeError("collector must run from the same protected executor commit as apply")
        if report["target"] != target:
            raise ForgeError("target identity changed between apply and evidence collection")
        self._verify_report_canaries(report, historical_record)
        live = self._live_ruleset(historical_record)
        if live is None or normalize_ruleset(live) != expected_ruleset(historical_record, enforcement="active"):
            raise ForgeError("collector live ruleset is not the exact active desired state")
        ruleset_id = historical_record["ruleset"]["rulesetId"]
        effective = self._effective(target, ruleset_id)
        if effective != report["postReadback"]["effectiveRules"]:
            raise ForgeError("collector effective-rules readback differs from the apply report")
        report = self._finalize_report_attestation(report)
        audit_event = self._collect_audit_event(report)
        final_record, final_target, live, effective = self._attestation_finalization_state(
            report,
            stage="immediately before activation artifact sealing",
        )
        if final_record != historical_record or final_target != target:
            raise ForgeError("authority changed before activation artifact sealing")
        attestation = report["activationAttestation"]
        tag_sha = self._read_attestation_ref_sha(report["applyLock"]["nonce"])
        if tag_sha is None or tag_sha != attestation["tagObjectSha"]:
            raise ForgeError("durable activation-attestation ref changed before artifact sealing")
        if self._verify_attestation_tag(report, tag_sha) != attestation:
            raise ForgeError("durable activation-attestation tag changed before artifact sealing")
        captured_at = self.clock().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        live_capture = {
            "capturedAt": captured_at,
            "rulesetId": ruleset_id,
            "updatedAt": live.get("updated_at"),
            "normalized": normalize_ruleset(live),
            "stateDigest": canonical_digest(normalize_ruleset(live)),
            "effectiveRules": effective,
            "effectiveRulesDigest": canonical_digest(effective),
        }
        artifact = seal_activation_artifact(report, audit_event, live_capture, captured_at)
        try:
            return validate_activation_artifact(artifact)
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc

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

    def _v4_live_observations(
        self,
        record: dict[str, Any],
        target: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        observation_target = self._assert_v4_target_snapshot(
            record, target, "before live observations"
        )
        summaries = self.api.pages(f"/orgs/{ORGANIZATION}/rulesets")
        if not isinstance(summaries, list):
            raise ForgeError("schema-v4 organization ruleset summary is not an array")
        observations: dict[str, dict[str, Any]] = {}
        evidence: dict[str, dict[str, Any]] = {}
        subjects = {
            "externalAdmission": record,
            "queueBarrier": record["queueBarrier"],
        }
        for subject, owner in subjects.items():
            name = owner["ruleset"]["name"]
            bound = owner["ruleset"]["rulesetId"]
            matches = [
                item for item in summaries
                if isinstance(item, dict) and item.get("name") == name
            ]
            if len(matches) > 1:
                raise ForgeError(f"multiple organization rulesets share schema-v4 subject name {name}")
            if bound is None:
                if matches:
                    raise ForgeError(f"{subject} live ruleset exists before Doctrine binds its ID")
                observations[subject] = {"live": None, "effective": False}
                evidence[subject] = {
                    "rulesetId": None,
                    "preReadback": None,
                    "effectiveRules": [],
                    "effectiveRulesDigest": canonical_digest([]),
                }
                continue
            if not matches or matches[0].get("id") != bound:
                raise ForgeError(f"{subject} bound ID/name does not resolve one live ruleset")
            raw = _require_api_object(
                self.api.get(f"/orgs/{ORGANIZATION}/rulesets/{bound}"),
                f"schema-v4 {subject} ruleset",
            )
            if (
                raw.get("id") != bound
                or raw.get("name") != name
                or raw.get("source_type") not in {None, "Organization"}
            ):
                raise ForgeError(f"schema-v4 {subject} ruleset identity differs")
            normalized = normalize_ruleset(raw)
            effective_rules = self._effective(observation_target, bound)
            effective = bool(effective_rules) and all(
                item["rulesetPresent"] for item in effective_rules
            )
            observations[subject] = {"live": normalized, "effective": effective}
            evidence[subject] = {
                "rulesetId": bound,
                "preReadback": {
                    "rulesetId": bound,
                    "updatedAt": raw.get("updated_at"),
                    "normalized": normalized,
                    "digest": canonical_digest(normalized),
                    "effectiveRules": effective_rules,
                    "effectiveRulesDigest": canonical_digest(effective_rules),
                },
                "effectiveRules": effective_rules,
                "effectiveRulesDigest": canonical_digest(effective_rules),
            }
        self._assert_v4_target_snapshot(record, target, "across live observations")
        return observations, evidence

    def _v4_activation_readback(
        self,
        record: dict[str, Any],
        subject: str,
        target: dict[str, Any],
        subject_evidence: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        snapshot = subject_evidence[subject]["preReadback"]
        if snapshot is None:
            raise ForgeError("schema-v4 activation subject lacks a bound pre-readback")
        ruleset_id = snapshot["rulesetId"]
        synthetic_live = {
            **copy.deepcopy(snapshot["normalized"]),
            "id": ruleset_id,
            "source_type": "Organization",
            "updated_at": snapshot["updatedAt"],
        }
        if subject == "externalAdmission":
            projection = _v4_external_projection(record)
            verified = self._verify_activation(
                projection,
                synthetic_live,
                target,
                copy.deepcopy(snapshot["effectiveRules"]),
            )
            return {"subject": subject, **verified}

        barrier = record["queueBarrier"]
        evidence = barrier["activationEvidence"]
        expected_evaluate = expected_v4_ruleset(
            record, "queueBarrier", enforcement="evaluate"
        )
        evaluate = evidence["evaluateReadback"]
        expected_locator = (
            f"https://github.com/organizations/{ORGANIZATION}/settings/rules/{ruleset_id}"
        )
        if (
            snapshot["normalized"] != expected_evaluate
            or evaluate["locator"].rstrip("/") != expected_locator
            or evaluate["observedAt"] != snapshot["updatedAt"]
            or evaluate["subjectDigest"] != canonical_digest(expected_evaluate)
        ):
            raise ForgeError(
                "schema-v4 queueBarrier evaluate evidence does not bind the live revision"
            )
        not_before = _provider_timestamp(
            snapshot["updatedAt"], "schema-v4 queueBarrier pre-readback updatedAt"
        )
        for field in [
            "pullRequestNoMutationCanary",
            "evaluateMergeGroupFailureCanary",
        ]:
            if _provider_timestamp(
                evidence[field]["observedAt"],
                f"schema-v4 queueBarrier {field}.observedAt",
            ) <= not_before:
                raise ForgeError(
                    f"schema-v4 queueBarrier {field} predates the evaluate revision"
                )
        return {
            "subject": subject,
            "rulesetId": ruleset_id,
            "liveEnforcement": "evaluate",
            "rulesetUpdatedAt": snapshot["updatedAt"],
            "canaryNotBefore": snapshot["updatedAt"],
            "activationEvidenceDigest": _v4_subject_activation_evidence_digest(
                record, subject
            ),
            "preEvidence": {
                field: copy.deepcopy(evidence[field])
                for field in QUEUE_BARRIER_PRE_ACTIVATION_EVIDENCE
            },
        }

    def _verify_v4_active_subject(
        self,
        record: dict[str, Any],
        subject: str,
        target: dict[str, Any],
        subject_evidence: dict[str, dict[str, Any]],
        current_lock: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.doctrine_head is None:
            raise ForgeError("Doctrine head is unavailable for schema-v4 active replay")
        schema_version = record.get("schemaVersion")
        owner = record if subject == "externalAdmission" else record["queueBarrier"]
        live_readback = subject_evidence[subject]["preReadback"]
        admitted_active_phases = (
            {"active", "post-activation", "verified"}
            if schema_version == 5 and subject == "queueBarrier"
            else {"active"}
        )
        if (
            owner["migration"]["phase"] not in admitted_active_phases
            or live_readback is None
            or live_readback["normalized"]
            != expected_v4_ruleset(record, subject, enforcement="active")
            or not live_readback["effectiveRules"]
            or not all(
                item["rulesetPresent"] for item in live_readback["effectiveRules"]
            )
        ):
            raise ForgeError(f"schema-v4 {subject} is not exact active-effective state")
        artifact, raw, blob_sha = self._v4_activation_artifact_at(
            self.doctrine_head, subject
        )
        report = artifact["applyReport"]
        historical_record, _ = self._verify_historical_apply_authority(
            report,
            admitted_current_lock=current_lock,
        )
        if report["plannedMutation"]["subject"] != subject:
            raise ForgeError("schema-v4 persisted activation artifact is cross-subject")
        policy, policy_evidence = self._attestation_policy_at(
            report["executor"]["commitSha"]
        )
        if self._live_attestation_ruleset(policy, policy_evidence) != report["attestationRuleset"]:
            raise ForgeError("schema-v4 immutable attestation ruleset changed")
        attestation = report["activationAttestation"]
        tag_sha = self._read_attestation_ref_sha(report["applyLock"]["nonce"])
        if tag_sha is None or tag_sha != attestation["tagObjectSha"]:
            raise ForgeError("schema-v4 durable activation attestation is missing or foreign")
        if self._verify_attestation_tag(report, tag_sha) != attestation:
            raise ForgeError("schema-v4 durable activation attestation differs")
        try:
            validate_activation_artifact(artifact, historical_record)
            transition = activation_transition_from_artifact(
                artifact,
                artifact_raw=raw,
                artifact_blob_sha=blob_sha,
                historical_record=historical_record,
            )
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        current_transition = owner["activationEvidence"]["activationTransition"]
        if transition != current_transition:
            raise ForgeError("schema-v4 active transition does not bind artifact bytes")

        historical_owner = (
            _v4_external_projection(historical_record)
            if subject == "externalAdmission"
            else historical_record["queueBarrier"]
        )
        current_owner = (
            _v4_external_projection(record)
            if subject == "externalAdmission"
            else owner
        )
        expected_owner = copy.deepcopy(historical_owner)
        expected_owner["migration"]["phase"] = owner["migration"]["phase"]
        expected_owner["activationEvidence"]["activationTransition"] = transition
        if subject == "queueBarrier":
            expected_owner["activationEvidence"]["effectiveRulesReadback"] = copy.deepcopy(
                transition["effectiveRulesReadback"]
            )
            expected_owner["activationEvidence"]["activeProviderRemovalCanary"] = copy.deepcopy(
                owner["activationEvidence"]["activeProviderRemovalCanary"]
            )
            if record["migration"]["phase"] == "active":
                for final_field in [
                    "activePassThroughCanary",
                    "activeExternalFailureCanary",
                ]:
                    final_value = owner["activationEvidence"][final_field]
                    final_required = (
                        schema_version == 4
                        or owner["migration"]["phase"] == "verified"
                    )
                    if historical_owner["activationEvidence"][final_field] is not None or (
                        final_required and final_value is None
                    ):
                        raise ForgeError(
                            "schema-v4 final barrier canaries do not follow external activation"
                        )
                    if final_value is None:
                        continue
                    expected_owner["activationEvidence"][final_field] = copy.deepcopy(
                        final_value
                    )
        if current_owner != expected_owner:
            raise ForgeError(
                f"schema-v4 {subject} active state changed beyond its admitted transition"
            )
        if subject == "externalAdmission":
            historical_barrier = historical_record["queueBarrier"]
            current_barrier = record["queueBarrier"]
            for final_field in [
                "activePassThroughCanary",
                "activeExternalFailureCanary",
            ]:
                final_value = current_barrier["activationEvidence"][final_field]
                final_required = (
                    schema_version == 4
                    or current_barrier["migration"]["phase"] == "verified"
                )
                if (
                    historical_barrier["activationEvidence"][final_field] is not None
                    or (final_required and final_value is None)
                ):
                    raise ForgeError(
                        "schema-v4 final barrier canaries do not follow external activation"
                    )
        capture = artifact["liveCapture"]
        if (
            capture["rulesetId"] != live_readback["rulesetId"]
            or capture["updatedAt"] != live_readback["updatedAt"]
            or capture["normalized"] != live_readback["normalized"]
            or capture["stateDigest"] != live_readback["digest"]
            or capture["effectiveRules"] != live_readback["effectiveRules"]
            or capture["effectiveRulesDigest"]
            != live_readback["effectiveRulesDigest"]
            or target["id"] != TARGET_REPOSITORY_ID
        ):
            raise ForgeError("schema-v4 current active state differs from sealed capture")
        return {
            "subject": subject,
            "artifactPath": transition["executorReport"]["path"],
            "artifactGitBlobSha": blob_sha,
            "artifactExactBytesDigest": exact_digest(raw),
            "artifactBodyDigest": artifact["bodyDigest"],
            "artifactEvidenceDigest": artifact["evidenceDigest"],
            "transitionDigest": canonical_digest(transition),
            "liveStateDigest": capture["stateDigest"],
            "effectiveRulesDigest": capture["effectiveRulesDigest"],
        }

    def _reconcile_v4(
        self,
        mode: str,
        report: dict[str, Any],
        lock: dict[str, Any] | None,
        record: dict[str, Any],
        desired: dict[str, Any],
    ) -> dict[str, Any]:
        schema_version = record["schemaVersion"]
        if schema_version not in {4, 5}:
            raise ForgeError("shared queue-barrier reconciler received unsupported schema")
        report["schemaVersion"] = schema_version
        report["desiredState"] = desired
        report["phase"] = {
            "externalAdmission": record["migration"]["phase"],
            "queueBarrier": record["queueBarrier"]["migration"]["phase"],
        }
        report["activationSequencingDigest"] = canonical_digest(
            record["activationSequencing"]
        )
        self._verify_organization()
        target = self._verify_target()
        report["target"] = target
        if schema_version == 5:
            try:
                validate_v5_target_url_lifecycle(record, target)
            except ContractError as exc:
                raise ForgeError(str(exc)) from exc
        if record["queueBarrier"]["workflowSource"]["commitSha"] is None:
            report["status"] = "BLOCKED"
            report["findings"].append(
                "schema-v4 protected source bundle is unresolved; provider reconciliation is disabled"
            )
            report["subjects"] = None
            self._assert_v4_target_snapshot(
                record, target, "before unresolved-source return"
            )
            return report
        report["source"] = self._verify_source(record)
        observations, subject_evidence = self._v4_live_observations(
            record, target
        )
        report["subjects"] = subject_evidence
        active_replay: dict[str, Any] = {}
        if record["migration"]["phase"] != "recovery":
            for active_subject in ["queueBarrier", "externalAdmission"]:
                active_owner = (
                    record
                    if active_subject == "externalAdmission"
                    else record["queueBarrier"]
                )
                if active_owner["migration"]["phase"] in {
                    "active",
                    "post-activation",
                    "verified",
                }:
                    active_replay[active_subject] = self._verify_v4_active_subject(
                        record,
                        active_subject,
                        target,
                        subject_evidence,
                        lock,
                    )
        if active_replay:
            report["activationReadback"] = active_replay

        for pending_subject in ["queueBarrier", "externalAdmission"]:
            pending_owner = (
                record
                if pending_subject == "externalAdmission"
                else record["queueBarrier"]
            )
            if (
                pending_owner["migration"]["phase"] == "ratchet"
                and observations[pending_subject]["live"]
                == expected_v4_ruleset(
                    record, pending_subject, enforcement="active"
                )
                and observations[pending_subject]["effective"] is True
            ):
                report["status"] = "BLOCKED"
                report["findings"].append(
                    f"BLOCKED_PENDING_TRANSITION_EVIDENCE:{pending_subject}"
                )
                self._assert_v4_target_snapshot(
                    record, target, "before pending-transition return"
                )
                return report
        try:
            actions = (
                plan_v5_ruleset_actions(record, observations)
                if schema_version == 5
                else plan_v4_ruleset_actions(record, observations)
            )
        except ContractError as exc:
            raise ForgeError(str(exc)) from exc
        if len(actions) > 1:
            raise ForgeError("schema-v4 planner violated the one-ruleset-mutation boundary")
        if not actions:
            self._assert_v4_target_snapshot(
                record, target, "before no-action return"
            )
            return report
        planned = actions[0]
        report["plannedMutation"] = copy.deepcopy(planned)
        if planned["payload"]["enforcement"] == "active":
            report["attestationRuleset"] = self._live_attestation_ruleset()
            report["activationReadback"] = self._v4_activation_readback(
                record,
                planned["subject"],
                target,
                subject_evidence,
            )
        if mode == "readback":
            report["status"] = "DRIFT"
            report["findings"].append(
                f"{planned['subject']} differs; readback mode cannot mutate"
            )
            report["plannedMutation"] = None
            self._assert_v4_target_snapshot(
                record, target, "before readback return"
            )
            return report
        if mode == "dry-run":
            report["status"] = "DRIFT"
            self._assert_v4_target_snapshot(
                record, target, "before dry-run return"
            )
            return report
        if lock is None:
            raise ForgeError("schema-v4 apply reached mutation planning without the fenced lock")
        if lock.get("authorization") != apply_lock_authorization_from_report(report):
            raise ForgeError("schema-v4 mutation authority changed after lock acquisition")
        self._assert_heads_unchanged()
        current_record, current_desired = self._load_doctrine()
        if current_record != record or current_desired != desired:
            raise ForgeError("Doctrine changed after schema-v4 lock acquisition")
        current_observations, _ = self._v4_live_observations(record, target)
        if current_observations != observations:
            raise ForgeError("schema-v4 live precondition changed after lock acquisition")
        self._verify_apply_lock(lock)
        self._verify_v5_apply_target_fence(record, lock)
        self._assert_v4_target_snapshot(
            record, target, "inside the apply fence before provider mutation"
        )
        self._verify_v5_apply_target_fence(record, lock)

        action = planned["action"]
        subject = planned["subject"]
        payload = planned["payload"]
        report["mutation"] = {
            "attempted": True,
            "subject": subject,
            "action": action,
            "outcome": "request-sent",
        }
        provider_request_id: str | None = None
        request_sent_at = self.clock().astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        try:
            if action == "create":
                response = _require_api_object(
                    self.api.post(f"/orgs/{ORGANIZATION}/rulesets", payload),
                    "schema-v4 ruleset create response",
                )
                ruleset_id = _positive_integer(
                    response.get("id"), "schema-v4 created ruleset ID"
                )
            elif action == "update":
                ruleset_id = _positive_integer(
                    planned["rulesetId"], "schema-v4 update ruleset ID"
                )
                if hasattr(self.api, "put_observed"):
                    response_value, provider_request_id = self.api.put_observed(
                        f"/orgs/{ORGANIZATION}/rulesets/{ruleset_id}",
                        payload,
                    )
                else:
                    response_value = self.api.put(
                        f"/orgs/{ORGANIZATION}/rulesets/{ruleset_id}",
                        payload,
                    )
                response = _require_api_object(
                    response_value, "schema-v4 ruleset update response"
                )
                if response.get("id") != ruleset_id:
                    raise ForgeError("schema-v4 update response ID differs")
            else:
                raise ForgeError("schema-v4 mutation action is unsupported")
        except (ContractError, ForgeError) as exc:
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "request-failed-or-outcome-unknown"
            report["findings"].append(str(exc))
            return report

        self._assert_v4_target_snapshot(
            record, target, "inside the apply fence after provider mutation"
        )
        post_raw = _require_api_object(
            self.api.get(f"/orgs/{ORGANIZATION}/rulesets/{ruleset_id}"),
            "schema-v4 post-mutation ruleset",
        )
        post_target = self._assert_v4_target_snapshot(
            record, target, "across provider post-readback"
        )
        post_normalized = normalize_ruleset(post_raw)
        if (
            post_raw.get("id") != ruleset_id
            or post_raw.get("name") != payload["name"]
            or post_normalized != payload
        ):
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "post-readback-mismatch"
            report["findings"].append(
                "schema-v4 ruleset differs on immediate post-write readback"
            )
            return report
        post_effective = self._effective(post_target, ruleset_id)
        self._assert_v4_target_snapshot(
            record, target, "across post-effective readback"
        )
        if payload["enforcement"] == "active" and (
            not post_effective
            or not all(item["rulesetPresent"] for item in post_effective)
        ):
            report["status"] = "ERROR"
            report["mutation"]["outcome"] = "post-effective-readback-mismatch"
            report["findings"].append(
                "schema-v4 active ruleset lacks immediate effective coverage"
            )
            return report
        post_observed_at = self.clock().astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        report["mutation"] = {
            "attempted": True,
            "subject": subject,
            "action": action,
            "outcome": "created" if action == "create" else "updated",
            "rulesetId": ruleset_id,
            "requestSentAt": request_sent_at,
            "requestId": provider_request_id,
        }
        report["postReadback"] = {
            "subject": subject,
            "rulesetId": ruleset_id,
            "updatedAt": post_raw.get("updated_at"),
            "observedAt": post_observed_at,
            "normalized": post_normalized,
            "digest": canonical_digest(post_normalized),
            "effectiveRules": post_effective,
            "effectiveRulesDigest": canonical_digest(post_effective),
        }
        if action == "create":
            report["status"] = "DRIFT"
            report["findings"].append(
                f"queueBarrier ruleset {ruleset_id} created in evaluate mode; bind its ID in Doctrine"
            )
            return report
        owner = record if subject == "externalAdmission" else record["queueBarrier"]
        if owner["migration"]["phase"] == "ratchet":
            if (
                not isinstance(provider_request_id, str)
                or AUDIT_REQUEST_ID_RE.fullmatch(provider_request_id) is None
            ):
                report["status"] = "ERROR"
                report["mutation"]["outcome"] = "updated-without-provider-request-id"
                report["findings"].append(
                    "schema-v4 activation update lacks provider request identity"
                )
                return report
            # Subject-qualified top-level projections let the shared immutable
            # attestation machinery seal this one mutation after lock release.
            report["preReadback"] = copy.deepcopy(
                report["subjects"][subject]["preReadback"]
            )
            report["status"] = "APPLIED_PENDING_ATTESTATION"
            report["findings"] = [PENDING_ATTESTATION_FINDING]
        return report

    def _reconcile(
        self,
        mode: str,
        report: dict[str, Any],
        lock: dict[str, Any] | None,
    ) -> dict[str, Any]:
        record, desired = self._load_doctrine()
        if record.get("schemaVersion") in {4, 5}:
            return self._reconcile_v4(mode, report, lock, record, desired)
        report["schemaVersion"] = _authority_schema_version(record)
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
        if phase in {"ratchet", "active"}:
            report["attestationRuleset"] = self._live_attestation_ruleset()
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

        if phase == "active":
            if live is None:
                raise ForgeError("active phase requires the exact bound live ruleset")
            if normalize_ruleset(live) != desired_payload:
                raise ForgeError("active live state differs from the exact desired payload")
            if not effective or not all(item["rulesetPresent"] for item in effective):
                raise ForgeError("active live state lacks effective target coverage")
            report["activationReadback"] = self._verify_active_transition(
                record,
                live,
                target,
                effective,
            )
            return report

        if phase == "ratchet" and live is not None and normalize_ruleset(live)["enforcement"] == "active":
            if normalize_ruleset(live) != desired_payload:
                raise ForgeError("ratchet live active state differs from the exact desired payload")
            if not effective or not all(item["rulesetPresent"] for item in effective):
                raise ForgeError("ratchet live active state lacks effective target coverage")
            report["status"] = "BLOCKED"
            report["findings"].append("BLOCKED_PENDING_TRANSITION_EVIDENCE")
            return report

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
        if lock.get("authorization") != apply_lock_authorization_from_report(report):
            raise ForgeError("desired state, mutation plan, or pre-readback changed after apply lock acquisition")
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
        if phase == "ratchet":
            final_attestation_ruleset = self._live_attestation_ruleset()
            if (
                final_attestation_ruleset != report["attestationRuleset"]
                or final_attestation_ruleset != lock["authorization"].get("attestationRuleset")
            ):
                raise ForgeError("immutable attestation ruleset changed before activation mutation")
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
        request_sent_at = self.clock().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        provider_request_id: str | None = None
        try:
            if hasattr(self.api, "put_observed"):
                response_value, provider_request_id = self.api.put_observed(
                    f"/orgs/{ORGANIZATION}/rulesets/{ruleset_id}", desired_payload
                )
            else:
                response_value = self.api.put(f"/orgs/{ORGANIZATION}/rulesets/{ruleset_id}", desired_payload)
            response = _require_api_object(response_value, "update ruleset response")
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
        _provider_timestamp(post.get("updated_at"), "post-update ruleset.updated_at")
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
        if phase == "ratchet" and (
            not isinstance(provider_request_id, str)
            or AUDIT_REQUEST_ID_RE.fullmatch(provider_request_id) is None
        ):
            report["status"] = "ERROR"
            report["mutation"] = {
                "attempted": True, "action": action, "outcome": "updated-without-provider-request-id",
                "rulesetId": ruleset_id, "requestSentAt": request_sent_at, "requestId": provider_request_id,
            }
            report["findings"].append("activation update lacks X-GitHub-Request-Id for audit correlation")
            return report
        post_observed_at = self.clock().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        report["mutation"] = {
            "attempted": True, "action": action, "outcome": "updated", "rulesetId": ruleset_id,
            "requestSentAt": request_sent_at, "requestId": provider_request_id,
        }
        report["postReadback"] = {
            "rulesetId": ruleset_id, "updatedAt": post.get("updated_at"), "observedAt": post_observed_at,
            "normalized": post_normalized, "digest": canonical_digest(post_normalized),
            "effectiveRules": post_effective, "effectiveRulesDigest": canonical_digest(post_effective),
        }
        if phase == "ratchet":
            report["status"] = "APPLIED_PENDING_ATTESTATION"
            report["findings"].append(PENDING_ATTESTATION_FINDING)
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
            "claimedAt": lock["claimedAt"],
            "acquireOutcome": lock["acquireOutcome"],
            "releaseOutcome": lock["releaseOutcome"],
            "finalRefAbsentAt": lock["finalRefAbsentAt"],
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
            "attestationRuleset": None,
            "activationAttestation": None,
        }
        if mode != "apply":
            return seal_report(self._reconcile(mode, report, None))

        # Build the exact desired/live mutation authorization without a write.
        # If no mutation is admitted (including active steady state and the
        # post-update/pending-evidence state), return without even a lock ref.
        preflight = self._reconcile("dry-run", copy.deepcopy(report), None)
        if preflight["plannedMutation"] is None:
            return seal_report(preflight)
        preflight_head = self.doctrine_head
        authorization = apply_lock_authorization_from_report(preflight)

        lock = self._acquire_apply_lock(
            actor,
            observed_at,
            authorization,
            preflight["schemaVersion"],
        )
        report["applyLock"] = self._public_apply_lock(lock)
        result = report
        body_error: BaseException | None = None
        release_error: ForgeError | None = None
        try:
            if preflight_head is None or _head(
                self.api,
                DOCTRINE_REPOSITORY_ID,
                DOCTRINE_BRANCH,
                "Doctrine post-lock probe head",
            ) != preflight_head:
                raise ForgeError("Doctrine main changed between the non-mutating phase probe and apply lock")
            result = self._reconcile(mode, report, lock)
        except BaseException as exc:  # The finally path must release even on cancellation.
            body_error = exc
        finally:
            try:
                self._release_apply_lock(lock)
                lock["releaseOutcome"] = "released"
                lock["finalRefAbsentAt"] = self.clock().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
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
        elif result.get("status") == "APPLIED_PENDING_ATTESTATION":
            try:
                result = (
                    self._finalize_v4_report_attestation(result)
                    if result.get("schemaVersion") in {4, 5}
                    else self._finalize_report_attestation(result)
                )
            except ForgeError:
                # The sealed pending-attestation report is the deterministic
                # recovery input.  Never disguise a completed ruleset update
                # as ERROR or attempt that update a second time.
                result["status"] = "APPLIED_PENDING_ATTESTATION"
                result["findings"] = [PENDING_ATTESTATION_FINDING]
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
    modes.add_argument(
        "--collect-transition",
        type=Path,
        metavar="SEALED_APPLY_REPORT",
        help="collect bounded provider evidence for one sealed pending-evidence apply report",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    mode = "collect-transition" if args.collect_transition is not None else "apply" if args.apply else "readback" if args.readback else "dry-run"
    try:
        token = keyring_token()
        api = GitHubAPI(token)
        executor = RulesetExecutor(api, Path(__file__).resolve().read_bytes())
        if args.collect_transition is not None:
            artifact = executor.collect_transition(args.collect_transition)
            print(canonical_bytes(artifact).decode("utf-8"))
            return 0
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
