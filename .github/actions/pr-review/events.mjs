import { validSha, Unavailable } from './github.mjs';
import { collectPullRequest } from './producer.mjs';

export async function eventTargets(github, eventName, event, generation) {
  if (event.repository?.full_name !== github.repository || !Number.isSafeInteger(event.repository.id)) {
    throw new Unavailable('Event repository does not match the authenticated caller.');
  }
  let numbers, expectedHead, id;
  if (eventName === 'workflow_dispatch') {
    if (!/^[1-9][0-9]*$/.test(event.inputs?.['pull-request'] ?? '')) throw new Unavailable('Dispatch requires a pull request number.');
    numbers = [Number(event.inputs['pull-request'])];
    id = `dispatch/${generation}`;
  } else if (eventName === 'workflow_run') {
    const run = event.workflow_run;
    if (run?.event !== 'pull_request' || !validSha(run.head_sha) || !Number.isSafeInteger(run.id)) return [];
    // Reconcile the trusted API association, including fork PRs whose webhook
    // pull_requests array may be empty. No artifact is downloaded or executed.
    const associated = await github.pages(`/commits/${run.head_sha}/pulls`);
    numbers = [...new Set(associated.filter(pr => pr.state === 'open' && pr.head.sha === run.head_sha &&
      pr.base.repo.id === event.repository.id).map(pr => pr.number))];
    expectedHead = run.head_sha;
    id = `pull-request/${run.head_sha}`;
  } else throw new Unavailable('Review runs only from a trusted workflow_run or dispatch.');
  const targets = [];
  for (const number of numbers) {
    const target = await collectPullRequest(github, number, expectedHead);
    if (target.repository_id !== String(event.repository.id)) throw new Unavailable('Pull request belongs to a different repository.');
    targets.push({ target, trigger: { kind: eventName, id, generation: eventName === 'workflow_run' ? '1' : String(generation) } });
  }
  return targets;
}
