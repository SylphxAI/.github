import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { GitHub, GitHubHTTPError, PublishUnverified, githubFailureDetails, terminalCheckReceipt, APP_ID, CHECK_NAME } from '../.github/actions/pr-review/github.mjs';
import { collectPullRequest, produceReview } from '../.github/actions/pr-review/producer.mjs';
import { createReviewInput } from '../.github/actions/pr-review/contract.mjs';

// These are synthetic GitHub-shaped responses, not retained production headers.
const base = 'a'.repeat(40), head = 'b'.repeat(40), start = 1_000_000;
const path = `/commits/${head}/check-runs?check_name=private-query`;
const refuse = (status, headers = {}, message = 'untrusted prose private-source private-token') =>
  new Response(JSON.stringify({ message }), { status, headers });
const ok = body => new Response(JSON.stringify(body), { status: 200 });
function fixture(replies, options = {}) {
  let now = start;
  const calls = [], waits = [], diagnostics = [];
  const github = new GitHub({ repository: 'example/repo', readToken: 'private-token', checkToken: 'private-check-token',
    now: () => now, deadlineMs: start + 120_000, wait: async delay => { waits.push(delay); now += delay; },
    onDiagnostic: record => diagnostics.push(record), fetcher: async (url, request) => {
      calls.push({ url, request });
      if (!replies.length) throw new Error('unexpected wire request');
      const response = replies.shift();
      if (response instanceof Error) throw response;
      return response;
    }, ...options });
  return { github, calls, waits, diagnostics };
}

for (const entry of [
  { name: 'primary', status: 403, headers: { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1004', 'x-ratelimit-limit': '5000' }, classification: 'primary_rate_limit', wait: 5000 },
  { name: 'secondary Retry-After', status: 403, headers: { 'retry-after': '7', 'x-ratelimit-remaining': '4000' }, classification: 'secondary_rate_limit', wait: 7000 },
  { name: 'secondary body', status: 403, message: 'You have exceeded a secondary rate limit. private-source', classification: 'secondary_rate_limit', wait: 60_000 },
  { name: '429 without headers', status: 429, classification: 'secondary_rate_limit', wait: 60_000 },
  { name: '429 primary', status: 429, headers: { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1002' }, classification: 'primary_rate_limit', wait: 3000 },
  { name: 'Retry-After longer than valid primary reset', status: 403, headers: { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1003', 'retry-after': '12' }, classification: 'primary_rate_limit', wait: 12000 },
  { name: 'both provider waits', status: 403, headers: { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1008', 'retry-after': '3' }, classification: 'primary_rate_limit', wait: 9000 },
]) test(`${entry.name} retries only the refused request once after its provider wait`, async () => {
  const f = fixture([refuse(entry.status, entry.headers, entry.message), ok({ check_runs: [] })]);
  assert.deepEqual(await f.github.checkRuns(head), []);
  assert.deepEqual(f.waits, [entry.wait]);
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0].url, f.calls[1].url);
  for (const call of f.calls) {
    assert.equal(call.request.headers.authorization, 'Bearer private-check-token');
    assert.equal(call.request.method, 'GET');
    assert.ok(call.request.signal instanceof AbortSignal);
  }
  assert.equal(f.diagnostics[0].classification, entry.classification);
  assert.equal(f.diagnostics[0].code, 'rate_limited');
  assert.equal(f.diagnostics[0].endpoint, `/commits/${head}/check-runs`);
});

test('safe diagnostics retain only validated endpoint/status/header facts, never prose, URL or credentials', async () => {
  const f = fixture([refuse(403, { 'x-github-request-id': 'BD46:28F0D9:21E0E8:336334:6AEE901E',
    'x-ratelimit-remaining': '4000', 'x-ratelimit-reset': '1005', 'x-ratelimit-resource': 'core',
    'authorization': 'private-token', 'x-private': 'private-source', 'retry-after': 'private-token' })]);
  await assert.rejects(f.github.request('/pulls/7?secret=private-token'), error => {
    assert.ok(error instanceof GitHubHTTPError);
    assert.equal(error.code, 'rate_limited');
    assert.equal(error.classification, 'secondary_rate_limit');
    assert.deepEqual(error.details.headers, { 'x-ratelimit-remaining': 4000, 'x-ratelimit-reset': 1005,
      'x-ratelimit-resource': 'core', 'x-github-request-id': 'BD46:28F0D9:21E0E8:336334:6AEE901E' });
    for (const forbidden of ['private-token', 'private-source', 'private-query', 'untrusted prose', 'authorization', 'https://']) {
      assert.equal((error.message + JSON.stringify(f.diagnostics)).includes(forbidden), false);
    }
    return true;
  });
  assert.equal(f.calls.length, 1); assert.deepEqual(f.waits, []);
  const unsafe = fixture([refuse(403)]);
  await assert.rejects(unsafe.github.request('/private-token'), error => error.details.endpoint === '/<redacted>' && !error.message.includes('private-token'));
});

const permissionsAtLimit = [...Array(7).fill(`${'a'.repeat(64)}=admin`), `${'b'.repeat(10)}=read`].join(',');
assert.equal(permissionsAtLimit.length, 512);
for (const [name, value] of [
  ['single permission', 'checks=read'],
  ['comma and semicolon permission sets', 'pull_requests=write,contents=read;checks=admin'],
  ['horizontal separator whitespace', 'checks=read , contents=write;\tissues=admin'],
  ['one-character permission name', 'a=read'],
  ['64-character permission name', `${'a'.repeat(64)}=read`],
  ['512-character value', permissionsAtLimit],
]) test(`accepted permissions retain ${name} without changing permission denial`, async () => {
  const f = fixture([refuse(403, { 'x-accepted-github-permissions': value }, 'Resource not accessible by integration')]);
  await assert.rejects(f.github.request(path), error => {
    assert.ok(error instanceof GitHubHTTPError);
    assert.equal(error.code, 'permission_denied');
    assert.equal(error.classification, 'permission');
    assert.deepEqual(error.details.headers, { 'x-accepted-github-permissions': value });
    assert.equal(f.diagnostics[0].headers['x-accepted-github-permissions'], value);
    return true;
  });
  assert.equal(f.calls.length, 1); assert.deepEqual(f.waits, []);
});

for (const [name, value] of [
  ['empty value', ''],
  ['unsupported level', 'checks=owner'],
  ['uppercase name', 'Checks=read'],
  ['hyphenated name', 'pull-requests=read'],
  ['numeric name', 'checks2=read'],
  ['missing name', '=read'],
  ['65-character name', `${'a'.repeat(65)}=read`],
  ['missing level', 'checks='],
  ['whitespace inside pair', 'checks =read'],
  ['missing separator', 'checks=read contents=write'],
  ['trailing separator', 'checks=read;'],
  ['empty permission set', 'checks=read,,contents=write'],
  ['513-character valid value', permissionsAtLimit.replace('bbbbbbbbbb=', 'bbbbbbbbbbb=')],
  ['oversized value with valid prefix', permissionsAtLimit + ';private-token'],
  ['token-like value', 'github_pat_' + 'not_a_real_token'.repeat(4)],
  ['token-like permission level', 'checks=github_pat_' + 'not_a_real_token'.repeat(4)],
  ['arbitrary prose', 'Contact an administrator for access'],
  ['valid prefix followed by prose', 'checks=read; private-source private-token'],
]) test(`accepted permissions drop the entire ${name} from diagnostics`, async () => {
  const f = fixture([refuse(403, { 'x-accepted-github-permissions': value }, 'Resource not accessible by integration')]);
  await assert.rejects(f.github.request(path), error => {
    assert.ok(error instanceof GitHubHTTPError);
    assert.equal(error.code, 'permission_denied');
    assert.equal(error.classification, 'permission');
    assert.deepEqual(error.details.headers, {});
    assert.deepEqual(f.diagnostics[0].headers, {});
    if (value) assert.equal((error.message + JSON.stringify(f.diagnostics)).includes(value), false);
    return true;
  });
  assert.equal(f.calls.length, 1); assert.deepEqual(f.waits, []);
});

for (const message of ['Resource not accessible by integration', 'Resource not accessible by personal access token', 'Forbidden by organization policy', '', 'API rate limit exceeded']) {
  test(`403 ${message || 'without body proof'} fails immediately with honest classification`, async () => {
    const f = fixture([refuse(403, {}, message)]);
    const permission = message.startsWith('Resource not accessible');
    await assert.rejects(f.github.request(path), error => error.classification === (permission ? 'permission' : 'unknown_forbidden'));
    assert.equal(f.calls.length, 1); assert.deepEqual(f.waits, []);
  });
}

for (const [name, headers, deadline] of [
  ['wait does not fit', { 'retry-after': '90' }, start + 120_000],
  ['exact retry-margin boundary', { 'retry-after': '5' }, start + 40_000],
  ['missing reset', { 'x-ratelimit-remaining': '0' }, start + 120_000],
  ['invalid reset', { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': 'secret' }, start + 120_000],
  ['invalid announced wait', { 'retry-after': '-1' }, start + 120_000],
]) test(`${name} is a typed terminal rate failure and blocks later cleanup wire requests`, async () => {
  const f = fixture([refuse(403, headers)], { deadlineMs: deadline });
  let failure;
  await assert.rejects(f.github.request(path), error => { failure = error; return error.code === 'rate_limited'; });
  await assert.rejects(f.github.request('/pulls/7'), error => error === failure);
  assert.equal(f.calls.length, 1); assert.deepEqual(f.waits, []);
});

test('a repeated rate refusal is terminal after two attempts, with no third wait or request', async () => {
  const f = fixture([refuse(429, { 'retry-after': '1' }), refuse(429, { 'retry-after': '1' })]);
  await assert.rejects(f.github.request(path), error => error.code === 'rate_limited' && error.details.attempt === 2);
  assert.equal(f.calls.length, 2); assert.deepEqual(f.waits, [1000]);
});

test('conclusive rate refusal permits a single identical POST retry, but permission and 5xx do not', async () => {
  const body = { name: CHECK_NAME, head_sha: head };
  const f = fixture([refuse(429, { 'retry-after': '1' }), ok({ id: 7 })]);
  assert.equal((await f.github.request('/check-runs', { method: 'POST', checks: true, body })).id, 7);
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0].request.body, f.calls[1].request.body);
  assert.equal(f.calls[0].request.headers.authorization, f.calls[1].request.headers.authorization);
  for (const response of [refuse(403, {}, 'Resource not accessible by integration'), refuse(503)]) {
    const denied = fixture([response]);
    await assert.rejects(denied.github.request('/check-runs', { method: 'POST', checks: true, body }));
    assert.equal(denied.calls.length, 1); assert.deepEqual(denied.waits, []);
  }
});

test('real peer receives POST then drops response: unknown outcome and exactly one dispatch', async () => {
  let dispatches = 0;
  const server = createServer((request, response) => {
    request.resume();
    request.on('end', () => { dispatches++; response.destroy(); });
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  try {
    const github = new GitHub({ repository: 'example/repo', checkToken: 'fixture',
      apiOrigin: `http://127.0.0.1:${server.address().port}` });
    await assert.rejects(github.request('/check-runs', { method: 'POST', checks: true, body: { head_sha: head } }),
      error => error.code === 'outcome_unknown' && !error.message.includes('fixture'));
    assert.equal(dispatches, 1);
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});

test('unreadable successful POST response keeps the write outcome unknown without replay', async () => {
  const f = fixture([new Response('private-token malformed JSON', { status: 201 })]);
  await assert.rejects(f.github.request('/check-runs', { method: 'POST', checks: true, body: {} }),
    error => error.code === 'outcome_unknown' && !error.message.includes('private-token'));
  assert.equal(f.calls.length, 1); assert.deepEqual(f.waits, []);
});

test('oversized or malformed response prose is not classification proof', async () => {
  for (const response of [refuse(403, {}, 'x'.repeat(9000) + 'secondary rate limit'),
    new Response('private-token invalid JSON', { status: 403 })]) {
    const f = fixture([response]);
    await assert.rejects(f.github.request('/pulls/7'), error => error.classification === 'unknown_forbidden');
    assert.equal(f.calls.length, 1); assert.deepEqual(f.waits, []);
    assert.equal(JSON.stringify(f.diagnostics).includes('private-token'), false);
  }
});

test('expired deadline stops initial PR discovery and later pagination before another request', async () => {
  const expired = fixture([], { deadlineMs: start - 1 });
  await assert.rejects(collectPullRequest(expired.github, 7), /deadline/); assert.equal(expired.calls.length, 0);
  let now = start, calls = 0;
  const github = new GitHub({ repository: 'example/repo', checkToken: 'fixture', now: () => now,
    deadlineMs: start + 10, fetcher: async () => { calls++; now += 11; return ok({ check_runs: Array(100).fill({}) }); } });
  await assert.rejects(github.checkRuns(head), /deadline/); assert.equal(calls, 1);
});

const target = { kind: 'pull_request', number: 7, base_sha: base, policy_sha: base, head_sha: head,
  repository_id: '1', files: [{ filename: 'src/change.js', status: 'modified' }] };
const trigger = { kind: 'workflow_dispatch', id: '7', generation: '1' };
const complete = input => ({ ...input, status: 'complete', findings: [], omitted_paths: [], reason: '' });
for (const stage of ['source', 'discovery', 'post-review', 'context']) for (const kind of ['rate_limited', 'permission_denied', 'unknown_forbidden']) test(`producer ${stage} ${kind} refusal never becomes a neutral or successful publication`, async () => {
  const f = fixture([kind === 'rate_limited' ? refuse(403, { 'retry-after': '90' }) :
    refuse(403, {}, kind === 'permission_denied' ? 'Resource not accessible by integration' : 'unproven failure')]);
  let finishes = 0, inferences = 0, reviewed = false;
  const realRequest = f.github.request.bind(f.github);
  f.github.source = async (path, sha) => {
    if (stage === 'source' || (stage === 'context' && path === 'src/caller.js')) return realRequest('/git/trees/' + sha);
    if (path === 'src/change.js') return sha === base ? 'before\n' : 'after\n';
    return null;
  };
  f.github.begin = stage === 'discovery' ? async () => realRequest(path) : async () => ({ id: 7, app: { id: APP_ID }, name: CHECK_NAME, head_sha: head, status: 'in_progress' });
  f.github.current = async () => stage === 'post-review' && reviewed ? realRequest('/pulls/7') : true;
  f.github.finish = async () => { finishes++; throw new Error('refusal must not publish'); };
  await assert.rejects(produceReview({ github: f.github, target, trigger, createReviewInput, engineVersion: 'test',
    deadlineMs: Date.now() + 120_000, engine: async input => {
      inferences++; reviewed = true;
      return stage === 'context' ? { ...complete(input), status: 'incomplete', context_requests: [{ path: 'src/caller.js', side: 'head' }] } : complete(input);
    } }), error => error.code === kind);
  assert.equal(f.calls.length, 1); assert.equal(finishes, 0);
  assert.equal(inferences, ['post-review', 'context'].includes(stage) ? 1 : 0);
});

for (const permission of [false, true]) test(`CLI ${permission ? 'permission' : 'rate_limited'} failure persists nonpass and finalizer never overwrites neutral`, () => {
  const directory = mkdtempSync(join(tmpdir(), 'review-rate-cli-'));
  try {
    writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'fixture' }), { mode: 0o600 });
    writeFileSync(join(directory, 'pr-review-state.json'), JSON.stringify([{ id: 7, head_sha: head }]));
    writeFileSync(join(directory, 'event.json'), JSON.stringify({ repository: { full_name: 'example/repo', id: 1 }, inputs: { 'pull-request': '7' } }));
    const requests = join(directory, 'requests');
    writeFileSync(join(directory, 'preload.mjs'), `import { appendFileSync } from 'node:fs';
      globalThis.fetch = async (url, request) => {
        appendFileSync(${JSON.stringify(requests)}, request.method + '\\n');
        return new Response(JSON.stringify({ message: ${JSON.stringify(permission ? 'Resource not accessible by integration' : 'private-source private-token')} }),
          { status: 403, headers: ${JSON.stringify(permission ? {} : { 'retry-after': '3600' })} });
      };`);
    const env = { ...process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_EVENT_NAME: 'workflow_dispatch',
      GITHUB_EVENT_PATH: join(directory, 'event.json'), GITHUB_RUN_ID: '1', GITHUB_RUN_ATTEMPT: '1', GITHUB_TOKEN: 'fixture',
      GITHUB_STEP_SUMMARY: join(directory, 'summary'), GITHUB_OUTPUT: join(directory, 'outputs') };
    const execute = command => spawnSync('node', ['--import', join(directory, 'preload.mjs'), '.github/actions/pr-review/cli.mjs', command], {
      cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000, env });
    const review = execute('review'); assert.equal(review.status, 1, review.stderr);
    const code = permission ? 'permission_denied' : 'rate_limited';
    assert.match(review.stderr, new RegExp(code));
    assert.equal(readFileSync(join(directory, 'outputs'), 'utf8'), 'outcome=failed\nreview_passed=false\n');
    assert.equal(JSON.parse(readFileSync(join(directory, 'pr-review-github-failure.json'))).code, code);
    const finalized = execute('finalize'); assert.equal(finalized.status, 1, finalized.stderr);
    assert.match(finalized.stderr, new RegExp(code));
    assert.equal(readFileSync(requests, 'utf8'), 'GET\n');
    assert.equal(existsSync(join(directory, 'pr-review-auth.json')), false);
    assert.equal((review.stderr + finalized.stderr).includes('private-source'), false);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('CLI records a terminal rate failure after begin and the finalizer preserves the unfinished Check without a neutral write', () => {
  const directory = mkdtempSync(join(tmpdir(), 'review-rate-after-begin-'));
  try {
    writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'fixture', inferenceToken: 'fixture-inference' }), { mode: 0o600 });
    writeFileSync(join(directory, 'event.json'), JSON.stringify({ repository: { full_name: 'example/repo', id: 1 }, inputs: { 'pull-request': '7' } }));
    const requests = join(directory, 'requests'), moduleUrl = new URL('../.github/actions/pr-review/github.mjs', import.meta.url).href;
    writeFileSync(join(directory, 'preload.mjs'), `import { appendFileSync } from 'node:fs';
      import { GitHub, APP_ID, CHECK_NAME } from ${JSON.stringify(moduleUrl)};
      let active = false;
      const current = GitHub.prototype.current;
      GitHub.prototype.current = async function(...args) { return active ? current.apply(this, args) : true; };
      GitHub.prototype.source = async (path, sha) => path === 'src/change.js' ? (sha === '${base}' ? 'before\\n' : 'after\\n') : null;
      GitHub.prototype.begin = async () => { active = true; return { id: 7, head_sha: '${head}', app: { id: APP_ID }, name: CHECK_NAME, status: 'in_progress' }; };
      globalThis.fetch = async (url, request) => {
        const path = new URL(url).pathname;
        appendFileSync(${JSON.stringify(requests)}, request.method + ' ' + path + '\\n');
        if (String(url).includes('/v1/public/models/')) return new Response(JSON.stringify({ id: 'vendor/reviewer', context_window: 1000000, max_output_tokens: 8192 }));
        if (path.endsWith('/pulls/7') && active) return new Response('{}', { status: 403, headers: { 'retry-after': '3600' } });
        if (path.endsWith('/pulls/7')) return new Response(JSON.stringify({ state: 'open', title: '[review] Source', changed_files: 1,
          head: { sha: '${head}' }, base: { sha: '${base}', ref: 'main', repo: { id: 1 } } }));
        if (path.endsWith('/pulls/7/files')) return new Response(JSON.stringify([{ filename: 'src/change.js', status: 'modified' }]));
        if (path.includes('/compare/')) return new Response(JSON.stringify({ merge_base_commit: { sha: '${base}' } }));
        throw new Error('Unexpected transport; no inference POST or Check PATCH permitted');
      };`);
    const env = { ...process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_EVENT_NAME: 'workflow_dispatch',
      GITHUB_EVENT_PATH: join(directory, 'event.json'), GITHUB_RUN_ID: '1', GITHUB_RUN_ATTEMPT: '1', GITHUB_TOKEN: 'fixture',
      REVIEW_MODEL: 'vendor/reviewer', GITHUB_OUTPUT: join(directory, 'outputs') };
    const execute = command => spawnSync('node', ['--import', join(directory, 'preload.mjs'), '.github/actions/pr-review/cli.mjs', command], {
      cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000, env });
    const review = execute('review'); assert.equal(review.status, 1, review.stderr); assert.match(review.stderr, /rate_limited/);
    assert.deepEqual(JSON.parse(readFileSync(join(directory, 'pr-review-state.json'))).map(run => run.id), [7]);
    assert.equal(readFileSync(join(directory, 'outputs'), 'utf8'), 'outcome=failed\nreview_passed=false\n');
    const before = readFileSync(requests, 'utf8');
    const finalized = execute('finalize'); assert.equal(finalized.status, 1, finalized.stderr); assert.match(finalized.stderr, /rate_limited/);
    assert.equal(readFileSync(requests, 'utf8'), before);
    assert.equal(before.includes('PATCH'), false); assert.equal(before.includes('POST'), false);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('producer never attempts neutral cleanup after an ambiguous terminal PATCH', async () => {
  const f = fixture([new Error('private-token lost response')]);
  f.github.source = async (path, sha) => path === 'src/change.js' ? (sha === base ? 'before\n' : 'after\n') : null;
  f.github.begin = async () => ({ id: 7, app: { id: APP_ID }, name: CHECK_NAME, head_sha: head, status: 'in_progress' });
  f.github.current = async () => true;
  await assert.rejects(produceReview({ github: f.github, target, trigger, createReviewInput, engineVersion: 'test',
    deadlineMs: Date.now() + 120_000, engine: async input => complete(input) }), error => error instanceof PublishUnverified && error.code === 'publish_unverified' &&
      githubFailureDetails(error)?.code === 'outcome_unknown' && error.details.cause.code === 'outcome_unknown');
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].request.method, 'PATCH');
  assert.equal(JSON.parse(f.calls[0].request.body).conclusion, 'success');
});

// Exercise both real CLI processes with #489's accepted-creation and durable
// terminal-intent contracts. These are explicitly synthetic provider responses.
for (const stage of ['create-read', 'finish-write', 'finish-read']) {
  for (const kind of ['primary_rate_limit', 'secondary_rate_limit', 'permission', 'unknown_forbidden']) {
    test(`CLI ${stage} ${kind} preserves refusal cause through publication uncertainty and prevents finalizer writes`, () => {
      const directory = mkdtempSync(join(tmpdir(), 'review-publication-refusal-'));
      try {
        writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'private-check-token', inferenceToken: 'private-inference-token' }), { mode: 0o600 });
        writeFileSync(join(directory, 'event.json'), JSON.stringify({ repository: { full_name: 'example/repo', id: 1 }, inputs: { 'pull-request': '7' } }));
        function preload(directory, base, head, stage, kind) {
          GitHub.prototype.current = async () => true;
          GitHub.prototype.source = async (path, sha) => path === 'src/change.js' ? (sha === base ? 'before\n' : 'after\n') : null;
          let run;
          const calls = [];
          const record = () => writeFileSync(`${directory}/wire.json`, JSON.stringify({ calls, run }));
          const ok = value => new Response(JSON.stringify(value));
          const refusal = () => new Response(JSON.stringify({ message: kind === 'permission' ? 'Resource not accessible by integration' : 'private-token private-source' }), {
            status: 403, headers: kind === 'primary_rate_limit' ? { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': String(Math.ceil(Date.now() / 1000) + 3600) } :
              kind === 'secondary_rate_limit' ? { 'retry-after': '3600' } : {},
          });
          globalThis.fetch = async (url, request) => {
            const path = new URL(url).pathname;
            calls.push(`${request.method} ${path}`); record();
            if (String(url).includes('/v1/public/models/')) return ok({ id: 'vendor/reviewer', context_window: 1000000, max_output_tokens: 8192 });
            if (path === '/v1/chat/completions') {
              const input = JSON.parse(JSON.parse(request.body).messages[1].content);
              const result = { schema_version: 1, repository_id: input.repository_id, base_sha: input.base_sha,
                head_sha: input.head_sha, input_digest: input.input_digest, reviewer_version: input.reviewer_version,
                policy_version: input.policy_version, completion: 'complete', findings: [], omitted_paths: [],
                context_notes: [], context_requests: [], reason: '', review_summary: 'The source change is complete.', related_evidence: [], binary_reviews: [] };
              return ok({ object: 'chat.completion', model: 'vendor/reviewer', choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(result) } }] });
            }
            if (path.endsWith('/pulls/7')) return ok({ state: 'open', title: '[review] Fixture', changed_files: 1,
              head: { sha: head }, base: { sha: base, ref: 'main', repo: { id: 1 } } });
            if (path.endsWith('/pulls/7/files')) return ok([{ filename: 'src/change.js', status: 'modified' }]);
            if (path.includes('/compare/')) return ok({ merge_base_commit: { sha: base } });
            if (path.includes('/commits/')) return ok({ check_runs: [] });
            assert.equal(request.headers.authorization, 'Bearer private-check-token');
            if (request.method === 'POST' && path.endsWith('/check-runs')) {
              run = { ...JSON.parse(request.body), id: 7, app: { id: APP_ID } }; record(); return ok(run);
            }
            assert.ok(path.endsWith('/check-runs/7'));
            const [state] = JSON.parse(readFileSync(`${directory}/pr-review-state.json`, 'utf8'));
            assert.equal(state.id, 7, 'accepted POST must be durable before its first read');
            if (request.method === 'PATCH') {
              assert.equal(state.publication.expected.conclusion, 'success', 'terminal intent must be durable before PATCH');
              if (stage === 'finish-write') return refusal();
              Object.assign(run, JSON.parse(request.body)); record(); return ok(run);
            }
            if (stage === 'create-read' || (stage === 'finish-read' && run.status === 'completed')) return refusal();
            return ok(run);
          };
        }
        writeFileSync(join(directory, 'preload.mjs'), `import assert from 'node:assert/strict';\nimport { readFileSync, writeFileSync } from 'node:fs';\nimport { GitHub, APP_ID } from ${JSON.stringify(new URL('../.github/actions/pr-review/github.mjs', import.meta.url).href)};\n(${preload.toString()})(${JSON.stringify(directory)}, ${JSON.stringify(base)}, ${JSON.stringify(head)}, ${JSON.stringify(stage)}, ${JSON.stringify(kind)});`);
        const env = { ...process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_EVENT_NAME: 'workflow_dispatch',
          GITHUB_EVENT_PATH: join(directory, 'event.json'), GITHUB_RUN_ID: '1', GITHUB_RUN_ATTEMPT: '1', GITHUB_TOKEN: 'private-read-token',
          REVIEW_MODEL: 'vendor/reviewer', GITHUB_OUTPUT: join(directory, 'outputs'), GITHUB_STEP_SUMMARY: join(directory, 'summary') };
        const execute = command => spawnSync('node', ['--import', join(directory, 'preload.mjs'), '.github/actions/pr-review/cli.mjs', command], {
          cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000, env });
        const review = execute('review');
        assert.equal(review.status, 1, review.stderr); assert.match(review.stderr, /publish_unverified/);
        const code = kind.endsWith('_rate_limit') ? 'rate_limited' : kind === 'permission' ? 'permission_denied' : kind;
        const failure = JSON.parse(readFileSync(join(directory, 'pr-review-github-failure.json'), 'utf8'));
        assert.equal(failure.code, code); assert.equal(failure.classification, kind); assert.equal(failure.attempt, 1);
        const [state] = JSON.parse(readFileSync(join(directory, 'pr-review-state.json'), 'utf8'));
        assert.equal(state.id, 7); assert.equal(state.head_sha, head);
        assert.equal(state.publication?.expected.conclusion, stage === 'create-read' ? undefined : 'success');
        const before = readFileSync(join(directory, 'wire.json'), 'utf8');
        const { calls, run } = JSON.parse(before);
        assert.equal(calls.filter(call => call === 'POST /repos/example/repo/check-runs').length, 1);
        assert.equal(calls.filter(call => call === 'PATCH /repos/example/repo/check-runs/7').length, stage === 'create-read' ? 0 : 1);
        assert.equal(run.status, stage === 'finish-read' ? 'completed' : 'in_progress');
        const finalized = execute('finalize');
        assert.equal(finalized.status, 1, finalized.stderr); assert.match(finalized.stderr, new RegExp(code));
        assert.equal(readFileSync(join(directory, 'wire.json'), 'utf8'), before, 'finalizer must not send any cleanup request after refusal');
        assert.equal(existsSync(join(directory, 'pr-review-auth.json')), false);
        for (const value of ['private-token', 'private-source', 'private-check-token', 'private-read-token', 'private-inference-token']) {
          assert.equal((review.stderr + finalized.stderr + JSON.stringify(state) + JSON.stringify(failure)).includes(value), false);
        }
      } finally { rmSync(directory, { recursive: true, force: true }); }
    });
  }
}

for (const completed of [false, true]) for (const kind of ['rate_limited', 'permission_denied', 'unknown_forbidden']) {
  test(`finalizer-first ${kind} on ${completed ? 'terminal receipt' : 'accepted creation'} stops all cleanup and persists cause across another process`, () => {
    const directory = mkdtempSync(join(tmpdir(), 'review-finalizer-refusal-'));
    try {
      const state = { id: 7, head_sha: head, app: { id: APP_ID }, name: CHECK_NAME, status: completed ? 'completed' : 'in_progress' };
      if (completed) state.publication = terminalCheckReceipt({ ...state, conclusion: 'success', completed_at: '2026-11-07T03:20:00Z', output: { title: 'Review complete', summary: 'private-source' } });
      writeFileSync(join(directory, 'pr-review-state.json'), JSON.stringify([state, { ...state, id: 8, status: 'in_progress', publication: undefined }]));
      const requests = join(directory, 'requests');
      writeFileSync(join(directory, 'preload.mjs'), `import { appendFileSync } from 'node:fs';
        globalThis.fetch = async (url, request) => {
          appendFileSync(${JSON.stringify(requests)}, request.method + '\\n');
          return new Response(JSON.stringify({ message: ${JSON.stringify(kind === 'permission_denied' ? 'Resource not accessible by integration' : 'private-source')} }),
            { status: 403, headers: ${JSON.stringify(kind === 'rate_limited' ? { 'retry-after': '3600' } : {})} });
        };`);
      const execute = () => {
        writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'fixture' }), { mode: 0o600 });
        return spawnSync('node', ['--import', join(directory, 'preload.mjs'), '.github/actions/pr-review/cli.mjs', 'finalize'], {
          cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000,
          env: { ...process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_TOKEN: 'fixture' } });
      };
      const first = execute(); assert.equal(first.status, 1, first.stderr); assert.match(first.stderr, new RegExp(kind));
      assert.equal(JSON.parse(readFileSync(join(directory, 'pr-review-github-failure.json'), 'utf8')).code, kind);
      const before = readFileSync(requests, 'utf8'); assert.equal(before, 'GET\n');
      const second = execute(); assert.equal(second.status, 1, second.stderr); assert.match(second.stderr, new RegExp(kind));
      assert.equal(readFileSync(requests, 'utf8'), before);
      assert.equal(existsSync(join(directory, 'pr-review-auth.json')), false);
      assert.equal((first.stderr + second.stderr).includes('private-source'), false);
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}

for (const message of ['This is not a secondary rate limit', 'secondary rate limiter policy denied access',
  'Preflight reported: You have exceeded a secondary rate limit.', 'You have exceeded a secondary rate limiter policy.']) {
  test(`403 body mention is not affirmative quota evidence: ${message}`, async () => {
    const f = fixture([refuse(403, {}, message), ok({ check_runs: [] })]);
    await assert.rejects(f.github.checkRuns(head), error => error.code === 'unknown_forbidden' && error.classification === 'unknown_forbidden');
    assert.equal(f.calls.length, 1); assert.deepEqual(f.waits, []);
  });
}
for (const reset of [undefined, 'private-token', '-1', '1.5']) for (const retry of ['0', '1', '15']) {
  test(`primary reset ${String(reset)} cannot be replaced by Retry-After ${retry}`, async () => {
    const f = fixture([refuse(403, { 'x-ratelimit-remaining': '0', 'retry-after': retry,
      ...(reset === undefined ? {} : { 'x-ratelimit-reset': reset }) }), ok({ check_runs: [] })]);
    await assert.rejects(f.github.checkRuns(head), error => error.code === 'rate_limited' &&
      error.classification === 'primary_rate_limit' && error.details.retry_at_ms === null);
    assert.equal(f.calls.length, 1); assert.deepEqual(f.waits, []);
    assert.equal(JSON.stringify(f.diagnostics).includes('private-token'), false);
  });
}
