import test from 'node:test';
import assert from 'node:assert/strict';
import { createReviewInput, parseReviewResult } from '../.github/actions/pr-review/contract.mjs';
import { bundleFile } from '../.github/actions/pr-review/github.mjs';

function review(content, quote, start = 1, end = start + quote.split('\n').length - 1, mode = 'context') {
  const path = 'kernel/artifacts/src/retention.rs';
  const input = createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
    base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: '1',
    trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' },
    files: [bundleFile({ filename: mode === 'file' ? path : 'src/change.js', status: 'modified' }, 'before\n', mode === 'file' ? content : 'after\n')],
    context: mode === 'context' ? [{ path, content }] : [] });
  if (mode === 'reference') input.references = [{ path, side: 'head', start_line: 1,
    end_line: content.split('\n').length, start_byte: null, end_byte: null, quote: content }];
  const result = { schema_version: 1, repository_id: input.repository_id, base_sha: input.base_sha,
    head_sha: input.head_sha, input_digest: input.input_digest, reviewer_version: input.reviewer_version,
    policy_version: input.policy_version, completion: 'complete', findings: [], binary_reviews: [],
    related_evidence: [{ path, side: mode === 'context' ? 'context' : 'head', start_line: start, end_line: end,
      start_byte: null, end_byte: null, quote }], review_summary: 'Reviewed the changed behavior.',
    omitted_paths: [], context_notes: [], context_requests: [], reason: '' };
  return parseReviewResult(JSON.stringify(result), input);
}

for (const mode of ['context', 'file', 'reference']) {
  test(`138-byte source vs 128-byte indentation-stripped quote is restored (${mode})`, () => {
    // Reproduce the witnessed byte lengths and four-line retention.rs window.
    const quote = ['if artifact.expired() {', 'remove_artifact(artifact.id())?;', '// ' + 'x'.repeat(66), '}'].join('\n');
    assert.equal(Buffer.byteLength(quote), 128);
    const source = quote.split('\n').map((line, i) => ' '.repeat([2, 4, 2, 2][i]) + line).join('\n');
    assert.equal(Buffer.byteLength(source), 138);
    const content = [...Array(243).fill('// prefix'), source].join('\n');
    const result = review(content, quote, 244, 247, mode);
    assert.equal(result.status, 'complete', result.reason);
    assert.equal(result.related_evidence[0].quote, source);
    assert.equal(result.snapped_evidence[0].reason, 'whitespace');
    assert.equal(result.snapped_evidence[0].start_line, 244);
  });
}

test('tabs vs spaces and trailing CR whitespace are snapped at the stated lines', () => {
  const source = '\talpha();  \r\n\t\tbeta();\t';
  const result = review(source, '  alpha();\n    beta();');
  assert.equal(result.status, 'complete', result.reason);
  assert.equal(result.related_evidence[0].quote, '\talpha();  \n\t\tbeta();\t');
  assert.equal(result.snapped_evidence[0].reason, 'whitespace');
});

test('unique relocated whole-window whitespace match supplies the validated text', () => {
  const result = review('prefix\n\talpha();\n\t beta();\nsuffix', ' alpha();\n beta();', 1, 2);
  assert.equal(result.status, 'complete', result.reason);
  assert.equal(result.related_evidence[0].quote, '\talpha();\n\t beta();');
  assert.equal(result.related_evidence[0].start_line, 2);
  assert.equal(result.related_evidence[0].end_line, 3);
});

for (const [name, content, quote] of [
  ['one-character content change', ' alpha();\n beta();', 'alpha();\nbeta(1);'],
  ['ambiguous two-window match', 'prefix\n alpha();\n beta();\nmiddle\n\talpha();\n\tbeta();', 'alpha();\nbeta();'],
  ['first-line-only match', 'prefix\n alpha();\n different();\nother', 'alpha();\nbeta();'],
]) test(`${name} stays rejected`, () => {
  const result = review(content, quote, 1, 2);
  assert.equal(result.status, 'unavailable');
  assert.equal(result.validation_failure.code, 'quote_mismatch');
  assert.equal(result.snapped_evidence, undefined);
});

test('stated normalized match wins even if another window is identical', () => {
  const result = review(' alpha();\n beta();\n alpha();\n beta();', 'alpha();\nbeta();', 1, 2);
  assert.equal(result.status, 'complete', result.reason);
  assert.equal(result.related_evidence[0].start_line, 1);
});
