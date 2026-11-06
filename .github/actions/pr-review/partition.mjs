import { digest, reviewerVersion, unavailable, canonical, discardedItems } from './contract.mjs';
import { literalSegments, changedRanges, fileSegments } from './source.mjs';
import { digest as sourceDigest } from './github.mjs';
import { recoveryLimits } from './recovery.mjs';

export const seal = value => ({ ...value, input_digest: digest(value), reviewer_version: reviewerVersion });
const bare = input => { const { input_digest, reviewer_version, ...value } = input; return value; };

function manifest(input) {
  return input.files.map(({ hunks, ...file }) => ({ ...file, hunks: hunks.map(({ rows, ...hunk }) => ({
    ...hunk, rows: rows.length, digest: digest(rows),
  })) }));
}

function splitFile(file) {
  if (file.hunks.length > 1) {
    const at = Math.ceil(file.hunks.length / 2);
    return [file.hunks.slice(0, at), file.hunks.slice(at)].map(hunks => ({ ...file, hunks }));
  }
  const hunk = file.hunks[0];
  if (hunk.rows.length <= 1) return null;
  const at = Math.ceil(hunk.rows.length / 2), offset = hunk.first_row ?? 0;
  return [hunk.rows.slice(0, at), hunk.rows.slice(at)].map((rows, i) => ({ ...file,
    hunks: [{ ...hunk, first_row: offset + (i ? at : 0), total_rows: hunk.total_rows ?? hunk.rows.length, rows }] }));
}

export function planReview(input, requestBytes, byteBudget) {
  if (requestBytes(input) <= byteBudget) return [input];
  const all = manifest(input), { files, context, ...identity } = bare(input);
  const common = { ...identity, source_digest: input.input_digest, manifest: all,
    scope: { kind: 'partition', index: '00000000', total: '00000000' } };
  const make = (files, context) => {
    const merged = new Map();
    for (const file of files) {
      if (merged.has(file.path)) merged.get(file.path).hunks.push(...file.hunks);
      else merged.set(file.path, { ...file, hunks: [...file.hunks] });
    }
    return seal({ ...common, files: [...merged.values()], context });
  };
  if (requestBytes(make([], [])) > byteBudget) throw new Error('The complete changed-path manifest exceeds the configured model context.');
  // Instructions normally accompany every part. If they alone do not fit,
  // distribute every literal context fragment as additional review units.
  const repeated = requestBytes(make([], context)) < byteBudget && input.files.every(f => {
    const largest = f.hunks.flatMap(h => h.rows.map(row => ({ h, row, bytes: Buffer.byteLength(JSON.stringify(row)) })))
      .reduce((a, b) => !a || b.bytes > a.bytes ? b : a, null);
    return !largest || requestBytes(make([{ ...f, hunks: [{ ...largest.h, rows: [largest.row], first_row: 0, total_rows: largest.h.rows.length }] }], context)) <= byteBudget;
  }) ? context : [];
  const pending = input.files.map(file => ({ file }));
  if (!repeated.length && context.length) {
    common.scope = { ...common.scope,
      partitioned_context: context.map(c => ({ path: c.path, side: c.side ?? 'context',
        ...(c.commit_sha ? { commit_sha: c.commit_sha } : {}),
        ...(c.absent === true ? { absent: true } : { bytes: c.bytes ?? Buffer.byteLength(c.content) }) })),
      note: 'Context paths in partitioned_context are already supplied in other partitions of this review scope and will be joined in cross-file synthesis; do not request them in context_requests or mark this partition incomplete for their absence here.' };
    if (requestBytes(make([], [])) > byteBudget) throw new Error('The complete changed-path manifest exceeds the configured model context.');
  }
  if (!repeated.length) for (const c of context) {
    const { content, ...identity } = c;
    pending.push({ context: c.absent === true || c.segments ? c : { ...identity,
      sha256: sourceDigest(content), bytes: Buffer.byteLength(content), segments: literalSegments(content) } });
  }
  const parts = [];
  let partFiles = [], partContext = [...repeated];
  const flush = () => { parts.push(make(partFiles, partContext)); partFiles = []; partContext = [...repeated]; };
  for (let at = 0; at < pending.length; at++) {
    const unit = pending[at];
    const nextFiles = unit.file ? [...partFiles, unit.file] : partFiles;
    const nextContext = unit.context ? [...partContext, unit.context] : partContext;
    if (requestBytes(make(nextFiles, nextContext)) <= byteBudget) {
      partFiles = nextFiles; partContext = nextContext; continue;
    }
    if (partFiles.length || partContext.length > repeated.length) { flush(); at--; continue; }
    let split;
    if (unit.file) split = splitFile(unit.file)?.map(file => ({ file }));
    else if (unit.context.segments?.length > 1) {
      const middle = Math.ceil(unit.context.segments.length / 2);
      split = [unit.context.segments.slice(0, middle), unit.context.segments.slice(middle)]
        .map(segments => ({ context: { ...unit.context, segments } }));
    }
    if (!split) throw new Error('A source fragment and the complete manifest exceed the configured model context.');
    pending.splice(at, 1, ...split); at--;
  }
  if (partFiles.length || partContext.length > repeated.length) flush();
  return parts.map((part, i) => {
    const value = seal({ ...bare(part), scope: { ...common.scope,
      index: String(i + 1).padStart(8, '0'), total: String(parts.length).padStart(8, '0') } });
    if (requestBytes(value) > byteBudget) throw new Error('Final partition exceeds the configured model context.');
    return value;
  });
}

export function unionFindings(results) {
  return [...new Map(results.flatMap(r => r.findings).map(f => [canonical(f), f])).values()];
}

function requestedSource(input, request) {
  if (!['base', 'head'].includes(request.side)) return null;
  const context = input.context.find(c => c.path === request.path && c.side === request.side && c.absent !== true);
  if (context) return context;
  const side = request.side === 'base' ? 'before' : 'after';
  const file = input.files.find(f => (request.side === 'base' ? f.previous_path ?? f.path : f.path) === request.path);
  const metadata = file?.[side];
  if (!metadata || metadata.encoding === 'binary') return null;
  const segments = fileSegments(file, side);
  if (metadata.bytes > 0 && !segments.length) return null;
  return { path: request.path, side: request.side, commit_sha: input[`${request.side}_sha`],
    sha256: metadata.sha256, bytes: metadata.bytes, segments };
}

export async function reviewPartitions(input, { execute, requestBytes, byteBudget, current = async () => true, completedParts }) {
  const isCurrent = async () => { try { return await current(); } catch { return null; } };
  let parts;
  try { parts = planReview(input, requestBytes, byteBudget); }
  catch (error) { return unavailable(input, error.message); }
  if (parts.length === 1) {
    const head = await isCurrent();
    if (head !== true) return unavailable(input, head === false ? 'Head superseded before review inference.' : 'Current head could not be verified before review inference.');
    try { const result = await execute(parts[0]); return { ...result, parts: { planned: 1, completed: result.status === 'complete' ? 1 : 0 } }; }
    catch { return unavailable(input, 'Review could not complete.'); }
  }
  const completed = [], failures = [], completedIndexes = new Set();
  let interrupted;
  for (let i = 0; i < parts.length; i++) {
    const partKey = `${i}:${parts.length}:${digest(parts[i].files)}:${parts[i].files.length ? '' : digest(parts[i].context)}`;
    if (completedParts?.has(partKey)) {
      completed.push({ ...completedParts.get(partKey), input_digest: parts[i].input_digest });
      completedIndexes.add(i);
      continue;
    }
    const head = await isCurrent();
    if (head !== true) { failures.push(head === false ? 'Head superseded before the next review part.' : 'Current head could not be verified before the next review part.'); break; }
    let result;
    try { result = await execute(parts[i]); }
    catch { result = unavailable(parts[i], 'Review part could not complete.'); }
    if (result.status !== 'complete') { interrupted = result; failures.push(`Part ${i + 1}/${parts.length}: ${result.reason}`); break; }
    completedParts?.set(partKey, result);
    completed.push(result);
    completedIndexes.add(i);
  }
  const snappedFromParts = completed.flatMap(r => r.snapped_evidence ?? []);
  const base = { ...unavailable(input, ''), status: 'incomplete', findings: unionFindings([...completed, ...(interrupted ? [interrupted] : [])]),
    context_notes: completed.flatMap(r => r.context_notes ?? []),
    discarded_items: discardedItems([...completed, ...(interrupted ? [interrupted] : [])]),
    ...(snappedFromParts.length ? { snapped_evidence: snappedFromParts } : {}),
    parts: { planned: parts.length, completed: completed.length },
    omitted_paths: input.files.filter(f => parts.some((p, i) => p.files.some(pf => pf.path === f.path) && !completedIndexes.has(i))).map(f => f.path) };
  if (failures.length) return { ...base, status: interrupted?.status === 'incomplete' ? 'incomplete' : 'unavailable', reason: failures.join(' ').slice(0, 2000),
    context_requests: interrupted?.status === 'incomplete' ? interrupted.context_requests ?? [] : [],
    ...(interrupted?.terminal_invalid ? { terminal_invalid: true } : {}),
    ...(interrupted?.diagnostics ? { diagnostics: interrupted.diagnostics } : {}),
    ...(interrupted?.validation_failure ? { validation_failure: interrupted.validation_failure } : {}) };
  const summaries = completed.map((r, i) => ({ part: i + 1,
    paths: parts[i].files.map(f => f.path), summary: r.review_summary,
    related_evidence: r.related_evidence, binary_reviews: r.binary_reviews, findings: r.findings, context_notes: r.context_notes }));
  const synthesis = seal({ ...bare(input), files: [], context: input.context.filter(c => c.absent === true), manifest: manifest(input),
    source_digest: input.input_digest, scope: { kind: 'synthesis' }, reviewed_parts: summaries,
    references: completed.flatMap(r => [...r.related_evidence, ...r.findings.flatMap(f => f.evidence),
      ...(r.binary_reviews ?? []).flatMap(b => [...b.source_evidence, ...b.derivation_evidence])]) });
  if (requestBytes(synthesis) > byteBudget) return { ...base, reason: 'All source parts completed, but cross-file synthesis exceeds the configured model context.' };
  const head = await isCurrent();
  if (head !== true) return { ...base, status: 'unavailable', reason: head === false ? 'Head superseded before cross-file synthesis.' : 'Current head could not be verified before cross-file synthesis.' };
  let result;
  try { result = await execute(synthesis, { files: input.files, context: input.context }); }
  catch { result = unavailable(synthesis, 'Cross-file synthesis could not complete.'); }
  // A synthesis initially receives summaries, not the source already reviewed in
  // other parts. Supply only its requested pinned source, without recollecting it
  // or putting the full validation bundle on the wire.
  for (let round = 0; result.status === 'incomplete' && round < recoveryLimits.rounds; round++) {
    if (input.context_recovery === 'exhausted') break;
    const requests = result.context_requests ?? [];
    if (!requests.length) break;
    const requested = [...new Map(requests.map(r => [canonical([r.path, r.side]), r])).values()]
      .map(r => requestedSource(input, r));
    if (requested.some(c => c === null)) break;
    const next = seal({ ...bare(synthesis), context: [...synthesis.context, ...requested] });
    if (requestBytes(next) > byteBudget) break;
    const head = await isCurrent();
    if (head !== true) return { ...base, status: 'unavailable', reason: head === false ? 'Head superseded before cross-file synthesis.' : 'Current head could not be verified before cross-file synthesis.' };
    try { result = await execute(next, { files: input.files, context: input.context }); }
    catch { result = unavailable(next, 'Cross-file synthesis could not complete.'); }
  }
  if (result.status !== 'complete') return { ...base, status: result.status, discarded_items: discardedItems([...completed, result]), findings: unionFindings([...completed, result]), reason: `Cross-file synthesis: ${result.reason}`,
    context_requests: result.status === 'incomplete' ? result.context_requests ?? [] : [],
    ...(result.terminal_invalid ? { terminal_invalid: true } : {}),
    ...(result.diagnostics ? { diagnostics: result.diagnostics } : {}),
    ...(result.validation_failure ? { validation_failure: result.validation_failure } : {}) };
  const findings = unionFindings([...completed, result]);
  // Every synthesized anchor must still be a real changed source coordinate.
  if (findings.some(f => !input.files.some(file => file.path === f.path && changedRanges(file)
    .some(r => r.side === f.side && f.start_line >= r.start && f.end_line <= r.end)))) {
    return { ...base, reason: 'Cross-file synthesis contains an invalid changed-source anchor.' };
  }
  const snappedAll = [...snappedFromParts, ...(result.snapped_evidence ?? [])];
  return { ...result, status: 'complete', head_sha: input.head_sha, base_sha: input.base_sha,
    input_digest: input.input_digest, reviewer_version: input.reviewer_version, findings,
    context_notes: [...base.context_notes, ...result.context_notes], parts: base.parts,
    discarded_items: discardedItems([...completed, result]),
    ...(snappedAll.length ? { snapped_evidence: snappedAll } : {}) };
}
