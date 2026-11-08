import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { APP_ID, CHECK_NAME, GitHub, PublishUnverified, digest, bundleFile } from '../.github/actions/pr-review/github.mjs';
import { produceReview, PUBLISH_RESERVE_MS } from '../.github/actions/pr-review/producer.mjs';
import { createReviewInput, reviewerVersion } from '../.github/actions/pr-review/contract.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40);
const target = { kind: 'pull_request', number: 7, base_sha: base, head_sha: head, repository_id: '1',
  files: [{ filename: 'src/change.js', status: 'modified' }] };
const externalId = `pr-review/v1/${digest(JSON.stringify(['example/repo', 'pull_request', 7, base, undefined, head, undefined,
  { kind: 'workflow_run', id: '7', generation: '1' }, reviewerVersion]))}`;
const input = createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: base, head_sha: head, policy_version: 'missing', trigger: { kind: 'workflow_run', id: '7', generation: '1' },
  files: [bundleFile(target.files[0], 'before\n', 'after\n')], context: [] });
const inputIdentity = `Head: \`${head}\`\n\nReviewer: \`${reviewerVersion}\`\n\nSource digest: \`${input.input_digest}\`\n\n`;
const initial = () => ({ output: { summary: `${inputIdentity}Reviewing exact head \`${head}\`.` }, details_url: 'https://github.com/example/repo/actions/runs/1', external_id: externalId, id: 41, app: { id: APP_ID }, name: CHECK_NAME, head_sha: head, status: 'in_progress' });
const output = { title: 'Review complete', summary: 'private-source-and-credential', annotations: [] };
const response = data => ({ ok: true, status: 200, json: async () => structuredClone(data) });
function boundary({ observe = run => run, patchError, create = false } = {}) {
  const writes = [], waits = [], events = [];
  let run = create ? null : initial(), reads = 0, now = 0;
  const github = new GitHub({ repository: 'example/repo', readToken: 'source', checkToken: 'checks',
    now: () => now, wait: async ms => { waits.push(ms); now += ms; },
    fetcher: async (url, request) => {
      const path = new URL(url).pathname;
      if (path.endsWith('/actions/runs/1')) {
        assert.equal(request.headers.authorization, 'Bearer source'); return response({ id: 1, status: 'in_progress' });
      }
      assert.equal(request.headers.authorization, 'Bearer checks');
      if (path.includes('/commits/')) return response({ check_runs: run ? [run] : [] });
      if (request.method === 'POST') {
        assert.equal(path, '/repos/example/repo/check-runs'); events.push('POST');
        run = { ...JSON.parse(request.body), id: 41, app: { id: APP_ID } };
        writes.push({ method: 'POST', body: JSON.parse(request.body) }); return response(run);
      }
      assert.equal(path, '/repos/example/repo/check-runs/41');
      if (request.method === 'PATCH') {
        writes.push({ method: 'PATCH', body: JSON.parse(request.body) }); events.push('PATCH');
        if (patchError) throw new Error('private-source-and-credential');
        Object.assign(run, JSON.parse(request.body)); return response(run);
      }
      events.push('GET'); reads++;
      const observed = observe(structuredClone(run), reads, request);
      if (observed instanceof Error) throw observed;
      if (typeof observed === 'number') return { ok: false, status: observed, json: () => { throw new Error('Never read error bodies'); } };
      return response(observed);
    } });
  github.current = async () => true;
  github.source = async (path, sha, optional) => {
    if (path === 'src/change.js') return sha === base ? 'before\n' : 'after\n';
    assert.equal(optional, true); return null;
  };
  return { github, writes, waits, events, run: () => run, reads: () => reads };
}
const options = github => ({ github, target, trigger: { kind: 'workflow_run', id: '7', generation: '1' },
  detailsUrl: 'https://github.com/example/repo/actions/runs/1', createReviewInput, engineVersion: reviewerVersion,
  engine: async input => ({ status: 'complete', head_sha: input.head_sha, base_sha: input.base_sha,
    input_digest: input.input_digest, reviewer_version: input.reviewer_version, findings: [], omitted_paths: [], reason: '' }) });
const safeFailure = error => {
  assert.ok(error instanceof PublishUnverified, error.stack);
  assert.equal(error.code, 'publish_unverified');
  assert.ok(!error.message.includes(output.summary));
  return true;
};

for (const conclusion of ['success', 'failure', 'neutral', 'cancelled']) {
  test(`stale first finish read converges to original ${conclusion} with exactly one PATCH`, async () => {
    const { github, writes, reads, waits } = boundary({ observe: (run, n) => n === 1 ? { ...run, status: 'in_progress', conclusion: null } : run });
    const result = await github.finish(initial(), conclusion, output);
    assert.equal(result.conclusion, conclusion); assert.deepEqual(result.output, output);
    assert.equal(writes.length, 1); assert.equal(writes[0].method, 'PATCH');
    assert.equal(reads(), 2); assert.deepEqual(waits, [1000]);
  });
}
for (const field of ['status', 'conclusion', 'output.title', 'output.summary', 'app.id', 'head_sha', 'name', 'completed_at']) {
  test(`persistent ${field} mismatch is typed and safely identifies expected/observed difference`, async () => {
    const { github, writes, reads } = boundary({ observe: run => {
      const parts = field.split('.'), key = parts.pop();
      const object = parts.reduce((value, part) => value[part], run);
      object[key] = field === 'app.id' ? 99 : field === 'head_sha' ? base : field === 'status' ? 'in_progress' :
        field === 'conclusion' ? 'failure' : field === 'completed_at' ? '2000-01-01T00:00:00Z' : `${output.summary}-different`;
      return run;
    } });
    await assert.rejects(() => github.finish(initial(), 'success', output), error => {
      safeFailure(error); assert.equal(error.details.failure, 'mismatch');
      assert.deepEqual(error.details.mismatched_fields, [field]);
      assert.notDeepEqual(error.details.expected[field], error.details.observed[field]); return true;
    });
    assert.equal(reads(), 3); assert.equal(writes.length, 1);
  });
}
for (const failure of [404, 500, 503, new Error('private-source-and-credential')]) {
  test(`finish readback ${String(failure)} stays typed, bounded and never replaces the original verdict`, async () => {
    const { github, writes, reads } = boundary({ observe: () => failure });
    await assert.rejects(() => produceReview(options(github)), safeFailure);
    assert.equal(reads(), 3); assert.equal(writes.length, 1);
    assert.equal(writes[0].body.conclusion, 'success');
  });
}
test('producer returns its original verdict after stale finish readback without a second PATCH', async () => {
  const { github, writes } = boundary({ observe: (run, n) => n === 1 ? { ...run, status: 'in_progress', conclusion: null } : run });
  const receipt = await produceReview(options(github));
  assert.equal(receipt.conclusion, 'success'); assert.equal(receipt.unavailable, undefined);
  assert.deepEqual(writes.map(write => write.body.conclusion), ['success']);
});
for (const failure of [503, new Error('private-source-and-credential')]) {
  test(`transient finish read failure ${String(failure)} converges with the original single PATCH`, async () => {
    const { github, writes, reads } = boundary({ observe: (run, n) => n === 1 ? failure : run });
    const result = await github.finish(initial(), 'success', output);
    assert.equal(result.conclusion, 'success'); assert.equal(reads(), 2); assert.equal(writes.length, 1);
  });
}
test('an ambiguous terminal PATCH failure is not followed by a neutral or replay PATCH', async () => {
  const { github, writes, reads } = boundary({ patchError: true });
  await assert.rejects(() => produceReview(options(github)), error => {
    safeFailure(error); assert.equal(error.details.failure, 'write_unverified'); return true;
  });
  assert.equal(writes.length, 1); assert.equal(reads(), 0);
});
test('a reconciliation deadline bounds slow reads and does not replay the write', async () => {
  let now = 0, calls = 0;
  const github = new GitHub({ repository: 'example/repo', checkToken: 'checks', now: () => now,
    wait: async ms => { now += ms; }, fetcher: async (_url, request) => {
      calls++; if (request.method === 'PATCH') return response(initial());
      assert.ok(request.signal instanceof AbortSignal);
      now += 10_001; throw new Error('private-source-and-credential');
    } });
  await assert.rejects(() => github.finish(initial(), 'success', output), error => {
    safeFailure(error); assert.equal(error.details.attempts, 1); return true;
  });
  assert.equal(calls, 2);
});
test('accepted POST identity is registered before 404 then 200 readback, with one creation', async () => {
  const { github, writes, reads, events } = boundary({ create: true, observe: (run, n) => n === 1 ? 404 : run });
  let saved;
  const receipt = await produceReview({ ...options(github), onBegin: run => { saved = structuredClone(run); events.push('persist'); } });
  assert.equal(receipt.conclusion, 'success'); assert.equal(saved.id, 41); assert.equal(saved.status, 'in_progress');
  assert.deepEqual(events.slice(0, 4), ['POST', 'persist', 'GET', 'GET']);
  assert.equal(reads(), 3); assert.deepEqual(writes.map(w => w.method), ['POST', 'PATCH']);
});

test('generated-only neutral persists terminal intent and finalizes read-only after a stale GET', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'review-generated-publication-'));
  try {
    const { github, writes, run } = boundary({ create: true });
    const reads = [];
    github.source = async path => {
      reads.push(path);
      assert.equal(path, '.github/review-policy.toml');
      return 'version=1\n[tier0]\npaths=[]\n[review]\npaths=[]\n[generated]\npaths=["generated/**"]\n';
    };
    let saved, publication;
    const receipt = await produceReview({ ...options(github), target: { ...target, review_requested: true,
      files: [{ filename: 'generated/capture.json', status: 'modified' }] },
      engine: async () => assert.fail('Excluded generated source must never enter inference'),
      onBegin: value => { saved = structuredClone(value); },
      onPublish: value => { publication = value; } });
    assert.equal(receipt.conclusion, 'neutral'); assert.equal(receipt.applicable, false);
    assert.deepEqual(reads, ['.github/review-policy.toml']);
    assert.match(run().output.summary, /excludes every changed source as generated/);
    assert.deepEqual(writes.map(write => write.method), ['POST', 'PATCH']);
    assert.equal(publication.expected.conclusion, 'neutral');
    assert.equal(publication.expected['output.title'].sha256, digest('Review outside path policy'));
    for (const readback of [null, 'stale']) {
      const { result, resource } = finalize(directory, run(), { ...saved, publication }, readback);
      assert.equal(result.status, readback === null ? 0 : 1, result.stderr);
      if (readback) assert.match(result.stderr, /publish_unverified/);
      assert.equal(resource.writes.length, 0);
      assert.equal(resource.run.output.title, 'Review outside path policy');
      assert.equal(resource.run.conclusion, 'neutral');
    }
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

// Run the actual always-run CLI against a durable resource/state, not an
// alternative finalizer implementation in the test controller.
function finalize(directory, run, state, readFailure = null, patchFailure = null) {
  writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'checks' }), { mode: 0o600 });
  writeFileSync(join(directory, 'pr-review-state.json'), JSON.stringify([state]), { mode: 0o600 });
  writeFileSync(join(directory, 'resource.json'), JSON.stringify({ run, writes: [], reads: 0 }));
  function preload(directory, readFailure, patchFailure) {
    // Keep reconciliation deterministic/instant while preserving the real CLI,
    // GitHub adapter, state persistence and wire request/response contracts.
    Date.now = () => 1;
    globalThis.fetch = async (_url, request) => {
      const path = `${directory}/resource.json`, data = JSON.parse(readFileSync(path, 'utf8'));
      if (request.method === 'PATCH') {
        const body = JSON.parse(request.body); data.writes.push(body);
        const failPatch = Array.isArray(patchFailure) ? patchFailure[data.writes.length - 1] : patchFailure;
        if (failPatch) { writeFileSync(path, JSON.stringify(data)); return { ok: false, status: failPatch }; }
        Object.assign(data.run, body);
        writeFileSync(path, JSON.stringify(data)); return { ok: true, status: 200, json: async () => data.run };
      }
      data.reads++; writeFileSync(path, JSON.stringify(data));
      const failure = Array.isArray(readFailure) ? readFailure[data.reads - 1] : readFailure;
      if (failure === 'stale') return { ok: true, status: 200, json: async () => ({ ...data.run, status: 'in_progress', conclusion: null }) };
      if (failure === 'network') throw new Error('private-source-and-credential');
      if (failure === 'conclusion') data.run = { ...data.run, conclusion: data.run.conclusion === 'success' ? 'failure' : 'success' };
      else if (failure === 'summary') data.run = { ...data.run, output: { ...data.run.output, summary: 'different-private-source' } };
      else if (failure === 'completed_at') data.run = { ...data.run, completed_at: '2000-01-01T00:00:00Z' };
      else if (failure) return { ok: false, status: failure };
      return { ok: true, status: 200, json: async () => data.run };
    };
  }
  writeFileSync(join(directory, 'preload.mjs'), `import { readFileSync, writeFileSync } from 'node:fs';\nimport { GitHub } from ${JSON.stringify(new URL('../.github/actions/pr-review/github.mjs', import.meta.url).href)};\nconst reconcile = GitHub.prototype.reconcile; GitHub.prototype.reconcile = function (...args) { this.wait = async () => {}; return reconcile.apply(this, args); };\n(${preload.toString()})(${JSON.stringify(directory)}, ${JSON.stringify(readFailure)}, ${JSON.stringify(patchFailure)});`);
  const result = spawnSync('node', ['--import', join(directory, 'preload.mjs'), '.github/actions/pr-review/cli.mjs', 'finalize'], {
    cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000,
    env: { ...process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_TOKEN: 'source',
      GITHUB_STEP_SUMMARY: join(directory, 'summary.md') },
  });
  assert.equal(existsSync(join(directory, 'pr-review-auth.json')), false);
  return { result, resource: JSON.parse(readFileSync(join(directory, 'resource.json'), 'utf8')) };
}
for (const failure of [404, 503, new Error('private-source-and-credential')]) {
  test(`persistent create readback ${String(failure)} preserves a finalizable created run, not an orphan`, async () => {
    const directory = mkdtempSync(join(tmpdir(), 'review-created-'));
    try {
      const { github, run, writes, reads } = boundary({ create: true, observe: () => failure });
      let state;
      await assert.rejects(() => produceReview({ ...options(github), onBegin: run => { state = structuredClone(run); } }), error => {
        safeFailure(error); assert.equal(error.details.phase, 'create'); return true;
      });
      assert.equal(state.id, 41); assert.equal(reads(), 3); assert.deepEqual(writes.map(w => w.method), ['POST']);
      const { result, resource } = finalize(directory, run(), state, typeof failure === 'number' ? failure : 503);
      assert.equal(result.status, 1, result.stderr); assert.match(result.stderr, /publish_unverified/);
      assert.equal(resource.run.status, 'completed'); assert.equal(resource.run.conclusion, 'neutral');
      assert.equal(resource.writes.length, 1); assert.equal(resource.writes[0].output.title, 'Review interrupted');
      const persisted = JSON.parse(readFileSync(join(directory, 'pr-review-state.json'), 'utf8'));
      assert.equal(persisted[0].publication.expected.conclusion, 'neutral');
      const again = finalize(directory, resource.run, persisted[0], 404);
      assert.equal(again.result.status, 1); assert.equal(again.resource.writes.length, 0, 'recovery must not resend terminal PATCH');
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}
for (const finalRead of [null, 'stale', 404, 503]) {
  test(`finalizer reconciles persisted terminal intent read-only after finish read failure (${finalRead})`, async () => {
    const directory = mkdtempSync(join(tmpdir(), 'review-published-'));
    try {
      const { github, run, writes } = boundary({ observe: () => 503 });
      let publication;
      await assert.rejects(() => produceReview({ ...options(github), onPublish: value => { publication = value; } }), safeFailure);
      assert.equal(writes.length, 1); assert.equal(run().conclusion, 'success');
      const state = { ...initial(), publication };
      assert.ok(!JSON.stringify(state).includes(output.summary));
      const { result, resource } = finalize(directory, run(), state, finalRead);
      assert.equal(result.status, finalRead === null ? 0 : 1, result.stderr);
      if (finalRead) assert.match(result.stderr, /publish_unverified/);
      assert.equal(resource.writes.length, 0); assert.equal(resource.run.conclusion, 'success');
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}

test('review CLI persists the accepted creation before a permanently 404 readback and exits typed failure', () => {
  const directory = mkdtempSync(join(tmpdir(), 'review-create-cli-'));
  try {
    writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'checks' }), { mode: 0o600 });
    writeFileSync(join(directory, 'event.json'), JSON.stringify({ repository: { full_name: 'example/repo', id: 1 }, inputs: { 'pull-request': '7' } }));
    function preload(directory, base, head) {
      const reconcile = GitHub.prototype.reconcile;
      GitHub.prototype.reconcile = function (...args) { this.wait = async () => {}; return reconcile.apply(this, args); };
      GitHub.prototype.current = async () => true;
      GitHub.prototype.source = async (path, sha) => path === 'src/change.js' ? (sha === base ? 'before\n' : 'after\n') : null;
      let created, reads = 0;
      globalThis.fetch = async (url, request) => {
        const path = new URL(url).pathname;
        const ok = value => ({ ok: true, status: 200, json: async () => value });
        if (path.endsWith('/pulls/7')) return ok({ state: 'open', changed_files: 1, title: '[review] Fixture',
          head: { sha: head }, base: { sha: base, ref: 'main', repo: { id: 1 } } });
        if (path.endsWith('/pulls/7/files')) return ok([{ filename: 'src/change.js', status: 'modified' }]);
        if (path.includes('/compare/')) return ok({ merge_base_commit: { sha: base } });
        if (path.includes('/commits/')) return ok({ check_runs: [] });
        if (request.method === 'POST') {
          created = { ...JSON.parse(request.body), id: 41, app: { id: 1299545 } }; return ok(created);
        }
        const saved = JSON.parse(readFileSync(`${directory}/pr-review-state.json`, 'utf8'));
        assert.equal(saved[0].id, created.id, 'CLI must save the accepted POST before any GET');
        assert.equal(saved[0].head_sha, created.head_sha); reads++;
        writeFileSync(`${directory}/created.json`, JSON.stringify({ created, reads }));
        return { ok: false, status: 404 };
      };
    }
    writeFileSync(join(directory, 'preload.mjs'), `import assert from 'node:assert/strict';\nimport { readFileSync, writeFileSync } from 'node:fs';\nimport { GitHub } from ${JSON.stringify(new URL('../.github/actions/pr-review/github.mjs', import.meta.url).href)};\n(${preload.toString()})(${JSON.stringify(directory)}, ${JSON.stringify(base)}, ${JSON.stringify(head)});`);
    const result = spawnSync('node', ['--import', join(directory, 'preload.mjs'), '.github/actions/pr-review/cli.mjs', 'review'], {
      cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000,
      env: { ...process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_TOKEN: 'source',
        GITHUB_EVENT_NAME: 'workflow_dispatch', GITHUB_EVENT_PATH: join(directory, 'event.json'), GITHUB_RUN_ID: '1', GITHUB_RUN_ATTEMPT: '1' },
    });
    assert.equal(result.status, 1); assert.match(result.stderr, /publish_unverified/);
    const { created, reads } = JSON.parse(readFileSync(join(directory, 'created.json'), 'utf8'));
    assert.equal(reads, 3);
    const [state] = JSON.parse(readFileSync(join(directory, 'pr-review-state.json'), 'utf8'));
    const settled = finalize(directory, created, state, 404);
    assert.equal(settled.result.status, 1); assert.equal(settled.resource.run.status, 'completed');
    assert.equal(settled.resource.writes.length, 1);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

// Reuse must cross both real CLI processes: the producer's completed run is
// persisted by review, then reloaded by finalize under a stale read projection.
async function reviewReused(directory, conclusion) {
  const { github, run } = boundary({ create: true });
  await produceReview({ ...options(github), trigger: { kind: 'workflow_dispatch', id: 'dispatch/1/1', generation: '1/1' } });
  const completed = structuredClone(run());
  completed.conclusion = conclusion;
  completed.output.title = conclusion === 'success' ? 'Review complete' : '1 defect requires a change';
  writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'checks' }), { mode: 0o600 });
  writeFileSync(join(directory, 'event.json'), JSON.stringify({ repository: { full_name: 'example/repo', id: 1 }, inputs: { 'pull-request': '7' } }));
  writeFileSync(join(directory, 'resource.json'), JSON.stringify({ run: completed, writes: [] }));
  function preload(directory, base, head) {
    GitHub.prototype.current = async () => true;
    GitHub.prototype.source = async (path, sha) => path === 'src/change.js' ? (sha === base ? 'before\n' : 'after\n') : null;
    globalThis.fetch = async (url, request) => {
      const path = new URL(url).pathname;
      const ok = value => ({ ok: true, status: 200, json: async () => value });
      if (path.endsWith('/pulls/7')) return ok({ state: 'open', changed_files: 1, title: '[review] Fixture',
        head: { sha: head }, base: { sha: base, ref: 'main', repo: { id: 1 } } });
      if (path.endsWith('/pulls/7/files')) return ok([{ filename: 'src/change.js', status: 'modified' }]);
      if (path.includes('/compare/')) return ok({ merge_base_commit: { sha: base } });
      if (path.includes('/commits/') && path.endsWith('/check-runs')) {
        return ok({ check_runs: [JSON.parse(readFileSync(`${directory}/resource.json`, 'utf8')).run] });
      }
      throw new Error(`Reuse must not infer, create, or update a check: ${request.method} ${path}`);
    };
  }
  writeFileSync(join(directory, 'review-preload.mjs'), `import { readFileSync } from 'node:fs';\nimport { GitHub } from ${JSON.stringify(new URL('../.github/actions/pr-review/github.mjs', import.meta.url).href)};\n(${preload.toString()})(${JSON.stringify(directory)}, ${JSON.stringify(base)}, ${JSON.stringify(head)});`);
  const result = spawnSync('node', ['--import', join(directory, 'review-preload.mjs'), '.github/actions/pr-review/cli.mjs', 'review'], {
    cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000,
    env: { ...process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_TOKEN: 'source',
      GITHUB_EVENT_NAME: 'workflow_dispatch', GITHUB_EVENT_PATH: join(directory, 'event.json'), GITHUB_RUN_ID: '1', GITHUB_RUN_ATTEMPT: '1' },
  });
  assert.equal(result.status, conclusion === 'success' ? 0 : 1, result.stderr);
  const receipt = JSON.parse(result.stdout).reviews[0];
  assert.equal(receipt.reused, true); assert.equal(receipt.conclusion, conclusion);
  assert.equal(receipt.review_passed, conclusion === 'success');
  const [state] = JSON.parse(readFileSync(join(directory, 'pr-review-state.json'), 'utf8'));
  return { completed, state };
}
for (const conclusion of ['success', 'failure']) {
  for (const readback of [null, ['stale', null], 'stale', 404, 503, 'network', 'conclusion', 'summary', 'completed_at']) {
    test(`actual CLI reused ${conclusion} preserves its terminal receipt with zero PATCH after ${JSON.stringify(readback)} readback`, async () => {
      const directory = mkdtempSync(join(tmpdir(), 'review-reused-cli-'));
      try {
        const { completed, state } = await reviewReused(directory, conclusion);
        const { result, resource } = finalize(directory, completed, state, readback);
        assert.equal(resource.writes.length, 0, `Terminal reuse must never publish neutral: ${JSON.stringify(resource.writes)}`);
        assert.deepEqual(resource.run, completed, 'the canonical original verdict and output must remain untouched');
        const converges = readback === null || Array.isArray(readback);
        assert.equal(result.status, converges && conclusion === 'success' ? 0 : 1, result.stderr);
        assert.equal(resource.reads, readback === null ? 1 : Array.isArray(readback) ? 2 : 3);
        if (!converges) assert.match(result.stderr, /publish_unverified/);
        else assert.ok(!result.stderr.includes('publish_unverified'), result.stderr);
        assert.equal(state.status, 'completed');
        assert.equal(state.publication.phase, 'reuse');
        assert.equal(state.publication.expected.conclusion, conclusion);
        assert.equal(state.publication.expected.status, 'completed');
        assert.equal(state.publication.expected.completed_at, Date.parse(completed.completed_at));
        assert.equal(state.publication.expected['output.title'].sha256, digest(completed.output.title));
        assert.equal(state.publication.expected['output.summary'].sha256, digest(completed.output.summary));
        assert.ok(!JSON.stringify(state).includes(completed.output.summary));
      } finally { rmSync(directory, { recursive: true, force: true }); }
    });
  }
}
test('a legacy persisted completed status without a full receipt never authorizes a neutral PATCH', () => {
  const directory = mkdtempSync(join(tmpdir(), 'review-legacy-completed-'));
  try {
    const completed = { ...initial(), status: 'completed', conclusion: 'success', output };
    const state = { id: completed.id, head_sha: head, app: { id: APP_ID }, name: CHECK_NAME, status: 'completed' };
    const { result, resource } = finalize(directory, completed, state, 'stale');
    assert.equal(resource.writes.length, 0); assert.equal(resource.run.conclusion, 'success');
    assert.equal(result.status, 1); assert.match(result.stderr, /publish_unverified/);
    assert.equal(resource.reads, 3);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

const blockingFindings = count => Array.from({ length: count }, (_, index) => ({
  path: 'src/change.js', side: 'head', start_line: 1, end_line: 1, severity: 'high',
  title: `Blocking defect ${index + 1}`, explanation: `Defect ${index + 1} breaks callers.`,
  trigger: 'Calling the changed function.', consequence: 'Runtime failure.',
  evidence: [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1, start_byte: null, end_byte: null, quote: 'after' }],
}));

test('inference stopping at inferenceDeadlineMs preserves PUBLISH_RESERVE_MS to publish failure and all annotation batches', async () => {
  const realNow = Date.now;
  let clock = 1_000_000;
  Date.now = () => clock;
  try {
    const outerDeadlineMs = clock + 35 * 60_000;
    const expectedInferenceDeadlineMs = outerDeadlineMs - PUBLISH_RESERVE_MS;
    const writes = [];
    let run = initial();
    const github = new GitHub({ repository: 'example/repo', readToken: 'source', checkToken: 'checks',
      now: () => clock, wait: async ms => { clock += ms; }, deadlineMs: outerDeadlineMs,
      fetcher: async (url, request) => {
        const path = new URL(url).pathname;
        if (path.endsWith('/actions/runs/1')) return response({ id: 1, status: 'in_progress' });
        if (path.includes('/commits/')) return response({ check_runs: [run] });
        if (path.endsWith('/pulls/7')) return response({ state: 'open', head: { sha: head }, base: { sha: base, ref: 'main' } });
        assert.equal(path, '/repos/example/repo/check-runs/41');
        if (request.method === 'PATCH') {
          const body = JSON.parse(request.body);
          writes.push(body);
          Object.assign(run, body);
          return response(run);
        }
        return response(run);
      } });
    github.source = async (path, sha, optional) => {
      if (path === 'src/change.js') return sha === base ? 'before\n' : 'after\n';
      assert.equal(optional, true); return null;
    };
    let seenEngineDeadlineMs, persistedPublication, persistedPending;
    const receipt = await produceReview({
      ...options(github),
      deadlineMs: outerDeadlineMs,
      onPublish: (publication, pending) => { persistedPublication = publication; persistedPending = pending; },
      engine: async (reviewInput, _externalId, { deadlineMs: engineDeadlineMs } = {}) => {
        seenEngineDeadlineMs = engineDeadlineMs;
        // Consume the entire inference budget so Date.now() reaches inferenceDeadlineMs,
        // leaving only PUBLISH_RESERVE_MS before outerDeadlineMs (reproducing HKMT#594).
        clock = engineDeadlineMs;
        return {
          status: 'incomplete', head_sha: reviewInput.head_sha, base_sha: reviewInput.base_sha,
          input_digest: reviewInput.input_digest, reviewer_version: reviewInput.reviewer_version,
          findings: blockingFindings(55), omitted_paths: ['src/change.js'],
          context_requests: [{ path: 'src/caller.js', side: 'head' }],
          reason: 'Review inference reached the inference deadline after earlier partitions found blocking defects.',
        };
      },
    });
    assert.equal(seenEngineDeadlineMs, expectedInferenceDeadlineMs);
    assert.equal(receipt.conclusion, 'failure');
    assert.equal(receipt.findings, 55);
    assert.equal(run.status, 'completed');
    assert.equal(run.conclusion, 'failure');
    assert.equal(run.output.title, '55 defects require a change; review incomplete');
    assert.equal(writes.length, 2, '55 annotations must upload in one 50-item batch plus the 5-item terminal PATCH');
    assert.equal(writes[0].status, undefined);
    assert.equal(writes[0].output.annotations.length, 50);
    assert.equal(writes[1].status, 'completed');
    assert.equal(writes[1].conclusion, 'failure');
    assert.equal(writes[1].output.annotations.length, 5);
    assert.equal(persistedPublication.expected.conclusion, 'failure');
    assert.equal(persistedPending, undefined, 'accepted terminal PATCH must clear pending_finish');
  } finally {
    Date.now = realNow;
  }
});

test('finalizer fails visibly with publish_unverified and settles an in_progress check from pending_finish including annotation batches', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'review-pending-finish-settle-'));
  const realNow = Date.now;
  let now = 1_000_000;
  Date.now = () => now;
  try {
    // Simulate a review step whose outer deadline expired right as finish() tried to PATCH,
    // leaving Check Run 41 in_progress on GitHub and publication + pending_finish in state.
    const { github, run, writes } = boundary();
    github.now = () => now;
    github.deadlineMs = now + 1000;
    const state = { ...initial() };
    const rememberState = (publication, pending_finish) => {
      state.publication = publication;
      if (pending_finish === undefined) delete state.pending_finish;
      else state.pending_finish = pending_finish;
    };
    await assert.rejects(() => produceReview({
      ...options(github),
      deadlineMs: now + 1000,
      onPublish: rememberState,
      engine: async reviewInput => {
        now += 1001; // Expire the outer deadline before post-review finish() can PATCH.
        return {
          status: 'incomplete', head_sha: reviewInput.head_sha, base_sha: reviewInput.base_sha,
          input_digest: reviewInput.input_digest, reviewer_version: reviewInput.reviewer_version,
          findings: blockingFindings(55), omitted_paths: ['src/change.js'],
          reason: 'Partition 2 exceeded deadline.',
        };
      },
    }), error => {
      safeFailure(error);
      assert.equal(error.details.failure, 'write_unverified');
      return true;
    });
    Date.now = realNow;
    assert.equal(writes.length, 0, 'expired outer deadline prevented any PATCH in the review step');
    assert.equal(run().status, 'in_progress');
    assert.equal(state.publication.expected.conclusion, 'failure');
    assert.equal(state.pending_finish.conclusion, 'failure');
    assert.equal(state.pending_finish.output.annotations.length, 55);

    const { result, resource } = finalize(directory, run(), state, null);
    assert.equal(result.status, 1, 'finalizer must still fail when primary publication was unverified');
    assert.match(result.stderr, /publish_unverified/);
    const summary = readFileSync(join(directory, 'summary.md'), 'utf8');
    assert.match(summary, /publish_unverified/);
    assert.equal(resource.run.status, 'completed', 'finalizer must never leave the Check Run in_progress');
    assert.equal(resource.run.conclusion, 'failure');
    assert.equal(resource.run.output.title, '55 defects require a change; review incomplete');
    assert.equal(resource.writes.length, 2, 'finalizer must upload the 50-item batch and the 5-item terminal PATCH');
    assert.equal(resource.writes[0].output.annotations.length, 50);
    assert.equal(resource.writes[1].status, 'completed');
    assert.equal(resource.writes[1].conclusion, 'failure');
    assert.equal(resource.writes[1].output.annotations.length, 5);
    const [persisted] = JSON.parse(readFileSync(join(directory, 'pr-review-state.json'), 'utf8'));
    assert.equal('pending_finish' in persisted, false, 'settled check must clear pending_finish');
  } finally {
    Date.now = realNow;
    rmSync(directory, { recursive: true, force: true });
  }
});

test('finalizer fails closed with conclusion=failure when pending_finish is unavailable or its PATCH fails, even for a success verdict', () => {
  for (const mode of ['missing-output', 'patch-rejected']) {
    const directory = mkdtempSync(join(tmpdir(), `review-pending-finish-fallback-${mode}-`));
    try {
      const active = initial();
      const state = {
        ...active,
        publication: {
          phase: 'finish',
          expected: {
            id: active.id, 'app.id': APP_ID, name: { bytes: CHECK_NAME.length, sha256: digest(CHECK_NAME) },
            head_sha: head, status: 'completed', conclusion: 'success', completed_at: 1_000,
            'output.title': { bytes: 1, sha256: digest('x') }, 'output.summary': { bytes: 1, sha256: digest('y') },
          },
        },
        pending_finish: mode === 'missing-output' ? null : {
          conclusion: 'success',
          output: { title: 'Review complete', summary: 'Clean review whose primary PATCH failed.', annotations: [] },
        },
      };
      const { result, resource } = finalize(directory, active, state, null, mode === 'patch-rejected' ? [422, null] : null);
      assert.equal(result.status, 1, `${mode}: finalizer must exit 1`);
      assert.match(result.stderr, /publish_unverified/);
      assert.equal(resource.run.status, 'completed');
      assert.equal(resource.run.conclusion, 'failure', `${mode}: fallback Review interrupted must always fail closed as failure`);
      assert.equal(resource.run.output.title, 'Review interrupted');
      assert.equal(resource.writes.length, mode === 'patch-rejected' ? 2 : 1);
      assert.equal(resource.writes.at(-1).conclusion, 'failure');
    } finally { rmSync(directory, { recursive: true, force: true }); }
  }
});

for (const mismatch of ['completed_at', 'summary', 'conclusion']) {
  test(`finalizer never overwrites an already-completed Check Run when reconcile mismatches on ${mismatch} even if pending_finish is present`, () => {
    const directory = mkdtempSync(join(tmpdir(), 'review-completed-no-overwrite-'));
    try {
      const completed = {
        ...initial(), status: 'completed', conclusion: 'failure', completed_at: '2026-11-08T21:00:00Z',
        output: { title: '2 defects require a change', summary: 'Canonical completed verdict on GitHub.', annotations: [] },
      };
      const state = {
        ...initial(),
        publication: {
          phase: 'finish',
          expected: {
            id: completed.id, 'app.id': APP_ID, name: { bytes: CHECK_NAME.length, sha256: digest(CHECK_NAME) },
            head_sha: head, status: 'completed', conclusion: 'failure', completed_at: Date.parse(completed.completed_at),
            'output.title': { bytes: Buffer.byteLength(completed.output.title), sha256: digest(completed.output.title) },
            'output.summary': { bytes: Buffer.byteLength(completed.output.summary), sha256: digest(completed.output.summary) },
          },
        },
        pending_finish: {
          conclusion: 'neutral',
          output: { title: 'Stale pending output', summary: 'Must never overwrite a completed check.', annotations: [] },
        },
      };
      const { result, resource } = finalize(directory, completed, state, mismatch);
      assert.equal(result.status, 1);
      assert.match(result.stderr, /publish_unverified/);
      assert.equal(resource.writes.length, 0, 'completed Check Run on GitHub must never be overwritten');
      assert.equal(resource.reads, 3);
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}
