import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { GitHub, bundleFile, digest, APP_ID, CHECK_NAME } from '../.github/actions/pr-review/github.mjs';
import { createReviewInput, parseReviewResult } from '../.github/actions/pr-review/contract.mjs';
import { collectBundle, produceReview } from '../.github/actions/pr-review/producer.mjs';
import { changedRanges, fileSegments } from '../.github/actions/pr-review/source.mjs';
import { planReview, reviewPartitions } from '../.github/actions/pr-review/partition.mjs';

const base = 'a'.repeat(40), head = 'b'.repeat(40);
const trigger = { kind: 'pull_request', id: '7', generation: '1' };
const binary = { filename: 'artifact.binpb', status: 'modified' };
const payload = Buffer.from([0x0a, 0x02, 0xc3, 0x28, 0x00, 0xff]);
const gitHash = bytes => createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
const generator = 'emit("artifact.binpb", compile("source.proto"));\n';
const source = 'message Frame { string value = 1; }\n';
const evidence = (path, quote) => ({ path, side: 'head', start_line: 1, end_line: 1,
  start_byte: null, end_byte: null, quote: quote.trimEnd() });
const provenance = { path: binary.filename, status: 'source-reviewed',
  source_evidence: [evidence('source.proto', source)], derivation_evidence: [evidence('generate.js', generator)],
  limitation: 'Reviewed source and derivation only; binary bytes were not decoded or regenerated; equivalence is unverified.' };
const bundle = files => ({ schema_version: 2, repository: 'example/repo', repository_id: '1',
  base_sha: base, head_sha: head, policy_version: 'missing', trigger, files,
  context: [{ path: 'source.proto', side: 'head', commit_sha: head, content: source },
    { path: 'generate.js', side: 'head', commit_sha: head, content: generator }] });
const inputOf = () => createReviewInput(bundle([bundleFile(binary, Buffer.from([0]), payload)]));
const wire = (input, extra = {}) => ({ schema_version: 1, repository_id: input.repository_id,
  base_sha: input.base_sha, head_sha: input.head_sha, input_digest: input.input_digest,
  reviewer_version: input.reviewer_version, policy_version: input.policy_version,
  completion: 'complete', findings: [], related_evidence: [], binary_reviews: [provenance],
  review_summary: 'Reviewed related source and derivation with binary limitations.',
  omitted_paths: [], context_requests: [], context_notes: [provenance.limitation], reason: '', ...extra });
const parse = (input, extra) => parseReviewResult(JSON.stringify(wire(input, extra)), input);

function provider(bytes, mutate = value => value) {
  const github = new GitHub({ repository: 'example/repo', readToken: 'read' });
  const sha = gitHash(bytes);
  github.request = async path => path.includes('/git/trees/')
    ? { tree: [{ path: binary.filename, type: 'blob', mode: '100644', sha, size: bytes.length }] }
    : mutate({ sha, encoding: 'base64', content: bytes.toString('base64'), size: bytes.length });
  return github;
}

test('verified invalid UTF8 and NUL blobs become finite metadata, never prompt text', async () => {
  for (const bytes of [payload, Buffer.from('valid UTF8\0but binary\n')]) {
    const github = provider(bytes);
    await assert.rejects(() => github.source(binary.filename, head), /not UTF-8 text/);
    const actual = await github.source(binary.filename, head, false, { allowBinary: true });
    assert.deepEqual(actual, bytes);
    const file = bundleFile({ ...binary, status: 'added' }, null, actual);
    assert.equal(file.source_mode, 'binary-metadata');
    assert.deepEqual(file.after, { sha256: digest(bytes), bytes: bytes.length, encoding: 'binary', line_count: null, segments: 0 });
    assert.equal(file.before, null);
    assert.deepEqual(file.hunks[0].rows, []);
    assert.deepEqual(changedRanges(file), []);
    const serialized = JSON.stringify(createReviewInput(bundle([file])));
    assert.ok(!serialized.includes(bytes.toString('base64')));
    assert.ok(!serialized.includes('�'));
    assert.match(serialized, /not supplied as text/);
    const large = bundleFile(binary, bytes, Buffer.concat([bytes, Buffer.alloc(1_000_000, 255)]));
    assert.ok(JSON.stringify(large).length < 1100);
  }
});

test('valid UTF8 including BOM/CRLF keeps the exact existing text representation', async () => {
  const text = '\uFEFFmessage Frame { string value = 1; }\r\n';
  const github = provider(Buffer.from(text));
  const actual = await github.source(binary.filename, head, false, { allowBinary: true });
  assert.equal(actual, text);
  assert.deepEqual(bundleFile(binary, '', actual), bundleFile(binary, '', text));
  assert.equal(bundleFile(binary, '', actual).source_mode, 'segments');
});

test('missing, truncated, wrong-hash, and malformed blob payloads still fail closed with binary enabled', async () => {
  for (const mutate of [v => ({ ...v, content: undefined }), v => ({ ...v, content: payload.subarray(1).toString('base64') }),
    v => ({ ...v, content: Buffer.alloc(payload.length).toString('base64') }), v => ({ ...v, size: -1 }),
    v => ({ ...v, encoding: 'none' })]) {
    await assert.rejects(() => provider(payload, mutate).source(binary.filename, head, false, { allowBinary: true }), /complete|incomplete/);
  }
  await assert.rejects(() => provider(payload).source('missing.binpb', head, false, { allowBinary: true }), /absent/);
  const truncated = provider(payload);
  truncated.request = async () => ({ truncated: true, tree: [] });
  await assert.rejects(() => truncated.source(binary.filename, head, false, { allowBinary: true }), /incomplete/);
});

test('binary add/remove/rename and mixed transitions preserve identities and real text sides', () => {
  for (const [status, before, after] of [['added', null, payload], ['removed', payload, null],
    ['renamed', payload, payload], ['modified', source, payload], ['modified', payload, source]]) {
    const file = bundleFile({ ...binary, status, ...(status === 'renamed' ? { previous_filename: 'old.binpb' } : {}) }, before, after);
    assert.equal(createReviewInput(bundle([file])).files[0].status, status);
    assert.equal(file.previous_path, status === 'renamed' ? 'old.binpb' : null);
    for (const [side, content] of [['before', before], ['after', after]]) {
      assert.equal(file[side]?.sha256 ?? null, content === null ? null : digest(content));
      assert.equal(fileSegments(file, side).map(s => s.content).join(''), typeof content === 'string' ? content : '');
    }
  }
});

test('binary completion requires every assessment and valid source AND derivation quotes', () => {
  const input = inputOf();
  assert.equal(parse(input).status, 'complete');
  for (const binary_reviews of [[], [{ ...provenance, status: 'unresolved' }], [{ ...provenance, source_evidence: [] }],
    [{ ...provenance, derivation_evidence: [] }], [provenance, provenance], [{ ...provenance, path: 'unknown.bin' }],
    [{ ...provenance, source_evidence: [evidence('source.proto', 'invented')] }]]) {
    assert.equal(parse(input, { binary_reviews }).status, 'unavailable');
  }
  const incomplete = parse(input, { completion: 'incomplete', binary_reviews: [{ ...provenance, status: 'unresolved', source_evidence: [], derivation_evidence: [] }],
    omitted_paths: [binary.filename], reason: 'Need derivation source.', context_requests: [{ path: 'other.js', side: 'head' }] });
  assert.equal(incomplete.status, 'incomplete');
  assert.deepEqual(incomplete.context_requests, [{ path: 'other.js', side: 'head' }]);
  const forged = parse(input, { findings: [{ path: binary.filename, side: 'head', start_line: 1, end_line: 1,
    severity: 'high', title: 'Bad binary', trigger: 'Load', consequence: 'Failure', explanation: 'Invented line',
    evidence: [evidence(binary.filename, 'invented')] }] });
  assert.equal(forged.status, 'unavailable');
});

test('producer collects binary and related text together and publishes visible limitations', async () => {
  const github = provider(payload);
  github.current = async () => true;
  const original = github.source.bind(github);
  github.source = async (path, sha, optional, options) => {
    if (path === 'generate.js') return generator;
    if (path === 'source.proto') return source;
    if (path !== binary.filename && optional) return null;
    return original(path, sha, optional, options);
  };
  let output;
  github.begin = async () => ({ id: 1, head_sha: head, app: { id: APP_ID }, name: CHECK_NAME });
  github.finish = async (run, conclusion, value) => { output = value; return { ...run, conclusion }; };
  const target = { kind: 'pull_request', number: 7, base_sha: base, head_sha: head, repository_id: '1', files: [binary,
    { filename: 'source.proto', status: 'added' }, { filename: 'generate.js', status: 'added' }] };
  const collected = await collectBundle(github, target, trigger);
  assert.equal(collected.bundle.files.length, 3);
  assert.equal(collected.bundle.files[0].after.bytes, payload.length);
  const receipt = await produceReview({ github, target, trigger, detailsUrl: 'https://example.com/run',
    createReviewInput, engineVersion: 'binary-test', engine: async input => parse(input) });
  assert.equal(receipt.conclusion, 'success');
  assert.match(output.summary, /does not prove regeneration/);
  assert.match(output.summary, /Binary provenance: artifact.binpb — source-reviewed/);
});

test('binary partition manifests, coverage and synthesis preserve provenance evidence', async () => {
  const files = [bundleFile(binary, Buffer.from([0]), payload), ...Array.from({ length: 6 }, (_, i) =>
    bundleFile({ filename: `src/${i}.js`, status: 'added' }, null, 'x'.repeat(5000) + '\n'))];
  const input = createReviewInput(bundle(files));
  const size = value => Buffer.byteLength(JSON.stringify(value));
  const parts = planReview(input, size, 16000);
  assert.ok(parts.length > 1);
  assert.ok(parts.some(p => p.files.some(f => f.path === binary.filename)));
  assert.ok(parts.every(p => p.manifest[0].after.sha256 === digest(payload)));
  let synthesis;
  const result = await reviewPartitions(input, { requestBytes: size, byteBudget: 16000,
    execute: async part => {
      if (part.scope.kind === 'synthesis') {
        synthesis = part;
        assert.ok(part.references.some(e => e.path === 'generate.js'));
      }
      return parse(part, { binary_reviews: (part.scope.kind === 'synthesis' ? part.manifest : part.files)
        .some(f => f.source_mode === 'binary-metadata') ? [provenance] : [] });
    } });
  assert.ok(synthesis);
  assert.equal(result.status, 'complete');
  assert.deepEqual(result.binary_reviews, [provenance]);
});
