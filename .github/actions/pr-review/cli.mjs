import { readFileSync, writeFileSync, existsSync, unlinkSync } from 'node:fs';
import { GitHub, APP_ID, CHECK_NAME, Unavailable } from './github.mjs';
import { credentials } from './auth.mjs';
import { eventTargets } from './events.mjs';
import { createReviewInput, reviewerVersion } from './contract.mjs';
import { gatewayReviewer } from './gateway.mjs';
import { produceReview } from './producer.mjs';

const env = process.env;
const statePath = `${env.RUNNER_TEMP}/pr-review-state.json`;
const authPath = `${env.RUNNER_TEMP}/pr-review-auth.json`;
const read = path => JSON.parse(readFileSync(path, 'utf8'));
const save = (path, value) => writeFileSync(path, JSON.stringify(value), { mode: 0o600 });
const githubFor = auth => new GitHub({ repository: env.GITHUB_REPOSITORY, readToken: env.GITHUB_TOKEN, checkToken: auth.checkToken });

async function main() {
  const command = process.argv[2];
  if (command === 'auth') {
    save(authPath, await credentials({ ciName: env.REVIEW_CI_NAME, trustPolicy: env.REVIEW_TRUST_POLICY, repository: env.GITHUB_REPOSITORY }));
    return;
  }
  const github = githubFor(read(authPath));
  if (command === 'finalize') {
    try {
    for (const { id, head_sha } of existsSync(statePath) ? read(statePath) : []) {
      const run = await github.request(`/check-runs/${id}`, { checks: true });
      if (run.app?.id !== APP_ID || run.head_sha !== head_sha || run.name !== CHECK_NAME) throw new Unavailable('Finalizer found a foreign check.');
      if (run.status !== 'completed') await github.finish(run, 'neutral', { title: 'Review interrupted',
        summary: `The workflow ended before a complete review was available for head \`${head_sha}\`. Dispatch a review to recover.` });
    }
    } finally { unlinkSync(authPath); }
    return;
  }
  if (command !== 'review') throw new Unavailable('Unknown review command.');
  const started = Date.now();
  const deadlineMs = started + 10 * 60_000;
  const targets = await eventTargets(github, env.GITHUB_EVENT_NAME, read(env.GITHUB_EVENT_PATH), `${env.GITHUB_RUN_ID}/${env.GITHUB_RUN_ATTEMPT}`);
  const states = [], receipts = [];
  for (const { target, trigger } of targets) {
    // Configure inside produceReview's fault boundary so configuration failure
    // still publishes an honest neutral result on the exact head.
    const engine = input => gatewayReviewer({ token: read(authPath).inferenceToken,
      model: env.REVIEW_MODEL, maxOutputTokens: env.REVIEW_MAX_OUTPUT_TOKENS ? Number(env.REVIEW_MAX_OUTPUT_TOKENS) : undefined,
      reasoningEffort: env.REVIEW_REASONING_EFFORT, deadlineMs })(input);
    const receipt = await produceReview({ github, target, trigger, engine, createReviewInput, engineVersion: reviewerVersion,
      detailsUrl: `https://github.com/${env.GITHUB_REPOSITORY}/actions/runs/${env.GITHUB_RUN_ID}`,
      onBegin: run => { states.push({ id: run.id, head_sha: run.head_sha }); save(statePath, states); } });
    receipts.push({ ...receipt, pull_request: target.number, event_to_check_ms: eventLatency(read(env.GITHUB_EVENT_PATH), Date.parse(receipt.observed_at)) });
  }
  save(`${env.RUNNER_TEMP}/pr-review-receipts.json`, receipts);
  process.stdout.write(`${JSON.stringify({ reviews: receipts })}\n`);
  if (env.GITHUB_STEP_SUMMARY) writeFileSync(env.GITHUB_STEP_SUMMARY,
    receipts.map(r => `PR #${r.pull_request}: ${r.head_sha}, ${r.conclusion}, check ${r.check_run_id}, ${r.elapsed_ms ?? 0} ms`).join('\n') + '\n', { flag: 'a' });
}

function eventLatency(event, now) {
  const requested = Date.parse(event.workflow_run?.created_at ?? '');
  return Number.isFinite(requested) ? now - requested : null;
}

try { await main(); }
catch (error) {
  const reason = error instanceof Unavailable ? error.message : 'Review integration could not complete; see its configured credentials and source references.';
  process.stderr.write(`${reason}\n`);
  process.exitCode = 1;
}
