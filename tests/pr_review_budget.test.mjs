import test from 'node:test';
import assert from 'node:assert/strict';
import { bundleFile, digest } from '../.github/actions/pr-review/github.mjs';
import { literalSegments, fileSegments, changedRanges } from '../.github/actions/pr-review/source.mjs';
import { createReviewInput, canonical, parseReviewResult, reviewerVersion } from '../.github/actions/pr-review/contract.mjs';
import { gatewayReviewer, reviewRequest, contextBudget } from '../.github/actions/pr-review/gateway.mjs';
import { planReview } from '../.github/actions/pr-review/partition.mjs';

const model = 'openai/gpt-6.1-sol';
const configuration = { model, maxOutputTokens: 8192, reasoningEffort: 'high' };
const metadata = { id: model, context_window: 150_000, max_output_tokens: 128_000 };
const measure = input => Buffer.byteLength(reviewRequest(input, configuration));
const budget = contextBudget(metadata, model, configuration.maxOutputTokens).request_byte_limit;
const inputFor = (files, context = []) => createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: '1',
  trigger: { kind: 'workflow_run', id: '42', generation: '1' }, files, context });
const wire = (input, findings = [], extra = {}) => ({ schema_version: 1, repository_id: input.repository_id,
  base_sha: input.base_sha, head_sha: input.head_sha, input_digest: input.input_digest, reviewer_version: reviewerVersion,
  policy_version: input.policy_version, completion: 'complete', findings, omitted_paths: [], context_notes: [], reason: '',
  review_summary: 'Reviewed source behavior, contracts, callers and relevant guards.', related_evidence: [], ...extra });
const response = (input, findings = [], extra = {}) => ({ ok: true, status: 200, json: async () => ({ object: 'chat.completion', model,
  choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(wire(input, findings, extra)) } }] }) });
const evidenceFor = (file, row) => {
  const side = row.after ? 'head' : 'base', segment = row.after ?? row.before;
  return { path: file.path, side, start_line: segment.start_line,
    end_line: segment.end_line - (segment.content.endsWith('\n') ? 1 : 0),
    start_byte: segment.start_byte, end_byte: segment.end_byte, quote: segment.content };
};
const findingFor = (file, row, id = '') => {
  const evidence = evidenceFor(file, row);
  return { path: file.path, side: evidence.side, start_line: evidence.start_line, end_line: evidence.start_line,
    severity: 'medium', title: `Changed behavior ${id}`, explanation: 'The supplied change breaks the caller.',
    trigger: 'The caller uses this changed value.', consequence: 'The caller receives the wrong result.', evidence: [evidence] };
};
const changed = part => {
  for (const file of part.files) for (const hunk of file.hunks) for (const row of hunk.rows) if (row.kind !== 'context') return { file, row };
};
const largeInput = () => inputFor(Array.from({ length: 7 }, (_, n) => bundleFile({ filename: `src/file${n}.rs`, status: 'modified' },
  Array.from({ length: 500 }, (_, i) => `fn row${i}() { return "old-${n}-${i}"; }\n`).join(''),
  Array.from({ length: 500 }, (_, i) => `fn row${i}() { return "new-${n}-${i}"; }\n`).join(''))));

test('literal views preserve numbers, strings, duplicate keys, invalid JSON, Unicode and line endings byte for byte', () => {
  for (const source of ['{"n":9007199254740993123456789,"e":1e-9999,"z":-0}',
    '{"x":1,"x":2,"s":"\\u0061\\\\\\\"{}[],"}', '{"invalid":[1,,2,}', '{"unclosed":"literal,}[',
    '\uFEFF{"s":"🐉漢字","crlf":true}\r\n', '"' + '🐉'.repeat(4000) + '"', 'x'.repeat(20_000)]) {
    const segments = literalSegments(source, true, 13);
    assert.equal(segments.map(s => s.content).join(''), source);
    assert.equal(digest(segments.map(s => s.content).join('')), digest(source));
    for (const s of segments) assert.equal(Buffer.from(source).subarray(s.start_byte, s.end_byte).toString(), s.content);
  }
  for (const [before, after] of [['{"n":9007199254740993123456789}', '{"n":9007199254740993123456788}'],
    ['{"x":1,"x":2}', '{"x":1,"x":3}'], ['{"s":"\\u0061"}', '{"s":"a"}'], ['{"x":1,}', '{"x":1}']]) {
    const file = bundleFile({ filename: 'source.json', status: 'modified' }, before, after);
    assert.ok(file.hunks.some(h => h.rows.some(r => r.kind === 'add')));
    assert.ok(file.hunks.some(h => h.rows.some(r => r.kind === 'delete')));
  }
});

test('a two megabyte minified fixture retains its literal changes without resending the whole physical line', () => {
  const before = '{"values":{' + Array.from({ length: 36_000 }, (_, i) => `"field${i}":{"n":9007199254740993123456789,"s":"\\u0061"}`).join(',') + '}}\n';
  assert.ok(Buffer.byteLength(before) > 1_980_000);
  const after = before.replace('"field18000":', '"renamed18000":');
  const file = bundleFile({ filename: 'fixtures/baseline.json', status: 'modified' }, before, after);
  assert.equal(file.before.sha256, digest(before)); assert.equal(file.after.sha256, digest(after));
  assert.ok(Buffer.byteLength(JSON.stringify(file)) < 45_000);
  assert.ok(fileSegments(file, 'after').some(s => s.content.includes('"renamed18000":')));
  assert.ok(changedRanges(file).every(r => r.start === 1 && r.end === 1));
  const input = inputFor([file]);
  const row = file.hunks.flatMap(h => h.rows).find(r => r.kind === 'add');
  const finding = findingFor(file, row);
  assert.equal(parseReviewResult(JSON.stringify(wire(input, [finding])), input).status, 'complete');
  for (const change of [{ quote: 'invented' }, { start_byte: row.after.start_byte + 1 }, { start_line: 2 }, { end_byte: null }]) {
    const forged = { ...finding, evidence: [{ ...finding.evidence[0], ...change }] };
    assert.equal(parseReviewResult(JSON.stringify(wire(input, [forged])), input).status, 'unavailable');
  }
});

test('all changed paths and hunk rows survive complete bounded partitioning with a global manifest', () => {
  const input = largeInput(), parts = planReview(input, measure, budget);
  assert.ok(parts.length > 1);
  const expected = input.files.flatMap(f => f.hunks.flatMap(h => h.rows.map(r => canonical([f.path, h.id, r])))).sort();
  const actual = parts.flatMap(p => p.files.flatMap(f => f.hunks.flatMap(h => h.rows.map(r => canonical([f.path, h.id, r]))))).sort();
  assert.deepEqual(actual, expected);
  for (const part of parts) {
    assert.ok(measure(part) <= budget);
    assert.equal(part.source_digest, input.input_digest);
    assert.deepEqual(part.manifest.map(f => f.path), input.files.map(f => f.path));
    assert.equal(part.manifest.reduce((n, f) => n + f.hunks.length, 0), input.files.reduce((n, f) => n + f.hunks.length, 0));
  }
  assert.equal(new Set(parts.map(p => p.input_digest)).size, parts.length);
});

test('bounded model calls preserve all findings and use source-qualified cross-file synthesis without changing model or budgets', async () => {
  const input = largeInput(), posts = [], findings = [];
  const review = gatewayReviewer({ token: 'private-value', ...configuration, deadlineMs: Date.now() + 10_000,
    fetcher: async (url, request) => {
      if (request.method === 'GET') return { ok: true, json: async () => metadata };
      const body = JSON.parse(request.body), part = JSON.parse(body.messages[1].content);
      assert.equal(body.model, model); assert.equal(body.max_completion_tokens, configuration.maxOutputTokens);
      assert.equal(body.reasoning_effort, 'high'); assert.ok(Buffer.byteLength(request.body) <= budget);
      posts.push(part);
      if (part.scope?.kind === 'synthesis') {
        assert.equal(part.reviewed_parts.length, posts.length - 1);
        assert.ok(part.references.length > 1);
        const cross = { ...findings[0], title: 'Cross-file regression', evidence: [part.references[0], part.references.at(-1)] };
        findings.push(cross); return response(part, [cross]);
      }
      const { file, row } = changed(part), finding = findingFor(file, row, posts.length);
      findings.push(finding); return response(part, [finding], { related_evidence: [evidenceFor(file, row)] });
    } });
  const result = await review(input);
  assert.equal(result.status, 'complete'); assert.equal(result.input_digest, input.input_digest);
  assert.deepEqual(new Set(result.findings.map(canonical)), new Set(findings.map(canonical)));
  assert.equal(posts.at(-1).scope.kind, 'synthesis');
  assert.equal(result.parts.completed, result.parts.planned);
});

test('late refusal retains validated findings, marks partially covered paths incomplete and stops without retry', async () => {
  const file = bundleFile({ filename: 'long-line.rs', status: 'modified' }, 'a'.repeat(200_000), 'b'.repeat(200_000));
  const input = inputFor([file]); let posts = 0;
  const review = gatewayReviewer({ token: 'private-value', ...configuration, deadlineMs: Date.now() + 10_000,
    fetcher: async (_url, request) => {
      if (request.method === 'GET') return { ok: true, json: async () => metadata };
      posts++;
      if (posts === 2) return { ok: false, status: 429, json: async () => ({ error: { code: 'included_capacity_unavailable' } }) };
      const part = JSON.parse(JSON.parse(request.body).messages[1].content), { file, row } = changed(part);
      return response(part, [findingFor(file, row)]);
    } });
  const result = await review(input);
  assert.equal(result.status, 'incomplete'); assert.equal(posts, 2);
  assert.equal(result.findings.length, 1); assert.deepEqual(result.omitted_paths, ['long-line.rs']);
  assert.ok(result.parts.completed < result.parts.planned); assert.match(result.reason, /HTTP 429/);
});

test('metadata and final wire bounds fail before inference rather than inventing a context cap', async () => {
  const input = largeInput();
  for (const meta of [{ ...metadata, id: 'other/model' }, { id: model }, { ...metadata, context_window: 1 },
    { ...metadata, max_output_tokens: 1 }]) {
    let posts = 0;
    const review = gatewayReviewer({ token: 'private-value', ...configuration, deadlineMs: Date.now() + 5000,
      fetcher: async (_url, request) => { if (request.method === 'POST') posts++; return { ok: true, json: async () => meta }; } });
    assert.equal((await review(input)).status, 'unavailable'); assert.equal(posts, 0);
  }
  assert.equal(contextBudget(metadata, model).reserved_output, metadata.max_output_tokens);
  assert.throws(() => planReview(input, measure, 10), /manifest/);
});

test('large context is fully partitioned, and deleted and renamed paths retain real physical coordinates', () => {
  const text = 'line 🐉\r\n'.repeat(30_000);
  const files = [bundleFile({ filename: 'new.rs', previous_filename: 'old.rs', status: 'renamed' }, 'unchanged\n', 'unchanged\n'),
    bundleFile({ filename: 'removed.rs', status: 'removed' }, 'deleted\r\n', null)];
  const input = inputFor(files, [{ path: 'AGENTS.md', content: text }]), parts = planReview(input, measure, budget);
  const segments = parts.flatMap(p => p.context.flatMap(c => c.segments ?? []));
  assert.equal(segments.map(s => s.content).join(''), text);
  assert.ok(parts.every(p => measure(p) <= budget));
  assert.equal(files[0].previous_path, 'old.rs');
  const renamedRow = files[0].hunks[0].rows[0], removedRow = files[1].hunks[0].rows[0];
  const findings = [findingFor(files[0], renamedRow), findingFor(files[1], removedRow)];
  assert.equal(findings[1].side, 'base');
  assert.equal(parseReviewResult(JSON.stringify(wire(input, findings)), input).status, 'complete');
});
