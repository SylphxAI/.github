import { spawnSync } from 'node:child_process';
import { Unavailable } from './github.mjs';

const PropagationMarginMs = 180_000;
const PropagationRetryMs = 2_000;
const PropagationCodes = new Set(['key_not_yet_propagated', 'NOT_YET_PROPAGATED']);

function activeAfterMs(issued) {
  return Number.isInteger(issued?.active_after_ms) && issued.active_after_ms >= 0 && issued.active_after_ms <= 60_000
    ? issued.active_after_ms
    : 0;
}

async function json(fetcher, url, options, label) {
  let reply;
  try { reply = await fetcher(url, { redirect: 'error', signal: AbortSignal.timeout(30_000), ...options }); }
  catch { throw new Unavailable(`${label} failed at the network.`); }
  if (!reply.ok) throw new Unavailable(`${label} returned HTTP ${reply.status}.`);
  try { return await reply.json(); } catch { throw new Unavailable(`${label} returned invalid JSON.`); }
}

async function exchangeBrokerToken(fetcher, url, apiKey, body, issued, { sleep, now }) {
  const deadline = now() + activeAfterMs(issued) + PropagationMarginMs;
  while (true) {
    const timeoutMs = Math.min(30_000, Math.max(1, deadline - now()));
    let reply;
    try {
      reply = await fetcher(url, {
        redirect: 'error',
        signal: AbortSignal.timeout(timeoutMs),
        method: 'POST',
        headers: { authorization: `Bearer ${apiKey}`, 'content-type': 'application/json' },
        body,
      });
    } catch {
      throw new Unavailable('Broker check-token exchange failed at the network.');
    }
    let payload;
    try {
      payload = await reply.json();
    } catch {
      if (!reply.ok) throw new Unavailable(`Broker check-token exchange returned HTTP ${reply.status}.`);
      throw new Unavailable('Broker check-token exchange returned invalid JSON.');
    }
    if (reply.ok) return payload;
    if (reply.status === 401 && payload && typeof payload === 'object' &&
        PropagationCodes.has(payload.code) && now() < deadline) {
      const waitMs = Math.min(PropagationRetryMs, Math.max(0, deadline - now()));
      if (waitMs > 0) await sleep(waitMs);
      if (now() < deadline) continue;
    }
    throw new Unavailable(`Broker check-token exchange returned HTTP ${reply.status}.`);
  }
}

export async function credentials({ ciName, trustPolicy, repository, env = process.env, fetcher = fetch,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), now = () => Date.now(),
  ready = issued => {
    const timeout = activeAfterMs(issued) + PropagationMarginMs + 15_000;
    const result = spawnSync('python3', [new URL('../_shared/access_ready.py', import.meta.url).pathname,
      '--api-url', 'https://api.sylphx.com'], { input: JSON.stringify(issued), encoding: 'utf8', timeout });
    if (result.status !== 0) throw new Unavailable('Issued Access key is not ready; no Broker request was sent.');
  }, mask = value => process.stdout.write(`::add-mask::${value}\n`) }) {
  if (!ciName || !/^orgs\/[A-Za-z0-9_-]+\/trust_policies\/[A-Za-z0-9_-]+$/.test(trustPolicy ?? '')) {
    throw new Unavailable('Review CI declaration or Broker policy is not configured.');
  }
  if (!env.ACTIONS_ID_TOKEN_REQUEST_URL || !env.ACTIONS_ID_TOKEN_REQUEST_TOKEN) {
    throw new Unavailable('Review workflow requires id-token: write.');
  }
  const oidcUrl = new URL(env.ACTIONS_ID_TOKEN_REQUEST_URL);
  if (oidcUrl.protocol !== 'https:') throw new Unavailable('Invalid Actions OIDC origin.');
  oidcUrl.searchParams.set('audience', 'sylphx-access');
  const oidc = await json(fetcher, oidcUrl, { headers: { authorization: `Bearer ${env.ACTIONS_ID_TOKEN_REQUEST_TOKEN}` } }, 'Actions OIDC');
  if (typeof oidc.value !== 'string' || !oidc.value || /[\r\n]/.test(oidc.value)) throw new Unavailable('Actions OIDC returned no valid token.');
  mask(oidc.value);
  const issued = await json(fetcher, 'https://api.sylphx.com/v1/access/github/token', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: ciName, token: oidc.value }),
  }, 'Access CI exchange');
  if (typeof issued.api_key !== 'string' || !issued.api_key || /[\r\n]/.test(issued.api_key)) throw new Unavailable('Access returned no valid inference key.');
  mask(issued.api_key);
  await ready(issued);
  const broker = await exchangeBrokerToken(
    fetcher,
    `https://api.sylphx.com/v1/${trustPolicy}:exchangeToken`,
    issued.api_key,
    JSON.stringify({ repositories: [repository], permissions: { checks: 'write' } }),
    issued,
    { sleep, now },
  );
  if (typeof broker.token !== 'string' || !broker.token || /[\r\n]/.test(broker.token) ||
      broker.issuer !== 'github-apps/1299545' || broker.credential_kind !== 'access_token' ||
      broker.repositories?.length !== 1 || broker.repositories[0] !== repository ||
      broker.permissions?.checks !== 'write' || Object.entries(broker.permissions).some(([k,v]) => k !== 'checks' && v !== 'none' && !(k === 'metadata' && v === 'read'))) {
    throw new Unavailable('Broker did not return a checks:write-only token.');
  }
  mask(broker.token);
  return { inferenceToken: issued.api_key, checkToken: broker.token };
}
