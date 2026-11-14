import test from 'node:test';
import assert from 'node:assert/strict';
import { adjudicationInstruction, adjudicationSchema, adjudicationRequest, buildCandidates, validateAdjudication, applyAdjudication } from '../.github/actions/pr-review/adjudication.mjs';
import { adjudicationRequest as gatewayRequest, gatewayReviewer } from '../.github/actions/pr-review/gateway.mjs';
import { createReviewInput, reviewerVersion } from '../.github/actions/pr-review/contract.mjs';
import { APP_ID, CHECK_NAME } from '../.github/actions/pr-review/github.mjs';
import { produceReview } from '../.github/actions/pr-review/producer.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40);
const input = { repository_id: '1', base_sha: base, head_sha: head, input_digest: 'digest', reviewer_version: reviewerVersion, policy_version: 'policy' };
const proof = { path: 'src/change.js', side: 'head', start_line: 2, end_line: 2, quote: 'β\r', start_byte: 3, end_byte: 6 };
const finding = { path: proof.path, side: 'head', start_line: 2, end_line: 2, severity: 'high', title: 'Synthetic candidate', explanation: 'Synthetic explanation', trigger: 'Synthetic trigger', consequence: 'Synthetic consequence', evidence: [proof] };
const candidates = buildCandidates({ findings: [finding] }, input);
const source = { files: [{ path: proof.path, side: 'head', content: 'a\r\nβ\r\n' }], context: [] };
const wire = adjudicationRequest(input, candidates, source);
const verdict = () => ({ schema_version: 1, ...input, completion: 'complete', reason: 'Checked causal source.', decisions: [{ candidate_id: candidates[0].candidate_id, verdict: 'retain', contradiction: '', proof_evidence: [structuredClone(proof)] }] });
const check = v => validateAdjudication(v, input, candidates, source);

test('actual gateway prompt, strict pinned schema and candidate identities bind to the reviewer identity', () => {
  const request = JSON.parse(gatewayRequest(wire, { model: 'vendor/reviewer' }));
  assert.equal(request.messages[0].content, adjudicationInstruction);
  assert.equal(JSON.parse(request.messages[1].content).instruction, adjudicationInstruction);
  assert.deepEqual(request.response_format.json_schema.schema.properties.head_sha.enum, [head]);
  assert.equal(request.response_format.json_schema.strict, true);
  assert.equal(adjudicationSchema.additionalProperties, false);
  assert.notEqual(buildCandidates({ findings: [finding] }, { ...input, input_digest: 'other' })[0].candidate_id, candidates[0].candidate_id);
  assert.notEqual(buildCandidates({ findings: [finding, finding] }, input)[1].candidate_id, candidates[0].candidate_id);
});

test('exact UTF-8 and CRLF source proof validates; absent optional byte offsets validate', () => {
  assert.equal(check(verdict()).ok, true);
  const v = verdict(); Object.assign(v.decisions[0].proof_evidence[0], { start_byte: null, end_byte: null });
  assert.equal(check(v).ok, true);
});

const invalid = {
  extra: v => { v.extra = 1; }, identity: v => { v.head_sha = base; }, schema: v => { v.schema_version = 2; },
  completion: v => { v.completion = true; }, reason: v => { v.reason = 1; }, emptyReason: v => { v.reason = ' '; },
  omitted: v => { v.decisions = []; }, duplicate: v => { v.decisions.push(v.decisions[0]); },
  unknown: v => { v.decisions[0].candidate_id = 'unknown'; }, verdict: v => { v.decisions[0].verdict = 'yes'; },
  decisionExtra: v => { v.decisions[0].extra = 1; }, contradictionType: v => { v.decisions[0].contradiction = null; },
  emptyProof: v => { v.decisions[0].proof_evidence = []; }, proofExtra: v => { v.decisions[0].proof_evidence[0].extra = 1; },
  fractionalLine: v => { v.decisions[0].proof_evidence[0].start_line = 1.5; },
  zeroLine: v => { v.decisions[0].proof_evidence[0].start_line = 0; },
  pastEnd: v => { v.decisions[0].proof_evidence[0].end_line = 4; },
  reversed: v => { v.decisions[0].proof_evidence[0].end_line = 1; },
  wrongQuote: v => { v.decisions[0].proof_evidence[0].quote = 'β'; },
  wrongPath: v => { v.decisions[0].proof_evidence[0].path = 'other.js'; },
  wrongSide: v => { v.decisions[0].proof_evidence[0].side = 'base'; },
  splitOffsets: v => { v.decisions[0].proof_evidence[0].start_byte = null; },
  forgedOffset: v => { v.decisions[0].proof_evidence[0].end_byte = 5; },
  stringOffset: v => { v.decisions[0].proof_evidence[0].start_byte = '3'; },
};
for (const [name, mutate] of Object.entries(invalid)) test(`adjudication rejects ${name}`, () => {
  const v = verdict(); mutate(v); assert.equal(check(v).ok, false);
});

test('whitespace-only and unavailable or binary source cannot prove a retained candidate', () => {
  const v = verdict(); Object.assign(v.decisions[0].proof_evidence[0], { start_line: 1, end_line: 1, quote: ' ', start_byte: 0, end_byte: 1 });
  assert.equal(validateAdjudication(v, input, candidates, { files: [{ path: proof.path, side: 'head', content: ' ' }] }).ok, false);
  for (const content of [null, undefined, Buffer.from('β')]) assert.equal(validateAdjudication(verdict(), input, candidates, { files: [{ path: proof.path, side: 'head', content }] }).ok, false);
});

test('insufficient-context anywhere clears all candidates and never publishes model diagnostic reason', () => {
  const second = { ...finding, title: 'Second candidate' }, all = buildCandidates({ findings: [finding, second] }, input);
  const v = verdict(); v.reason = 'PRIVATE_MODEL_DIAGNOSTIC';
  v.decisions = [v.decisions[0], { candidate_id: all[1].candidate_id, verdict: 'insufficient-context', contradiction: 'Private details', proof_evidence: [] }];
  assert.equal(validateAdjudication(v, input, all, source).ok, true);
  const result = applyAdjudication({ status: 'complete', findings: [finding, second] }, v, all);
  assert.deepEqual(result.findings, []); assert.equal(result.status, 'incomplete'); assert.ok(!result.reason.includes('PRIVATE'));
  assert.equal(result.causal_validation.reason, v.reason); // private artifact only
});

test('discard retracts and retain preserves only individually adjudicated candidates', () => {
  const v = verdict(); v.decisions[0].verdict = 'discard'; v.decisions[0].proof_evidence = [];
  assert.equal(check(v).ok, true);
  assert.deepEqual(applyAdjudication({ findings: [finding] }, v, candidates).findings, []);
  assert.deepEqual(applyAdjudication({ findings: [finding] }, verdict(), candidates).findings, [finding]);
});

for (const mode of ['valid', 'late-metadata', 'late-body', 'refusal', 'tool', 'malformed', 'oversized']) test(`adjudication gateway ${mode}`, async () => {
  let clock = 0, posts = 0;
  const review = gatewayReviewer({ token: 'fixture', model: 'vendor/reviewer', deadlineMs: 10_000, now: () => clock, identityLog: () => {},
    fetcher: async (_url, options) => {
      const metadata = options.method === 'GET'; if (!metadata) posts++;
      return { ok: true, json: async () => {
        if (mode === 'late-metadata' && metadata || mode === 'late-body' && !metadata) clock = 10_001;
        if (metadata) return { id: 'vendor/reviewer', context_window: mode === 'oversized' ? 12_300 : 1_000_000, max_output_tokens: 8192 };
        const message = { role: 'assistant', content: JSON.stringify(verdict()) };
        if (mode === 'refusal') message.refusal = 'Cannot';
        if (mode === 'tool') message.tool_calls = [{}];
        if (mode === 'malformed') message.content = '{} no';
        return { object: 'chat.completion', model: 'vendor/reviewer', choices: [{ finish_reason: 'stop', message }] };
      } };
    } });
  const value = await review.adjudicate({ input: wire, deadlineMs: 10_000 });
  if (mode === 'valid') assert.equal(check(value).ok, true);
  else assert.equal(value.status, 'incomplete');
  assert.equal(posts, ['late-metadata', 'oversized'].includes(mode) ? 0 : 1);
});

for (const mode of ['retain', 'discard', 'missing', 'insufficient', 'incomplete', 'partial', 'terminal', 'empty', 'late', 'stale', 'empty-proof', 'failed', 'binary', 'unreadable', 'current-unreadable', 'stale-before', 'current-null', 'current-undefined']) test(`authoritative producer boundary ${mode}`, async t => {
  const clock = Date.now(); let time = clock, current = true, gates = 0, produced = false, postReads = 0; const saved = [], reads = [];
  t.mock.method(Date, 'now', () => time);
  const target = { repository_id: '1', base_sha: base, head_sha: head, policy_sha: base, review_requested: true, files: [{ filename: proof.path, status: 'modified' }] };
  const github = { repository: 'example/repo', current: async () => {
      if (produced && ['current-null', 'current-undefined'].includes(mode)) return mode === 'current-null' ? null : undefined;
      if (produced && ++postReads === 2 && mode === 'current-unreadable') throw new Error('current unavailable');
      return current;
    },
    source: async (path, sha, _optional, options) => {
      if (path === proof.path && reads.filter(r => r.path === proof.path).length >= 2) {
        if (mode === 'binary') return Buffer.from('binary');
        if (mode === 'unreadable') throw new Error('source unavailable');
      }
      reads.push({ path, sha, deadline: options?.deadlineMs }); return path === proof.path ? (sha === head ? 'a\r\nβ\r\n' : 'before\n') : null; },
    begin: async () => ({ id: 1, name: CHECK_NAME, app: { id: APP_ID }, head_sha: head, status: 'in_progress' }),
    finish: async (_run, conclusion, output) => { assert.equal(saved.at(-1).findings.length, mode === 'retain' ? 1 : 0); assert.equal(conclusion, mode === 'stale-before' ? 'cancelled' : ['discard', 'empty'].includes(mode) ? 'success' : ['retain', 'incomplete', 'terminal'].includes(mode) ? 'failure' : 'neutral');
      if (mode === 'stale-before') assert.equal(output.annotations, undefined);
      else assert.equal(output.annotations.length, mode === 'retain' ? 1 : 0); return { conclusion, output }; } };
  const engine = async i => { produced = true; if (mode === 'stale-before') current = false; return ({ status: ['incomplete', 'terminal'].includes(mode) ? 'incomplete' : 'complete',
    base_sha: base, head_sha: head, input_digest: i.input_digest, reviewer_version: i.reviewer_version,
    findings: mode === 'empty' ? [] : [finding], omitted_paths: [], reason: ['incomplete', 'terminal'].includes(mode) ? 'Source incomplete.' : '',
    ...(mode === 'partial' ? { parts: { planned: 2, completed: 1 } } : {}), ...(mode === 'terminal' ? { terminal_incomplete: true } : {}) }); };
  if (mode !== 'missing') engine.adjudicate = async ({ input: request, deadlineMs }) => {
    gates++; if (mode === 'failed') throw new Error('adjudicator unavailable'); assert.equal(deadlineMs, clock + 60_000); assert.deepEqual(request.source_files.map(f => [f.path, f.side]), [[proof.path, 'base'], [proof.path, 'head']]);
    if (mode === 'late') time = clock + 60_001;
    if (mode === 'stale') current = false;
    return { schema_version: 1, ...Object.fromEntries(Object.keys(input).map(k => [k, request[k]])), completion: 'complete', reason: 'PRIVATE_REASON',
      decisions: request.candidates.map(c => ({ candidate_id: c.candidate_id, verdict: mode === 'insufficient' ? 'insufficient-context' : mode === 'discard' ? 'discard' : 'retain', contradiction: '', proof_evidence: ['insufficient', 'discard', 'empty-proof'].includes(mode) ? [] : [proof] })) };
  };
  const receipt = await produceReview({ github, target, trigger: { kind: 'synthetic', id: '1', generation: '1' }, createReviewInput, engineVersion: reviewerVersion,
    engine, onResult: r => saved.push(r), deadlineMs: clock + 120_000 });
  assert.equal(saved.length, 1, 'Only the authoritative result may be persisted.');
  const final = saved.at(-1);
  assert.equal(final.status, ['retain', 'discard', 'empty'].includes(mode) ? 'complete' : 'incomplete');
  assert.equal(final.findings.length, mode === 'retain' ? 1 : 0);
  assert.equal(gates, ['missing', 'empty', 'incomplete', 'partial', 'terminal', 'binary', 'unreadable', 'current-unreadable', 'stale-before', 'current-null', 'current-undefined'].includes(mode) ? 0 : 1);
  assert.ok(!JSON.stringify(receipt).includes('PRIVATE_REASON'));
  assert.ok(reads.every(r => r.deadline === clock + 60_000));
});
