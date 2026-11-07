import { APP_ID, CHECK_NAME, Unavailable, digest, validSha, validPath, bundleFile } from './github.mjs';
import { changedRanges } from './source.mjs';
import { spawnSync } from 'node:child_process';
import { posix } from 'node:path';
import { recoverContext } from './recovery.mjs';

function glob(pattern) {
  let out = '^';
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === '*' && pattern[i + 1] === '*') {
      i++;
      if (pattern[i + 1] === '/') { i++; out += '(?:.*/)?'; }
      else out += '.*';
    } else if (c === '*') out += '[^/]*';
    else if (c === '?') out += '[^/]';
    else out += c.replace(/[\\^$.*+?()[\]{}|]/g, '\\$&');
  }
  return new RegExp(`${out}$`);
}

export function parsePolicy(content) {
  if (content === null) return { version: 'missing', tier0: { paths: [] }, generated: { paths: [] }, review: { paths: [] } };
  const parsed = spawnSync('python3', ['-c', 'import json,sys,tomllib;print(json.dumps(tomllib.loads(sys.stdin.read())))'],
    { input: content, encoding: 'utf8', maxBuffer: 1024 * 1024 });
  if (parsed.status !== 0) throw new Unavailable('Base review policy is invalid TOML.');
  const policy = JSON.parse(parsed.stdout);
  if (!Number.isInteger(policy.version) || policy.version < 1) throw new Unavailable('Base review policy has no valid version.');
  for (const section of ['tier0', 'generated', 'review']) {
    if (!Array.isArray(policy[section]?.paths) || policy[section].paths.some(p => typeof p !== 'string')) {
      throw new Unavailable(`Base review policy has invalid ${section} paths.`);
    }
  }
  return policy;
}

export function needsReview(policy, paths) {
  const matches = (section, path) => (policy[section]?.paths ?? []).some(p => glob(p).test(path));
  return paths.some(path => path === '.github/review-policy.toml' || matches('review', path) ||
    !(matches('tier0', path) || matches('generated', path)));
}

export async function collectPullRequest(github, number, expectedHead) {
  const pr = await github.request(`/pulls/${number}`);
  if (pr.state !== 'open') throw new Unavailable('Pull request is closed.');
  if (!validSha(pr.head.sha) || !validSha(pr.base.sha) || (expectedHead && pr.head.sha !== expectedHead)) {
    throw new Unavailable('Pull request head changed before collection.');
  }
  const files = await github.pages(`/pulls/${number}/files`);
  if (files.length !== pr.changed_files || files.length === 0) throw new Unavailable('Changed-file population is incomplete or empty.');
  const comparison = await github.request(`/compare/${pr.base.sha}...${pr.head.sha}`);
  if (!validSha(comparison.merge_base_commit?.sha)) throw new Unavailable('No pinned merge base.');
  const target = { kind: 'pull_request', number, base_sha: comparison.merge_base_commit.sha,
    policy_sha: pr.base.sha, base_ref: pr.base.ref, head_sha: pr.head.sha,
    review_requested: /\[review\]/i.test(pr.title ?? ''),
    repository_id: String(pr.base.repo.id), files };
  if (!await github.current(target)) throw new Unavailable('Pull request changed during file collection.');
  return target;
}

export async function collectBundle(github, target, trigger) {
  const policyText = await github.source('.github/review-policy.toml', target.policy_sha ?? target.base_sha, true);
  const policy = parsePolicy(policyText);
  const paths = target.files.flatMap(f => [f.filename, f.previous_filename].filter(Boolean));
  if (!paths.every(validPath)) throw new Unavailable('Invalid changed-file path.');
  const needs_review = target.review_requested || needsReview(policy, paths);
  if (!needs_review) return { needs_review: false };
  const files = [];
  // A narrow sequential stream respects provider admission and avoids holding
  // the entire large repository source in memory at once.
  for (const file of target.files) {
    if (!['added', 'modified', 'removed', 'renamed', 'copied'].includes(file.status)) {
      throw new Unavailable(`Unsupported change kind for ${file.filename}.`);
    }
    const before = file.status === 'added' ? null : await github.source(file.previous_filename ?? file.filename, target.base_sha, false, { allowBinary: true });
    const after = file.status === 'removed' ? null : await github.source(file.filename, target.head_sha, false, { allowBinary: true });
    files.push(bundleFile(file, before, after));
  }
  const context = [];
  if (policyText !== null) context.push({ path: '.github/review-policy.toml', content: policyText });
  // Base-side instructions are context, never authority to change credentials,
  // verdict rules, or the pinned reviewer definition.
  const instructionPaths = new Set(['AGENTS.md']);
  for (const path of paths) {
    let directory = posix.dirname(path);
    while (directory !== '.') { instructionPaths.add(`${directory}/AGENTS.md`); directory = posix.dirname(directory); }
  }
  for (const path of instructionPaths) {
    const content = await github.source(path, target.policy_sha ?? target.base_sha, true);
    if (content !== null) context.push({ path, content });
  }
  if (!await github.current(target)) throw new Unavailable('Head changed during source collection.');
  return { needs_review, bundle: { schema_version: 2,
    repository: github.repository, repository_id: target.repository_id,
    base_sha: target.base_sha, head_sha: target.head_sha, policy_version: String(policy.version), trigger, files, context } };
}

export function validateResult(result, input) {
  if (!['complete', 'incomplete', 'unavailable'].includes(result.status) || result.head_sha !== input.head_sha || result.base_sha !== input.base_sha ||
      result.input_digest !== input.input_digest || result.reviewer_version !== input.reviewer_version ||
      !Array.isArray(result.findings) || !Array.isArray(result.omitted_paths) ||
      (result.status === 'complete' && (result.omitted_paths.length || result.reason !== ''))) {
    throw new Unavailable(result.reason || 'Review result is incomplete or does not match the pinned input.');
  }
  for (const finding of result.findings) {
    const file = input.files.find(f => f.path === finding.path);
    const ranges = file && changedRanges(file);
    if (!file || !Number.isInteger(finding.start_line) || !Number.isInteger(finding.end_line) ||
        finding.start_line < 1 || finding.end_line < finding.start_line ||
        !['base', 'head'].includes(finding.side) ||
        !ranges.some(r => r.side === finding.side && finding.start_line >= r.start && finding.end_line <= r.end) ||
        !['critical', 'high', 'medium', 'low'].includes(finding.severity) ||
        !['title', 'explanation', 'trigger', 'consequence'].every(k => typeof finding[k] === 'string' && finding[k].trim()) ||
        !Array.isArray(finding.evidence) || finding.evidence.length === 0) {
      throw new Unavailable('Review finding is not anchored to the pinned diff with concrete evidence.');
    }
  }
  return result;
}

function outputFor(result, input) {
  const blocking = result.findings.filter(f => ['critical', 'high', 'medium'].includes(f.severity));
  const summary = [`Head: \`${input.head_sha}\``, `Reviewer: \`${input.reviewer_version}\``,
    `Source digest: \`${input.input_digest}\``, `${result.findings.length} findings; ${blocking.length} blocking.`];
  if (input.files.some(f => f.source_mode === 'binary-metadata')) summary.push('Binary content is represented by verified byte counts and hashes, not decoded bytes. Source/derivation review does not prove regeneration or binary semantic equivalence.');
  for (const review of result.binary_reviews ?? []) summary.push(`Binary provenance: ${review.path} — ${review.status}. Source: ${review.source_evidence.map(e => e.path).join(', ')}. Derivation: ${review.derivation_evidence.map(e => e.path).join(', ')}. Limitation: ${review.limitation}`);
  if (result.discarded_items?.length) summary.push(`${result.discarded_items.length} items discarded: evidence not in source.`);
  if (result.parts) summary.push(`Source parts: ${result.parts.completed}/${result.parts.planned} complete.`);
  if (result.recovery_sources?.length) summary.push(`Pinned context outcomes (path fingerprints, not source):\n\n\`\`\`json\n${JSON.stringify(result.recovery_sources)}\n\`\`\``);
  if (result.snapped_evidence?.length) summary.push(`Snapped evidence ranges:\n\n\`\`\`json\n${JSON.stringify(result.snapped_evidence)}\n\`\`\``);
  if (result.status !== 'complete') summary.push(`Review incomplete: ${result.reason}\nUnreviewed changed paths: ${result.omitted_paths.length}.`);
  for (const note of result.context_notes ?? []) summary.push(`Context: ${note}`);
  for (const finding of result.findings.filter(f => f.side === 'base')) {
    summary.push(`${finding.severity}: ${finding.title} (${finding.path}, base lines ${finding.start_line}–${finding.end_line})\n${finding.explanation}\nTrigger: ${finding.trigger}\nConsequence: ${finding.consequence}`);
  }
  const annotations = result.findings.filter(f => f.side === 'head').map(f => ({ path: f.path, start_line: f.start_line, end_line: f.end_line,
    annotation_level: ['critical', 'high', 'medium'].includes(f.severity) ? 'failure' : 'notice',
    title: f.title.slice(0, 255), message: `${f.explanation}\nTrigger: ${f.trigger}\nConsequence: ${f.consequence}`.slice(0, 65535) }));
  return { conclusion: blocking.length || result.terminal_incomplete || result.terminal_invalid ? 'failure' : result.status === 'complete' ? 'success' : 'neutral', output: {
    title: blocking.length ? `${blocking.length} defects require a change${result.status === 'complete' ? '' : '; review incomplete'}` : result.status === 'complete' ? 'Review complete' : result.terminal_invalid ? 'Review invalid' : result.terminal_incomplete ? 'Review incomplete' : 'Review unavailable',
    summary: summary.join('\n\n'), annotations,
  } };
}

// This title is authored only by this publisher after the pinned policy check.
export function completedReviewReceipt(run) {
  const outsidePolicy = run.conclusion === 'neutral' && run.output?.title === 'Review outside path policy';
  return { check_run_id: run.id, head_sha: run.head_sha, conclusion: run.conclusion,
    ...(outsidePolicy ? { applicable: false } : run.conclusion !== 'success' ?
      { reason: run.output?.title || 'Review unavailable or incomplete.' } : {}) };
}

export async function produceReview({ github, target, trigger, detailsUrl, engine, createReviewInput, engineVersion, onBegin, onResult, deadlineMs }) {
  const started = Date.now();
  const externalId = `pr-review/v1/${digest(JSON.stringify([github.repository, target.kind, target.number ?? target.ref,
    target.base_sha, target.policy_sha, target.head_sha, target.review_requested, trigger, engineVersion]))}`;
  const begin = async (restartCompleted = false) => {
    const run = await github.begin(target, externalId, detailsUrl, { restartCompleted });
    if (run.app?.id !== APP_ID || run.head_sha !== target.head_sha || run.name !== CHECK_NAME ||
        (restartCompleted && run.status === 'completed')) {
      throw new Unavailable('Publisher did not receive the canonical App Check Run in an active state.');
    }
    if (run.status !== 'completed' && !run.deferred) await onBegin?.(run);
    return run;
  };
  let run = await begin();
  const observedAt = new Date().toISOString();
  if (run.deferred) return { check_run_id: run.id, head_sha: run.head_sha, conclusion: null, deferred: true,
    owner_run_id: run.owner_run_id, reason: 'A live workflow already owns the current-head review.', observed_at: observedAt };
  let collected, initialInput, collectionError;
  try {
    collected = await collectBundle(github, target, trigger);
    if (collected.needs_review) initialInput = createReviewInput(collected.bundle);
  } catch (error) { collectionError = error; }
  if (run.status === 'completed') {
    // Completion alone includes unavailable/cancelled attempts. Only a verdict
    // bound to this freshly collected input can replace another assessment.
    const identity = initialInput && `Head: \`${initialInput.head_sha}\`\n\nReviewer: \`${initialInput.reviewer_version}\`\n\nSource digest: \`${initialInput.input_digest}\`\n\n`;
    if (!collectionError && identity && ['success', 'failure'].includes(run.conclusion) &&
        typeof run.output?.summary === 'string' && run.output.summary.startsWith(identity)) {
      await onBegin?.(run);
      return { ...completedReviewReceipt(run), reused: true, input_digest: initialInput.input_digest,
        started_at: run.started_at, observed_at: observedAt };
    }
    // Keep rejected history immutable and register only the new check with the
    // finalizer. begin() reconciles an accepted in-progress create before POST.
    run = await begin(true);
    if (run.deferred) return { check_run_id: run.id, head_sha: run.head_sha, conclusion: null, deferred: true,
      owner_run_id: run.owner_run_id, reason: 'A live workflow already owns the current-head review.', observed_at: observedAt };
  }
  try {
    if (collectionError) throw collectionError;
    if (!collected.needs_review) {
      const receipt = await github.finish(run, 'neutral', { title: 'Review outside path policy',
        summary: `Head: \`${target.head_sha}\`\n\nThe base branch policy classifies every changed path as tier0 or generated; no [review] marker requests a review.` });
      return { check_run_id: receipt.id, head_sha: target.head_sha, conclusion: 'neutral', applicable: false,
        started_at: run.started_at, observed_at: observedAt, elapsed_ms: Date.now() - started };
    }
    const reviewed = await recoverContext({ input: initialInput, github, target,
      execute: input => engine(input, externalId), createReviewInput, validateResult, onResult, deadlineMs });
    const input = reviewed.input;
    let result = reviewed.result;
    let current;
    try { current = await github.current(target); }
    catch {
      result = { ...result, status: 'incomplete', terminal_incomplete: false, terminal_invalid: false, reason: 'Current head could not be verified after source review.' };
      await onResult?.(result);
    }
    if (current === false) {
      const receipt = await github.finish(run, 'cancelled', { title: 'Head superseded', summary: `Old head \`${target.head_sha}\` was superseded. The new head needs its own review.` });
      return { check_run_id: receipt.id, head_sha: target.head_sha, conclusion: 'cancelled', elapsed_ms: Date.now() - started };
    }
    const { conclusion, output } = outputFor(result, input);
    // GitHub admits 50 annotations per request. Intermediate batches stay in
    // progress; only the final request publishes the terminal conclusion.
    while (output.annotations.length > 50) {
      await github.request(`/check-runs/${run.id}`, { method: 'PATCH', checks: true, body: {
        output: { ...output, annotations: output.annotations.splice(0, 50) },
      } });
    }
    const receipt = await github.finish(run, conclusion, output);
    return { check_run_id: receipt.id, head_sha: target.head_sha, conclusion, findings: result.findings.length,
      ...(result.status === 'complete' ? {} : { unavailable: true, reason: result.reason }),
      ...(result.discarded_items?.length ? { discarded_items: result.discarded_items.length } : {}),
      parts: result.parts, budget: result.budget, request_bytes: result.request_bytes, recovery: result.recovery,
      recovery_sources: result.recovery_sources,
      ...(result.snapped_evidence?.length ? { snapped_evidence: result.snapped_evidence } : {}),
      ...(result.validation_failure ? { validation_failure: result.validation_failure } : {}),
      started_at: run.started_at, observed_at: observedAt,
      session_name: result.session_name, input_digest: input.input_digest, reviewer_version: input.reviewer_version,
      elapsed_ms: Date.now() - started };
  } catch (error) {
    // Operational unavailability is distinct from a code finding. Keep the
    // advisory check terminal even after collection
    // or inference fails. Provider failures remain visible to the caller.
    const reason = error instanceof Unavailable ? error.message : 'The review engine or source collection failed.';
    const current = await github.current(target);
    const conclusion = current ? 'neutral' : 'cancelled';
    const receipt = await github.finish(run, conclusion, { title: current ? 'Review unavailable' : 'Head superseded',
      summary: `Head: \`${target.head_sha}\`\n\n${reason}` });
    return { check_run_id: receipt.id, head_sha: target.head_sha, conclusion, unavailable: true, reason,
      started_at: run.started_at, observed_at: observedAt,
      elapsed_ms: Date.now() - started };
  }
}
