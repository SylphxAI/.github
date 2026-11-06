import test from 'node:test';
import assert from 'node:assert/strict';
import { createReviewInput, parseReviewResult, resultSchema } from '../.github/actions/pr-review/contract.mjs';
import { gatewayReviewer, reviewRequest } from '../.github/actions/pr-review/gateway.mjs';
import { planReview } from '../.github/actions/pr-review/partition.mjs';
import { produceReview } from '../.github/actions/pr-review/producer.mjs';
import { APP_ID, CHECK_NAME, bundleFile } from '../.github/actions/pr-review/github.mjs';

const model = 'vendor/reviewer', maxOutputTokens = 8192, byteBudget = 70_000;
const configuration = { model, maxOutputTokens };
const base = 'a'.repeat(40), head = 'b'.repeat(40);
const source = Object.fromEntries(Array.from({ length: 3 }, (_, i) => [`src/change${i}.js`, [
  `before-${i}-${'x'.repeat(12_000)}\n`, `after-${i}-${'y'.repeat(12_000)}\n`,
]]));
const dependency = { path: 'src/dependency.js', side: 'head', commit_sha: head,
  content: 'export const dependency = true;\n' };
const evidence = { path: dependency.path, side: 'head', start_line: 1, end_line: 1,
  start_byte: null, end_byte: null, quote: dependency.content.trimEnd() };
const inputFor = (extra = {}) => createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: base, head_sha: head, policy_version: '1', trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' },
  files: Object.entries(source).map(([filename, [before, after]]) => bundleFile({ filename, status: 'modified' }, before, after)),
  context: [dependency], ...extra });
const wire = (input, changes = {}) => ({ schema_version: 1, repository_id: input.repository_id,
  base_sha: input.base_sha, head_sha: input.head_sha, input_digest: input.input_digest,
  reviewer_version: input.reviewer_version, policy_version: input.policy_version,
  completion: 'complete', findings: [], omitted_paths: [], context_notes: [], context_requests: [], reason: '',
  review_summary: 'Reviewed every assigned source row, the dependency and changed contract.', related_evidence: [], binary_reviews: [], ...changes });
const response = value => ({ ok: true, status: 200, json: async () => ({ object: 'chat.completion', model,
  choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(value) } }] }) });
function engine(reply) {
  const posts = [], attempts = new Map();
  const review = gatewayReviewer({ token: 'fixture', ...configuration, deadlineMs: Date.now() + 10_000,
    fetcher: async (url, request) => {
      if (request.method === 'GET') {
        assert.equal(new URL(url).pathname, '/v1/public/models/vendor/reviewer');
        return { ok: true, json: async () => ({ id: model, max_output_tokens: maxOutputTokens,
          context_window: byteBudget + maxOutputTokens + 4096 }) };
      }
      assert.equal(String(url), 'https://api.sylphx.com/v1/chat/completions');
      assert.ok(Buffer.byteLength(request.body) <= byteBudget);
      const body = JSON.parse(request.body), part = JSON.parse(body.messages[1].content);
      posts.push({ body, part });
      const n = (attempts.get(part.input_digest) ?? 0) + 1; attempts.set(part.input_digest, n);
      const value = reply(part, n, body);
      return value?.error ? { ok: false, status: 429, json: async () => value } : response(value);
    } });
  return { review, posts };
}
async function publish(reply, { current = () => true } = {}) {
  const { review, posts } = engine(reply), run = { id: 1, name: CHECK_NAME, head_sha: head, app: { id: APP_ID } };
  const reads = [];
  const github = { repository: 'example/repo', current: async () => current(posts), begin: async () => run,
    finish: async (_run, conclusion, output) => Object.assign(run, { conclusion, output }),
    source: async (path, sha, optional) => {
      reads.push({ path, sha });
      if (path === '.github/review-policy.toml') return 'version=1\n[tier0]\npaths=[]\n[generated]\npaths=[]\n[review]\npaths=["**"]\n';
      if (source[path]) return source[path][sha === base ? 0 : 1];
      if (path === dependency.path) { assert.equal(sha, head); return dependency.content; }
      if (optional) return null;
      throw new Error('Unexpected source request');
    } };
  const receipt = await produceReview({ github, target: { kind: 'pull_request', number: 1, repository_id: '1',
    base_sha: base, head_sha: head, files: Object.keys(source).map(filename => ({ filename, status: 'modified' })) },
    trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' }, createReviewInput,
    engineVersion: 'fixture', engine: review, deadlineMs: Date.now() + 10_000 });
  return { receipt, run, posts, reads };
}
const second = part => part.scope?.kind === 'partition' && part.scope.index === '00000002';
const needDependency = part => wire(part, { completion: 'incomplete', omitted_paths: part.files.map(f => f.path),
  reason: 'Material dependency needed.', context_requests: [{ path: dependency.path, side: 'head' }] });
const hasDependency = part => part.context.some(c => c.path === dependency.path);

// The witnesses establish the rejection class, not the unavailable raw model
// bytes. Simulate a global/previous digest echoed by part 2 after pinned recovery.
test('every request binds its own exact identity, not the global source digest or another part', () => {
  const input = inputFor(), measure = part => Buffer.byteLength(reviewRequest(part, configuration));
  const parts = planReview(input, measure, byteBudget);
  assert.equal(parts.length, 3);
  assert.equal(new Set(parts.map(p => p.input_digest)).size, 3);
  for (const part of parts) {
    assert.equal(part.source_digest, input.input_digest);
    assert.notEqual(part.input_digest, part.source_digest);
    const schema = JSON.parse(reviewRequest(part, configuration)).response_format.json_schema.schema;
    for (const key of ['repository_id', 'base_sha', 'head_sha', 'input_digest', 'reviewer_version', 'policy_version']) {
      assert.deepEqual(schema.properties[key].enum, [part[key]], key);
      assert.equal(schema.properties[key].type, 'string');
      assert.equal(resultSchema.properties[key].enum, undefined, 'static validation schema must not be mutated');
    }
  }
});

test('three source parts and synthesis complete with exact pinned context and distinct identities', async () => {
  const input = inputFor(), { review, posts } = engine(part => wire(part,
    part.scope?.kind === 'partition' ? { related_evidence: [evidence] } : {}));
  const result = await review(input);
  assert.equal(result.status, 'complete', result.reason);
  assert.deepEqual(result.parts, { planned: 3, completed: 3 });
  assert.equal(result.input_digest, input.input_digest);
  assert.equal(posts.length, 4);
  assert.equal(posts.at(-1).part.scope.kind, 'synthesis');
  assert.equal(posts.at(-1).part.reviewed_parts.length, 3);
});

test('16006 shape: part 2 corrects a miscopied digest after collecting exact pinned context', async () => {
  const { receipt, run, posts, reads } = await publish((part, attempt, body) => {
    if (!second(part)) return wire(part);
    if (!hasDependency(part)) return needDependency(part);
    if (attempt === 1) return wire(part, { input_digest: part.source_digest });
    const correction = JSON.parse(body.messages.at(-1).content);
    assert.equal(correction.reason, 'mismatched input_digest');
    assert.equal(correction.input_digest, part.input_digest);
    return wire(part, { related_evidence: [evidence] });
  });
  assert.equal(receipt.conclusion, 'success', receipt.reason);
  assert.deepEqual(receipt.parts, { planned: 3, completed: 3 });
  assert.equal(reads.filter(r => r.path === dependency.path).length, 1);
  assert.equal(posts.filter(p => p.part.scope?.index === '00000001').length, 1, 'completed source part is reused');
  const attempts = posts.filter(p => second(p.part) && hasDependency(p.part));
  assert.equal(attempts.length, 2);
  assert.equal(attempts[0].part.input_digest, attempts[1].part.input_digest);
  assert.match(run.output.summary, /Source parts: 3\/3 complete/);
});

for (const exhausted of [false, true]) test(`persistent part-2 digest mismatch fails closed${exhausted ? ' in the final pass' : ' after bounded correction'}`, async () => {
  const input = inputFor(exhausted ? { context_recovery: 'exhausted' } : {});
  const { review, posts } = engine(part => wire(part, second(part) ? { input_digest: part.source_digest } : {}));
  const result = await review(input);
  assert.equal(result.status, 'unavailable');
  assert.equal(result.terminal_invalid, true);
  assert.match(result.reason, /Part 2\/3: mismatched input_digest/);
  assert.deepEqual(result.parts, { planned: 3, completed: 1 });
  assert.equal(posts.filter(p => second(p.part)).length, exhausted ? 1 : 3);
  assert.equal(posts.some(p => p.part.scope.kind === 'synthesis'), false);
  const wrong = wire(posts.at(-1).part, { input_digest: input.input_digest });
  assert.equal(parseReviewResult(JSON.stringify(wrong), posts.at(-1).part, { discardUngrounded: true }).status, 'unavailable');
});

for (const phase of ['part-2', 'synthesis']) test(`a persistent ${phase} mismatch publishes an explicit failure, never a neutral or a false pass`, async () => {
  const { receipt, run } = await publish(part => wire(part,
    (phase === 'part-2' ? second(part) : part.scope?.kind === 'synthesis') ? { input_digest: part.source_digest } : {}));
  assert.equal(receipt.conclusion, 'failure');
  assert.equal(run.output.title, 'Review invalid');
  assert.match(receipt.reason, phase === 'part-2' ? /Part 2\/3: mismatched input_digest/ : /Cross-file synthesis: mismatched input_digest/);
  assert.deepEqual(run.output.annotations, []);
});

test('16082 shape: part-2 unsupported evidence is corrected/discarded without losing exact context or source coverage', async () => {
  const { receipt, run, posts } = await publish(part => {
    if (!second(part)) return wire(part);
    if (!hasDependency(part)) return needDependency(part);
    return wire(part, { related_evidence: [evidence], findings: [{ path: part.files[0].path, side: 'head',
      start_line: 1, end_line: 1, severity: 'medium', title: 'Dependency contract changed',
      explanation: 'The changed contract breaks the dependency.', trigger: 'A caller reads the dependency.',
      consequence: 'The caller receives an invalid result.', evidence: [{ ...evidence, quote: 'PRIVATE_FABRICATED_QUOTE' }] }] });
  });
  assert.equal(receipt.conclusion, 'success', receipt.reason);
  assert.deepEqual(receipt.parts, { planned: 3, completed: 3 });
  assert.equal(receipt.discarded_items, 1);
  assert.equal(posts.filter(p => second(p.part) && hasDependency(p.part)).length, 3);
  assert.match(run.output.summary, /1 items discarded: evidence not in source/);
  assert.ok(!JSON.stringify(receipt).includes('PRIVATE_FABRICATED_QUOTE'));
  assert.ok(!JSON.stringify(run.output).includes('PRIVATE_FABRICATED_QUOTE'));
  assert.deepEqual(posts.at(-1).part.reviewed_parts[1].related_evidence, [evidence]);
});

test('a real middle-part quota refusal stays neutral and is not retried', async () => {
  const { receipt, posts } = await publish(part => second(part)
    ? { error: { code: 'included_capacity_unavailable', message: 'private' } } : wire(part));
  assert.equal(receipt.conclusion, 'neutral');
  assert.equal(posts.length, 2);
  assert.match(receipt.reason, /HTTP 429 \(included_capacity_unavailable\)/);
});

test('a head-read outage supersedes terminal invalidity rather than declaring a review failure', async () => {
  const { receipt } = await publish(part => wire(part, second(part) ? { input_digest: part.source_digest } : {}), {
    current: posts => { if (posts.length >= 4) throw new Error('Head read unavailable'); return true; },
  });
  assert.equal(receipt.conclusion, 'neutral');
  assert.match(receipt.reason, /Current head could not be verified/);
});
