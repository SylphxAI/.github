import test from 'node:test';
import assert from 'node:assert/strict';
import { APP_ID, CHECK_NAME } from '../.github/actions/pr-review/github.mjs';
import { createReviewInput } from '../.github/actions/pr-review/contract.mjs';
import { planReview } from '../.github/actions/pr-review/partition.mjs';
import { collectBundle, produceReview } from '../.github/actions/pr-review/producer.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40), policySha = 'c'.repeat(40);
const policy = (review = []) => `version=1\n[tier0]\npaths=[]\n[review]\npaths=${JSON.stringify(review)}\n[generated]\npaths=["generated/**"]\n[[generated.provenance]]\npaths=["generated/**"]\njobs=["regenerate"]\n`;
const trigger = { kind: 'synthetic', id: '1', generation: '1' };
const modified = path => ({ filename: path, status: 'modified' });
const target = files => ({ base_sha: base, policy_sha: policySha, head_sha: head, repository_id: '1', review_requested: true, files });
const complete = input => ({ status: 'complete', base_sha: input.base_sha, head_sha: input.head_sha,
  input_digest: input.input_digest, reviewer_version: input.reviewer_version, findings: [], omitted_paths: [], reason: '' });

// Entirely synthetic: operation names/scopes are separated by 1,200 invented
// endpoints. No production source or receipt is copied into this fixture.
function openapi(version) {
  return JSON.stringify({ openapi: '3.1.0', info: { title: 'Synthetic regression only', version }, paths: {
    '/ai/models/{id}': { get: { operationId: 'ai.ai_models.get', security: [{ 'ai:read': [] }] } },
    ...Object.fromEntries(Array.from({ length: 1200 }, (_, i) => [`/synthetic/${i}`, { get: { description: `Synthetic ${version} ${'x'.repeat(160)}` } }])),
    '/capacity/cells/{id}': { get: { operationId: 'capacity.read_cell_capacity', security: [{ 'capacity:read': [] }] } },
  } }, null, 2) + '\n';
}

function provider(text = policy(), sources = {}) {
  const reads = [], runs = [];
  return { repository: 'example/synthetic', reads, runs,
    current: async () => true,
    source: async (path, sha) => {
      reads.push({ path, sha });
      if (path === '.github/review-policy.toml') { assert.equal(sha, policySha); return text; }
      if (path in sources) return typeof sources[path] === 'function' ? sources[path](sha) : sources[path];
      if (path === 'src/manual.js') return sha === base ? 'export const x=1;\n' : 'export const x=2;\n';
      return null;
    },
    begin: async t => { const run = { id: runs.length + 1, name: CHECK_NAME, app: { id: APP_ID }, head_sha: t.head_sha, status: 'in_progress' }; runs.push(run); return run; },
    finish: async (run, conclusion, output) => { Object.assign(run, { conclusion, output, status: 'completed' }); return run; },
  };
}
const options = (github, files) => ({ github, target: target(files), trigger, createReviewInput, engineVersion: 'generated-test' });

test('307796-byte synthetic OpenAPI consumes zero generated source reads and zero AI parts in a mixed review', async () => {
  const text = openapi('after'), parsed = JSON.parse(text);
  assert.equal(Buffer.byteLength(text), 307796);
  assert.deepEqual(parsed.paths['/ai/models/{id}'].get, { operationId: 'ai.ai_models.get', security: [{ 'ai:read': [] }] });
  assert.deepEqual(parsed.paths['/capacity/cells/{id}'].get, { operationId: 'capacity.read_cell_capacity', security: [{ 'capacity:read': [] }] });
  const github = provider(policy(), { 'generated/openapi.json': sha => openapi(sha === base ? 'before' : 'after') });
  const collected = await collectBundle(github, target([modified('generated/openapi.json'), modified('src/manual.js')]), trigger);
  const parts = planReview(createReviewInput(collected.bundle), p => Buffer.byteLength(JSON.stringify(p)), 24000);
  assert.deepEqual(collected.bundle.files.map(f => f.path), ['src/manual.js']);
  assert.equal(github.reads.filter(r => r.path.startsWith('generated/')).length, 0);
  assert.equal(parts.filter(p => p.files.some(f => f.path === 'generated/openapi.json')).length, 0);
  assert.equal(parts.length, 1);
  let reviewed = 0;
  const receipt = await produceReview({ ...options(github, [modified('generated/openapi.json'), modified('src/manual.js')]),
    engine: async input => { reviewed++; assert.deepEqual(input.files.map(f => f.path), ['src/manual.js']); return complete(input); } });
  assert.equal(reviewed, 1); assert.equal(receipt.conclusion, 'success');
});

test('generated-only [review] requests are explicitly non-applicable, not completed empty reviews', async () => {
  const github = provider();
  const receipt = await produceReview({ ...options(github, [modified('generated/capture.json')]),
    engine: async () => { throw new Error('must not infer an empty scope'); } });
  assert.equal(receipt.applicable, false); assert.equal(receipt.conclusion, 'neutral');
  assert.match(github.runs[0].output.summary, /excludes every changed source as generated/);
  assert.equal(github.reads.length, 1);
});

test('explicit review paths override generated source exclusion, while provenance remains independent', async () => {
  const github = provider(policy(['generated/openapi.json']), { 'generated/openapi.json': 'synthetic contract\n' });
  const collected = await collectBundle(github, target([modified('generated/openapi.json')]), trigger);
  assert.equal(collected.bundle.files[0].path, 'generated/openapi.json');
  assert.equal(github.reads.filter(r => r.path === 'generated/openapi.json').length, 2);
});

test('added, deleted, modified, copied and generated-to-generated renamed files never fetch generated bytes', async () => {
  const files = ['added', 'removed', 'modified', 'copied', 'renamed'].map((status, i) => ({ filename: `generated/${i}.json`, status,
    ...(['copied', 'renamed'].includes(status) ? { previous_filename: `generated/old${i}.json` } : {}) }));
  const github = provider();
  assert.equal((await collectBundle(github, target(files), trigger)).needs_review, false);
  assert.equal(github.reads.length, 1);
  await assert.rejects(() => collectBundle(github, target([{ filename: 'generated/x', status: 'unknown' }]), trigger), /Unsupported change kind/);
});

for (const [before, after] of [['generated/old.js', 'src/new.js'], ['src/old.js', 'generated/new.js']]) {
  test(`rename transition reviews both literal sides when either identity is handwritten: ${before} -> ${after}`, async () => {
    const github = provider(policy(), { [before]: 'old literal\n', [after]: 'new literal\n' });
    const collected = await collectBundle(github, target([{ filename: after, previous_filename: before, status: 'renamed' }]), trigger);
    assert.equal(collected.bundle.files[0].previous_path, before);
    assert.ok(github.reads.some(r => r.path === before && r.sha === base));
    assert.ok(github.reads.some(r => r.path === after && r.sha === head));
    assert.equal(collected.allowContext(before), true);
  });
}

test('a missing base policy grants no exclusion; a head-side declaration cannot lower the tier', async () => {
  const github = provider(null, { 'generated/openapi.json': 'synthetic\n' });
  const collected = await collectBundle(github, target([modified('generated/openapi.json')]), trigger);
  assert.equal(collected.bundle.files.length, 1);
  assert.equal(collected.bundle.policy_version, 'missing');
});

test('context recovery cannot reintroduce excluded generated source and retains one bounded final assessment', async () => {
  const github = provider(); let executions = 0;
  const receipt = await produceReview({ ...options(github, [modified('generated/openapi.json'), modified('src/manual.js')]),
    engine: async input => {
      executions++;
      if (input.context_recovery === 'exhausted') return complete(input);
      return { ...complete(input), status: 'incomplete', reason: 'Need generated source.',
        context_requests: [{ path: 'generated/openapi.json', side: 'head' }] };
    } });
  assert.equal(receipt.conclusion, 'success'); assert.equal(executions, 2);
  assert.equal(github.reads.filter(r => r.path.startsWith('generated/')).length, 0);
});

test('mixed recovery still collects requested handwritten context, not generated bytes or generated ancestor instructions', async () => {
  const github = provider(policy(['generated/handwritten.js']), { 'src/helper.js': 'helper literal\n', 'generated/handwritten.js': 'handwritten literal\n' });
  let executions = 0;
  const receipt = await produceReview({ ...options(github, [modified('generated/handwritten.js')]), engine: async input => {
    executions++;
    if (input.context.some(c => c.path === 'src/helper.js')) return complete(input);
    return { ...complete(input), status: 'incomplete', reason: 'Need helper.', context_requests: [
      { path: 'generated/capture.json', side: 'head' }, { path: 'src/helper.js', side: 'head' }] };
  } });
  assert.equal(receipt.conclusion, 'success'); assert.equal(executions, 2);
  assert.ok(github.reads.some(r => r.path === 'src/helper.js' && r.sha === head));
  assert.ok(!github.reads.some(r => ['generated/capture.json', 'generated/AGENTS.md'].includes(r.path)));
});
