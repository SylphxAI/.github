import { contextRequestLimit } from './schema.mjs';
import { validPath } from './github.mjs';
import { fileSegments, segmentWindows } from './source.mjs';

// Recovery is evidence collection, not an inference retry. Its fixed total
// bounds share the original deadline and each call's qualified wire budget.
export const recoveryLimits = { rounds: 3, files: contextRequestLimit, bytes: 1024 * 1024 };

export async function recoverContext({ input, github, target, execute, createReviewInput,
  validateResult, onResult, deadlineMs = Infinity }) {
  const complete = (bytes, segments) => {
    const windows = segmentWindows(segments);
    return bytes === 0 || (windows.length === 1 && windows[0].start_byte === 0 && windows[0].end_byte === bytes);
  };
  const seen = new Set(input.context.filter(c => c.side && (typeof c.content === 'string' || complete(c.bytes, c.segments)))
    .map(c => `${c.side}:${c.path}`));
  for (const file of input.files) for (const [side, sourceSide] of [['base', 'before'], ['head', 'after']]) {
    const metadata = file[sourceSide];
    if (!metadata) continue;
    if (complete(metadata.bytes, fileSegments(file, sourceSide))) {
      seen.add(`${side}:${side === 'base' ? file.previous_path ?? file.path : file.path}`);
    }
  }
  const diagnostics = [], sizes = [];
  let rounds = 0, added = 0, bytes = 0, result;
  const record = async stop => {
    result = { ...result, recovery: { rounds, added_files: added, added_bytes: bytes, stop },
      recovery_diagnostics: diagnostics, request_bytes: [...sizes] };
    await onResult?.(result);
  };
  const stop = async (cause, reason) => {
    result = { ...result, reason: `${result.reason} ${reason}`.trim() };
    await record(cause);
    return { input, result };
  };
  while (true) {
    if (Date.now() >= deadlineMs) {
      result = { status: 'unavailable', head_sha: input.head_sha, base_sha: input.base_sha,
        input_digest: input.input_digest, reviewer_version: input.reviewer_version,
        findings: [], omitted_paths: input.files.map(f => f.path), reason: 'Review deadline expired before source review.' };
      return stop('deadline', '');
    }
    try { result = validateResult(await execute(input), input); }
    catch {
      result = { status: 'unavailable', head_sha: input.head_sha, base_sha: input.base_sha,
        input_digest: input.input_digest, reviewer_version: input.reviewer_version,
        findings: [], omitted_paths: input.files.map(f => f.path), reason: 'Review engine could not return a valid pinned result.' };
    }
    sizes.push(...(result.request_bytes ?? []));
    if (result.diagnostics) diagnostics.push(result.diagnostics);
    await record(result.status === 'incomplete' ? 'pending' : result.status);
    if (result.status !== 'incomplete') return { input, result };
    const requests = result.context_requests ?? [];
    if (!requests.length) return stop('no_requests', 'No exact-source context was requested.');
    if (rounds >= recoveryLimits.rounds) return stop('round_limit', 'Context recovery round limit reached.');
    if (requests.length > recoveryLimits.files || requests.some(r => !validPath(r.path) ||
        r.path.length > 1024 || !['base', 'head'].includes(r.side))) {
      return stop('invalid_request', 'Context recovery request is invalid.');
    }
    const fresh = [...new Map(requests.map(r => [`${r.side}:${r.path}`, r])).entries()].filter(([key]) => !seen.has(key));
    if (!fresh.length) return stop('no_new_context', 'Requested exact-source context was already supplied.');
    if (added + fresh.length > recoveryLimits.files) return stop('file_limit', 'Context recovery file limit reached.');
    const context = [];
    for (const [key, request] of fresh) {
      if (Date.now() >= deadlineMs) return stop('deadline', 'Review deadline expired during context recovery.');
      let current;
      try { current = await github.current(target, { deadlineMs }); } catch { current = null; }
      if (current !== true) return stop('head_unverified', 'Current head changed or could not be verified during context recovery.');
      const commit_sha = input[`${request.side}_sha`];
      let content;
      try { content = await github.source(request.path, commit_sha, true,
        { deadlineMs, maxBytes: Math.min(recoveryLimits.bytes, result.budget?.request_byte_limit ?? Infinity) - bytes }); }
      catch { return stop('source_unavailable', 'Requested pinned context could not be collected.'); }
      if (content === null) return stop('source_absent', 'Requested pinned context is absent.');
      bytes += Buffer.byteLength(content);
      if (bytes > Math.min(recoveryLimits.bytes, result.budget?.request_byte_limit ?? Infinity)) return stop('byte_limit', 'Context recovery byte limit reached.');
      seen.add(key);
      context.push({ path: request.path, side: request.side, commit_sha, content });
    }
    let current;
    try { current = await github.current(target, { deadlineMs }); } catch { current = null; }
    if (current !== true) return stop('head_unverified', 'Current head changed or could not be verified after context recovery.');
    if (Date.now() >= deadlineMs) return stop('deadline', 'Review deadline expired during context recovery.');
    const next = createReviewInput({ ...input, context: [...input.context.filter(c => !context.some(n => n.path === c.path && n.side === c.side)), ...context] });
    if (next.input_digest === input.input_digest) return stop('no_new_context', 'Context recovery did not change the pinned evidence.');
    input = next;
    added += context.length;
    rounds++;
  }
}
