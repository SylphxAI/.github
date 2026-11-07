import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { APP_ID, CHECK_NAME, GitHub, PublishUnverified, digest } from '../.github/actions/pr-review/github.mjs';
import { produceReview } from '../.github/actions/pr-review/producer.mjs';
import { createReviewInput, reviewerVersion } from '../.github/actions/pr-review/contract.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40);
const target = { kind: 'pull_request', number: 7, base_sha: base, head_sha: head, repository_id: '1',
  files: [{ filename: 'src/change.js', status: 'modified' }] };
const externalId = `pr-review/v1/${digest(JSON.stringify(['example/repo', 'pull_request', 7, base, undefined, head, undefined,
  { kind: 'workflow_run', id: '7', generation: '1' }, reviewerVersion]))}`;
const initial = () => ({ external_id: externalId, id: 41, app: { id: APP_ID }, name: CHECK_NAME, head_sha: head, status: 'in_progress' });
const output = { title: 'Review complete', summary: 'private-source-and-credential', annotations: [] };
const response = data => ({ ok: true, status: 200, json: async () => structuredClone(data) });
function boundary({ observe = run => run, patchError, create = false } = {}) {
  const writes = [], waits = [], events = [];
  let run = create ? null : initial(), reads = 0, now = 0;
  const github = new GitHub({ repository: 'example/repo', readToken: 'source', checkToken: 'checks',
    now: () => now, wait: async ms => { waits.push(ms); now += ms; },
    fetcher: async (url, request) => {
      assert.equal(request.headers.authorization, 'Bearer checks');
      const path = new URL(url).pathname;
      if (path.includes('/commits/')) return response({ check_runs: run ? [run] : [] });
      if (request.method === 'POST') {
        assert.equal(path, '/repos/example/repo/check-runs'); events.push('POST');
        run = { ...JSON.parse(request.body), ...initial() };
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
  detailsUrl: 'https://example.com/run/1', createReviewInput, engineVersion: reviewerVersion,
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

// Run the actual always-run CLI against a durable resource/state, not an
// alternative finalizer implementation in the test controller.
function finalize(directory, run, state, readFailure = null) {
  writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'checks' }), { mode: 0o600 });
  writeFileSync(join(directory, 'pr-review-state.json'), JSON.stringify([state]), { mode: 0o600 });
  writeFileSync(join(directory, 'resource.json'), JSON.stringify({ run, writes: [], reads: 0 }));
  function preload(directory, readFailure) {
    // Keep reconciliation deterministic/instant while preserving the real CLI,
    // GitHub adapter, state persistence and wire request/response contracts.
    Date.now = () => 1;
    globalThis.fetch = async (_url, request) => {
      const path = `${directory}/resource.json`, data = JSON.parse(readFileSync(path, 'utf8'));
      if (request.method === 'PATCH') {
        const body = JSON.parse(request.body); Object.assign(data.run, body); data.writes.push(body);
        writeFileSync(path, JSON.stringify(data)); return { ok: true, status: 200, json: async () => data.run };
      }
      data.reads++; writeFileSync(path, JSON.stringify(data));
      if (readFailure === 'stale') return { ok: true, status: 200, json: async () => ({ ...data.run, status: 'in_progress', conclusion: null }) };
      if (readFailure) return { ok: false, status: readFailure };
      return { ok: true, status: 200, json: async () => data.run };
    };
  }
  writeFileSync(join(directory, 'preload.mjs'), `import { readFileSync, writeFileSync } from 'node:fs';\nimport { GitHub } from ${JSON.stringify(new URL('../.github/actions/pr-review/github.mjs', import.meta.url).href)};\nconst reconcile = GitHub.prototype.reconcile; GitHub.prototype.reconcile = function (...args) { this.wait = async () => {}; return reconcile.apply(this, args); };\n(${preload.toString()})(${JSON.stringify(directory)}, ${JSON.stringify(readFailure)});`);
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
