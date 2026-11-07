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

test('binary provenance can be resolved from changed source and generator in later partitions', async () => {
  const files = [bundleFile(binary, Buffer.from([0]), payload),
    bundleFile({ filename: 'source.proto', status: 'added' }, null, source + Array.from({ length: 200 }, (_, i) => `field_${i}_${"x".repeat(100)}\n`).join('')),
    bundleFile({ filename: 'generate.js', status: 'added' }, null, generator)];
  const input = createReviewInput({ ...bundle(files), context: [] });
  const size = value => Buffer.byteLength(JSON.stringify(value));
  const budget = 19000, parts = planReview(input, size, budget);
  assert.ok(parts.length > 2);
  assert.deepEqual(parts[0].files.map(f => f.path), [binary.filename]);
  let calls = 0, synthesis;
  const result = await reviewPartitions(input, { requestBytes: size, byteBudget: budget,
    execute: async part => {
      calls++;
      if (part.scope.kind === 'synthesis') {
        synthesis = part;
        assert.ok(part.references.some(e => e.path === 'source.proto'));
        assert.ok(part.references.some(e => e.path === 'generate.js'));
        assert.equal(parse(part, { binary_reviews: [{ ...provenance, status: 'unresolved' }] }).status, 'unavailable');
        return parse(part);
      }
      const related_evidence = [];
      for (const quote of [...provenance.source_evidence, ...provenance.derivation_evidence]) {
        if (part.files.some(f => f.path === quote.path && fileSegments(f, 'after').some(s => s.start_line === 1))) related_evidence.push(quote);
      }
      const binary_reviews = part.files.some(f => f.path === binary.filename)
        ? [{ ...provenance, status: 'unresolved', source_evidence: [], derivation_evidence: [] }] : [];
      const parsed = parse(part, { related_evidence, binary_reviews });
      assert.equal(parsed.status, 'complete');
      return parsed;
    } });
  assert.equal(calls, parts.length + 1);
  assert.ok(synthesis);
  assert.equal(result.status, 'complete');
  assert.deepEqual(result.binary_reviews, [provenance]);
});

test('binary-only producer recovers pinned text provenance without refetching binary bytes', async () => {
  const github = provider(payload), reads = [], inputs = [];
  github.current = async () => true;
  const original = github.source.bind(github);
  github.source = async (path, sha, optional, options) => {
    reads.push({ path, sha });
    if (path === 'source.proto') return source;
    if (path === 'generate.js') return generator;
    if (path !== binary.filename && optional) return null;
    return original(path, sha, optional, options);
  };
  github.begin = async () => ({ id: 1, head_sha: head, app: { id: APP_ID }, name: CHECK_NAME });
  github.finish = async (run, conclusion) => ({ ...run, conclusion });
  const target = { kind: 'pull_request', number: 7, base_sha: base, head_sha: head, repository_id: '1', files: [binary] };
  const receipt = await produceReview({ github, target, trigger, createReviewInput, engineVersion: 'binary-test',
    engine: async input => {
      inputs.push(input);
      if (inputs.length === 1) {
        assert.equal(input.context.length, 0);
        return parse(input, { completion: 'incomplete', omitted_paths: [binary.filename], reason: 'Need original source and generator.',
          binary_reviews: [{ ...provenance, status: 'unresolved', source_evidence: [], derivation_evidence: [] }],
          context_requests: [{ path: 'source.proto', side: 'head' }, { path: 'generate.js', side: 'head' }] });
      }
      assert.deepEqual(input.context, [{ path: 'source.proto', side: 'head', commit_sha: head, content: source },
        { path: 'generate.js', side: 'head', commit_sha: head, content: generator }]);
      return parse(input);
    } });
  assert.equal(receipt.conclusion, 'success');
  assert.equal(inputs.length, 2);
  assert.notEqual(inputs[0].input_digest, inputs[1].input_digest);
  assert.equal(receipt.input_digest, inputs[1].input_digest);
  assert.deepEqual(reads.filter(r => r.path === binary.filename), [{ path: binary.filename, sha: base }, { path: binary.filename, sha: head }]);
  assert.deepEqual(reads.filter(r => ['source.proto', 'generate.js'].includes(r.path)),
    [{ path: 'source.proto', sha: head }, { path: 'generate.js', sha: head }]);
});

const boundarySource = '  // A complete fictional example request in wire (snake_case) JSON. Path\n' +
  '  // variables default to the generated fictional resource names. Use when\n' +
  '  // required-field placeholders cannot express conditional input modes.\n' +
  '  string example_request_json = 15;\n';
const boundaryPath = 'options.proto';
function boundaryReview(quote, { source = boundarySource, start = 5536, end = 5536 + Buffer.byteLength(source),
  kind = 'binary_source_evidence', endLine = 5 } = {}) {
  const content = ' '.repeat(5535) + '\n' + source;
  const input = createReviewInput(bundle([bundleFile(binary, Buffer.from([0]), payload),
    bundleFile({ filename: boundaryPath, status: 'added' }, null, content)]));
  const e = { path: boundaryPath, side: 'head', start_line: 2, end_line: endLine, start_byte: start, end_byte: end, quote };
  const review = structuredClone(provenance), extra = { binary_reviews: [review] };
  if (kind === 'binary_source_evidence') review.source_evidence = [e];
  else if (kind === 'binary_derivation_evidence') review.derivation_evidence = [e];
  else if (kind === 'related_evidence') extra.related_evidence = [e];
  else extra.findings = [{ path: boundaryPath, side: 'head', start_line: 2, end_line: 5,
    severity: 'low', title: 'Example defect', explanation: 'The changed example triggers a defect.',
    trigger: 'Reading the example.', consequence: 'Incorrect example output.', evidence: [e] }];
  return parse(input, extra);
}

for (const kind of ['binary_source_evidence', 'binary_derivation_evidence', 'related_evidence', 'finding_evidence']) {
  test(`byte-qualified evidence accepts only one terminal LF difference (${kind})`, () => {
    assert.equal(Buffer.byteLength(boundarySource), 258);
    assert.equal(digest(boundarySource), '60e6a76b319d420ba9f5f487de3b9251603e95cea49fe7599cf4ebed83d4535f');
    const withoutLF = boundarySource.slice(0, -1);
    assert.equal(Buffer.byteLength(withoutLF), 257);
    assert.equal(digest(withoutLF), 'af0f7d52219ad83e278d775950648c09187426b50d4a841ddcb9442e9a7dc9fa');
    for (const [quote, end, operation, expected] of [[withoutLF, 5794, 'add', boundarySource],
      [boundarySource, 5793, 'remove', withoutLF]]) {
      const result = boundaryReview(quote, { kind, end });
      assert.equal(result.status, 'complete', result.reason);
      assert.deepEqual(result.snapped_evidence, [{ kind, evidence_index: 0, finding_index: kind === 'finding_evidence' ? 0 : null,
        path: boundaryPath, side: 'head', reason: 'trailing_lf', operation,
        start_line: 2, end_line: 5, start_byte: 5536, end_byte: end,
        expected_bytes: Buffer.byteLength(expected), returned_bytes: Buffer.byteLength(quote) }]);
      const normalized = kind === 'binary_source_evidence' ? result.binary_reviews[0].source_evidence[0] :
        kind === 'binary_derivation_evidence' ? result.binary_reviews[0].derivation_evidence[0] :
          kind === 'related_evidence' ? result.related_evidence[0] : result.findings[0].evidence[0];
      assert.equal(normalized.quote, expected);
      assert.equal(normalized.start_byte, 5536);
      assert.equal(normalized.end_byte, end);
    }
    const exact = boundaryReview(boundarySource, { kind });
    assert.equal(exact.status, 'complete', exact.reason);
    assert.equal(exact.snapped_evidence, undefined);
  });
}

for (const [name, quote, options = {}, code = 'quote_mismatch'] of [
  ['changed byte', boundarySource.replace('wire', 'tire')],
  ['interior CRLF', boundarySource.replace('Path\n', 'Path\r\n')],
  ['leading whitespace added', ' ' + boundarySource],
  ['leading whitespace removed', boundarySource.slice(1)],
  ['two trailing LFs added', boundarySource.slice(0, -1) + '\n\n', { end: 5793 }],
  ['one LF added to an existing LF', boundarySource + '\n'],
  ['one LF removed from two trailing LFs', boundarySource, { source: boundarySource + '\n', endLine: 6 }],
  ['non-LF truncation', boundarySource.slice(0, -2)],
  ['interior byte removed', boundarySource.replace('wire', 'wre')],
  ['LF removed and byte changed', boundarySource.slice(0, -1).replace('wire', 'tire')],
  ['end offset outside file', boundarySource.slice(0, -1), { end: 5795 }, 'source_unavailable'],
  ['start offset outside file', boundarySource.slice(0, -1), { start: 5795, end: 5796 }, 'source_unavailable'],
]) test(`byte-qualified evidence refuses ${name}`, () => {
  const result = boundaryReview(quote, options);
  assert.equal(result.status, 'unavailable');
  assert.equal(result.validation_failure.kind, 'binary_source_evidence');
  assert.equal(result.validation_failure.code, code);
  assert.equal(result.snapped_evidence, undefined);
});
