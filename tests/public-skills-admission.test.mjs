import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import test from "node:test";

import { AdmissionError, authorizeCandidateGraph, validateCandidate } from "../scripts/public-skills-admission.mjs";

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
  const candidateSha = git(root, ["rev-parse", "HEAD"]);
  const parents = git(root, ["show", "-s", "--format=%P", candidateSha]).split(" ").filter(Boolean);
  const eventName = overrides.eventName ?? "policy-baseline";
  return {
    repository: "SylphxAI/skills-public-cleanroom",
    repositoryId: 1297840366,
    repositoryNodeId: "R_kgDOTVt47g",
    candidateSha,
    eventName,
    eventRef: "",
    baseRef: "",
    headRef: "",
    eventBaseSha: ["pull_request", "merge_group"].includes(eventName) ? (parents[0] ?? "") : "",
    pullRequestHeadSha: eventName === "pull_request" ? (parents.at(-1) ?? "") : "",
    eventRepositoryId: 1297840366,
    pullRequestHeadRepositoryId: "",
    ...overrides,
  };
}

function cloneCandidate(t, source = candidateSource) {
  const parent = mkdtempSync(resolve(tmpdir(), "public-skills-admission-"));
  const clone = resolve(parent, "candidate");
  execFileSync("git", ["clone", "--local", "--no-hardlinks", source, clone], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  const sourceRemoteRefs = git(source, ["for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)", "refs/remotes/origin"])
    .split("\n")
    .filter(Boolean)
    .map((record) => record.split("\t"));
  for (const [ref, oid, symbolicTarget] of sourceRemoteRefs) {
    if (symbolicTarget) git(clone, ["symbolic-ref", ref, symbolicTarget]);
    else git(clone, ["update-ref", "--no-deref", ref, oid]);
  }
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

function postMergeCandidate(t) {
  const clone = cloneCandidate(t);
  const rootCommit = policy.target.postMergeCanonicalization.parentCommit;
  const tree = policy.target.postMergeCanonicalization.tree;
  const canonicalMain = git(clone, [
    "commit-tree",
    tree,
    "-p",
    rootCommit,
    "-m",
    "canonical squash merge",
  ]);
  git(clone, ["checkout", "--detach", canonicalMain]);
  const refs = git(clone, ["for-each-ref", "--format=%(refname)%09%(symref)"])
    .split("\n")
    .filter(Boolean)
    .map((value) => value.split("\t"));
  for (const [ref, symbolicTarget] of refs) {
    if (symbolicTarget) git(clone, ["symbolic-ref", "--delete", ref]);
    else git(clone, ["update-ref", "-d", ref]);
  }
  git(clone, ["update-ref", "refs/heads/main", canonicalMain]);
  return { clone, canonicalMain, rootCommit, tree };
}

function expectAdmissionError(callback, code) {
  assert.throws(callback, (error) => error instanceof AdmissionError && error.code === code);
}

const fixtureOid = (label) => createHash("sha1").update("public-skills-graph:" + label).digest("hex");

function graphRuntime(snapshot, overrides = {}) {
  return {
    repository: "SylphxAI/skills-public-cleanroom",
    repositoryId: 1297840366,
    repositoryNodeId: "R_kgDOTVt47g",
    candidateSha: snapshot.head,
    eventName: "policy-baseline",
    eventRef: "",
    baseRef: "",
    headRef: "",
    eventBaseSha: "",
    pullRequestHeadSha: "",
    eventRepositoryId: 1297840366,
    pullRequestHeadRepositoryId: "",
    ...overrides,
  };
}

function launchGraphFixture(advanceCount = 2) {
  const [root, middle, launch] = policy.target.approvedCommits.map((record) => record.commit);
  const metadata = policy.target.approvedCommits.map((record) => ({
    commit: record.commit,
    tree: record.tree,
    parents: [...record.parents],
  }));
  let main = root;
  const advances = [];
  for (let index = 1; index <= advanceCount; index += 1) {
    const commit = fixtureOid("pre-main-" + index);
    metadata.push({
      commit,
      tree: policy.target.postMergeCanonicalization.preLaunchTree,
      parents: [main],
    });
    advances.push(commit);
    main = commit;
  }
  const head = fixtureOid("launch-event-" + advanceCount);
  metadata.push({
    commit: head,
    tree: policy.target.baseline.tree,
    parents: [main, launch],
  });
  const snapshot = {
    head,
    headTree: policy.target.baseline.tree,
    commits: metadata.map((record) => record.commit),
    refs: [
      ["refs/heads/main", main, "commit"],
      ["refs/heads/codex/launch-public-cleanroom", launch, "commit"],
      ["refs/remotes/origin/codex/launch-public-cleanroom", launch, "commit"],
      ["refs/remotes/pull/1/merge", head, "commit"],
    ],
    roots: [root],
    commitMetadata: metadata,
  };
  return {
    advances,
    main,
    snapshot,
    runtimeIdentity: graphRuntime(snapshot, {
      eventName: "pull_request",
      eventRef: "refs/pull/1/merge",
      baseRef: "main",
      headRef: "codex/launch-public-cleanroom",
      eventBaseSha: main,
      pullRequestHeadSha: launch,
      pullRequestHeadRepositoryId: "1297840366",
    }),
  };
}

function nonEventGraphFixture() {
  const [root, , launch] = policy.target.approvedCommits.map((record) => record.commit);
  const snapshot = {
    head: launch,
    headTree: policy.target.baseline.tree,
    commits: policy.target.approvedCommits.map((record) => record.commit),
    refs: [
      ["refs/heads/main", root, "commit"],
      ["refs/heads/codex/launch-public-cleanroom", launch, "commit"],
      ["refs/remotes/origin/codex/launch-public-cleanroom", launch, "commit"],
    ],
    roots: [root],
    commitMetadata: policy.target.approvedCommits.map((record) => ({
      commit: record.commit,
      tree: record.tree,
      parents: [...record.parents],
    })),
  };
  return { snapshot, runtimeIdentity: graphRuntime(snapshot) };
}

function sameTreeCanaryFixture(lifecycle, eventName) {
  const root = policy.target.postMergeCanonicalization.parentCommit;
  const launch = policy.target.baseline.commit;
  const preLaunch = lifecycle === "pre-launch";
  const tree = preLaunch
    ? policy.target.postMergeCanonicalization.preLaunchTree
    : policy.target.postMergeCanonicalization.tree;
  const main = preLaunch ? root : fixtureOid("canonical-launch");
  const branchName = preLaunch
    ? "canary/public-skills/pre-launch/barrier-pull-request-0123456789ab"
    : "canary/public-skills/post-launch/barrier-pass-through-0123456789ab";
  const branch = fixtureOid(lifecycle + "-branch");
  const head = fixtureOid(lifecycle + "-" + eventName + "-event");
  const metadata = preLaunch
    ? policy.target.approvedCommits.map((record) => ({
        commit: record.commit,
        tree: record.tree,
        parents: [...record.parents],
      }))
    : [
        {
          commit: root,
          tree: policy.target.postMergeCanonicalization.preLaunchTree,
          parents: [],
        },
        {
          commit: main,
          tree,
          parents: [root],
        },
      ];
  metadata.push(
    { commit: branch, tree, parents: [main] },
    { commit: head, tree, parents: [main, branch] },
  );
  const pullRequest = eventName === "pull_request";
  const eventRef = pullRequest
    ? "refs/pull/4/merge"
    : "refs/heads/gh-readonly-queue/main/pr-4-deadbeef";
  const dynamicRef = pullRequest
    ? "refs/remotes/pull/4/merge"
    : "refs/remotes/origin/gh-readonly-queue/main/pr-4-deadbeef";
  const snapshot = {
    head,
    headTree: tree,
    commits: metadata.map((record) => record.commit),
    refs: [
      ["refs/heads/main", main, "commit"],
      ["refs/heads/" + branchName, branch, "commit"],
      [dynamicRef, head, "commit"],
    ],
    roots: [root],
    commitMetadata: metadata,
  };
  if (preLaunch) {
    snapshot.refs.push(
      ["refs/heads/codex/launch-public-cleanroom", launch, "commit"],
      ["refs/remotes/origin/codex/launch-public-cleanroom", launch, "commit"],
    );
  }
  return {
    branch,
    main,
    snapshot,
    runtimeIdentity: graphRuntime(snapshot, {
      eventName,
      eventRef,
      baseRef: pullRequest ? "main" : "refs/heads/main",
      headRef: pullRequest ? branchName : eventRef,
      eventBaseSha: main,
      pullRequestHeadSha: pullRequest ? branch : "",
      pullRequestHeadRepositoryId: pullRequest ? "1297840366" : "",
    }),
  };
}

test("pure graph authority admits PR1 after bounded same-tree main advancement", () => {
  const fixture = launchGraphFixture();
  const decision = authorizeCandidateGraph({
    policy,
    runtimeIdentity: fixture.runtimeIdentity,
    snapshot: fixture.snapshot,
  });
  assert.deepEqual(decision, {
    eventContext: "launch",
    eventPullRequestNumber: 1,
    graphVariant: "launch-pending",
    canonicalMainCommit: null,
    mainTip: fixture.main,
    sameTreeCanaryClass: null,
    dynamicEventHead: true,
  });
});

test("pure graph authority admits strict pre-launch and post-launch PR and merge-group canaries", async (t) => {
  for (const lifecycle of ["pre-launch", "post-launch"]) {
    for (const eventName of ["pull_request", "merge_group"]) {
      await t.test(lifecycle + " " + eventName, () => {
        const fixture = sameTreeCanaryFixture(lifecycle, eventName);
        const decision = authorizeCandidateGraph({
          policy,
          runtimeIdentity: fixture.runtimeIdentity,
          snapshot: fixture.snapshot,
        });
        assert.equal(decision.sameTreeCanaryClass, lifecycle);
        assert.equal(decision.eventContext, "sameTreeCanary:" + lifecycle);
        assert.equal(decision.dynamicEventHead, true);
      });
    }
  }
});

test("pure graph authority rejects stale provider SHA, parent drift, and foreign refs", async (t) => {
  await t.test("stale provider base SHA", () => {
    const fixture = launchGraphFixture();
    fixture.runtimeIdentity.eventBaseSha = fixture.advances[0];
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "REPOSITORY_IDENTITY",
    );
  });
  await t.test("synthetic head parent drift", () => {
    const fixture = launchGraphFixture();
    const head = fixture.snapshot.commitMetadata.find((record) => record.commit === fixture.snapshot.head);
    head.parents[0] = fixture.advances[0];
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "UNAPPROVED_COMMIT",
    );
  });
  await t.test("foreign ref alias", () => {
    const fixture = launchGraphFixture();
    fixture.snapshot.refs.push(["refs/heads/foreign-main-alias", fixture.main, "commit"]);
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "UNAPPROVED_REF",
    );
  });
  await t.test("foreign pull-request head SHA", () => {
    const fixture = sameTreeCanaryFixture("pre-launch", "pull_request");
    fixture.runtimeIdentity.pullRequestHeadSha = fixtureOid("foreign-pr-head");
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "REPOSITORY_IDENTITY",
    );
  });
});

test("pure graph authority rejects overflow and foreign-root inventories", async (t) => {
  await t.test("bounded pre-launch main overflow", () => {
    const fixture = launchGraphFixture(policy.target.postMergeCanonicalization.maximumPreLaunchMainCommits + 1);
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "HISTORY_LIMIT",
    );
  });
  await t.test("second history root", () => {
    const fixture = launchGraphFixture();
    const foreignRoot = fixtureOid("foreign-root");
    fixture.snapshot.commits.push(foreignRoot);
    fixture.snapshot.roots.push(foreignRoot);
    fixture.snapshot.commitMetadata.push({
      commit: foreignRoot,
      tree: policy.target.postMergeCanonicalization.preLaunchTree,
      parents: [],
    });
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "FRESH_ROOT",
    );
  });
});

test("pure graph authority admits non-event baseline only without provider event claims", async (t) => {
  const fixture = nonEventGraphFixture();
  const decision = authorizeCandidateGraph({
    policy,
    runtimeIdentity: fixture.runtimeIdentity,
    snapshot: fixture.snapshot,
  });
  assert.equal(decision.eventContext, null);
  assert.equal(decision.dynamicEventHead, false);
  assert.equal(decision.graphVariant, "launch-pending");

  for (const [name, overrides] of [
    ["event ref", { eventRef: "refs/pull/1/merge" }],
    ["base ref", { baseRef: "main" }],
    ["head ref", { headRef: "codex/launch-public-cleanroom" }],
    ["base SHA", { eventBaseSha: fixture.snapshot.commits[0] }],
    ["pull head SHA", { pullRequestHeadSha: fixture.snapshot.head }],
    ["pull head repository", { pullRequestHeadRepositoryId: "1297840366" }],
  ]) {
    await t.test(name, () => {
      expectAdmissionError(
        () => authorizeCandidateGraph({
          policy,
          runtimeIdentity: { ...fixture.runtimeIdentity, ...overrides },
          snapshot: fixture.snapshot,
        }),
        "REPOSITORY_IDENTITY",
      );
    });
  }
});

test("pure graph authority requires exact provider and canary ref inventories", async (t) => {
  await t.test("missing dynamic provider ref", () => {
    const fixture = launchGraphFixture();
    fixture.snapshot.refs = fixture.snapshot.refs.filter(([ref]) => ref !== "refs/remotes/pull/1/merge");
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "UNAPPROVED_REF",
    );
  });
  await t.test("dynamic provider ref points to the wrong commit", () => {
    const fixture = launchGraphFixture();
    fixture.snapshot.refs.find(([ref]) => ref === "refs/remotes/pull/1/merge")[1] = fixture.main;
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "UNAPPROVED_REF",
    );
  });
  await t.test("duplicate dynamic provider records", () => {
    const fixture = launchGraphFixture();
    fixture.snapshot.refs.push(["refs/remotes/pull/1/merge", fixture.snapshot.head, "commit"]);
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "UNAPPROVED_REF",
    );
  });
  await t.test("annotated tag", () => {
    const fixture = launchGraphFixture();
    fixture.snapshot.refs.push(["refs/tags/foreign", fixture.snapshot.head, "tag"]);
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "UNAPPROVED_TAG",
    );
  });
  await t.test("non-commit ref", () => {
    const fixture = launchGraphFixture();
    fixture.snapshot.refs.push(["refs/heads/foreign-blob", fixture.snapshot.head, "blob"]);
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "NON_COMMIT_REF",
    );
  });
  await t.test("commit ref outside reachable inventory", () => {
    const fixture = launchGraphFixture();
    fixture.snapshot.refs.push(["refs/heads/foreign-object", fixtureOid("unreachable"), "commit"]);
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "MALFORMED_REF",
    );
  });
  await t.test("missing canary branch", () => {
    const fixture = sameTreeCanaryFixture("pre-launch", "pull_request");
    fixture.snapshot.refs = fixture.snapshot.refs.filter(([ref]) => !ref.includes("canary/public-skills/"));
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "UNAPPROVED_REF",
    );
  });
  await t.test("merge-group checkout must not create a local queue alias", () => {
    const fixture = sameTreeCanaryFixture("pre-launch", "merge_group");
    fixture.snapshot.refs.push([fixture.runtimeIdentity.eventRef, fixture.snapshot.head, "commit"]);
    expectAdmissionError(
      () => authorizeCandidateGraph({ policy, runtimeIdentity: fixture.runtimeIdentity, snapshot: fixture.snapshot }),
      "UNAPPROVED_REF",
    );
  });
});

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
  assert.match(
    workflow,
    /repository: \$\{\{ github\.repository \}\}\n          ref: \$\{\{ github\.event_name == 'pull_request' && github\.ref \|\| github\.sha \}\}\n          fetch-depth: 0/,
  );
  const candidateCheckout = workflow.slice(
    workflow.indexOf("- name: Checkout the exact candidate"),
    workflow.indexOf("- name: Checkout the source-owned policy"),
  );
  assert.equal(
    candidateCheckout.match(/ref: \$\{\{ github\.event_name == 'pull_request' && github\.ref \|\| github\.sha \}\}/g)?.length,
    1,
  );
  assert.match(workflow, /^          CANDIDATE_SHA: \$\{\{ github\.sha \}\}$/m);
  assert.match(workflow, /^          ref: \$\{\{ github\.workflow_sha \}\}$/m);
  assert.match(workflow, /^          persist-credentials: false$/m);
  assert.match(workflow, /^          EVENT_BASE_REF: \$\{\{ github\.event\.merge_group\.base_ref \|\| github\.base_ref \}\}$/m);
  assert.match(workflow, /^          EVENT_BASE_SHA: \$\{\{ github\.event\.merge_group\.base_sha \|\| github\.event\.pull_request\.base.sha \|\| '' \}\}$/m);
  assert.match(workflow, /^          EVENT_HEAD_REF: \$\{\{ github\.event\.merge_group\.head_ref \|\| github\.head_ref \}\}$/m);
  assert.match(workflow, /^          EVENT_NAME: \$\{\{ github\.event_name \}\}$/m);
  assert.match(workflow, /^          EVENT_REF: \$\{\{ github\.ref \}\}$/m);
  assert.match(workflow, /^          EVENT_REPOSITORY_ID: \$\{\{ github\.repository_id \}\}$/m);
  assert.match(workflow, /^          PULL_REQUEST_HEAD_REPOSITORY_ID: \$\{\{ github\.event\.pull_request\.head\.repo\.id \|\| '' \}\}$/m);
  assert.match(workflow, /^          PULL_REQUEST_HEAD_SHA: \$\{\{ github\.event\.pull_request\.head\.sha \|\| '' \}\}$/m);
  assert.match(workflow, /^            --event-name "\$EVENT_NAME" \\$/m);
  assert.match(workflow, /^            --event-ref "\$EVENT_REF" \\$/m);
  assert.match(workflow, /^            --base-ref "\$EVENT_BASE_REF" \\$/m);
  assert.match(workflow, /^            --head-ref "\$EVENT_HEAD_REF" \\$/m);
  assert.match(workflow, /^            --event-base-sha "\$EVENT_BASE_SHA" \\$/m);
  assert.match(workflow, /^            --pull-request-head-sha "\$PULL_REQUEST_HEAD_SHA" \\$/m);
  assert.match(workflow, /^            --event-repository-id "\$EVENT_REPOSITORY_ID" \\$/m);
  assert.match(workflow, /^            --pull-request-head-repository-id "\$PULL_REQUEST_HEAD_REPOSITORY_ID" \\$/m);
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
    protectedSourceBundle: {
      relation: "same-protected-source-commit",
      runtimeRevisionInput: "github.workflow_sha",
    },
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
  assert.deepEqual(policy.target.eventContexts, {
    launch: { pullRequestNumber: 1, headRef: "codex/launch-public-cleanroom", headRepositoryId: 1297840366, mergeGroupAllowed: true },
    negativeControl: { pullRequestNumber: 2, headRef: "canary/negative", headRepositoryId: 1297840366, mergeGroupAllowed: false },
    postMergeCanary: { pullRequestNumber: 3, headRef: "codex/post-merge-source-canary", headRepositoryId: 1297840366, mergeGroupAllowed: true },
  });
  assert.deepEqual(policy.target.postMergeCanonicalization, {
    parentCommit: "e477aee5c1d93b2bac8619fdc6f15f27483855a3",
    preLaunchTree: "e14830463796e0bea1c7cc9c6490bbb3e395a4db",
    tree: "2741f0883bf636568d375974c98301ed16a633fb",
    mainRefs: ["refs/heads/main", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
    noOpBranchRefs: ["refs/heads/codex/post-merge-source-canary", "refs/remotes/origin/codex/post-merge-source-canary"],
    releaseTagRefs: ["refs/tags/v1.0.0"],
    maximumNoOpBranchCommits: 1,
    maximumPreLaunchMainCommits: 8,
    maximumPostLaunchMainCommits: 8,
  });
  assert.deepEqual(policy.target.sameTreeCanaryClasses, [
    {
      id: "post-launch",
      headRefPattern: "^canary/public-skills/post-launch/(?:barrier-pass-through|post-merge-source|release-readback)-[0-9a-f]{12}$",
      tree: "2741f0883bf636568d375974c98301ed16a633fb",
    },
    {
      id: "pre-launch",
      headRefPattern: "^canary/public-skills/pre-launch/(?:barrier-pull-request|barrier-evaluate-merge-group|barrier-active-removal|external-pull-request|external-merge-group|evaluate-rule-suite)-[0-9a-f]{12}$",
      tree: "e14830463796e0bea1c7cc9c6490bbb3e395a4db",
    },
  ]);
  assert.deepEqual(policy.target.dynamicEventHead.eventParentSets.map((rule) => rule.event), ["merge_group", "pull_request"]);
  assert.deepEqual(
    Object.fromEntries(policy.target.dynamicEventHead.eventParentSets.map((rule) => [rule.event, {
      runtimeRefPattern: rule.runtimeRefPattern,
      gitRefPatterns: rule.gitRefPatterns,
    }])),
    {
      merge_group: {
        runtimeRefPattern: "^refs/heads/gh-readonly-queue/main/pr-[1-9][0-9]*-[0-9a-f]+$",
        gitRefPatterns: ["^refs/remotes/origin/gh-readonly-queue/main/pr-[1-9][0-9]*-[0-9a-f]+$"],
      },
      pull_request: {
        runtimeRefPattern: "^refs/pull/[1-9][0-9]*/merge$",
        gitRefPatterns: ["^refs/remotes/pull/[1-9][0-9]*/merge$"],
      },
    },
  );
  assert.equal(jsonDigest(policy.target), "45b048125a303713908f4f0cad78828f46769769c76efea376e17a6b790736bc");
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

test("adversarial fixtures preserve Actions-style remote refs from a detached source checkout", { skip: !hasCandidate }, (t) => {
  const detached = cloneCandidate(t);
  git(detached, ["checkout", "--detach", policy.target.baseline.commit]);
  for (const ref of git(detached, ["for-each-ref", "--format=%(refname)", "refs/heads"]).split("\n").filter(Boolean)) {
    git(detached, ["update-ref", "-d", ref]);
  }
  const clone = cloneCandidate(t, detached);
  assert.equal(
    git(clone, ["rev-parse", "refs/remotes/origin/codex/launch-public-cleanroom"]),
    policy.target.baseline.commit,
  );
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
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: candidateSource, policy, runtimeIdentity: runtimeIdentity(candidateSource, { eventRepositoryId: 999999999 }) }),
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
  git(pullRequest, ["update-ref", "refs/remotes/pull/1/merge", prHead]);
  const prReport = validateCandidate({
    candidateRoot: pullRequest,
    policy,
    runtimeIdentity: runtimeIdentity(pullRequest, {
      eventName: "pull_request",
      eventRef: "refs/pull/1/merge",
      baseRef: "main",
      headRef: "codex/launch-public-cleanroom",
      pullRequestHeadRepositoryId: "1297840366",
    }),
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
  git(mergeGroup, ["update-ref", "refs/remotes/origin/gh-readonly-queue/main/pr-1-abc", queueHead]);
  const queueReport = validateCandidate({
    candidateRoot: mergeGroup,
    policy,
    runtimeIdentity: runtimeIdentity(mergeGroup, {
      eventName: "merge_group",
      eventRef: "refs/heads/gh-readonly-queue/main/pr-1-abc",
      baseRef: "refs/heads/main",
      headRef: "refs/heads/gh-readonly-queue/main/pr-1-abc",
    }),
  });
  assert.equal(queueReport.candidate.dynamicEventHead, true);
  expectAdmissionError(
    () => validateCandidate({
      candidateRoot: mergeGroup,
      policy,
      runtimeIdentity: runtimeIdentity(mergeGroup, {
        eventName: "merge_group",
        eventRef: "refs/heads/gh-readonly-queue/main/pr-1-abc",
        baseRef: "refs/heads/main",
        headRef: "refs/heads/gh-readonly-queue/main/pr-1-abc",
        pullRequestHeadRepositoryId: "1297840366",
      }),
    }),
    "REPOSITORY_IDENTITY",
  );

  for (const eventIdentity of [
    {
      eventName: "pull_request",
      eventRef: "refs/pull/1/merge",
      baseRef: "main",
      headRef: "codex/launch-public-cleanroom",
      eventBaseSha: rootCommit,
      pullRequestHeadSha: approvedHead,
      pullRequestHeadRepositoryId: "1297840366",
    },
    {
      eventName: "merge_group",
      eventRef: "refs/heads/gh-readonly-queue/main/pr-1-abc",
      baseRef: "refs/heads/main",
      headRef: "refs/heads/gh-readonly-queue/main/pr-1-abc",
      eventBaseSha: rootCommit,
    },
  ]) {
    expectAdmissionError(
      () => validateCandidate({
        candidateRoot: candidateSource,
        policy,
        runtimeIdentity: runtimeIdentity(candidateSource, eventIdentity),
      }),
      "UNAPPROVED_COMMIT",
    );
  }

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

test("strict pre-launch same-tree branch classes admit PR and merge-group canaries without launch bytes", { skip: !hasCandidate }, (t) => {
  const rootCommit = policy.target.postMergeCanonicalization.parentCommit;
  const rootTree = policy.target.postMergeCanonicalization.preLaunchTree;
  const headRef = "canary/public-skills/pre-launch/barrier-pull-request-0123456789ab";

  const pullRequest = cloneCandidate(t);
  const branch = git(pullRequest, ["commit-tree", rootTree, "-p", rootCommit, "-m", "bounded pre-launch PR canary"]);
  const pullRequestHead = git(pullRequest, [
    "commit-tree",
    rootTree,
    "-p",
    rootCommit,
    "-p",
    branch,
    "-m",
    "synthetic pre-launch pull request",
  ]);
  git(pullRequest, ["checkout", "--detach", pullRequestHead]);
  git(pullRequest, ["update-ref", `refs/remotes/origin/${headRef}`, branch]);
  git(pullRequest, ["update-ref", "refs/remotes/pull/4/merge", pullRequestHead]);
  const pullReport = validateCandidate({
    candidateRoot: pullRequest,
    policy,
    runtimeIdentity: runtimeIdentity(pullRequest, {
      eventName: "pull_request",
      eventRef: "refs/pull/4/merge",
      baseRef: "main",
      headRef,
      pullRequestHeadRepositoryId: "1297840366",
    }),
  });
  assert.equal(pullReport.candidate.eventContext, "sameTreeCanary:pre-launch");
  assert.equal(pullReport.candidate.sameTreeCanaryClass, "pre-launch");
  assert.equal(pullReport.candidate.preboundHeadRepositoryId, 1297840366);
  assert.equal(pullReport.candidate.tree, rootTree);

  const mergeGroup = cloneCandidate(t);
  const mergeBranchRef = "canary/public-skills/pre-launch/barrier-evaluate-merge-group-abcdef012345";
  const mergeBranch = git(mergeGroup, ["commit-tree", rootTree, "-p", rootCommit, "-m", "bounded pre-launch queue canary"]);
  const queueHead = git(mergeGroup, ["commit-tree", rootTree, "-p", rootCommit, "-m", "synthetic queue candidate"]);
  git(mergeGroup, ["checkout", "--detach", queueHead]);
  git(mergeGroup, ["update-ref", `refs/remotes/origin/${mergeBranchRef}`, mergeBranch]);
  git(mergeGroup, ["update-ref", "refs/remotes/origin/gh-readonly-queue/main/pr-5-cafe", queueHead]);
  const queueReport = validateCandidate({
    candidateRoot: mergeGroup,
    policy,
    runtimeIdentity: runtimeIdentity(mergeGroup, {
      eventName: "merge_group",
      eventRef: "refs/heads/gh-readonly-queue/main/pr-5-cafe",
      baseRef: "refs/heads/main",
      headRef: "refs/heads/gh-readonly-queue/main/pr-5-cafe",
    }),
  });
  assert.equal(queueReport.candidate.eventContext, "sameTreeCanary:pre-launch");
  assert.equal(queueReport.candidate.sameTreeCanaryClass, "pre-launch");
  assert.equal(queueReport.candidate.preboundHeadRepositoryId, 1297840366);

  expectAdmissionError(
    () => validateCandidate({
      candidateRoot: pullRequest,
      policy,
      runtimeIdentity: runtimeIdentity(pullRequest, {
        eventName: "pull_request",
        eventRef: "refs/pull/4/merge",
        baseRef: "main",
        headRef: "canary/public-skills/post-launch/barrier-pass-through-0123456789ab",
        pullRequestHeadRepositoryId: "1297840366",
      }),
    }),
    "UNAPPROVED_REF",
  );
});

test("PR 1 launch accepts only bounded same-tree main advances with exact provider ancestry", { skip: !hasCandidate }, (t) => {
  const rootCommit = policy.target.postMergeCanonicalization.parentCommit;
  const rootTree = policy.target.postMergeCanonicalization.preLaunchTree;
  const launchHead = policy.target.baseline.commit;
  const launchTree = policy.target.baseline.tree;

  const pullRequest = cloneCandidate(t);
  const advanceOne = git(pullRequest, ["commit-tree", rootTree, "-p", rootCommit, "-m", "authorized empty main advance one"]);
  const advanceTwo = git(pullRequest, ["commit-tree", rootTree, "-p", advanceOne, "-m", "authorized empty main advance two"]);
  for (const ref of ["refs/heads/main", "refs/remotes/origin/main"]) git(pullRequest, ["update-ref", ref, advanceTwo]);
  const pullRequestHead = git(pullRequest, [
    "commit-tree",
    launchTree,
    "-p",
    advanceTwo,
    "-p",
    launchHead,
    "-m",
    "synthetic launch over advanced no-diff main",
  ]);
  git(pullRequest, ["checkout", "--detach", pullRequestHead]);
  git(pullRequest, ["update-ref", "refs/remotes/pull/1/merge", pullRequestHead]);
  const pullReport = validateCandidate({
    candidateRoot: pullRequest,
    policy,
    runtimeIdentity: runtimeIdentity(pullRequest, {
      eventName: "pull_request",
      eventRef: "refs/pull/1/merge",
      baseRef: "main",
      headRef: "codex/launch-public-cleanroom",
      pullRequestHeadRepositoryId: "1297840366",
    }),
  });
  assert.equal(pullReport.candidate.graphVariant, "launch-pending");
  assert.equal(pullReport.candidate.mainCommit, advanceTwo);
  assert.equal(pullReport.candidate.tree, launchTree);
  assert.equal(pullReport.inventory.headFiles, Object.keys(policy.expectedFiles).length);

  expectAdmissionError(
    () => validateCandidate({
      candidateRoot: pullRequest,
      policy,
      runtimeIdentity: runtimeIdentity(pullRequest, {
        eventName: "pull_request",
        eventRef: "refs/pull/1/merge",
        baseRef: "main",
        headRef: "codex/launch-public-cleanroom",
        eventBaseSha: advanceOne,
        pullRequestHeadRepositoryId: "1297840366",
      }),
    }),
    "REPOSITORY_IDENTITY",
  );

  const mergeGroup = cloneCandidate(t);
  const queueAdvance = git(mergeGroup, ["commit-tree", rootTree, "-p", rootCommit, "-m", "authorized empty queue base"]);
  for (const ref of ["refs/heads/main", "refs/remotes/origin/main"]) git(mergeGroup, ["update-ref", ref, queueAdvance]);
  const queueHead = git(mergeGroup, ["commit-tree", launchTree, "-p", queueAdvance, "-m", "synthetic squash queue launch"]);
  git(mergeGroup, ["checkout", "--detach", queueHead]);
  git(mergeGroup, ["update-ref", "refs/remotes/origin/gh-readonly-queue/main/pr-1-dead", queueHead]);
  const queueReport = validateCandidate({
    candidateRoot: mergeGroup,
    policy,
    runtimeIdentity: runtimeIdentity(mergeGroup, {
      eventName: "merge_group",
      eventRef: "refs/heads/gh-readonly-queue/main/pr-1-dead",
      baseRef: "refs/heads/main",
      headRef: "refs/heads/gh-readonly-queue/main/pr-1-dead",
    }),
  });
  assert.equal(queueReport.candidate.mainCommit, queueAdvance);
  assert.equal(queueReport.candidate.tree, launchTree);

  git(mergeGroup, ["update-ref", "refs/heads/unapproved-main-alias", queueAdvance]);
  expectAdmissionError(
    () => validateCandidate({
      candidateRoot: mergeGroup,
      policy,
      runtimeIdentity: runtimeIdentity(mergeGroup, {
        eventName: "merge_group",
        eventRef: "refs/heads/gh-readonly-queue/main/pr-1-dead",
        baseRef: "refs/heads/main",
        headRef: "refs/heads/gh-readonly-queue/main/pr-1-dead",
      }),
    }),
    "UNAPPROVED_REF",
  );
});

test("one exact post-squash main graph survives launch merge without source-policy rotation", { skip: !hasCandidate }, (t) => {
  const { clone, canonicalMain } = postMergeCandidate(t);
  const report = validateCandidate({
    candidateRoot: clone,
    policy,
    runtimeIdentity: runtimeIdentity(clone),
  });
  assert.equal(report.candidate.commit, canonicalMain);
  assert.equal(report.candidate.graphVariant, "post-merge-canonical");
  assert.equal(report.candidate.canonicalMainCommit, canonicalMain);
  assert.equal(report.candidate.dynamicEventHead, false);
  assert.equal(report.inventory.commits, 2);

  git(clone, ["update-ref", "refs/tags/v1.0.0", canonicalMain]);
  const released = validateCandidate({
    candidateRoot: clone,
    policy,
    runtimeIdentity: runtimeIdentity(clone),
  });
  assert.equal(released.candidate.canonicalMainCommit, canonicalMain);

  git(clone, ["update-ref", "refs/tags/v1.0.1", canonicalMain]);
  expectAdmissionError(
    () => validateCandidate({
      candidateRoot: clone,
      policy,
      runtimeIdentity: runtimeIdentity(clone),
    }),
    "UNAPPROVED_REF",
  );
});

test("post-merge policy admits only one exact same-tree no-op canary and event HEAD", { skip: !hasCandidate }, (t) => {
  const { clone, canonicalMain, tree } = postMergeCandidate(t);
  const branch = git(clone, [
    "commit-tree",
    tree,
    "-p",
    canonicalMain,
    "-m",
    "post-merge no-op source canary",
  ]);
  const pullRequestHead = git(clone, [
    "commit-tree",
    tree,
    "-p",
    canonicalMain,
    "-p",
    branch,
    "-m",
    "synthetic post-merge pull request",
  ]);
  git(clone, ["checkout", "--detach", pullRequestHead]);
  git(clone, ["update-ref", "refs/heads/codex/post-merge-source-canary", branch]);
  git(clone, ["update-ref", "refs/remotes/pull/3/merge", pullRequestHead]);
  const report = validateCandidate({
    candidateRoot: clone,
    policy,
    runtimeIdentity: runtimeIdentity(clone, {
      eventName: "pull_request",
      eventRef: "refs/pull/3/merge",
      baseRef: "main",
      headRef: "codex/post-merge-source-canary",
      pullRequestHeadRepositoryId: "1297840366",
    }),
  });
  assert.equal(report.candidate.graphVariant, "post-merge-canonical");
  assert.equal(report.candidate.canonicalMainCommit, canonicalMain);
  assert.equal(report.candidate.dynamicEventHead, true);
  assert.equal(report.inventory.commits, 4);
  for (const overrides of [
    { pullRequestHeadRepositoryId: "999999999" },
    { pullRequestHeadRepositoryId: "" },
    { headRef: "fork-no-op" },
    { eventRef: "refs/pull/2/merge", headRef: "canary/negative" },
    { baseRef: "develop" },
  ]) {
    expectAdmissionError(
      () => validateCandidate({
        candidateRoot: clone,
        policy,
        runtimeIdentity: runtimeIdentity(clone, {
          eventName: "pull_request",
          eventRef: "refs/pull/3/merge",
          baseRef: "main",
          headRef: "codex/post-merge-source-canary",
          pullRequestHeadRepositoryId: "1297840366",
          ...overrides,
        }),
      }),
      "REPOSITORY_IDENTITY",
    );
  }

  const mergeGroup = postMergeCandidate(t);
  const fetchedBranch = git(mergeGroup.clone, [
    "commit-tree",
    mergeGroup.tree,
    "-p",
    mergeGroup.canonicalMain,
    "-m",
    "post-merge no-op source canary",
  ]);
  const queueHead = git(mergeGroup.clone, [
    "commit-tree",
    mergeGroup.tree,
    "-p",
    mergeGroup.canonicalMain,
    "-m",
    "synthetic squash merge queue candidate",
  ]);
  git(mergeGroup.clone, ["checkout", "--detach", queueHead]);
  git(mergeGroup.clone, ["update-ref", "refs/remotes/origin/codex/post-merge-source-canary", fetchedBranch]);
  git(mergeGroup.clone, ["update-ref", "refs/remotes/origin/gh-readonly-queue/main/pr-3-abc", queueHead]);
  const queueReport = validateCandidate({
    candidateRoot: mergeGroup.clone,
    policy,
    runtimeIdentity: runtimeIdentity(mergeGroup.clone, {
      eventName: "merge_group",
      eventRef: "refs/heads/gh-readonly-queue/main/pr-3-abc",
      baseRef: "refs/heads/main",
      headRef: "refs/heads/gh-readonly-queue/main/pr-3-abc",
    }),
  });
  assert.equal(queueReport.candidate.graphVariant, "post-merge-canonical");
  assert.equal(queueReport.candidate.canonicalMainCommit, mergeGroup.canonicalMain);
  assert.equal(queueReport.candidate.dynamicEventHead, true);
  assert.equal(queueReport.inventory.commits, 4);

  git(mergeGroup.clone, ["update-ref", "refs/heads/fork-no-op", fetchedBranch]);
  git(mergeGroup.clone, ["update-ref", "-d", "refs/remotes/origin/codex/post-merge-source-canary"]);
  expectAdmissionError(
    () => validateCandidate({
      candidateRoot: mergeGroup.clone,
      policy,
      runtimeIdentity: runtimeIdentity(mergeGroup.clone, {
        eventName: "merge_group",
        eventRef: "refs/heads/gh-readonly-queue/main/pr-3-abc",
        baseRef: "refs/heads/main",
        headRef: "refs/heads/gh-readonly-queue/main/pr-3-abc",
      }),
    }),
    "UNAPPROVED_REF",
  );
});

test("post-merge canonicalization rejects partial launch history and over-bound same-tree commits", { skip: !hasCandidate }, (t) => {
  const partial = cloneCandidate(t);
  const rootCommit = policy.target.postMergeCanonicalization.parentCommit;
  const launchMiddle = policy.target.approvedCommits.find((record) => record.parents[0] === rootCommit).commit;
  const canonicalMain = git(partial, [
    "commit-tree",
    policy.target.postMergeCanonicalization.tree,
    "-p",
    rootCommit,
    "-m",
    "canonical squash merge",
  ]);
  git(partial, ["checkout", "--detach", canonicalMain]);
  const partialRefs = git(partial, ["for-each-ref", "--format=%(refname)"]).split("\n").filter(Boolean);
  for (const ref of partialRefs) git(partial, ["update-ref", "-d", ref]);
  git(partial, ["update-ref", "refs/heads/main", canonicalMain]);
  git(partial, ["update-ref", "refs/heads/partial-launch", launchMiddle]);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: partial, policy, runtimeIdentity: runtimeIdentity(partial) }),
    "MIXED_HISTORY_VARIANT",
  );

  const extra = postMergeCandidate(t);
  let extraCommit = extra.canonicalMain;
  for (let index = 0; index <= policy.target.postMergeCanonicalization.maximumPostLaunchMainCommits; index += 1) {
    extraCommit = git(extra.clone, [
      "commit-tree",
      extra.tree,
      "-p",
      extraCommit,
      "-m",
      `over-bound same-tree extension ${index}`,
    ]);
  }
  git(extra.clone, ["checkout", "--detach", extraCommit]);
  git(extra.clone, ["update-ref", "refs/heads/main", extraCommit]);
  expectAdmissionError(
    () => validateCandidate({ candidateRoot: extra.clone, policy, runtimeIdentity: runtimeIdentity(extra.clone) }),
    "HISTORY_LIMIT",
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
