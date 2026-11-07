import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { segmentBundleFile } from './source.mjs';

export const CHECK_NAME = 'Sylphx PR Review';
export const APP_ID = 1299545;
export const digest = value => createHash('sha256').update(value).digest('hex');
export const validSha = value => typeof value === 'string' && /^[a-f0-9]{40}$/.test(value);
export const validPath = value => typeof value === 'string' && value.length > 0 &&
  !value.startsWith('/') && !value.split('/').some(p => !p || p === '.' || p === '..') &&
  !/[\0\r\n\\]/.test(value);

export class Unavailable extends Error {}

export class GitHub {
  constructor({ repository, readToken, checkToken, apiOrigin = 'https://api.github.com', fetcher = fetch }) {
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) throw new Unavailable('Invalid repository.');
    this.repository = repository;
    this.readToken = readToken;
    this.checkToken = checkToken;
    this.apiOrigin = apiOrigin.replace(/\/$/, '');
    this.fetcher = fetcher;
    this.sourceTrees = new Map();
  }

  async tree(sha, deadlineMs) {
    if (!this.sourceTrees.has(sha)) this.sourceTrees.set(sha, this.request(`/git/trees/${sha}`, { deadlineMs }));
    const tree = await this.sourceTrees.get(sha);
    if (tree.truncated || !Array.isArray(tree.tree)) throw new Unavailable('Pinned source tree is incomplete.');
    return tree.tree;
  }

  async request(path, { method = 'GET', body, checks = false, deadlineMs = Infinity } = {}) {
    const token = checks ? this.checkToken : this.readToken;
    if (!token) throw new Unavailable(`Missing ${checks ? 'checks' : 'source'} credential.`);
    const remaining = Math.min(30_000, deadlineMs - Date.now());
    if (!(remaining > 0)) throw new Unavailable('Pinned source deadline expired.');
    const response = await this.fetcher(`${this.apiOrigin}/repos/${this.repository}${path}`, {
      method, redirect: 'error', signal: AbortSignal.timeout(remaining),
      headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json',
        'x-github-api-version': '2022-11-28', 'content-type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      // Never log a response body: a provider can echo a credential or source.
      throw new Unavailable(`GitHub ${method} ${path.split('?')[0]} returned HTTP ${response.status}.`);
    }
    return response.json();
  }

  async pages(path, field) {
    const rows = [];
    for (let page = 1; ; page++) {
      const body = await this.request(`${path}${path.includes('?') ? '&' : '?'}per_page=100&page=${page}`,
        { checks: field === 'check_runs' });
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

  async checkRuns(sha) {
    return this.pages(`/commits/${sha}/check-runs?check_name=${encodeURIComponent(CHECK_NAME)}&filter=all`, 'check_runs');
  }

  async begin(target, externalId, detailsUrl, { restartCompleted = false, inputIdentity } = {}) {
    const runs = await this.checkRuns(target.head_sha);
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
          const workflow = await this.request(`/actions/runs/${owner}`);
          live = String(workflow.id) === owner && ['queued', 'in_progress', 'waiting', 'pending', 'requested'].includes(workflow.status);
        } catch {}
      }
      if (live && owner !== current) return { ...existing, deferred: true, owner_run_id: owner };
      // A delivery may reconcile its own accepted creation. Completed owners,
      // missing/unknown ownership and read errors instead need a fresh attempt.
      if (live && owner === current && existing.external_id === externalId) return existing;
    }
    // POST has no retry. After an ambiguous transport result the next delivery
    // finds this stable external_id before attempting another creation.
    const created = await this.request('/check-runs', { method: 'POST', checks: true, body: {
      name: CHECK_NAME, head_sha: target.head_sha, external_id: externalId,
      details_url: detailsUrl, status: 'in_progress', started_at: new Date().toISOString(),
      output: { title: 'Review running', summary: `${inputIdentity ?? ''}Reviewing exact head \`${target.head_sha}\`.` },
    } });
    return this.request(`/check-runs/${created.id}`, { checks: true });
  }

  async finish(run, conclusion, output) {
    await this.request(`/check-runs/${run.id}`, { method: 'PATCH', checks: true, body: {
      status: 'completed', conclusion, completed_at: new Date().toISOString(), output,
    } });
    const readback = await this.request(`/check-runs/${run.id}`, { checks: true });
    if (readback.app?.id !== APP_ID || readback.head_sha !== run.head_sha ||
        readback.conclusion !== conclusion || readback.status !== 'completed') {
      throw new Unavailable('Check Run readback did not match the published result.');
    }
    return readback;
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
