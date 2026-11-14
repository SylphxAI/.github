import { APP_ID, CHECK_NAME, Unavailable, PublishUnverified, githubTerminalFailure, digest, validSha, validPath, bundleFile } from './github.mjs';
import { changedRanges } from './source.mjs';
import { spawnSync } from 'node:child_process';
import { posix } from 'node:path';
import { fileURLToPath } from 'node:url';
import { recoverContext } from './recovery.mjs';
import { buildCandidates, adjudicationRequest, validateAdjudication, applyAdjudication, adjudicationVersion } from './adjudication.mjs';

export function parsePolicy(content) {
  const parsed = spawnSync('python3', [fileURLToPath(new URL('../_shared/review_policy.py', import.meta.url))],
    { input: JSON.stringify(content), encoding: 'utf8', maxBuffer: 1024 * 1024, timeout: 5000 });
  if (parsed.status !== 0) throw new Unavailable('Base review policy is invalid or unavailable.');
  return JSON.parse(parsed.stdout);
}

// Match Python's codepoints, including valid Unicode line/paragraph separators.
const matches = (policy, section, path) => policy._patterns[section].some(p => new RegExp(p, 'us').test(path));
export const generatedSource = (policy, path) => path !== '.github/review-policy.toml' &&
  matches(policy, 'generated', path) && !matches(policy, 'review', path);

export function needsReview(policy, paths) {
  return paths.some(path => path === '.github/review-policy.toml' || matches(policy, 'review', path) ||
    !(matches(policy, 'tier0', path) || matches(policy, 'generated', path)));
}

export async function collectPullRequest(github, number, expectedHead, { deadlineMs } = {}) {
  const pr = await github.request(`/pulls/${number}`, { deadlineMs });
  if (pr.state !== 'open') throw new Unavailable('Pull request is closed.');
  if (!validSha(pr.head.sha) || !validSha(pr.base.sha) || (expectedHead && pr.head.sha !== expectedHead)) {
    throw new Unavailable('Pull request head changed before collection.');
  }
  const files = await github.pages(`/pulls/${number}/files`, undefined, { deadlineMs });
  if (files.length !== pr.changed_files || files.length === 0) throw new Unavailable('Changed-file population is incomplete or empty.');
  const comparison = await github.request(`/compare/${pr.base.sha}...${pr.head.sha}`, { deadlineMs });
  if (!validSha(comparison.merge_base_commit?.sha)) throw new Unavailable('No pinned merge base.');
  const target = { kind: 'pull_request', number, base_sha: comparison.merge_base_commit.sha,
    policy_sha: pr.base.sha, base_ref: pr.base.ref, head_sha: pr.head.sha,
    review_requested: /\[review\]/i.test(pr.title ?? ''),
    repository_id: String(pr.base.repo.id), files };
  if (!await github.current(target, { deadlineMs })) throw new Unavailable('Pull request changed during file collection.');
  return target;
}

export async function collectBundle(github, target, trigger, { deadlineMs } = {}) {
  const policyText = await github.source('.github/review-policy.toml', target.policy_sha ?? target.base_sha, true, { deadlineMs });
  const policy = parsePolicy(policyText);
  const paths = target.files.flatMap(f => [f.filename, f.previous_filename].filter(Boolean));
  if (!paths.every(validPath)) throw new Unavailable('Invalid changed-file path.');
  for (const file of target.files) if (!['added', 'modified', 'removed', 'renamed', 'copied'].includes(file.status)) {
    throw new Unavailable(`Unsupported change kind for ${file.filename}.`);
  }
  const needs_review = target.review_requested || needsReview(policy, paths);
  if (!needs_review) return { needs_review: false };
  const files = [];
  // Preserve the whole rename/copy transition when either identity is authored
  // or explicitly reviewed. Generated-to-generated changes never fetch bytes.
  const selected = target.files.filter(file =>
    ![file.filename, file.previous_filename].filter(Boolean).every(path => generatedSource(policy, path)));
  if (!selected.length) return { needs_review: false,
    reason: 'The base policy excludes every changed source as generated; deterministic provenance is enforced independently by ci-ok, including for a [review] title.' };
  // A narrow sequential stream respects provider admission and avoids holding
  // the entire large repository source in memory at once.
  for (const file of selected) {
    const before = file.status === 'added' ? null : await github.source(file.previous_filename ?? file.filename, target.base_sha, false, { allowBinary: true, deadlineMs });
    const after = file.status === 'removed' ? null : await github.source(file.filename, target.head_sha, false, { allowBinary: true, deadlineMs });
    files.push(bundleFile(file, before, after));
  }
  const selectedPaths = new Set(selected.flatMap(f => [f.filename, f.previous_filename].filter(Boolean)));
  const context = [];
  if (policyText !== null) context.push({ path: '.github/review-policy.toml', content: policyText });
  // Base-side instructions are context, never authority to change credentials,
  // verdict rules, or the pinned reviewer definition.
  const instructionPaths = new Set(['AGENTS.md']);
  for (const path of selected.flatMap(f => [f.filename, f.previous_filename].filter(Boolean))) {
    let directory = posix.dirname(path);
    while (directory !== '.') { instructionPaths.add(`${directory}/AGENTS.md`); directory = posix.dirname(directory); }
  }
  for (const path of instructionPaths) {
    if (generatedSource(policy, path) && !selectedPaths.has(path)) continue;
    const content = await github.source(path, target.policy_sha ?? target.base_sha, true, { deadlineMs });
    if (content !== null) context.push({ path, content });
  }
  if (!await github.current(target, { deadlineMs })) throw new Unavailable('Head changed during source collection.');
  return { needs_review, allowContext: path => selectedPaths.has(path) || !generatedSource(policy, path), bundle: { schema_version: 2,
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

async function finalAdjudication({ github, target, input, result, engine, deadlineMs }) {
  // A candidate from an incomplete original or recovery pass is not an
  // authoritative blocker. Do not let the final gate resurrect it; preserve
  // the incomplete state and publish zero findings.
  const partsIncomplete = result.parts && Number.isInteger(result.parts.planned) &&
    Number.isInteger(result.parts.completed) && result.parts.completed < result.parts.planned;
  if (result.status !== 'complete' || result.terminal_incomplete || result.terminal_invalid || partsIncomplete) {
    return { ...result, status: 'incomplete', findings: [], reason: result.reason || 'Original or recovered review context is incomplete.',
      causal_validation: { version: adjudicationVersion, status: 'incomplete', candidates: result.findings?.length ?? 0, decisions: 0 } };
  }
  const candidates = buildCandidates(result, input);
  if (!candidates.length) return result;
  const files = [];
  try {
    if (await github.current(target, { deadlineMs }) !== true) throw new Unavailable('Pinned head could not be verified before final adjudication.');
    const requireTime = () => { if (Number.isFinite(deadlineMs) && Date.now() >= deadlineMs) throw new Unavailable('Final adjudication deadline expired.'); };
    requireTime();
    const selectedPaths = new Set(input.files.flatMap(file => [file.path, file.previous_path].filter(Boolean)));
    const selected = target.files.filter(file => [file.filename, file.previous_filename].filter(Boolean).some(path => selectedPaths.has(path)));
    if (!selected.length) throw new Unavailable('No reviewed source population is available for final adjudication.');
    for (const file of selected) {
      const beforePath = file.previous_filename ?? file.filename;
      const before = file.status === 'added' ? null : await github.source(beforePath, target.base_sha, false, { allowBinary: true, deadlineMs });
      const after = file.status === 'removed' ? null : await github.source(file.filename, target.head_sha, false, { allowBinary: true, deadlineMs });
      const encode = (content, path, side) => content === null ? { path, side, absent: true, content: null } : Buffer.isBuffer(content) ? { path, side, binary: true, bytes: content.length, sha256: digest(content), content: null } : { path, side, binary: false, bytes: Buffer.byteLength(content), sha256: digest(content), content };
      const encodedBefore = encode(before, beforePath, 'base'), encodedAfter = encode(after, file.filename, 'head');
      if (encodedBefore.binary || encodedAfter.binary) throw new Unavailable('Required adjudication source is binary and cannot prove a blocker.');
      files.push(encodedBefore, encodedAfter);
    }
    const context = (input.context ?? []).map(c => ({ path: c.path, side: c.side ?? 'context', content: typeof c.content === 'string' ? c.content : null, ...(typeof c.content === 'string' ? { bytes: Buffer.byteLength(c.content), sha256: digest(c.content) } : { unavailable: true }) }));
    if (context.some(c => c.unavailable)) throw new Unavailable('Required recovered context is unavailable for final adjudication.');
    if (await github.current(target, { deadlineMs }) !== true) throw new Unavailable('Pinned head changed before final adjudication.');
    requireTime();
    const wire = adjudicationRequest(input, candidates, { files, context });
    // The dedicated gateway callback serializes this object in the actual model request.
    const value = typeof engine.adjudicate === 'function' ? await engine.adjudicate({ input: wire, deadlineMs }) : { status: 'incomplete', reason: 'Final adjudication transport is unavailable.' };
    requireTime();
    const checked = validateAdjudication(value, input, candidates, { files, context });
    if (!checked.ok) return { ...result, status: 'incomplete', findings: [], reason: checked.reason, causal_validation: { version: adjudicationVersion, status: 'incomplete', candidates: candidates.length, decisions: 0 } };
    const currentAfter = await github.current(target, { deadlineMs });
    if (currentAfter !== true) return { ...result, status: 'incomplete', findings: [], reason: 'Pinned head could not be verified after final adjudication.', causal_validation: { version: adjudicationVersion, status: 'incomplete', candidates: candidates.length, decisions: value.decisions.length } };
    requireTime();
    return applyAdjudication(result, value, candidates);
  } catch (error) {
    if (githubTerminalFailure(error)) throw error;
    return { ...result, status: 'incomplete', findings: [], reason: error instanceof Unavailable ? error.message : 'Final adjudication source collection failed.', causal_validation: { version: adjudicationVersion, status: 'incomplete', candidates: candidates.length, decisions: 0 } };
  }
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
  for (const finding of result.findings) for (const claim of finding.semantic_claims ?? []) {
    summary.push(`Documentation for ${claim.tool} (${finding.path}, ${finding.side} lines ${finding.start_line}–${finding.end_line}; read ${claim.documentation_read_date}): ${claim.documentation_url}\n${claim.documentation_quote}`);
  }
  for (const finding of result.findings.filter(f => f.side === 'base')) {
    summary.push(`${finding.severity}: ${finding.title} (${finding.path}, base lines ${finding.start_line}–${finding.end_line})\n${finding.explanation}\nTrigger: ${finding.trigger}\nConsequence: ${finding.consequence}`);
  }
  const annotations = result.findings.filter(f => f.side === 'head').map(f => ({ path: f.path, start_line: f.start_line, end_line: f.end_line,
    annotation_level: ['critical', 'high', 'medium'].includes(f.severity) ? 'failure' : 'notice',
    title: f.title.slice(0, 255), message: `${f.explanation}\nTrigger: ${f.trigger}\nConsequence: ${f.consequence}`.slice(0, 65535) }));
  const title = blocking.length ? `${blocking.length} defects require a change${result.status === 'complete' ? '' : '; review incomplete'}` : result.status === 'complete' ? 'Review complete' : result.terminal_invalid ? 'Review invalid' : result.terminal_incomplete ? 'Review incomplete' : 'Review unavailable';
  return { conclusion: blocking.length || result.terminal_incomplete || result.terminal_invalid ? 'failure' : result.status === 'complete' ? 'success' : 'neutral', output: {
    title: result.unavailability_code ? `${title} (${result.unavailability_code})` : title,
    summary: [...summary, ...(result.unavailability_code ? [`Unavailability cause: ${result.unavailability_code}`] : [])].join('\n\n'), annotations,
  } };
}

// This title is authored only by this publisher after the pinned policy check.
export function completedReviewReceipt(run) {
  const outsidePolicy = run.conclusion === 'neutral' && run.output?.title === 'Review outside path policy';
  return { check_run_id: run.id, head_sha: run.head_sha, conclusion: run.conclusion,
    ...(outsidePolicy ? { applicable: false } : run.conclusion !== 'success' ?
      { reason: run.output?.title || 'Review unavailable or incomplete.' } : {}) };
}

export const PUBLISH_RESERVE_MS = 60_000;
export const inferenceDeadlineFor = (deadlineMs, now = Date.now()) =>
  Number.isFinite(deadlineMs) && deadlineMs - PUBLISH_RESERVE_MS > now ? deadlineMs - PUBLISH_RESERVE_MS : deadlineMs;

export async function produceReview({ github, target, trigger, detailsUrl, engine, createReviewInput, engineVersion, onBegin, onPublish, onResult, deadlineMs }) {
  const started = Date.now();
  const inferenceDeadlineMs = inferenceDeadlineFor(deadlineMs, started);
  const externalId = `pr-review/v1/${digest(JSON.stringify([github.repository, target.kind, target.number ?? target.ref,
    target.base_sha, target.policy_sha, target.head_sha, target.review_requested, trigger, engineVersion]))}`;
  // Resolve the exact source/policy input before choosing any prior work. The
  // same head alone does not prove the base, instructions or policy are equal.
  let collected, initialInput, collectionError;
  try {
    collected = await collectBundle(github, target, trigger, { deadlineMs: inferenceDeadlineMs });
    if (collected.needs_review) initialInput = createReviewInput(collected.bundle);
  } catch (error) { if (githubTerminalFailure(error)) throw error; collectionError = error; }
  const identity = initialInput && `Head: \`${initialInput.head_sha}\`\n\nReviewer: \`${initialInput.reviewer_version}\`\n\nSource digest: \`${initialInput.input_digest}\`\n\n`;
  const begin = async (restartCompleted = false) => {
    let recorded = false;
    const register = async run => {
      if (run.app?.id !== APP_ID || run.head_sha !== target.head_sha || run.name !== CHECK_NAME) {
        throw new Unavailable('Publisher did not receive the canonical App Check Run.');
      }
      await onBegin?.(run); recorded = true;
    };
    const run = await github.begin(target, externalId, detailsUrl, { restartCompleted, inputIdentity: identity, onCreated: register, deadlineMs });
    if (run.app?.id !== APP_ID || run.head_sha !== target.head_sha || run.name !== CHECK_NAME ||
        (restartCompleted && run.status === 'completed')) {
      throw new Unavailable('Publisher did not receive the canonical App Check Run in an active state.');
    }
    if (!recorded && run.status !== 'completed' && !run.deferred) await register(run);
    return run;
  };
  let run = await begin();
  const observedAt = new Date().toISOString();
  const deferredReceipt = run => ({ check_run_id: run.id, head_sha: run.head_sha, conclusion: null, deferred: true,
    owner_run_id: run.owner_run_id, input_digest: initialInput?.input_digest,
    reason: 'A live workflow already owns the same-input review; no verdict is available from this delivery.', observed_at: observedAt });
  if (run.deferred) return deferredReceipt(run);
  if (run.status === 'completed') {
    // Completion alone includes unavailable/cancelled attempts. Only a verdict
    // bound to this freshly collected input can replace another assessment.
    if (!collectionError && identity && ['success', 'failure'].includes(run.conclusion) &&
        typeof run.output?.summary === 'string' && run.output.summary.startsWith(identity)) {
      await onBegin?.(run);
      return { ...completedReviewReceipt(run), reused: true, input_digest: initialInput.input_digest,
        started_at: run.started_at, observed_at: observedAt };
    }
    // Keep rejected history immutable and register only the new check with the
    // finalizer. begin() reconciles an accepted in-progress create before POST.
    run = await begin(true);
    if (run.deferred) return deferredReceipt(run);
  }
  const finish = (conclusion, output) => github.finish(run, conclusion, output, { onPublish, deadlineMs });
  try {
    if (collectionError) throw collectionError;
    if (!collected.needs_review) {
      const receipt = await finish('neutral', { title: 'Review outside path policy',
        summary: `Head: \`${target.head_sha}\`\n\n${collected.reason ?? 'The base branch policy classifies every changed path as tier0 or generated; no [review] marker requests a review.'}` });
      return { check_run_id: receipt.id, head_sha: target.head_sha, conclusion: 'neutral', applicable: false,
        started_at: run.started_at, observed_at: observedAt, elapsed_ms: Date.now() - started };
    }
    const reviewed = await recoverContext({ input: initialInput, github, target, allowContext: collected.allowContext,
      // Recovery observations are provisional. Only the final authoritative result
      // may cross the persistence/publication callback boundary.
      execute: input => engine(input, externalId, { deadlineMs: inferenceDeadlineMs }), createReviewInput, validateResult, deadlineMs: inferenceDeadlineMs });
    const input = reviewed.input;
    let result = reviewed.result;
    let current;
    try {
      current = await github.current(target, { deadlineMs });
      if (current !== true && current !== false) throw new Unavailable('Current head response is unreadable.');
    }
    catch (error) {
      if (githubTerminalFailure(error)) throw error;
      result = { ...result, status: 'incomplete', terminal_incomplete: false, terminal_invalid: false, reason: 'Current head could not be verified after source review.' };
    }
    if (current === false) {
      result = { ...result, status: 'incomplete', findings: [], reason: 'Head superseded before final adjudication.' };
      await onResult?.(result);
      const receipt = await finish('cancelled', { title: 'Head superseded', summary: `Old head \`${target.head_sha}\` was superseded. The new head needs its own review.` });
      return { check_run_id: receipt.id, head_sha: target.head_sha, conclusion: 'cancelled', elapsed_ms: Date.now() - started };
    }
    result = await finalAdjudication({ github, target, input, result, engine, deadlineMs: inferenceDeadlineMs });
    await onResult?.(result);
    const { conclusion, output } = outputFor(result, input);
    const receipt = await finish(conclusion, output);
    return { check_run_id: receipt.id, head_sha: target.head_sha, conclusion, findings: result.findings.length,
      ...(result.status === 'complete' ? {} : { unavailable: true, reason: result.reason }),
      ...(result.unavailability_code ? { unavailability_code: result.unavailability_code } : {}),
      ...(result.discarded_items?.length ? { discarded_items: result.discarded_items.length } : {}),
      parts: result.parts, budget: result.budget, request_bytes: result.request_bytes, recovery: result.recovery,
      recovery_sources: result.recovery_sources,
      ...(result.snapped_evidence?.length ? { snapped_evidence: result.snapped_evidence } : {}),
      ...(result.validation_failure ? { validation_failure: result.validation_failure } : {}),
      started_at: run.started_at, observed_at: observedAt,
      session_name: result.session_name, input_digest: input.input_digest, reviewer_version: input.reviewer_version,
      elapsed_ms: Date.now() - started };
  } catch (error) {
    // A terminal write may already be visible to GitHub. Never replace its
    // verdict because its read projection is stale or unavailable.
    if (error instanceof PublishUnverified || githubTerminalFailure(error)) throw error;
    // Operational unavailability is distinct from a code finding. Keep the
    // advisory check terminal even after collection
    // or inference fails. Provider failures remain visible to the caller.
    const reason = error instanceof Unavailable ? error.message : 'The review engine or source collection failed.';
    const current = await github.current(target, { deadlineMs });
    const conclusion = current ? 'neutral' : 'cancelled';
    const receipt = await finish(conclusion, { title: current ? 'Review unavailable' : 'Head superseded',
      summary: `Head: \`${target.head_sha}\`\n\n${reason}` });
    return { check_run_id: receipt.id, head_sha: target.head_sha, conclusion, unavailable: true, reason,
      started_at: run.started_at, observed_at: observedAt,
      elapsed_ms: Date.now() - started };
  }
}
