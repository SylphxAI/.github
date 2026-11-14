import test from 'node:test';
import assert from 'node:assert/strict';
import { APP_ID, CHECK_NAME, GitHub } from '../.github/actions/pr-review/github.mjs';
import { createReviewInput, parseReviewResult, reviewerVersion, unavailable } from '../.github/actions/pr-review/contract.mjs';
import { produceReview } from '../.github/actions/pr-review/producer.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40);
const target = { kind: 'pull_request', number: 7, base_sha: base, head_sha: head, repository_id: '1',
  files: [{ filename: 'src/change.js', status: 'modified' }] };
const trigger = { kind: 'workflow_run', id: `pull-request/${head}`, generation: '1' };
const findings = [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1, severity: 'medium',
  title: 'Return value changed', explanation: 'The caller requires the earlier value.', trigger: 'The caller reads the value.',
  consequence: 'The request fails.', evidence: [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1,
    start_byte: null, end_byte: null, quote: 'after' }] }];
const complete = (input, bad = false) => parseReviewResult(JSON.stringify({ schema_version: 1,
  repository_id: input.repository_id, base_sha: input.base_sha, head_sha: input.head_sha,
  input_digest: input.input_digest, reviewer_version: input.reviewer_version, policy_version: input.policy_version,
  completion: 'complete', findings: bad ? findings : [], related_evidence: [], binary_reviews: [],
  review_summary: 'Reviewed the complete changed behavior.', omitted_paths: [], context_notes: [], context_requests: [], reason: '' }), input);

// Intentional complete finding fixtures must opt into the producer's typed
// final adjudication gate; production has no compatibility fallback.
const withAdjudication = engine => {
  const review = async input => engine(input);
  review.adjudicate = async request => { const input = request.input ?? request; return ({ schema_version: 1, repository_id: input.repository_id,
    base_sha: input.base_sha, head_sha: input.head_sha, input_digest: input.input_digest,
    reviewer_version: input.reviewer_version, policy_version: input.policy_version,
    completion: 'complete', reason: 'Fixture explicitly adjudicated supplied candidates.',
    decisions: (input.candidates ?? []).map(candidate => ({ candidate_id: candidate.candidate_id,
      verdict: 'retain', contradiction: '', proof_evidence: candidate.finding?.evidence ?? [] })) }); };
  return review;
};

// Exercise the shipped check discovery/create/readback adapter, not a fake
// begin() that decides independently which completed results can be reused.
function boundary() {
  const runs = [], writes = [], owners = new Map(), ownerReads = [];
  const github = new GitHub({ repository: 'example/repo', readToken: 'source', checkToken: 'checks',
    fetcher: async (url, request = {}) => {
      const path = new URL(url).pathname, body = request.body && JSON.parse(request.body);
      let data;
      if (path.includes('/actions/runs/')) {
        assert.equal(request.headers.authorization, 'Bearer source');
        const id = path.split('/').at(-1); ownerReads.push(id);
        const value = owners.get(id);
        if (value instanceof Error) throw value;
        return { ok: true, status: 200, json: async () => ({ id: Number(id), ...(value ?? { status: 'in_progress' }) }) };
      }
      assert.equal(request.headers.authorization, 'Bearer checks');
      if (path.includes('/commits/') && path.endsWith('/check-runs')) data = { check_runs: runs };
      else if (path.endsWith('/check-runs') && request.method === 'POST') {
        data = { ...body, id: runs.length + 1, app: { id: APP_ID } }; runs.push(data); writes.push({ method: 'POST', id: data.id });
      } else if (/\/check-runs\/\d+$/.test(path)) {
        data = runs.find(r => r.id === Number(path.split('/').at(-1))); assert.ok(data);
        if (request.method === 'PATCH') { Object.assign(data, body); writes.push({ method: 'PATCH', id: data.id }); }
      } else throw new Error(`Unexpected check request: ${path}`);
      return { ok: true, status: 200, json: async () => structuredClone(data) };
    } });
  github.source = async (path, sha, optional) => {
    if (path === 'src/change.js') return sha === base ? 'before\n' : 'after\n';
    assert.equal(optional, true); return null;
  };
  github.current = async () => true;
  return { github, runs, writes, owners, ownerReads, options: { github, target, trigger, createReviewInput, engineVersion: reviewerVersion,
    detailsUrl: 'https://github.com/example/repo/actions/runs/1' } };
}

for (const conclusion of ['success', 'failure']) test(`same-head same-input completed ${conclusion} reuses the receipt without inference`, async () => {
  const { runs, options } = boundary(); let calls = 0;
  const opts = { ...options, engine: withAdjudication(async input => { calls++; return complete(input, conclusion === 'failure'); }) };
  const first = await produceReview(opts), observed = [];
  const next = await produceReview({ ...opts, onBegin: run => observed.push(run.id) });
  assert.equal(first.conclusion, conclusion); assert.equal(next.conclusion, conclusion);
  assert.equal(next.reused, true); assert.equal(calls, 1); assert.equal(runs.length, 1);
  assert.equal(next.input_digest, first.input_digest); assert.deepEqual(observed, [first.check_run_id]);
});

for (const conclusion of ['neutral', 'cancelled', 'timed_out', 'action_required', 'stale', 'skipped']) {
  test(`completed ${conclusion} starts a fresh check and real review, preserving the old receipt`, async () => {
    const { runs, writes, options } = boundary(); let calls = 0;
    const opts = { ...options, engine: async input => { calls++; return complete(input); } };
    await produceReview(opts);
    Object.assign(runs[0], { conclusion, output: { ...runs[0].output, title: conclusion === 'neutral' ? 'Review outside path policy' : 'Previous result' } });
    const previous = structuredClone(runs[0]), observed = [], before = writes.length;
    const next = await produceReview({ ...opts, onBegin: run => observed.push(run.id) });
    assert.equal(next.reused, undefined); assert.equal(next.conclusion, 'success'); assert.equal(calls, 2);
    assert.equal(runs.length, 2); assert.notEqual(next.check_run_id, previous.id);
    assert.deepEqual(runs[0], previous); assert.ok(writes.slice(before).every(w => w.id === next.check_run_id));
    assert.deepEqual(observed, [next.check_run_id], 'finalization must not include the rejected terminal check');
  });
}

for (const mismatch of ['head', 'digest', 'missing-digest', 'reviewer', 'foreign-app']) {
  test(`completed success with ${mismatch} mismatch cannot bypass a fresh review`, async () => {
    const { runs, options } = boundary(); let calls = 0;
    const opts = { ...options, engine: async input => { calls++; return complete(input); } };
    await produceReview(opts);
    if (mismatch === 'head') runs[0].head_sha = 'c'.repeat(40);
    else if (mismatch === 'foreign-app') runs[0].app.id = 99;
    else runs[0].output.summary = runs[0].output.summary.replace(
      mismatch === 'reviewer' ? /Reviewer: `[^`]+`/ : /Source digest: `[^`]+`/,
      mismatch === 'reviewer' ? 'Reviewer: `old-reviewer`' : mismatch === 'digest' ? `Source digest: \`${'0'.repeat(64)}\`` : 'Legacy result has no digest');
    const previous = structuredClone(runs[0]);
    const next = await produceReview(opts);
    assert.equal(next.reused, undefined); assert.equal(next.conclusion, 'success'); assert.equal(calls, 2);
    assert.equal(runs.length, 2); assert.notEqual(next.check_run_id, previous.id); assert.deepEqual(runs[0], previous);
  });
}

test('recovery after a capacity neutral invokes inference and creates a new canonical result', async () => {
  const { runs, options } = boundary(); let calls = 0;
  const opts = { ...options, engine: async input => ++calls === 1 ? unavailable(input, 'Review gateway refused HTTP 429.') : complete(input) };
  const prior = await produceReview(opts); assert.equal(prior.conclusion, 'neutral');
  const next = await produceReview(opts);
  assert.equal(calls, 2); assert.equal(next.reused, undefined); assert.equal(next.conclusion, 'success');
  assert.notEqual(next.check_run_id, prior.check_run_id); assert.equal(runs[0].conclusion, 'neutral');
  assert.equal(runs[1].name, CHECK_NAME); assert.equal(runs[1].app.id, APP_ID); assert.equal(runs[1].head_sha, head);
});

test('a completed candidate never hides source unavailability or overwrites its prior result', async () => {
  const { github, runs, options } = boundary(); let calls = 0;
  const opts = { ...options, engine: async input => { calls++; return complete(input); } };
  await produceReview(opts); const previous = structuredClone(runs[0]), observed = [];
  github.source = async () => { throw new Error('source outage'); };
  const next = await produceReview({ ...opts, onBegin: run => observed.push(run.id) });
  assert.equal(next.reused, undefined); assert.equal(next.conclusion, 'neutral'); assert.equal(next.unavailable, true);
  assert.equal(calls, 1); assert.equal(runs.length, 2); assert.deepEqual(runs[0], previous);
  assert.deepEqual(observed, [next.check_run_id]);
});

test('an in-progress check is reconciled without a second create when creation was already accepted', async () => {
  const { github, runs, options } = boundary();
  await produceReview({ ...options, engine: async input => complete(input) });
  Object.assign(runs[0], { status: 'in_progress', conclusion: null });
  const inputIdentity = runs[0].output.summary.split('\n\n').slice(0, 3).join('\n\n') + '\n\n';
  const next = await github.begin(target, runs[0].external_id, options.detailsUrl, { restartCompleted: true, inputIdentity });
  assert.equal(next.id, runs[0].id); assert.equal(runs.length, 1);
});

for (const state of ['active', 'neutral']) test(`newer ${state} delivery shadows an older matching success and forces a fresh review`, async () => {
  const { runs, options } = boundary(); let calls = 0;
  const opts = { ...options, engine: async input => { calls++; return complete(input); } };
  await produceReview(opts);
  const later = { ...structuredClone(runs[0]), id: 2, external_id: 'pr-review/v1/different-delivery',
    status: state === 'active' ? 'in_progress' : 'completed', conclusion: state === 'active' ? null : 'neutral' };
  runs.push(later); const previous = structuredClone(runs), observed = [];
  const next = await produceReview({ ...opts, onBegin: run => observed.push(run.id) });
  assert.equal(next.reused, undefined); assert.equal(next.check_run_id, 3); assert.equal(calls, 2);
  assert.equal(next.conclusion, 'success'); assert.deepEqual(runs.slice(0, 2), previous);
  assert.deepEqual(observed, [3]);
});

test('newest valid completed result may reuse across delivery IDs only with matching input identity', async () => {
  const { runs, options } = boundary(); let calls = 0;
  const opts = { ...options, engine: async input => { calls++; return complete(input); } };
  await produceReview(opts);
  runs.push({ ...structuredClone(runs[0]), id: 2, external_id: 'pr-review/v1/different-delivery' });
  const next = await produceReview(opts);
  assert.equal(next.reused, true); assert.equal(next.check_run_id, 2); assert.equal(calls, 1); assert.equal(runs.length, 2);
});

test('check-discovery errors fail without creating a check, inference, or an older fallback', async () => {
  let calls = 0, writes = 0;
  const github = new GitHub({ repository: 'example/repo', readToken: 'source', checkToken: 'checks',
    fetcher: async (_url, request) => { if (request.method !== 'GET') writes++; return { ok: false, status: 403 }; } });
  await assert.rejects(() => produceReview({ github, target, trigger, createReviewInput, engineVersion: reviewerVersion,
    engine: async input => { calls++; return complete(input); } }), /HTTP 403/);
  assert.equal(calls, 0); assert.equal(writes, 0);
});


const ownerUrl = id => `https://github.com/example/repo/actions/runs/${id}`;
test('overlapping deliveries defer to one live owner and execute inference exactly once', async () => {
  const { runs, options, ownerReads } = boundary(); let release, started, calls = 0;
  const entered = new Promise(resolve => { started = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const first = produceReview({ ...options, engine: async input => { calls++; started(); await held; return complete(input); } });
  await entered;
  const observed = [];
  const second = await produceReview({ ...options, detailsUrl: ownerUrl(2),
    onBegin: run => observed.push(run.id), engine: async input => { calls++; return complete(input); } });
  assert.equal(second.deferred, true); assert.equal(second.owner_run_id, '1');
  assert.equal(second.conclusion, null); assert.equal(runs.length, 1); assert.equal(calls, 1); assert.deepEqual(observed, []);
  assert.deepEqual(ownerReads, ['1']); release(); assert.equal((await first).conclusion, 'success');
});

for (const ownerState of ['completed', 'read-error', 'unknown', 'malformed', 'different-run']) test(`active check with ${ownerState} owner starts a fresh review`, async () => {
  const { runs, options, owners } = boundary(); let calls = 0;
  const opts = { ...options, engine: async input => { calls++; return complete(input); } };
  await produceReview(opts);
  Object.assign(runs[0], { status: 'in_progress', conclusion: null,
    details_url: ownerState === 'unknown' ? 'https://example.com/unknown' : ownerUrl(99) });
  owners.set('99', ownerState === 'read-error' ? new Error('owner read unavailable') :
    ownerState === 'malformed' ? {} : ownerState === 'different-run' ? { id: 98, status: 'in_progress' } : { status: 'completed', conclusion: 'failure' });
  const previous = structuredClone(runs[0]), next = await produceReview(opts);
  assert.equal(next.deferred, undefined); assert.equal(next.reused, undefined); assert.equal(next.check_run_id, 2);
  assert.equal(next.conclusion, 'success'); assert.equal(calls, 2); assert.deepEqual(runs[0], previous);
});

test('newest same-input live check defers after collection without creating or invoking inference', async () => {
  const { runs, options, github, writes } = boundary();
  await produceReview({ ...options, engine: async input => complete(input) });
  runs.push({ ...structuredClone(runs[0]), id: 2, external_id: 'pr-review/v1/another-delivery',
    status: 'in_progress', conclusion: null, details_url: ownerUrl(99) });
  const previous = structuredClone(runs), count = writes.length;
  const source = github.source.bind(github); let reads = 0;
  github.source = async (...args) => { reads++; return source(...args); };
  const next = await produceReview({ ...options, engine: async () => { throw new Error('Deferral must not infer'); } });
  assert.equal(next.deferred, true); assert.equal(next.check_run_id, 2); assert.equal(next.owner_run_id, '99');
  assert.equal(writes.length, count); assert.deepEqual(runs, previous); assert.ok(reads > 0);
});


for (const change of ['base', 'policy', 'unreadable-identity']) test(`same-head active owner cannot defer a ${change} input`, async () => {
  const { runs, options, github, ownerReads } = boundary(); let calls = 0;
  const opts = { ...options, engine: async input => { calls++; return complete(input); } };
  await produceReview(opts);
  Object.assign(runs[0], { status: 'in_progress', conclusion: null, details_url: ownerUrl(99) });
  if (change === 'unreadable-identity') runs[0].output.summary = 'Legacy check without input identity';
  const original = github.source.bind(github);
  if (change === 'policy') github.source = async (path, ...args) => path === '.github/review-policy.toml'
    ? 'version=2\n[tier0]\npaths=[]\n[review]\npaths=["src/**"]\n[generated]\npaths=[]\n' : original(path, ...args);
  const previous = structuredClone(runs[0]);
  const next = await produceReview({ ...opts, target: change === 'base' ? { ...target, base_sha: 'c'.repeat(40) } : target });
  assert.equal(next.deferred, undefined); assert.equal(next.reused, undefined); assert.equal(next.check_run_id, 2);
  assert.equal(next.conclusion, 'success'); assert.equal(calls, 2); assert.deepEqual(runs[0], previous);
  assert.deepEqual(ownerReads, [], 'different or unreadable input cannot delegate before ownership lookup');
});

test('initial created check carries the authoritative input before inference begins', async () => {
  const { runs, options } = boundary();
  await produceReview({ ...options, engine: async input => {
    assert.equal(runs[0].status, 'in_progress');
    assert.ok(runs[0].output.summary.startsWith(`Head: \`${input.head_sha}\`\n\nReviewer: \`${input.reviewer_version}\`\n\nSource digest: \`${input.input_digest}\`\n\n`));
    return complete(input);
  } });
});


test('simultaneous same-input reviews select the newest verdict despite late older/stale completions', async () => {
  const { github, runs, options } = boundary();
  const hold = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
  const staleEntered = hold(), staleRelease = hold();
  let calls = 0;
  const stale = produceReview({ ...options, target: { ...target, base_sha: 'c'.repeat(40) }, detailsUrl: ownerUrl(99),
    engine: async input => { calls++; staleEntered.resolve(); await staleRelease.promise; return complete(input); } });
  await staleEntered.promise;
  // Both deliveries observe the same population before either creates a check.
  const checkRuns = github.checkRuns.bind(github), discovered = hold(); let discoveries = 0;
  github.checkRuns = async sha => {
    const snapshot = structuredClone(await checkRuns(sha));
    if (++discoveries === 2) discovered.resolve();
    await discovered.promise;
    return snapshot;
  };
  const entered = [hold(), hold()], release = [hold(), hold()], inputs = [];
  const work = [0, 1].map(i => produceReview({ ...options, detailsUrl: ownerUrl(i + 1),
    engine: withAdjudication(async input => {
      calls++; inputs[i] = input; entered[i].resolve(); await release[i].promise; return complete(input, i === 1);
    }) }));
  await Promise.all(entered.map(x => x.promise));
  assert.equal(runs.length, 3); assert.equal(calls, 3);
  assert.equal(inputs[0].input_digest, inputs[1].input_digest);
  assert.notEqual(inputs[0].input_digest, runs[0].output.summary.match(/Source digest: `([^`]+)`/)[1]);
  release[1].resolve(); const newest = await work[1];
  release[0].resolve(); const older = await work[0];
  staleRelease.resolve(); const oldInput = await stale;
  assert.equal(newest.check_run_id, 3); assert.equal(newest.conclusion, 'failure');
  assert.equal(older.check_run_id, 2); assert.equal(older.conclusion, 'success');
  assert.equal(oldInput.check_run_id, 1); assert.equal(oldInput.conclusion, 'success');
  assert.ok(runs.every(run => run.status === 'completed'));
  const selected = await produceReview({ ...options, engine: async () => { throw new Error('Must reuse newest canonical same-input verdict'); } });
  assert.equal(selected.reused, true); assert.equal(selected.check_run_id, newest.check_run_id);
  assert.equal(selected.conclusion, 'failure'); assert.equal(selected.input_digest, inputs[1].input_digest);
  assert.equal(runs.length, 3); assert.equal(calls, 3);
});
