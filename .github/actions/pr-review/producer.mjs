import { APP_ID, CHECK_NAME, Unavailable, digest, validSha, validPath, hunkRanges, bundleFile } from './github.mjs';
import { spawnSync } from 'node:child_process';
import { posix } from 'node:path';

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
    const before = file.status === 'added' ? null : await github.source(file.previous_filename ?? file.filename, target.base_sha);
    const after = file.status === 'removed' ? null : await github.source(file.filename, target.head_sha);
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
  return { needs_review, bundle: { schema_version: 1,
    repository: github.repository, repository_id: target.repository_id,
    base_sha: target.base_sha, head_sha: target.head_sha, policy_version: String(policy.version), trigger, files, context } };
}

export function validateResult(result, input) {
  if (result.status !== 'complete' || result.head_sha !== input.head_sha || result.base_sha !== input.base_sha ||
      result.input_digest !== input.input_digest || result.reviewer_version !== input.reviewer_version ||
      !Array.isArray(result.findings) || !Array.isArray(result.omitted_paths) || result.omitted_paths.length) {
    throw new Unavailable(result.reason || 'Review result is incomplete or does not match the pinned input.');
  }
  for (const finding of result.findings) {
    const file = input.files.find(f => f.path === finding.path);
    const ranges = file && hunkRanges(file.patch);
    if (!file || !Number.isInteger(finding.start_line) || !Number.isInteger(finding.end_line) ||
        finding.start_line < 1 || finding.end_line < finding.start_line ||
        !['base', 'head'].includes(finding.side) ||
        !ranges.some(r => r[finding.side].count > 0 && finding.start_line >= r[finding.side].start &&
          finding.end_line < r[finding.side].start + r[finding.side].count) ||
        !['critical', 'high', 'medium', 'low', 'info'].includes(finding.severity) ||
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
  for (const note of result.context_notes ?? []) summary.push(`Context: ${note}`);
  for (const finding of result.findings.filter(f => f.side === 'base')) {
    summary.push(`${finding.severity}: ${finding.title} (${finding.path}, base lines ${finding.start_line}–${finding.end_line})\n${finding.explanation}\nTrigger: ${finding.trigger}\nConsequence: ${finding.consequence}`);
  }
  const annotations = result.findings.filter(f => f.side === 'head').map(f => ({ path: f.path, start_line: f.start_line, end_line: f.end_line,
    annotation_level: ['critical', 'high', 'medium'].includes(f.severity) ? 'failure' : 'notice',
    title: f.title.slice(0, 255), message: `${f.explanation}\nTrigger: ${f.trigger}\nConsequence: ${f.consequence}`.slice(0, 65535) }));
  return { conclusion: blocking.length ? 'failure' : 'success', output: {
    title: blocking.length ? `${blocking.length} defects require a change` : 'Review complete',
    summary: summary.join('\n\n'), annotations,
  } };
}

export async function produceReview({ github, target, trigger, detailsUrl, engine, createReviewInput, engineVersion, onBegin }) {
  const started = Date.now();
  const externalId = `pr-review/v1/${digest(JSON.stringify([github.repository, target.kind, target.number ?? target.ref,
    target.base_sha, target.policy_sha, target.head_sha, target.review_requested, trigger, engineVersion]))}`;
  const run = await github.begin(target, externalId, detailsUrl);
  const observedAt = new Date().toISOString();
  if (run.app?.id !== APP_ID || run.head_sha !== target.head_sha || run.name !== CHECK_NAME) {
    throw new Unavailable('Publisher did not receive the canonical App Check Run.');
  }
  await onBegin?.(run);
  if (run.status === 'completed') return { reused: true, check_run_id: run.id, head_sha: run.head_sha, conclusion: run.conclusion, started_at: run.started_at, observed_at: observedAt };
  try {
    const { bundle, needs_review } = await collectBundle(github, target, trigger);
    if (!needs_review) {
      const receipt = await github.finish(run, 'neutral', { title: 'Review outside path policy',
        summary: `Head: \`${target.head_sha}\`\n\nThe base branch policy classifies every changed path as tier0 or generated; no [review] marker requests a review.` });
      return { check_run_id: receipt.id, head_sha: target.head_sha, conclusion: 'neutral', applicable: false,
        started_at: run.started_at, observed_at: observedAt, elapsed_ms: Date.now() - started };
    }
    const input = createReviewInput(bundle);
    const result = validateResult(await engine(input, externalId), input);
    if (!await github.current(target)) {
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
