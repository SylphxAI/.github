import test from 'node:test';
import assert from 'node:assert/strict';
import { createReviewInput, parseReviewResult } from '../.github/actions/pr-review/contract.mjs';
import { bundleFile } from '../.github/actions/pr-review/github.mjs';

const inputFor = () => createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: '1',
  trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' }, files: [
    bundleFile({ filename: 'change.js', status: 'modified' }, 'before\nstable\n', 'after\nstable\n'),
    bundleFile({ filename: 'caller.js', status: 'modified' }, 'old caller\n', 'new caller\n'),
  ], context: [] });
const evidence = (path, quote, line = 1, side = 'head') => ({ path, side, start_line: line, end_line: line,
  start_byte: null, end_byte: null, quote });
const finding = evidence => ({ path: 'change.js', side: 'head', start_line: 2, end_line: 2,
  severity: 'high', title: 'Changed contract breaks the consumer', explanation: 'The caller no longer receives its expected value.',
  trigger: 'The caller reads the value.', consequence: 'The request fails.', evidence });
const wire = (input, findings) => ({ schema_version: 1, repository_id: input.repository_id,
  base_sha: input.base_sha, head_sha: input.head_sha, input_digest: input.input_digest,
  reviewer_version: input.reviewer_version, policy_version: input.policy_version, completion: 'complete',
  findings, related_evidence: [], binary_reviews: [], review_summary: 'Reviewed the changed contracts.',
  omitted_paths: [], context_requests: [], context_notes: [], reason: '' });
const parse = (findings, input = inputFor()) => parseReviewResult(JSON.stringify(wire(input, findings)), input);

for (const [name, proof, path, side] of [
  ['exact changed evidence', evidence('change.js', 'after'), 'change.js', 'head'],
  ['cross-file changed evidence', evidence('caller.js', 'new caller'), 'caller.js', 'head'],
  ['deleted evidence', evidence('change.js', 'before', 1, 'base'), 'change.js', 'base'],
  ['snapped changed evidence', evidence('change.js', 'after', 99), 'change.js', 'head'],
]) test(`off-diff finding reanchors on ${name} without lowering severity`, () => {
  const result = parse([finding([evidence('change.js', 'stable', 2), proof])]);
  assert.equal(result.status, 'complete', result.reason);
  assert.equal(result.findings.length, 1);
  assert.deepEqual([result.findings[0].path, result.findings[0].side, result.findings[0].start_line,
    result.findings[0].end_line, result.findings[0].severity], [path, side, 1, 1, 'high']);
});

test('off-diff evidence without a causative changed line remains a visible non-blocking note', () => {
  const result = parse([finding([evidence('change.js', 'stable', 2)])]);
  assert.equal(result.status, 'complete');
  assert.deepEqual(result.findings, []);
  assert.equal(result.context_notes.length, 1);
  assert.match(result.context_notes[0], /Outside the change \(non-blocking; high\)/);
  for (const text of ['Changed contract breaks', 'request fails', 'caller reads']) assert.ok(result.context_notes[0].includes(text));
});

for (const mode of ['fabricated quote', 'unknown finding path', 'unknown evidence path']) test(`${mode} still fails source validation`, () => {
  const f = finding([evidence(mode === 'unknown evidence path' ? 'unknown.js' : 'change.js', mode === 'fabricated quote' ? 'invented' : 'after')]);
  if (mode === 'unknown finding path') f.path = 'unknown.js';
  const result = parse([f]);
  assert.equal(result.status, 'unavailable');
  assert.match(result.reason, /finding outside supplied source|evidence differs from supplied source/);
});
