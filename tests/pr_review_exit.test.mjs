import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { completedReviewReceipt } from '../.github/actions/pr-review/producer.mjs';
import { APP_ID, CHECK_NAME } from '../.github/actions/pr-review/github.mjs';

const head = 'b'.repeat(40);
const outcomes = [
  { conclusion: 'success', title: 'Review complete', exit: 0 },
  { conclusion: 'failure', title: '2 defects require a change', exit: 1 },
  { conclusion: 'failure', title: 'Review incomplete', exit: 1 },
  { conclusion: 'failure', title: 'Review invalid', exit: 1 },
  { conclusion: 'neutral', title: 'Review unavailable', exit: 1 },
  { conclusion: 'neutral', title: 'Review outside path policy', exit: 0, applicable: false },
  { conclusion: 'cancelled', title: 'Head superseded', exit: 1 },
];
for (const outcome of outcomes) test(`completed-check receipt retains ${outcome.title} applicability for exit evaluation`, () => {
  const run = { id: 1, head_sha: head, app: { id: APP_ID }, name: CHECK_NAME, status: 'completed',
    conclusion: outcome.conclusion, output: { title: outcome.title } };
  const result = completedReviewReceipt(run);
  assert.equal(result.conclusion, outcome.conclusion);
  assert.equal(result.applicable, outcome.applicable);
  assert.equal(result.reason, outcome.exit ? outcome.title : undefined);
});

for (const entries of [...outcomes.map(o => [o]), [{ status: 'in_progress', title: 'Review running', exit: 1 }],
  [outcomes[0], outcomes[3]], [outcomes[4], outcomes[1]]]) {
  test(`finalizer exit mirrors canonical checks: ${entries.map(e => e.title).join(' + ')}`, () => {
    const directory = mkdtempSync(join(tmpdir(), 'review-exit-cli-'));
    try {
      const runs = entries.map((entry, i) => ({ id: i + 1, head_sha: head, app: { id: APP_ID }, name: CHECK_NAME,
        status: entry.status ?? 'completed', conclusion: entry.conclusion, output: { title: entry.title } }));
      writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'fixture' }), { mode: 0o600 });
      writeFileSync(join(directory, 'pr-review-state.json'), JSON.stringify(runs.map(({ id, head_sha }) => ({ id, head_sha }))));
      function preload(runs) {
        globalThis.fetch = async (url, request = {}) => {
          const id = Number(new URL(url).pathname.split('/').at(-1)), run = runs.find(r => r.id === id);
          if (!run) throw new Error('Unexpected finalizer request');
          if (request.method === 'PATCH') Object.assign(run, JSON.parse(request.body));
          return { ok: true, status: 200, json: async () => run };
        };
      }
      writeFileSync(join(directory, 'preload.mjs'), `(${preload.toString()})(${JSON.stringify(runs)});`);
      const result = spawnSync('node', ['--import', join(directory, 'preload.mjs'), '.github/actions/pr-review/cli.mjs', 'finalize'], {
        cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000,
        env: { ...process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_TOKEN: 'fixture',
          GITHUB_STEP_SUMMARY: join(directory, 'summary.md') },
      });
      const failed = entries.some(e => e.exit !== 0);
      assert.equal(result.status, failed ? 1 : 0, result.stderr);
      assert.equal(existsSync(join(directory, 'pr-review-auth.json')), false);
      if (failed) {
        const summary = readFileSync(join(directory, 'summary.md'), 'utf8');
        for (const entry of entries.filter(e => e.exit !== 0)) {
          assert.ok(summary.includes(entry.status === 'in_progress' ? 'Review interrupted before completion' : entry.title));
        }
      }
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}
