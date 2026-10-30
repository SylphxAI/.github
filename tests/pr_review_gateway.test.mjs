import test from 'node:test';
import assert from 'node:assert/strict';
import { gatewayReviewer } from '../.github/actions/pr-review/gateway.mjs';
import { createReviewInput, parseReviewResult, reviewerVersion } from '../.github/actions/pr-review/contract.mjs';
import { bundleFile, GitHub } from '../.github/actions/pr-review/github.mjs';
import { credentials } from '../.github/actions/pr-review/auth.mjs';
import { eventTargets } from '../.github/actions/pr-review/events.mjs';
import { createHash } from 'node:crypto';

function inputFor(before = 'return value\n', after = 'return undefined\n') {
  return createReviewInput({ schema_version: 1, repository: 'example/repo', repository_id: '1',
    base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: '1',
    trigger: { kind: 'workflow_run', id: '42', generation: '1' },
    files: [bundleFile({ filename: 'src/value.js', status: after === null ? 'removed' : 'modified' }, before, after)], context: [] });
}
function wire(input, findings = []) {
  return { schema_version: 1, repository_id: input.repository_id, base_sha: input.base_sha,
    head_sha: input.head_sha, input_digest: input.input_digest, reviewer_version: reviewerVersion,
    policy_version: input.policy_version, completion: 'complete', findings, omitted_paths: [], context_notes: [], reason: '' };
}
const finding = { path: 'src/value.js', side: 'head', start_line: 1, end_line: 1, severity: 'medium',
  title: 'The value is discarded', explanation: 'The return value is always undefined.', trigger: 'A caller supplies a value.',
  consequence: 'The caller loses that value.', evidence: [{ path: 'src/value.js', side: 'head', start_line: 1, end_line: 1, quote: 'return undefined' }] };
const reply = (input, changes = {}) => ({ object: 'chat.completion', model: 'vendor/reviewer', choices: [{
  finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(wire(input, [finding])) },
}], ...changes });

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

test('deleted code and absolute windows are reviewable without invented head lines', () => {
  const deleted = inputFor('return value\n', null);
  const baseFinding = { ...finding, side: 'base', evidence: [{ ...finding.evidence[0], side: 'base', quote: 'return value' }] };
  assert.equal(parseReviewResult(JSON.stringify(wire(deleted, [baseFinding])), deleted).status, 'complete');
  const before = Array.from({ length: 5000 }, (_, i) => `row ${i}`).join('\n');
  const input = inputFor(before, before.replace('row 4000', 'changed 4000'));
  const at4001 = { ...finding, start_line: 4001, end_line: 4001,
    evidence: [{ ...finding.evidence[0], start_line: 4001, end_line: 4001, quote: 'changed 4000' }] };
  assert.equal(input.files[0].source_mode, 'windows');
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

test('source provenance reads the exact Git tree and refuses followed links and mismatched blobs', async () => {
  const bytes = Buffer.from('source\n');
  const sha = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
  const github = new GitHub({ repository: 'example/repo', readToken: 'read' });
  let mode = '100644', corrupted = false;
  github.request = async path => path.includes('/git/trees/') ? { tree: [{ path: 'file.js', mode, type: 'blob', sha }] } : {
    sha, encoding: 'base64', size: bytes.length, content: (corrupted ? Buffer.from('forged\n') : bytes).toString('base64'),
  };
  assert.equal(await github.source('file.js', 'a'.repeat(40)), 'source\n');
  assert.equal(await github.source('absent', 'a'.repeat(40), true), null);
  mode = '120000'; github.sourceTrees.clear();
  await assert.rejects(() => github.source('file.js', 'a'.repeat(40)), /ordinary file/);
  mode = '100644'; corrupted = true; github.sourceTrees.clear();
  await assert.rejects(() => github.source('file.js', 'a'.repeat(40)), /incomplete/);
});
