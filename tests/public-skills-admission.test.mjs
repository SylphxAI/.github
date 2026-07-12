import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import test from "node:test";

import { AdmissionError, validateCandidate } from "../scripts/public-skills-admission.mjs";

const repositoryRoot = resolve(import.meta.dirname, "..");
const policy = JSON.parse(readFileSync(resolve(repositoryRoot, "policies/public-skills-admission.json"), "utf8"));
const candidateSource = process.env.PUBLIC_SKILLS_CANDIDATE
  ? resolve(process.env.PUBLIC_SKILLS_CANDIDATE)
  : null;
const hasCandidate = candidateSource !== null && existsSync(resolve(candidateSource, ".git"));

function jsonDigest(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function git(root, args, options = {}) {
  return execFileSync("git", args, {
    cwd: root,
    encoding: options.encoding ?? "utf8",
    input: options.input,
    maxBuffer: 64 * 1024 * 1024,
    stdio: [options.input === undefined ? "ignore" : "pipe", "pipe", "pipe"],
  }).trim();
}

function runtimeIdentity(root, overrides = {}) {
  return {
    repository: "SylphxAI/skills-public-cleanroom",
    repositoryId: 1297840366,
    repositoryNodeId: "R_kgDOTVt47g",
    candidateSha: git(root, ["rev-parse", "HEAD"]),
    eventName: "policy-baseline",
    ...overrides,
  };
}

function cloneCandidate(t) {
  const parent = mkdtempSync(resolve(tmpdir(), "public-skills-admission-"));
  const clone = resolve(parent, "candidate");
  execFileSync("git", ["clone", "--local", "--no-hardlinks", candidateSource, clone], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  git(clone, ["config", "user.name", "Admission Adversary"]);
  git(clone, ["config", "user.email", "adversary@example.invalid"]);
  t.after(() => rmSync(parent, { force: true, recursive: true }));
  return clone;
}

function commitAll(root, subject) {
  git(root, ["add", "--all"]);
  git(root, ["commit", "--no-gpg-sign", "-m", subject]);
}

function restoreFile(root, baseline, path, subject) {
  git(root, ["checkout", baseline, "--", path]);
  commitAll(root, subject);
}

function expectAdmissionError(callback, code) {
  assert.throws(callback, (error) => error instanceof AdmissionError && error.code === code);
}

test("external workflow is target-only, source-owned, immutable-action-pinned, and candidate-inert", () => {
  const workflow = readFileSync(resolve(repositoryRoot, ".github/workflows/public-skills-admission.yml"), "utf8");
  assert.match(workflow, /^name: public-skills-external-admission$/m);
  assert.match(workflow, /^  pull_request:$/m);
  assert.match(workflow, /^  merge_group:$/m);
  assert.match(workflow, /^    name: public-skills-external-admission\/pass$/m);

  const selector = /^    if: \$\{\{ github\.repository_id == (\d+) \}\}$/m.exec(workflow);
  assert.ok(selector, "workflow must have one fail-closed numeric repository selector");
  assert.equal(Number(selector[1]), policy.target.repositoryId, "target repository must execute the gate");
  assert.notEqual(Number(selector[1]), policy.source.repositoryId, "source-repository PRs must skip the target gate");

  assert.match(workflow, /^    runs-on: ubuntu-24\.04$/m);
  assert.match(workflow, /^permissions:\n  contents: read$/m);
  assert.match(workflow, /^          repository: SylphxAI\/\.github$/m);
  assert.match(workflow, /^          ref: \$\{\{ github\.workflow_sha \}\}$/m);
  assert.match(workflow, /^          persist-credentials: false$/m);
  assert.match(workflow, /^          EVENT_NAME: \$\{\{ github\.event_name \}\}$/m);
  assert.match(workflow, /^            --event-name "\$EVENT_NAME" \\$/m);
  assert.doesNotMatch(workflow, /pull_request_target|secrets\.|cancel-in-progress/);
  assert.doesNotMatch(workflow, /\b(?:npm|npx|pnpm|yarn|bun|python|pip)\b/);
  assert.doesNotMatch(workflow, /candidate\/[^\s"']+\.(?:js|mjs|cjs|py|sh)\b/);

  const uses = [...workflow.matchAll(/^\s*uses:\s*([^\s#]+).*$/gm)].map((match) => match[1]);
  assert.ok(uses.length >= 3);
  for (const reference of uses) assert.match(reference, /^[^@\s]+@[0-9a-f]{40}$/);
});

test("policy pins one fresh root, the exact eight IDs, target identities, and every candidate file", () => {
  assert.deepEqual(policy.source, {
    repository: "SylphxAI/.github",
    repositoryId: 1091169653,
    workflowPath: ".github/workflows/public-skills-admission.yml",
    validatorPath: "scripts/public-skills-admission.mjs",
    policyPath: "policies/public-skills-admission.json",
  });
  assert.equal(policy.bundleDigestAlgorithm, "git-tree-manifest-sha256-v1");
  assert.equal(policy.target.repositoryId, 1297840366);
  assert.equal(policy.target.repositoryNodeId, "R_kgDOTVt47g");
  assert.deepEqual(policy.target.allowedRepositories, ["SylphxAI/skills", "SylphxAI/skills-public-cleanroom"]);
  assert.deepEqual(policy.target.baseline, {
    commit: "580791895d660755ca78c5e6f8233d1437f709fa",
    tree: "2741f0883bf636568d375974c98301ed16a633fb",
  });
  assert.equal(policy.target.approvedCommits.find((record) => record.parents.length === 0).commit, "e477aee5c1d93b2bac8619fdc6f15f27483855a3");
  assert.equal(policy.target.approvedCommits.length, 3);
  assert.equal(policy.target.approvedRefs.length, 5);
  assert.deepEqual(policy.target.dynamicEventHead.eventParentSets.map((rule) => rule.event), ["merge_group", "pull_request"]);
  assert.equal(jsonDigest(policy.target), "1038c61c3e5eedf482b4d64686164bfb87c8223c15f09477e905b78b2c8d7050");
  assert.deepEqual(
    policy.skills.map((skill) => skill.id),
    [
      "customer-support-operations",
      "decision-memo-writer",
      "fleet-migration-factory",
      "interface-craft",
      "market-research-synthesis",
      "public-skill-repository-governance",
      "skill-eval-designer",
      "source-to-skill-distiller",
    ],
  );
  assert.equal(jsonDigest(policy.skills), "9c3d84efdb8065027da048362bc240d8f9fb025c8e290911dbc6101f16f976b7");
  assert.deepEqual(
    Object.fromEntries([...new Set(policy.skills.map((skill) => skill.provenanceClass))].sort().map((name) => [name, policy.skills.filter((skill) => skill.provenanceClass === name).length])),
    {
      "historical-public-derived": 2,
      "historical-public-import": 4,
      "public-declassified-derivative": 1,
      "public-original": 1,
    },
  );
  assert.ok(policy.skills.every((skill) => skill.provenanceClass === "public-original" ? skill.sourceCommit === null : skill.sourceCommit === "4350900a59faeee7903937a52c24909aaba538ca"));
  assert.deepEqual(
    Object.fromEntries(policy.skills.map((skill) => [skill.id, {
      fileCount: skill.targetFileCount,
      bundleDigest: skill.targetBundleDigest,
    }])),
    {
      "customer-support-operations": { fileCount: 3, bundleDigest: "83177e56018859709cb6625652ed3621c0e90a8ae4f1eb3dab29e2c57bf6a271" },
      "decision-memo-writer": { fileCount: 3, bundleDigest: "03362bf5450680e6222ce2bce0999df47047d1b527191c357ee59ba1a3547dc8" },
      "fleet-migration-factory": { fileCount: 5, bundleDigest: "7a20138ba7be3f94e66c7388d88d1e58baf18a5fad6e8676af4386f0f5c70cb8" },
      "interface-craft": { fileCount: 3, bundleDigest: "395d8ee12dafff939ca48463f5c2d5a1fe6fb1c8a63ca98008e2494f518173c3" },
      "market-research-synthesis": { fileCount: 3, bundleDigest: "fdd52260add450d6c04ceb4fdeb7cca2ac0947a5bfdfa686ec7fef8d0c35d7aa" },
      "public-skill-repository-governance": { fileCount: 3, bundleDigest: "6f1612a8548cfbef01d36c202de431ffb3af3f8d04b8934181a4846762517023" },
      "skill-eval-designer": { fileCount: 3, bundleDigest: "46a7fc4b918b17992b3ca1096672eb6de087df2c3974d730e2476bbab63236ac" },
      "source-to-skill-distiller": { fileCount: 3, bundleDigest: "737c9988d8019a5badd252664f054937b8ca2dbbf2ebf6eabc0ff0a9dd9fd0b9" },
    },
  );
  assert.deepEqual(policy.content.forbiddenLiterals, ["DO-NOT-PUBLISH", "INTERNAL-ONLY", "PRIVATE-BOUNDARY-MARKER"]);
  assert.equal(Object.keys(policy.expectedFiles).length, 73);
  assert.equal(jsonDigest(policy.expectedFiles), "66f60f83fbbb107da9b3f74be88724692ee2673c2f223237ad77274e7680987a");
  assert.ok(Object.values(policy.expectedFiles).every((digest) => /^[0-9a-f]{64}$/.test(digest)));
});

test("the exact source-approved candidate passes full-history admission", { skip: !hasCandidate }, () => {
  const report = validateCandidate({
    candidateRoot: candidateSource,
    policy,
    runtimeIdentity: runtimeIdentity(candidateSource),
  });
  assert.equal(report.status, "pass");
  assert.equal(report.candidate.tree, policy.target.baseline.tree);
  assert.equal(report.candidate.rootCommit, policy.target.approvedCommits.find((record) => record.parents.length === 0).commit);
  assert.equal(report.candidate.dynamicEventHead, false);
  assert.equal(report.inventory.skills.length, 8);
  assert.ok(report.inventory.commits >= 1);
  assert.ok(report.inventory.uniqueBlobsScanned >= report.inventory.headFiles);
});

test("numeric and node repository identity mismatches fail closed", { skip: !hasCandidate }, () => {
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: candidateSource, policy, runtimeIdentity: runtimeIdentity(candidateSource, { repositoryId: 999999999 }) }),
    "REPOSITORY_IDENTITY",
  );
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: candidateSource, policy, runtimeIdentity: runtimeIdentity(candidateSource, { repositoryNodeId: "R_attacker" }) }),
    "REPOSITORY_IDENTITY",
  );
});

test("policy drift cannot silently change the eight-skill contract or a pinned digest", { skip: !hasCandidate }, () => {
  const missingSkillPolicy = structuredClone(policy);
  missingSkillPolicy.skills.pop();
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: candidateSource, policy: missingSkillPolicy, runtimeIdentity: runtimeIdentity(candidateSource) }),
    "POLICY_SHAPE",
  );

  const wrongDigestPolicy = structuredClone(policy);
  wrongDigestPolicy.expectedFiles["README.md"] = "0".repeat(64);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: candidateSource, policy: wrongDigestPolicy, runtimeIdentity: runtimeIdentity(candidateSource) }),
    "FILE_DIGEST",
  );

  const wrongTransferPolicy = structuredClone(policy);
  wrongTransferPolicy.skills[0].targetBundleDigest = "0".repeat(64);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: candidateSource, policy: wrongTransferPolicy, runtimeIdentity: runtimeIdentity(candidateSource) }),
    "BUNDLE_CONTRACT",
  );

  const wrongTransferCountPolicy = structuredClone(policy);
  wrongTransferCountPolicy.skills[0].targetFileCount += 1;
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: candidateSource, policy: wrongTransferCountPolicy, runtimeIdentity: runtimeIdentity(candidateSource) }),
    "BUNDLE_CONTRACT",
  );
});

test("benign historical payload followed by an exact HEAD restore is still rejected", { skip: !hasCandidate }, (t) => {
  const clone = cloneCandidate(t);
  const baseline = git(clone, ["rev-parse", "HEAD"]);
  const readme = resolve(clone, "README.md");
  writeFileSync(readme, `${readFileSync(readme, "utf8")}\nreviewed-looking but unapproved historical payload\n`);
  commitAll(clone, "test: add benign historical payload");
  restoreFile(clone, baseline, "README.md", "test: restore exact approved tree");
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: clone, policy, runtimeIdentity: runtimeIdentity(clone) }),
    "UNAPPROVED_COMMIT",
  );
});

test("unknown same-tree commits cannot extend the approved graph", { skip: !hasCandidate }, (t) => {
  const clone = cloneCandidate(t);
  git(clone, ["commit", "--allow-empty", "--no-gpg-sign", "-m", "test: empty same-tree commit"]);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: clone, policy, runtimeIdentity: runtimeIdentity(clone) }),
    "UNAPPROVED_COMMIT",
  );
});

test("only exact GitHub event HEAD shapes may be dynamic", { skip: !hasCandidate }, (t) => {
  const rootCommit = policy.target.approvedCommits.find((record) => record.parents.length === 0).commit;
  const approvedHead = policy.target.baseline.commit;

  const pullRequest = cloneCandidate(t);
  const prHead = git(pullRequest, [
    "commit-tree",
    policy.target.baseline.tree,
    "-p",
    rootCommit,
    "-p",
    approvedHead,
    "-m",
    "synthetic pull request candidate",
  ]);
  git(pullRequest, ["checkout", "--detach", prHead]);
  const prReport = validateCandidate({
    candidateRoot: pullRequest,
    policy,
    runtimeIdentity: runtimeIdentity(pullRequest, { eventName: "pull_request" }),
  });
  assert.equal(prReport.candidate.dynamicEventHead, true);

  const mergeGroup = cloneCandidate(t);
  const queueHead = git(mergeGroup, [
    "commit-tree",
    policy.target.baseline.tree,
    "-p",
    rootCommit,
    "-m",
    "synthetic squash merge queue candidate",
  ]);
  git(mergeGroup, ["checkout", "--detach", queueHead]);
  const queueReport = validateCandidate({
    candidateRoot: mergeGroup,
    policy,
    runtimeIdentity: runtimeIdentity(mergeGroup, { eventName: "merge_group" }),
  });
  assert.equal(queueReport.candidate.dynamicEventHead, true);

  expectAdmissionError(
    () => validateCandidate({
      candidateRoot: pullRequest,
      policy,
      runtimeIdentity: runtimeIdentity(pullRequest, { eventName: "push" }),
    }),
    "UNAPPROVED_COMMIT",
  );
  expectAdmissionError(
    () => validateCandidate({
      candidateRoot: pullRequest,
      policy,
      runtimeIdentity: runtimeIdentity(pullRequest, { eventName: "" }),
    }),
    "REPOSITORY_IDENTITY",
  );
});

test("a private boundary marker remains rejected after it is deleted from HEAD", { skip: !hasCandidate }, (t) => {
  const clone = cloneCandidate(t);
  const baseline = git(clone, ["rev-parse", "HEAD"]);
  const readme = resolve(clone, "README.md");
  writeFileSync(readme, `${readFileSync(readme, "utf8")}\nPRIVATE-BOUNDARY-MARKER\n`);
  commitAll(clone, "test: inject a protected marker");
  restoreFile(clone, baseline, "README.md", "test: hide the protected marker");
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: clone, policy, runtimeIdentity: runtimeIdentity(clone) }),
    "PRIVATE_MARKER",
  );
});

test("a token-shaped secret remains rejected after it is deleted from HEAD", { skip: !hasCandidate }, (t) => {
  const clone = cloneCandidate(t);
  const baseline = git(clone, ["rev-parse", "HEAD"]);
  const readme = resolve(clone, "README.md");
  writeFileSync(readme, `${readFileSync(readme, "utf8")}\nghp_1234567890abcdefghijklmnopqrstuvwxyzABCD\n`);
  commitAll(clone, "test: inject a token-shaped value");
  restoreFile(clone, baseline, "README.md", "test: hide the token-shaped value");
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: clone, policy, runtimeIdentity: runtimeIdentity(clone) }),
    "SECRET_PATTERN",
  );
});

test("a historical symlink remains rejected after the regular file is restored", { skip: !hasCandidate }, (t) => {
  const clone = cloneCandidate(t);
  const baseline = git(clone, ["rev-parse", "HEAD"]);
  const readme = resolve(clone, "README.md");
  rmSync(readme);
  symlinkSync("LICENSE", readme);
  commitAll(clone, "test: inject a symlink");
  rmSync(readme);
  restoreFile(clone, baseline, "README.md", "test: restore the regular file");
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: clone, policy, runtimeIdentity: runtimeIdentity(clone) }),
    "SYMLINK",
  );
});

test("a historical binary blob remains rejected after text is restored", { skip: !hasCandidate }, (t) => {
  const clone = cloneCandidate(t);
  const baseline = git(clone, ["rev-parse", "HEAD"]);
  writeFileSync(resolve(clone, "README.md"), Buffer.from([0x41, 0x00, 0x42]));
  commitAll(clone, "test: inject binary data");
  restoreFile(clone, baseline, "README.md", "test: restore text data");
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: clone, policy, runtimeIdentity: runtimeIdentity(clone) }),
    "BINARY_BLOB",
  );
});

test("an extra orphan history root is rejected even when HEAD is unchanged", { skip: !hasCandidate }, (t) => {
  const clone = cloneCandidate(t);
  const emptyTree = git(clone, ["mktree"], { input: "" });
  const orphanCommit = git(clone, ["commit-tree", emptyTree, "-m", "orphan history"]);
  git(clone, ["update-ref", "refs/heads/orphan-history", orphanCommit]);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: clone, policy, runtimeIdentity: runtimeIdentity(clone) }),
    "FRESH_ROOT",
  );
});

test("tag refs cannot hide secrets or uncommitted blobs outside commit trees", { skip: !hasCandidate }, (t) => {
  const taggedSecret = cloneCandidate(t);
  git(taggedSecret, ["tag", "--annotate", "--message", "ghp_1234567890abcdefghijklmnopqrstuvwxyzABCD", "secret-tag"]);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: taggedSecret, policy, runtimeIdentity: runtimeIdentity(taggedSecret) }),
    "SECRET_PATTERN",
  );

  const taggedBlob = cloneCandidate(t);
  const blob = git(taggedBlob, ["hash-object", "-w", "--stdin"], { input: "detached blob\n" });
  git(taggedBlob, ["update-ref", "refs/tags/detached-blob", blob]);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: taggedBlob, policy, runtimeIdentity: runtimeIdentity(taggedBlob) }),
    "UNCOMMITTED_BLOB",
  );
});

test("benign annotated tags and unknown branches are rejected", { skip: !hasCandidate }, (t) => {
  const tagged = cloneCandidate(t);
  git(tagged, ["tag", "--annotate", "--message", "benign but unapproved", "unapproved-tag"]);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: tagged, policy, runtimeIdentity: runtimeIdentity(tagged) }),
    "UNAPPROVED_TAG",
  );

  const branched = cloneCandidate(t);
  git(branched, ["branch", "unapproved-branch", policy.target.baseline.commit]);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: branched, policy, runtimeIdentity: runtimeIdentity(branched) }),
    "UNAPPROVED_REF",
  );
});

test("candidate-controlled scripts are rejected as bytes and never executed", { skip: !hasCandidate }, (t) => {
  const clone = cloneCandidate(t);
  const sentinel = resolve(tmpdir(), `candidate-script-sentinel-${process.pid}-${Date.now()}`);
  t.after(() => rmSync(sentinel, { force: true }));
  const packagePath = resolve(clone, "package.json");
  const packageJson = JSON.parse(readFileSync(packagePath, "utf8"));
  packageJson.scripts = { preinstall: `node -e 'require(\"node:fs\").writeFileSync(\"${sentinel}\",\"ran\")'` };
  writeFileSync(packagePath, `${JSON.stringify(packageJson, null, 2)}\n`);
  commitAll(clone, "test: attempt candidate execution");
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: clone, policy, runtimeIdentity: runtimeIdentity(clone) }),
    "PINNED_TREE",
  );
  assert.equal(existsSync(sentinel), false);
});
