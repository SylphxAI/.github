import test from 'node:test';
import assert from 'node:assert/strict';
import { createReviewInput, reviewerVersion, sourceValidationReasons } from '../.github/actions/pr-review/contract.mjs';
import { gatewayReviewer, reviewRequest } from '../.github/actions/pr-review/gateway.mjs';
import { bundleFile } from '../.github/actions/pr-review/github.mjs';
import { seal } from '../.github/actions/pr-review/partition.mjs';

const model = 'vendor/reviewer', maxOutputTokens = 8192;
const configuration = { model, maxOutputTokens };
const inputFor = (kind = 'partition', exhausted = false) => {
  const input = createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
    base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: '1',
    trigger: { kind: 'workflow_run', id: '42', generation: '1' },
    files: [bundleFile({ filename: 'src/change.js', status: 'modified' }, 'before\n', 'after\n')], context: [] });
  return seal({ ...input, scope: { kind }, ...(exhausted ? { context_recovery: 'exhausted' } : {}),
    ...(kind === 'synthesis' ? { files: [], manifest: input.files, references: [evidence] } : {}) });
};
const evidence = { path: 'src/change.js', side: 'head', start_line: 1, end_line: 1,
  start_byte: null, end_byte: null, quote: 'after' };
const finding = { path: evidence.path, side: 'head', start_line: 1, end_line: 1, severity: 'medium',
  title: 'Changed return value', explanation: 'The changed value breaks its caller.', trigger: 'The caller uses the value.',
  consequence: 'The caller receives the wrong result.', evidence: [evidence] };
const wire = (input, changes = {}) => ({ schema_version: 1, repository_id: input.repository_id,
  base_sha: input.base_sha, head_sha: input.head_sha, input_digest: input.input_digest,
  reviewer_version: reviewerVersion, policy_version: input.policy_version, completion: 'complete',
  findings: [finding], omitted_paths: [], context_notes: [], context_requests: [], reason: '',
  review_summary: 'Reviewed the changed behavior.', related_evidence: [], binary_reviews: [], ...changes });
const invalid = (input, reason = 'evidence differs from supplied source') => wire(input,
  reason === 'finding outside changed source' ? { findings: [{ ...finding, start_line: 9, end_line: 9 }] } :
  reason === 'finding outside supplied source' ? { findings: [{ ...finding, path: 'unknown.js' }] } :
  reason === 'related evidence differs from supplied source' ? { related_evidence: [{ ...evidence, quote: 'fabricated' }] } :
  { findings: [{ ...finding, evidence: [{ ...evidence, quote: 'fabricated' }] }] });

async function run(input, reply, byteLimit = 100_000) {
  const posts = [];
  const review = gatewayReviewer({ token: 'test-token', ...configuration, deadlineMs: Date.now() + 10_000,
    fetcher: async (url, request) => {
      if (request.method === 'GET') return { ok: true, json: async () => ({ id: model,
        context_window: byteLimit + maxOutputTokens + 4096, max_output_tokens: maxOutputTokens }) };
      assert.equal(String(url), 'https://api.sylphx.com/v1/chat/completions');
      const body = JSON.parse(request.body), part = JSON.parse(body.messages[1].content);
      posts.push(body);
      assert.equal(part.source_files, undefined); assert.equal(part.source_context, undefined);
      const value = reply(part, posts.length);
      return { ok: true, status: 200, json: async () => {
        if (value instanceof Error) throw value;
        return { object: 'chat.completion', model, choices: [{ finish_reason: 'stop', message: {
          role: 'assistant', content: typeof value === 'string' ? value : JSON.stringify(value) } }] };
      } };
    } });
  return { result: await review(input), posts };
}

for (const kind of ['partition', 'synthesis']) test(`${kind} corrects rejected evidence using bounded machine metadata`, async () => {
  const input = inputFor(kind);
  const { result, posts } = await run(input, (part, n) => n === 1 ? invalid(part) : wire(part));
  assert.equal(result.status, 'complete'); assert.equal(posts.length, 2);
  assert.deepEqual(posts[1].messages.slice(0, 2), posts[0].messages);
  assert.deepEqual(posts[1].messages[2], { role: 'assistant', content: JSON.stringify(invalid(input)) });
  assert.equal(posts[1].messages[3].role, 'user');
  const correction = JSON.parse(posts[1].messages[3].content);
  assert.deepEqual(correction, { reason: 'evidence differs from supplied source', validation_failure: {
    kind: 'finding_evidence', index: 0, finding_index: 0, path: evidence.path, side: 'head' },
    rule: "anchor every finding on lines inside its file's changed hunks; cite unchanged lines only as evidence; quote evidence verbatim with exact line numbers" });
  for (const source of ['fabricated', 'after', finding.explanation]) assert.ok(!posts[1].messages[3].content.includes(source));
});

for (const reason of sourceValidationReasons) test(`three rejected attempts preserve ${reason}`, async () => {
  const { result, posts } = await run(inputFor(), part => invalid(part, reason));
  assert.equal(result.status, 'unavailable'); assert.equal(result.reason, reason); assert.equal(posts.length, 3);
  assert.equal(posts[2].messages.length, 4);
  assert.deepEqual(posts[2].messages.slice(0, 2), posts[0].messages);
});

test('corrective feedback over the byte budget resends the original body', async () => {
  const input = inputFor(), limit = Buffer.byteLength(reviewRequest(input, configuration));
  const { result, posts } = await run(input, (part, n) => n === 1 ? invalid(part) : wire(part), limit);
  assert.equal(result.status, 'complete'); assert.equal(posts.length, 2); assert.deepEqual(posts[1], posts[0]);
});

for (const retry of ['HTTP invalid JSON', 'review invalid JSON', 'supplied context', 'source validation']) {
  test(`exhausted context prevents ${retry} retries after one POST`, async () => {
    const { result, posts } = await run(inputFor('partition', true), part =>
      retry === 'HTTP invalid JSON' ? new SyntaxError('malformed response') :
      retry === 'review invalid JSON' ? '{' : retry === 'source validation' ? invalid(part) :
      wire(part, { completion: 'incomplete', findings: [], omitted_paths: [evidence.path], reason: 'Need context.',
        context_requests: [{ path: evidence.path, side: 'head' }] }));
    assert.equal(posts.length, 1);
    assert.equal(result.status, retry === 'supplied context' ? 'incomplete' : 'unavailable');
  });
}

test('non-source validator failures do not trigger corrective retries', async () => {
  const { result, posts } = await run(inputFor(), part => wire(part, { head_sha: part.base_sha }));
  assert.equal(result.reason, 'mismatched head_sha'); assert.equal(posts.length, 1);
});

test('real partitioned synthesis keeps local validation source out of corrective requests', async () => {
  const input = seal({ ...inputFor(), scope: undefined,
    files: Array.from({ length: 3 }, (_, i) => bundleFile({ filename: `src/change${i}.js`, status: 'modified' },
      `before-${i}-${'x'.repeat(12_000)}\n`, `after-${i}-${'y'.repeat(12_000)}\n`)) });
  const file = input.files[0], row = file.hunks.flatMap(h => h.rows).find(r => r.kind === 'add');
  const validated = { ...finding, path: file.path, evidence: [{ ...evidence, path: file.path,
    start_byte: row.after.start_byte, end_byte: row.after.end_byte, quote: row.after.content }] };
  let synthesisCalls = 0;
  const { result, posts } = await run(input, part => {
    if (part.scope?.kind !== 'synthesis') return wire(part, { findings: [] });
    synthesisCalls++;
    assert.deepEqual(part.references, []);
    assert.deepEqual(part.files, []);
    return wire(part, { findings: [{ ...validated, evidence: synthesisCalls === 1
      ? [{ ...validated.evidence[0], quote: 'fabricated' }] : validated.evidence }] });
  }, 70_000);
  assert.equal(result.status, 'complete', result.reason); assert.ok(result.parts.planned > 1); assert.equal(synthesisCalls, 2);
  const [first, second] = posts.slice(-2);
  assert.deepEqual(second.messages.slice(0, 2), first.messages);
  assert.ok(!second.messages[3].content.includes(row.after.content.trimEnd()));
  assert.equal(second.messages.length, 4);
});

test('a validator rejection at the deadline is returned without another POST', async t => {
  const now = Date.now(); let current = now;
  t.mock.method(Date, 'now', () => current);
  const { result, posts } = await run(inputFor(), part => {
    current = now + 10_000;
    return invalid(part);
  });
  assert.equal(result.reason, 'evidence differs from supplied source'); assert.equal(posts.length, 1);
});
