import test from 'node:test';
import assert from 'node:assert/strict';
import { createReviewInput, parseReviewResult } from '../.github/actions/pr-review/contract.mjs';
import { APP_ID, CHECK_NAME } from '../.github/actions/pr-review/github.mjs';
import { recoverContext } from '../.github/actions/pr-review/recovery.mjs';
import { produceReview, validateResult } from '../.github/actions/pr-review/producer.mjs';
import { reviewPartitions } from '../.github/actions/pr-review/partition.mjs';
import { reviewRequest } from '../.github/actions/pr-review/gateway.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40);
const source = 'message Frame { string value = 1; }\n';
const generator = 'emit("artifact.png", compile("source.proto"));\n';
const evidence = (path, quote) => ({ path, side: 'head', start_line: 1, end_line: 1,
  start_byte: null, end_byte: null, quote: quote.trimEnd() });
const provenance = { path: 'artifact.png', status: 'source-reviewed',
  source_evidence: [evidence('source.proto', source)], derivation_evidence: [evidence('generate.js', generator)],
  limitation: 'Source and derivation reviewed; binary equivalence was not verified.' };
const wire = (input, extra = {}) => ({ schema_version: 1, repository_id: input.repository_id,
  base_sha: input.base_sha, head_sha: input.head_sha, input_digest: input.input_digest,
  reviewer_version: input.reviewer_version, policy_version: input.policy_version,
  completion: 'complete', findings: [], related_evidence: [], binary_reviews: [provenance],
  review_summary: 'Reviewed source and derivation; 0 findings.', omitted_paths: [],
  context_requests: [], context_notes: [provenance.limitation], reason: '', ...extra });
const incomplete = input => wire(input, { completion: 'incomplete', omitted_paths: ['artifact.png'],
  binary_reviews: [{ ...provenance, status: 'unresolved', source_evidence: [], derivation_evidence: [] }],
  reason: 'No binary provenance verdict returned.' });

async function publish(mode) {
  const inputs = []; let output;
  const github = { repository: 'example/repo', current: async () => true,
    source: async path => path === 'artifact.png' ? Buffer.from([0, 0xff])
      : path === 'source.proto' ? source : path === 'generate.js' ? generator : null,
    begin: async () => ({ id: 1, head_sha: head, app: { id: APP_ID }, name: CHECK_NAME }),
    finish: async (run, conclusion, value) => { output = value; return { ...run, conclusion }; },
  };
  const target = { kind: 'pull_request', number: 7, base_sha: base, head_sha: head, repository_id: '1',
    files: [{ filename: 'artifact.png', status: 'modified' },
      { filename: 'source.proto', status: 'added' }, { filename: 'generate.js', status: 'added' }] };
  const receipt = await produceReview({ github, target, trigger: { kind: 'pull_request', id: '7', generation: '1' },
    createReviewInput, engineVersion: 'binary-verdict-test', deadlineMs: Date.now() + 60000,
    engine: async input => {
      inputs.push(input);
      // Both initial and explicit-verdict requests include the exact pinned
      // source/derivation text and verified binary identity, within the wire cap.
      assert.equal(input.files.find(f => f.path === 'artifact.png').after.sha256.length, 64);
      assert.ok(input.files.some(f => f.path === 'source.proto'));
      assert.ok(input.files.some(f => f.path === 'generate.js'));
      assert.ok(Buffer.byteLength(reviewRequest(input, { model: 'review-model' })) <= 1_013_136);
      let result;
      if (inputs.length === 1 && mode.startsWith('initial-invalid')) result = {
        ...parseReviewResult(JSON.stringify(wire(input, { binary_reviews: [] })), input), terminal_invalid: true };
      else if (mode === 'text-incomplete' || (inputs.length > 1 && mode === 'binary-then-text-incomplete')) result = parseReviewResult(JSON.stringify(wire(input, { completion: 'incomplete',
        omitted_paths: ['source.proto'], reason: 'Text review remains incomplete.' })), input);
      else if (inputs.length === 1 || mode === 'no-verdict' || mode === 'initial-invalid-no-verdict') result = parseReviewResult(JSON.stringify(incomplete(input)), input);
      else if (mode === 'invalid') result = { ...parseReviewResult(JSON.stringify(wire(input, { binary_reviews: [] })), input), terminal_invalid: true };
      else if (mode === 'engine-unavailable') result = { ...parseReviewResult(JSON.stringify(wire(input, { completion: 'unavailable',
        omitted_paths: ['artifact.png'], reason: 'Gateway refused HTTP 503.' })), input) };
      else result = parseReviewResult(JSON.stringify(wire(input)), input);
      return result;
    } });
  return { inputs, receipt, output };
}

for (const mode of ['no-verdict', 'invalid', 'engine-unavailable', 'reviewed', 'initial-invalid-reviewed', 'initial-invalid-no-verdict']) test(`binary no_requests gets one explicit provenance verdict pass: ${mode}`, async () => {
  const { inputs, receipt, output } = await publish(mode);
  assert.equal(inputs.length, 2);
  assert.equal(inputs[1].binary_provenance_verdict, true);
  assert.equal(inputs[1].context_recovery, 'exhausted');
  assert.notEqual(inputs[0].input_digest, inputs[1].input_digest);
  assert.deepEqual(inputs[1].files, inputs[0].files);
  assert.deepEqual(inputs[1].context, inputs[0].context);
  if (['reviewed', 'initial-invalid-reviewed'].includes(mode)) {
    assert.equal(receipt.conclusion, 'success');
    assert.equal(receipt.unavailability_code, undefined);
    assert.equal(receipt.recovery.stop, 'no_requests_finalized');
  } else {
    // Author-supplied provenance and full hashes plus zero findings and zero
    // context requests MUST NOT become a successful review without a verdict.
    assert.notEqual(receipt.conclusion, 'success');
    assert.equal(receipt.findings, 0);
    const code = { 'initial-invalid-no-verdict': 'binary_provenance_no_verdict', 'no-verdict': 'binary_provenance_no_verdict', invalid: 'binary_provenance_invalid_verdict',
      'engine-unavailable': 'binary_provenance_engine_unavailable' }[mode];
    assert.equal(receipt.unavailability_code, code);
    assert.equal(receipt.recovery.stop, code);
    assert.ok(receipt.reason.includes(code));
    assert.ok(output.summary.includes(`Unavailability cause: ${code}`));
    assert.ok(output.title.includes(code));
  }
});
test('binary verdict deadline is typed and does not start a second inference without its budget', async () => {
  const { inputs } = await publish('reviewed');
  const originalNow = Date.now; let clock = 0, calls = 0;
  try {
    Date.now = () => clock;
    const { result } = await recoverContext({ input: inputs[0], github: {}, target: {},
      createReviewInput, validateResult, deadlineMs: 100, execute: async input => {
        calls++; clock = 60; return parseReviewResult(JSON.stringify(incomplete(input)), input);
      } });
    assert.equal(calls, 1);
    assert.equal(result.status, 'unavailable');
    assert.equal(result.unavailability_code, 'binary_provenance_deadline');
    assert.equal(result.recovery.stop, 'binary_provenance_deadline');
  } finally { Date.now = originalNow; }
});

for (const mode of ['text-incomplete', 'binary-then-text-incomplete']) test(`resolved binary provenance preserves text terminal incompleteness: ${mode}`, async () => {
  const { inputs, receipt, output } = await publish(mode);
  assert.equal(inputs.length, 2);
  assert.equal(inputs[1].binary_provenance_verdict, mode === 'text-incomplete' ? undefined : true);
  assert.equal(receipt.conclusion, 'failure');
  assert.equal(receipt.unavailability_code, undefined);
  assert.equal(receipt.recovery.stop, 'no_requests');
  assert.equal(output.title, 'Review incomplete');
  assert.ok(!output.summary.includes('binary_provenance_no_verdict'));
});

test('validated resolved binary assessment survives incomplete text parsing and final recovery', async () => {
  const { inputs } = await publish('reviewed');
  const { result } = await recoverContext({ input: inputs[0], github: {}, target: {},
    createReviewInput, validateResult, execute: async input => parseReviewResult(JSON.stringify(wire(input, {
      completion: 'incomplete', omitted_paths: ['source.proto'], reason: 'Text review remains incomplete.',
    })), input) });
  assert.equal(result.status, 'incomplete');
  assert.equal(result.terminal_incomplete, true);
  assert.deepEqual(result.binary_reviews, [provenance]);
  assert.equal(result.unavailability_code, undefined);
});

test('partition aggregation retains resolved binary provenance while a text part is incomplete', async () => {
  const { inputs } = await publish('reviewed');
  const input = createReviewInput({ ...inputs[0], context: [...inputs[0].context,
    { path: 'source.proto', side: 'head', commit_sha: head, content: source },
    { path: 'generate.js', side: 'head', commit_sha: head, content: generator },
  ] });
  const result = await reviewPartitions(input, {
    // Force a two-file binary/source part followed by a text-only part.
    requestBytes: value => value.files.length * 10, byteBudget: 20,
    execute: async part => {
      const textPending = part.files.some(file => file.path === 'generate.js');
      return parseReviewResult(JSON.stringify(wire(part, {
        completion: textPending ? 'incomplete' : 'complete',
        binary_reviews: part.files.some(file => file.source_mode === 'binary-metadata') ? [provenance] : [],
        omitted_paths: textPending ? ['generate.js'] : [],
      })), part);
    },
  });
  assert.equal(result.status, 'incomplete');
  assert.deepEqual(result.parts, { planned: 2, completed: 1 });
  assert.deepEqual(result.binary_reviews, [provenance]);
  assert.deepEqual(result.omitted_paths, ['generate.js']);
});
