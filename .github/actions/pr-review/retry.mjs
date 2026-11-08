import { setTimeout as sleep } from 'node:timers/promises';

// Four wire attempts per request, within the caller's unchanged deadline.
export async function waitForRetry({ attempt, response, deadlineMs, now = Date.now, wait = sleep, random = Math.random, maxAttempts = 4 }) {
  if (attempt >= maxAttempts - 1) return false;
  const hint = response?.headers?.get?.('retry-after');
  let providerDelay = 0;
  if (hint != null) {
    providerDelay = /^\d+$/.test(hint) ? Number(hint) * 1000 : Date.parse(hint) - now();
    if (!Number.isFinite(providerDelay)) return false;
  }
  const delay = Math.max(2000 * 2 ** attempt * (1 + random() / 4), providerDelay);
  // Never shorten a provider wait to squeeze in another call.
  if (delay + 1 >= deadlineMs - now()) return false;
  await wait(delay);
  return now() < deadlineMs;
}
export const transientStatus = status => [429, 502, 503, 504].includes(status);

export async function fetchWithRetry(url, init, options) {
  for (let attempt = 0; ; attempt++) {
    let response;
    try {
      const remaining = options.deadlineMs - (options.now ?? Date.now)();
      if (!(remaining > 0)) throw new Error('Request deadline expired.');
      response = await options.fetcher(url, { ...init, signal: AbortSignal.timeout(Math.ceil(Math.min(options.timeoutMs ?? remaining, remaining))) });
    } catch (error) {
      if (await waitForRetry({ ...options, attempt })) continue;
      throw error;
    }
    if (transientStatus(response.status) && await waitForRetry({ ...options, response, attempt })) {
      await response.body?.cancel?.().catch(() => {});
      continue;
    }
    return response;
  }
}
