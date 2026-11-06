import test from 'node:test';
import assert from 'node:assert/strict';
import { createReviewInput, parseReviewResult, reviewerVersion } from '../.github/actions/pr-review/contract.mjs';
import { gatewayReviewer } from '../.github/actions/pr-review/gateway.mjs';
import { bundleFile, GitHub } from '../.github/actions/pr-review/github.mjs';
import { validateResult } from '../.github/actions/pr-review/producer.mjs';
import { recoverContext, recoveryLimits } from '../.github/actions/pr-review/recovery.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40);
const inputFor = () => createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: base, head_sha: head, policy_version: '1', trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' },
  files: [bundleFile({ filename: 'src/change.js', status: 'modified' }, 'before\n', 'after\n')], context: [] });
const target = { kind: 'pull_request', number: 7, base_sha: base, head_sha: head, repository_id: '1' };
const request = n => ({ path: `src/dependency-${n}.js`, side: 'head' });
const wire = (input, requests = []) => ({ schema_version: 1, repository_id: input.repository_id,
  base_sha: input.base_sha, head_sha: input.head_sha, input_digest: input.input_digest,
  reviewer_version: reviewerVersion, policy_version: input.policy_version,
  completion: requests.length ? 'incomplete' : 'complete', findings: [], related_evidence: [], binary_reviews: [],
  review_summary: 'Reviewed the supplied changed behavior.', omitted_paths: requests.length ? ['src/change.js'] : [],
  context_notes: [], context_requests: requests, reason: requests.length ? 'Missing exact source.' : '' });
const parsed = (input, requests) => parseReviewResult(JSON.stringify(wire(input, requests)), input);
const run = options => recoverContext({ input: inputFor(), target, createReviewInput, validateResult,
  github: { current: async () => true, source: async () => 'dependency\n' }, ...options });

async function overWire(reply, options = {}) {
  const posts = [], results = [];
  const deadlineMs = Date.now() + 10_000;
  const execute = gatewayReviewer({ token: 'test-token', model: 'vendor/reviewer', deadlineMs,
    maxOutputTokens: 8192, fetcher: async (_url, init) => {
      if (init.method === 'GET') return { ok: true, json: async () => ({
        id: 'vendor/reviewer', context_window: 100_000, max_output_tokens: 8192 }) };
      assert.equal(init.method, 'POST');
      const body = JSON.parse(init.body), input = JSON.parse(body.messages[1].content);
      posts.push({ body, input });
      return { ok: true, json: async () => ({ object: 'chat.completion', model: 'vendor/reviewer',
        choices: [{ finish_reason: 'stop', message: { role: 'assistant',
          content: JSON.stringify(reply(input, posts.length)) } }] }) };
    } });
  const outcome = await run({ deadlineMs, execute, onResult: result => results.push(result), ...options });
  return { ...outcome, posts, results };
}

test('three recovery rounds allow exactly one final POST and record round_limit_finalized', async () => {
  let reads = 0;
  const outcome = await overWire((input, call) => wire(input,
    input.context_recovery === 'exhausted' ? [] : [request(call)]), {
    github: { current: async () => true, source: async () => { reads++; return 'dependency\n'; } },
  });
  assert.equal(outcome.result.status, 'complete');
  assert.deepEqual(outcome.result.parts, { planned: 1, completed: 1 });
  assert.equal(outcome.posts.length, 5); // Initial POST, three enriched POSTs, one final POST.
  assert.equal(reads, 3);
  assert.deepEqual(outcome.result.recovery, {
    rounds: 3, added_files: 3, added_bytes: 33, stop: 'round_limit_finalized',
  });
  assert.equal(outcome.results.at(-1).input_digest, outcome.input.input_digest);
});

test('a final round requesting context preserves the original stop and never starts another recovery', async () => {
  let reads = 0;
  const outcome = await overWire((input, call) => ({ ...wire(input, [request(call)]),
    reason: input.context_recovery === 'exhausted' ? 'Final request must not replace the original.' : 'Original request.' }), {
    github: { current: async () => true, source: async () => { reads++; return 'dependency\n'; } },
  });
  assert.equal(outcome.posts.length, 5);
  assert.equal(reads, 3);
  assert.equal(outcome.result.status, 'incomplete');
  assert.equal(outcome.result.terminal_incomplete, true);
  assert.equal(outcome.result.recovery.stop, 'round_limit');
  assert.equal(outcome.result.reason,
    'review incomplete (completion=incomplete, omitted_paths=1, reason=nonempty) Context recovery round limit reached.');
  assert.equal(outcome.result.recovery_diagnostics.at(-1).reason, 'Final request must not replace the original.');
});

test('file exhaustion and both byte exhaustion paths each allow one final round', async t => {
  for (const limit of ['file', 'byte', 'collector-byte']) await t.test(limit, async () => {
    let reads = 0, collectorCalls = 0;
    const collector = new GitHub({ repository: 'example/repo', readToken: 'test-token', fetcher: async () => {
      collectorCalls++;
      return { ok: true, json: async () => ({ tree: [{ path: 'src', type: 'tree', mode: '040000', sha: base },
        { path: 'dependency-1.js', type: 'blob', mode: '100644', sha: head, size: recoveryLimits.bytes + 1 }] }) };
    } });
    const outcome = await overWire((input, call) => wire(input, input.context_recovery === 'exhausted' ? [] :
      limit === 'file' && call === 1 ? Array.from({ length: recoveryLimits.files }, (_, n) => request(n)) : [request(100 + call)]), {
      github: { current: async () => true, source: async (_path, sha, optional, options) => {
        reads++;
        if (limit === 'collector-byte') return collector.source('dependency-1.js', sha, optional, options);
        return limit === 'byte' ? 'x'.repeat(recoveryLimits.bytes + 1) : 'dependency\n';
      } },
    });
    assert.equal(outcome.result.status, 'complete');
    assert.equal(outcome.result.recovery.stop, limit === 'file' ? 'file_limit_finalized' : 'byte_limit_finalized');
    assert.equal(outcome.posts.length, limit === 'file' ? 3 : 2);
    assert.equal(reads, limit === 'file' ? recoveryLimits.files : 1);
    assert.equal(outcome.input.context.length, limit === 'file' ? recoveryLimits.files : 0);
    if (limit === 'collector-byte') {
      assert.equal(collectorCalls, 1); // Metadata refusal, without downloading the blob.
      assert.equal(outcome.result.recovery_sources[0].status, 'unavailable');
    }
  });
});

test('insufficient remaining deadline prevents the final round at every budget boundary', async t => {
  for (const limit of ['round', 'file', 'byte']) await t.test(limit, async t => {
    let now = 1000, calls = 0;
    t.mock.method(Date, 'now', () => now);
    const outcome = await run({ deadlineMs: limit === 'round' ? 1450 : 1250,
      github: { current: async () => true, source: async () => {
        if (limit === 'byte') { now += 60; return 'x'.repeat(recoveryLimits.bytes + 1); }
        return 'dependency\n';
      } }, execute: async input => {
        calls++; now += 100;
        return parsed(input, limit === 'file' && calls === 1
          ? Array.from({ length: recoveryLimits.files }, (_, n) => request(n)) : [request(100 + calls)]);
      } });
    assert.equal(calls, limit === 'round' ? 4 : limit === 'file' ? 2 : 1);
    assert.equal(outcome.input.context_recovery, undefined);
    assert.equal(outcome.result.recovery.stop, 'deadline');
    assert.match(outcome.result.reason, /Insufficient review deadline remaining/);
  });
});

test('excluded stops never trigger a final inference', async t => {
  for (const cause of ['deadline', 'head_unverified', 'source_unavailable', 'invalid_request']) {
    await t.test(cause, async () => {
      let calls = 0, reads = 0;
      const outcome = await run({ deadlineMs: cause === 'deadline' ? Date.now() - 1 : Infinity,
        github: { current: async () => cause !== 'head_unverified', source: async () => {
          reads++;
          if (cause === 'source_unavailable') throw new Error('Source transport failed.');
          return 'dependency\n';
        } }, execute: async input => {
          calls++;
          const result = parsed(input, [{ path: cause === 'no_new_context' ? 'src/change.js' : 'src/dependency.js', side: 'head' }]);
          // Exercise recovery's own guard; the wire parser independently refuses invalid paths.
          return cause === 'invalid_request' ? { ...result, context_requests: [{ path: '../outside.js', side: 'head' }] } : result;
        } });
      assert.equal(outcome.result.recovery.stop, cause);
      assert.equal(calls, cause === 'deadline' ? 0 : 1);
      assert.equal(reads, cause === 'source_unavailable' ? 1 : 0);
      assert.equal(outcome.input.context_recovery, undefined);
    });
  }
});

test('the final wire marker is digested and ordinary pinned-result validation still applies', async t => {
  const outcome = await overWire((input, call) => wire(input,
    input.context_recovery === 'exhausted' ? [] : [request(call)]));
  const previous = outcome.posts.at(-2).input, final = outcome.posts.at(-1).input;
  assert.equal(previous.context_recovery, undefined);
  assert.equal(final.context_recovery, 'exhausted');
  assert.deepEqual(final.files, previous.files);
  assert.deepEqual(final.context, previous.context);
  assert.notEqual(final.input_digest, previous.input_digest);
  assert.equal(createReviewInput(final).input_digest, final.input_digest);
  const { context_recovery, ...unmarked } = final;
  assert.equal(createReviewInput(unmarked).input_digest, previous.input_digest);
  assert.equal(outcome.result.input_digest, final.input_digest);
  for (const invalid of ['stale-digest', 'omitted-path']) await t.test(invalid, async () => {
    let calls = 0, previous;
    const rejected = await run({ execute: async input => {
      calls++;
      if (input.context_recovery !== 'exhausted') { previous = input; return parsed(input, [request(calls)]); }
      return invalid === 'stale-digest' ? parsed(previous, []) : {
        ...parsed(input, []), omitted_paths: ['src/change.js'],
      };
    } });
    assert.equal(calls, 5);
    assert.equal(rejected.result.status, 'unavailable');
    assert.equal(rejected.result.recovery.stop, 'round_limit');
    assert.match(rejected.result.reason, /Review engine could not return a valid pinned result\. Final verdict unavailable\. Context recovery round limit reached\./);
  });
});

test('every recovery budget boundary preserves a grounded final defect or explicitly fails an incomplete verdict', async t => {
  for (const limit of ['round', 'file', 'byte']) for (const verdict of ['defect', 'incomplete']) await t.test(`${limit}/${verdict}`, async () => {
    const outcome = await overWire((input, call) => {
      if (input.context_recovery !== 'exhausted') return wire(input, limit === 'file' && call === 1
        ? Array.from({ length: recoveryLimits.files }, (_, n) => request(n)) : [request(100 + call)]);
      if (verdict === 'incomplete') return wire(input, [request(999)]);
      return { ...wire(input), findings: [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1,
        severity: 'high', title: 'The new value breaks the contract', explanation: 'The supplied contract requires the original value.',
        trigger: 'A consumer reads this value.', consequence: 'The consumer rejects the new value.',
        evidence: [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1,
          start_byte: null, end_byte: null, quote: 'after' }] }] };
    }, { github: { current: async () => true, source: async () => limit === 'byte'
      ? 'x'.repeat(recoveryLimits.bytes + 1) : 'dependency\n' } });
    assert.equal(outcome.posts.length, limit === 'round' ? 5 : limit === 'file' ? 3 : 2);
    assert.equal(outcome.result.status, verdict === 'defect' ? 'complete' : 'incomplete');
    assert.equal(outcome.result.terminal_incomplete === true, verdict === 'incomplete');
    assert.equal(outcome.result.findings.length, verdict === 'defect' ? 1 : 0);
    assert.equal(outcome.result.recovery.stop, `${limit}_limit${verdict === 'defect' ? '_finalized' : ''}`);
  });
});
