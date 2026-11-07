import { readFileSync, writeFileSync, existsSync, unlinkSync } from 'node:fs';
import { GitHub, APP_ID, CHECK_NAME, Unavailable, terminalCheckReceipt, githubTerminalFailure, githubFailureDetails } from './github.mjs';
import { credentials } from './auth.mjs';
import { eventTargets } from './events.mjs';
import { createReviewInput, reviewerVersion } from './contract.mjs';
import { gatewayReviewer } from './gateway.mjs';
import { produceReview, completedReviewReceipt } from './producer.mjs';

const env = process.env;
const statePath = `${env.RUNNER_TEMP}/pr-review-state.json`;
const authPath = `${env.RUNNER_TEMP}/pr-review-auth.json`;
const githubFailurePath = `${env.RUNNER_TEMP}/pr-review-github-failure.json`;
const read = path => JSON.parse(readFileSync(path, 'utf8'));
const save = (path, value) => writeFileSync(path, JSON.stringify(value), { mode: 0o600 });
const passed = receipt => !receipt.unavailable && (receipt.conclusion === 'success' ||
  (receipt.conclusion === 'neutral' && receipt.applicable === false));
const reasonFor = receipt => receipt.reason || (receipt.applicable === false ? 'Review not required by path policy.' :
  receipt.conclusion === 'failure' ? 'Review found blocking defects.' :
  receipt.conclusion === 'cancelled' ? 'Head superseded.' :
  receipt.conclusion === 'success' ? 'Review complete.' : 'Review unavailable or incomplete.');
const summarize = line => {
  if (env.GITHUB_STEP_SUMMARY) writeFileSync(env.GITHUB_STEP_SUMMARY, `${line}\n`, { flag: 'a' });
};
const remember = record => {
  const states = existsSync(statePath) ? read(statePath) : [];
  const index = states.findIndex(state => state.id === record.id);
  if (index < 0) states.push(record); else states[index] = { ...states[index], ...record };
  save(statePath, states);
};
const rememberPublication = publication => remember({ id: publication.expected.id, publication });
const githubFor = (auth, deadlineMs) => new GitHub({ repository: env.GITHUB_REPOSITORY, readToken: env.GITHUB_TOKEN, checkToken: auth.checkToken, deadlineMs });

async function main() {
  const command = process.argv[2];
  if (command === 'auth') {
    save(authPath, await credentials({ ciName: env.REVIEW_CI_NAME, trustPolicy: env.REVIEW_TRUST_POLICY, repository: env.GITHUB_REPOSITORY }));
    return;
  }
  // Bound discovery, collection, publication and recovery as well as inference.
  const deadlineMs = Date.now() + (command === 'finalize' ? 90_000 : 35 * 60_000);
  const github = githubFor(read(authPath), deadlineMs);
  if (command === 'finalize') {
    try {
      if (existsSync(githubFailurePath)) {
        const failure = read(githubFailurePath);
        throw new Unavailable(`GitHub terminal failure (${failure.code}): ${JSON.stringify(failure)}`);
      }
      for (const state of existsSync(statePath) ? read(statePath) : []) {
        try {
          const { id, head_sha } = state;
          let run;
          if (state.publication || state.status === 'completed') {
            // Neither attempted publication nor an observed terminal result may
            // be downgraded by a stale GET. Legacy terminal state without a full
            // receipt can fail verification, but still cannot authorize a PATCH.
            run = await github.reconcile(state.publication ?? terminalCheckReceipt(state));
          } else {
            try { run = await github.request(`/check-runs/${id}`, { checks: true }); }
            catch (error) {
              if (githubTerminalFailure(error)) throw error;
              // An accepted creation receipt remains authoritative when its GET
              // projection is unavailable. Only a persisted canonical identity
              // can authorize completion without this read.
              run = state;
            }
          }
          if (run.app?.id !== APP_ID || run.head_sha !== head_sha || run.name !== CHECK_NAME) throw new Unavailable('Finalizer found no authoritative canonical check identity.');
          if (run.status !== 'completed') {
            await github.finish(run, 'neutral', { title: 'Review interrupted',
              summary: `The workflow ended before a complete review was available for head \`${head_sha}\`. Dispatch a review to recover.` },
            { onPublish: rememberPublication });
            process.exitCode = 1;
            summarize(`Check ${id}: neutral — Review interrupted before completion.`);
          } else if (!passed(completedReviewReceipt(run))) {
            process.exitCode = 1;
            summarize(`Check ${id}: ${run.conclusion} — ${run.output?.title || 'Review did not complete successfully.'}`);
          }
        } catch (error) {
          if (githubTerminalFailure(error)) throw error;
          process.exitCode = 1;
          const reason = error instanceof Unavailable ? error.message : 'Review finalization could not complete.';
          process.stderr.write(`${reason}\n`); summarize(reason);
        }
      }
    } finally { unlinkSync(authPath); }
    return;
  }
  if (command !== 'review') throw new Unavailable('Unknown review command.');
  const started = Date.now();
  const targets = await eventTargets(github, env.GITHUB_EVENT_NAME, read(env.GITHUB_EVENT_PATH), `${env.GITHUB_RUN_ID}/${env.GITHUB_RUN_ATTEMPT}`, { deadlineMs });
  const receipts = [], results = [];
  for (const { target, trigger } of targets) {
    // Configure inside produceReview's fault boundary so configuration failure
    // still publishes an honest neutral result on the exact head.
    let reviewer;
    const engine = input => (reviewer ??= gatewayReviewer({ token: read(authPath).inferenceToken,
      model: env.REVIEW_MODEL, maxOutputTokens: env.REVIEW_MAX_OUTPUT_TOKENS ? Number(env.REVIEW_MAX_OUTPUT_TOKENS) : undefined,
      reasoningEffort: env.REVIEW_REASONING_EFFORT, deadlineMs, current: () => github.current(target, { deadlineMs }) }))(input);
    const receipt = await produceReview({ github, target, trigger, engine, createReviewInput, engineVersion: reviewerVersion, deadlineMs,
      detailsUrl: `https://github.com/${env.GITHUB_REPOSITORY}/actions/runs/${env.GITHUB_RUN_ID}`,
      onResult: result => {
        const index = results.findIndex(r => r.pull_request === target.number && r.head_sha === result.head_sha);
        const record = { pull_request: target.number, ...result };
        if (index < 0) results.push(record); else results[index] = record;
        save(`${env.RUNNER_TEMP}/pr-review-results.json`, results);
      },
      onBegin: run => remember({ id: run.id, head_sha: run.head_sha, app: { id: run.app.id }, name: run.name, status: run.status,
        ...(run.status === 'completed' ? { publication: terminalCheckReceipt(run) } : {}) }),
      onPublish: rememberPublication });
    receipts.push({ ...receipt, review_passed: passed(receipt), pull_request: target.number, event_to_check_ms: eventLatency(read(env.GITHUB_EVENT_PATH), Date.parse(receipt.observed_at)) });
  }
  save(`${env.RUNNER_TEMP}/pr-review-receipts.json`, receipts);
  process.stdout.write(`${JSON.stringify({ reviews: receipts })}\n`);
  for (const receipt of receipts) {
    summarize(`PR #${receipt.pull_request}: ${receipt.head_sha}, ${receipt.conclusion}, check ${receipt.check_run_id}, ${receipt.elapsed_ms ?? 0} ms — ${reasonFor(receipt)}`);
  }
  const failed = receipts.some(receipt => !receipt.deferred && !passed(receipt));
  const outcome = failed ? 'failed' : receipts.some(receipt => receipt.deferred) ? 'deferred' : 'passed';
  if (env.GITHUB_OUTPUT) writeFileSync(env.GITHUB_OUTPUT,
    `outcome=${outcome}\nreview_passed=${outcome === 'passed'}\n`, { flag: 'a' });
  // A deferred delivery did no assessment: its explicit false verdict output
  // cannot replace the still-pending canonical check owned by another workflow.
  if (failed) process.exitCode = 1;
}

function eventLatency(event, now) {
  const requested = Date.parse(event.workflow_run?.created_at ?? '');
  return Number.isFinite(requested) ? now - requested : null;
}

try { await main(); }
catch (error) {
  // Refusal causes survive PublishUnverified, without replay or neutral cleanup.
  if (githubTerminalFailure(error)) save(githubFailurePath, githubFailureDetails(error));
  if (env.GITHUB_OUTPUT) writeFileSync(env.GITHUB_OUTPUT, 'outcome=failed\nreview_passed=false\n', { flag: 'a' });
  const reason = error instanceof Unavailable ? error.message : 'Review integration could not complete; see its configured credentials and source references.';
  process.stderr.write(`${reason}\n`);
  summarize(reason);
  process.exitCode = 1;
}
