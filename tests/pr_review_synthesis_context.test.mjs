import test from 'node:test';
import assert from 'node:assert/strict';
import { bundleFile } from '../.github/actions/pr-review/github.mjs';
import { createReviewInput, reviewerVersion, sourceQuote } from '../.github/actions/pr-review/contract.mjs';
import { gatewayReviewer, reviewRequest, contextBudget } from '../.github/actions/pr-review/gateway.mjs';
import { recoveryLimits } from '../.github/actions/pr-review/recovery.mjs';
import { reviewPartitions } from '../.github/actions/pr-review/partition.mjs';

const model = 'openai/gpt-6.1-sol';
const configuration = { model, maxOutputTokens: 8192 };
const metadata = { id: model, context_window: 48_000, max_output_tokens: 8192 };
const budget = contextBudget(metadata, model, configuration.maxOutputTokens).request_byte_limit;
const inputFor = (context = []) => createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: '1',
  trigger: { kind: 'workflow_run', id: '42', generation: '1' }, context,
  files: Array.from({ length: 4 }, (_, n) => bundleFile({ filename: `src/file${n}.js`, status: 'modified' },
    Array.from({ length: 80 }, (_, i) => `const old${i} = ${n};\r\n`).join(''),
    Array.from({ length: 80 }, (_, i) => `const next${i} = "🐉-${n}";\r\n`).join(''))) });
const response = (input, extra = {}) => ({ ok: true, status: 200, json: async () => ({ object: 'chat.completion', model,
  choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify({
    schema_version: 1, repository_id: input.repository_id, base_sha: input.base_sha, head_sha: input.head_sha,
    input_digest: input.input_digest, reviewer_version: reviewerVersion, policy_version: input.policy_version,
    completion: 'complete', findings: [], omitted_paths: [], context_notes: [], context_requests: [], reason: '',
    review_summary: 'Reviewed assigned source.', related_evidence: [], binary_reviews: [], ...extra,
  }) } }] }) });
const incomplete = requests => ({ completion: 'incomplete', reason: 'Need exact dependency source.', context_requests: requests });
async function run(input, synthesize, current) {
  const posts = [], synthesis = [];
  const review = gatewayReviewer({ token: 'test-token', ...configuration, deadlineMs: Date.now() + 10_000, current,
    fetcher: async (_url, request) => {
      if (request.method === 'GET') return { ok: true, json: async () => metadata };
      assert.ok(Buffer.byteLength(request.body) <= budget);
      const part = JSON.parse(JSON.parse(request.body).messages[1].content);
      assert.equal(part.source_files, undefined);
      assert.equal(part.source_context, undefined);
      posts.push(part);
      if (part.scope?.kind === 'synthesis') {
        synthesis.push(part);
        return response(part, synthesize(part, synthesis.length));
      }
      return response(part);
    } });
  return { result: await review(input), posts, synthesis };
}

test('synthesis receives only requested in-bundle source on the next wire call and validates against the full bundle', async () => {
  const context = { path: 'src/helper.js', side: 'head', commit_sha: 'b'.repeat(40), content: 'export const helper = "🐉";\r\n' };
  const input = inputFor([context]), requests = [{ path: 'src/file0.js', side: 'head' }, { path: context.path, side: 'head' }];
  const { result, synthesis } = await run(input, (part, attempt) => {
    if (attempt === 1) { assert.deepEqual(part.files, []); assert.deepEqual(part.context, []); return incomplete(requests); }
    assert.deepEqual(part.files, []);
    assert.deepEqual(part.context.map(c => [c.path, c.side]), requests.map(r => [r.path, r.side]));
    const changed = part.context.find(c => c.path === 'src/file0.js');
    assert.equal(changed.commit_sha, input.head_sha);
    assert.equal(changed.segments.map(s => s.content).join(''),
      Array.from({ length: 80 }, (_, i) => `const next${i} = "🐉-0";\r\n`).join(''));
    assert.deepEqual(part.context.find(c => c.path === context.path), context);
    return { related_evidence: [{ path: 'src/file3.js', side: 'head', start_line: 1, end_line: 1,
      start_byte: null, end_byte: null, quote: 'const next0 = "🐉-3";' }] };
  });
  assert.equal(result.status, 'complete');
  assert.equal(result.input_digest, input.input_digest);
  assert.equal(result.parts.completed, result.parts.planned);
  assert.equal(synthesis.length, 2);
  assert.notEqual(synthesis[0].input_digest, synthesis[1].input_digest);
});

test('over-budget requested source preserves the original incomplete result without an extra POST', async () => {
  const context = { path: 'src/huge.js', side: 'head', commit_sha: 'b'.repeat(40), content: 'x'.repeat(budget) };
  const requests = [{ path: context.path, side: 'head' }];
  const { result, synthesis, posts } = await run(inputFor([context]), () => incomplete(requests));
  assert.equal(synthesis.length, 1);
  assert.ok(Buffer.byteLength(reviewRequest({ ...synthesis[0], context: [context] }, configuration)) > budget);
  assert.equal(result.reason, 'Cross-file synthesis: review incomplete (completion=incomplete, reason=nonempty)');
  assert.deepEqual(result.context_requests, requests);
  assert.equal(posts.length, result.parts.planned + 1);
});

test('requests outside supplied source or on a different side stay with normal recovery', async () => {
  const input = inputFor([{ path: 'src/helper.js', side: 'base', commit_sha: 'a'.repeat(40), content: 'base only\n' },
    { path: 'src/absent.js', side: 'head', commit_sha: 'b'.repeat(40), absent: true }]);
  for (const requests of [[{ path: 'src/missing.js', side: 'head' }],
    [{ path: 'src/file0.js', side: 'head' }, { path: 'src/missing.js', side: 'head' }],
    [{ path: 'src/helper.js', side: 'head' }], [{ path: 'src/absent.js', side: 'head' }]]) {
    const { result, synthesis } = await run(input, () => incomplete(requests));
    assert.equal(result.status, 'incomplete');
    assert.deepEqual(result.context_requests, requests);
    assert.equal(synthesis.length, 1);
  }
});

test('repeated in-bundle requests stop after the existing recovery round limit', async () => {
  const requests = [{ path: 'src/file0.js', side: 'base' }];
  const { result, synthesis } = await run(inputFor(), () => incomplete(requests));
  assert.equal(result.status, 'incomplete');
  assert.equal(synthesis.length, 1 + recoveryLimits.rounds);
  for (const part of synthesis.slice(1)) {
    assert.deepEqual(part.context.map(c => [c.path, c.side]), [['src/file0.js', 'base']]);
    assert.equal(part.context[0].segments.map(s => s.content).join(''),
      Array.from({ length: 80 }, (_, i) => `const old${i} = 0;\r\n`).join(''));
  }
});

test('successive synthesis requests include only the latest requested source and retain verified absence', async () => {
  const absent = { path: 'src/absent.js', side: 'head', commit_sha: 'b'.repeat(40), absent: true };
  const { result, synthesis } = await run(inputFor([absent]), (part, attempt) => {
    assert.deepEqual(part.context[0], absent);
    if (attempt < 3) return incomplete([{ path: `src/file${attempt - 1}.js`, side: 'head' }]);
    assert.deepEqual(part.context.map(c => c.path), [absent.path, 'src/file1.js']);
    return {};
  });
  assert.equal(result.status, 'complete');
  assert.equal(synthesis.length, 3);
});

test('head changes and unverified heads stop before the additional synthesis POST', async () => {
  for (const head of [false, null]) {
    let requested = false;
    const { result, synthesis } = await run(inputFor(), () => {
      requested = true;
      return incomplete([{ path: 'src/file0.js', side: 'head' }]);
    }, async () => requested ? head : true);
    assert.equal(result.status, 'incomplete');
    assert.equal(synthesis.length, 1);
    assert.match(result.reason, head === false ? /superseded/ : /could not be verified/);
  }
});

test('re-synthesis validates unrequested bundle context and refuses quotes absent from every source', async () => {
  const requested = { path: 'src/requested.js', side: 'head', commit_sha: 'b'.repeat(40), content: 'requested source\n' };
  const unrequested = { path: 'src/unrequested.js', side: 'head', commit_sha: 'b'.repeat(40),
    content: 'first line\n  return "bundle source";\nlast line\n' };
  for (const mode of ['exact', 'snap', 'forged']) {
    const { result, synthesis } = await run(inputFor([requested, unrequested]), (part, attempt) => {
      if (attempt === 1) return incomplete([{ path: requested.path, side: 'head' }]);
      assert.deepEqual(part.context, [requested]);
      return { related_evidence: [{ path: unrequested.path, side: 'head',
        start_line: mode === 'snap' ? 1 : 2, end_line: mode === 'snap' ? 1 : 2,
        start_byte: null, end_byte: null, quote: mode === 'forged' ? 'fabricated source' : '  return "bundle source";' }] };
    });
    assert.equal(synthesis.length, 2);
    if (mode === 'forged') {
      assert.equal(result.status, 'incomplete');
      assert.equal(result.validation_failure.code, 'quote_mismatch');
      assert.equal(result.validation_failure.path, unrequested.path);
      assert.equal(result.validation_failure.input_digest, synthesis[1].input_digest);
    } else {
      assert.equal(result.status, 'complete');
      if (mode === 'snap') assert.equal(result.snapped_evidence.at(-1).start_line, 2);
    }
  }
});

test('nonempty wire source retains other bundle identities and takes precedence over duplicate context identities', () => {
  const base = { path: 'src/helper.js', side: 'base', commit_sha: 'a'.repeat(40), content: 'base helper\n' };
  const head = { path: base.path, side: 'head', commit_sha: 'b'.repeat(40), content: 'bundle helper\n' };
  const input = inputFor([base, head]);
  const scoped = { ...input, files: [input.files[0]], context: [{ ...head, content: 'wire helper\n' }],
    source_files: input.files, source_context: input.context };
  assert.equal(sourceQuote(scoped, 'src/file3.js', 'head', 1, 1), 'const next0 = "🐉-3";');
  assert.equal(sourceQuote(scoped, base.path, 'base', 1, 1), 'base helper');
  assert.equal(sourceQuote(scoped, head.path, 'head', 1, 1), 'wire helper');
});

test('requested changed source uses renamed base paths and never substitutes a missing or binary side', async () => {
  const source = bundleFile({ filename: 'src/new-name.js', previous_filename: 'src/old-name.js', status: 'renamed' },
    'before rename\r\n', 'after rename\r\n');
  const removed = bundleFile({ filename: 'src/removed.js', status: 'removed' }, 'removed source\n', null);
  const binary = bundleFile({ filename: 'src/binary.dat', status: 'modified' }, Buffer.from([0]), Buffer.from([1]));
  const input = createReviewInput({ ...inputFor(), files: [...inputFor().files, source, removed, binary] });
  // Binary provenance belongs to the ordinary review contract; this test only
  // exercises routing of exact-source requests through the public partition API.
  for (const [path, side, content] of [['src/old-name.js', 'base', 'before rename\r\n'],
    ['src/new-name.js', 'head', 'after rename\r\n'], ['src/removed.js', 'base', 'removed source\n'],
    ['src/old-name.js', 'head', null], ['src/removed.js', 'head', null], ['src/binary.dat', 'head', null]]) {
    const synthesis = [];
    const result = await reviewPartitions(input, { requestBytes: part => Buffer.byteLength(reviewRequest(part, configuration)),
      byteBudget: budget, execute: async (part, scope) => {
        const complete = { status: 'complete', findings: [], context_notes: [], related_evidence: [], binary_reviews: [] };
        if (part.scope.kind !== 'synthesis') return complete;
        assert.deepEqual(scope, { files: input.files, context: input.context });
        synthesis.push(part);
        if (synthesis.length === 1) return { ...complete, status: 'incomplete', reason: 'Need source.', context_requests: [{ path, side }] };
        assert.equal(part.context[0].segments.map(s => s.content).join(''), content);
        return complete;
      } });
    assert.equal(synthesis.length, content === null ? 1 : 2);
    assert.equal(result.status, content === null ? 'incomplete' : 'complete');
  }
});
