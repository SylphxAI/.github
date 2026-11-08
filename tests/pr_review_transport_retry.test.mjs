import test from 'node:test';
import assert from 'node:assert/strict';
import { GitHub, bundleFile } from '../.github/actions/pr-review/github.mjs';
import { gatewayReviewer } from '../.github/actions/pr-review/gateway.mjs';
import { createReviewInput } from '../.github/actions/pr-review/contract.mjs';
const input = createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: 'missing',
  trigger: { kind: 'pull_request', id: '7', generation: '1' },
  files: [bundleFile({ filename: 'value.js', status: 'modified' }, 'before\n', 'after\n')], context: [] });
const result = value => ({ schema_version: 1, ...Object.fromEntries(['repository_id', 'base_sha', 'head_sha', 'input_digest', 'reviewer_version', 'policy_version'].map(k => [k, value[k]])),
  completion: 'complete', findings: [], related_evidence: [], binary_reviews: [], review_summary: 'Reviewed source.', omitted_paths: [], context_requests: [], context_notes: [], reason: '' });
const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers });
for (const method of ['GET', 'POST', 'PATCH']) for (const failure of ['transport', 'body', 502, 503, 504]) {
  test(`GitHub ${method} ${failure} only replays idempotent reads`, async () => {
    let calls = 0, now = 1000; const waits = [];
    const github = new GitHub({ repository: 'example/repo', readToken: 'fixture', now: () => now, deadlineMs: 100000,
      wait: async ms => { waits.push(ms); now += ms; }, onDiagnostic: () => {}, fetcher: async () => {
        calls++;
        if (calls > 1) return json({ value: 7 });
        if (failure === 'transport') throw new Error('synthetic disconnect');
        if (failure === 'body') return { ok: true, json: async () => { throw new Error('synthetic body loss'); } };
        return json({}, failure);
      } });
    if (method === 'GET') { assert.deepEqual(await github.request('/pulls/7', { method }), { value: 7 }); assert.equal(calls, 2); assert.equal(waits.length, 1); }
    else { await assert.rejects(github.request('/pulls/7', { method })); assert.equal(calls, 1); assert.deepEqual(waits, []); }
  });
}
for (const phase of ['metadata', 'inference']) for (const failure of [429, 502, 503, 504, 'network']) {
  test(`gateway ${phase} ${failure} retries without consuming corrections`, async () => {
    let now = 1000, failed = false, posts = 0; const waits = [];
    const review = gatewayReviewer({ token: 'fixture', model: 'vendor/reviewer', deadlineMs: 100000, now: () => now, random: () => 0,
      sleep: async ms => { waits.push(ms); now += ms; }, fetcher: async (_url, request) => {
        const selected = (request.method === 'GET') === (phase === 'metadata');
        if (selected && !failed) { failed = true; if (failure === 'network') throw new Error('synthetic disconnect'); return json({}, failure, { 'retry-after': '3' }); }
        if (request.method === 'GET') return json({ id: 'vendor/reviewer', context_window: 1000000, max_output_tokens: 10000 });
        posts++;
        const value = JSON.parse(JSON.parse(request.body).messages[1].content);
        return json({ object: 'chat.completion', model: 'vendor/reviewer', choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: posts < 3 ? '{' : JSON.stringify(result(value)) } }] });
      } });
    assert.equal((await review(input)).status, 'complete'); assert.equal(posts, 3); assert.deepEqual(waits, [failure === 'network' ? 2000 : 3000]);
  });
}
test('gateway persistent capacity and oversized provider wait terminate within original deadline', async () => {
  for (const hint of ['2', '100000']) {
    let now = 1000, calls = 0; const waits = [];
    const review = gatewayReviewer({ token: 'fixture', model: 'vendor/reviewer', deadlineMs: 20000, now: () => now, random: () => 0,
      sleep: async ms => { waits.push(ms); now += ms; }, fetcher: async () => { calls++; return json({}, 429, { 'retry-after': hint }); } });
    assert.equal((await review(input)).status, 'unavailable');
    assert.equal(calls, hint === '2' ? 4 : 1); assert.ok(now < 20000); assert.ok(waits.length <= 3);
  }
});
test('GitHub persistent GET loss terminates after four attempts; short deadline permits no wait', async () => {
  for (const budget of [100000, 1000]) {
    let now = 1000, calls = 0;
    const github = new GitHub({ repository: 'example/repo', readToken: 'fixture', now: () => now, deadlineMs: now + budget,
      wait: async ms => { now += ms; }, fetcher: async () => { calls++; throw new Error('synthetic'); } });
    await assert.rejects(github.request('/pulls/7'), e => e.code === 'read_unavailable');
    assert.equal(calls, budget === 1000 ? 1 : 4);
  }
});
