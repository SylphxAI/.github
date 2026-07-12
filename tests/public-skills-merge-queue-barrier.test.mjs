import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  BarrierError,
  canonicalDigest,
  executeBarrier,
  normalizeRepositorySnapshot,
  reportExitCode,
  validateInvocation,
  validatePolicy,
} from "../scripts/public-skills-merge-queue-barrier.mjs";

const SOURCE_SHA = "d".repeat(40);
const BASE_SHA = "a".repeat(40);
const CANDIDATE_SHA = "b".repeat(40);
const HEAD_SHA = "c".repeat(40);
const POLICY = JSON.parse(readFileSync(new URL("../policies/public-skills-merge-queue-barrier.json", import.meta.url), "utf8"));

function clone(value) {
  return structuredClone(value);
}

function repositoryPayload(name = "SylphxAI/skills-public-cleanroom") {
  return {
    id: 1297840366,
    node_id: "R_kgDOTVt47g",
    full_name: name,
    default_branch: "main",
  };
}

function runtime(eventName, overrides = {}) {
  return {
    repository: "SylphxAI/skills-public-cleanroom",
    repositoryId: "1297840366",
    repositoryNodeId: "R_kgDOTVt47g",
    eventName,
    eventRef: eventName === "merge_group"
      ? "refs/heads/gh-readonly-queue/main/pr-1-deadbeef"
      : "refs/pull/1/merge",
    candidateSha: CANDIDATE_SHA,
    sourceSha: SOURCE_SHA,
    ...overrides,
  };
}

function pullRequestEvent(overrides = {}) {
  return {
    action: "synchronize",
    repository: repositoryPayload(),
    pull_request: {
      number: 1,
      merge_commit_sha: CANDIDATE_SHA,
      base: { ref: "main", sha: BASE_SHA, repo: { id: 1297840366 } },
      head: { ref: "codex/launch-public-cleanroom", sha: HEAD_SHA, repo: { id: 1297840366 } },
    },
    ...overrides,
  };
}

function mergeGroupEvent(overrides = {}) {
  return {
    action: "checks_requested",
    repository: repositoryPayload(),
    merge_group: {
      base_ref: "refs/heads/main",
      base_sha: BASE_SHA,
      head_ref: "refs/heads/gh-readonly-queue/main/pr-1-deadbeef",
      head_sha: CANDIDATE_SHA,
    },
    ...overrides,
  };
}

function snapshotPayload({
  name = "SylphxAI/skills-public-cleanroom",
  mainSha = BASE_SHA,
  headSha = HEAD_SHA,
  headRef = "codex/launch-public-cleanroom",
  queue = true,
  autoMerge = true,
  merged = false,
  state = "OPEN",
  queueBaseSha = BASE_SHA,
  queueHeadSha = CANDIDATE_SHA,
} = {}) {
  return {
    data: {
      repository: {
        id: "R_kgDOTVt47g",
        nameWithOwner: name,
        defaultBranchRef: { name: "main", target: { oid: mainSha } },
        pullRequest: {
          id: "PR_kwDOTVt47s7woraU",
          number: 1,
          state,
          merged,
          baseRefName: "main",
          headRefName: headRef,
          headRefOid: headSha,
          baseRepository: { id: "R_kgDOTVt47g", nameWithOwner: name },
          headRepository: { id: "R_kgDOTVt47g", nameWithOwner: name },
          autoMergeRequest: autoMerge ? { enabledAt: "2026-07-12T14:00:00Z" } : null,
          mergeQueueEntry: queue
            ? {
                id: "MQE_kwDOTVt47g4A",
                position: 1,
                state: "AWAITING_CHECKS",
                baseCommit: { oid: queueBaseSha },
                headCommit: { oid: queueHeadSha },
                pullRequest: { id: "PR_kwDOTVt47s7woraU", number: 1 },
              }
            : null,
        },
      },
    },
  };
}

function externalCheckPayload(conclusion = "success", overrides = {}) {
  const check = {
    id: 7001,
    name: "public-skills-external-admission/pass",
    head_sha: CANDIDATE_SHA,
    status: "completed",
    conclusion,
    details_url: "https://github.com/SylphxAI/skills-public-cleanroom/actions/runs/6001/job/7001",
    app: { id: 15368, slug: "github-actions", name: "GitHub Actions" },
    ...overrides,
  };
  return { total_count: 1, check_runs: [check] };
}

function workflowRun(conclusion = "success", overrides = {}) {
  return {
    id: 6001,
    path: `.github/workflows/public-skills-admission.yml@${SOURCE_SHA}`,
    event: "merge_group",
    status: "completed",
    conclusion,
    head_sha: CANDIDATE_SHA,
    run_attempt: 1,
    created_at: "2026-07-12T14:01:00Z",
    updated_at: "2026-07-12T14:02:00Z",
    repository: { id: 1297840366, full_name: "SylphxAI/skills-public-cleanroom" },
    ...overrides,
  };
}

function candidateRuleSuite({
  id = 8001,
  aggregateResult = null,
  repositoryId = 1297840366,
  afterSha = CANDIDATE_SHA,
  ref = "refs/heads/main",
  enforcement = "active",
  result = "pass",
  evaluations = null,
} = {}) {
  const external = {
    rule_source: { type: "ruleset", id: 18831380, name: "public-skills-external-admission" },
    rule_type: "workflows",
    enforcement,
    result,
    details: "Required workflow evaluated successfully",
  };
  return {
    id,
    repository_id: repositoryId,
    before_sha: BASE_SHA,
    after_sha: afterSha,
    ref,
    pushed_at: "2026-07-12T14:00:30Z",
    result: aggregateResult,
    rule_evaluations: evaluations ?? [external],
  };
}

function candidateRuleSuiteSummary(suite = candidateRuleSuite()) {
  return [{
    id: suite.id,
    repository_id: suite.repository_id,
    before_sha: suite.before_sha,
    after_sha: suite.after_sha,
    ref: suite.ref,
    pushed_at: suite.pushed_at,
    result: suite.result,
  }];
}

function workflowJobs(conclusion = "success", overrides = {}) {
  const job = {
    id: 7001,
    name: "public-skills-external-admission/pass",
    head_sha: CANDIDATE_SHA,
    status: "completed",
    conclusion,
    ...overrides,
  };
  return { total_count: 1, jobs: [job] };
}

function effectiveRuleset(overrides = {}) {
  return {
    id: 18831380,
    name: "public-skills-external-admission",
    source_type: "Organization",
    source: "SylphxAI",
    target: "branch",
    enforcement: "active",
    bypass_actors: [],
    current_user_can_bypass: "never",
    conditions: { ref_name: { exclude: [], include: ["~DEFAULT_BRANCH"] } },
    rules: [{
      type: "workflows",
      parameters: {
        do_not_enforce_on_create: false,
        workflows: [{
          repository_id: 1091169653,
          path: ".github/workflows/public-skills-admission.yml",
          sha: SOURCE_SHA,
          ref: "main",
        }],
      },
    }],
    ...overrides,
  };
}

class FakeApi {
  constructor({
    snapshots,
    checks = [externalCheckPayload()],
    run = workflowRun(),
    jobs = workflowJobs(),
    suite = candidateRuleSuite(),
    suiteSummaries = null,
    rulesetStates = ["not-effective"],
    ruleset = effectiveRuleset(),
  }) {
    this.snapshots = snapshots.map(clone);
    this.checks = checks.map(clone);
    this.run = clone(run);
    this.jobs = clone(jobs);
    this.suite = clone(suite);
    this.suiteSummaries = (suiteSummaries ?? candidateRuleSuiteSummary(suite)).map(clone);
    this.rulesetStates = [...rulesetStates];
    this.ruleset = clone(ruleset);
    this.calls = [];
  }

  next(values) {
    if (values.length > 1) return values.shift();
    return values[0];
  }

  async graphql(query, variables) {
    this.calls.push({ kind: "graphql", query, variables: clone(variables) });
    if (query.includes("PublicSkillsBarrierSnapshot")) return clone(this.next(this.snapshots));
    throw new Error("unexpected GraphQL query");
  }

  async rest(path) {
    this.calls.push({ kind: "rest", path });
    if (path.includes("/check-runs?")) return clone(this.next(this.checks));
    if (path.includes("/actions/runs/6001/jobs?")) return clone(this.jobs);
    if (path.endsWith("/actions/runs/6001")) return clone(this.run);
    if (path.includes("/rulesets/rule-suites?")) return clone(this.suiteSummaries);
    if (path.endsWith(`/rulesets/rule-suites/${this.suite.id}`)) return clone(this.suite);
    if (path.includes("/rulesets?includes_parents=true")) {
      const state = this.next(this.rulesetStates);
      return state === "active-effective"
        ? [{ id: 18831380, name: "public-skills-external-admission", enforcement: "active" }]
        : [];
    }
    if (path.includes("/rulesets/18831380?")) return clone(this.ruleset);
    throw new Error(`unexpected REST path ${path}`);
  }
}

async function runMerge({ api, event = mergeGroupEvent(), runtimeValue = runtime("merge_group") } = {}) {
  return executeBarrier(
    {
      event,
      runtime: runtimeValue,
      policy: clone(POLICY),
      source: {
        repository: "SylphxAI/.github",
        commit: SOURCE_SHA,
        protectedSourceBundle: POLICY.source.protectedSourceBundle,
      },
    },
    { api, sleep: async () => {} },
  );
}

async function expectBarrierError(promise, code) {
  await assert.rejects(promise, (error) => error instanceof BarrierError && error.code === code);
}

test("policy is an exact immutable target/external/barrier contract", () => {
  assert.deepEqual(validatePolicy(clone(POLICY)), POLICY);
  const changed = clone(POLICY);
  changed.source.protectedSourceBundle.relation = "same-branch-name";
  assert.throws(() => validatePolicy(changed), (error) => error.code === "POLICY_IDENTITY");
  const widened = clone(POLICY);
  widened.barrier.bypassActors.push({ actor_id: 1 });
  assert.throws(() => validatePolicy(widened), (error) => error.code === "POLICY_IDENTITY");
});

test("pull_request validates exact same-repository identity and never mutates", async () => {
  const api = new FakeApi({ snapshots: [snapshotPayload({ queue: false, autoMerge: false })] });
  const report = await executeBarrier(
    {
      event: pullRequestEvent(),
      runtime: runtime("pull_request"),
      policy: clone(POLICY),
      source: {
        repository: "SylphxAI/.github",
        commit: SOURCE_SHA,
        protectedSourceBundle: POLICY.source.protectedSourceBundle,
      },
    },
    { api, sleep: async () => {} },
  );
  assert.equal(report.status, "pass");
  assert.equal(report.decision.action, "pass-pull-request-identity");
  assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
  assert.equal(api.calls.filter((call) => call.kind === "rest").length, 0);
});

test("pull_request rejects a fork head and a foreign target name", () => {
  const fork = pullRequestEvent();
  fork.pull_request.head.repo.id = 2;
  assert.throws(
    () => validateInvocation({ event: fork, runtime: runtime("pull_request"), policy: clone(POLICY) }),
    (error) => error.code === "EVENT_IDENTITY",
  );
  const foreign = runtime("pull_request", { repository: "SylphxAI/not-skills" });
  assert.throws(
    () => validateInvocation({ event: pullRequestEvent(), runtime: foreign, policy: clone(POLICY) }),
    (error) => error.code === "TARGET_IDENTITY",
  );
});

test("evaluate/not-effective merge_group fails and leaves queue ownership to GitHub", async () => {
  const api = new FakeApi({
    snapshots: [snapshotPayload(), snapshotPayload()],
    suite: candidateRuleSuite({ enforcement: "evaluate", result: "pass" }),
    rulesetStates: ["not-effective"],
  });
  const report = await runMerge({ api });
  assert.equal(report.status, "fail");
  assert.equal(report.externalAdmission.check.conclusion, "success");
  assert.equal(report.decision.action, "reject-merge-group");
  assert.equal(report.decision.requirements.externalConclusion.satisfied, true);
  assert.equal(report.externalRuleSuite.suite.externalRuleEvaluation.enforcement, "evaluate");
  assert.equal(report.externalRuleSuite.suite.externalRuleEvaluation.result, "pass");
  assert.equal(report.decision.requirements.externalRuleSuite.satisfied, false);
  assert.equal(report.decision.requirements.externalRuleset.satisfied, false);
  assert.deepEqual(report.queueMutation, { owner: "github-provider", attempted: false });
  assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
});

test("active external failure returns exact rejected suite evidence without a queue mutation", async () => {
  const api = new FakeApi({
    snapshots: [snapshotPayload(), snapshotPayload()],
    checks: [externalCheckPayload("failure")],
    run: workflowRun("failure"),
    jobs: workflowJobs("failure"),
    suite: candidateRuleSuite({ enforcement: "active", result: "fail" }),
    rulesetStates: ["active-effective", "active-effective"],
  });
  const report = await runMerge({ api });
  assert.equal(report.status, "fail");
  assert.equal(report.externalAdmission.check.conclusion, "failure");
  assert.equal(report.decision.action, "reject-merge-group");
  assert.equal(report.decision.requirements.externalConclusion.satisfied, false);
  assert.equal(report.externalRuleSuite.suite.externalRuleEvaluation.enforcement, "active");
  assert.equal(report.externalRuleSuite.suite.externalRuleEvaluation.result, "fail");
  assert.equal(report.decision.requirements.externalRuleSuite.satisfied, false);
  assert.equal(report.decision.requirements.externalRuleset.satisfied, true);
  assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
});

test("active/effective exact ruleset plus successful external admission passes read-only", async () => {
  const api = new FakeApi({
    snapshots: [snapshotPayload(), snapshotPayload()],
    rulesetStates: ["active-effective", "active-effective"],
  });
  const report = await runMerge({ api });
  assert.equal(report.status, "pass");
  assert.equal(report.decision.action, "pass-active-admission");
  assert.equal(report.decision.admitted, true);
  assert.equal(report.externalRuleSuite.suite.aggregateResult, null);
  assert.deepEqual(report.externalRuleSuite.suite.externalRuleEvaluation, {
    ruleSource: { id: 18831380, name: "public-skills-external-admission", type: "ruleset" },
    ruleType: "workflows",
    enforcement: "active",
    result: "pass",
  });
  assert.equal(report.decision.requirements.externalRuleSuite.satisfied, true);
  assert.equal(report.decision.mutationAttempted, false);
  assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
});

test("candidate aggregate null, pass, or unrelated fail never replaces the exact external rule verdict", async (t) => {
  for (const aggregateResult of [null, "pass", "fail"]) {
    await t.test(String(aggregateResult), async () => {
      const suite = candidateRuleSuite({ aggregateResult });
      const api = new FakeApi({
        snapshots: [snapshotPayload(), snapshotPayload()],
        rulesetStates: ["active-effective", "active-effective"],
        suite,
      });
      const report = await runMerge({ api });
      assert.equal(report.status, "pass");
      assert.equal(report.externalRuleSuite.suite.aggregateResult, aggregateResult);
      assert.equal(report.externalRuleSuite.suite.externalRuleEvaluation.result, "pass");
    });
  }
});

test("spoofed, duplicate, foreign, bypassed, or non-active candidate rule suites fail closed", async (t) => {
  const exact = candidateRuleSuite().rule_evaluations[0];
  const attacks = [
    ["duplicate summaries", {
      suiteSummaries: [
        ...candidateRuleSuiteSummary(),
        { ...candidateRuleSuiteSummary()[0], id: 8002 },
      ],
    }, "PROVIDER_IDENTITY"],
    ["foreign summary repository", {
      suiteSummaries: candidateRuleSuiteSummary(candidateRuleSuite({ repositoryId: 999 })),
    }, "PROVIDER_IDENTITY"],
    ["foreign detail repository", {
      suite: candidateRuleSuite({ repositoryId: 999 }),
      suiteSummaries: candidateRuleSuiteSummary(),
    }, "PROVIDER_IDENTITY"],
    ["foreign detail ref", {
      suite: candidateRuleSuite({ ref: "refs/heads/foreign" }),
      suiteSummaries: candidateRuleSuiteSummary(),
    }, "PROVIDER_IDENTITY"],
    ["foreign detail head", {
      suite: candidateRuleSuite({ afterSha: "e".repeat(40) }),
      suiteSummaries: candidateRuleSuiteSummary(),
    }, "PROVIDER_IDENTITY"],
    ["spoofed source type", {
      suite: candidateRuleSuite({ evaluations: [{ ...exact, rule_source: { ...exact.rule_source, type: "Organization" } }] }),
    }, "PROVIDER_IDENTITY"],
    ["foreign ID reusing exact name", {
      suite: candidateRuleSuite({ evaluations: [{ ...exact, rule_source: { ...exact.rule_source, id: 999 } }] }),
    }, "PROVIDER_IDENTITY"],
    ["duplicate exact evaluation", {
      suite: candidateRuleSuite({ evaluations: [exact, { ...exact }] }),
    }, "PROVIDER_IDENTITY"],
    ["unknown enforcement", {
      suite: candidateRuleSuite({ evaluations: [{ ...exact, enforcement: "disabled" }] }),
    }, "PROVIDER_STATE"],
    ["unknown external result", {
      suite: candidateRuleSuite({ evaluations: [{ ...exact, result: "bypass" }] }),
    }, "PROVIDER_STATE"],
    ["aggregate bypass", {
      suite: candidateRuleSuite({ aggregateResult: "bypass" }),
    }, "PROVIDER_STATE"],
  ];
  for (const [name, fixture, code] of attacks) {
    await t.test(name, async () => {
      const api = new FakeApi({ snapshots: [snapshotPayload()], ...fixture });
      await expectBarrierError(runMerge({ api }), code);
      assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
    });
  }
});

test("active/effective external failure fails the barrier too", async () => {
  const api = new FakeApi({
    snapshots: [snapshotPayload(), snapshotPayload()],
    checks: [externalCheckPayload("failure")],
    run: workflowRun("failure"),
    jobs: workflowJobs("failure"),
    suite: candidateRuleSuite({ enforcement: "active", result: "fail" }),
    rulesetStates: ["active-effective", "active-effective"],
  });
  const report = await runMerge({ api });
  assert.equal(report.status, "fail");
  assert.equal(report.decision.action, "reject-merge-group");
  assert.equal(report.decision.requirements.externalConclusion.satisfied, false);
  assert.equal(report.decision.requirements.externalRuleset.satisfied, true);
  assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
});

test("check conclusion and exact external rule result must agree", async (t) => {
  for (const [name, checkConclusion, ruleResult] of [
    ["success-check with fail-rule", "success", "fail"],
    ["failure-check with pass-rule", "failure", "pass"],
  ]) {
    await t.test(name, async () => {
      const api = new FakeApi({
        snapshots: [snapshotPayload()],
        checks: [externalCheckPayload(checkConclusion)],
        run: workflowRun(checkConclusion),
        jobs: workflowJobs(checkConclusion),
        suite: candidateRuleSuite({ enforcement: "active", result: ruleResult }),
      });
      await expectBarrierError(runMerge({ api }), "PROVIDER_STATE");
      assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
    });
  }
});

test("active-to-inactive race fails without touching a successor queue entry", async () => {
  const api = new FakeApi({
    snapshots: [snapshotPayload(), snapshotPayload()],
    rulesetStates: ["active-effective", "not-effective"],
  });
  const report = await runMerge({ api });
  assert.equal(report.status, "fail");
  assert.equal(report.decision.action, "reject-merge-group");
  assert.equal(report.effectiveRuleset.first.state, "active-effective");
  assert.equal(report.effectiveRuleset.confirmation.state, "not-effective");
  assert.equal(report.decision.mutationAttempted, false);
});

test("a stale run cannot authorize or mutate a regrouped successor candidate", async () => {
  const api = new FakeApi({
    snapshots: [
      snapshotPayload(),
      snapshotPayload({ queueHeadSha: "e".repeat(40) }),
    ],
    rulesetStates: ["active-effective", "active-effective"],
  });
  await expectBarrierError(runMerge({ api }), "QUEUE_RACE");
  assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
});

test("missing external check times out without any queue mutation", async () => {
  const policy = clone(POLICY);
  policy.polling.externalCheckAttempts = 2;
  const api = new FakeApi({
    snapshots: [snapshotPayload()],
    checks: [{ total_count: 0, check_runs: [] }, { total_count: 0, check_runs: [] }],
  });
  await expectBarrierError(
    executeBarrier(
      {
        event: mergeGroupEvent(),
        runtime: runtime("merge_group"),
        policy,
        source: {
          repository: "SylphxAI/.github",
          commit: SOURCE_SHA,
          protectedSourceBundle: POLICY.source.protectedSourceBundle,
        },
      },
      { api, sleep: async () => {} },
    ),
    "EXTERNAL_ADMISSION_TIMEOUT",
  );
  assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
});

test("duplicate, foreign-producer, wrong-head, and neutral external checks fail closed", async (t) => {
  const attacks = [
    ["duplicate", { total_count: 2, check_runs: [externalCheckPayload().check_runs[0], { ...externalCheckPayload().check_runs[0], id: 7002 }] }, "PROVIDER_IDENTITY"],
    ["foreign producer", externalCheckPayload("success", { app: { id: 1, slug: "spoof", name: "Spoof" } }), "PROVIDER_IDENTITY"],
    ["wrong head", externalCheckPayload("success", { head_sha: "e".repeat(40) }), "PROVIDER_IDENTITY"],
    ["neutral", externalCheckPayload("success", { conclusion: "neutral" }), "PROVIDER_STATE"],
  ];
  for (const [name, checks, code] of attacks) {
    await t.test(name, async () => {
      const api = new FakeApi({ snapshots: [snapshotPayload()], checks: [checks] });
      await expectBarrierError(runMerge({ api }), code);
      assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
    });
  }
});

test("wrong external workflow source SHA, path, event, job, or repository fails closed", async (t) => {
  const attacks = [
    ["source SHA", workflowRun("success", { path: `.github/workflows/public-skills-admission.yml@${"e".repeat(40)}` }), workflowJobs(), "PROVIDER_IDENTITY"],
    ["path", workflowRun("success", { path: ".github/workflows/spoof.yml" }), workflowJobs(), "PROVIDER_IDENTITY"],
    ["event", workflowRun("success", { event: "pull_request" }), workflowJobs(), "PROVIDER_IDENTITY"],
    ["job", workflowRun(), workflowJobs("success", { name: "spoof" }), "PROVIDER_IDENTITY"],
    ["repository", workflowRun("success", { repository: { id: 3, full_name: "SylphxAI/other" } }), workflowJobs(), "PROVIDER_IDENTITY"],
  ];
  for (const [name, run, jobs, code] of attacks) {
    await t.test(name, async () => {
      const api = new FakeApi({ snapshots: [snapshotPayload()], run, jobs });
      await expectBarrierError(runMerge({ api }), code);
    });
  }
});

test("effective ruleset semantic drift fails closed without any queue mutation", async (t) => {
  const attacks = [
    ["wrong SHA", { rules: [{ type: "workflows", parameters: { do_not_enforce_on_create: false, workflows: [{ repository_id: 1091169653, path: ".github/workflows/public-skills-admission.yml", sha: "e".repeat(40), ref: "main" }] } }] }],
    ["bypass", { bypass_actors: [{ actor_id: 1 }] }],
    ["actor bypass", { current_user_can_bypass: "always" }],
    ["selector", { conditions: { ref_name: { exclude: [], include: ["refs/heads/other"] } } }],
    ["enforcement", { enforcement: "evaluate" }],
  ];
  for (const [name, override] of attacks) {
    await t.test(name, async () => {
      const api = new FakeApi({
        snapshots: [snapshotPayload(), snapshotPayload()],
        rulesetStates: ["active-effective"],
        ruleset: effectiveRuleset(override),
      });
      await expectBarrierError(runMerge({ api }), "RULESET_DRIFT");
      assert.equal(api.calls.every((call) => !call.query?.includes("mutation")), true);
    });
  }
});

test("foreign same-name effective ruleset fails closed", async () => {
  const api = new FakeApi({ snapshots: [snapshotPayload(), snapshotPayload()], rulesetStates: ["not-effective"] });
  const originalRest = api.rest.bind(api);
  api.rest = async (path) => path.includes("/rulesets?includes_parents=true")
    ? [{ id: 999, name: "public-skills-external-admission", enforcement: "active" }]
    : originalRest(path);
  await expectBarrierError(runMerge({ api }), "RULESET_DRIFT");
});

test("merge-group main/base, queue presence, and provider ref are exact preconditions", () => {
  const context = validateInvocation({ event: mergeGroupEvent(), runtime: runtime("merge_group"), policy: clone(POLICY) });
  const wrongMain = snapshotPayload({ mainSha: "e".repeat(40) });
  const normalized = normalizeRepositorySnapshot(wrongMain, context, POLICY);
  assert.equal(normalized.repository.mainSha, "e".repeat(40));
  const wrongRef = mergeGroupEvent();
  wrongRef.merge_group.head_ref = "refs/heads/gh-readonly-queue/main/not-a-pr";
  assert.throws(
    () => validateInvocation({ event: wrongRef, runtime: runtime("merge_group", { eventRef: wrongRef.merge_group.head_ref }), policy: clone(POLICY) }),
    (error) => error.code === "EVENT_IDENTITY",
  );
});

test("canonical report digest is stable across object insertion order", () => {
  assert.equal(canonicalDigest({ b: 2, a: { d: 4, c: 3 } }), canonicalDigest({ a: { c: 3, d: 4 }, b: 2 }));
});

test("a well-formed rejected report is evidence and still exits nonzero", () => {
  assert.equal(reportExitCode({ status: "pass" }), 0);
  assert.equal(reportExitCode({ status: "fail" }), 1);
  assert.throws(() => reportExitCode({ status: "pending" }), (error) => error.code === "REPORT_STATE");
});

test("workflow is source-only, exact-target checked, SHA-pinned, and least privilege", () => {
  const workflow = readFileSync(new URL("../.github/workflows/public-skills-merge-queue-barrier.yml", import.meta.url), "utf8");
  for (const permission of ["actions: read", "checks: read", "contents: read", "pull-requests: read"]) assert.match(workflow, new RegExp(`  ${permission}`));
  assert.doesNotMatch(workflow, /pull-requests: write/);
  assert.match(workflow, /github\.repository_id != 1091169653/);
  assert.match(workflow, /repository: SylphxAI\/\.github/);
  assert.match(workflow, /ref: \$\{\{ github\.workflow_sha \}\}/);
  assert.match(workflow, /actions\/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5/);
  assert.match(workflow, /actions\/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02/);
  assert.doesNotMatch(workflow, /repository: \$\{\{ github\.repository \}\}/);
  assert.doesNotMatch(workflow, /pull_request_target/);
  assert.doesNotMatch(workflow, /cancel-in-progress/);
});

test("controller has no queue mutation surface and delegates removal to GitHub", () => {
  const controller = readFileSync(new URL("../scripts/public-skills-merge-queue-barrier.mjs", import.meta.url), "utf8");
  assert.doesNotMatch(controller, /dequeuePullRequest|PublicSkillsBarrierDequeue|clientMutationId/);
  assert.match(controller, /queueOwner: "github-provider"/);
  assert.match(controller, /mutationAttempted: false/);
  assert.deepEqual(POLICY.barrier, {
    rulesetName: "public-skills-merge-queue-barrier",
    requiredCheck: "public-skills-merge-queue-barrier/pass",
    initialEnforcement: "evaluate",
    requiredEnforcement: "active",
    bypassActors: [],
    refInclude: ["~DEFAULT_BRANCH"],
    refExclude: [],
    doNotEnforceOnCreate: false,
  });
});
