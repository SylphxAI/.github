import { digest, reviewerVersion, unavailable, canonical } from './contract.mjs';
import { literalSegments, changedRanges } from './source.mjs';
import { digest as sourceDigest } from './github.mjs';

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
  if (!repeated.length) for (const c of context) pending.push({ context: { path: c.path,
    sha256: sourceDigest(c.content), bytes: Buffer.byteLength(c.content), segments: literalSegments(c.content) } });
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
    else if (unit.context.segments.length > 1) {
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

export async function reviewPartitions(input, { execute, requestBytes, byteBudget, current = async () => true }) {
  let parts;
  try { parts = planReview(input, requestBytes, byteBudget); }
  catch (error) { return unavailable(input, error.message); }
  if (parts.length === 1) {
    if (!await current()) return unavailable(input, 'Head superseded before review inference.');
    try { const result = await execute(parts[0]); return { ...result, parts: { planned: 1, completed: result.status === 'complete' ? 1 : 0 } }; }
    catch { return unavailable(input, 'Review could not complete.'); }
  }
  const completed = [], failures = [], completedIndexes = new Set();
  for (let i = 0; i < parts.length; i++) {
    if (!await current()) { failures.push('Head superseded before the next review part.'); break; }
    let result;
    try { result = await execute(parts[i]); }
    catch { result = unavailable(parts[i], 'Review part could not complete.'); }
    if (result.status !== 'complete') { failures.push(`Part ${i + 1}/${parts.length}: ${result.reason}`); break; }
    completed.push(result);
    completedIndexes.add(i);
  }
  const base = { ...unavailable(input, ''), status: 'incomplete', findings: unionFindings(completed),
    context_notes: completed.flatMap(r => r.context_notes ?? []),
    parts: { planned: parts.length, completed: completed.length },
    omitted_paths: input.files.filter(f => parts.some((p, i) => p.files.some(pf => pf.path === f.path) && !completedIndexes.has(i))).map(f => f.path) };
  if (failures.length) return { ...base, reason: failures.join(' ').slice(0, 2000) };
  const summaries = completed.map((r, i) => ({ part: i + 1,
    paths: parts[i].files.map(f => f.path), summary: r.review_summary,
    related_evidence: r.related_evidence, findings: r.findings, context_notes: r.context_notes }));
  const synthesis = seal({ ...bare(input), files: [], context: [], manifest: manifest(input),
    source_digest: input.input_digest, scope: { kind: 'synthesis' }, reviewed_parts: summaries,
    references: completed.flatMap(r => [...r.related_evidence, ...r.findings.flatMap(f => f.evidence)]) });
  if (requestBytes(synthesis) > byteBudget) return { ...base, reason: 'All source parts completed, but cross-file synthesis exceeds the configured model context.' };
  const result = await execute(synthesis);
  if (result.status !== 'complete') return { ...base, reason: `Cross-file synthesis: ${result.reason}` };
  const findings = unionFindings([...completed, result]);
  // Every synthesized anchor must still be a real changed source coordinate.
  if (findings.some(f => !input.files.some(file => file.path === f.path && changedRanges(file)
    .some(r => r.side === f.side && f.start_line >= r.start && f.end_line <= r.end)))) {
    return { ...base, reason: 'Cross-file synthesis contains an invalid changed-source anchor.' };
  }
  return { ...result, status: 'complete', head_sha: input.head_sha, base_sha: input.base_sha,
    input_digest: input.input_digest, reviewer_version: input.reviewer_version, findings,
    context_notes: [...base.context_notes, ...result.context_notes], parts: base.parts };
}
