#!/usr/bin/env node

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync, realpathSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const API_ROOT = "https://api.github.com";
const API_VERSION = "2022-11-28";
const GITHUB_ACTIONS_APP = Object.freeze({ id: 15368, slug: "github-actions", name: "GitHub Actions" });
const SHA_RE = /^[0-9a-f]{40}$/;
const NODE_ID_RE = /^(?:R|PR|MQE)_[A-Za-z0-9_-]+$/;

const REPOSITORY_SNAPSHOT_QUERY = `query PublicSkillsBarrierSnapshot($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    id
    nameWithOwner
    defaultBranchRef { name target { ... on Commit { oid } } }
    pullRequest(number: $number) {
      id
      number
      state
      merged
      baseRefName
      headRefName
      headRefOid
      baseRepository { id nameWithOwner }
      headRepository { id nameWithOwner }
      autoMergeRequest { enabledAt }
      mergeQueueEntry {
        id
        position
        state
        baseCommit { oid }
        headCommit { oid }
        pullRequest { id number }
      }
    }
  }
}`;

export class BarrierError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "BarrierError";
    this.code = code;
  }
}

function reject(code, message) {
  throw new BarrierError(code, message);
}

function requireCondition(condition, code, message) {
  if (!condition) reject(code, message);
}

function sorted(values) {
  return [...values].sort();
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function requireExactKeys(value, keys, label) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), "CONTRACT_SHAPE", `${label} must be an object.`);
  requireCondition(
    JSON.stringify(sorted(Object.keys(value))) === JSON.stringify(sorted(keys)),
    "CONTRACT_SHAPE",
    `${label} keys differ from the exact contract.`,
  );
}

function requireInteger(value, label, { minimum = 1, maximum = Number.MAX_SAFE_INTEGER } = {}) {
  requireCondition(Number.isSafeInteger(value) && value >= minimum && value <= maximum, "CONTRACT_SHAPE", `${label} must be an integer from ${minimum} through ${maximum}.`);
  return value;
}

function requireString(value, label, pattern = null) {
  requireCondition(typeof value === "string" && value.length > 0, "CONTRACT_SHAPE", `${label} must be a non-empty string.`);
  if (pattern) requireCondition(pattern.test(value), "CONTRACT_SHAPE", `${label} has an invalid value.`);
  return value;
}

function requireSha(value, label) {
  return requireString(value, label, SHA_RE);
}

function requireNodeId(value, label) {
  return requireString(value, label, NODE_ID_RE);
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(sorted(Object.keys(value)).map((key) => [key, canonicalize(value[key])]));
  }
  return value;
}

export function canonicalDigest(value) {
  return `sha256:${createHash("sha256").update(JSON.stringify(canonicalize(value))).digest("hex")}`;
}

function validateSafeSourcePath(value, label) {
  requireString(value, label, /^[A-Za-z0-9._/-]+$/);
  requireCondition(!value.startsWith("/") && !value.includes("..") && !value.includes("//"), "CONTRACT_SHAPE", `${label} is not a safe repository path.`);
}

export function validatePolicy(policy) {
  requireExactKeys(policy, ["schemaVersion", "kind", "source", "target", "externalAdmission", "barrier", "events", "polling"], "policy");
  requireCondition(policy.schemaVersion === 3 && policy.kind === "sylphx-public-skills-merge-queue-barrier", "POLICY_IDENTITY", "Policy identity differs from version 3.");

  requireExactKeys(policy.source, ["repository", "repositoryId", "repositoryNodeId", "defaultBranch", "workflowPath", "controllerPath", "policyPath", "protectedSourceBundle"], "policy.source");
  requireCondition(policy.source.repository === "SylphxAI/.github", "POLICY_IDENTITY", "Source repository differs.");
  requireCondition(policy.source.repositoryId === 1091169653, "POLICY_IDENTITY", "Source repository ID differs.");
  requireCondition(policy.source.repositoryNodeId === "R_kgDOQQntdQ", "POLICY_IDENTITY", "Source repository node ID differs.");
  requireCondition(policy.source.defaultBranch === "main", "POLICY_IDENTITY", "Source default branch differs.");
  requireCondition(policy.source.workflowPath === ".github/workflows/public-skills-merge-queue-barrier.yml", "POLICY_IDENTITY", "Barrier workflow path differs.");
  requireCondition(policy.source.controllerPath === "scripts/public-skills-merge-queue-barrier.mjs", "POLICY_IDENTITY", "Barrier controller path differs.");
  requireCondition(policy.source.policyPath === "policies/public-skills-merge-queue-barrier.json", "POLICY_IDENTITY", "Barrier policy path differs.");
  for (const field of ["workflowPath", "controllerPath", "policyPath"]) validateSafeSourcePath(policy.source[field], `policy.source.${field}`);
  requireExactKeys(policy.source.protectedSourceBundle, ["relation", "runtimeRevisionInput"], "policy.source.protectedSourceBundle");
  requireCondition(
    JSON.stringify(policy.source.protectedSourceBundle) === JSON.stringify({
      relation: "same-protected-source-commit",
      runtimeRevisionInput: "github.workflow_sha",
    }),
    "POLICY_IDENTITY",
    "Barrier protected-source identity differs from the shared source contract.",
  );

  requireExactKeys(policy.target, ["organization", "repositoryId", "repositoryNodeId", "allowedRepositories", "defaultBranch"], "policy.target");
  requireCondition(policy.target.organization === "SylphxAI", "POLICY_IDENTITY", "Target organization differs.");
  requireCondition(policy.target.repositoryId === 1297840366, "POLICY_IDENTITY", "Target repository ID differs.");
  requireCondition(policy.target.repositoryNodeId === "R_kgDOTVt47g", "POLICY_IDENTITY", "Target repository node ID differs.");
  requireCondition(policy.target.defaultBranch === "main", "POLICY_IDENTITY", "Target default branch differs.");
  requireCondition(
    JSON.stringify(policy.target.allowedRepositories) === JSON.stringify(["SylphxAI/skills", "SylphxAI/skills-public-cleanroom"]),
    "POLICY_IDENTITY",
    "Target current/final repository names differ.",
  );

  requireExactKeys(policy.externalAdmission, ["rulesetId", "rulesetName", "rulesetSource", "sourceRepositoryId", "sourceWorkflowPath", "sourceRef", "requiredCheck", "terminalConclusions", "requiredConclusion", "ruleSuiteObservation"], "policy.externalAdmission");
  requireCondition(policy.externalAdmission.rulesetId === 18831380, "POLICY_IDENTITY", "External admission ruleset ID differs.");
  requireCondition(policy.externalAdmission.rulesetName === "public-skills-external-admission", "POLICY_IDENTITY", "External admission ruleset name differs.");
  requireCondition(policy.externalAdmission.rulesetSource === "SylphxAI", "POLICY_IDENTITY", "External admission ruleset source differs.");
  requireCondition(policy.externalAdmission.sourceRepositoryId === 1091169653, "POLICY_IDENTITY", "External admission source repository ID differs.");
  requireCondition(policy.externalAdmission.sourceWorkflowPath === ".github/workflows/public-skills-admission.yml", "POLICY_IDENTITY", "External admission workflow path differs.");
  requireCondition(policy.externalAdmission.sourceRef === "main", "POLICY_IDENTITY", "External admission source ref differs.");
  requireCondition(policy.externalAdmission.requiredCheck === "public-skills-external-admission/pass", "POLICY_IDENTITY", "External admission check differs.");
  requireCondition(JSON.stringify(policy.externalAdmission.terminalConclusions) === JSON.stringify(["failure", "success"]), "POLICY_IDENTITY", "External admission terminal conclusions differ.");
  requireCondition(policy.externalAdmission.requiredConclusion === "success", "POLICY_IDENTITY", "External admission required conclusion differs.");
  requireExactKeys(policy.externalAdmission.ruleSuiteObservation, ["ref", "ruleSourceType", "ruleType", "admittedEnforcementsAtDecision", "admittedResultsAtDecision", "requiredEnforcement", "requiredResult", "admittedAggregateResultsAtDecision"], "policy.externalAdmission.ruleSuiteObservation");
  requireCondition(
    JSON.stringify(policy.externalAdmission.ruleSuiteObservation) === JSON.stringify({
      ref: "refs/heads/main",
      ruleSourceType: "ruleset",
      ruleType: "workflows",
      admittedEnforcementsAtDecision: ["evaluate", "active"],
      admittedResultsAtDecision: ["fail", "pass"],
      requiredEnforcement: "active",
      requiredResult: "pass",
      admittedAggregateResultsAtDecision: [null, "fail", "pass"],
    }),
    "POLICY_IDENTITY",
    "External admission rule-suite authority differs.",
  );

  requireExactKeys(policy.barrier, ["rulesetName", "requiredCheck", "initialEnforcement", "requiredEnforcement", "bypassActors", "refInclude", "refExclude", "doNotEnforceOnCreate"], "policy.barrier");
  requireCondition(policy.barrier.rulesetName === "public-skills-merge-queue-barrier", "POLICY_IDENTITY", "Barrier ruleset name differs.");
  requireCondition(policy.barrier.requiredCheck === "public-skills-merge-queue-barrier/pass", "POLICY_IDENTITY", "Barrier check name differs.");
  requireCondition(policy.barrier.initialEnforcement === "evaluate", "POLICY_IDENTITY", "Barrier must begin in evaluate mode.");
  requireCondition(policy.barrier.requiredEnforcement === "active", "POLICY_IDENTITY", "Barrier terminal enforcement must be active.");
  requireCondition(Array.isArray(policy.barrier.bypassActors) && policy.barrier.bypassActors.length === 0, "POLICY_IDENTITY", "Barrier bypass actors must be empty.");
  requireCondition(JSON.stringify(policy.barrier.refInclude) === JSON.stringify(["~DEFAULT_BRANCH"]), "POLICY_IDENTITY", "Barrier ref include differs.");
  requireCondition(Array.isArray(policy.barrier.refExclude) && policy.barrier.refExclude.length === 0, "POLICY_IDENTITY", "Barrier ref exclude differs.");
  requireCondition(policy.barrier.doNotEnforceOnCreate === false, "POLICY_IDENTITY", "Barrier creation enforcement differs.");

  requireExactKeys(policy.events, ["mergeGroupRefPattern", "pullRequestRefPattern"], "policy.events");
  requireCondition(policy.events.mergeGroupRefPattern === "^refs/heads/gh-readonly-queue/main/pr-([1-9][0-9]*)-([0-9a-f]+)$", "POLICY_IDENTITY", "Merge-group ref contract differs.");
  requireCondition(policy.events.pullRequestRefPattern === "^refs/pull/([1-9][0-9]*)/merge$", "POLICY_IDENTITY", "Pull-request ref contract differs.");

  requireExactKeys(policy.polling, ["externalCheckAttempts", "externalCheckIntervalMilliseconds", "ruleSuiteAttempts", "ruleSuiteIntervalMilliseconds"], "policy.polling");
  requireInteger(policy.polling.externalCheckAttempts, "policy.polling.externalCheckAttempts", { maximum: 180 });
  requireInteger(policy.polling.externalCheckIntervalMilliseconds, "policy.polling.externalCheckIntervalMilliseconds", { maximum: 10_000 });
  requireInteger(policy.polling.ruleSuiteAttempts, "policy.polling.ruleSuiteAttempts", { maximum: 60 });
  requireInteger(policy.polling.ruleSuiteIntervalMilliseconds, "policy.polling.ruleSuiteIntervalMilliseconds", { maximum: 5_000 });
  return policy;
}

function validateRuntimeIdentity(runtime, policy) {
  requireExactKeys(runtime, ["repository", "repositoryId", "repositoryNodeId", "eventName", "eventRef", "candidateSha", "sourceSha"], "runtime");
  requireCondition(policy.target.allowedRepositories.includes(runtime.repository), "TARGET_IDENTITY", "Runtime repository name is not an admitted current/final target name.");
  requireCondition(Number(runtime.repositoryId) === policy.target.repositoryId, "TARGET_IDENTITY", "Runtime repository ID differs.");
  requireCondition(runtime.repositoryNodeId === policy.target.repositoryNodeId, "TARGET_IDENTITY", "Runtime repository node ID differs.");
  requireCondition(runtime.eventName === "pull_request" || runtime.eventName === "merge_group", "EVENT_IDENTITY", "Runtime event is not pull_request or merge_group.");
  requireString(runtime.eventRef, "runtime.eventRef");
  requireSha(runtime.candidateSha, "runtime.candidateSha");
  requireSha(runtime.sourceSha, "runtime.sourceSha");
}

function validateEventRepository(event, runtime, policy) {
  const repository = event?.repository;
  requireCondition(repository && typeof repository === "object", "EVENT_IDENTITY", "Event repository is absent.");
  requireCondition(repository.id === policy.target.repositoryId, "EVENT_IDENTITY", "Event repository ID differs.");
  requireCondition(repository.node_id === policy.target.repositoryNodeId, "EVENT_IDENTITY", "Event repository node ID differs.");
  requireCondition(repository.full_name === runtime.repository, "EVENT_IDENTITY", "Event repository name differs from runtime.");
  requireCondition(repository.default_branch === policy.target.defaultBranch, "EVENT_IDENTITY", "Event default branch differs.");
}

export function validateInvocation({ event, runtime, policy }) {
  validatePolicy(policy);
  validateRuntimeIdentity(runtime, policy);
  validateEventRepository(event, runtime, policy);

  if (runtime.eventName === "pull_request") {
    const pull = event.pull_request;
    requireCondition(pull && typeof pull === "object", "EVENT_IDENTITY", "Pull-request payload is absent.");
    const number = requireInteger(pull.number, "event.pull_request.number");
    const refMatch = new RegExp(policy.events.pullRequestRefPattern).exec(runtime.eventRef);
    requireCondition(refMatch && Number(refMatch[1]) === number, "EVENT_IDENTITY", "Pull-request event ref does not bind the payload number.");
    requireCondition(pull.base?.repo?.id === policy.target.repositoryId && pull.head?.repo?.id === policy.target.repositoryId, "EVENT_IDENTITY", "Pull-request head and base must remain in the target repository.");
    requireCondition(pull.base?.ref === policy.target.defaultBranch, "EVENT_IDENTITY", "Pull-request base branch differs.");
    requireSha(pull.base?.sha, "event.pull_request.base.sha");
    requireSha(pull.head?.sha, "event.pull_request.head.sha");
    requireString(pull.head?.ref, "event.pull_request.head.ref", /^[A-Za-z0-9._/-]+$/);
    requireCondition(pull.merge_commit_sha === runtime.candidateSha, "EVENT_IDENTITY", "Pull-request synthetic merge SHA differs from runtime candidate SHA.");
    return {
      eventName: "pull_request",
      pullRequestNumber: number,
      baseSha: pull.base.sha,
      headSha: pull.head.sha,
      headRef: pull.head.ref,
      candidateSha: runtime.candidateSha,
    };
  }

  requireCondition(event.action === "checks_requested", "EVENT_IDENTITY", "Merge-group action must be checks_requested.");
  const mergeGroup = event.merge_group;
  requireCondition(mergeGroup && typeof mergeGroup === "object", "EVENT_IDENTITY", "Merge-group payload is absent.");
  requireSha(mergeGroup.base_sha, "event.merge_group.base_sha");
  requireSha(mergeGroup.head_sha, "event.merge_group.head_sha");
  requireCondition(mergeGroup.base_ref === `refs/heads/${policy.target.defaultBranch}`, "EVENT_IDENTITY", "Merge-group base ref differs.");
  requireCondition(mergeGroup.head_ref === runtime.eventRef, "EVENT_IDENTITY", "Merge-group head ref differs from runtime event ref.");
  requireCondition(mergeGroup.head_sha === runtime.candidateSha, "EVENT_IDENTITY", "Merge-group head SHA differs from runtime candidate SHA.");
  const refMatch = new RegExp(policy.events.mergeGroupRefPattern).exec(mergeGroup.head_ref);
  requireCondition(refMatch, "EVENT_IDENTITY", "Merge-group head ref is not the provider queue-ref shape.");
  return {
    eventName: "merge_group",
    pullRequestNumber: Number(refMatch[1]),
    baseSha: mergeGroup.base_sha,
    headSha: mergeGroup.head_sha,
    headRef: mergeGroup.head_ref,
    candidateSha: runtime.candidateSha,
  };
}

function repositoryParts(nameWithOwner) {
  const match = /^([^/]+)\/([^/]+)$/.exec(nameWithOwner);
  requireCondition(match, "TARGET_IDENTITY", "Target repository name is not owner/name.");
  return { owner: match[1], name: match[2] };
}

function normalizeQueueEntry(value, pullRequestId, pullRequestNumber) {
  if (value === null) return null;
  requireCondition(value && typeof value === "object", "PROVIDER_SHAPE", "Merge queue entry is not an object.");
  requireNodeId(value.id, "mergeQueueEntry.id");
  requireInteger(value.position, "mergeQueueEntry.position");
  requireString(value.state, "mergeQueueEntry.state", /^[A-Z_]+$/);
  if (value.baseCommit !== null) requireSha(value.baseCommit?.oid, "mergeQueueEntry.baseCommit.oid");
  if (value.headCommit !== null) requireSha(value.headCommit?.oid, "mergeQueueEntry.headCommit.oid");
  requireCondition(value.pullRequest?.id === pullRequestId && value.pullRequest?.number === pullRequestNumber, "PROVIDER_IDENTITY", "Merge queue entry does not bind the pull request.");
  return {
    id: value.id,
    position: value.position,
    state: value.state,
    baseCommit: value.baseCommit?.oid ?? null,
    headCommit: value.headCommit?.oid ?? null,
  };
}

export function normalizeRepositorySnapshot(payload, context, policy) {
  requireCondition(payload && typeof payload === "object" && payload.data && typeof payload.data === "object" && !Array.isArray(payload.data), "PROVIDER_SHAPE", "GraphQL snapshot response is invalid.");
  requireCondition(!payload.errors, "PROVIDER_SHAPE", "GraphQL snapshot response contains errors.");
  const repository = payload.data.repository;
  requireCondition(repository && typeof repository === "object", "PROVIDER_SHAPE", "GraphQL target repository is absent.");
  requireCondition(repository.id === policy.target.repositoryNodeId, "PROVIDER_IDENTITY", "GraphQL repository node ID differs.");
  requireCondition(policy.target.allowedRepositories.includes(repository.nameWithOwner), "PROVIDER_IDENTITY", "GraphQL repository name differs.");
  requireCondition(repository.defaultBranchRef?.name === policy.target.defaultBranch, "PROVIDER_IDENTITY", "GraphQL default branch name differs.");
  const mainSha = requireSha(repository.defaultBranchRef?.target?.oid, "repository.defaultBranchRef.target.oid");
  const pull = repository.pullRequest;
  requireCondition(pull && typeof pull === "object", "PROVIDER_SHAPE", "GraphQL pull request is absent.");
  requireNodeId(pull.id, "pullRequest.id");
  requireCondition(pull.number === context.pullRequestNumber, "PROVIDER_IDENTITY", "GraphQL pull-request number differs.");
  requireCondition(pull.state === "OPEN" && pull.merged === false, "PROVIDER_IDENTITY", "Pull request must remain open and unmerged.");
  requireCondition(pull.baseRefName === policy.target.defaultBranch, "PROVIDER_IDENTITY", "GraphQL pull-request base differs.");
  requireString(pull.headRefName, "pullRequest.headRefName", /^[A-Za-z0-9._/-]+$/);
  requireSha(pull.headRefOid, "pullRequest.headRefOid");
  requireCondition(pull.baseRepository?.id === policy.target.repositoryNodeId, "PROVIDER_IDENTITY", "GraphQL base repository node ID differs.");
  requireCondition(pull.headRepository?.id === policy.target.repositoryNodeId, "PROVIDER_IDENTITY", "GraphQL head repository node ID differs.");
  requireCondition(policy.target.allowedRepositories.includes(pull.baseRepository?.nameWithOwner) && pull.baseRepository?.nameWithOwner === repository.nameWithOwner, "PROVIDER_IDENTITY", "GraphQL base repository name differs.");
  requireCondition(pull.headRepository?.nameWithOwner === repository.nameWithOwner, "PROVIDER_IDENTITY", "GraphQL head repository name differs.");
  if (context.eventName === "pull_request") {
    requireCondition(pull.headRefName === context.headRef && pull.headRefOid === context.headSha, "PROVIDER_IDENTITY", "GraphQL pull-request head differs from event payload.");
  }
  const autoMerge = pull.autoMergeRequest === null
    ? null
    : { enabledAt: requireString(pull.autoMergeRequest?.enabledAt, "pullRequest.autoMergeRequest.enabledAt") };
  const queueEntry = normalizeQueueEntry(pull.mergeQueueEntry, pull.id, pull.number);
  return {
    repository: { id: repository.id, name: repository.nameWithOwner, defaultBranch: policy.target.defaultBranch, mainSha },
    pullRequest: {
      id: pull.id,
      number: pull.number,
      headRef: pull.headRefName,
      headSha: pull.headRefOid,
      state: pull.state,
      merged: pull.merged,
      autoMerge,
      queueEntry,
    },
  };
}

function normalizeCheckRun(item, policy, context) {
  requireCondition(item && typeof item === "object", "PROVIDER_SHAPE", "Check run is not an object.");
  requireInteger(item.id, "checkRun.id");
  requireCondition(item.name === policy.externalAdmission.requiredCheck, "PROVIDER_IDENTITY", "External check name differs.");
  requireCondition(item.head_sha === context.candidateSha, "PROVIDER_IDENTITY", "External check head SHA differs.");
  requireCondition(item.app?.id === GITHUB_ACTIONS_APP.id && item.app?.slug === GITHUB_ACTIONS_APP.slug && item.app?.name === GITHUB_ACTIONS_APP.name, "PROVIDER_IDENTITY", "External check producer is not the GitHub Actions App.");
  requireCondition(
    item.status === "completed" && policy.externalAdmission.terminalConclusions.includes(item.conclusion),
    "PROVIDER_STATE",
    "External check is not completed with an admitted terminal conclusion.",
  );
  const repositoryPattern = policy.target.allowedRepositories.map(escapeRegExp).join("|");
  const detailsPattern = new RegExp(`^https://github\\.com/(?:${repositoryPattern})/actions/runs/([1-9][0-9]*)(?:/job/[1-9][0-9]*)?/?$`);
  const details = detailsPattern.exec(item.details_url);
  requireCondition(details, "PROVIDER_IDENTITY", "External check details URL differs.");
  return {
    id: item.id,
    name: item.name,
    headSha: item.head_sha,
    status: item.status,
    conclusion: item.conclusion,
    runId: Number(details[1]),
    detailsUrl: item.details_url,
    app: GITHUB_ACTIONS_APP,
  };
}

function normalizeWorkflowRun(run, jobsPayload, check, policy, context, sourceSha) {
  requireCondition(run && typeof run === "object", "PROVIDER_SHAPE", "External workflow run is not an object.");
  requireCondition(run.id === check.runId, "PROVIDER_IDENTITY", "External workflow run ID differs.");
  requireCondition(run.repository?.id === policy.target.repositoryId && policy.target.allowedRepositories.includes(run.repository?.full_name), "PROVIDER_IDENTITY", "External workflow target repository differs.");
  requireCondition(run.head_sha === context.candidateSha && run.event === context.eventName, "PROVIDER_IDENTITY", "External workflow head/event differs.");
  requireCondition(run.status === "completed" && run.conclusion === check.conclusion, "PROVIDER_STATE", "External workflow conclusion differs from its required check.");
  requireInteger(run.run_attempt, "workflowRun.run_attempt");
  requireString(run.created_at, "workflowRun.created_at");
  requireString(run.updated_at, "workflowRun.updated_at");
  requireCondition(
    run.path === policy.externalAdmission.sourceWorkflowPath || run.path === `${policy.externalAdmission.sourceWorkflowPath}@${sourceSha}`,
    "PROVIDER_IDENTITY",
    "External workflow run path differs.",
  );
  requireCondition(jobsPayload && typeof jobsPayload === "object" && Array.isArray(jobsPayload.jobs), "PROVIDER_SHAPE", "External workflow jobs response differs.");
  requireCondition(jobsPayload.total_count === jobsPayload.jobs.length && jobsPayload.jobs.length <= 100, "PROVIDER_SHAPE", "External workflow jobs response is incomplete.");
  requireCondition(jobsPayload.jobs.every((job) => job && typeof job === "object" && job.head_sha === context.candidateSha), "PROVIDER_IDENTITY", "External workflow contains a foreign job head.");
  const matches = jobsPayload.jobs.filter((job) => job.name === policy.externalAdmission.requiredCheck);
  requireCondition(matches.length === 1, "PROVIDER_IDENTITY", "External workflow does not contain exactly one admission job.");
  requireCondition(matches[0].id === check.id && matches[0].status === "completed" && matches[0].conclusion === check.conclusion, "PROVIDER_STATE", "External workflow admission job differs from the check run.");
  return {
    id: run.id,
    runAttempt: run.run_attempt,
    path: run.path,
    event: run.event,
    headSha: run.head_sha,
    status: run.status,
    conclusion: run.conclusion,
    createdAt: run.created_at,
    updatedAt: run.updated_at,
    jobId: matches[0].id,
  };
}

function normalizeEffectiveRuleset(value, policy, sourceSha) {
  requireCondition(value && typeof value === "object", "PROVIDER_SHAPE", "Effective external ruleset is not an object.");
  requireCondition(value.id === policy.externalAdmission.rulesetId, "RULESET_DRIFT", "Effective external ruleset ID differs.");
  requireCondition(value.name === policy.externalAdmission.rulesetName, "RULESET_DRIFT", "Effective external ruleset name differs.");
  requireCondition(value.source_type === "Organization" && value.source === policy.externalAdmission.rulesetSource, "RULESET_DRIFT", "Effective external ruleset owner differs.");
  requireCondition(value.target === "branch" && value.enforcement === "active", "RULESET_DRIFT", "Effective external ruleset is not active branch policy.");
  requireCondition(Array.isArray(value.bypass_actors) && value.bypass_actors.length === 0, "RULESET_DRIFT", "Effective external ruleset has bypass actors.");
  requireCondition(value.current_user_can_bypass === "never", "RULESET_DRIFT", "Effective external ruleset does not prove zero actor bypass.");
  const conditions = value.conditions;
  requireCondition(conditions && typeof conditions === "object", "RULESET_DRIFT", "Effective external ruleset conditions are absent.");
  const conditionKeys = sorted(Object.keys(conditions));
  requireCondition(
    JSON.stringify(conditionKeys) === JSON.stringify(["ref_name"]) || JSON.stringify(conditionKeys) === JSON.stringify(["ref_name", "repository_id"]),
    "RULESET_DRIFT",
    "Effective external ruleset conditions differ.",
  );
  requireCondition(JSON.stringify(conditions.ref_name) === JSON.stringify({ exclude: [], include: ["~DEFAULT_BRANCH"] }), "RULESET_DRIFT", "Effective external ruleset ref selector differs.");
  if (conditions.repository_id) requireCondition(JSON.stringify(conditions.repository_id) === JSON.stringify({ repository_ids: [policy.target.repositoryId] }), "RULESET_DRIFT", "Effective external ruleset repository selector differs.");
  requireCondition(Array.isArray(value.rules) && value.rules.length === 1, "RULESET_DRIFT", "Effective external ruleset must contain one workflow rule.");
  const rule = value.rules[0];
  requireCondition(rule?.type === "workflows", "RULESET_DRIFT", "Effective external ruleset rule type differs.");
  requireCondition(
    JSON.stringify(rule.parameters) === JSON.stringify({
      do_not_enforce_on_create: false,
      workflows: [{
        repository_id: policy.externalAdmission.sourceRepositoryId,
        path: policy.externalAdmission.sourceWorkflowPath,
        sha: sourceSha,
        ref: policy.externalAdmission.sourceRef,
      }],
    }),
    "RULESET_DRIFT",
    "Effective external workflow identity differs.",
  );
  return {
    id: value.id,
    name: value.name,
    sourceType: value.source_type,
    source: value.source,
    target: value.target,
    enforcement: value.enforcement,
    currentUserCanBypass: value.current_user_can_bypass,
    conditions: value.conditions,
    rules: value.rules,
  };
}

export class GitHubApi {
  constructor(token, { fetchImplementation = globalThis.fetch, timeoutMilliseconds = 20_000 } = {}) {
    requireCondition(typeof token === "string" && token.length > 0 && !/[\r\n]/.test(token), "TOKEN", "GITHUB_TOKEN is absent or malformed.");
    requireCondition(typeof fetchImplementation === "function", "RUNTIME", "A fetch implementation is required.");
    this.token = token;
    this.fetchImplementation = fetchImplementation;
    this.timeoutMilliseconds = timeoutMilliseconds;
  }

  async request(method, path, body = undefined) {
    requireCondition(typeof path === "string" && path.startsWith("/") && !path.startsWith("//") && !path.includes("://"), "PROVIDER_ENDPOINT", "GitHub API path is not fixed-host relative.");
    let response;
    try {
      response = await this.fetchImplementation(`${API_ROOT}${path}`, {
        method,
        redirect: "error",
        signal: AbortSignal.timeout(this.timeoutMilliseconds),
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${this.token}`,
          "Content-Type": "application/json",
          "User-Agent": "SylphxAI-public-skills-merge-queue-barrier/1",
          "X-GitHub-Api-Version": API_VERSION,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (error) {
      reject("PROVIDER_REQUEST", `GitHub API ${method} ${path} failed before a response: ${error instanceof Error ? error.name : "unknown"}.`);
    }
    requireCondition(response.ok, "PROVIDER_REQUEST", `GitHub API ${method} ${path} returned HTTP ${response.status}.`);
    let payload;
    try {
      payload = await response.json();
    } catch {
      reject("PROVIDER_SHAPE", `GitHub API ${method} ${path} returned non-JSON.`);
    }
    return payload;
  }

  async rest(path) {
    return this.request("GET", path);
  }

  async graphql(query, variables) {
    return this.request("POST", "/graphql", { query, variables });
  }
}

async function readSnapshot(api, context, policy, runtime) {
  const { owner, name } = repositoryParts(runtime.repository);
  return normalizeRepositorySnapshot(
    await api.graphql(REPOSITORY_SNAPSHOT_QUERY, { owner, name, number: context.pullRequestNumber }),
    context,
    policy,
  );
}

function validateMergeGroupSnapshot(snapshot, context) {
  requireCondition(snapshot.repository.mainSha === context.baseSha, "QUEUE_RACE", "Default branch changed from the merge-group base SHA.");
  requireCondition(snapshot.pullRequest.queueEntry !== null, "QUEUE_RACE", "Merge-group pull request is no longer in the queue.");
  if (snapshot.pullRequest.queueEntry.baseCommit !== null) {
    requireCondition(snapshot.pullRequest.queueEntry.baseCommit === context.baseSha, "QUEUE_RACE", "Merge queue entry base commit differs from the event.");
  }
  if (snapshot.pullRequest.queueEntry.headCommit !== null) {
    requireCondition(snapshot.pullRequest.queueEntry.headCommit === context.candidateSha, "QUEUE_RACE", "Merge queue entry candidate differs from the event.");
  }
}

async function waitForExternalAdmission(api, context, policy, sourceSha, sleep) {
  const encodedSha = encodeURIComponent(context.candidateSha);
  for (let attempt = 1; attempt <= policy.polling.externalCheckAttempts; attempt += 1) {
    const payload = await api.rest(`/repositories/${policy.target.repositoryId}/commits/${encodedSha}/check-runs?filter=latest&per_page=100&page=1`);
    requireCondition(payload && typeof payload === "object" && Array.isArray(payload.check_runs), "PROVIDER_SHAPE", "Check-runs response differs.");
    requireCondition(payload.total_count === payload.check_runs.length && payload.check_runs.length <= 100, "PROVIDER_SHAPE", "Check-runs response is incomplete.");
    const matches = payload.check_runs.filter((item) => item?.name === policy.externalAdmission.requiredCheck);
    requireCondition(matches.length <= 1, "PROVIDER_IDENTITY", "More than one external admission check exists for the exact candidate.");
    if (matches.length === 1 && matches[0].status === "completed") {
      const check = normalizeCheckRun(matches[0], policy, context);
      const run = await api.rest(`/repositories/${policy.target.repositoryId}/actions/runs/${check.runId}`);
      const jobs = await api.rest(`/repositories/${policy.target.repositoryId}/actions/runs/${check.runId}/jobs?filter=latest&per_page=100&page=1`);
      const workflowRun = normalizeWorkflowRun(run, jobs, check, policy, context, sourceSha);
      return { attempt, check, workflowRun, digest: canonicalDigest({ check, workflowRun }) };
    }
    if (attempt < policy.polling.externalCheckAttempts) await sleep(policy.polling.externalCheckIntervalMilliseconds);
  }
  reject("EXTERNAL_ADMISSION_TIMEOUT", "Exact external admission check did not reach completed success/failure inside the bounded wait.");
}

function normalizeCandidateRuleSuite(value, context, policy) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), "PROVIDER_SHAPE", "Candidate rule suite is not an object.");
  requireInteger(value.id, "ruleSuite.id");
  requireCondition(value.repository_id === policy.target.repositoryId, "PROVIDER_IDENTITY", "Candidate rule-suite repository differs.");
  requireCondition(value.ref === policy.externalAdmission.ruleSuiteObservation.ref, "PROVIDER_IDENTITY", "Candidate rule-suite target ref differs.");
  requireCondition(value.after_sha === context.candidateSha, "PROVIDER_IDENTITY", "Candidate rule-suite after SHA differs.");
  if (value.before_sha !== null && value.before_sha !== undefined) requireSha(value.before_sha, "ruleSuite.before_sha");
  requireString(value.pushed_at, "ruleSuite.pushed_at");
  requireCondition(
    policy.externalAdmission.ruleSuiteObservation.admittedAggregateResultsAtDecision.includes(value.result),
    "PROVIDER_STATE",
    "Candidate rule-suite aggregate is bypassed or outside the admitted in-flight result set.",
  );
  requireCondition(Array.isArray(value.rule_evaluations), "PROVIDER_SHAPE", "Candidate rule-suite evaluations are absent.");
  const identityCandidates = value.rule_evaluations.filter((item) => (
    item
    && typeof item === "object"
    && !Array.isArray(item)
    && item.rule_source
    && typeof item.rule_source === "object"
    && !Array.isArray(item.rule_source)
    && (
      item.rule_source.id === policy.externalAdmission.rulesetId
      || item.rule_source.name === policy.externalAdmission.rulesetName
    )
  ));
  requireCondition(identityCandidates.length === 1, "PROVIDER_IDENTITY", "Candidate rule suite must contain exactly one external-admission ruleset identity.");
  const external = identityCandidates[0];
  requireCondition(
    external.rule_source.id === policy.externalAdmission.rulesetId
      && external.rule_source.name === policy.externalAdmission.rulesetName
      && external.rule_source.type === policy.externalAdmission.ruleSuiteObservation.ruleSourceType
      && external.rule_type === policy.externalAdmission.ruleSuiteObservation.ruleType,
    "PROVIDER_IDENTITY",
    "Candidate rule-suite external source identity differs.",
  );
  requireCondition(
    policy.externalAdmission.ruleSuiteObservation.admittedEnforcementsAtDecision.includes(external.enforcement),
    "PROVIDER_STATE",
    "Candidate rule-suite external enforcement is outside evaluate/active.",
  );
  requireCondition(
    policy.externalAdmission.ruleSuiteObservation.admittedResultsAtDecision.includes(external.result),
    "PROVIDER_STATE",
    "Candidate rule-suite external result is outside pass/fail.",
  );
  const source = external.rule_source;
  return {
    id: value.id,
    repositoryId: value.repository_id,
    beforeSha: value.before_sha ?? null,
    afterSha: value.after_sha,
    ref: value.ref,
    pushedAt: value.pushed_at,
    aggregateResult: value.result,
    externalRuleEvaluation: {
      ruleSource: { id: source.id, name: source.name, type: source.type },
      ruleType: external.rule_type,
      enforcement: external.enforcement,
      result: external.result,
    },
  };
}

async function waitForCandidateRuleSuite(api, context, policy, sleep) {
  const query = `ref=${encodeURIComponent(policy.externalAdmission.ruleSuiteObservation.ref)}&per_page=100&page=1`;
  for (let attempt = 1; attempt <= policy.polling.ruleSuiteAttempts; attempt += 1) {
    const summaries = await api.rest(`/repositories/${policy.target.repositoryId}/rulesets/rule-suites?${query}`);
    requireCondition(Array.isArray(summaries) && summaries.length <= 100, "PROVIDER_SHAPE", "Candidate rule-suite summary response differs.");
    const matches = summaries.filter((item) => item?.after_sha === context.candidateSha);
    requireCondition(matches.length <= 1, "PROVIDER_IDENTITY", "More than one rule suite exists for the exact candidate SHA.");
    if (matches.length === 1) {
      requireInteger(matches[0].id, "ruleSuite summary id");
      requireCondition(matches[0].repository_id === policy.target.repositoryId, "PROVIDER_IDENTITY", "Candidate rule-suite summary repository differs.");
      requireCondition(matches[0].ref === policy.externalAdmission.ruleSuiteObservation.ref, "PROVIDER_IDENTITY", "Candidate rule-suite summary ref differs.");
      const raw = await api.rest(`/repositories/${policy.target.repositoryId}/rulesets/rule-suites/${matches[0].id}`);
      const normalized = normalizeCandidateRuleSuite(raw, context, policy);
      requireCondition(
        matches[0].result === normalized.aggregateResult,
        "PROVIDER_STATE",
        "Candidate rule-suite summary/detail aggregate differs.",
      );
      return { attempt, suite: normalized, digest: canonicalDigest(normalized) };
    }
    if (attempt < policy.polling.ruleSuiteAttempts) await sleep(policy.polling.ruleSuiteIntervalMilliseconds);
  }
  reject("RULE_SUITE_TIMEOUT", "Exact candidate rule suite did not expose one terminal external ruleset evaluation inside the bounded wait.");
}

async function readExternalRulesetState(api, policy, sourceSha) {
  const summaries = await api.rest(`/repositories/${policy.target.repositoryId}/rulesets?includes_parents=true&per_page=100&page=1`);
  requireCondition(Array.isArray(summaries) && summaries.length <= 100, "PROVIDER_SHAPE", "Effective ruleset summary response differs.");
  const sameName = summaries.filter((item) => item?.name === policy.externalAdmission.rulesetName);
  requireCondition(sameName.every((item) => item.id === policy.externalAdmission.rulesetId), "RULESET_DRIFT", "A foreign effective ruleset reuses the external admission name.");
  const matches = summaries.filter((item) => item?.id === policy.externalAdmission.rulesetId);
  requireCondition(matches.length <= 1, "RULESET_DRIFT", "External admission ruleset appears more than once in effective state.");
  if (matches.length === 0) return { state: "not-effective", ruleset: null, digest: canonicalDigest([]) };
  const raw = await api.rest(`/repositories/${policy.target.repositoryId}/rulesets/${policy.externalAdmission.rulesetId}?includes_parents=true`);
  const ruleset = normalizeEffectiveRuleset(raw, policy, sourceSha);
  return { state: "active-effective", ruleset, digest: canonicalDigest(ruleset) };
}

function baseReport(policy, runtime, context, source) {
  return {
    schemaVersion: 3,
    kind: "sylphx-public-skills-merge-queue-barrier-report",
    status: "pending",
    source,
    target: {
      repository: runtime.repository,
      repositoryId: policy.target.repositoryId,
      repositoryNodeId: policy.target.repositoryNodeId,
    },
    event: context,
    externalAdmission: null,
    externalRuleSuite: null,
    effectiveRuleset: null,
    queueMutation: {
      owner: "github-provider",
      attempted: false,
    },
    decision: null,
  };
}

export async function executeBarrier({ event, runtime, policy, source }, { api, sleep = (milliseconds) => new Promise((resolveSleep) => setTimeout(resolveSleep, milliseconds)) }) {
  const context = validateInvocation({ event, runtime, policy });
  requireCondition(
    JSON.stringify(source) === JSON.stringify({
      repository: policy.source.repository,
      commit: runtime.sourceSha,
      protectedSourceBundle: policy.source.protectedSourceBundle,
    }),
    "SOURCE_IDENTITY",
    "Barrier report source does not bind github.workflow_sha and the shared protected-source relation.",
  );
  requireCondition(api && typeof api.graphql === "function" && typeof api.rest === "function", "RUNTIME", "GitHub provider adapter is invalid.");
  const report = baseReport(policy, runtime, context, source);
  const initial = await readSnapshot(api, context, policy, runtime);
  report.initialSnapshot = { value: initial, digest: canonicalDigest(initial) };

  if (context.eventName === "pull_request") {
    report.status = "pass";
    report.decision = {
      action: "pass-pull-request-identity",
      reason: "Pull-request events validate exact target/same-repository identity and never mutate queue state.",
    };
    return report;
  }

  validateMergeGroupSnapshot(initial, context);
  const external = await waitForExternalAdmission(api, context, policy, runtime.sourceSha, sleep);
  report.externalAdmission = external;
  const externalSucceeded = external.check.conclusion === policy.externalAdmission.requiredConclusion;
  report.externalRuleSuite = await waitForCandidateRuleSuite(api, context, policy, sleep);
  const externalEvaluation = report.externalRuleSuite.suite.externalRuleEvaluation;
  const expectedRuleResult = external.check.conclusion === "success" ? "pass" : "fail";
  requireCondition(
    externalEvaluation.result === expectedRuleResult,
    "PROVIDER_STATE",
    "External check conclusion and exact rule-suite result disagree.",
  );
  const pre = await readSnapshot(api, context, policy, runtime);
  validateMergeGroupSnapshot(pre, context);
  report.preDecisionSnapshot = { value: pre, digest: canonicalDigest(pre) };

  const firstRuleset = await readExternalRulesetState(api, policy, runtime.sourceSha);
  report.effectiveRuleset = { first: firstRuleset };
  let confirmation = null;
  if (firstRuleset.state === "active-effective") {
    confirmation = await readExternalRulesetState(api, policy, runtime.sourceSha);
    report.effectiveRuleset.confirmation = confirmation;
  }
  const ruleSuiteAuthorized = (
    externalEvaluation.enforcement === policy.externalAdmission.ruleSuiteObservation.requiredEnforcement
    && externalEvaluation.result === policy.externalAdmission.ruleSuiteObservation.requiredResult
  );
  const rulesetConfirmed = (
    firstRuleset.state === "active-effective"
    && confirmation?.state === "active-effective"
    && confirmation.digest === firstRuleset.digest
  );
  report.decision = {
    action: externalSucceeded && ruleSuiteAuthorized && rulesetConfirmed
      ? "pass-active-admission"
      : "reject-merge-group",
    admitted: externalSucceeded && ruleSuiteAuthorized && rulesetConfirmed,
    mutationAttempted: false,
    queueOwner: "github-provider",
    requirements: {
      externalConclusion: {
        required: policy.externalAdmission.requiredConclusion,
        observed: external.check.conclusion,
        satisfied: externalSucceeded,
      },
      externalRuleSuite: {
        required: "exact-candidate-active-workflows-pass",
        ruleSuiteId: report.externalRuleSuite.suite.id,
        aggregateResultAtDecision: report.externalRuleSuite.suite.aggregateResult,
        observedEnforcement: externalEvaluation.enforcement,
        observedResult: externalEvaluation.result,
        satisfied: ruleSuiteAuthorized,
      },
      externalRuleset: {
        required: "active-effective-confirmed",
        first: firstRuleset.state,
        confirmation: confirmation?.state ?? null,
        satisfied: rulesetConfirmed,
      },
    },
    reason: externalSucceeded && ruleSuiteAuthorized && rulesetConfirmed
      ? "The exact source-pinned external ruleset is active/effective on two identical reads, its exact candidate rule evaluation is active/pass, and its source-bound check succeeded."
      : "The merge group is rejected because exact external check success, exact candidate active/pass rule-suite authority, and confirmed active/effective ruleset state are all mandatory; GitHub owns any queue removal.",
  };
  report.status = report.decision.admitted ? "pass" : "fail";
  return report;
}

function parseArguments(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    requireCondition(flag?.startsWith("--") && value !== undefined, "CLI_ARGUMENT", "CLI arguments must be --name value pairs.");
    const name = flag.slice(2);
    requireCondition(!Object.hasOwn(values, name), "CLI_ARGUMENT", `Duplicate CLI argument --${name}.`);
    values[name] = value;
  }
  const required = ["policy", "event", "source-root", "source-sha", "repository", "repository-id", "repository-node-id", "event-name", "event-ref", "candidate-sha", "report"];
  for (const name of required) requireCondition(typeof values[name] === "string" && values[name].length > 0, "CLI_ARGUMENT", `Missing --${name}.`);
  requireCondition(Object.keys(values).length === required.length, "CLI_ARGUMENT", "Unknown CLI arguments are forbidden.");
  return values;
}

function git(root, args) {
  try {
    return execFileSync("git", ["-C", root, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], maxBuffer: 8 * 1024 * 1024 }).trim();
  } catch {
    reject("SOURCE_IDENTITY", `git ${args[0]} failed for the protected source checkout.`);
  }
}

function writeReport(path, report) {
  writeFileSync(path, `${JSON.stringify(report, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
}

export function reportExitCode(report) {
  requireCondition(
    report && (report.status === "pass" || report.status === "fail"),
    "REPORT_STATE",
    "Terminal barrier report status must be pass or fail.",
  );
  return report.status === "pass" ? 0 : 1;
}

export async function runCli(argv, environment = process.env) {
  let reportPath;
  try {
    const args = parseArguments(argv);
    reportPath = resolve(args.report);
    const sourceRoot = realpathSync(args["source-root"]);
    const sourceSha = requireSha(args["source-sha"], "source SHA");
    requireCondition(git(sourceRoot, ["rev-parse", "--verify", "HEAD^{commit}"]) === sourceSha, "SOURCE_IDENTITY", "Protected source checkout HEAD differs from github.workflow_sha.");
    const policyPath = resolve(args.policy);
    const eventPath = resolve(args.event);
    requireCondition(policyPath.startsWith(`${sourceRoot}/`), "SOURCE_IDENTITY", "Policy is outside the protected source checkout.");
    const policy = validatePolicy(JSON.parse(readFileSync(policyPath, "utf8")));
    const event = JSON.parse(readFileSync(eventPath, "utf8"));
    const runtime = {
      repository: args.repository,
      repositoryId: args["repository-id"],
      repositoryNodeId: args["repository-node-id"],
      eventName: args["event-name"],
      eventRef: args["event-ref"],
      candidateSha: args["candidate-sha"],
      sourceSha,
    };
    const api = new GitHubApi(environment.GITHUB_TOKEN);
    const report = await executeBarrier(
      {
        event,
        runtime,
        policy,
        source: {
          repository: policy.source.repository,
          commit: sourceSha,
          protectedSourceBundle: policy.source.protectedSourceBundle,
        },
      },
      { api },
    );
    writeReport(reportPath, report);
    const output = `${JSON.stringify(report, null, 2)}\n`;
    const exitCode = reportExitCode(report);
    if (exitCode === 0) {
      process.stdout.write(output);
      return exitCode;
    }
    process.stderr.write(output);
    return exitCode;
  } catch (error) {
    const failure = {
      schemaVersion: 3,
      kind: "sylphx-public-skills-merge-queue-barrier-report",
      status: "fail",
      error: {
        code: error instanceof BarrierError ? error.code : "UNEXPECTED_FAILURE",
        message: error instanceof Error ? error.message : String(error),
      },
    };
    if (reportPath) writeReport(reportPath, failure);
    process.stderr.write(`${JSON.stringify(failure, null, 2)}\n`);
    return 1;
  }
}

const isMain = process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url;
if (isMain) process.exitCode = await runCli(process.argv.slice(2));
