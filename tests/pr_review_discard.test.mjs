import test from 'node:test';
import assert from 'node:assert/strict';
import { createReviewInput, parseReviewResult } from '../.github/actions/pr-review/contract.mjs';
import { gatewayReviewer } from '../.github/actions/pr-review/gateway.mjs';
import { APP_ID, CHECK_NAME, bundleFile } from '../.github/actions/pr-review/github.mjs';
import { produceReview, validateResult } from '../.github/actions/pr-review/producer.mjs';
import { recoverContext } from '../.github/actions/pr-review/recovery.mjs';

const model = 'vendor/reviewer';
const inputFor = (large = false) => createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: '1',
  trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' }, context: [],
  files: Array.from({ length: large ? 3 : 1 }, (_, i) => bundleFile({ filename: `change${i}.js`, status: 'modified' },
    `before-${i}\n${large ? 'x'.repeat(12_000) : 'stable'}\n`, `after-${i}\n${large ? 'y'.repeat(12_000) : 'stable'}\n`)) });
const evidence = (i = 0, quote = `after-${i}`) => ({ path: `change${i}.js`, side: 'head', start_line: 1, end_line: 1,
  start_byte: null, end_byte: null, quote });
const finding = (proof = evidence(), title = 'Changed value breaks the caller') => ({ path: 'change0.js', side: 'head',
  start_line: 1, end_line: 1, severity: 'high', title, explanation: 'The caller no longer receives the expected value.',
  trigger: 'A caller reads the changed result.', consequence: 'The request fails.', evidence: [proof] });
const wire = (input, extra = {}) => ({ schema_version: 1, repository_id: input.repository_id,
  base_sha: input.base_sha, head_sha: input.head_sha, input_digest: input.input_digest,
  reviewer_version: input.reviewer_version, policy_version: input.policy_version, completion: 'complete',
  findings: [], related_evidence: [], binary_reviews: [], review_summary: 'Reviewed the supplied source.',
  omitted_paths: [], context_requests: [], context_notes: [], reason: '', ...extra });
const parse = (input, extra, salvage = true) => parseReviewResult(JSON.stringify(wire(input, extra)), input, { discardUngrounded: salvage });

for (const bad of [evidence(0, 'PRIVATE_UNSUPPORTED_QUOTE'), evidence(4, 'PRIVATE_UNKNOWN_SOURCE'),
  { ...evidence(), start_byte: 0, end_byte: 2, quote: 'after-0' }]) {
  test(`only the ungrounded related item is discarded (${bad.path}, ${bad.start_byte})`, () => {
    const input = inputFor(), result = parse(input, { findings: [finding()], related_evidence: [bad, evidence()] });
    assert.equal(result.status, 'complete'); assert.deepEqual(result.findings, [finding()]);
    assert.deepEqual(result.related_evidence, [evidence()]); assert.equal(result.discarded_items.length, 1);
    for (const canary of ['PRIVATE_UNSUPPORTED_QUOTE', 'PRIVATE_UNKNOWN_SOURCE']) assert.ok(!JSON.stringify(result).includes(canary));
    assert.equal(parse(input, { related_evidence: [bad] }, false).status, 'unavailable');
  });
}

test('one bad quote discards its whole finding, not other findings or validated evidence', () => {
  const input = inputFor(), bad = { ...finding(evidence(0, 'UNSUPPORTED_FINDING_QUOTE'), 'UNSUPPORTED_FINDING_TITLE'),
    evidence: [evidence(), evidence(0, 'UNSUPPORTED_FINDING_QUOTE')] };
  const result = parse(input, { findings: [bad, finding()], related_evidence: [evidence()] });
  assert.equal(result.status, 'complete'); assert.deepEqual(result.findings, [finding()]);
  assert.deepEqual(result.related_evidence, [evidence()]); assert.equal(result.discarded_items.length, 1);
  for (const canary of ['UNSUPPORTED_FINDING_QUOTE', 'UNSUPPORTED_FINDING_TITLE']) assert.ok(!JSON.stringify(result).includes(canary));
});

test('discarding evidence cannot repair incomplete coverage, wrong identity, invalid anchors or unresolved binary provenance', () => {
  const input = inputFor(), extra = { related_evidence: [evidence(0, 'unsupported')] };
  const incomplete = parse(input, { ...extra, completion: 'incomplete', omitted_paths: ['change0.js'], reason: 'Missing work.' });
  assert.equal(incomplete.status, 'incomplete'); assert.equal(incomplete.discarded_items.length, 1);
  for (const changes of [{ head_sha: input.base_sha }, { findings: [{ ...finding(), path: 'unknown.js' }] },
    { omitted_paths: ['unknown.js'] }]) assert.equal(parse(input, { ...extra, ...changes }).status, 'unavailable');
  const binary = createReviewInput({ ...input, files: [bundleFile({ filename: 'asset.bin', status: 'modified' },
    Buffer.from([0, 1]), Buffer.from([0, 2]))] });
  assert.equal(parse(binary, extra).status, 'unavailable');
});

function reviewer(reply, { budget = 100_000, current } = {}) {
  const posts = [];
  const execute = gatewayReviewer({ token: 'test-token', model, maxOutputTokens: 8192, deadlineMs: Date.now() + 10_000, current,
    fetcher: async (_url, request) => {
      if (request.method === 'GET') return { ok: true, json: async () => ({ id: model,
        context_window: budget + 8192 + 4096, max_output_tokens: 8192 }) };
      const body = JSON.parse(request.body), part = JSON.parse(body.messages[1].content);
      assert.equal(part.source_files, undefined); assert.equal(part.source_context, undefined); posts.push(part);
      const value = reply(part, posts);
      return { ok: true, status: 200, json: async () => ({ object: 'chat.completion', model,
        choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(wire(part, value)) } }] }) };
    } });
  return { execute, posts };
}

for (const mode of ['related', 'finding', 'both']) test(`partition publication isolates ${mode} after exactly three correction attempts`, async () => {
  const { execute, posts } = reviewer(() => ({ findings: [finding(), ...mode === 'related' ? [] :
    [finding(evidence(0, 'PRIVATE_UNGROUNDED'), 'PRIVATE_UNGROUNDED_TITLE')]],
    related_evidence: mode === 'finding' ? [evidence()] : [evidence(0, 'PRIVATE_UNGROUNDED'), evidence()] }));
  const source = { before: 'before-0\nstable\n', after: 'after-0\nstable\n' }, runs = [];
  const github = { repository: 'example/repo', current: async () => true,
    source: async (path, sha) => path === 'change0.js' ? source[sha === 'a'.repeat(40) ? 'before' : 'after'] : null,
    begin: async () => ({ id: 1, name: CHECK_NAME, head_sha: 'b'.repeat(40), app: { id: APP_ID } }),
    finish: async (run, conclusion, output) => { runs.push({ conclusion, output }); return run; } };
  const receipt = await produceReview({ github, target: { kind: 'pull_request', number: 1, repository_id: '1',
    base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), files: [{ filename: 'change0.js', status: 'modified' }] },
    trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' }, detailsUrl: 'https://example.com/review',
    createReviewInput, engineVersion: 'test', engine: execute });
  assert.equal(posts.length, 3); assert.equal(receipt.conclusion, 'failure');
  assert.equal(receipt.discarded_items, mode === 'both' ? 2 : 1);
  assert.match(runs[0].output.summary, new RegExp(`${mode === 'both' ? 2 : 1} items discarded: evidence not in source`));
  assert.equal(runs[0].output.annotations.length, 1);
  assert.ok(!JSON.stringify({ receipt, runs }).includes('PRIVATE_UNGROUNDED'));
});

test('discarded-only findings complete without an annotation or invented code failure', async () => {
  const { execute, posts } = reviewer(() => ({ findings: [finding(evidence(0, 'unsupported'))] }));
  const result = await execute(inputFor());
  assert.equal(result.status, 'complete'); assert.equal(posts.length, 3);
  assert.deepEqual(result.findings, []); assert.equal(result.discarded_items.length, 1);
});

test('all partitions and synthesis complete after independently discarding unsupported related items', async () => {
  const { execute, posts } = reviewer(part => ({ related_evidence: [
    { ...evidence(), path: part.scope.kind === 'synthesis' ? 'synthesis-unknown.js' : `${part.files[0]?.path ?? 'context'}-unknown.js`,
      quote: 'UNSUPPORTED_SYNTHESIS_QUOTE' },
    ...part.scope.kind === 'synthesis' ? [evidence()] : [],
  ] }), { budget: 60_000 });
  const result = await execute(inputFor(true));
  assert.equal(result.status, 'complete', result.reason); assert.ok(result.parts.planned > 1);
  assert.equal(result.parts.completed, result.parts.planned);
  assert.equal(posts.filter(p => p.scope.kind === 'synthesis').length, 3);
  assert.equal(posts.length, 3 * (result.parts.planned + 1));
  assert.equal(result.discarded_items.length, result.parts.planned + 1);
  assert.deepEqual(result.related_evidence, [evidence()]);
  assert.ok(!JSON.stringify(result).includes('UNSUPPORTED_SYNTHESIS_QUOTE'));
});

for (const badQuote of [false, true]) for (const final of ['complete', 'incomplete']) test(`synthesis no_new_context reaches one final ${final} assessment (discard=${badQuote})`, async () => {
  const input = inputFor(true), related_evidence = badQuote ? [evidence(0, 'unsupported')] : [];
  const { execute, posts } = reviewer(part => {
    if (part.scope.kind !== 'synthesis') return {};
    if (part.context_recovery === 'exhausted' && final === 'complete') return { related_evidence };
    return { completion: 'incomplete', omitted_paths: input.files.map(f => f.path), reason: 'Need the already supplied changed source.',
      context_requests: [{ path: 'change0.js', side: 'head' }], related_evidence };
  }, { budget: 60_000 });
  let sourceReads = 0;
  const github = { current: async () => true, source: async () => { sourceReads++; throw new Error('Already supplied'); } };
  const { result } = await recoverContext({ input, github, target: {}, execute, createReviewInput, validateResult });
  assert.equal(result.status, final); assert.equal(sourceReads, 0);
  assert.equal(result.recovery.stop, final === 'complete' ? 'no_new_context_finalized' : 'no_new_context');
  assert.equal(result.terminal_incomplete ?? false, final === 'incomplete');
  assert.equal(posts.filter(p => p.scope.kind === 'synthesis' && p.context_recovery === 'exhausted').length, badQuote ? 3 : 1);
  assert.equal(result.discarded_items.length, badQuote ? 1 : 0);
});
