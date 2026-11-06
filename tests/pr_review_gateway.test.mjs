import test from 'node:test';
import assert from 'node:assert/strict';
import { gatewayReviewer as actualGatewayReviewer } from '../.github/actions/pr-review/gateway.mjs';
import { createReviewInput, parseReviewResult, reviewerVersion } from '../.github/actions/pr-review/contract.mjs';
import { bundleFile, GitHub } from '../.github/actions/pr-review/github.mjs';
import { credentials } from '../.github/actions/pr-review/auth.mjs';
import { eventTargets } from '../.github/actions/pr-review/events.mjs';
import { createHash } from 'node:crypto';

function inputFor(before = 'return value\n', after = 'return undefined\n') {
  return createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
    base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: '1',
    trigger: { kind: 'workflow_run', id: '42', generation: '1' },
    files: [bundleFile({ filename: 'src/value.js', status: after === null ? 'removed' : 'modified' }, before, after)], context: [] });
}
function wire(input, findings = []) {
  return { schema_version: 1, repository_id: input.repository_id, base_sha: input.base_sha,
    head_sha: input.head_sha, input_digest: input.input_digest, reviewer_version: reviewerVersion,
    policy_version: input.policy_version, completion: 'complete', findings, omitted_paths: [], context_notes: [], context_requests: [], reason: '',
    review_summary: 'The return value behavior was reviewed.', related_evidence: [], binary_reviews: [] };
}
const finding = { path: 'src/value.js', side: 'head', start_line: 1, end_line: 1, severity: 'medium',
  title: 'The value is discarded', explanation: 'The return value is always undefined.', trigger: 'A caller supplies a value.',
  consequence: 'The caller loses that value.', evidence: [{ path: 'src/value.js', side: 'head', start_line: 1, end_line: 1,
    start_byte: null, end_byte: null, quote: 'return undefined' }] };
const reply = (input, changes = {}) => ({ object: 'chat.completion', model: 'vendor/reviewer', choices: [{
  finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(wire(input, [finding])) },
}], ...changes });
const gatewayReviewer = options => actualGatewayReviewer({ ...options, fetcher: async (url, request) => request.method === 'GET' ? {
  ok: true, status: 200, json: async () => ({ id: options.model, context_window: 1_050_000, max_output_tokens: 128_000 }),
} : options.fetcher(url, request) });

// Strict structured output requires a type at every schema node, including
// enum/const leaves. Ordinary JSON Schema validators allow those leaves to
// omit a type, so validating a returned review alone misses this refusal.
function assertTypedSchema(schema, path = '$') {
  assert.ok(['object', 'array', 'string', 'integer'].includes(schema.type) ||
    JSON.stringify(schema.type) === JSON.stringify(['integer', 'null']), `explicit type at ${path}`);
  if (schema.type === 'object') {
    assert.equal(schema.additionalProperties, false, path);
    assert.deepEqual(schema.required, Object.keys(schema.properties), path);
    for (const [name, child] of Object.entries(schema.properties)) assertTypedSchema(child, `${path}.${name}`);
  }
  if (schema.type === 'array') assertTypedSchema(schema.items, `${path}[]`);
}

test('outbound strict review schema includes explicit scalar types at every depth', async () => {
  const input = inputFor(); let format;
  const review = gatewayReviewer({ token: 'private-value', model: 'vendor/reviewer', deadlineMs: Date.now() + 5000,
    fetcher: async (_url, request) => {
      format = JSON.parse(request.body).response_format;
      return { ok: true, status: 200, json: async () => reply(input) };
    } });
  assert.equal((await review(input)).status, 'complete');
  assert.equal(format.type, 'json_schema');
  assert.equal(format.json_schema.strict, true);
  assertTypedSchema(format.json_schema.schema);
});

test('typed discriminators retain exact version, completion, side and severity refusals', () => {
  const input = inputFor();
  for (const value of [{ ...wire(input), schema_version: 2 }, { ...wire(input), completion: 'other' },
    wire(input, [{ ...finding, side: 'context' }]), wire(input, [{ ...finding, severity: 'other' }]),
    wire(input, [{ ...finding, evidence: [{ ...finding.evidence[0], side: 'other' }] }])]) {
    assert.equal(parseReviewResult(JSON.stringify(value), input).status, 'unavailable');
  }
  assert.equal(parseReviewResult(JSON.stringify(wire(input, [finding])), input).status, 'complete');
});

test('direct gateway sends the strict bound schema without tools and validates concrete results', async () => {
  const input = inputFor(); let requests = 0;
  const review = gatewayReviewer({ token: 'private-value', model: 'vendor/reviewer', deadlineMs: Date.now() + 5000,
    fetcher: async (url, request) => {
      requests++;
      assert.equal(String(url), 'https://api.sylphx.com/v1/chat/completions');
      assert.equal(request.headers.authorization, 'Bearer private-value');
      const body = JSON.parse(request.body);
      assert.equal(body.store, false); assert.equal(body.stream, false);
      assert.equal(body.response_format.json_schema.strict, true);
      assert.equal(body.tools, undefined);
      assert.equal(JSON.parse(body.messages[1].content).head_sha, input.head_sha);
      return { ok: true, status: 200, json: async () => reply(input) };
    } });
  assert.equal((await review(input)).status, 'complete'); assert.equal(requests, 1);
});

test('truncation, refusal, wrong model, multiple choices and malformed output cannot pass', async () => {
  const input = inputFor();
  for (const body of [reply(input, { model: 'other/model' }), reply(input, { choices: [] }),
    reply(input, { choices: [{ finish_reason: 'length', message: { role: 'assistant', content: JSON.stringify(wire(input)) } }] }),
    reply(input, { choices: [{ finish_reason: 'stop', message: { role: 'assistant', refusal: 'refused', content: '{}' } }] }),
    reply(input, { choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: '{}' } }] })]) {
    const review = gatewayReviewer({ token: 'private-value', model: 'vendor/reviewer', deadlineMs: Date.now() + 5000,
      fetcher: async () => ({ ok: true, status: 200, json: async () => body }) });
    assert.equal((await review(input)).status, 'unavailable');
  }
});

test('quota and transport failures are explicit, secret-safe and never retried', async () => {
  const input = inputFor(); let requests = 0;
  const review = gatewayReviewer({ token: 'private-value', model: 'vendor/reviewer', deadlineMs: Date.now() + 5000,
    fetcher: async () => { requests++; return { ok: false, status: 429, json: async () => ({ error: {
      code: 'included_capacity_unavailable', message: 'private-value',
    } }) }; } });
  const result = await review(input);
  assert.equal(requests, 1); assert.match(result.reason, /HTTP 429 \(included_capacity_unavailable\)/);
  assert.ok(!JSON.stringify(result).includes('private-value'));
  const failed = gatewayReviewer({ token: 'private-value', model: 'vendor/reviewer', deadlineMs: Date.now() + 5000,
    fetcher: async () => { throw new Error('private-value'); } });
  assert.equal((await failed(input)).reason, 'Review inference failed at the network.');
  const streamDeadline = Date.now() + 1000;
  const now = Date.now;
  try {
    let fakeNow = streamDeadline - 500;
    Date.now = () => fakeNow;
    const abortedStream = gatewayReviewer({ token: 'private-value', model: 'vendor/reviewer', deadlineMs: streamDeadline,
      fetcher: async () => ({ ok: true, status: 200, json: async () => { fakeNow = streamDeadline + 10; throw new Error('TimeoutError: The operation was aborted due to timeout'); } }) });
    assert.equal((await abortedStream(input)).reason, 'Review inference exceeded its deadline.');
  } finally { Date.now = now; }
});

test('schema rejects source/version drift, fabricated quotes, omissions and extra fields', () => {
  const input = inputFor();
  for (const value of [{ ...wire(input), head_sha: input.base_sha }, { ...wire(input), input_digest: 'other' },
    { ...wire(input), reviewer_version: 'other' }, { ...wire(input), omitted_paths: ['src/value.js'] },
    { ...wire(input), extra: true }, wire(input, [{ ...finding, evidence: [{ ...finding.evidence[0], quote: 'invented' }] }])]) {
    assert.equal(parseReviewResult(JSON.stringify(value), input).status, 'unavailable');
  }
  assert.equal(parseReviewResult(JSON.stringify(wire(input)), input).status, 'complete');
});

test('incomplete diagnostics retain bounded model detail privately and whitelist omitted paths', () => {
  const input = inputFor(), privateText = 'private-value\n::warning::untrusted output';
  const changes = { completion: 'incomplete', omitted_paths: ['src/value.js'], reason: privateText,
    context_notes: [privateText], context_requests: [{ path: 'src/caller.js', side: 'head' }] };
  const result = parseReviewResult(JSON.stringify({ ...wire(input, [finding]), ...changes }), input);
  assert.equal(result.status, 'incomplete');
  assert.equal(result.reason, 'review incomplete (completion=incomplete, omitted_paths=1, reason=nonempty)');
  assert.deepEqual(result.findings, [finding]);
  assert.deepEqual(result.omitted_paths, ['src/value.js']);
  assert.deepEqual(result.context_requests, changes.context_requests);
  assert.equal(result.context_notes, undefined);
  assert.equal(result.diagnostics.reason, privateText);
  assert.deepEqual(result.diagnostics.context_notes, [privateText]);
  const { diagnostics, ...publicFields } = result;
  assert.ok(!JSON.stringify(publicFields).includes(privateText));
  const bounded = parseReviewResult(JSON.stringify({ ...wire(input), ...changes,
    reason: 'x'.repeat(10_000), context_notes: Array(100).fill('y'.repeat(2000)) }), input);
  assert.equal(bounded.diagnostics.reason.length, 4096);
  assert.equal(bounded.diagnostics.context_notes.length, 16);
  assert.equal(bounded.diagnostics.context_notes[0].length, 1024);
  for (const change of [{ omitted_paths: [privateText] }, { context_requests: [{ path: '../outside', side: 'head' }] },
    { context_requests: [{ path: 'https://example.com/file', side: 'other' }] }, { head_sha: input.base_sha }]) {
    const rejected = parseReviewResult(JSON.stringify({ ...wire(input), ...changes, ...change }), input);
    assert.equal(rejected.status, 'unavailable');
    const { diagnostics, ...safe } = rejected;
    assert.ok(!JSON.stringify(safe).includes(privateText));
  }
  for (const change of [{ reason: privateText }, { omitted_paths: ['src/value.js'] },
    { context_requests: [{ path: 'src/caller.js', side: 'head' }] }]) {
    assert.equal(parseReviewResult(JSON.stringify({ ...wire(input), ...change }), input).status, 'unavailable');
  }
  const stale = parseReviewResult(JSON.stringify({ ...wire(input), ...changes, input_digest: 'wrong' }), input);
  assert.equal(stale.diagnostics, undefined);
});

test('deleted code and absolute windows are reviewable without invented head lines', () => {
  const deleted = inputFor('return value\n', null);
  const baseFinding = { ...finding, side: 'base', evidence: [{ ...finding.evidence[0], side: 'base', quote: 'return value' }] };
  assert.equal(parseReviewResult(JSON.stringify(wire(deleted, [baseFinding])), deleted).status, 'complete');
  const before = Array.from({ length: 5000 }, (_, i) => `row ${i}`).join('\n');
  const input = inputFor(before, before.replace('row 4000', 'changed 4000'));
  const at4001 = { ...finding, start_line: 4001, end_line: 4001,
    evidence: [{ ...finding.evidence[0], start_line: 4001, end_line: 4001, quote: 'changed 4000' }] };
  assert.equal(input.files[0].source_mode, 'segments');
  assert.equal(parseReviewResult(JSON.stringify(wire(input, [at4001])), input).status, 'complete');
});

test('same finite Access key becomes ready before the only narrowed Broker exchange', async () => {
  const calls = [], masks = [];
  const auth = await credentials({ ciName: 'review', trustPolicy: 'orgs/example/trust_policies/review', repository: 'example/repo',
    env: { ACTIONS_ID_TOKEN_REQUEST_URL: 'https://example.com/oidc?x=1', ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'oidc-authority' },
    mask: value => masks.push(value), ready: issued => { assert.equal(issued.api_key, 'inference'); calls.push('ready'); },
    fetcher: async (url, request) => {
      const path = new URL(url).pathname; calls.push(path);
      let body;
      if (path === '/oidc') body = { value: 'identity' };
      else if (path === '/v1/access/github/token') body = { api_key: 'inference' };
      else { assert.equal(calls.at(-2), 'ready'); assert.equal(request.headers.authorization, 'Bearer inference');
        assert.deepEqual(JSON.parse(request.body), { repositories: ['example/repo'], permissions: { checks: 'write' } });
        body = { token: 'checks', issuer: 'github-apps/1299545', credential_kind: 'access_token',
          repositories: ['example/repo'], permissions: { checks: 'write' } }; }
      return { ok: true, status: 200, json: async () => body };
    } });
  assert.deepEqual(auth, { inferenceToken: 'inference', checkToken: 'checks' });
  assert.equal(calls.length, 4); assert.equal(masks.length, 3);
});

test('readiness refusal never submits a Broker request', async () => {
  let calls = 0;
  await assert.rejects(() => credentials({ ciName: 'review', trustPolicy: 'orgs/example/trust_policies/review', repository: 'example/repo',
    env: { ACTIONS_ID_TOKEN_REQUEST_URL: 'https://example.com/oidc', ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'authority' }, mask: () => {},
    ready: () => { throw new Error('not ready'); }, fetcher: async () => { calls++; return {
      ok: true, status: 200, json: async () => calls === 1 ? { value: 'identity' } : { api_key: 'inference' },
    }; } }), /not ready/);
  assert.equal(calls, 2);
});

test('Broker exchange retries only key_not_yet_propagated within the propagation deadline', async () => {
  let nowMs = 1_000, brokerCalls = 0;
  const sleeps = [];
  const auth = await credentials({
    ciName: 'review', trustPolicy: 'orgs/example/trust_policies/review', repository: 'example/repo',
    env: { ACTIONS_ID_TOKEN_REQUEST_URL: 'https://example.com/oidc', ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'authority' },
    mask: () => {}, ready: () => {}, now: () => nowMs,
    sleep: async ms => { sleeps.push(ms); nowMs += ms; },
    fetcher: async url => {
      const path = new URL(url).pathname;
      if (path === '/oidc') return { ok: true, status: 200, json: async () => ({ value: 'identity' }) };
      if (path === '/v1/access/github/token') return { ok: true, status: 200, json: async () => ({ api_key: 'inference', active_after_ms: 3000 }) };
      brokerCalls++;
      if (brokerCalls < 3) {
        const code = brokerCalls === 1 ? 'key_not_yet_propagated' : 'NOT_YET_PROPAGATED';
        return { ok: false, status: 401, json: async () => ({ code, error: 'API key not yet active' }) };
      }
      return {
        ok: true, status: 200,
        json: async () => ({
          token: 'checks', issuer: 'github-apps/1299545', credential_kind: 'access_token',
          repositories: ['example/repo'], permissions: { checks: 'write' },
        }),
      };
    },
  });
  assert.deepEqual(auth, { inferenceToken: 'inference', checkToken: 'checks' });
  assert.equal(brokerCalls, 3);
  assert.deepEqual(sleeps, [2000, 2000]);

  let unauthCalls = 0;
  await assert.rejects(() => credentials({
    ciName: 'review', trustPolicy: 'orgs/example/trust_policies/review', repository: 'example/repo',
    env: { ACTIONS_ID_TOKEN_REQUEST_URL: 'https://example.com/oidc', ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'authority' },
    mask: () => {}, ready: () => {}, now: () => 0, sleep: async () => { throw new Error('must not sleep'); },
    fetcher: async url => {
      const path = new URL(url).pathname;
      if (path === '/oidc') return { ok: true, status: 200, json: async () => ({ value: 'identity' }) };
      if (path === '/v1/access/github/token') return { ok: true, status: 200, json: async () => ({ api_key: 'inference', active_after_ms: 3000 }) };
      unauthCalls++;
      return { ok: false, status: 401, json: async () => ({ code: 'UNAUTHENTICATED' }) };
    },
  }), /Broker check-token exchange returned HTTP 401/);
  assert.equal(unauthCalls, 1);

  let expiredNow = 0, expiredCalls = 0;
  await assert.rejects(() => credentials({
    ciName: 'review', trustPolicy: 'orgs/example/trust_policies/review', repository: 'example/repo',
    env: { ACTIONS_ID_TOKEN_REQUEST_URL: 'https://example.com/oidc', ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'authority' },
    mask: () => {}, ready: () => {}, now: () => expiredNow,
    sleep: async ms => { expiredNow += ms; },
    fetcher: async url => {
      const path = new URL(url).pathname;
      if (path === '/oidc') return { ok: true, status: 200, json: async () => ({ value: 'identity' }) };
      if (path === '/v1/access/github/token') return { ok: true, status: 200, json: async () => ({ api_key: 'inference', active_after_ms: 4000 }) };
      expiredCalls++;
      return { ok: false, status: 401, json: async () => ({ code: 'key_not_yet_propagated' }) };
    },
  }), /Broker check-token exchange returned HTTP 401/);
  assert.equal(expiredNow, 184_000);
  assert.equal(expiredCalls, 92);
});

test('trusted event resolves fork-safe API associations and ignores unrelated runs', async () => {
  const head = 'b'.repeat(40), base = 'a'.repeat(40);
  const github = { repository: 'example/repo', current: async () => true,
    pages: async path => path.includes('/commits/') ? [{ number: 7, state: 'open', head: { sha: head }, base: { repo: { id: 1 } } }] : [{ filename: 'src/value.js' }],
    request: async path => path.includes('/compare/') ? { merge_base_commit: { sha: base } } : {
      state: 'open', changed_files: 1, head: { sha: head }, base: { sha: base, ref: 'main', repo: { id: 1 } },
    } };
  const event = { repository: { full_name: 'example/repo', id: 1 }, workflow_run: { event: 'pull_request', head_sha: head, id: 42, pull_requests: [] } };
  const targets = await eventTargets(github, 'workflow_run', event, '12');
  assert.equal(targets[0].target.number, 7); assert.equal(targets[0].target.head_sha, head);
  assert.deepEqual(await eventTargets(github, 'workflow_run', { ...event, workflow_run: { ...event.workflow_run, event: 'push' } }, '13'), []);
});

test('source provenance preserves BOM and refuses followed links and mismatched blobs', async () => {
  const bytes = Buffer.from('\uFEFFsource\n');
  const sha = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
  const github = new GitHub({ repository: 'example/repo', readToken: 'read' });
  let mode = '100644', corrupted = false;
  github.request = async path => path.includes('/git/trees/') ? { tree: [{ path: 'file.js', mode, type: 'blob', sha }] } : {
    sha, encoding: 'base64', size: bytes.length, content: (corrupted ? Buffer.from('forged\n') : bytes).toString('base64'),
  };
  assert.equal(await github.source('file.js', 'a'.repeat(40)), '\uFEFFsource\n');
  assert.equal(await github.source('absent', 'a'.repeat(40), true), null);
  mode = '120000'; github.sourceTrees.clear();
  await assert.rejects(() => github.source('file.js', 'a'.repeat(40)), /ordinary file/);
  mode = '100644'; corrupted = true; github.sourceTrees.clear();
  await assert.rejects(() => github.source('file.js', 'a'.repeat(40)), /incomplete/);
});

test('CRLF source uses absolute lines with canonical evidence line endings', () => {
  const input = inputFor('return value\r\n', 'return undefined\r\n');
  assert.equal(parseReviewResult(JSON.stringify(wire(input, [finding])), input).status, 'complete');
  const before = Array.from({ length: 5000 }, (_, i) => `row ${i}`).join('\r\n');
  const windows = inputFor(before, before.replace('row 4000', 'changed 4000'));
  const changed = { ...finding, start_line: 4001, end_line: 4001,
    evidence: [{ ...finding.evidence[0], start_line: 4001, end_line: 4001, quote: 'changed 4000' }] };
  assert.equal(parseReviewResult(JSON.stringify(wire(windows, [changed])), windows).status, 'complete');
});

test('failed evidence receipts distinguish quote mismatch from absent source without retaining either quote', () => {
  const input = inputFor(), canary = 'PRIVATE_QUOTE_漢字\r\n::warning::model prose';
  const fingerprint = value => ({ bytes: Buffer.byteLength(value), sha256: createHash('sha256').update(value).digest('hex') });
  for (const kind of ['finding_evidence', 'related_evidence']) {
    const evidence = { ...finding.evidence[0], quote: canary };
    const result = parseReviewResult(JSON.stringify(kind === 'finding_evidence' ?
      wire(input, [{ ...finding, evidence: [evidence] }]) : { ...wire(input), related_evidence: [evidence] }), input);
    assert.equal(result.status, 'unavailable');
    assert.equal(result.reason, kind === 'related_evidence' ? 'related evidence differs from supplied source' : 'evidence differs from supplied source');
    assert.deepEqual(result.validation_failure, { version: 1, code: 'quote_mismatch', input_digest: input.input_digest,
      kind, evidence_index: 0, finding_index: kind === 'finding_evidence' ? 0 : null,
      path: 'src/value.js', side: 'head', start_line: 1, end_line: 1, start_byte: null, end_byte: null,
      expected: fingerprint('return undefined'), returned: fingerprint(canary) });
    assert.ok(!JSON.stringify(result).includes(canary));
    const absent = parseReviewResult(JSON.stringify({ ...wire(input), related_evidence: [{ ...evidence, start_line: 300, end_line: 300 }] }), input);
    assert.equal(absent.validation_failure.code, 'source_unavailable');
    assert.equal(absent.validation_failure.expected, null);
    assert.equal(absent.validation_failure.start_line, 300);
  }
  assert.equal(parseReviewResult(JSON.stringify(wire(input, [finding])), input).validation_failure, undefined);
});

test('evidence metadata never emits unknown paths or stale and malformed model data', () => {
  const input = inputFor(), canary = 'PRIVATE_UNKNOWN_PATH';
  const forged = { ...wire(input), related_evidence: [{ ...finding.evidence[0], path: canary, quote: canary }] };
  const result = parseReviewResult(JSON.stringify(forged), input);
  assert.equal(result.status, 'unavailable'); assert.equal(result.validation_failure.path, null);
  assert.equal(result.validation_failure.code, 'source_unavailable');
  assert.ok(!JSON.stringify(result).includes(canary));
  assert.ok(Buffer.byteLength(JSON.stringify(result.validation_failure)) < 2048);
  for (const change of [{ head_sha: input.base_sha }, { input_digest: 'stale' }, { extra: canary },
    { related_evidence: [{ ...finding.evidence[0], start_byte: -1 }] }]) {
    assert.equal(parseReviewResult(JSON.stringify({ ...forged, ...change }), input).validation_failure, undefined);
  }
});

test('byte-qualified evidence fingerprints exact UTF8 and CRLF without normalizing the validator', () => {
  const input = inputFor('old\r\n', '漢字🐉\r\n'), source = '漢字🐉\r\n';
  const evidence = { ...finding.evidence[0], start_byte: 0, end_byte: Buffer.byteLength(source), quote: source };
  assert.equal(parseReviewResult(JSON.stringify({ ...wire(input), related_evidence: [evidence] }), input).status, 'complete');
  const result = parseReviewResult(JSON.stringify({ ...wire(input), related_evidence: [{ ...evidence, quote: source.replace('\r', '') }] }), input);
  assert.equal(result.status, 'unavailable'); assert.equal(result.validation_failure.code, 'quote_mismatch');
  assert.equal(result.validation_failure.expected.bytes, Buffer.byteLength(source));
  assert.equal(result.validation_failure.expected.sha256, createHash('sha256').update(source).digest('hex'));
  assert.equal(result.validation_failure.end_byte, Buffer.byteLength(source));
});


test('known renamed and context identities are located but unsafe supplied path spellings remain masked', () => {
  const bundle = { schema_version: 2, repository: 'example/repo', repository_id: '1', base_sha: 'a'.repeat(40),
    head_sha: 'b'.repeat(40), policy_version: '1', trigger: { kind: 'workflow_run', id: '42', generation: '1' },
    files: [bundleFile({ filename: 'src/new.js', previous_filename: 'src/old.js', status: 'renamed' }, 'same\n', 'same\n')],
    context: [{ path: 'caller.js', content: 'caller\n' }, { path: 'unsafe::notice.js', content: 'caller\n' }] };
  const input = createReviewInput(bundle);
  for (const [path, side, knownPath] of [['src/old.js', 'base', 'src/old.js'], ['caller.js', 'context', 'caller.js'],
    ['unsafe::notice.js', 'context', null]]) {
    const result = parseReviewResult(JSON.stringify({ ...wire(input), related_evidence: [{ path, side,
      start_line: 1, end_line: 1, start_byte: null, end_byte: null, quote: 'different' }] }), input);
    assert.equal(result.status, 'unavailable'); assert.equal(result.validation_failure.code, 'quote_mismatch');
    assert.equal(result.validation_failure.path, knownPath);
    if (knownPath === null) assert.ok(!JSON.stringify(result.validation_failure).includes(path));
  }
});

test('line-qualified evidence recovers unique verbatim supplied lines, records snapped ranges, and refuses ambiguous or non-verbatim quotes', () => {
  const head = ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'duplicate', 'duplicate', ...Array.from({ length: 16 }, (_, i) => `line_${i}`), 'omega'].join('\n') + '\n';
  const input = inputFor('old\n', head);
  const shifted = parseReviewResult(JSON.stringify({ ...wire(input), related_evidence: [{ path: 'src/value.js', side: 'head',
    start_line: 1, end_line: 2, start_byte: null, end_byte: null, quote: 'epsilon\nduplicate' }] }), input);
  assert.equal(shifted.status, 'complete');
  assert.equal(shifted.related_evidence[0].start_line, 5);
  assert.equal(shifted.related_evidence[0].end_line, 6);
  assert.deepEqual(shifted.snapped_evidence, [{ kind: 'related_evidence', evidence_index: 0, finding_index: null,
    path: 'src/value.js', side: 'head', from_start_line: 1, from_end_line: 2, start_line: 5, end_line: 6 }]);
  const halfOpen = parseReviewResult(JSON.stringify({ ...wire(input), related_evidence: [{ path: 'src/value.js', side: 'head',
    start_line: 4, end_line: 6, start_byte: null, end_byte: null, quote: 'delta\nepsilon' }] }), input);
  assert.equal(halfOpen.status, 'complete');
  assert.equal(halfOpen.related_evidence[0].start_line, 4);
  assert.equal(halfOpen.related_evidence[0].end_line, 5);
  const farUnique = parseReviewResult(JSON.stringify({ ...wire(input), related_evidence: [{ path: 'src/value.js', side: 'head',
    start_line: 7, end_line: 7, start_byte: null, end_byte: null, quote: 'omega' }] }), input);
  assert.equal(farUnique.status, 'complete');
  assert.equal(farUnique.related_evidence[0].start_line, 24);
  assert.equal(farUnique.related_evidence[0].end_line, 24);
  assert.equal(farUnique.snapped_evidence[0].from_start_line, 7);
  assert.equal(farUnique.snapped_evidence[0].start_line, 24);
  const ambiguous = parseReviewResult(JSON.stringify({ ...wire(input), related_evidence: [{ path: 'src/value.js', side: 'head',
    start_line: 5, end_line: 5, start_byte: null, end_byte: null, quote: 'duplicate' }] }), input);
  assert.equal(ambiguous.status, 'unavailable');
  assert.equal(ambiguous.validation_failure.code, 'quote_mismatch');
  const nonVerbatim = parseReviewResult(JSON.stringify({ ...wire(input), related_evidence: [{ path: 'src/value.js', side: 'head',
    start_line: 1, end_line: 1, start_byte: null, end_byte: null, quote: 'omega_missing' }] }), input);
  assert.equal(nonVerbatim.status, 'unavailable');
  assert.equal(nonVerbatim.validation_failure.code, 'quote_mismatch');
});

test('gateway retries transient HTTP 200 invalid JSON per part within the deadline', async () => {
  const input = inputFor(); let attempts = 0;
  const review = gatewayReviewer({ token: 'private-value', model: 'vendor/reviewer', deadlineMs: Date.now() + 5000,
    fetcher: async () => {
      attempts++;
      if (attempts === 1) return { ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected end of JSON input'); } };
      if (attempts === 2) return { ok: true, status: 200, json: async () => ({ object: 'chat.completion', model: 'vendor/reviewer',
        choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: '{truncated' } }] }) };
      return { ok: true, status: 200, json: async () => reply(input) };
    } });
  const result = await review(input);
  assert.equal(result.status, 'complete');
  assert.equal(attempts, 3);
});
