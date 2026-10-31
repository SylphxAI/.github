import test from 'node:test';
import assert from 'node:assert/strict';
import { APP_ID, CHECK_NAME, GitHub, bundleFile, digest } from '../.github/actions/pr-review/github.mjs';
import { fileSegments } from '../.github/actions/pr-review/source.mjs';
import { collectPullRequest, needsReview, parsePolicy, produceReview, validateResult } from '../.github/actions/pr-review/producer.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40);
const policy = 'version = 1\n[tier0]\npaths = ["**/*.md", "tests/**"]\n[review]\npaths = [".github/**", "**/fixtures/**"]\n[generated]\npaths = ["generated/**"]\n';
const changed = { filename: 'src/total.js', status: 'modified' };
const before = 'export const total = (n) => n * 2;\n';
const after = 'export const total = (n) => n / 2;\n';
const target = { kind: 'pull_request', number: 7, base_sha: base, head_sha: head,
  repository_id: '1', files: [changed] };
const trigger = { kind: 'pull_request', id: '7', generation: '1' };
const inputOf = bundle => ({ ...bundle, input_digest: digest(JSON.stringify(bundle)), reviewer_version: 'v1' });
const complete = (input, findings = []) => ({ status: 'complete', base_sha: input.base_sha,
  head_sha: input.head_sha, input_digest: input.input_digest, reviewer_version: input.reviewer_version,
  findings, omitted_paths: [], context_notes: [], reason: '' });
const finding = { path: changed.filename, side: 'head', start_line: 1, end_line: 1, severity: 'medium',
  title: 'Total is halved', explanation: 'The result is too small.', trigger: 'Calling total(2)',
  consequence: 'Returns 1 instead of 4.', evidence: [{ path: changed.filename, side: 'head',
    start_line: 1, end_line: 1, quote: after.trimEnd() }] };

class Provider {
  repository = 'example/repository';
  runs = [];
  currentHead = head;
  async current(t) { return t.head_sha === this.currentHead; }
  async source(path, sha, optional) {
    if (path === '.github/review-policy.toml') return policy;
    if (path === changed.filename) return sha === base ? before : after;
    if (optional) return null;
    throw new Error('unexpected source');
  }
  async begin(t, externalId) {
    let run = this.runs.find(r => r.head_sha === t.head_sha && r.external_id === externalId);
    if (!run) { run = { id: this.runs.length + 1, head_sha: t.head_sha, name: CHECK_NAME,
      app: { id: APP_ID }, external_id: externalId, status: 'in_progress' }; this.runs.push(run); }
    return run;
  }
  async finish(run, conclusion, output) { Object.assign(run, { conclusion, output, status: 'completed' }); return run; }
}
const options = github => ({ github, target, trigger, detailsUrl: 'https://example.com/run/1',
  createReviewInput: inputOf, engineVersion: 'v1' });

test('base policy precedence keeps instruction/fixture changes reviewed and defaults unknown paths to review', () => {
  const parsed = parsePolicy(policy);
  assert.equal(needsReview(parsed, ['notes.md']), false);
  assert.equal(needsReview(parsed, ['src/a.js']), true);
  assert.equal(needsReview(parsed, ['tests/fixtures/a.md']), true);
  assert.equal(needsReview(parsed, ['.github/review-policy.toml']), true);
  assert.equal(needsReview(parsePolicy(null), ['notes.md']), true);
  assert.throws(() => parsePolicy('version = "wrong"'));
});

test('large sources preserve changed physical lines with literal fragments and full source digests', () => {
  const original = Array.from({ length: 10_000 }, (_, n) => `const row${n} = ${n};`).join('\n') + '\n';
  const updated = original.replace('const row8000 = 8000;', 'const row8000 = 0;');
  const file = bundleFile(changed, original, updated);
  assert.equal(file.source_mode, 'segments');
  assert.equal(file.after.sha256, digest(updated));
  assert.equal(file.after.line_count, 10_000);
  assert.ok(fileSegments(file, 'after').some(s => s.start_line === 8001 && s.content === 'const row8000 = 0;\n'));
  for (const [side, source] of [['before', original], ['after', updated]]) {
    for (const s of fileSegments(file, side)) assert.equal(Buffer.from(source).subarray(s.start_byte, s.end_byte).toString(), s.content);
  }
  assert.ok(JSON.stringify(file).length < 40_000);
});

test('empty created files and pure renames have honest empty content diffs', () => {
  assert.deepEqual(bundleFile({ ...changed, status: 'added' }, null, '').hunks[0].rows, []);
  assert.ok(bundleFile({ ...changed, status: 'renamed', previous_filename: 'old.js' }, before, before).hunks[0].rows.every(r => r.kind === 'context'));
});

test('real-defect result fails while clean result passes', async () => {
  const bad = new Provider();
  assert.equal((await produceReview({ ...options(bad), engine: async input => complete(input, [finding]) })).conclusion, 'failure');
  assert.equal(bad.runs[0].output.annotations[0].path, changed.filename);
  const clean = new Provider();
  assert.equal((await produceReview({ ...options(clean), engine: async input => complete(input) })).conclusion, 'success');
});

test('incomplete aggregate preserves findings and publishes their real annotation coordinates', async () => {
  const github = new Provider(); let saved;
  const receipt = await produceReview({ ...options(github), onResult: result => { saved = result; },
    engine: async input => ({ ...complete(input, [finding]), status: 'incomplete', omitted_paths: [changed.filename],
      reason: 'A later source part was unavailable.', parts: { planned: 3, completed: 1 } }) });
  assert.equal(receipt.conclusion, 'failure'); assert.equal(receipt.unavailable, true);
  assert.equal(saved.findings.length, 1); assert.equal(receipt.findings, 1);
  assert.match(github.runs[0].output.summary, /1\/3 complete/);
  assert.match(github.runs[0].output.title, /incomplete/);
  assert.equal(github.runs[0].output.annotations[0].start_line, 1);
  const low = new Provider();
  assert.equal((await produceReview({ ...options(low), engine: async input => ({ ...complete(input, [{ ...finding, severity: 'low' }]),
    status: 'incomplete', omitted_paths: [changed.filename], reason: 'Unreviewed source remains.' }) })).conclusion, 'neutral');
});

test('new head creates a new check and a late old-head result is cancelled', async () => {
  const github = new Provider();
  await produceReview({ ...options(github), engine: async input => { github.currentHead = 'c'.repeat(40); return complete(input); } });
  assert.equal(github.runs[0].conclusion, 'cancelled');
  const next = { ...target, head_sha: github.currentHead };
  const receipt = await produceReview({ ...options(github), target: next, engine: async input => complete(input) });
  assert.equal(receipt.conclusion, 'success');
  assert.equal(github.runs.length, 2);
  assert.notEqual(github.runs[0].external_id, github.runs[1].external_id);
  assert.equal(github.runs[1].head_sha, next.head_sha);
});

test('redelivery reuses the terminal receipt and never submits another model execution', async () => {
  const github = new Provider(); let executions = 0;
  const opts = { ...options(github), engine: async input => { executions++; return complete(input); } };
  await produceReview(opts); const second = await produceReview(opts);
  assert.equal(second.reused, true); assert.equal(executions, 1); assert.equal(github.runs.length, 1);
});

test('incomplete, stale, malformed and operational failures cannot produce success', async () => {
  for (const execute of [async () => ({}), async input => ({ ...complete(input), head_sha: base }),
    async input => ({ ...complete(input), omitted_paths: ['src/total.js'] }), async () => { throw new Error('provider'); }]) {
    const github = new Provider();
    const receipt = await produceReview({ ...options(github), engine: execute });
    assert.equal(receipt.conclusion, 'neutral'); assert.equal(receipt.unavailable, true);
    assert.equal(github.runs[0].output.title, 'Review unavailable');
  }
});

test('finding outside the actual diff is unavailable', () => {
  const input = inputOf({ ...target, files: [bundleFile(changed, before, after)] });
  assert.throws(() => validateResult(complete(input, [{ ...finding, start_line: 30, end_line: 30 }]), input));
});

test('PR files must be complete and source base uses the merge base rather than moving main', async () => {
  const github = new Provider();
  github.request = async path => path.includes('/compare/') ? { merge_base_commit: { sha: base } } : {
    state: 'open', changed_files: 1, head: { sha: head }, base: { sha: 'd'.repeat(40), ref: 'main', repo: { id: 1 } },
  };
  github.pages = async () => [changed];
  const collected = await collectPullRequest(github, 7, head);
  assert.equal(collected.base_sha, base); assert.equal(collected.policy_sha, 'd'.repeat(40));
  github.pages = async () => [];
  await assert.rejects(() => collectPullRequest(github, 7, head), /incomplete/);
});

test('publisher refuses a same-name check owned by another App', async () => {
  const github = new Provider(); github.begin = async () => ({ id: 1, head_sha: head, name: CHECK_NAME, app: { id: 9 } });
  await assert.rejects(() => produceReview({ ...options(github), engine: async input => complete(input) }), /canonical App/);
});

test('provider errors are explicit and writes are never retried', async () => {
  let calls = 0;
  const github = new GitHub({ repository: 'example/repository', readToken: 'source', checkToken: 'checks',
    fetcher: async (_url, request) => { calls++; assert.equal(request.headers.authorization, 'Bearer checks');
      return { ok: false, status: 403 }; } });
  await assert.rejects(() => github.request('/check-runs', { method: 'POST', checks: true, body: {} }), /HTTP 403/);
  assert.equal(calls, 1);
});
