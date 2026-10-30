import { spawnSync } from 'node:child_process';
import { Unavailable } from './github.mjs';

async function json(fetcher, url, options, label) {
  let reply;
  try { reply = await fetcher(url, { redirect: 'error', signal: AbortSignal.timeout(30_000), ...options }); }
  catch { throw new Unavailable(`${label} failed at the network.`); }
  if (!reply.ok) throw new Unavailable(`${label} returned HTTP ${reply.status}.`);
  try { return await reply.json(); } catch { throw new Unavailable(`${label} returned invalid JSON.`); }
}

export async function credentials({ ciName, trustPolicy, repository, env = process.env, fetcher = fetch,
  ready = issued => {
    const result = spawnSync('python3', [new URL('../_shared/access_ready.py', import.meta.url).pathname,
      '--api-url', 'https://api.sylphx.com'], { input: JSON.stringify(issued), encoding: 'utf8', timeout: 95_000 });
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
  const broker = await json(fetcher, `https://api.sylphx.com/v1/${trustPolicy}:exchangeToken`, {
    method: 'POST', headers: { authorization: `Bearer ${issued.api_key}`, 'content-type': 'application/json' },
    body: JSON.stringify({ repositories: [repository], permissions: { checks: 'write' } }),
  }, 'Broker check-token exchange');
  if (typeof broker.token !== 'string' || !broker.token || /[\r\n]/.test(broker.token) ||
      broker.issuer !== 'github-apps/1299545' || broker.credential_kind !== 'access_token' ||
      broker.repositories?.length !== 1 || broker.repositories[0] !== repository ||
      broker.permissions?.checks !== 'write' || Object.entries(broker.permissions).some(([k,v]) => k !== 'checks' && v !== 'none' && !(k === 'metadata' && v === 'read'))) {
    throw new Unavailable('Broker did not return a checks:write-only token.');
  }
  mask(broker.token);
  return { inferenceToken: issued.api_key, checkToken: broker.token };
}
