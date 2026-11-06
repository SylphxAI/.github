import test from 'node:test';
import assert from 'node:assert/strict';
import { createReviewInput, parseReviewResult, sourceQuote, reviewerVersion } from '../.github/actions/pr-review/contract.mjs';
import { produceReview } from '../.github/actions/pr-review/producer.mjs';
import { recoverContext, recoveryLimits } from '../.github/actions/pr-review/recovery.mjs';
import { reviewPartitions, planReview } from '../.github/actions/pr-review/partition.mjs';
import { bundleFile, APP_ID, CHECK_NAME, GitHub } from '../.github/actions/pr-review/github.mjs';
import { gatewayReviewer } from '../.github/actions/pr-review/gateway.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40);
const inputFor = (context = []) => createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: base, head_sha: head, policy_version: '1', trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' },
  files: [bundleFile({ filename: 'src/change.js', status: 'modified' }, 'before\n', 'after\n')], context });
const wire = (input, extra = {}) => ({ schema_version: 1, repository_id: input.repository_id, base_sha: input.base_sha,
  head_sha: input.head_sha, input_digest: input.input_digest, reviewer_version: reviewerVersion, policy_version: input.policy_version,
  completion: 'complete', findings: [], related_evidence: [], binary_reviews: [], review_summary: 'Reviewed the complete changed behavior.',
  omitted_paths: [], context_notes: [], context_requests: [], reason: '', ...extra });
const parsed = (input, extra) => parseReviewResult(JSON.stringify(wire(input, extra)), input);
const missing = (input, requests = [{ path: 'src/caller.js', side: 'head' }]) => parsed(input, {
  completion: 'incomplete', omitted_paths: ['src/change.js'], reason: 'private reason ::warning::source', context_requests: requests });
const target = { kind: 'pull_request', number: 7, base_sha: base, head_sha: head, repository_id: '1',
  files: [{ filename: 'src/change.js', status: 'modified' }] };
const validate = (result, input) => { assert.equal(result.input_digest, input.input_digest); return result; };
const runRecovery = (options = {}) => recoverContext({ input: inputFor(), target, createReviewInput, validateResult: validate,
  github: { current: async () => true, source: async () => 'caller\n' }, ...options });

class Publisher {
  repository = 'example/repo'; run;
  async current() { return true; }
  async source(path, sha, optional) {
    if (path === '.github/review-policy.toml' || path.endsWith('AGENTS.md')) return null;
    if (path === 'src/change.js') return sha === base ? 'before\n' : 'after\n';
    assert.equal(path, 'src/caller.js'); assert.equal(sha, head); assert.equal(optional, true);
    return 'caller\n';
  }
  async begin(t, external_id, _detailsUrl, { restartCompleted = false } = {}) {
    if (!this.run || (restartCompleted && this.run.status === 'completed')) {
      this.run = { id: (this.run?.id ?? 0) + 1, app: { id: APP_ID }, name: CHECK_NAME, head_sha: t.head_sha, external_id, status: 'in_progress' };
    }
    return this.run;
  }
  async finish(run, conclusion, output) { Object.assign(run, { conclusion, output, status: 'completed' }); return run; }
}

test('normal publisher recovers exact-head context, binds final digest and keeps model prose off check and receipt', async () => {
  const github = new Publisher(), seen = [], saved = [];
  const options = { github, target, trigger: inputFor().trigger, createReviewInput, engineVersion: reviewerVersion,
    onResult: result => saved.push(structuredClone(result)), engine: async input => {
      seen.push(input);
      if (!input.context.some(c => c.path === 'src/caller.js')) return missing(input);
      assert.deepEqual(input.context.at(-1), { path: 'src/caller.js', side: 'head', commit_sha: head, content: 'caller\n' });
      return parsed(input, { related_evidence: [{ path: 'src/caller.js', side: 'head', start_line: 1, end_line: 1,
        start_byte: null, end_byte: null, quote: 'caller' }] });
    } };
  const receipt = await produceReview(options);
  assert.equal(receipt.conclusion, 'success'); assert.equal(seen.length, 2);
  assert.notEqual(seen[0].input_digest, seen[1].input_digest);
  assert.equal(receipt.input_digest, seen[1].input_digest);
  assert.match(github.run.output.summary, new RegExp(seen[1].input_digest));
  assert.equal(saved.at(-1).recovery_diagnostics[0].reason, 'private reason ::warning::source');
  assert.equal(saved.at(-1).input_digest, seen[1].input_digest);
  assert.ok(!JSON.stringify({ receipt, check: github.run }).includes('private reason'));
  // The enriched final seal differs from a newly collected initial input. It
  // cannot be reused merely because both assessments started at the same head.
  const next = await produceReview(options);
  assert.equal(next.reused, undefined); assert.equal(next.check_run_id, 2); assert.equal(seen.length, 4);
  assert.equal(next.input_digest, receipt.input_digest);
});

test('absent, already supplied, repeated, invalid, deadline and head-change requests never retry unchanged evidence', async () => {
  for (const failure of ['absent', 'repeated', 'invalid', 'deadline', 'head', 'already-supplied', 'exception', 'byte-limit']) {
    let calls = 0, reads = 0;
    const result = await runRecovery({ deadlineMs: failure === 'deadline' ? Date.now() - 1 : Infinity,
      github: { current: async () => failure !== 'head', source: async () => {
        reads++;
        if (failure === 'exception') throw new Error('private error');
        return failure === 'absent' ? null : failure === 'byte-limit' ? 'x'.repeat(recoveryLimits.bytes + 1) : 'caller\n';
      } }, execute: async input => {
        calls++;
        return missing(input, [{ path: failure === 'invalid' ? '../outside' : failure === 'already-supplied' ? 'src/change.js' : 'src/caller.js', side: 'head' }]);
      } });
    assert.notEqual(result.result.status, 'complete', failure);
    assert.equal(calls, failure === 'deadline' ? 0 : ['absent', 'repeated'].includes(failure) ? 3 : ['byte-limit', 'already-supplied'].includes(failure) ? 2 : 1, failure);
    if (['head', 'deadline', 'already-supplied', 'invalid'].includes(failure)) assert.equal(reads, 0, failure);
    assert.ok(!result.result.reason.includes('private error'));
  }
});

test('bounded new dependencies stop honestly at the original round and file budgets', async () => {
  let calls = 0;
  const result = await runRecovery({ execute: async input => missing(input, [{ path: `src/dependency-${calls++}.js`, side: 'base' }]) });
  assert.equal(calls, recoveryLimits.rounds + 2);
  assert.equal(result.result.recovery.stop, 'round_limit');
  assert.equal(result.result.status, 'incomplete');
  let first = true, limitedCalls = 0;
  const limited = await runRecovery({ execute: async input => {
    limitedCalls++;
    const requests = Array.from({ length: first ? recoveryLimits.files : 1 }, (_, n) => ({ path: `src/dep-${first ? n : recoveryLimits.files + 1}.js`, side: 'head' }));
    first = false; return missing(input, requests);
  } });
  assert.equal(limited.result.recovery.stop, 'file_limit');
  assert.equal(limitedCalls, 3);
  assert.equal(limited.result.status, 'incomplete');
});

test('unchanged context is side-qualified and exact, including renamed base aliases and partition fragments', () => {
  const input = inputFor([{ path: 'src/caller.js', side: 'base', commit_sha: base, content: 'old caller\n' },
    { path: 'src/caller.js', side: 'head', commit_sha: head, content: 'new caller\n' }]);
  assert.equal(sourceQuote(input, 'src/caller.js', 'base', 1, 1), 'old caller');
  assert.equal(sourceQuote(input, 'src/caller.js', 'head', 1, 1), 'new caller');
  assert.equal(sourceQuote(input, 'src/caller.js', 'context', 1, 1), null);
  const singleSide = inputFor([{ path: 'src/caller.js', side: 'head', commit_sha: head, content: 'new caller\n' }]);
  assert.equal(sourceQuote(singleSide, 'src/caller.js', 'context', 1, 1), 'new caller');
  assert.equal(sourceQuote(singleSide, 'src/caller.js', 'base', 1, 1), null);
  assert.equal(parsed(singleSide, { related_evidence: [{ path: 'src/caller.js', side: 'context', start_line: 1, end_line: 1,
    start_byte: null, end_byte: null, quote: 'new caller' }] }).status, 'complete');
  assert.throws(() => inputFor([{ path: 'src/caller.js', side: 'head', commit_sha: base, content: 'wrong pin' }]));
  const renamed = createReviewInput({ ...input, files: [bundleFile({ filename: 'new.js', previous_filename: 'old.js', status: 'renamed' }, 'old\n', 'new\n')] });
  assert.equal(sourceQuote(renamed, 'new.js', 'base', 1, 1), null);
  assert.equal(sourceQuote(renamed, 'old.js', 'base', 1, 1), 'old');
  const long = inputFor([{ path: 'src/caller.js', side: 'head', commit_sha: head, content: 'caller\n'.repeat(3000) }]);
  const parts = planReview(long, value => Buffer.byteLength(JSON.stringify(value)), 12_000);
  const contexts = parts.flatMap(p => p.context);
  assert.ok(contexts.length > 1);
  assert.ok(contexts.every(c => c.side === 'head' && c.commit_sha === head));
  assert.equal(contexts.flatMap(c => c.segments).map(s => s.content).join(''), 'caller\n'.repeat(3000));
  for (const part of parts) for (const context of part.context) {
    const s = context.segments[0];
    assert.equal(sourceQuote(part, context.path, 'head', s.start_line, s.end_line - 1, s.start_byte, s.end_byte), s.content);
  }
});

test('partition and synthesis requests retain private bound diagnostics and restart the whole source scope', async () => {
  const input = inputFor([{ path: 'AGENTS.md', content: 'source\n'.repeat(2000) }]);
  for (const phase of ['part', 'synthesis']) {
    const result = await reviewPartitions(input, { byteBudget: 12_000, requestBytes: x => Buffer.byteLength(JSON.stringify(x)),
      execute: async part => phase === 'synthesis' && part.scope?.kind !== 'synthesis' ? parsed(part) : missing(part) });
    assert.equal(result.status, 'incomplete'); assert.equal(result.input_digest, input.input_digest);
    assert.deepEqual(result.context_requests, [{ path: 'src/caller.js', side: 'head' }]);
    assert.equal(result.diagnostics.reason, 'private reason ::warning::source');
    assert.notEqual(result.diagnostics.input_digest, input.input_digest);
    const { diagnostics, ...safe } = result;
    assert.ok(!JSON.stringify(safe).includes('private reason'));
  }
});

test('configured gateway model and deadline remain unchanged across evidence-qualified rounds', async () => {
  const posts = [];
  const engine = gatewayReviewer({ token: 'private token', model: 'vendor/reviewer', deadlineMs: Date.now() + 5000,
    maxOutputTokens: 8192, reasoningEffort: 'high', fetcher: async (_url, request) => {
      if (request.method === 'GET') return { ok: true, json: async () => ({ id: 'vendor/reviewer', context_window: 100_000, max_output_tokens: 8192 }) };
      const body = JSON.parse(request.body), input = JSON.parse(body.messages[1].content); posts.push(body);
      return { ok: true, json: async () => ({ object: 'chat.completion', model: 'vendor/reviewer', choices: [{ finish_reason: 'stop',
        message: { role: 'assistant', content: JSON.stringify(wire(input, posts.length === 1 ? {
          completion: 'incomplete', omitted_paths: ['src/change.js'], context_requests: [{ path: 'src/caller.js', side: 'head' }], reason: 'missing caller' } : {})) } }] }) };
    } });
  const result = await runRecovery({ execute: engine });
  assert.equal(result.result.status, 'complete'); assert.equal(posts.length, 2);
  assert.ok(posts.every(p => p.model === 'vendor/reviewer' && p.max_completion_tokens === 8192 && p.reasoning_effort === 'high'));
  assert.notEqual(posts[0].messages[1].content, posts[1].messages[1].content);
});

test('source collector enforces the shared deadline and blob size before downloading unbounded context', async () => {
  let calls = 0;
  const github = new GitHub({ repository: 'example/repo', readToken: 'private', fetcher: async () => {
    calls++; return { ok: true, json: async () => ({ tree: [{ path: 'large.txt', type: 'blob', mode: '100644', sha: base, size: 100 }] }) };
  } });
  await assert.rejects(() => github.source('large.txt', head, true, { maxBytes: 10 }), /byte limit/);
  assert.equal(calls, 1);
  await assert.rejects(() => github.current(target, { deadlineMs: Date.now() - 1 }), /deadline/);
  assert.equal(calls, 1);
});

test('enriched reruns replace stale findings and reject an earlier round digest without retrying', async () => {
  const defect = { path: 'src/change.js', side: 'head', start_line: 1, end_line: 1, severity: 'medium',
    title: 'Missing guard', explanation: 'A required guard was not visible.', trigger: 'An unchecked caller enters.',
    consequence: 'The operation can fail.', evidence: [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1,
      start_byte: null, end_byte: null, quote: 'after' }] };
  for (const later of ['clean', 'quota', 'stale']) {
    const github = new Publisher(), saved = []; let initial, calls = 0;
    const receipt = await produceReview({ github, target, trigger: inputFor().trigger, createReviewInput, engineVersion: reviewerVersion,
      onResult: r => saved.push(structuredClone(r)), engine: async input => {
        calls++;
        if (calls === 1) { initial = input; return { ...missing(input), findings: [defect] }; }
        if (later === 'clean') return parsed(input);
        if (later === 'stale') return parsed(initial);
        return { status: 'unavailable', head_sha: head, base_sha: base, input_digest: input.input_digest,
          reviewer_version: reviewerVersion, findings: [], omitted_paths: ['src/change.js'], reason: 'Review gateway refused HTTP 429.' };
      } });
    assert.equal(calls, 2);
    assert.equal(receipt.conclusion, later === 'clean' ? 'success' : 'neutral');
    assert.equal(receipt.findings, 0);
    assert.equal(github.run.output.annotations.length, 0);
    assert.equal(saved.at(-1).recovery_diagnostics[0].reason, 'private reason ::warning::source');
    if (later === 'stale') assert.match(receipt.reason, /valid pinned result/);
  }
});

test('context collection keeps base and head pins distinct, deduplicates requests and stops after a late deadline', async () => {
  const requests = [{ path: 'src/caller.js', side: 'base' }, { path: 'src/caller.js', side: 'head' },
    { path: 'src/caller.js', side: 'base' }], pins = []; let calls = 0;
  const result = await runRecovery({ github: { current: async () => true, source: async (_path, sha) => { pins.push(sha); return 'same bytes\n'; } },
    execute: async input => ++calls === 1 ? missing(input, requests) : parsed(input) });
  assert.equal(result.result.status, 'complete'); assert.deepEqual(pins, [base, head]);
  assert.equal(result.input.context.length, 2);
  const now = Date.now; let time = now(), lateCalls = 0;
  try {
    Date.now = () => time;
    const late = await runRecovery({ deadlineMs: time + 1000, github: { current: async () => true,
      source: async () => { time += 2000; return 'caller\n'; } }, execute: async input => { lateCalls++; return missing(input); } });
    assert.equal(lateCalls, 1); assert.equal(late.result.recovery.stop, 'deadline');
    let reserveCalls = 0;
    const reserve = await runRecovery({ deadlineMs: time + 2500, github: { current: async () => true, source: async () => 'caller\n' },
      execute: async input => { reserveCalls++; time += 1500; return missing(input); } });
    assert.equal(reserveCalls, 1); assert.equal(reserve.result.recovery.stop, 'deadline');
    assert.match(reserve.result.reason, /Insufficient review deadline remaining/);
  } finally { Date.now = now; }
});

test('a head advancing during context acquisition is cancelled, and requested-path errors never reach public sinks', async () => {
  for (const failure of ['head', 'path']) {
    const github = new Publisher(), source = github.source.bind(github), saved = []; let current = true, calls = 0;
    const canary = 'PRIVATE_CONTEXT_CANARY';
    github.current = async () => current;
    github.source = async (path, ...args) => {
      if (path.includes(canary)) throw new Error(`Source ${path} crosses a non-directory.`);
      if (path === 'src/caller.js') { current = false; return 'caller\n'; }
      return source(path, ...args);
    };
    const receipt = await produceReview({ github, target, trigger: inputFor().trigger, createReviewInput, engineVersion: reviewerVersion,
      onResult: r => saved.push(r), engine: async input => { calls++; return missing(input,
        [{ path: failure === 'path' ? `AGENTS.md/${canary}` : 'src/caller.js', side: 'head' }]); } });
    assert.equal(calls, 1); assert.equal(receipt.conclusion, failure === 'head' ? 'cancelled' : 'neutral');
    assert.ok(!JSON.stringify({ receipt, check: github.run }).includes(canary));
    if (failure === 'path') assert.ok(JSON.stringify(saved.at(-1).recovery_diagnostics).includes(canary));
  }
});

test('only validated omitted names are retained and request/file bounds share the schema contract', () => {
  const input = inputFor(), reason = 'bounded private cause';
  const result = parsed(input, { completion: 'incomplete', reason, omitted_paths: ['src/change.js', 'UNTRUSTED_OMITTED_NAME'] });
  assert.equal(result.status, 'unavailable');
  assert.deepEqual(result.diagnostics.omitted_paths, ['src/change.js']);
  assert.equal(result.diagnostics.reason, reason);
  assert.equal(parsed(input, { completion: 'incomplete', context_requests: Array.from({ length: recoveryLimits.files + 1 },
    (_, n) => ({ path: `src/${n}.js`, side: 'head' })) }).status, 'unavailable');
});

for (const mode of ['recovery', 'absence', 'anchor', 'defect', 'unavailable', 'incomplete', 'outside', 'cancelled']) test(`CLI ${mode} persists mode-0600 results and emits only the safe final receipt`, async () => {
  const { mkdtempSync, writeFileSync, readFileSync, statSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const { spawnSync } = await import('node:child_process');
  const directory = mkdtempSync(join(tmpdir(), 'review-recovery-cli-'));
  try {
    writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'fixture', inferenceToken: 'fixture' }), { mode: 0o600 });
    writeFileSync(join(directory, 'event.json'), JSON.stringify({ repository: { full_name: 'example/repo', id: 1 }, inputs: { 'pull-request': '7' } }));
    async function preload(mode) {
      const { createHash } = await import('node:crypto');
      const { writeFileSync } = await import('node:fs');
      const base = 'a'.repeat(40), head = 'b'.repeat(40), treeSha = 'c'.repeat(40), blobs = new Map();
      const file = (path, content) => {
        const sha = createHash('sha1').update(`blob ${Buffer.byteLength(content)}\0`).update(content).digest('hex');
        blobs.set(sha, { encoding: 'base64', content: Buffer.from(content).toString('base64'), size: Buffer.byteLength(content) });
        return { path, mode: '100644', type: 'blob', sha, size: Buffer.byteLength(content) };
      };
      const before = file('change.js', 'before\n'), after = file('change.js', 'after\n'), caller = file('caller.js', 'caller\n');
      const policy = file('review-policy.toml', 'version = 1\n[tier0]\npaths = ["src/**"]\n[generated]\npaths = []\n[review]\npaths = []\n');
      let run, calls = 0;
      globalThis.fetch = async (url, request = {}) => {
        const path = new URL(url).pathname, body = request.body && JSON.parse(request.body); let data;
        if (path.includes('/v1/public/models/')) data = { id: 'vendor/reviewer', context_window: 100_000, max_output_tokens: 8192 };
        else if (path === '/v1/chat/completions') {
          const input = JSON.parse(body.messages[1].content); calls++;
          if (mode === 'unavailable') return { ok: false, status: 503, json: async () => ({ error: { code: 'service_unavailable' } }) };
          const result = { schema_version: 1, repository_id: input.repository_id, base_sha: base, head_sha: head,
            input_digest: input.input_digest, reviewer_version: input.reviewer_version, policy_version: input.policy_version,
            completion: calls === 1 ? 'incomplete' : 'complete', findings: [], related_evidence: [], binary_reviews: [], review_summary: 'Reviewed source.',
            omitted_paths: calls === 1 ? ['src/change.js'] : [], context_notes: [],
            context_requests: calls === 1 ? [{ path: mode === 'absence' ? 'PRIVATE_CLI_DIAGNOSTIC.js' : 'src/caller.js', side: 'head' }] : [], reason: calls === 1 ? 'PRIVATE_CLI_DIAGNOSTIC' : '' };
          if (mode === 'anchor') Object.assign(result, { completion: 'complete', omitted_paths: [], context_requests: [], reason: '',
            related_evidence: [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1,
              start_byte: null, end_byte: null, quote: 'PRIVATE_CLI_ANCHOR' }] });
          if (mode === 'incomplete') Object.assign(result, { completion: 'incomplete', omitted_paths: ['src/change.js'],
            context_requests: [], reason: 'PRIVATE_CLI_DIAGNOSTIC' });
          if (mode === 'defect' || mode === 'cancelled') Object.assign(result, { completion: 'complete', omitted_paths: [], context_requests: [], reason: '',
            findings: mode === 'defect' ? [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1,
              severity: 'high', title: 'Changed return value', explanation: 'The new value breaks its caller.', trigger: 'A caller uses the value.',
              consequence: 'The request fails.', evidence: [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1,
                start_byte: null, end_byte: null, quote: 'after' }] }] : [] });
          data = { object: 'chat.completion', model: 'vendor/reviewer', choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(result) } }] };
        } else if (path.endsWith('/pulls/7')) data = { state: 'open', changed_files: 1, title: 'Review this source',
          head: { sha: mode === 'cancelled' && calls ? 'f'.repeat(40) : head }, base: { sha: base, ref: 'main', repo: { id: 1 } } };
        else if (path.endsWith('/pulls/7/files')) data = [{ filename: 'src/change.js', status: 'modified' }];
        else if (path.includes('/compare/')) data = { merge_base_commit: { sha: base } };
        else if (path.includes('/git/trees/')) {
          const sha = path.split('/').at(-1);
          data = { tree: sha === treeSha ? [before, caller] : sha === 'd'.repeat(40) ? [after, caller] : sha === 'e'.repeat(40) ? [policy] :
            [{ path: 'src', type: 'tree', mode: '040000', sha: sha === base ? treeSha : 'd'.repeat(40) },
              ...mode === 'outside' ? [{ path: '.github', type: 'tree', mode: '040000', sha: 'e'.repeat(40) }] : []] };
        } else if (path.includes('/git/blobs/')) data = blobs.get(path.split('/').at(-1));
        else if (path.includes('/commits/') && path.endsWith('/check-runs')) data = { check_runs: [] };
        else if (path.endsWith('/check-runs') && request.method === 'POST') data = run = { ...body, id: 1, app: { id: 1299545 } };
        else if (path.endsWith('/check-runs/1')) {
          if (body) Object.assign(run, body);
          data = run;
          writeFileSync(`${process.env.RUNNER_TEMP}/public-check.json`, JSON.stringify(run));
        } else throw new Error('Unexpected fixture request');
        return { ok: true, status: 200, json: async () => data };
      };
    }
    writeFileSync(join(directory, 'preload.mjs'), `await (${preload.toString()})(${JSON.stringify(mode)});`);
    const process = spawnSync('node', ['--import', join(directory, 'preload.mjs'), '.github/actions/pr-review/cli.mjs', 'review'], {
      cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000,
      env: { ...globalThis.process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_EVENT_NAME: 'workflow_dispatch',
        GITHUB_EVENT_PATH: join(directory, 'event.json'), GITHUB_RUN_ID: '1', GITHUB_RUN_ATTEMPT: '1', GITHUB_TOKEN: 'fixture',
        REVIEW_MODEL: 'vendor/reviewer', REVIEW_MAX_OUTPUT_TOKENS: '8192', GITHUB_STEP_SUMMARY: join(directory, 'summary.md') },
    });
    assert.equal(process.status, ['defect', 'unavailable', 'incomplete', 'cancelled'].includes(mode) ? 1 : 0, process.stderr);
    const receipt = JSON.parse(process.stdout).reviews[0];
    assert.equal(receipt.conclusion, mode === 'defect' || mode === 'incomplete' ? 'failure' :
      mode === 'unavailable' || mode === 'outside' ? 'neutral' : mode === 'cancelled' ? 'cancelled' : 'success');
    const summary = readFileSync(join(directory, 'summary.md'), 'utf8');
    assert.ok(summary.includes(receipt.conclusion));
    if (mode === 'outside') {
      assert.equal(receipt.applicable, false); assert.match(summary, /Review not required by path policy/); return;
    }
    const results = JSON.parse(readFileSync(join(directory, 'pr-review-results.json')));
    if (mode === 'recovery' || mode === 'absence') {
      assert.equal(results[0].recovery_diagnostics[0].reason, 'PRIVATE_CLI_DIAGNOSTIC');
      assert.equal(receipt.recovery_sources[0].status, mode === 'absence' ? 'absent' : 'present');
      assert.deepEqual(receipt.recovery_sources, results[0].recovery_sources);
    }
    else if (mode === 'anchor') {
      assert.equal(receipt.validation_failure, undefined);
      assert.equal(receipt.discarded_items, 1);
      assert.equal(results[0].discarded_items.length, 1);
      assert.equal(JSON.parse(readFileSync(join(directory, 'pr-review-receipts.json')))[0].discarded_items, 1);
      // request_bytes records qualified review executions, not corrective POSTs.
      assert.equal(receipt.request_bytes.length, 1);
      assert.match(JSON.parse(readFileSync(join(directory, 'public-check.json'))).output.summary, /1 items discarded: evidence not in source/);
    }
    if (mode === 'defect') assert.match(summary, /Review found blocking defects/);
    if (mode === 'unavailable') assert.match(summary, /HTTP 503/);
    if (mode === 'incomplete') assert.match(summary, /No exact-source context was requested/);
    if (mode === 'cancelled') assert.match(summary, /Head superseded/);
    assert.equal(statSync(join(directory, 'pr-review-results.json')).mode & 0o777, 0o600);
    for (const output of [process.stdout, process.stderr, readFileSync(join(directory, 'pr-review-receipts.json'), 'utf8'),
      readFileSync(join(directory, 'public-check.json'), 'utf8'), readFileSync(join(directory, 'summary.md'), 'utf8')]) {
      assert.ok(!output.includes('PRIVATE_CLI_DIAGNOSTIC'));
      assert.ok(!output.includes('PRIVATE_CLI_ANCHOR'));
    }
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('publisher retains a safe failed-anchor receipt without inferred recovery or public quote leakage', async () => {
  const github = new Publisher(), saved = []; let calls = 0;
  const receipt = await produceReview({ github, target, trigger: inputFor().trigger, createReviewInput, engineVersion: reviewerVersion,
    onResult: result => saved.push(result), engine: async input => {
      calls++;
      return parsed(input, { related_evidence: [{ path: 'src/change.js', side: 'head', start_line: 1, end_line: 1,
        start_byte: null, end_byte: null, quote: 'PRIVATE_FAILED_ANCHOR' }] });
    } });
  assert.equal(calls, 1); assert.equal(receipt.conclusion, 'neutral'); assert.equal(receipt.unavailable, true);
  assert.equal(receipt.validation_failure.code, 'quote_mismatch');
  assert.deepEqual(receipt.validation_failure, saved.at(-1).validation_failure);
  assert.deepEqual(receipt.recovery, { rounds: 0, added_files: 0, added_bytes: 0, stop: 'unavailable' });
  assert.ok(!JSON.stringify({ receipt, check: github.run, saved }).includes('PRIVATE_FAILED_ANCHOR'));
});


test('mixed present and absent pinned context survives either batch order and changes the sealed input', async () => {
  for (const reverse of [false, true]) {
    const requests = [{ path: 'src/caller.js', side: 'head' }, { path: 'src/missing.js', side: 'base' }];
    if (reverse) requests.reverse();
    const seen = [], pins = [], content = 'x'.repeat(68919);
    const result = await runRecovery({ github: { current: async () => true, source: async (path, sha, optional) => {
      pins.push([path, sha, optional]); return path === 'src/caller.js' ? content : null;
    } }, execute: async input => {
      seen.push(input);
      if (seen.length === 1) return missing(input, requests);
      assert.deepEqual(input.context.find(c => c.absent), { path: 'src/missing.js', side: 'base', commit_sha: base, absent: true });
      assert.equal(input.context.find(c => !c.absent).content, content);
      return parsed(input);
    } });
    assert.equal(result.result.status, 'complete'); assert.equal(seen.length, 2);
    assert.notEqual(seen[0].input_digest, seen[1].input_digest);
    assert.deepEqual(result.result.recovery, { rounds: 1, added_files: 2, added_bytes: 68919, stop: 'complete' });
    assert.deepEqual(pins, requests.map(r => [r.path, r.side === 'base' ? base : head, true]));
    assert.deepEqual(result.result.recovery_sources.map(s => [s.side, s.commit_sha, s.status, s.bytes]),
      requests.map(r => [r.side, r.side === 'base' ? base : head, r.side === 'base' ? 'absent' : 'present', r.side === 'base' ? 0 : 68919]));
    assert.ok(result.result.recovery_sources.every(s => s.input_digest === seen[0].input_digest && /^[a-f0-9]{64}$/.test(s.path_sha256)));
  }
});

test('pinned absence is negative evidence, never literal source or an automatic complete review', async () => {
  let calls = 0, reads = 0;
  const result = await runRecovery({ github: { current: async () => true, source: async () => { reads++; return null; } },
    execute: async input => { calls++; return missing(input); } });
  assert.equal(calls, 3); assert.equal(reads, 1);
  assert.equal(result.result.status, 'incomplete'); assert.equal(result.result.recovery.stop, 'no_new_context');
  assert.deepEqual(result.result.recovery, { rounds: 1, added_files: 1, added_bytes: 0, stop: 'no_new_context' });
  assert.equal(sourceQuote(result.input, 'src/caller.js', 'head', 1, 1), null);
  const invalid = parsed(result.input, { related_evidence: [{ path: 'src/caller.js', side: 'head', start_line: 1, end_line: 1,
    start_byte: null, end_byte: null, quote: 'invented missing source' }] });
  assert.equal(invalid.status, 'unavailable'); assert.equal(invalid.validation_failure.code, 'source_unavailable');
  assert.equal(invalid.validation_failure.path, null);
  for (const change of [{ content: '' }, { segments: [] }, { side: undefined }, { commit_sha: base }, { absent: false }, { path: 'src/change.js' }]) {
    assert.throws(() => inputFor([{ path: 'src/caller.js', side: 'head', commit_sha: head, absent: true, ...change }]));
  }
});

test('absence units remain indivisible in partitioned context and explicitly accompany synthesis', async () => {
  const absent = { path: 'src/missing.js', side: 'head', commit_sha: head, absent: true };
  const input = inputFor([{ path: 'AGENTS.md', content: 'instructions\n'.repeat(2000) }, absent]);
  const parts = planReview(input, value => Buffer.byteLength(JSON.stringify(value)), 12_000);
  assert.ok(parts.length > 1);
  assert.deepEqual(parts.flatMap(p => p.context.filter(c => c.absent)), [absent]);
  let synthesized = false;
  const result = await reviewPartitions(input, { byteBudget: 12_000, requestBytes: x => Buffer.byteLength(JSON.stringify(x)),
    execute: async part => {
      if (part.scope?.kind === 'synthesis') { synthesized = true; assert.deepEqual(part.context, [absent]); }
      return parsed(part);
    } });
  assert.equal(synthesized, true); assert.equal(result.status, 'complete');
});

test('source unavailability is not absence, and collected outcomes do not imply sealed progress', async () => {
  const result = await runRecovery({ github: { current: async () => true, source: async path => {
    if (path === 'src/caller.js') return 'caller\n';
    throw new Error('PRIVATE_SOURCE_ERROR');
  } }, execute: async input => missing(input, [{ path: 'src/caller.js', side: 'head' }, { path: 'PRIVATE_SOURCE_PATH', side: 'base' }]) });
  assert.equal(result.result.recovery.stop, 'source_unavailable'); assert.equal(result.input.context.length, 0);
  assert.deepEqual(result.result.recovery, { rounds: 0, added_files: 0, added_bytes: 7, stop: 'source_unavailable' });
  assert.deepEqual(result.result.recovery_sources.map(s => s.status), ['present', 'unavailable']);
  assert.ok(!JSON.stringify(result.result.recovery_sources).includes('PRIVATE_SOURCE'));
});

test('multi-part recovery memoizes completed partitions and attributes partition deadline expiry to deadline', async () => {
  const files = ['src/a.js', 'src/b.js'].map(filename =>
    bundleFile({ filename, status: 'modified' }, 'export const v = 1;\n'.repeat(200), 'export const v = 2;\n'.repeat(200)));
  const multiInput = createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
    base_sha: base, head_sha: head, policy_version: '1', trigger: { kind: 'workflow_run', id: '42', generation: '1' }, files, context: [] });
  const completedParts = new Map(), executedScopes = [];
  const recovered = await runRecovery({
    input: multiInput,
    github: { current: async () => true, source: async path => `// ${path}\n` },
    execute: async currentInput => reviewPartitions(currentInput, {
      byteBudget: 14_000,
      requestBytes: value => Buffer.byteLength(JSON.stringify(value)),
      completedParts,
      execute: async part => {
        executedScopes.push(`${part.scope.kind}:${part.scope.index ?? 'all'}`);
        if (part.scope.kind === 'partition' && part.scope.index === '00000002' && !currentInput.context.some(c => c.path === 'src/helper.js')) {
          return parseReviewResult(JSON.stringify({
            ...wire(part),
            completion: 'incomplete',
            omitted_paths: part.files.map(f => f.path),
            context_requests: [{ path: 'src/helper.js', side: 'head' }],
            reason: 'need helper',
          }), part);
        }
        return parsed(part);
      },
    }),
  });
  assert.equal(recovered.result.status, 'complete');
  assert.deepEqual(executedScopes, ['partition:00000001', 'partition:00000002', 'partition:00000002', 'synthesis:all']);

  const timedOut = await runRecovery({
    input: multiInput,
    execute: async currentInput => ({
      status: 'incomplete',
      head_sha: currentInput.head_sha,
      base_sha: currentInput.base_sha,
      input_digest: currentInput.input_digest,
      reviewer_version: currentInput.reviewer_version,
      findings: [],
      omitted_paths: ['src/b.js'],
      context_requests: [],
      parts: { planned: 9, completed: 1 },
      reason: 'Part 2/9: Review deadline expired before inference.',
    }),
  });
  assert.equal(timedOut.result.recovery.stop, 'deadline');
  assert.equal(timedOut.result.reason, 'Part 2/9: Review deadline expired before inference.');

  const hugeContextInput = inputFor([{ path: 'src/caller.js', side: 'head', commit_sha: head, content: 'caller\n'.repeat(2000) }]);
  const partitioned = planReview(hugeContextInput, value => Buffer.byteLength(JSON.stringify(value)), 12_000);
  assert.ok(partitioned.length > 1);
  assert.deepEqual(partitioned[0].scope.partitioned_context, [{ path: 'src/caller.js', side: 'head', commit_sha: head, bytes: Buffer.byteLength('caller\n'.repeat(2000)) }]);
});
