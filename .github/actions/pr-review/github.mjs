import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { segmentBundleFile } from './source.mjs';

export const CHECK_NAME = 'Sylphx PR Review';
export const APP_ID = 1299545;
export const digest = value => createHash('sha256').update(value).digest('hex');
export const validSha = value => typeof value === 'string' && /^[a-f0-9]{40}$/.test(value);
export const validPath = value => typeof value === 'string' && value.length > 0 &&
  !value.startsWith('/') && !value.split('/').some(p => !p || p === '.' || p === '..') &&
  !/[\0\r\n\\]/.test(value);

export class Unavailable extends Error {}

// Keep this status-bearing type usable by read-only publication reconciliation.
export class GitHubHTTPError extends Unavailable {
  constructor(details) {
    super(`GitHub ${details.method} ${details.endpoint} returned HTTP ${details.status} (${details.code}/${details.classification}): ${JSON.stringify(details)}`);
    this.status = details.status;
    this.code = details.code;
    this.classification = details.classification;
    this.details = details;
  }
}

export const githubRefusal = error => error instanceof GitHubHTTPError && [403, 429].includes(error.status);
export const githubFailureDetails = error => githubRefusal(error) ||
  (error instanceof Unavailable && error.code === 'outcome_unknown') ? error.details :
  error instanceof PublishUnverified ? githubFailureDetails(error.cause) : null;
export const githubTerminalFailure = error => githubFailureDetails(error) !== null;
function unreadableResponse(method, path, stage) {
  const code = method === 'GET' ? 'read_unavailable' : 'outcome_unknown';
  const endpoint = safeEndpoint(path);
  const error = new Unavailable(`GitHub ${method} ${endpoint} ${stage} (${code}); request not retried.`);
  error.code = code;
  error.details = { method, endpoint, status: null, code, classification: code, headers: {}, retry_at_ms: null };
  return error;
}
const requestTimeoutMs = 30_000, retryMarginMs = 5_000;
const safeEndpoint = path => {
  const endpoint = path.split('?')[0];
  return /^\/(?:pulls\/[1-9][0-9]*(?:\/files)?|commits\/[a-f0-9]{40}\/(?:check-runs|pulls)|check-runs(?:\/[1-9][0-9]*)?|git\/(?:trees|blobs)\/[a-f0-9]{40}|compare\/[a-f0-9]{40}\.\.\.[a-f0-9]{40}|actions\/runs\/[1-9][0-9]*)$/.test(endpoint)
    ? endpoint : endpoint.startsWith('/git/ref/heads/') ? '/git/ref/heads/<redacted>' : '/<redacted>';
};
const integer = value => typeof value === 'string' && /^[0-9]{1,16}$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null;
function safeHeaders(response) {
  const get = name => response.headers?.get?.(name) ?? null;
  const headers = {};
  for (const name of ['x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-used', 'x-ratelimit-reset', 'retry-after']) {
    const value = integer(get(name));
    if (value !== null) headers[name] = value;
  }
  const resource = get('x-ratelimit-resource'), id = get('x-github-request-id');
  if (['core', 'search', 'code_search', 'graphql', 'integration_manifest'].includes(resource)) headers['x-ratelimit-resource'] = resource;
  if (typeof id === 'string' && /^[A-Fa-f0-9:]{1,128}$/.test(id)) headers['x-github-request-id'] = id;
  return headers;
}

async function refusalMessage(response) {
  // Inspect only a bounded JSON message internally, never retain provider prose.
  const reader = response.body?.getReader?.();
  if (!reader) return '';
  const chunks = []; let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 8192) return '';
      chunks.push(value);
    }
    const message = JSON.parse(Buffer.concat(chunks).toString('utf8')).message;
    return typeof message === 'string' ? message : '';
  } catch { return ''; }
  finally { await reader.cancel().catch(() => {}); }
}

async function classify(response, method, path, now) {
  const headers = safeHeaders(response), status = response.status;
  const message = [403, 429].includes(status) ? await refusalMessage(response) : '';
  let classification = 'http_error', retryAtMs = null;
  if (status === 403 && ['Resource not accessible by integration', 'Resource not accessible by personal access token'].includes(message)) {
    classification = 'permission';
  } else if ([403, 429].includes(status)) {
    if (headers['x-ratelimit-remaining'] === 0) classification = 'primary_rate_limit';
    else if (status === 429 || response.headers?.has?.('retry-after') || /secondary rate limit/i.test(message)) classification = 'secondary_rate_limit';
    else classification = 'unknown_forbidden';
    if (classification.endsWith('_rate_limit')) {
      const retry = headers['retry-after'], reset = headers['x-ratelimit-reset'];
      // Do not guess a shorter wait when an announced value is malformed.
      if (response.headers?.has?.('retry-after') && retry === undefined) retryAtMs = null;
      else if (classification === 'primary_rate_limit' && reset === undefined && retry === undefined) retryAtMs = null;
      else retryAtMs = Math.max(now + (retry ?? 0) * 1000,
        headers['x-ratelimit-remaining'] === 0 && reset !== undefined ? reset * 1000 + 1000 : 0,
        classification === 'secondary_rate_limit' && retry === undefined ? now + 60_000 : 0);
    }
  }
  const code = classification.endsWith('_rate_limit') ? 'rate_limited' : classification === 'permission' ? 'permission_denied' : classification;
  return { method, endpoint: safeEndpoint(path), status, code, classification, headers, retry_at_ms: retryAtMs };
}

export class PublishUnverified extends Unavailable {
  constructor(details, cause) {
    const refusal = githubFailureDetails(cause);
    const safeDetails = { ...details, ...(refusal ? { cause: refusal } : {}) };
    super(`Check Run publication unverified (publish_unverified): ${JSON.stringify(safeDetails)}`, refusal ? { cause } : undefined);
    this.code = 'publish_unverified';
    this.details = safeDetails;
  }
}

// A direct Check Run GET exposes these written fields, but not annotation
// contents. Fingerprint arbitrary strings: neither source nor provider prose
// may cross the diagnostic or durable finalizer-state boundary.
const fingerprint = value => typeof value === 'string' ? { bytes: Buffer.byteLength(value), sha256: digest(value) } : null;
const checkProjection = (run, fields) => Object.fromEntries(fields.map(field => {
  const value = field.split('.').reduce((object, key) => object?.[key], run);
  return [field, ['id', 'app.id'].includes(field) ? (Number.isSafeInteger(value) ? value : null) :
    field === 'head_sha' ? (validSha(value) ? value : null) :
    field === 'status' ? (['queued', 'in_progress', 'completed', 'waiting', 'pending', 'requested'].includes(value) ? value : null) :
    field === 'conclusion' ? (['success', 'failure', 'neutral', 'cancelled', 'timed_out', 'action_required', 'stale', 'skipped', 'startup_failure'].includes(value) ? value : null) :
    field.endsWith('_at') ? (typeof value === 'string' && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null) : fingerprint(value)];
}));
const identityFields = ['id', 'app.id', 'name', 'head_sha'];
const timestamp = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');

// A reused terminal observation has the same no-overwrite authority as our own
// attempted terminal write. Retain its verdict, completion time and output
// fingerprints across the review/finalize process boundary.
export const terminalCheckReceipt = (run, phase = 'reuse') => ({ phase, expected: checkProjection(run,
  [...identityFields, 'status', 'conclusion', 'completed_at', 'output.title', 'output.summary']) });

export class GitHub {
  constructor({ repository, readToken, checkToken, apiOrigin = 'https://api.github.com', fetcher = fetch,
    now = Date.now, wait = sleep, deadlineMs, onDiagnostic = details => process.stderr.write(`GitHub refusal: ${JSON.stringify(details)}\n`) }) {
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) throw new Unavailable('Invalid repository.');
    this.repository = repository;
    this.readToken = readToken;
    this.checkToken = checkToken;
    this.apiOrigin = apiOrigin.replace(/\/$/, '');
    this.fetcher = fetcher;
    this.now = now;
    this.wait = wait;
    this.deadlineMs = deadlineMs ?? now() + 35 * 60_000;
    this.onDiagnostic = onDiagnostic;
    this.sourceTrees = new Map();
  }

  async tree(sha, deadlineMs) {
    if (!this.sourceTrees.has(sha)) this.sourceTrees.set(sha, this.request(`/git/trees/${sha}`, { deadlineMs }));
    const tree = await this.sourceTrees.get(sha);
    if (tree.truncated || !Array.isArray(tree.tree)) throw new Unavailable('Pinned source tree is incomplete.');
    return tree.tree;
  }

  async request(path, { method = 'GET', body, checks = false, deadlineMs = this.deadlineMs } = {}) {
    if (this.rateFailure) throw this.rateFailure;
    const token = checks ? this.checkToken : this.readToken;
    if (!token) throw new Unavailable(`Missing ${checks ? 'checks' : 'source'} credential.`);
    deadlineMs = Math.min(deadlineMs, this.deadlineMs);
    const encodedBody = body === undefined ? undefined : JSON.stringify(body);
    for (let attempt = 0; ; attempt++) {
      const remaining = Math.min(requestTimeoutMs, deadlineMs - this.now());
      if (!(remaining > 0)) throw new Unavailable('Pinned source deadline expired.');
      let response;
      try {
        response = await this.fetcher(`${this.apiOrigin}/repos/${this.repository}${path}`, {
          method, redirect: 'error', signal: AbortSignal.timeout(Math.ceil(remaining)),
          headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json',
            'x-github-api-version': '2022-11-28', 'content-type': 'application/json' },
          ...(encodedBody === undefined ? {} : { body: encodedBody }),
        });
      } catch {
        // A lost response after POST/PATCH does not prove the write was rejected.
        throw unreadableResponse(method, path, 'transport failed');
      }
      if (response.ok) {
        try { return await response.json(); }
        catch {
          throw unreadableResponse(method, path, 'response could not be read');
        }
      }
      const details = { ...await classify(response, method, path, this.now()), attempt: attempt + 1 };
      const error = new GitHubHTTPError(details);
      try { this.onDiagnostic(details); } catch { /* Diagnostics cannot replace the provider refusal. */ }
      if (error.code !== 'rate_limited') throw error;
      // GitHub documents 403/429 rate responses as refusals and explicitly
      // permits retrying the request after its wait. Never retry a lost response.
      const delay = details.retry_at_ms === null ? null : Math.max(0, details.retry_at_ms - this.now());
      if (attempt || delay === null || delay + requestTimeoutMs + retryMarginMs >= deadlineMs - this.now()) {
        this.rateFailure = error; // Stop this delivery, including later cleanup calls.
        throw error;
      }
      await this.wait(delay);
      if (this.now() + requestTimeoutMs + retryMarginMs >= deadlineMs) {
        this.rateFailure = error;
        throw error;
      }
    }
  }

  async pages(path, field, { deadlineMs = this.deadlineMs } = {}) {
    const rows = [];
    for (let page = 1; ; page++) {
      const body = await this.request(`${path}${path.includes('?') ? '&' : '?'}per_page=100&page=${page}`,
        { checks: field === 'check_runs', deadlineMs });
      const items = field ? body[field] : body;
      if (!Array.isArray(items)) throw new Unavailable('Malformed GitHub list response.');
      rows.push(...items);
      if (items.length < 100) return rows;
    }
  }

  async source(path, sha, optional = false, { deadlineMs, maxBytes = Infinity, allowBinary = false } = {}) {
    if (!validSha(sha) || !validPath(path)) throw new Unavailable('Invalid pinned source reference.');
    let object, treeSha = sha;
    const segments = path.split('/');
    for (let i = 0; i < segments.length; i++) {
      object = (await this.tree(treeSha, deadlineMs)).find(entry => entry.path === segments[i]);
      if (!object) {
        if (optional) return null;
        throw new Unavailable(`Pinned source ${path} is absent.`);
      }
      if (i < segments.length - 1) {
        if (object.type !== 'tree' || !validSha(object.sha)) throw new Unavailable(`Source ${path} crosses a non-directory.`);
        treeSha = object.sha;
      }
    }
    // Contents follows internal symlinks. Git trees prove the path's actual
    // object/mode instead, so a link or submodule cannot masquerade as source.
    if (object.type !== 'blob' || !['100644', '100755'].includes(object.mode) || !validSha(object.sha)) {
      throw new Unavailable(`Source ${path} is not an ordinary file.`);
    }
    const blobSha = object.sha;
    if (object.size > maxBytes) throw new Unavailable('Pinned source exceeds the context recovery byte limit.');
    object = await this.request(`/git/blobs/${blobSha}`, { deadlineMs });
    if (object.size > maxBytes) throw new Unavailable('Pinned source exceeds the context recovery byte limit.');
    if (!Number.isSafeInteger(object.size) || object.size < 0 || object.encoding !== 'base64' || typeof object.content !== 'string') {
      throw new Unavailable(`Source ${path} has no complete content.`);
    }
    const bytes = Buffer.from(object.content, 'base64');
    const actualSha = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
    if (bytes.length !== object.size || actualSha !== blobSha) throw new Unavailable(`Source ${path} is incomplete.`);
    try {
      const content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
      if (content.includes('\0')) throw new Error('binary');
      return content;
    } catch {
      if (allowBinary) return bytes;
      throw new Unavailable(`Source ${path} is not UTF-8 text.`);
    }
  }

  async current(target, { deadlineMs } = {}) {
    if (target.kind === 'pull_request') {
      const pr = await this.request(`/pulls/${target.number}`, { deadlineMs });
      return pr.state === 'open' && pr.head.sha === target.head_sha && (!target.base_ref || pr.base.ref === target.base_ref);
    }
    const ref = await this.request(`/git/ref/heads/${target.ref.split('/').map(encodeURIComponent).join('/')}`, { deadlineMs });
    return ref.object?.sha === target.head_sha;
  }

  async checkRuns(sha, { deadlineMs = this.deadlineMs } = {}) {
    return this.pages(`/commits/${sha}/check-runs?check_name=${encodeURIComponent(CHECK_NAME)}&filter=all`, 'check_runs', { deadlineMs });
  }

  async begin(target, externalId, detailsUrl, { restartCompleted = false, inputIdentity, onCreated, deadlineMs = this.deadlineMs } = {}) {
    const runs = await this.checkRuns(target.head_sha, { deadlineMs });
    const existing = runs.filter(r => r.app?.id === APP_ID && r.name === CHECK_NAME &&
      r.head_sha === target.head_sha).sort((a, b) => b.id - a.id)[0];
    if (existing?.status === 'completed' && !restartCompleted) return existing;
    if (existing && existing.status !== 'completed' && typeof inputIdentity === 'string' &&
        typeof existing.output?.summary === 'string' && existing.output.summary.startsWith(inputIdentity)) {
      const runId = value => {
        try {
          const url = new URL(value), match = url.pathname.match(/^\/([^/]+)\/([^/]+)\/actions\/runs\/([1-9][0-9]*)(?:\/attempts\/[1-9][0-9]*)?\/?$/);
          return url.origin === 'https://github.com' && match && `${match[1]}/${match[2]}` === this.repository ? match[3] : null;
        } catch { return null; }
      };
      const owner = runId(existing.details_url), current = runId(detailsUrl);
      let live = false;
      if (owner) {
        try {
          const workflow = await this.request(`/actions/runs/${owner}`, { deadlineMs });
          live = String(workflow.id) === owner && ['queued', 'in_progress', 'waiting', 'pending', 'requested'].includes(workflow.status);
        } catch (error) { if (githubRefusal(error)) throw error; }
      }
      if (live && owner !== current) return { ...existing, deferred: true, owner_run_id: owner };
      // A delivery may reconcile its own accepted creation. Completed owners,
      // missing/unknown ownership and read errors instead need a fresh attempt.
      if (live && owner === current && existing.external_id === externalId) return existing;
    }
    // POST retries only a conclusive rate refusal. After an ambiguous transport result the next delivery
    // finds this stable external_id before attempting another creation.
    const body = {
      name: CHECK_NAME, head_sha: target.head_sha, external_id: externalId,
      details_url: detailsUrl, status: 'in_progress', started_at: timestamp(),
      output: { title: 'Review running', summary: `${inputIdentity ?? ''}Reviewing exact head \`${target.head_sha}\`.` },
    };
    const created = await this.request('/check-runs', { method: 'POST', checks: true, body, deadlineMs });
    // The accepted POST response is the created resource's authoritative receipt.
    // Persist its identity before an eventually consistent GET can fail.
    await onCreated?.(created);
    const expected = checkProjection({ ...body, id: created.id, app: { id: APP_ID } },
      [...identityFields, 'external_id', 'details_url', 'status', 'started_at', 'output.title', 'output.summary']);
    return this.reconcile({ phase: 'create', expected }, { deadlineMs });
  }

  async reconcile(publication, { deadlineMs = this.deadlineMs } = {}) {
    const { phase, expected } = publication;
    deadlineMs = Math.min(deadlineMs, this.deadlineMs, this.now() + 10_000);
    let terminalCause;
    let observed = null, failure = 'deadline', httpStatus = null, attempts = 0;
    const fields = Object.keys(expected);
    for (; attempts < 3 && this.now() < deadlineMs;) {
      attempts++;
      try {
        const readback = await this.request(`/check-runs/${expected.id}`, { checks: true, deadlineMs });
        observed = checkProjection(readback, fields);
        if (fields.every(field => JSON.stringify(observed[field]) === JSON.stringify(expected[field]))) return readback;
        failure = 'mismatch'; httpStatus = null;
      } catch (error) {
        failure = error instanceof GitHubHTTPError ? 'http' : 'read_failed';
        httpStatus = error instanceof GitHubHTTPError ? error.status : null;
        if (githubTerminalFailure(error)) { terminalCause = error; break; }
        if (httpStatus !== null && httpStatus !== 404 && httpStatus < 500) break;
      }
      const remaining = deadlineMs - this.now();
      if (attempts < 3 && remaining > 0) await this.wait(Math.min(1000, remaining));
    }
    throw new PublishUnverified({ phase, check_run_id: expected.id, attempts, failure, http_status: httpStatus,
      expected, observed, mismatched_fields: observed === null ? [] : fields.filter(field =>
        JSON.stringify(observed[field]) !== JSON.stringify(expected[field])) }, terminalCause);
  }

  async finish(run, conclusion, output, { onPublish, deadlineMs = this.deadlineMs } = {}) {
    const body = { status: 'completed', conclusion, completed_at: timestamp(), output };
    const publication = terminalCheckReceipt({ ...body,
      id: run.id, head_sha: run.head_sha, name: CHECK_NAME, app: { id: APP_ID } }, 'finish');
    // Save the intended result before the only terminal PATCH. The always-run
    // finalizer can reconcile it read-only even after a crash or stale GET.
    await onPublish?.(publication);
    try {
      await this.request(`/check-runs/${run.id}`, { method: 'PATCH', checks: true, body, deadlineMs });
    } catch (error) {
      throw new PublishUnverified({ phase: 'finish', check_run_id: run.id, attempts: 0,
        failure: 'write_unverified', http_status: error instanceof GitHubHTTPError ? error.status : null,
        expected: publication.expected, observed: null, mismatched_fields: [] }, error);
    }
    return this.reconcile(publication, { deadlineMs });
  }
}

// Diff only two data files. External diff/text converters and caller Git
// configuration are disabled; repository programs are never checked out/run.
export function sourcePatch(before, after, context = 3) {
  const dir = mkdtempSync(join(tmpdir(), 'pr-review-'));
  try {
    const a = join(dir, 'base'), b = join(dir, 'head');
    writeFileSync(a, before ?? '', { mode: 0o600 });
    writeFileSync(b, after ?? '', { mode: 0o600 });
    try {
      return execFileSync('git', ['diff', '--no-index', '--no-ext-diff', '--no-textconv', `--unified=${context}`, '--', a, b], {
        encoding: 'utf8', maxBuffer: 100 * 1024 * 1024,
        env: { PATH: process.env.PATH, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', LC_ALL: 'C' },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      if (error.status === 1 && typeof error.stdout === 'string') {
        return error.stdout.replaceAll(a, 'base').replaceAll(b, 'head');
      }
      throw new Unavailable('Could not compute the complete pinned source diff.');
    }
  } finally { rmSync(dir, { recursive: true }); }
}

export function hunkRanges(patch) {
  return [...patch.matchAll(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/gm)].map(m => ({
    base: { start: +m[1], count: m[2] === undefined ? 1 : +m[2] },
    head: { start: +m[3], count: m[4] === undefined ? 1 : +m[4] },
  }));
}

export const bundleFile = segmentBundleFile;
