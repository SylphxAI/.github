import test from 'node:test';
import assert from 'node:assert/strict';
import { bundleFile } from '../.github/actions/pr-review/github.mjs';
import { createReviewInput, reviewerVersion } from '../.github/actions/pr-review/contract.mjs';
import { gatewayReviewer, reviewRequest, contextBudget } from '../.github/actions/pr-review/gateway.mjs';
import { recoveryLimits } from '../.github/actions/pr-review/recovery.mjs';

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
async function run(input, synthesize) {
  const posts = [], synthesis = [];
  const review = gatewayReviewer({ token: 'test-token', ...configuration, deadlineMs: Date.now() + 10_000,
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
    assert.equal(part.context[0].segments[0].content, 'const old0 = 0;\r\n');
  }
});
