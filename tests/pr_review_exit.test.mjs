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

test('CLI deferral exports a nonpassing deferred verdict without failing or finalizing its live owner', () => {
  const directory = mkdtempSync(join(tmpdir(), 'review-deferred-cli-'));
  try {
    writeFileSync(join(directory, 'pr-review-auth.json'), JSON.stringify({ checkToken: 'fixture' }), { mode: 0o600 });
    writeFileSync(join(directory, 'event.json'), JSON.stringify({ repository: { full_name: 'example/repo', id: 1 }, inputs: { 'pull-request': '7' } }));
    const moduleUrl = new URL('../.github/actions/pr-review/github.mjs', import.meta.url).href;
    writeFileSync(join(directory, 'preload.mjs'), `import { GitHub, APP_ID, CHECK_NAME } from ${JSON.stringify(moduleUrl)};
      globalThis.fetch = async () => { throw new Error('Deferral must not infer or mutate a check'); };
      GitHub.prototype.current = async () => true;
      GitHub.prototype.pages = async () => [{ filename: 'src/change.js', status: 'modified' }];
      GitHub.prototype.request = async path => path.includes('/compare/') ? { merge_base_commit: { sha: '${'a'.repeat(40)}' } } :
        { state: 'open', changed_files: 1, title: 'Review source', head: { sha: '${head}' }, base: { sha: '${'a'.repeat(40)}', ref: 'main', repo: { id: 1 } } };
      GitHub.prototype.begin = async () => ({ id: 9, app: { id: APP_ID }, name: CHECK_NAME, head_sha: '${head}',
        status: 'in_progress', conclusion: null, deferred: true, owner_run_id: '99' });
      GitHub.prototype.source = async (path, sha) => path === 'src/change.js' ? (sha === '${'a'.repeat(40)}' ? 'before\\n' : 'after\\n') : null;`);
    const env = { ...process.env, RUNNER_TEMP: directory, GITHUB_REPOSITORY: 'example/repo', GITHUB_EVENT_NAME: 'workflow_dispatch',
      GITHUB_EVENT_PATH: join(directory, 'event.json'), GITHUB_RUN_ID: '1', GITHUB_RUN_ATTEMPT: '1', GITHUB_TOKEN: 'fixture',
      GITHUB_STEP_SUMMARY: join(directory, 'summary.md'), GITHUB_OUTPUT: join(directory, 'outputs') };
    const execute = command => spawnSync('node', ['--import', join(directory, 'preload.mjs'), '.github/actions/pr-review/cli.mjs', command], {
      cwd: new URL('..', import.meta.url), encoding: 'utf8', timeout: 10_000, env });
    const result = execute('review'); assert.equal(result.status, 0, result.stderr);
    const receipt = JSON.parse(result.stdout).reviews[0];
    assert.equal(receipt.deferred, true); assert.equal(receipt.review_passed, false); assert.equal(receipt.conclusion, null); assert.equal(receipt.owner_run_id, '99');
    assert.equal(readFileSync(join(directory, 'outputs'), 'utf8'), 'outcome=deferred\nreview_passed=false\n');
    assert.equal(existsSync(join(directory, 'pr-review-state.json')), false);
    assert.match(readFileSync(join(directory, 'summary.md'), 'utf8'), /live workflow already owns the same-input/);
    assert.equal(execute('finalize').status, 0); assert.equal(existsSync(join(directory, 'pr-review-auth.json')), false);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
