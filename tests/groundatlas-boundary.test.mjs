import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));
const readText = (path) => readFileSync(path, "utf8");

test("project manifest is the vendor-neutral GroundAtlas control file", () => {
  const manifest = readJson("project.manifest.json");

  assert.equal(manifest.schemaVersion, 1);
  assert.equal(manifest.project.id, "sylphxai-github");
  assert.equal(manifest.project.repository, "https://github.com/SylphxAI/.github");
  assert.equal(manifest.adoption.status, "adopted");
  assert.equal(manifest.truth.agentAdapter, "AGENTS.md");
  assert.ok(
    manifest.surfaces.some(
      (surface) =>
        surface.path === ".doctrine/project.json" &&
        surface.description.includes("not the vendor-neutral GroundAtlas default"),
    ),
  );
  assert.ok(
    manifest.adoption.notes.includes("Generated .groundatlas* reports are evidence/navigation only"),
  );
});

test("doctrine adapter remains Sylphx-specific and does not replace the neutral manifest", () => {
  const doctrine = readJson(".doctrine/project.json");

  assert.equal(doctrine.project.repo, "SylphxAI/.github");
  assert.equal(doctrine.adoption.status, "adopted");
  assert.ok(
    doctrine.boundaries.publicSurfaces.some(
      (surface) => surface.type === "manifest" && surface.location === "project.manifest.json",
    ),
  );
  assert.ok(
    doctrine.boundaries.allowedDependencies.some(
      (dependency) => dependency.repo === "SylphxAI/groundatlas",
    ),
  );
});

test("local workflow dogfoods the released GroundAtlas package and action", () => {
  const workflow = readText(".github/workflows/groundatlas.yml");
  const admissionValidator = readText("scripts/public-skills-admission.mjs");
  const admissionTests = readText("tests/public-skills-admission.test.mjs");

  assert.ok(workflow.includes("runs-on: ubuntu-24.04"));
  assert.ok(!workflow.includes("self-hosted"));
  assert.ok(workflow.includes("uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5"));
  assert.ok(workflow.includes("persist-credentials: false"));
  assert.ok(workflow.includes("uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020"));
  assert.ok(workflow.includes("uses: SylphxAI/groundatlas@38ce903733901cd2954a01aa4d31d7968de00ead"));
  assert.ok(workflow.includes("uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"));
  assert.ok(workflow.includes("node --test tests/public-skills-admission.test.mjs"));
  assert.ok(workflow.includes("node --test tests/public-skills-merge-queue-barrier.test.mjs"));
  assert.equal(
    workflow.match(/node --test tests\/public-skills-merge-queue-barrier\.test\.mjs/g)?.length,
    1,
    "the protected workflow must run exactly one merge-queue barrier suite",
  );
  assert.ok(admissionValidator.includes("export function authorizeCandidateGraph"));
  assert.ok(admissionValidator.includes("const graphAuthorization = authorizeCandidateGraph({"));
  assert.equal(
    admissionValidator.match(/const graphAuthorization = authorizeCandidateGraph\(\{/g)?.length,
    1,
    "production must call the pure graph authority exactly once",
  );
  const productionAdapter = admissionValidator.split("export function validateCandidate", 2)[1];
  assert.ok(productionAdapter, "production checkout adapter is missing");
  assert.ok(!productionAdapter.includes("GRAPH_AUTHORIZATION_DRIFT"));
  for (const removedDuplicateMarker of [
    "let eventContext",
    "approvedCommitMap",
    "selectSameTreeCanaryBranch",
    "usesDynamicEventHead",
  ]) {
    assert.ok(!productionAdapter.includes(removedDuplicateMarker), "duplicate graph authority returned: " + removedDuplicateMarker);
  }
  assert.ok(productionAdapter.includes("} = graphAuthorization;"));
  for (const requiredGraphGate of [
    "pure graph authority admits PR1 after bounded same-tree main advancement",
    "pure graph authority admits strict pre-launch and post-launch PR and merge-group canaries",
    "pure graph authority rejects stale provider SHA, parent drift, and foreign refs",
    "pure graph authority rejects overflow and foreign-root inventories",
    "pure graph authority admits non-event baseline only without provider event claims",
    "pure graph authority requires exact provider and canary ref inventories",
  ]) {
    assert.ok(admissionTests.includes(requiredGraphGate), "protected CI lost required graph gate: " + requiredGraphGate);
  }
  assert.ok(workflow.includes("package-spec: groundatlas@0.1.2"));
  assert.ok(workflow.includes('require-atlas: "true"'));
  assert.ok(workflow.includes('strict: "true"'));
  assert.ok(workflow.includes("project.manifest.json"));
  assert.ok(workflow.includes(".doctrine/project.json"));
});
