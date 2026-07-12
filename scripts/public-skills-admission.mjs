#!/usr/bin/env node

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync, realpathSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const MAX_GIT_OUTPUT_BYTES = 128 * 1024 * 1024;
const REQUIRED_POLICY_KEYS = [
  "schemaVersion",
  "kind",
  "source",
  "target",
  "history",
  "content",
  "skills",
  "expectedFiles",
];

export class AdmissionError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "AdmissionError";
    this.code = code;
  }
}

function reject(code, message) {
  throw new AdmissionError(code, message);
}

function requireCondition(condition, code, message) {
  if (!condition) reject(code, message);
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function sorted(values) {
  return [...values].sort();
}

function requireSortedUniqueStrings(values, field, { allowEmpty = false } = {}) {
  requireCondition(Array.isArray(values), "POLICY_SHAPE", `${field} must be an array.`);
  requireCondition(allowEmpty || values.length > 0, "POLICY_SHAPE", `${field} must not be empty.`);
  requireCondition(
    values.every((value) => typeof value === "string" && value.length > 0),
    "POLICY_SHAPE",
    `${field} must contain non-empty strings.`,
  );
  requireCondition(
    JSON.stringify(values) === JSON.stringify(sorted(new Set(values))),
    "POLICY_SHAPE",
    `${field} must be sorted and unique.`,
  );
}

function requireExactKeys(value, expected, field) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), "POLICY_SHAPE", `${field} must be an object.`);
  const actual = sorted(Object.keys(value));
  requireCondition(
    JSON.stringify(actual) === JSON.stringify(sorted(expected)),
    "POLICY_SHAPE",
    `${field} keys must be exactly: ${sorted(expected).join(", ")}.`,
  );
}

function validateSafePath(path, policy, context) {
  requireCondition(typeof path === "string" && path.length > 0, "UNSAFE_PATH", `${context} has an empty path.`);
  requireCondition(!path.startsWith("/") && !path.includes("\\"), "UNSAFE_PATH", `${context} has an unsafe path.`);
  requireCondition(path.normalize("NFC") === path, "UNSAFE_PATH", `${context} path is not NFC-normalized.`);
  if (policy.history.requireAsciiPaths) {
    requireCondition(/^[\x20-\x7e]+$/.test(path), "UNSAFE_PATH", `${context} path is not printable ASCII.`);
  }
  const segments = path.split("/");
  requireCondition(
    segments.every((segment) => segment && segment !== "." && segment !== ".."),
    "UNSAFE_PATH",
    `${context} has an unsafe path segment.`,
  );
  const forbidden = new Set(policy.content.forbiddenPathSegments.map((segment) => segment.toLowerCase()));
  requireCondition(
    segments.every((segment) => !forbidden.has(segment.toLowerCase())),
    "FORBIDDEN_PATH",
    `${context} uses a forbidden path segment.`,
  );
}

function validatePolicy(policy) {
  requireExactKeys(policy, REQUIRED_POLICY_KEYS, "policy");
  requireCondition(policy.schemaVersion === 1, "POLICY_SHAPE", "policy.schemaVersion must be 1.");
  requireCondition(
    policy.kind === "sylphx-public-skills-external-admission",
    "POLICY_SHAPE",
    "policy.kind is not supported.",
  );

  requireExactKeys(policy.source, ["repository", "repositoryId", "workflowPath", "validatorPath", "policyPath"], "policy.source");
  requireCondition(policy.source.repository === "SylphxAI/.github", "POLICY_SHAPE", "Source repository must be SylphxAI/.github.");
  requireCondition(Number.isSafeInteger(policy.source.repositoryId), "POLICY_SHAPE", "Source repository ID must be an integer.");
  for (const field of ["workflowPath", "validatorPath", "policyPath"]) validateSafePath(policy.source[field], policy, `policy.source.${field}`);

  requireExactKeys(
    policy.target,
    ["repositoryId", "repositoryNodeId", "allowedRepositories", "baseline", "approvedCommits", "approvedRefs", "dynamicEventHead"],
    "policy.target",
  );
  requireCondition(Number.isSafeInteger(policy.target.repositoryId), "POLICY_SHAPE", "Target repository ID must be an integer.");
  requireCondition(/^R_[A-Za-z0-9_-]+$/.test(policy.target.repositoryNodeId), "POLICY_SHAPE", "Target node ID is invalid.");
  requireSortedUniqueStrings(policy.target.allowedRepositories, "policy.target.allowedRepositories");
  requireExactKeys(policy.target.baseline, ["commit", "tree"], "policy.target.baseline");
  for (const [field, value] of [
    ["policy.target.baseline.commit", policy.target.baseline.commit],
    ["policy.target.baseline.tree", policy.target.baseline.tree],
  ]) requireCondition(/^[0-9a-f]{40}$/.test(value), "POLICY_SHAPE", `${field} must be a full SHA-1 object ID.`);

  requireCondition(Array.isArray(policy.target.approvedCommits) && policy.target.approvedCommits.length > 0, "POLICY_SHAPE", "Approved commits are required.");
  const approvedCommitIds = [];
  const approvedTrees = new Map();
  let approvedRootCount = 0;
  for (const record of policy.target.approvedCommits) {
    requireExactKeys(record, ["commit", "tree", "parents"], "approved commit");
    requireCondition(/^[0-9a-f]{40}$/.test(record.commit), "POLICY_SHAPE", "Approved commit IDs must be full SHA-1 IDs.");
    requireCondition(/^[0-9a-f]{40}$/.test(record.tree), "POLICY_SHAPE", `Approved commit ${record.commit} has an invalid tree ID.`);
    requireCondition(Array.isArray(record.parents) && record.parents.length <= 2, "POLICY_SHAPE", `Approved commit ${record.commit} parents are invalid.`);
    requireCondition(record.parents.every((parent) => /^[0-9a-f]{40}$/.test(parent)), "POLICY_SHAPE", `Approved commit ${record.commit} has an invalid parent.`);
    requireCondition(record.parents.length === new Set(record.parents).size, "POLICY_SHAPE", `Approved commit ${record.commit} repeats a parent.`);
    if (record.parents.length === 0) approvedRootCount += 1;
    approvedCommitIds.push(record.commit);
    approvedTrees.set(record.commit, record.tree);
  }
  requireCondition(approvedCommitIds.length === new Set(approvedCommitIds).size, "POLICY_SHAPE", "Approved commit IDs must be unique.");
  const approvedCommitSet = new Set(approvedCommitIds);
  requireCondition(approvedRootCount === 1, "POLICY_SHAPE", "Exactly one approved commit must be the fresh root.");
  for (const record of policy.target.approvedCommits) {
    requireCondition(record.parents.every((parent) => approvedCommitSet.has(parent)), "POLICY_SHAPE", `Approved commit ${record.commit} has an unapproved parent.`);
  }
  requireCondition(
    approvedTrees.get(policy.target.baseline.commit) === policy.target.baseline.tree,
    "POLICY_SHAPE",
    "Baseline commit and tree must match one approved commit record.",
  );

  requireCondition(Array.isArray(policy.target.approvedRefs) && policy.target.approvedRefs.length > 0, "POLICY_SHAPE", "Approved refs are required.");
  const approvedRefNames = [];
  for (const record of policy.target.approvedRefs) {
    requireExactKeys(record, ["name", "commits"], "approved ref");
    requireCondition(/^refs\/[A-Za-z0-9._\/-]+$/.test(record.name), "POLICY_SHAPE", `Approved ref ${record.name} is invalid.`);
    requireSortedUniqueStrings(record.commits, `approved ref ${record.name} commits`);
    requireCondition(record.commits.every((commit) => approvedCommitSet.has(commit)), "POLICY_SHAPE", `Approved ref ${record.name} targets an unapproved commit.`);
    approvedRefNames.push(record.name);
  }
  requireCondition(
    JSON.stringify(approvedRefNames) === JSON.stringify(sorted(new Set(approvedRefNames))),
    "POLICY_SHAPE",
    "Approved refs must be sorted and unique by name.",
  );

  requireExactKeys(policy.target.dynamicEventHead, ["eventParentSets", "tree", "allowedRefPatterns"], "policy.target.dynamicEventHead");
  requireCondition(policy.target.dynamicEventHead.tree === policy.target.baseline.tree, "POLICY_SHAPE", "Dynamic HEAD tree must equal the pinned baseline tree.");
  requireCondition(Array.isArray(policy.target.dynamicEventHead.eventParentSets) && policy.target.dynamicEventHead.eventParentSets.length === 2, "POLICY_SHAPE", "Dynamic HEAD must define pull_request and merge_group parent sets.");
  const dynamicEvents = [];
  for (const eventRule of policy.target.dynamicEventHead.eventParentSets) {
    requireExactKeys(eventRule, ["event", "parentSets"], "dynamic event rule");
    requireCondition(eventRule.event === "pull_request" || eventRule.event === "merge_group", "POLICY_SHAPE", "Dynamic HEAD events may only be pull_request or merge_group.");
    requireCondition(Array.isArray(eventRule.parentSets) && eventRule.parentSets.length > 0, "POLICY_SHAPE", `Dynamic HEAD ${eventRule.event} parent sets are required.`);
    const parentSetKeys = [];
    for (const parents of eventRule.parentSets) {
      requireCondition(Array.isArray(parents) && parents.length > 0 && parents.length <= 2, "POLICY_SHAPE", "Dynamic HEAD parent sets must contain one or two commits.");
      requireCondition(parents.every((parent) => approvedCommitSet.has(parent)), "POLICY_SHAPE", "Dynamic HEAD parents must all be approved commits.");
      parentSetKeys.push(parents.join(" "));
    }
    requireCondition(parentSetKeys.length === new Set(parentSetKeys).size, "POLICY_SHAPE", `Dynamic HEAD ${eventRule.event} parent sets must be unique.`);
    dynamicEvents.push(eventRule.event);
  }
  requireCondition(JSON.stringify(dynamicEvents) === JSON.stringify(["merge_group", "pull_request"]), "POLICY_SHAPE", "Dynamic HEAD event rules must be sorted and complete.");
  requireSortedUniqueStrings(policy.target.dynamicEventHead.allowedRefPatterns, "policy.target.dynamicEventHead.allowedRefPatterns", { allowEmpty: true });
  for (const source of policy.target.dynamicEventHead.allowedRefPatterns) {
    requireCondition(source.startsWith("^") && source.endsWith("$"), "POLICY_SHAPE", "Dynamic ref patterns must be anchored.");
    try {
      new RegExp(source);
    } catch {
      reject("POLICY_SHAPE", "Dynamic ref pattern does not compile.");
    }
  }
  requireExactKeys(
    policy.history,
    ["maximumBlobBytes", "maximumCommitCount", "allowedModes", "allowedExecutableFiles", "rejectSymlinks", "rejectSubmodules", "rejectBinaryBlobs", "requireAsciiPaths", "requireSingleFreshRoot"],
    "policy.history",
  );
  requireCondition(Number.isSafeInteger(policy.history.maximumBlobBytes) && policy.history.maximumBlobBytes > 0, "POLICY_SHAPE", "maximumBlobBytes must be positive.");
  requireCondition(Number.isSafeInteger(policy.history.maximumCommitCount) && policy.history.maximumCommitCount > 0, "POLICY_SHAPE", "maximumCommitCount must be positive.");
  requireSortedUniqueStrings(policy.history.allowedModes, "policy.history.allowedModes");
  requireSortedUniqueStrings(policy.history.allowedExecutableFiles, "policy.history.allowedExecutableFiles", { allowEmpty: true });
  for (const path of policy.history.allowedExecutableFiles) validateSafePath(path, policy, "allowed executable");
  for (const flag of ["rejectSymlinks", "rejectSubmodules", "rejectBinaryBlobs", "requireAsciiPaths", "requireSingleFreshRoot"]) {
    requireCondition(policy.history[flag] === true, "POLICY_SHAPE", `policy.history.${flag} must be true.`);
  }

  requireExactKeys(policy.content, ["forbiddenPathSegments", "forbiddenLiterals", "secretPatterns"], "policy.content");
  requireSortedUniqueStrings(policy.content.forbiddenPathSegments, "policy.content.forbiddenPathSegments");
  requireSortedUniqueStrings(policy.content.forbiddenLiterals, "policy.content.forbiddenLiterals");
  requireCondition(Array.isArray(policy.content.secretPatterns) && policy.content.secretPatterns.length > 0, "POLICY_SHAPE", "Secret patterns are required.");
  const secretNames = [];
  for (const rule of policy.content.secretPatterns) {
    requireExactKeys(rule, ["name", "source", "flags"], "secret pattern");
    requireCondition(typeof rule.name === "string" && /^[a-z0-9-]+$/.test(rule.name), "POLICY_SHAPE", "Secret pattern name is invalid.");
    requireCondition(typeof rule.source === "string" && rule.source.length > 0, "POLICY_SHAPE", `Secret pattern ${rule.name} is empty.`);
    requireCondition(typeof rule.flags === "string" && /^[gimsuy]*$/.test(rule.flags) && rule.flags.includes("g"), "POLICY_SHAPE", `Secret pattern ${rule.name} flags are invalid.`);
    try {
      new RegExp(rule.source, rule.flags);
    } catch {
      reject("POLICY_SHAPE", `Secret pattern ${rule.name} does not compile.`);
    }
    secretNames.push(rule.name);
  }
  requireCondition(secretNames.length === new Set(secretNames).size, "POLICY_SHAPE", "Secret pattern names must be unique.");

  requireCondition(Array.isArray(policy.skills) && policy.skills.length === 8, "POLICY_SHAPE", "Exactly eight public skills are required.");
  const skillIds = policy.skills.map((skill) => skill.id);
  requireCondition(JSON.stringify(skillIds) === JSON.stringify(sorted(new Set(skillIds))), "POLICY_SHAPE", "Skill IDs must be sorted and unique.");
  let originalCount = 0;
  for (const skill of policy.skills) {
    requireExactKeys(skill, ["id", "provenanceClass", "sourceCommit", "sourcePath"], `skill ${skill.id ?? "unknown"}`);
    requireCondition(/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(skill.id), "POLICY_SHAPE", `Skill ID ${skill.id} is invalid.`);
    requireCondition(skill.sourcePath === `skills/${skill.id}`, "POLICY_SHAPE", `Skill ${skill.id} has the wrong source path.`);
    if (skill.provenanceClass === "public-original") {
      originalCount += 1;
      requireCondition(skill.sourceCommit === null, "POLICY_SHAPE", `Public-original skill ${skill.id} must not claim an import commit.`);
    } else {
      requireCondition(
        ["historical-public-derived", "historical-public-import", "public-declassified-derivative"].includes(skill.provenanceClass),
        "POLICY_SHAPE",
        `Skill ${skill.id} has an unsupported provenance class.`,
      );
      requireCondition(/^[0-9a-f]{40}$/.test(skill.sourceCommit), "POLICY_SHAPE", `Source-derived skill ${skill.id} needs a full source commit.`);
    }
  }
  requireCondition(originalCount === 1, "POLICY_SHAPE", "Exactly one skill must be public-original.");

  requireCondition(policy.expectedFiles && typeof policy.expectedFiles === "object" && !Array.isArray(policy.expectedFiles), "POLICY_SHAPE", "expectedFiles must be an object.");
  const filePaths = Object.keys(policy.expectedFiles);
  requireCondition(filePaths.length > 0, "POLICY_SHAPE", "expectedFiles must not be empty.");
  requireCondition(JSON.stringify(filePaths) === JSON.stringify(sorted(filePaths)), "POLICY_SHAPE", "expectedFiles keys must be sorted.");
  for (const path of filePaths) {
    validateSafePath(path, policy, "expected file");
    requireCondition(/^[0-9a-f]{64}$/.test(policy.expectedFiles[path]), "POLICY_SHAPE", `Expected digest for ${path} is invalid.`);
  }
  for (const path of policy.history.allowedExecutableFiles) {
    requireCondition(Object.hasOwn(policy.expectedFiles, path), "POLICY_SHAPE", `Allowed executable ${path} is not an expected file.`);
  }
  for (const skill of policy.skills) {
    requireCondition(Object.hasOwn(policy.expectedFiles, `skills/${skill.id}/SKILL.md`), "POLICY_SHAPE", `Skill ${skill.id} has no pinned SKILL.md.`);
    requireCondition(Object.hasOwn(policy.expectedFiles, `evals/${skill.id}.eval.yaml`), "POLICY_SHAPE", `Skill ${skill.id} has no pinned eval.`);
  }
}

function git(candidateRoot, args, { text = true } = {}) {
  try {
    return execFileSync(
      "git",
      ["--no-pager", "--no-replace-objects", "-c", "core.quotepath=false", ...args],
      {
        cwd: candidateRoot,
        encoding: text ? "utf8" : undefined,
        maxBuffer: MAX_GIT_OUTPUT_BYTES,
        stdio: ["ignore", "pipe", "pipe"],
        env: {
          ...process.env,
          GIT_CONFIG_NOSYSTEM: "1",
          GIT_CONFIG_GLOBAL: "/dev/null",
          GIT_NO_REPLACE_OBJECTS: "1",
          LC_ALL: "C",
        },
      },
    );
  } catch (error) {
    const stderr = Buffer.isBuffer(error.stderr) ? error.stderr.toString("utf8") : String(error.stderr ?? "");
    reject("GIT_FAILURE", `Git inspection failed for ${args[0]}: ${stderr.trim() || error.message}`);
  }
}

function parseTree(candidateRoot, commit) {
  const output = git(candidateRoot, ["ls-tree", "-r", "-z", "--full-tree", commit], { text: false });
  const entries = [];
  for (const record of output.toString("utf8").split("\0").filter(Boolean)) {
    const match = /^(\d{6}) ([^ ]+) ([0-9a-f]{40})\t([\s\S]+)$/.exec(record);
    requireCondition(match, "MALFORMED_TREE", `Commit ${commit} has an unparseable tree record.`);
    entries.push({ mode: match[1], type: match[2], oid: match[3], path: match[4] });
  }
  return entries;
}

function decodeText(bytes, objectLabel, policy) {
  requireCondition(bytes.length <= policy.history.maximumBlobBytes, "OVERSIZED_BLOB", `${objectLabel} exceeds the blob-size policy.`);
  if (policy.history.rejectBinaryBlobs) {
    requireCondition(!bytes.includes(0), "BINARY_BLOB", `${objectLabel} contains a NUL byte.`);
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      reject("BINARY_BLOB", `${objectLabel} is not valid UTF-8 text.`);
    }
  }
  return bytes.toString("utf8");
}

function scanText(text, objectLabel, policy) {
  requireCondition(!/[\u202a-\u202e\u2066-\u2069\ufeff]/u.test(text), "INVISIBLE_CONTROL", `${objectLabel} contains a bidi or byte-order control.`);
  requireCondition(!text.startsWith("version https://git-lfs.github.com/spec/v1\n"), "LFS_POINTER", `${objectLabel} is a Git LFS pointer.`);
  const lower = text.toLowerCase();
  for (const literal of policy.content.forbiddenLiterals) {
    requireCondition(!lower.includes(literal.toLowerCase()), "PRIVATE_MARKER", `${objectLabel} contains forbidden boundary marker ${literal}.`);
  }
  for (const rule of policy.content.secretPatterns) {
    const pattern = new RegExp(rule.source, rule.flags);
    requireCondition(!pattern.test(text), "SECRET_PATTERN", `${objectLabel} matches secret rule ${rule.name}.`);
  }
}

function scanPath(path, objectLabel, policy) {
  validateSafePath(path, policy, objectLabel);
  const lower = path.toLowerCase();
  for (const literal of policy.content.forbiddenLiterals) {
    requireCondition(!lower.includes(literal.toLowerCase()), "PRIVATE_MARKER", `${objectLabel} contains forbidden boundary marker ${literal}.`);
  }
}

function parseJsonBlob(candidateRoot, commit, path, policy) {
  const bytes = git(candidateRoot, ["show", `${commit}:${path}`], { text: false });
  const text = decodeText(bytes, `${commit}:${path}`, policy);
  try {
    return JSON.parse(text);
  } catch {
    reject("INVALID_JSON", `${path} is not valid JSON.`);
  }
}

function validateAdmissions(candidateRoot, head, policy) {
  const admission = parseJsonBlob(candidateRoot, head, "admissions/public-skills.json", policy);
  requireCondition(admission.repository === "SylphxAI/skills", "ADMISSION_CONTRACT", "Admissions repository must be SylphxAI/skills.");
  requireCondition(admission.packageRoot === "skills", "ADMISSION_CONTRACT", "Admissions packageRoot must be skills.");
  requireCondition(admission.evaluationRoot === "evals", "ADMISSION_CONTRACT", "Admissions evaluationRoot must be evals.");
  requireCondition(admission.catalogPath === "registry/catalog.json", "ADMISSION_CONTRACT", "Admissions catalogPath is invalid.");
  requireCondition(admission.defaultChannel === "candidate", "ADMISSION_CONTRACT", "Admissions channel must remain candidate.");
  requireCondition(admission.defaultVerification === "unverified", "ADMISSION_CONTRACT", "Admissions verification must remain unverified.");
  requireCondition(Array.isArray(admission.skills), "ADMISSION_CONTRACT", "Admissions skills must be an array.");
  const expectedIds = policy.skills.map((skill) => skill.id);
  const actualIds = admission.skills.map((skill) => skill.name);
  requireCondition(JSON.stringify(actualIds) === JSON.stringify(expectedIds), "ADMISSION_CONTRACT", "Admissions must contain the exact ordered eight-skill allowlist.");

  const expectedById = new Map(policy.skills.map((skill) => [skill.id, skill]));
  for (const record of admission.skills) {
    const expected = expectedById.get(record.name);
    requireCondition(record.path === `skills/${record.name}`, "ADMISSION_CONTRACT", `Skill ${record.name} has the wrong package path.`);
    requireCondition(record.evalPath === `evals/${record.name}.eval.yaml`, "ADMISSION_CONTRACT", `Skill ${record.name} has the wrong eval path.`);
    requireCondition(record.channel === "candidate" && record.verification === "unverified", "ADMISSION_CONTRACT", `Skill ${record.name} overclaims verification.`);
    requireCondition(record.owner === "SylphxAI/skills" && record.license === "MIT", "ADMISSION_CONTRACT", `Skill ${record.name} has the wrong owner or license.`);
    requireCondition(record.provenance?.class === expected.provenanceClass, "ADMISSION_CONTRACT", `Skill ${record.name} has the wrong provenance class.`);
    requireCondition(record.provenance?.sourceRepository === "SylphxAI/skills", "ADMISSION_CONTRACT", `Skill ${record.name} has the wrong provenance repository.`);
    requireCondition(record.provenance?.sourcePath === expected.sourcePath, "ADMISSION_CONTRACT", `Skill ${record.name} has the wrong provenance path.`);
    if (expected.sourceCommit === null) {
      requireCondition(!Object.hasOwn(record.provenance, "sourceCommit"), "ADMISSION_CONTRACT", `Public-original skill ${record.name} must not claim an import commit.`);
    } else {
      requireCondition(record.provenance?.sourceCommit === expected.sourceCommit, "ADMISSION_CONTRACT", `Skill ${record.name} has the wrong provenance commit.`);
    }
  }

  const catalog = parseJsonBlob(candidateRoot, head, "registry/catalog.json", policy);
  requireCondition(catalog.repository === "SylphxAI/skills", "CATALOG_CONTRACT", "Catalog repository must be SylphxAI/skills.");
  requireCondition(catalog.channel === "candidate" && catalog.verification === "unverified", "CATALOG_CONTRACT", "Catalog overclaims verification.");
  requireCondition(Array.isArray(catalog.skills), "CATALOG_CONTRACT", "Catalog skills must be an array.");
  requireCondition(JSON.stringify(catalog.skills.map((skill) => skill.name)) === JSON.stringify(expectedIds), "CATALOG_CONTRACT", "Catalog must contain the exact ordered eight-skill allowlist.");

  for (const id of expectedIds) {
    const bytes = git(candidateRoot, ["show", `${head}:skills/${id}/SKILL.md`], { text: false });
    const text = decodeText(bytes, `${head}:skills/${id}/SKILL.md`, policy);
    const frontmatter = /^---\n([\s\S]*?)\n---\n/.exec(text);
    requireCondition(frontmatter, "SKILL_CONTRACT", `Skill ${id} has no bounded YAML frontmatter.`);
    const nameMatch = /^name:\s*["']?([^"'\s]+)["']?\s*$/m.exec(frontmatter[1]);
    requireCondition(nameMatch?.[1] === id, "SKILL_CONTRACT", `Skill ${id} frontmatter name does not match its directory.`);
  }
}

export function validateCandidate({ candidateRoot, policy, runtimeIdentity }) {
  validatePolicy(policy);
  const root = realpathSync(candidateRoot);
  const gitDirectory = git(root, ["rev-parse", "--absolute-git-dir"]).trim();
  requireCondition(gitDirectory.length > 0, "NOT_GIT_REPOSITORY", "Candidate root is not a Git repository.");

  requireExactKeys(runtimeIdentity, ["repository", "repositoryId", "repositoryNodeId", "candidateSha", "eventName"], "runtime identity");
  requireCondition(policy.target.allowedRepositories.includes(runtimeIdentity.repository), "REPOSITORY_IDENTITY", "Runtime repository slug is not allowed.");
  requireCondition(String(policy.target.repositoryId) === String(runtimeIdentity.repositoryId), "REPOSITORY_IDENTITY", "Runtime repository numeric ID does not match policy.");
  requireCondition(policy.target.repositoryNodeId === runtimeIdentity.repositoryNodeId, "REPOSITORY_IDENTITY", "Runtime repository node ID does not match policy.");
  requireCondition(/^[0-9a-f]{40}$/.test(runtimeIdentity.candidateSha), "REPOSITORY_IDENTITY", "Runtime candidate SHA must be a full commit ID.");
  requireCondition(typeof runtimeIdentity.eventName === "string" && runtimeIdentity.eventName.length > 0, "REPOSITORY_IDENTITY", "Runtime event name is required.");

  const head = git(root, ["rev-parse", "--verify", "HEAD^{commit}"]).trim();
  requireCondition(head === runtimeIdentity.candidateSha, "CANDIDATE_SHA", "Checked-out HEAD does not match the event candidate SHA.");
  const headTree = git(root, ["show", "-s", "--format=%T", head]).trim();
  requireCondition(headTree === policy.target.baseline.tree, "PINNED_TREE", "Candidate HEAD tree is not the source-approved snapshot.");

  const commits = git(root, ["rev-list", "--all", "HEAD"]).trim().split("\n").filter(Boolean);
  requireCondition(commits.length > 0, "EMPTY_HISTORY", "Candidate history has no commits.");
  requireCondition(commits.length <= policy.history.maximumCommitCount, "HISTORY_LIMIT", "Candidate history exceeds the approved commit-count bound.");
  requireCondition(commits.every((commit) => /^[0-9a-f]{40}$/.test(commit)), "MALFORMED_HISTORY", "Candidate history contains a malformed commit ID.");
  const commitSet = new Set(commits);

  const refs = git(root, ["for-each-ref", "--format=%(refname)%09%(objectname)%09%(objecttype)"])
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((record) => record.split("\t"));
  for (const fields of refs) {
    requireCondition(fields.length === 3, "MALFORMED_REF", "Candidate contains an unparseable ref record.");
    const [ref, oid, type] = fields;
    requireCondition(/^refs\/[A-Za-z0-9._\/-]+$/.test(ref), "MALFORMED_REF", "Candidate contains an unsafe ref name.");
    requireCondition(/^[0-9a-f]{40}$/.test(oid), "MALFORMED_REF", `Ref ${ref} has an invalid object ID.`);
    requireCondition(["blob", "commit", "tag", "tree"].includes(type), "NON_COMMIT_REF", `Ref ${ref} points to unsupported object type ${type}.`);
    scanText(ref, `ref ${ref}`, policy);
  }

  const roots = git(root, ["rev-list", "--all", "HEAD", "--max-parents=0"]).trim().split("\n").filter(Boolean);
  const approvedRoot = policy.target.approvedCommits.find((record) => record.parents.length === 0);
  requireCondition(roots.length === 1, "FRESH_ROOT", "Candidate must have exactly one reachable root commit.");
  requireCondition(roots[0] === approvedRoot.commit, "FRESH_ROOT", "Candidate root commit is not the approved fresh root.");
  requireCondition(git(root, ["show", "-s", "--format=%T", roots[0]]).trim() === approvedRoot.tree, "FRESH_ROOT", "Candidate root tree is not approved.");

  const expectedPaths = Object.keys(policy.expectedFiles);
  const expectedPathSet = new Set(expectedPaths);
  const executableSet = new Set(policy.history.allowedExecutableFiles);
  const blobPaths = new Map();
  const commitMetadata = new Map();
  let treeEntryCount = 0;
  for (const commit of commits) {
    const commitBytes = git(root, ["cat-file", "commit", commit], { text: false });
    scanText(decodeText(commitBytes, `commit ${commit}`, policy), `commit ${commit}`, policy);
    const metadata = git(root, ["show", "-s", "--format=%T%n%P", commit]).trimEnd().split("\n");
    commitMetadata.set(commit, {
      tree: metadata[0],
      parents: metadata[1] ? metadata[1].split(" ") : [],
    });
    const caseFoldedPaths = new Set();
    for (const entry of parseTree(root, commit)) {
      treeEntryCount += 1;
      scanPath(entry.path, `tree path in ${commit}`, policy);
      requireCondition(expectedPathSet.has(entry.path), "HISTORICAL_ONLY_PATH", `Commit ${commit} contains non-allowlisted path ${entry.path}.`);
      const folded = entry.path.toLowerCase();
      requireCondition(!caseFoldedPaths.has(folded), "CASE_COLLISION", `Commit ${commit} contains a case-folded path collision.`);
      caseFoldedPaths.add(folded);
      if (entry.mode === "120000") requireCondition(!policy.history.rejectSymlinks, "SYMLINK", `Commit ${commit} contains a symlink.`);
      if (entry.mode === "160000" || entry.type === "commit") requireCondition(!policy.history.rejectSubmodules, "SUBMODULE", `Commit ${commit} contains a submodule.`);
      requireCondition(entry.type === "blob", "NON_BLOB_ENTRY", `Commit ${commit} contains a non-blob tree entry.`);
      requireCondition(policy.history.allowedModes.includes(entry.mode), "FILE_MODE", `Commit ${commit} contains forbidden mode ${entry.mode}.`);
      if (entry.mode === "100755") requireCondition(executableSet.has(entry.path), "EXECUTABLE_FILE", `Commit ${commit} contains an unapproved executable file.`);
      if (entry.mode === "100644") requireCondition(!executableSet.has(entry.path) || commit !== head, "EXECUTABLE_FILE", `Approved executable ${entry.path} lost its executable mode at HEAD.`);
      if (!blobPaths.has(entry.oid)) blobPaths.set(entry.oid, new Set());
      blobPaths.get(entry.oid).add(entry.path);
    }
  }

  for (const [oid, paths] of blobPaths) {
    const bytes = git(root, ["cat-file", "blob", oid], { text: false });
    const label = `blob ${oid} (${sorted(paths).join(", ")})`;
    scanText(decodeText(bytes, label, policy), label, policy);
  }

  const reachableObjects = git(root, ["rev-list", "--objects", "--all", "HEAD"])
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((record) => record.slice(0, 40));
  requireCondition(reachableObjects.every((oid) => /^[0-9a-f]{40}$/.test(oid)), "MALFORMED_HISTORY", "Reachable object inventory contains an invalid object ID.");
  for (const oid of new Set(reachableObjects)) {
    const type = git(root, ["cat-file", "-t", oid]).trim();
    requireCondition(["blob", "commit", "tag", "tree"].includes(type), "MALFORMED_HISTORY", `Reachable object ${oid} has unsupported type ${type}.`);
    if (type === "blob") {
      requireCondition(blobPaths.has(oid), "UNCOMMITTED_BLOB", `Reachable blob ${oid} is not owned by a committed tree.`);
    } else if (type === "tag") {
      const bytes = git(root, ["cat-file", "tag", oid], { text: false });
      scanText(decodeText(bytes, `tag ${oid}`, policy), `tag ${oid}`, policy);
    }
  }

  const approvedCommitMap = new Map(policy.target.approvedCommits.map((record) => [record.commit, record]));
  for (const record of policy.target.approvedCommits) {
    requireCondition(commitSet.has(record.commit), "MISSING_APPROVED_COMMIT", `Approved commit ${record.commit} is not reachable.`);
    const actual = commitMetadata.get(record.commit);
    requireCondition(actual?.tree === record.tree, "APPROVED_COMMIT_DRIFT", `Approved commit ${record.commit} has the wrong tree.`);
    requireCondition(JSON.stringify(actual.parents) === JSON.stringify(record.parents), "APPROVED_COMMIT_DRIFT", `Approved commit ${record.commit} has the wrong parent graph.`);
  }

  const unknownCommits = commits.filter((commit) => !approvedCommitMap.has(commit));
  let usesDynamicEventHead = false;
  if (unknownCommits.length > 0) {
    requireCondition(unknownCommits.length === 1 && unknownCommits[0] === head, "UNAPPROVED_COMMIT", "Reachable history contains a non-approved commit outside the event HEAD exception.");
    const eventRule = policy.target.dynamicEventHead.eventParentSets.find((rule) => rule.event === runtimeIdentity.eventName);
    requireCondition(eventRule, "UNAPPROVED_COMMIT", `Event ${runtimeIdentity.eventName} cannot use a dynamic candidate HEAD.`);
    const actual = commitMetadata.get(head);
    requireCondition(actual.tree === policy.target.dynamicEventHead.tree, "UNAPPROVED_COMMIT", "Dynamic event HEAD has an unapproved tree.");
    requireCondition(
      eventRule.parentSets.some((parents) => JSON.stringify(parents) === JSON.stringify(actual.parents)),
      "UNAPPROVED_COMMIT",
      `Dynamic ${runtimeIdentity.eventName} HEAD has an unapproved parent graph.`,
    );
    usesDynamicEventHead = true;
  } else {
    requireCondition(approvedCommitMap.has(head), "UNAPPROVED_COMMIT", "Candidate HEAD is not approved.");
  }

  const approvedRefMap = new Map(policy.target.approvedRefs.map((record) => [record.name, new Set(record.commits)]));
  const dynamicRefPatterns = policy.target.dynamicEventHead.allowedRefPatterns.map((source) => new RegExp(source));
  for (const [ref, oid, type] of refs) {
    requireCondition(type !== "tag", "UNAPPROVED_TAG", `Annotated tag ${ref} is not approved.`);
    requireCondition(type === "commit", "NON_COMMIT_REF", `Ref ${ref} points to unsupported object type ${type}.`);
    const approvedTargets = approvedRefMap.get(ref);
    if (approvedTargets?.has(oid)) continue;
    if (usesDynamicEventHead && oid === head && dynamicRefPatterns.some((pattern) => pattern.test(ref))) continue;
    reject("UNAPPROVED_REF", `Ref ${ref} is not bound to an approved commit or permitted dynamic event HEAD.`);
  }

  const headEntries = parseTree(root, head);
  const actualPaths = headEntries.map((entry) => entry.path);
  requireCondition(JSON.stringify(actualPaths) === JSON.stringify(expectedPaths), "PHYSICAL_ALLOWLIST", "Candidate HEAD paths differ from the exact physical allowlist.");
  for (const entry of headEntries) {
    const bytes = git(root, ["cat-file", "blob", entry.oid], { text: false });
    requireCondition(sha256(bytes) === policy.expectedFiles[entry.path], "FILE_DIGEST", `Candidate file ${entry.path} does not match its source-approved SHA-256 digest.`);
  }

  const physicalSkillIds = sorted(new Set(actualPaths.filter((path) => path.startsWith("skills/")).map((path) => path.split("/")[1])));
  const physicalEvalIds = sorted(actualPaths.filter((path) => path.startsWith("evals/") && path.endsWith(".eval.yaml")).map((path) => path.slice("evals/".length, -".eval.yaml".length)));
  const expectedSkillIds = policy.skills.map((skill) => skill.id);
  requireCondition(JSON.stringify(physicalSkillIds) === JSON.stringify(expectedSkillIds), "PHYSICAL_ALLOWLIST", "Physical skill directories differ from the exact eight-skill allowlist.");
  requireCondition(JSON.stringify(physicalEvalIds) === JSON.stringify(expectedSkillIds), "PHYSICAL_ALLOWLIST", "Physical eval files differ from the exact eight-skill allowlist.");
  validateAdmissions(root, head, policy);

  return {
    schemaVersion: 1,
    kind: "sylphx-public-skills-external-admission-report",
    status: "pass",
    policy: {
      repository: policy.source.repository,
      path: policy.source.policyPath,
      baselineCommit: policy.target.baseline.commit,
      baselineTree: policy.target.baseline.tree,
    },
    candidate: {
      repository: runtimeIdentity.repository,
      repositoryId: Number(runtimeIdentity.repositoryId),
      repositoryNodeId: runtimeIdentity.repositoryNodeId,
      eventName: runtimeIdentity.eventName,
      commit: head,
      tree: headTree,
      rootCommit: roots[0],
      dynamicEventHead: usesDynamicEventHead,
    },
    inventory: {
      commits: commits.length,
      treeEntriesScanned: treeEntryCount,
      refsScanned: refs.length,
      reachableObjectsScanned: new Set(reachableObjects).size,
      uniqueBlobsScanned: blobPaths.size,
      headFiles: headEntries.length,
      skills: expectedSkillIds,
    },
  };
}

function parseArguments(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    requireCondition(flag?.startsWith("--") && value !== undefined, "CLI_ARGUMENT", "Arguments must be --name value pairs.");
    requireCondition(!Object.hasOwn(values, flag.slice(2)), "CLI_ARGUMENT", `Duplicate argument ${flag}.`);
    values[flag.slice(2)] = value;
  }
  const required = ["policy", "candidate", "repository", "repository-id", "repository-node-id", "candidate-sha", "event-name", "source-root", "source-sha", "report"];
  for (const name of required) requireCondition(typeof values[name] === "string" && values[name].length > 0, "CLI_ARGUMENT", `Missing --${name}.`);
  return values;
}

function writeReport(reportPath, report) {
  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
}

export function runCli(argv) {
  let reportPath;
  try {
    const args = parseArguments(argv);
    reportPath = resolve(args.report);
    const sourceRoot = realpathSync(args["source-root"]);
    const policyPath = resolve(args.policy);
    requireCondition(policyPath.startsWith(`${sourceRoot}/`), "SOURCE_IDENTITY", "Policy must be read from the source-owned checkout.");
    const sourceHead = git(sourceRoot, ["rev-parse", "--verify", "HEAD^{commit}"]).trim();
    requireCondition(sourceHead === args["source-sha"], "SOURCE_IDENTITY", "Source checkout does not match github.workflow_sha.");
    const policy = JSON.parse(readFileSync(policyPath, "utf8"));
    const report = validateCandidate({
      candidateRoot: resolve(args.candidate),
      policy,
      runtimeIdentity: {
        repository: args.repository,
        repositoryId: args["repository-id"],
        repositoryNodeId: args["repository-node-id"],
        candidateSha: args["candidate-sha"],
        eventName: args["event-name"],
      },
    });
    report.source = { repository: policy.source.repository, commit: sourceHead };
    writeReport(reportPath, report);
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return 0;
  } catch (error) {
    const failure = {
      schemaVersion: 1,
      kind: "sylphx-public-skills-external-admission-report",
      status: "fail",
      error: {
        code: error instanceof AdmissionError ? error.code : "UNEXPECTED_FAILURE",
        message: error instanceof Error ? error.message : String(error),
      },
    };
    if (reportPath) writeReport(reportPath, failure);
    process.stderr.write(`${JSON.stringify(failure, null, 2)}\n`);
    return 1;
  }
}

const isMain = process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url;
if (isMain) process.exitCode = runCli(process.argv.slice(2));
