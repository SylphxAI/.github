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

// Exercise the same collector/parser/recovery/publisher boundary as the action,
// including the actual Check Run payload, not just parser status.
const { APP_ID, CHECK_NAME } = await import('../.github/actions/pr-review/github.mjs');
const { produceReview } = await import('../.github/actions/pr-review/producer.mjs');
const { reviewPartitions } = await import('../.github/actions/pr-review/partition.mjs');
async function publish(reply, { current, deadlineMs, source: suppliedSource } = {}) {
  const source = suppliedSource ?? { 'change.js': ['before\nstable\n', 'after\nstable\n'], 'caller.js': ['old caller\n', 'new caller\n'] };
  const reads = [], posts = [], runs = [];
  const github = { repository: 'example/repo',
    current: async () => current ? current(posts) : true,
    source: async (path, sha, optional) => {
      reads.push(path);
      if (path === '.github/review-policy.toml') return 'version=1\n[tier0]\npaths=[]\n[generated]\npaths=[]\n[review]\npaths=["**"]\n';
      if (source[path]) return source[path][sha === 'a'.repeat(40) ? 0 : 1];
      if (optional) return null;
      throw new Error('Unexpected source request');
    },
    begin: async () => { const run = { id: 1, name: CHECK_NAME, head_sha: 'b'.repeat(40), app: { id: APP_ID } }; runs.push(run); return run; },
    finish: async (run, conclusion, output) => Object.assign(run, { conclusion, output }),
  };
  const receipt = await produceReview({ github, target: { kind: 'pull_request', number: 1,
    repository_id: '1', base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40),
    files: Object.keys(source).map(filename => ({ filename, status: 'modified' })) },
    trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' }, detailsUrl: 'https://example.com/1',
    createReviewInput, engineVersion: 'test', deadlineMs,
    engine: async input => { posts.push(input); return parseReviewResult(JSON.stringify(reply(input, posts.length)), input); },
  });
  return { receipt, run: runs[0], reads, posts };
}

for (const mode of ['head', 'cross-file', 'base', 'outside']) test(`published ${mode} observation has a terminal verdict and no off-diff annotation`, async () => {
  const { receipt, run, posts } = await publish(input => wire(input, [finding([
    evidence('change.js', 'stable', 2), ...mode === 'outside' ? [] : [mode === 'cross-file'
      ? evidence('caller.js', 'new caller') : evidence('change.js', mode === 'base' ? 'before' : 'after', 1, mode)],
  ])]));
  assert.equal(posts.length, 1);
  assert.equal(receipt.conclusion, mode === 'outside' ? 'success' : 'failure');
  assert.equal(run.output.annotations.length, ['outside', 'base'].includes(mode) ? 0 : 1);
  if (run.output.annotations.length) {
    assert.equal(run.output.annotations[0].start_line, 1);
    assert.equal(run.output.annotations[0].path, mode === 'cross-file' ? 'caller.js' : 'change.js');
    assert.equal(run.output.annotations[0].annotation_level, 'failure');
  }
  if (mode === 'outside') assert.match(run.output.summary, /Outside the change \(non-blocking; high\)/);
  if (mode === 'base') assert.match(run.output.summary, /base lines 1–1/);
});

const incomplete = input => ({ ...wire(input, []), completion: 'incomplete', omitted_paths: ['change.js'], reason: 'Missing external revision.' });
for (const mode of ['assumption', 'defect', 'incomplete']) test(`an external-pin review with no source requests ends as ${mode}`, async () => {
  const { receipt, run, reads, posts } = await publish(input => input.context_recovery !== 'exhausted' || mode === 'incomplete'
    ? incomplete(input) : { ...wire(input, mode === 'defect' ? [finding([evidence('change.js', 'after')])] : []),
      context_notes: ['Assumption: external pinned contents are unavailable; the local call contract was reviewed.'] });
  assert.equal(posts.length, 2);
  assert.notEqual(posts[0].input_digest, posts[1].input_digest);
  assert.equal(posts[1].context_recovery, 'exhausted');
  assert.equal(receipt.conclusion, mode === 'assumption' ? 'success' : 'failure');
  assert.equal(reads.filter(p => !['change.js', 'caller.js', 'AGENTS.md', '.github/review-policy.toml'].includes(p)).length, 0);
  assert.equal(run.output.annotations.length, mode === 'defect' ? 1 : 0);
  if (mode === 'incomplete') {
    assert.equal(run.output.title, 'Review incomplete');
    assert.match(run.output.summary, /Review incomplete: review incomplete/);
    assert.match(receipt.reason, /No exact-source context was requested/);
  } else assert.match(run.output.summary, /Assumption: external pinned contents/);
});

for (const phase of ['initial', 'final']) test(`true ${phase} unavailability stays neutral, with no inference retry`, async () => {
  const { receipt, run, posts } = await publish((input, call) => phase === 'final' && call === 1 ? incomplete(input)
    : { ...wire(input, []), completion: 'unavailable', omitted_paths: ['change.js'], reason: 'Gateway unavailable.' });
  assert.equal(posts.length, phase === 'initial' ? 1 : 2);
  assert.equal(receipt.conclusion, 'neutral');
  assert.equal(run.output.title, 'Review unavailable');
});

test('a late head-read outage cannot turn a stubborn final incomplete result into a code failure', async () => {
  const { receipt, run, posts } = await publish(incomplete, { current: posts => {
    if (posts.length === 2) throw new Error('Head read unavailable');
    return true;
  } });
  assert.equal(posts.length, 2);
  assert.equal(receipt.conclusion, 'neutral');
  assert.equal(run.output.annotations.length, 0);
  assert.match(run.output.summary, /Current head could not be verified/);
});

test('off-diff anchoring clips an overlapping quote to changed coordinates and resolves renamed base evidence', () => {
  const input = inputFor();
  const overlap = finding([evidence('change.js', 'after\nstable')]);
  overlap.evidence[0].end_line = 2;
  const clipped = parse([overlap], input);
  assert.equal(clipped.status, 'complete');
  assert.equal(clipped.findings[0].end_line, 1);
  input.files[0] = bundleFile({ filename: 'change.js', previous_filename: 'old.js', status: 'renamed' }, 'before\nstable\n', 'after\nstable\n');
  const renamed = parse([finding([evidence('old.js', 'before', 1, 'base')])], createReviewInput(input));
  assert.equal(renamed.status, 'complete');
  assert.deepEqual([renamed.findings[0].path, renamed.findings[0].side, renamed.findings[0].start_line], ['change.js', 'base', 1]);
});

test('partial-byte evidence must still match exact source bytes before it can supply an anchor', () => {
  const proof = { ...evidence('change.js', 'wrong'), start_byte: 0, end_byte: 5 };
  assert.equal(parse([finding([proof])]).status, 'unavailable');
  proof.quote = 'after';
  assert.equal(parse([finding([proof])]).status, 'complete');
});

test('partition and synthesis observations retain notes and cross-file anchors from the full validation bundle', async () => {
  const input = inputFor(), posts = [];
  const result = await reviewPartitions(input, { requestBytes: p => p.files.length * 500, byteBudget: 500,
    execute: async (part, sources) => {
      posts.push(part);
      if (part.scope.kind === 'synthesis') {
        assert.deepEqual(part.files, []);
        return parseReviewResult(JSON.stringify(wire(part, [finding([evidence('caller.js', 'new caller')])])),
          { ...part, source_files: sources.files, source_context: sources.context });
      }
      return parseReviewResult(JSON.stringify(wire(part, part.files[0].path === 'change.js'
        ? [finding([evidence('change.js', 'stable', 2)])] : [])), part);
    } });
  assert.equal(posts.length, 3);
  assert.equal(result.status, 'complete', result.reason);
  assert.equal(result.findings.length, 1);
  assert.equal(result.findings[0].path, 'caller.js');
  assert.equal(result.findings[0].severity, 'high');
  assert.match(result.context_notes.join('\n'), /Outside the change/);
});

test('an incomplete finding can be reassessed by a final verdict but is not erased by final unavailability', async () => {
  for (const final of ['complete', 'unavailable']) {
    const { receipt, run, posts } = await publish(input => input.context_recovery !== 'exhausted'
      ? { ...incomplete(input), findings: [finding([evidence('change.js', 'after')])] }
      : { ...wire(input, []), ...(final === 'unavailable' ? { completion: final, omitted_paths: ['change.js'], reason: 'Gateway unavailable.' } : {}) });
    assert.equal(posts.length, 2);
    assert.equal(receipt.conclusion, final === 'complete' ? 'success' : 'failure');
    assert.equal(run.output.annotations.length, final === 'complete' ? 0 : 1);
    if (final === 'unavailable') {
      assert.equal(run.output.annotations[0].start_line, 1);
      assert.match(receipt.reason, /Final verdict unavailable/);
    }
  }
});

const priorityPath = 'data-infra-priority-floor.yaml';
const priorityPattern = 'stateful|infra-critical|production|production-nopreempt|system-cluster-critical|system-node-critical';
const priorityLine = `                  priorityClassName: "${priorityPattern}"`;
const prioritySource = { [priorityPath]: ['', '# context\n'.repeat(88) + priorityLine + '\n'] };
const semanticFinding = () => ({ path: priorityPath, side: 'head', start_line: 89, end_line: 89,
  severity: 'high', title: 'The priority pattern matches only a literal string',
  explanation: 'The tool treats the pipe-separated alternatives as one literal value.',
  trigger: 'Creating a workload with priorityClassName stateful.', consequence: 'The workload is rejected.',
  evidence: [evidence(priorityPath, priorityLine, 89)] });
const uncitedClaim = { tool: 'Kyverno', claim: 'The pipe-separated pattern matches only the literal string.' };
// Authoritative Operators table, read 2026-11-07: `|` means “logical or”.
// It refutes the uncited claim above; this citation tests structure, not a fact list.
const citation = { tool: 'Kyverno', claim: 'The pattern operator | means logical OR.',
  documentation_url: 'https://kyverno.io/docs/policy-types/cluster-policy/validate/#operators',
  documentation_quote: '| logical or', documentation_read_date: '2026-11-07' };
const semanticPublish = f => publish(input => wire(input, [f]), { source: prioritySource });

// The absence of a declaration never weakens a source-only finding, including
// old fixtures that predate semantic_claims. Assert the full annotation bytes.
test('source-evidenced finding without semantic_claims retains the exact blocking verdict', async () => {
  const { receipt, run } = await publish(input => wire(input, [finding([evidence('change.js', 'after')])]));
  assert.equal(receipt.conclusion, 'failure');
  assert.equal(receipt.findings, 1);
  assert.deepEqual(run.output.annotations, [{ path: 'change.js', start_line: 1, end_line: 1,
    annotation_level: 'failure', title: 'Changed contract breaks the consumer',
    message: 'The caller no longer receives its expected value.\nTrigger: The caller reads the value.\nConsequence: The request fails.' }]);
  assert.equal(run.output.title, '1 defects require a change');
  assert.ok(!run.output.summary.includes('tool semantics'));
});

test('uncited Kyverno pipe-pattern claim becomes a visible non-blocking question', async () => {
  const { receipt, run } = await semanticPublish({ ...semanticFinding(), semantic_claims: [uncitedClaim] });
  assert.equal(receipt.conclusion, 'success', receipt.reason);
  assert.equal(receipt.findings, 0);
  assert.deepEqual(run.output.annotations, []);
  assert.match(run.output.summary, /0 findings; 0 blocking/);
  assert.match(run.output.summary, /Unverified tool semantics \(non-blocking question\): Can authoritative documentation confirm this claim\?/);
  assert.match(run.output.summary, /The priority pattern matches only a literal string/);
  assert.match(run.output.summary, /head lines 89–89/);
});

test('declared semantics with a complete citation retain blocking power and publish the citation', async () => {
  // A genuinely wrong literal pattern excludes the intended second alternative.
  const line = '                  priorityClassName: "stateful"';
  const f = { ...semanticFinding(), title: 'The literal pattern omits the intended alternative',
    explanation: 'Only stateful is allowed; infra-critical is not one of the alternatives.',
    trigger: 'Creating an infra-critical workload.', semantic_claims: [citation], evidence: [evidence(priorityPath, line, 89)] };
  const { receipt, run } = await publish(input => wire(input, [f]),
    { source: { [priorityPath]: ['', '# context\n'.repeat(88) + line + '\n'] } });
  assert.equal(receipt.conclusion, 'failure');
  assert.equal(receipt.findings, 1);
  assert.equal(run.output.annotations[0].annotation_level, 'failure');
  assert.ok(run.output.summary.includes(citation.documentation_url));
  assert.ok(run.output.summary.includes(citation.documentation_quote));
  assert.match(run.output.summary, /read 2026-11-07/);
});

for (const key of ['documentation_url', 'documentation_quote', 'documentation_read_date']) {
  for (const mode of ['missing', 'empty']) test(`declared semantics with ${mode} ${key} are a question, not a failed review`, async () => {
    const claim = { ...citation };
    if (mode === 'missing') delete claim[key]; else claim[key] = '  ';
    const { receipt, run } = await semanticPublish({ ...semanticFinding(), semantic_claims: [claim] });
    assert.equal(receipt.conclusion, 'success', receipt.reason);
    assert.equal(receipt.findings, 0);
    assert.deepEqual(run.output.annotations, []);
    assert.match(run.output.summary, /non-blocking question/);
  });
}

for (const [name, changes] of [
  ['non-HTTPS URL', { documentation_url: 'http://example.org/reference' }],
  ['invalid URL', { documentation_url: 'not a URL' }],
  ['credential-bearing URL', { documentation_url: 'https://user:password@example.org/reference' }],
  ['impossible date', { documentation_read_date: '2026-02-30' }],
  ['undated prose', { documentation_read_date: 'today' }],
]) test(`declared semantics with ${name} remain non-blocking`, async () => {
  const { receipt, run } = await semanticPublish({ ...semanticFinding(), semantic_claims: [{ ...citation, ...changes }] });
  assert.equal(receipt.conclusion, 'success', receipt.reason);
  assert.equal(receipt.findings, 0);
  assert.match(run.output.summary, /non-blocking question/);
});

test('every declared claim needs a citation; the validator has no vendor fact list', async () => {
  const { receipt, run } = await semanticPublish({ ...semanticFinding(),
    semantic_claims: [{ ...citation, tool: 'Another DSL' }, { ...uncitedClaim, tool: 'Another DSL' }] });
  assert.equal(receipt.conclusion, 'success');
  assert.equal(receipt.findings, 0);
  assert.match(run.output.summary, /non-blocking question/);
});

test('uncited semantics cannot turn fabricated evidence into a public question', async () => {
  const input = createReviewInput({ schema_version: 2, repository: 'example/repo', repository_id: '1',
    base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40), policy_version: '1',
    trigger: { kind: 'workflow_dispatch', id: '1', generation: '1' }, context: [],
    files: [bundleFile({ filename: priorityPath, status: 'added' }, null, prioritySource[priorityPath][1])] });
  const f = { ...semanticFinding(), semantic_claims: [uncitedClaim], evidence: [evidence(priorityPath, 'fabricated', 89)] };
  const result = parseReviewResult(JSON.stringify(wire(input, [f])), input);
  assert.equal(result.status, 'unavailable');
  assert.equal(result.validation_failure.code, 'quote_mismatch');
  assert.ok(!JSON.stringify(result).includes('non-blocking question'));
});
