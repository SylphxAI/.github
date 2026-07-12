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
    "active": {"active"},
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


def _validate_actor(value: Any, label: str, *, include_type: bool) -> dict[str, Any]:
    actor = _object(value, label)
    keys = {"id", "login", "type"} if include_type else {"id", "login"}
    _exact_keys(actor, keys, label)
    if actor.get("id") != GITHUB_ACTOR_ID or actor.get("login") != GITHUB_ACTOR_LOGIN:
        raise ContractError(f"{label} differs from the admitted actor")
    if include_type and actor.get("type") != GITHUB_ACTOR_TYPE:
        raise ContractError(f"{label}.type differs from the admitted actor type")
    return actor


def _activation_evidence_digest(evidence: dict[str, Any]) -> str:
    return canonical_digest({field: evidence[field] for field in EVIDENCE_KINDS})


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
    if transition["kind"] != TRANSITION_KIND or transition["schemaVersion"] != 1:
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
    if authorization["activationEvidenceDigest"] != _activation_evidence_digest(record["activationEvidence"]):
        raise ContractError("activation transition canary-evidence digest differs")

    pre = _validate_transition_readback(transition["pre"], "activationTransition.pre", ruleset_id=ruleset_id, enforcement="evaluate")
    post = _validate_transition_readback(transition["post"], "activationTransition.post", ruleset_id=ruleset_id, enforcement="active")
    if pre["stateDigest"] != canonical_digest(expected_ruleset(record, enforcement="evaluate")):
        raise ContractError("activation transition pre-state digest differs")
    if post["stateDigest"] != authorization["desiredPayloadDigest"]:
        raise ContractError("activation transition post-state digest differs")

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
        "schemaVersion": 2,
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
    _exact_keys(evidence, {*EVIDENCE_KINDS, "activationTransition"}, "activationEvidence")
    observed: dict[str, datetime] = {}
    for field in EVIDENCE_KINDS:
        value = _validate_evidence(evidence[field], field, root)
        if value is not None:
            observed[field] = value

    if source["commitSha"] is None:
        if phase != "expand" or ruleset["rulesetId"] is not None or ruleset["enforcement"] != "evaluate":
            raise ContractError("unresolved source SHA is allowed only in unbound expand/evaluate")
    if phase in {"reconcile", "ratchet", "active", "recovery"} and ruleset["rulesetId"] is None:
        raise ContractError(f"phase {phase} requires an exact ruleset ID")
    if phase in {"ratchet", "active"}:
        missing = [field for field in EVIDENCE_KINDS if evidence[field] is None]
        if missing:
            raise ContractError(f"{phase} lacks activation evidence {missing}")
        if "localRequiredChecks" not in source or "negativeControlPolicy" not in source:
            raise ContractError(f"{phase} requires the complete negative-control policy")
        if not observed["evaluateReadback"] <= observed["pullRequestCanary"] <= observed["effectiveRulesReadback"]:
            raise ContractError("pull-request canary chronology is invalid")
        if not observed["evaluateReadback"] <= observed["mergeGroupCanary"] <= observed["effectiveRulesReadback"]:
            raise ContractError("merge-group canary chronology is invalid")
        if not observed["evaluateReadback"] <= observed["negativeControl"] <= observed["effectiveRulesReadback"]:
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


def _validate_effective_rules(value: Any, label: str, ruleset_id: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 1:
        raise ContractError(f"{label} must contain exactly one target projection")
    item = _object(value[0], f"{label}[0]")
    _exact_keys(item, {"repositoryId", "rulesetId", "rulesetPresent"}, f"{label}[0]")
    if item != {
        "repositoryId": TARGET_REPOSITORY_ID,
        "rulesetId": ruleset_id,
        "rulesetPresent": True,
    }:
        raise ContractError(f"{label} does not prove the exact target/ruleset binding")
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
    effective = _validate_effective_rules(readback["effectiveRules"], f"{label}.effectiveRules", ruleset_id)
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
    _exact_keys(
        readback,
        {
            "rulesetId", "liveEnforcement", "rulesetUpdatedAt", "canaryNotBefore",
            "activationEvidenceDigest", "workflowEvidence", "effectiveRulesDigest",
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
    if record is not None and readback["activationEvidenceDigest"] != _activation_evidence_digest(record["activationEvidence"]):
        raise ContractError("apply report activation-evidence digest differs")
    if readback["effectiveRulesDigest"] != report["preReadback"]["effectiveRulesDigest"]:
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
    return readback


def validate_apply_report(value: Any, record: dict[str, Any] | None = None) -> dict[str, Any]:
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
    if report["schemaVersion"] != 1 or report["kind"] != EXECUTION_REPORT_KIND:
        raise ContractError("apply report identity is unsupported")
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
    pre = _validate_report_readback(report["preReadback"], "apply report.preReadback", ruleset_id=ruleset_id, enforcement="evaluate", include_observed_at=False)
    post = _validate_report_readback(report["postReadback"], "apply report.postReadback", ruleset_id=ruleset_id, enforcement="active", include_observed_at=True)
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
    effective = _validate_effective_rules(capture["effectiveRules"], "activation artifact.liveCapture.effectiveRules", ruleset_id)
    if _digest(capture["effectiveRulesDigest"], "activation artifact.liveCapture.effectiveRulesDigest") != canonical_digest(effective):
        raise ContractError("activation artifact live effective-rules digest differs")
    return capture


def seal_activation_artifact(
    apply_report: dict[str, Any],
    audit_event: dict[str, Any],
    live_capture: dict[str, Any],
    captured_at: str,
) -> dict[str, Any]:
    body = {
        "schemaVersion": 1,
        "kind": ACTIVATION_REPORT_KIND,
        "capturedAt": captured_at,
        "applyReport": copy.deepcopy(apply_report),
        "auditEvent": copy.deepcopy(audit_event),
        "liveCapture": copy.deepcopy(live_capture),
    }
    artifact = {**body, "bodyDigest": canonical_digest(body)}
    artifact["evidenceDigest"] = canonical_digest(artifact)
    return artifact


def validate_activation_artifact(value: Any) -> dict[str, Any]:
    artifact = _object(value, "activation artifact")
    _exact_keys(
        artifact,
        {
            "schemaVersion", "kind", "capturedAt", "applyReport", "auditEvent",
            "liveCapture", "bodyDigest", "evidenceDigest",
        },
        "activation artifact",
    )
    if artifact["schemaVersion"] != 1 or artifact["kind"] != ACTIVATION_REPORT_KIND:
        raise ContractError("activation artifact identity is unsupported")
    captured_at = _timestamp(artifact["capturedAt"], "activation artifact.capturedAt")
    body = {field: artifact[field] for field in ["schemaVersion", "kind", "capturedAt", "applyReport", "auditEvent", "liveCapture"]}
    if _digest(artifact["bodyDigest"], "activation artifact.bodyDigest") != canonical_digest(body):
        raise ContractError("activation artifact bodyDigest differs")
    evidence_subject = copy.deepcopy(artifact)
    supplied_evidence_digest = _digest(evidence_subject.pop("evidenceDigest"), "activation artifact.evidenceDigest")
    if supplied_evidence_digest != canonical_digest(evidence_subject):
        raise ContractError("activation artifact evidenceDigest differs")
    report = validate_apply_report(artifact["applyReport"])
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
    if (
        normalized_audit["requestId"] != report["mutation"]["requestId"]
        or normalized_audit["actor"] != {"id": report["actor"]["id"], "login": report["actor"]["login"]}
        or normalized_audit["organization"] != {"id": ORGANIZATION_ID, "login": ORGANIZATION}
        or normalized_audit["ruleset"] != {
            "id": ruleset_id,
            "name": RULESET_NAME,
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


def activation_transition_from_artifact(
    artifact: dict[str, Any],
    *,
    artifact_raw: bytes,
    artifact_blob_sha: str,
) -> dict[str, Any]:
    report = artifact["applyReport"]
    audit = artifact["auditEvent"]
    pre = report["preReadback"]
    post = report["postReadback"]
    lock = report["applyLock"]
    return {
        "kind": TRANSITION_KIND,
        "schemaVersion": 1,
        "authorization": {
            "desiredState": copy.deepcopy(report["desiredState"]),
            "executor": copy.deepcopy(report["executor"]),
            "desiredPayloadDigest": report["plannedMutation"]["payloadDigest"],
            "activationEvidenceDigest": report["activationReadback"]["activationEvidenceDigest"],
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
            "path": ACTIVATION_EVIDENCE_PATH,
            "gitBlobSha": artifact_blob_sha,
            "exactBytesDigest": exact_digest(artifact_raw),
            "bodyDigest": artifact["bodyDigest"],
            "evidenceDigest": artifact["evidenceDigest"],
        },
        "capturedAt": artifact["capturedAt"],
    }


def apply_lock_authorization_from_report(report: dict[str, Any]) -> dict[str, Any]:
    planned = _object(report.get("plannedMutation"), "apply preflight plannedMutation")
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
        "schemaVersion": 1,
        "kind": "public-skills-ruleset-apply-lock",
        "repositoryId": EXECUTOR_REPOSITORY_ID,
        "ref": APPLY_LOCK_REF,
        "executorCommitSha": report["executor"]["commitSha"],
        "actor": copy.deepcopy(report["actor"]),
        "nonce": lock["nonce"],
        "claimedAt": lock["claimedAt"],
        "authorization": apply_lock_authorization_from_report(report),
    }
    return {
        "schemaVersion": 1,
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
        record = validate_record(strict_json_loads(raw, label="Doctrine desired-state record"))
        metadata = _require_api_object(item, "Doctrine desired-state record")
        return record, {
            "repositoryId": DOCTRINE_REPOSITORY_ID, "commitSha": commit_sha, "path": DOCTRINE_RECORD_PATH,
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
            "schemaVersion": 1,
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
        if self._read_lock_ref_sha() is not None:
            raise ForgeError("apply lock ref exists during post-release evidence verification")
        return historical_record, historical_metadata

    def _collect_audit_event(self, report: dict[str, Any]) -> dict[str, Any]:
        mutation = report["mutation"]
        ruleset_id = mutation["rulesetId"]
        request_id = mutation["requestId"]
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
                            "name": RULESET_NAME,
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
        effective_item = evidence["effectiveRulesReadback"]
        if effective_item["locator"] != f"https://github.com/{target['name']}/settings/rules" or not effective or not all(item["rulesetPresent"] for item in effective) or effective_item["subjectDigest"] != canonical_digest(effective):
            raise ForgeError("effective-rules evidence differs from live coverage")
        return {
            "rulesetId": ruleset_id,
            "liveEnforcement": live_enforcement,
            "rulesetUpdatedAt": live["updated_at"],
            "canaryNotBefore": live["updated_at"] if not_before is not None else None,
            "activationEvidenceDigest": _activation_evidence_digest(evidence),
            "workflowEvidence": workflow_readback,
            "effectiveRulesDigest": canonical_digest(effective),
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

    def collect_transition(self, path: Path) -> dict[str, Any]:
        current_executor = self._verify_executor()
        actor = self._verify_actor()
        self._verify_organization()
        target = self._verify_target()
        report = read_sealed_report(path)
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

        lock = self._acquire_apply_lock(actor, observed_at, authorization)
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
                result = self._finalize_report_attestation(result)
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
